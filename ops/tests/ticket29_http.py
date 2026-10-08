"""Ticket 29：僅對 ticket28_test 隔離資料執行 Echo 作者操作驗收。"""

import json
import base64
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from ticket28_http import ECHO, LARAVEL, call, check, expect, guard as ticket28_guard, sign_token, sql
import ticket28_http

ticket28_http.MESSAGES["E0201"] = "內容已更新，請確認最新內容後再送出"


# guard 延用隔離專案、資料卷及金鑰檢查，再確認 NGINX 與 Echo image 身分。
def guard():
    ticket28_guard()
    ids = subprocess.check_output(
        ["docker", "compose", "--profile", "echo", "ps", "-q", "nginx"], text=True
    ).splitlines()
    check(len(ids) == 1, "隔離 NGINX 容器未唯一啟動")
    info = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    labels = info["Config"]["Labels"]
    check(labels.get("com.docker.compose.project") == "ticket28_test"
          and labels.get("com.docker.compose.service") == "nginx", "NGINX 屬於其他專案")
    check(info["State"]["Running"], "NGINX 未執行")
    check(info["HostConfig"]["PortBindings"].get("80/tcp") ==
          [{"HostIp": "127.0.0.1", "HostPort": "18090"}], "NGINX 實際埠綁定不符")
    db_id = subprocess.check_output(
        ["docker", "compose", "--profile", "echo", "ps", "-q", "db"], text=True
    ).strip()
    db = json.loads(subprocess.check_output(["docker", "inspect", db_id], text=True))[0]
    check(db["Config"]["Labels"].get("com.docker.compose.service") == "db"
          and db["State"]["Running"] and
          db["State"].get("Health", {}).get("Status") == "healthy", "DB 實際身分或健康狀態不符")
    echo_id = subprocess.check_output(
        ["docker", "compose", "--profile", "echo", "ps", "-q", "echo"], text=True
    ).strip()
    running_image = json.loads(subprocess.check_output(["docker", "inspect", echo_id], text=True))[0]["Image"]
    built_image = subprocess.check_output(
        ["docker", "compose", "--profile", "echo", "images", "-q", "echo"], text=True
    ).strip()
    check(running_image == "sha256:" + built_image, "Echo 容器未使用目前 Compose image")


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
    email = f"t29-{tag}@example.test"
    expect(call(ECHO, "POST", "/api/auth/register",
                {"email": email, "password": "abcd1234", "nickname": "Ticket29"}), 201)
    login = call(ECHO, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"})
    check(login[0] == 200, "Echo fixture 登入失敗")
    data = expect(login, 200)
    user_id = data["user"]["id"]
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE id={int(user_id)}")
    return {"id": user_id, "email": email, "token": data["access_token"]}


# page 核對個人頁完整 ID 與精確總數，包含超頁及不可見內容。
def page(kind, actor, ids):
    path = f"/api/me/{kind}"
    for number, expected in ((1, ids[:1]), (2, ids[1:2]), (999, [])):
        data = expect(call(ECHO, "GET", f"{path}?page={number}&per_page=1",
                           token=actor["token"]), 200)
        check(set(data) == {"list", "pagination"}, "個人列表 data 形狀不符")
        check([item["id"] for item in data["list"]] == expected, "個人列表 ID 或排序不符")
        for item in data["list"]:
            fields = {"id", "body", "author", "version", "created_backend", "created_at",
                      "updated_at", "visibility", "can_restore"}
            fields |= {"title"} if kind == "articles" else {"article_id"}
            check(set(item) == fields and item["author"]["id"] == actor["id"]
                  and set(item["author"]) == {"id", "nickname"}, "個人列表資源形狀或作者不符")
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
               f"SET application_name='ticket29_holder'; BEGIN; {lock}; SELECT pg_sleep({seconds}); {update}; COMMIT;"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, error = process.communicate()
            raise AssertionError("隔離鎖交易未持鎖：" + error.strip())
        if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='ticket29_holder' "
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


# plans 執行與 Echo 個人列表相同的 WHERE/JOIN/ORDER/LIMIT，保留實測計畫。
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
    expect(call(ECHO, "POST", "/api/articles", {"title": title, "body": "原文"}, author["token"]), 201)
    article = one("articles", author["id"], "title", title)
    before = state("articles", article)
    # 保持 SEC05 的固定八小時效期，並讓到期點落在稍後的持鎖期間；session 時間須與 claims 完全相符。
    current = int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"))
    issued = current - 28800 + 5
    expires = issued + 28800
    jti = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    claims = {"iss": "ai-workflow-fullstack:echo", "aud": "ai-workflow-fullstack-api:echo",
              "sub": str(author["id"]), "jti": jti, "iat": issued, "nbf": issued, "exp": expires}
    sql("INSERT INTO login_sessions (jti,account_id,backend_id,issued_at,expires_at) VALUES ("
        f"{literal(jti)},{int(author['id'])},'echo',to_timestamp({issued}),to_timestamp({expires}))")
    token = sign_token({"alg": "HS256", "typ": "JWT"}, claims)
    expect(call(ECHO, "GET", "/api/me", token=token), 200)
    # 先鎖帳號八秒，讓已通過外層驗證的寫入等待；提交時同時把此測試帳號停用。
    lock = locked_sql(f"SELECT id FROM accounts WHERE id={int(author['id'])} FOR UPDATE",
                      f"UPDATE accounts SET status='disabled' WHERE id={int(author['id'])}", seconds=8)
    try:
        check(int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) < expires,
              "短效 token 在受控請求前已到期")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, ECHO, "PUT", f"/api/articles/{article}",
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


