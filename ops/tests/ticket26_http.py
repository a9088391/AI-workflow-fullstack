"""Ticket 26：僅對 ticket25_test 隔離資料執行 Gin 作者操作驗收。"""

import json
import base64
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from ticket25_http import GIN, LARAVEL, call, check, expect, guard as ticket25_guard, sign_token, sql
import ticket25_http

ticket25_http.MESSAGES["E0201"] = "內容已更新，請確認最新內容後再送出"


# guard 延用已驗證的專案、資料卷及金鑰檢查，另核實 NGINX 實際入口。
def guard():
    ticket25_guard()
    ids = subprocess.check_output(
        ["docker", "compose", "--profile", "gin", "ps", "-q", "nginx"], text=True
    ).splitlines()
    check(len(ids) == 1, "隔離 NGINX 容器未唯一啟動")
    info = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    labels = info["Config"]["Labels"]
    check(labels.get("com.docker.compose.project") == "ticket25_test"
          and labels.get("com.docker.compose.service") == "nginx", "NGINX 屬於其他專案")
    check(info["State"]["Running"], "NGINX 未執行")
    check(info["HostConfig"]["PortBindings"].get("80/tcp") ==
          [{"HostIp": "127.0.0.1", "HostPort": "8090"}], "NGINX 實際埠綁定不符")
    db_id = subprocess.check_output(
        ["docker", "compose", "--profile", "gin", "ps", "-q", "db"], text=True
    ).strip()
    db = json.loads(subprocess.check_output(["docker", "inspect", db_id], text=True))[0]
    check(db["Config"]["Labels"].get("com.docker.compose.service") == "db"
          and db["State"]["Running"] and
          db["State"].get("Health", {}).get("Status") == "healthy", "DB 實際身分或健康狀態不符")


# literal 只組合本腳本生成的 fixture 字串，避免 SQL 測試查詢混入引號。
def literal(value):
    return "'" + value.replace("'", "''") + "'"


# one 根據唯一 nonce 和作者取建立 ID，不猜資料庫 sequence。
def one(table, author, column, value):
    check(table in ("articles", "comments") and column in ("title", "body"), "不允許的查詢欄")
    ids = sql(f"SELECT id FROM {table} WHERE author_id={int(author)} AND {column}={literal(value)}").splitlines()
    check(len(ids) == 1, "建立項目未唯一辨識")
    return int(ids[0])


# state 取得測試斷言所需的持久欄位，與 HTTP 結果交叉驗證。
def state(table, item_id):
    check(table in ("articles", "comments"), "不允許的資料表")
    title = "title," if table == "articles" else ""
    raw = sql("SELECT row_to_json(t) FROM (SELECT author_id,body,visibility,restore_control,"
              f"{title}version,created_backend,created_at=updated_at AS same_time,updated_at::text AS updated "
              f"FROM {table} WHERE id={int(item_id)}) t")
    return json.loads(raw)