# disabled_race 讓寫入在帳號鎖後重讀停用狀態，驗證不能提交已失效會員的修改。
def disabled_race():
    nonce = secrets.token_hex(6)
    author = create_user(nonce + "-disabled")
    title = "停用競爭 " + nonce
    expect(call(ECHO, "POST", "/api/articles", {"title": title, "body": "原文"}, author["token"]), 201)
    article = one("articles", author["id"], "title", title)
    before = state("articles", article)
    lock = locked_sql(f"SELECT id FROM accounts WHERE id={int(author['id'])} FOR UPDATE",
                      f"UPDATE accounts SET status='disabled' WHERE id={int(author['id'])}")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, ECHO, "PUT", f"/api/articles/{article}",
                                  {"title": "不應保存", "body": "原文", "version": 1}, author["token"])
            wait_lock(fragment="SELECT id,role,status FROM accounts")
            finish_lock(lock)
            expect(pending.result(), 403, "E0102")
    finally:
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    check(state("articles", article) == before, "停用後仍寫入")


# hidden_edits 以獨立作者資料確認編修下架內容不會改變下架與恢復控制權。
def hidden_edits():
    nonce = secrets.token_hex(6)
    author = create_user(nonce + "-hidden")
    title = "下架編修 " + nonce
    comment_body = "個別下架 " + nonce
    expect(call(ECHO, "POST", "/api/articles", {"title": title, "body": "原文"}, author["token"]), 201)
    article = one("articles", author["id"], "title", title)
    expect(call(ECHO, "POST", f"/api/articles/{article}/comments",
                {"body": comment_body}, author["token"]), 201)
    comment = one("comments", author["id"], "body", comment_body)

    # 父文章仍公開時，作者可改個別下架的留言，但不能因此恢復它。
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 1}, author["token"]), 200)
    before_comment = state("comments", comment)
    check(before_comment["restore_control"] == "author" and before_comment["version"] == 2,
          "留言下架前提不符")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}",
                {"body": comment_body + " 編修", "version": 2}, author["token"]), 200)
    after_comment = state("comments", comment)
    check(after_comment["body"] == comment_body + " 編修" and after_comment["version"] == 3
          and after_comment["visibility"] == "hidden"
          and after_comment["restore_control"] == before_comment["restore_control"],
          "編修個別下架留言改變狀態或恢復權")

    # 文章下架後仍允許作者改內文；來源與恢復控制權都要沿用原值。
    expect(call(ECHO, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 1}, author["token"]), 200)
    before_article = state("articles", article)
    check(before_article["restore_control"] == "author" and before_article["version"] == 2,
          "文章下架前提不符")
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": title + " 編修", "body": "新版原文", "version": 2}, author["token"]), 200)
    after_article = state("articles", article)
    check(after_article["title"] == title + " 編修" and after_article["body"] == "新版原文"
          and after_article["version"] == 3 and after_article["visibility"] == "hidden"
          and after_article["restore_control"] == before_article["restore_control"]
          and after_article["created_backend"] == "echo", "編修下架文章改變狀態、來源或恢復權")