# create_user 建立本次唯一帳號；管理員角色僅在隔離資料庫調整。
def create_user(tag, admin=False):
    email = f"t26-{tag}@example.test"
    expect(call(GIN, "POST", "/api/auth/register",
                {"email": email, "password": "abcd1234", "nickname": "Ticket26"}), 201)
    login = call(GIN, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"})
    check(login[0] == 200, "Gin fixture 登入失敗")
    data = expect(login, 200)
    user_id = data["user"]["id"]
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE id={int(user_id)}")
    return {"id": user_id, "email": email, "token": data["access_token"]}


# page 核對個人頁完整 ID 與精確總數，包含超頁及不可見內容。
def page(kind, actor, ids):
    path = f"/api/me/{kind}"
    for number, expected in ((1, ids[:1]), (2, ids[1:2]), (999, [])):
        data = expect(call(GIN, "GET", f"{path}?page={number}&per_page=1",
                           token=actor["token"]), 200)
        check([item["id"] for item in data["list"]] == expected, "個人列表 ID 或排序不符")
        check(data["pagination"] == {"page": number, "per_page": 1, "total": len(ids),
                                      "total_pages": len(ids)}, "個人列表分頁不符")
    check(sql(f"SELECT count(*) FROM {kind} WHERE author_id={int(actor['id'])}") == str(len(ids)),
          "個人 fixture 數量不符")


# wait_lock 等待後端查詢真的阻塞，避免只靠固定 sleep 假稱發生競爭。
def wait_lock(minimum=1, fragment=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        query = "SELECT count(*) FROM pg_stat_activity WHERE usename='articles_app' AND wait_event_type='Lock'"
        if fragment is not None:
            query += " AND query LIKE " + literal("%" + fragment + "%")
        count = int(sql(query))
        if count >= minimum:
            return
        time.sleep(0.05)
    raise AssertionError("未觀察到指定數量的後端鎖等待")


# locked_sql 以一次性 psql 交易先持鎖再提交；只對已驗證的隔離 DB 操作。
def locked_sql(lock, update, seconds=6):
    check(seconds in (6, 8), "不支援的受控鎖時間")
    command = ["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c",
               f"SET application_name='ticket26_holder'; BEGIN; {lock}; SELECT pg_sleep({seconds}); {update}; COMMIT;"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, error = process.communicate()
            raise AssertionError("隔離鎖交易未持鎖：" + error.strip())
        if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='ticket26_holder' "
               "AND wait_event='PgSleep'") == "1":
            return process
        time.sleep(0.05)
    process.terminate()
    process.communicate()
    raise AssertionError("隔離鎖交易未進入持鎖等待")


# finish_lock 確認受控 SQL 交易提交成功，不讓後續結果建立在未知資料狀態。
def finish_lock(process):
    output, error = process.communicate(timeout=12)
    check(process.returncode == 0, "隔離鎖交易失敗：" + error.strip())
    return output


# plans 執行與 Gin 個人列表相同的 WHERE/JOIN/ORDER/LIMIT，保留實測計畫。
def plans(user_id):
    columns = {
        "articles": "articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname",
        "comments": "comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname",
    }
    for table in ("articles", "comments"):
        base = f" FROM {table} JOIN accounts ON accounts.id={table}.author_id WHERE {table}.author_id={int(user_id)}"
        print(f"FIXTURE {table} owned={sql(f'SELECT count(*){base}')} total={sql(f'SELECT count(*) FROM {table}')}")
        for name, query in (("count", "SELECT count(*)" + base),
                            ("list", "SELECT " + columns[table] + base +
                             f" ORDER BY {table}.created_at DESC,{table}.id DESC LIMIT 20 OFFSET 0")):
            print(f"PLAN {table} {name}:\n{sql('EXPLAIN (ANALYZE, BUFFERS) ' + query)}")


# expiry_race 用符合八小時 claims 的短剩餘時間 session 重現等鎖後到期與停用的優先順序。
def expiry_race():
    nonce = secrets.token_hex(6)
    author = create_user(nonce + "-expiry")
    title = "到期競爭 " + nonce
    expect(call(GIN, "POST", "/api/articles", {"title": title, "body": "原文"}, author["token"]), 201)
    article = one("articles", author["id"], "title", title)
    before = state("articles", article)
    # 保持 SEC05 的固定八小時效期，並讓到期點落在稍後的持鎖期間；session 時間須與 claims 完全相符。
    current = int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"))
    issued = current - 28800 + 5
    expires = issued + 28800
    jti = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    claims = {"iss": "ai-workflow-fullstack:gin", "aud": "ai-workflow-fullstack-api:gin",
              "sub": str(author["id"]), "jti": jti, "iat": issued, "nbf": issued, "exp": expires}
    sql("INSERT INTO login_sessions (jti,account_id,backend_id,issued_at,expires_at) VALUES ("
        f"{literal(jti)},{int(author['id'])},'gin',to_timestamp({issued}),to_timestamp({expires}))")
    token = sign_token({"alg": "HS256", "typ": "JWT"}, claims)
    expect(call(GIN, "GET", "/api/me", token=token), 200)
    # 先鎖帳號八秒，讓已通過外層驗證的寫入等待；提交時同時把此測試帳號停用。
    lock = locked_sql(f"SELECT id FROM accounts WHERE id={int(author['id'])} FOR UPDATE",
                      f"UPDATE accounts SET status='disabled' WHERE id={int(author['id'])}", seconds=8)
    try:
        check(int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) < expires,
              "短效 token 在受控請求前已到期")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, GIN, "PUT", f"/api/articles/{article}",
                                  {"title": "不應保存", "body": "原文", "version": 1}, token)
            # 此查詢只會在外層 JWT/DB 驗證完成後的 writeTx 執行。
            wait_lock(fragment="SELECT id,role,status FROM accounts")
            finish_lock(lock)
            result = pending.result()
    finally:
        # 若等待或斷言失敗，結束仍在執行的持鎖交易，避免測試留下鎖阻塞其他案例。
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    # 先確認到期、停用及文章未變都已落實，再判斷業務錯誤碼的優先順序。
    expired = int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) >= expires
    disabled = sql(f"SELECT status FROM accounts WHERE id={int(author['id'])}") == "disabled"
    unchanged = state("articles", article) == before
    print(f"COD-001 fixture={nonce} expired={expired} disabled={disabled} "
          f"HTTP={result[0]} code={result[1].get('error_code')} unchanged={unchanged}")
    check(expired and disabled and unchanged, "到期競爭的時間或持久資料前提不成立")
    expect(result, 401, "E0002")