# main 先守衛，再用唯一資料驗證跨後端、權限、版本及受控競爭。
def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    check(len(sys.argv) <= 2 and (len(sys.argv) == 1 or sys.argv[1] in ("--guard-only", "--cod-001-only")), "不支援此參數")
    guard()
    if len(sys.argv) == 2 and sys.argv[1] == "--guard-only":
        print("PASS ticket28_test guard（含 DB/Echo/Laravel/NGINX/image）")
        return
    if len(sys.argv) == 2:
        expiry_race()
        print("PASS COD-001 到期與停用的等待寫入")
        return
    nonce = secrets.token_hex(6)
    print("FIXTURE t29-" + nonce)
    author = create_user(nonce + "-author")
    outsider = create_user(nonce + "-outsider")
    admin = create_user(nonce + "-admin", admin=True)
    title = "Echo 原文 " + nonce
    body = "原樣\r\n保留 😀"
    expect(call(ECHO, "POST", "/api/articles", {"title": title, "body": body}, author["token"]), 201)
    article = one("articles", author["id"], "title", title)
    item = state("articles", article)
    check(item["created_backend"] == "echo" and item["same_time"] and item["version"] == 1
          and item["visibility"] == "visible" and item["body"] == body, "Echo 建文來源或初始狀態")
    other_title = "Echo 第二篇 " + nonce
    expect(call(ECHO, "POST", "/api/articles", {"title": other_title, "body": "第二篇"}, author["token"]), 201)
    second_article = one("articles", author["id"], "title", other_title)
    page("articles", author, [second_article, article])
    laravel_article = expect(call(LARAVEL, "GET", f"/api/articles/{article}"), 200)
    check(laravel_article["created_backend"] == "echo" and laravel_article["body"] == body,
          "Laravel 未讀到 Echo 原文或來源")
    comment_body = "Echo 留言 " + nonce
    expect(call(ECHO, "POST", f"/api/articles/{article}/comments", {"body": comment_body}, author["token"]), 201)
    comment = one("comments", author["id"], "body", comment_body)
    check(state("comments", comment)["created_backend"] == "echo", "Echo 留言來源")
    another_body = "第二留言 " + nonce
    expect(call(ECHO, "POST", f"/api/articles/{article}/comments", {"body": another_body}, author["token"]), 201)
    second_comment = one("comments", author["id"], "body", another_body)
    page("comments", author, [second_comment, comment])
    check(expect(call(LARAVEL, "GET", f"/api/articles/{article}/comments?page=1&per_page=20"), 200)["list"][0]["created_backend"] == "echo", "Laravel 未讀到 Echo 留言來源")
    expect(call(ECHO, "GET", f"/api/comments/{comment}", token=outsider["token"]), 403, "E0003")
    admin_detail = expect(call(ECHO, "GET", f"/api/comments/{comment}", token=admin["token"]), 200)
    check(admin_detail["id"] == comment and admin_detail["article_id"] == article,
          "管理員無法讀取公開留言詳情")
    expect(call(ECHO, "GET", "/api/comments/9007199254740991", token=author["token"]), 404, "E0004")

    # Laravel 另建內容，再由 Echo 讀取；兩個方向都確認來源由實際建立者寫入。
    foreign_email = f"t29-{nonce}-laravel@example.test"
    expect(call(LARAVEL, "POST", "/api/auth/register",
                {"email": foreign_email, "password": "abcd1234", "nickname": "Ticket29 Laravel"}), 201)
    foreign_login = expect(call(LARAVEL, "POST", "/api/auth/login",
                                {"email": foreign_email, "password": "abcd1234"}), 200)
    foreign_id = foreign_login["user"]["id"]
    foreign_token = foreign_login["access_token"]
    foreign_title = "Laravel 文章給 Echo " + nonce
    expect(call(LARAVEL, "POST", "/api/articles",
                {"title": foreign_title, "body": "跨後端原文"}, foreign_token), 201)
    foreign_article = one("articles", foreign_id, "title", foreign_title)
    foreign_read = expect(call(ECHO, "GET", f"/api/articles/{foreign_article}"), 200)
    check(foreign_read["created_backend"] == "laravel" and foreign_read["body"] == "跨後端原文",
          "Echo 未讀到 Laravel 原文或來源")
    foreign_body = "Laravel 留言給 Echo " + nonce
    expect(call(LARAVEL, "POST", f"/api/articles/{foreign_article}/comments",
                {"body": foreign_body}, foreign_token), 201)
    foreign_comment = one("comments", foreign_id, "body", foreign_body)
    foreign_echo_token = expect(call(ECHO, "POST", "/api/auth/login",
                                     {"email": foreign_email, "password": "abcd1234"}), 200)["access_token"]
    foreign_detail = expect(call(ECHO, "GET", f"/api/comments/{foreign_comment}",
                                 token=foreign_echo_token), 200)
    check(foreign_detail["created_backend"] == "laravel" and foreign_detail["body"] == foreign_body,
          "Echo 未讀到 Laravel 留言或來源")

    # 同值寫入保留資料庫時間，舊版本即使同值也必須衝突。
    before = state("articles", article)
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": title, "body": body, "version": 1}, author["token"]), 200)
    check(state("articles", article) == before, "文章同值改動版本或時間")
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": title + " 編修", "body": body, "version": 1}, author["token"]), 200)
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": title, "body": body, "version": 1}, author["token"]), 409, "E0201")
    check(state("articles", article)["version"] == 2, "文章版本未原子更新")
    before = state("comments", comment)
    expect(call(ECHO, "PUT", f"/api/comments/{comment}",
                {"body": comment_body, "version": 1}, author["token"]), 200)
    check(state("comments", comment) == before, "留言同值改動版本或時間")
    edited_comment = comment_body + " 編修"
    expect(call(ECHO, "PUT", f"/api/comments/{comment}",
                {"body": edited_comment, "version": 1}, author["token"]), 200)
    check(expect(call(ECHO, "GET", f"/api/comments/{comment}", token=author["token"]), 200)["body"] == edited_comment,
          "留言獨立詳情未顯示編修")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}",
                {"body": edited_comment, "version": 1}, author["token"]), 409, "E0201")

    # 管理員下架作者內容會接管恢復權；管理員自己的內容仍依作者規則。
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 2}, author["token"]), 200)
    hidden_comment = state("comments", comment)
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 3}, author["token"]), 200)
    check(state("comments", comment) == hidden_comment, "留言同值下架改動版本或時間")
    check(expect(call(ECHO, "GET", f"/api/comments/{comment}", token=author["token"]), 200)["can_restore"],
          "作者個別下架留言不可讀或恢復")
    expect(call(ECHO, "GET", f"/api/comments/{comment}", token=outsider["token"]), 404, "E0004")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 3}, admin["token"]), 200)
    check(state("comments", comment)["restore_control"] == "admin"
          and state("comments", comment)["version"] == 4, "管理員留言接管")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "visible", "version": 4}, author["token"]), 403, "E0003")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "visible", "version": 4}, admin["token"]), 200)
    expect(call(ECHO, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 2}, author["token"]), 200)
    hidden_article = state("articles", article)
    expect(call(ECHO, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 3}, author["token"]), 200)
    check(state("articles", article) == hidden_article, "文章同值下架改動版本或時間")
    expect(call(ECHO, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 3}, admin["token"]), 200)
    check(state("articles", article)["restore_control"] == "admin", "管理員文章接管")
    expect(call(ECHO, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "visible", "version": 4}, author["token"]), 403, "E0003")
    expect(call(ECHO, "GET", f"/api/articles/{article}"), 404, "E0004")
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "x", "version": 1}, outsider["token"]), 404, "E0004")
    expect(call(ECHO, "GET", f"/api/comments/{comment}", token=outsider["token"]), 404, "E0004")
    own_comment = expect(call(ECHO, "GET", f"/api/comments/{comment}", token=author["token"]), 200)
    check(own_comment["article_id"] == article and own_comment["body"] == edited_comment,
          "父文不可讀時作者留言詳情洩漏或遺失")
    check(expect(call(ECHO, "GET", f"/api/comments/{comment}", token=admin["token"]), 200)["id"] == comment,
          "父文下架時管理員留言詳情不可讀")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "hidden", "version": 5}, author["token"]), 200)
    expect(call(ECHO, "PUT", f"/api/comments/{comment}/visibility",
                {"visibility": "visible", "version": 6}, author["token"]), 200)
    check(state("comments", comment)["visibility"] == "visible", "父文下架時恢復留言失敗")
    expect(call(ECHO, "GET", f"/api/comments/{comment}", token=outsider["token"]), 404, "E0004")
    page("articles", author, [second_article, article])
    page("comments", author, [second_comment, comment])
    expect(call(ECHO, "POST", f"/api/articles/{article}/comments",
                {"body": "不能新增"}, author["token"]), 403, "E0003")
    expect(call(ECHO, "PUT", f"/api/comments/{comment}",
                {"body": "不能修改", "version": 5}, author["token"]), 403, "E0003")
    expect(call(ECHO, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "visible", "version": 4}, admin["token"]), 200)
    admin_title = "管理員本人 " + nonce
    expect(call(ECHO, "POST", "/api/articles", {"title": admin_title, "body": "本人"}, admin["token"]), 201)
    admin_article = one("articles", admin["id"], "title", admin_title)
    expect(call(ECHO, "PUT", f"/api/articles/{admin_article}/visibility",
                {"visibility": "hidden", "version": 1}, admin["token"]), 200)
    check(state("articles", admin_article)["restore_control"] == "author", "管理員本人應依作者規則")
    expect(call(ECHO, "PUT", f"/api/articles/{admin_article}/visibility",
                {"visibility": "visible", "version": 2}, admin["token"]), 200)
    admin_comment_body = "管理員本人留言 " + nonce
    expect(call(ECHO, "POST", f"/api/articles/{admin_article}/comments",
                {"body": admin_comment_body}, admin["token"]), 201)
    admin_comment = one("comments", admin["id"], "body", admin_comment_body)
    expect(call(ECHO, "PUT", f"/api/comments/{admin_comment}/visibility",
                {"visibility": "hidden", "version": 1}, admin["token"]), 200)
    check(state("comments", admin_comment)["restore_control"] == "author", "管理員本人留言應依作者規則")

    # 錯誤順序與原始 Query/JSON 保留在客戶端，避免 urllib 代為接受錯誤型態。
    expect(call(ECHO, "PUT", f"/api/articles/{article}", {"title": "x", "body": "x", "version": 1}), 401, "E0002")
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "x", "version": 1}, outsider["token"]), 403, "E0003")
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "x", "version": 1}, admin["token"]), 403, "E0003")
    expect(call(ECHO, "GET", "/api/me/articles?page=1&page=2&per_page=20", token=author["token"]),
           422, "E0001", {"page": ["此參數不可重複"]})
    expect(call(ECHO, "GET", "/api/me/comments?page=1&per_page=20&x=1", token=author["token"]),
           422, "E0001", {"x": ["不支援此欄位"]})
    expect(call(ECHO, "PUT", f"/api/articles/{article}",
                {"title": "x", "body": "y", "version": True}, author["token"]),
           422, "E0001", {"version": ["欄位型態不正確"]})
    expect(call(ECHO, "POST", "/api/articles", {"title": "\u3000\r\n", "body": "x"}, author["token"]),
           422, "E0001", {"title": ["欄位格式或範圍不正確"]})
    nul = {"title": "x\x00", "body": "x"}
    check(b"\\u0000" in json.dumps(nul, ensure_ascii=False).encode(), "NUL 測試未序列化為 JSON escape")
    expect(call(ECHO, "POST", "/api/articles", nul, author["token"]),
           422, "E0001", {"title": ["欄位格式或範圍不正確"]})
    expect(call(ECHO, "POST", f"/api/articles/{article}/comments",
                {"body": "x\x00"}, author["token"]),
           422, "E0001", {"body": ["欄位格式或範圍不正確"]})
    expect(call(ECHO, "PUT", f"/api/articles/{article}", "{bad", author["token"]), 400, "E0005")

    # 真正不同後端對同版本競爭；文章鎖先持有時第二個請求可停在前置帳號鎖。
    version = state("articles", second_article)["version"]
    # Laravel 登入也鎖帳號；先取得它自己的憑證，避免測試準備阻塞 Echo 的待測寫入。
    laravel_login = call(LARAVEL, "POST", "/api/auth/login",
                         {"email": author["email"], "password": "abcd1234"})
    check(laravel_login[0] == 200, "Laravel fixture 登入失敗")
    laravel_token = expect(laravel_login, 200)["access_token"]
    lock = locked_sql(f"SELECT id FROM articles WHERE id={second_article} FOR UPDATE", "SELECT 1")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            echo_write = pool.submit(call, ECHO, "PUT", f"/api/articles/{second_article}",
                                    {"title": "Echo 競爭 " + nonce, "body": "第二篇", "version": version}, author["token"])
            laravel_write = pool.submit(call, LARAVEL, "PUT", f"/api/articles/{second_article}",
                                        {"title": "Laravel 競爭 " + nonce, "body": "第二篇", "version": version}, laravel_token)
            wait_lock(2)
            finish_lock(lock)
            results = [echo_write.result(), laravel_write.result()]
    finally:
        if lock.poll() is None:
            lock.terminate()
            lock.communicate()
    check(sorted(result[0] for result in results) == [200, 409], "Echo/Laravel 同版本競爭結果")
    expect(next(result for result in results if result[0] == 409), 409, "E0201")
    check(state("articles", second_article)["version"] == version + 1, "跨後端版本增加不只一次")

    # 預先持有父文鎖並下架，等待中的留言建立與編修須在提交後重查公開狀態。
    race_body = "父文競爭 " + nonce
    lock = locked_sql(f"SELECT id FROM articles WHERE id={second_article} FOR UPDATE",
                      f"UPDATE articles SET visibility='hidden',restore_control='author',version=version+1,updated_at=clock_timestamp() WHERE id={second_article}")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, ECHO, "POST", f"/api/articles/{second_article}/comments",
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
            pending = pool.submit(call, ECHO, "PUT", f"/api/comments/{second_comment}",
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
            pending = pool.submit(call, ECHO, "PUT", f"/api/articles/{second_article}",
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
    expect(call(ECHO, "PUT", f"/api/articles/{second_article}",
                {"title": "仍不可寫", "body": "第二篇", "version": before["version"]}, author["token"]),
           401, "E0002")
    expiry_race()
    disabled_race()
    hidden_edits()
    plans(author["id"])
    print("PASS Ticket 29 Echo 作者 HTTP/SQL 驗收")


if __name__ == "__main__":
    main()