# main 先守衛，再用唯一資料驗證跨後端、權限、版本及受控競爭。
def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    check(len(sys.argv) <= 2 and (len(sys.argv) == 1 or sys.argv[1] in ("--guard-only", "--cod-001-only")), "不支援此參數")
    guard()
    if len(sys.argv) == 2 and sys.argv[1] == "--guard-only":
        print("PASS ticket25_test guard（含 DB/Gin/Laravel/NGINX）")
        return
    if len(sys.argv) == 2:
        expiry_race()
        print("PASS COD-001 到期與停用的等待寫入")
        return
    nonce = secrets.token_hex(6)
    print("FIXTURE t26-" + nonce)
    author = create_user(nonce + "-author")
    outsider = create_user(nonce + "-outsider")
    admin = create_user(nonce + "-admin", admin=True)
    title = "Gin 原文 " + nonce
    body = "原樣\r\n保留 😀"
    expect(call(GIN, "POST", "/api/articles", {"title": title, "body": body}, author["token"]), 201)
    article = one("articles", author["id"], "title", title)
    item = state("articles", article)
    check(item["created_backend"] == "gin" and item["same_time"] and item["version"] == 1
          and item["visibility"] == "visible" and item["body"] == body, "Gin 建文來源或初始狀態")
    other_title = "Gin 第二篇 " + nonce
    expect(call(GIN, "POST", "/api/articles", {"title": other_title, "body": "第二篇"}, author["token"]), 201)
    second_article = one("articles", author["id"], "title", other_title)
    page("articles", author, [second_article, article])
    laravel_article = expect(call(LARAVEL, "GET", f"/api/articles/{article}"), 200)
    check(laravel_article["created_backend"] == "gin" and laravel_article["body"] == body,
          "Laravel 未讀到 Gin 原文或來源")
    comment_body = "Gin 留言 " + nonce
    expect(call(GIN, "POST", f"/api/articles/{article}/comments", {"body": comment_body}, author["token"]), 201)
    comment = one("comments", author["id"], "body", comment_body)
    check(state("comments", comment)["created_backend"] == "gin", "Gin 留言來源")
    another_body = "第二留言 " + nonce
    expect(call(GIN, "POST", f"/api/articles/{article}/comments", {"body": another_body}, author["token"]), 201)
    second_comment = one("comments", author["id"], "body", another_body)
    page("comments", author, [second_comment, comment])
    check(expect(call(LARAVEL, "GET", f"/api/articles/{article}/comments?page=1&per_page=20"), 200)["list"][0]["created_backend"] == "gin", "Laravel 未讀到 Gin 留言來源")
    expect(call(GIN, "GET", f"/api/comments/{comment}", token=outsider["token"]), 403, "E0003")

    # 同值寫入保留資料庫時間，舊版本即使同值也必須衝突。
    before = state("articles", article)
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": title, "body": body, "version": 1}, author["token"]), 200)
    check(state("articles", article) == before, "文章同值改動版本或時間")
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": title + " 編修", "body": body, "version": 1}, author["token"]), 200)
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": title, "body": body, "version": 1}, author["token"]), 409, "E0201")
    check(state("articles", article)["version"] == 2, "文章版本未原子更新")
    before = state("comments", comment)
    expect(call(GIN, "PUT", f"/api/comments/{comment}",
                {"body": comment_body, "version": 1}, author["token"]), 200)
    check(state("comments", comment) == before, "留言同值改動版本或時間")
    edited_comment = comment_body + " 編修"
    expect(call(GIN, "PUT", f"/api/comments/{comment}",
                {"body": edited_comment, "version": 1}, author["token"]), 200)
    check(expect(call(GIN, "GET", f"/api/comments/{comment}", token=author["token"]), 200)["body"] == edited_comment,
          "留言獨立詳情未顯示編修")
    expect(call(GIN, "PUT", f"/api/comments/{comment}",
                {"body": edited_comment, "version": 1}, author["token"]), 409, "E0201")

    # 管理員下架作者內容會接管恢復權；管理員自己的內容仍依作者規則。
    expect(call(GIN, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 2}, author["token"]), 200)
    hidden_comment = state("comments", comment)
    expect(call(GIN, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 3}, author["token"]), 200)
    check(state("comments", comment) == hidden_comment, "留言同值下架改動版本或時間")
    check(expect(call(GIN, "GET", f"/api/comments/{comment}", token=author["token"]), 200)["can_restore"],
          "作者個別下架留言不可讀或恢復")
    expect(call(GIN, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 3}, admin["token"]), 200)
    check(state("comments", comment)["restore_control"] == "admin"
          and state("comments", comment)["version"] == 4, "管理員留言接管")
    expect(call(GIN, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "visible", "version": 4}, author["token"]), 403, "E0003")
    expect(call(GIN, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "visible", "version": 4}, admin["token"]), 200)
    expect(call(GIN, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 2}, author["token"]), 200)
    hidden_article = state("articles", article)
    expect(call(GIN, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 3}, author["token"]), 200)
    check(state("articles", article) == hidden_article, "文章同值下架改動版本或時間")
    expect(call(GIN, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 3}, admin["token"]), 200)
    check(state("articles", article)["restore_control"] == "admin", "管理員文章接管")
    expect(call(GIN, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "visible", "version": 4}, author["token"]), 403, "E0003")
    expect(call(GIN, "GET", f"/api/articles/{article}"), 404, "E0004")
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "x", "version": 1}, outsider["token"]), 404, "E0004")
    expect(call(GIN, "GET", f"/api/comments/{comment}", token=outsider["token"]), 404, "E0004")
    own_comment = expect(call(GIN, "GET", f"/api/comments/{comment}", token=author["token"]), 200)
    check(own_comment["article_id"] == article and own_comment["body"] == edited_comment,
          "父文不可讀時作者留言詳情洩漏或遺失")
    page("articles", author, [second_article, article])
    page("comments", author, [second_comment, comment])
    expect(call(GIN, "POST", f"/api/articles/{article}/comments",
                {"body": "不能新增"}, author["token"]), 403, "E0003")
    expect(call(GIN, "PUT", f"/api/comments/{comment}",
                {"body": "不能修改", "version": 5}, author["token"]), 403, "E0003")
    expect(call(GIN, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "visible", "version": 4}, admin["token"]), 200)
    admin_title = "管理員本人 " + nonce
    expect(call(GIN, "POST", "/api/articles", {"title": admin_title, "body": "本人"}, admin["token"]), 201)
    admin_article = one("articles", admin["id"], "title", admin_title)
    expect(call(GIN, "PUT", f"/api/articles/{admin_article}/visibility",
                {"visibility": "hidden", "version": 1}, admin["token"]), 200)
    check(state("articles", admin_article)["restore_control"] == "author", "管理員本人應依作者規則")
    expect(call(GIN, "PUT", f"/api/articles/{admin_article}/visibility",
                {"visibility": "visible", "version": 2}, admin["token"]), 200)
    admin_comment_body = "管理員本人留言 " + nonce
    expect(call(GIN, "POST", f"/api/articles/{admin_article}/comments",
                {"body": admin_comment_body}, admin["token"]), 201)
    admin_comment = one("comments", admin["id"], "body", admin_comment_body)
    expect(call(GIN, "PUT", f"/api/comments/{admin_comment}/visibility",
                {"visibility": "hidden", "version": 1}, admin["token"]), 200)
    check(state("comments", admin_comment)["restore_control"] == "author", "管理員本人留言應依作者規則")

    # 錯誤順序與原始 Query/JSON 保留在客戶端，避免 urllib 代為接受錯誤型態。
    expect(call(GIN, "PUT", f"/api/articles/{article}", {"title": "x", "body": "x", "version": 1}), 401, "E0002")
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "x", "version": 1}, outsider["token"]), 403, "E0003")
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "x", "version": 1}, admin["token"]), 403, "E0003")
    expect(call(GIN, "GET", "/api/me/articles?page=1&page=2&per_page=20", token=author["token"]),
           422, "E0001", {"page": ["此參數不可重複"]})
    expect(call(GIN, "GET", "/api/me/comments?page=1&per_page=20&x=1", token=author["token"]),
           422, "E0001", {"x": ["不支援此欄位"]})
    expect(call(GIN, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "y", "version": True}, author["token"]),
           422, "E0001", {"version": ["欄位型態不正確"]})
    expect(call(GIN, "POST", "/api/articles", {"title": "\u3000\r\n", "body": "x"}, author["token"]),
           422, "E0001", {"title": ["欄位格式或範圍不正確"]})
    nul = {"title": "x\x00", "body": "x"}
    check(b"\\u0000" in json.dumps(nul, ensure_ascii=False).encode(), "NUL 測試未序列化為 JSON escape")
    expect(call(GIN, "POST", "/api/articles", nul, author["token"]),
           422, "E0001", {"title": ["欄位格式或範圍不正確"]})
    expect(call(GIN, "POST", f"/api/articles/{article}/comments",
                {"body": "x\x00"}, author["token"]),
           422, "E0001", {"body": ["欄位格式或範圍不正確"]})
    expect(call(GIN, "PUT", f"/api/articles/{article}", "{bad", author["token"]), 400, "E0005")

    # 真正不同後端對同版本競爭；文章鎖先持有時第二個請求可停在前置帳號鎖。
    version = state("articles", second_article)["version"]
    # Laravel 登入也鎖帳號；先取得它自己的憑證，避免測試準備阻塞 Gin 的待測寫入。
    laravel_login = call(LARAVEL, "POST", "/api/auth/login",
                         {"email": author["email"], "password": "abcd1234"})
    check(laravel_login[0] == 200, "Laravel fixture 登入失敗")
    laravel_token = expect(laravel_login, 200)["access_token"]
    lock = locked_sql(f"SELECT id FROM articles WHERE id={second_article} FOR UPDATE", "SELECT 1")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            gin_write = pool.submit(call, GIN, "PUT", f"/api/articles/{second_article}",
                                    {"title": "Gin 競爭 " + nonce, "body": "第二篇", "version": version}, author["token"])
            laravel_write = pool.submit(call, LARAVEL, "PUT", f"/api/articles/{second_article}",
                                        {"title": "Laravel 競爭 " + nonce, "body": "第二篇", "version": version}, laravel_token)
            wait_lock(2)
            finish_lock(lock)
            results = [gin_write.result(), laravel_write.result()]
    finally:
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    check(sorted(result[0] for result in results) == [200, 409], "Gin/Laravel 同版本競爭結果")
    expect(next(result for result in results if result[0] == 409), 409, "E0201")
    check(state("articles", second_article)["version"] == version + 1, "跨後端版本增加不只一次")

    # 預先持有父文鎖並下架，等待中的留言建立與編修須在提交後重查公開狀態。
    race_body = "父文競爭 " + nonce
    lock = locked_sql(f"SELECT id FROM articles WHERE id={second_article} FOR UPDATE",
                      f"UPDATE articles SET visibility='hidden',restore_control='author',version=version+1,updated_at=clock_timestamp() WHERE id={second_article}")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, GIN, "POST", f"/api/articles/{second_article}/comments",
                                  {"body": race_body}, author["token"])
            wait_lock()
            finish_lock(lock)
            expect(pending.result(), 403, "E0003")
    finally:
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    check(sql(f"SELECT count(*) FROM comments WHERE body={literal(race_body)}") == "0", "下架後插入留言")

    # 同一父文章列亦保護既有留言內文，父文先下架時等待中的編修須失敗。
    comment_before = state("comments", second_comment)
    lock = locked_sql(f"SELECT id FROM articles WHERE id={article} FOR UPDATE",
                      f"UPDATE articles SET visibility='hidden',restore_control='author',version=version+1,updated_at=clock_timestamp() WHERE id={article}")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, GIN, "PUT", f"/api/comments/{second_comment}",
                                  {"body": "不應保存", "version": comment_before["version"]}, author["token"])
            wait_lock()
            finish_lock(lock)
            expect(pending.result(), 403, "E0003")
    finally:
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    check(state("comments", second_comment) == comment_before, "父文下架後仍編修留言")

    # 已撤銷 session 與等待帳號鎖的寫入競爭，提交後不得變更資料。
    before = state("articles", second_article)
    claims = json.loads(base64.urlsafe_b64decode(author["token"].split(".")[1] + "=="))
    jti = claims["jti"]
    lock = locked_sql(f"SELECT id FROM accounts WHERE id={int(author['id'])} FOR UPDATE",
                      f"UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE jti={literal(jti)}")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, GIN, "PUT", f"/api/articles/{second_article}",
                                  {"title": "不應保存", "body": "第二篇", "version": before["version"]},
                                  author["token"])
            wait_lock()
            finish_lock(lock)
            expect(pending.result(), 401, "E0002")
    finally:
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    check(state("articles", second_article) == before, "撤銷後仍寫入")
    expect(call(GIN, "PUT", f"/api/articles/{second_article}",
                {"title": "仍不可寫", "body": "第二篇", "version": before["version"]}, author["token"]),
           401, "E0002")
    expiry_race()
    plans(author["id"])
    print("PASS Ticket 26 Gin 作者 HTTP/SQL 驗收")


if __name__ == "__main__":
    main()
