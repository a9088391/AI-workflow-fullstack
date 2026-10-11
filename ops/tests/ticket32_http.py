"""Ticket 32：僅在專用 Compose 資料庫驗證 Fiber 作者操作與 Laravel 互通。"""

import json
import os
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

FIBER = "http://127.0.0.1:18321"
LARAVEL = "http://127.0.0.1:18322"
PROJECT = "ticket32_test"
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
    "E0005": "系統暫時無法處理，請稍後再試",
    "E0102": "此帳號已停用，請聯絡系統管理員",
    "E0201": "內容已更新，請確認最新內容後再送出",
}


# check 將第一個契約偏差當場中止，避免後續寫入掩蓋根因。
def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


# compose 只向已設定的 Ticket 32 專案發送 Docker 指令。
def compose(*args):
    return subprocess.check_output(["docker", "compose", "--profile", "fiber", *args], text=True).strip()


# sql 僅查詢或修改已核對的專用資料庫測資。
def sql(statement):
    return compose("exec", "-T", "db", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A", "-U", "articles_owner", "-d", "articles", "-c", statement)


# guard 在任何測試寫入前核對實際容器、資料卷、埠與金鑰掛載。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT, "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == "compose.yaml:ops/tests/ticket32_compose.yaml", "錯誤 Compose 檔")
    config = json.loads(compose("config", "--format", "json"))
    check(config["name"] == PROJECT and config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data", "非專用資料卷")
    check(config["services"]["db"]["environment"]["TICKET32_FIXTURE"] == "isolated-ticket32", "fixture 標記不符")
    for service, port in (("fiber", "18321"), ("laravel", "18322")):
        check(config["services"][service]["ports"][0]["published"] == port, "宣告埠不符：" + service)
        ids = compose("ps", "-q", service).splitlines()
        check(len(ids) == 1, "容器未唯一啟動：" + service)
        container = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(container["Config"]["Labels"].get("com.docker.compose.project") == PROJECT, "容器專案錯誤")
        check(container["State"]["Running"] and container["State"]["Health"]["Status"] == "healthy", "容器未就緒")
        check(container["HostConfig"]["PortBindings"]["8000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": port}], "實際埠不符")
        key = os.path.realpath(".local/ticket32-backend/" + service + ".key")
        check(any(m["Type"] == "bind" and os.path.realpath(m["Source"]) == key and m["RW"] is False for m in container["Mounts"]), "金鑰掛載不符")
        with urlopen((FIBER if service == "fiber" else LARAVEL) + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "API 未就緒")
    db_ids = compose("ps", "-q", "db").splitlines()
    check(len(db_ids) == 1, "DB 未唯一啟動")
    db = json.loads(subprocess.check_output(["docker", "inspect", db_ids[0]], text=True))[0]
    check(any(m.get("Name") == PROJECT + "_postgres_data" for m in db["Mounts"]), "DB 實際資料卷不符")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 身分不符")


# call 保留原始 JSON 字串，讓數字型態與欄位順序測試不受 Python 轉換影響。
def call(base, method, path, body=None, token=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False)
    if body is not None:
        body = body.encode()
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=body, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


# expect 比對狀態、共同文案與可變 log_id 以外的完整錯誤欄位。
def expect(result, status, code=None, fields=None):
    actual, payload = result
    check(actual == status, f"HTTP {actual} != {status}: {payload}")
    if code:
        expected = {"success": False, "message": MESSAGES[code], "error_code": code}
        check(all(payload.get(k) == v for k, v in expected.items()) and payload.get("log_id"), f"錯誤 envelope: {payload}")
        check(("errors" in payload) == (fields is not None), f"errors 有無錯誤: {payload}")
        if fields is not None:
            check(payload["errors"] == fields, f"errors: {payload['errors']} != {fields}")
        return payload
    check(payload == {"success": True, "message": "OK", "data": payload.get("data")}, f"成功 envelope: {payload}")
    return payload["data"]


# register 透過真實 API 建立唯一帳號，再取後端專屬 session。
def register(base, name):
    email = name + "@example.test"
    expect(call(base, "POST", "/api/auth/register", {"email": email, "password": "abcd1234", "nickname": name}), 201)
    return email


# token 登入指定後端，避免把跨後端不可互用的 JWT 當成互通證據。
def token(base, email):
    return expect(call(base, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"}), 200)["access_token"]


# item 從專屬列表以原文尋找新 ID，不假設自增序號或寫入回傳 ID。
def item(base, path, token_value, key, value):
    rows = expect(call(base, "GET", path + "?page=1&per_page=100", token=token_value), 200)["list"]
    matches = [row for row in rows if row[key] == value]
    check(len(matches) == 1, f"列表未唯一找到 {key}={value}: {rows}")
    return matches[0]


# waiting 確認 HTTP 真的卡在指定資料表的列鎖；逾時代表屏障無法證明競爭。
def waiting(fragment, minimum=1):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        count = sql("SELECT count(*) FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' AND wait_event_type='Lock' AND query ILIKE '%" + fragment + "%' ")
        if int(count) >= minimum:
            print(f"LOCK_WAIT fragment={fragment} count={count}")
            return
        time.sleep(0.03)
    raise AssertionError("HTTP 未等待列鎖：" + fragment)


# race 先持有 fixture 鎖，再觀察每個真實請求的等待點後提交指定狀態。
def race(lock_table, lock_id, requests, waits, update=""):
    process = subprocess.Popen(
        ["docker", "compose", "--profile", "fiber", "exec", "-T", "db", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-qAt", "-U", "articles_owner", "-d", "articles"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    pool = ThreadPoolExecutor(max_workers=len(requests))
    try:
        process.stdin.write(f"BEGIN;\nSELECT id FROM {lock_table} WHERE id={lock_id} FOR UPDATE;\n")
        process.stdin.flush()
        check(process.stdout.readline().strip() == str(lock_id), "fixture 未取得列鎖")
        futures = []
        seen = {}
        for request, fragment in zip(requests, waits):
            futures.append(pool.submit(call, *request))
            seen[fragment] = seen.get(fragment, 0) + 1
            waiting(fragment, seen[fragment])
        # SQL 狀態先提交；等待中的 HTTP 只可按提交後狀態判定。
        process.stdin.write(update + "\nCOMMIT;\n")
        process.stdin.flush()
        process.stdin.close()
        check(process.wait(timeout=10) == 0, "fixture 提交失敗：" + process.stderr.read())
        return [future.result(timeout=15) for future in futures]
    finally:
        # 出錯時釋放 fixture 交易，避免測試留下阻塞其他請求的鎖。
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        pool.shutdown(wait=False, cancel_futures=True)


# parity 對相同初始資源逐後端送同一錯誤輸入，逐字比對共同錯誤欄位。
def parity(cases, article_id, owner, laravel_owner):
    for method, path, body, status, code, fields in cases:
        results = []
        for base, credential in ((FIBER, owner), (LARAVEL, laravel_owner)):
            result = call(base, method, path.format(id=article_id), body, credential)
            expect(result, status, code, fields)
            results.append({key: value for key, value in result[1].items() if key != "log_id"})
        check(results[0] == results[1], "Fiber/Laravel 錯誤欄位不一致：" + path)


# measured_queries 以獨立 SQL 標記框住單次 HTTP，從 DB 實際 execute 日誌量查詢次數。
def measured_queries(path, credential):
    marker = secrets.token_hex(8)
    since = datetime.now(timezone.utc).isoformat()
    sql("SELECT 'T32START" + marker + "'")
    expect(call(FIBER, "GET", path, token=credential), 200)
    sql("SELECT 'T32END" + marker + "'")
    lines = compose("logs", "--since", since, "--no-log-prefix", "db").splitlines()
    starts = [i for i, line in enumerate(lines) if "T32START" + marker in line]
    ends = [i for i, line in enumerate(lines) if "T32END" + marker in line]
    check(len(starts) == len(ends) == 1 and starts[0] < ends[0], "SQL 日誌標記不完整")
    statements = [line.rsplit(": ", 1)[1] for line in lines[starts[0]+1:ends[0]]
                  if "articles_app@articles LOG:" in line and " execute stmtcache_" in line
                  and "SELECT 1 FROM accounts,login_sessions" not in line]
    check(statements, "未擷取到 HTTP 實際查詢")
    return statements


# query_evidence 將真實送出的 SQL 帶入同次 fixture 參數，列印實測次數及完整查詢計畫。
def query_evidence(owner_email, owner, comment_id):
    owner_id = int(sql("SELECT id FROM accounts WHERE email='" + owner_email + "'"))
    articles_count = sql(f"SELECT count(*) FROM articles WHERE author_id={owner_id}")
    comments_count = sql(f"SELECT count(*) FROM comments WHERE author_id={owner_id}")
    global_counts = sql("SELECT (SELECT count(*) FROM articles)||','||(SELECT count(*) FROM comments)")
    print(f"FIXTURE owner_id={owner_id} own_articles={articles_count} own_comments={comments_count} global_articles,comments={global_counts}")
    for name, path, table, expected in (
        ("my_articles", "/api/me/articles?page=1&per_page=20", "articles", 4),
        ("my_comments", "/api/me/comments?page=1&per_page=20", "comments", 4),
        ("comment_detail", f"/api/comments/{comment_id}", "comments", 3),
    ):
        statements = measured_queries(path, owner)
        check(len(statements) == expected, f"{name} 實際查詢次數 {len(statements)} != {expected}: {statements}")
        content = [statement for statement in statements if " FROM " + table in statement and "SELECT 1 FROM" not in statement]
        check(len(content) == (1 if name == "comment_detail" else 2), f"{name} 內容查詢不符：{statements}")
        print(f"QUERY_COUNT {name}={len(statements)} content={len(content)}")
        for statement in content:
            bound = statement.replace("$1", str(comment_id if name == "comment_detail" else owner_id)).replace("$2", "20").replace("$3", "0")
            plan = sql("EXPLAIN (ANALYZE, BUFFERS) " + bound)
            check("Execution Time:" in plan and "Buffers:" in plan, "缺少 EXPLAIN 實測：" + name)
            print("EXPLAIN " + name + ": " + plan.replace("\n", " | "))


# main 依版本、下架與父文章狀態檢查真實持久化結果及跨後端讀取。
def main():
    guard()
    tag = "t32" + secrets.token_hex(4)
    owner_email = register(FIBER, tag + "owner")
    other_email = register(FIBER, tag + "other")
    admin_email = register(FIBER, tag + "admin")
    # 管理員僅供隔離 fixture；角色無公開建立 API，且 SQL 已被 guard 限定於專用資料卷。
    sql("UPDATE accounts SET role='admin' WHERE email='" + admin_email + "'")
    owner = token(FIBER, owner_email)
    other = token(FIBER, other_email)
    admin = token(FIBER, admin_email)
    laravel_owner = token(LARAVEL, owner_email)
    title = tag + "文章"
    expect(call(FIBER, "POST", "/api/articles", {"title": title, "body": "原文\r\n第二行"}, owner), 201)
    article = item(FIBER, "/api/me/articles", owner, "title", title)
    article_id = article["id"]
    check(article["created_backend"] == "fiber" and article["version"] == 1 and article["created_at"] == article["updated_at"], "文章建立欄位")
    check(expect(call(LARAVEL, "GET", f"/api/articles/{article_id}"), 200)["body"] == article["body"], "Laravel 未讀到 Fiber 文章")
    expect(call(FIBER, "POST", f"/api/articles/{article_id}/comments", {"body": tag + "留言"}, owner), 201)
    comment = item(FIBER, "/api/me/comments", owner, "body", tag + "留言")
    comment_id = comment["id"]
    check(comment["created_backend"] == "fiber" and comment["article_id"] == article_id and comment["version"] == 1 and comment["created_at"] == comment["updated_at"], "留言建立欄位")
    check(any(row["id"] == comment_id for row in expect(call(LARAVEL, "GET", f"/api/articles/{article_id}/comments?page=1&per_page=20"), 200)["list"]), "Laravel 未讀到 Fiber 留言")
    expect(call(FIBER, "GET", f"/api/comments/{comment_id}", token=other), 403, "E0003")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "改文", "version": 1}, other), 403, "E0003")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "改文", "version": 1}, owner), 200)
    edited = expect(call(FIBER, "GET", f"/api/articles/{article_id}", token=owner), 200)
    check(edited["version"] == 2 and edited["created_backend"] == "fiber", "文章編修版本或來源")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "改文", "version": 1}, owner), 409, "E0201")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "改文", "version": 2}, owner), 200)
    same_article = expect(call(FIBER, "GET", f"/api/articles/{article_id}", token=owner), 200)
    check((same_article["version"], same_article["updated_at"]) == (edited["version"], edited["updated_at"]), "文章同值改變版本或時間")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 2}, owner), 200)
    hidden = item(FIBER, "/api/me/articles", owner, "title", title)
    check(hidden["version"] == 3 and hidden["can_restore"], "作者下架權限")
    expect(call(FIBER, "GET", f"/api/articles/{article_id}", token=other), 404, "E0004")
    expect(call(FIBER, "POST", f"/api/articles/{article_id}/comments", {"body": "blocked"}, other), 404, "E0004")
    expect(call(FIBER, "POST", f"/api/articles/{article_id}/comments", {"body": "blocked"}, owner), 403, "E0003")
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}", {"body": "blocked", "version": 1}, owner), 403, "E0003")
    own_comment = expect(call(FIBER, "GET", f"/api/comments/{comment_id}", token=owner), 200)
    check(own_comment["article_id"] == article_id and "title" not in own_comment, "作者留言詳情洩漏父文章")
    expect(call(FIBER, "GET", f"/api/comments/{comment_id}", token=other), 404, "E0004")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 3}, admin), 200)
    taken = item(FIBER, "/api/me/articles", owner, "title", title)
    check(taken["version"] == 4 and not taken["can_restore"], "管理員未接管恢復權")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "visible", "version": 4}, owner), 403, "E0003")
    expect(call(FIBER, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "visible", "version": 4}, admin), 200)
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}", {"body": tag + "新留言", "version": 1}, owner), 200)
    changed = expect(call(FIBER, "GET", f"/api/comments/{comment_id}", token=owner), 200)
    check(changed["version"] == 2 and changed["body"] == tag + "新留言" and changed["created_backend"] == "fiber", "留言編修或來源")
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}", {"body": changed["body"], "version": 2}, owner), 200)
    same_comment = expect(call(FIBER, "GET", f"/api/comments/{comment_id}", token=owner), 200)
    check((same_comment["version"], same_comment["updated_at"]) == (changed["version"], changed["updated_at"]), "留言同值改變版本或時間")
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 2}, owner), 200)
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 3}, admin), 200)
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "visible", "version": 4}, owner), 403, "E0003")
    expect(call(FIBER, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "visible", "version": 4}, admin), 200)
    # Laravel 寫入後 Fiber 依同一契約讀取，涵蓋相反互通方向。
    laravel_title = tag + "Laravel"
    expect(call(LARAVEL, "POST", "/api/articles", {"title": laravel_title, "body": "跨後端"}, laravel_owner), 201)
    laravel_article = item(FIBER, "/api/me/articles", owner, "title", laravel_title)
    check(laravel_article["created_backend"] == "laravel" and laravel_article["created_at"] == laravel_article["updated_at"], "Fiber 未讀到 Laravel 來源")
    expect(call(FIBER, "PUT", f"/api/articles/{laravel_article['id']}", '{"title":"\u3000","body":"x\\u0000","version":1,"unexpected":1}', owner), 422, "E0001", {"title": ["欄位格式或範圍不正確"], "body": ["欄位格式或範圍不正確"], "unexpected": ["不支援此欄位"]})
    expect(call(FIBER, "PUT", f"/api/articles/{laravel_article['id']}", '{"title":"x","body":"y","version":9007199254740992}', owner), 422, "E0001", {"version": ["欄位格式或範圍不正確"]})
    expect(call(FIBER, "PUT", f"/api/articles/{laravel_article['id']}", '{"title":"x","body":"y","version":"1"}', owner), 422, "E0001", {"version": ["欄位型態不正確"]})
    # 同一份合法初始資料逐後端送出區分框架預設的格式與型態案例。
    parity([
        ("PUT", "/api/articles/{id}", "{", 400, "E0005", None),
        ("PUT", "/api/articles/{id}?extra=1", {"title": "x", "body": "y", "version": 1}, 422, "E0001", {"extra": ["不支援此欄位"]}),
        ("PUT", "/api/articles/{id}?extra=1&extra=2", {"title": "x", "body": "y", "version": 1}, 422, "E0001", {"extra": ["此參數不可重複"]}),
        ("PUT", "/api/articles/{id}", '{"title":"x","body":"y","version":"1"}', 422, "E0001", {"version": ["欄位型態不正確"]}),
        ("PUT", "/api/articles/{id}", '{"title":"x","body":"y","version":9007199254740992}', 422, "E0001", {"version": ["欄位格式或範圍不正確"]}),
    ], laravel_article["id"], owner, laravel_owner)
    laravel_other = token(LARAVEL, other_email)
    expect(call(FIBER, "PUT", f"/api/articles/{laravel_article['id']}/visibility", {"visibility": "hidden", "version": 1}, owner), 200)
    parity([
        ("PUT", "/api/articles/{id}", {"title": "x", "body": "y", "version": 1}, 404, "E0004", None),
        ("PUT", "/api/articles/{id}", "{", 400, "E0005", None),
        ("PUT", "/api/articles/{id}", '{"title":"x","body":"y","version":"1"}', 422, "E0001", {"version": ["欄位型態不正確"]}),
    ], laravel_article["id"], other, laravel_other)
    check(item(FIBER, "/api/me/articles", owner, "title", laravel_title)["created_backend"] == "laravel", "跨後端下架改寫建立來源")
    # 屏障先鎖帳號並撤銷 session；被阻塞的 Fiber 寫入醒來後必須拒絕且不落資料。
    race_email = register(FIBER, tag + "race")
    race_token = token(FIBER, race_email)
    race_account = int(sql("SELECT id FROM accounts WHERE email='" + race_email + "'"))
    revoked_title = tag + "撤銷競爭"
    revoked = race("accounts", race_account,
                   [(FIBER, "POST", "/api/articles", {"title": revoked_title, "body": "不可落庫"}, race_token)],
                   ["FROM accounts WHERE id="],
                   f"UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id={race_account} AND backend_id='fiber' AND revoked_at IS NULL;")[0]
    expect(revoked, 401, "E0002")
    check(sql("SELECT count(*) FROM articles WHERE title='" + revoked_title + "'") == "0", "撤銷後仍寫入文章")
    # 停用與撤銷先提交：兩個寫入都等同一帳號鎖，醒來後停用優先於已撤銷憑證。
    disabled_email = register(FIBER, tag + "disabled")
    disabled_token = token(FIBER, disabled_email)
    disabled_account = int(sql("SELECT id FROM accounts WHERE email='" + disabled_email + "'"))
    disabled_title = tag + "停用競爭"
    expect(call(FIBER, "POST", "/api/articles", {"title": disabled_title, "body": "原文"}, disabled_token), 201)
    disabled_article = item(FIBER, "/api/me/articles", disabled_token, "title", disabled_title)
    before_disable = sql(f"SELECT row_to_json(a)::text FROM articles a WHERE id={disabled_article['id']}")
    denied_title = tag + "停用後不可新增"
    denied = race("accounts", disabled_account, [
        (FIBER, "POST", "/api/articles", {"title": denied_title, "body": "不可落庫"}, disabled_token),
        (FIBER, "PUT", f"/api/articles/{disabled_article['id']}", {"title": disabled_title, "body": "不可改寫", "version": 1}, disabled_token),
    ], ["FROM accounts WHERE id=", "FROM accounts WHERE id="],
        f"UPDATE accounts SET status='disabled' WHERE id={disabled_account}; "
        f"UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id={disabled_account} AND revoked_at IS NULL;")
    expect(denied[0], 403, "E0102")
    expect(denied[1], 403, "E0102")
    check(sql("SELECT count(*) FROM articles WHERE title='" + denied_title + "'") == "0", "停用後仍新增文章")
    check(sql(f"SELECT row_to_json(a)::text FROM articles a WHERE id={disabled_article['id']}") == before_disable, "停用後仍編修文章")
    check(sql(f"SELECT status FROM accounts WHERE id={disabled_account}") == "disabled"
          and sql(f"SELECT count(*) FROM login_sessions WHERE account_id={disabled_account} AND revoked_at IS NULL") == "0", "停用與撤銷未一起提交")
    # 寫入先持有帳號鎖：Laravel 停用須等待 Fiber 編修提交，之後舊憑證立即失權。
    winner_email = register(FIBER, tag + "winner")
    winner_token = token(FIBER, winner_email)
    winner_account = int(sql("SELECT id FROM accounts WHERE email='" + winner_email + "'"))
    winner_title = tag + "先寫後停用"
    expect(call(FIBER, "POST", "/api/articles", {"title": winner_title, "body": "原文"}, winner_token), 201)
    winner_article = item(FIBER, "/api/me/articles", winner_token, "title", winner_title)
    laravel_admin = token(LARAVEL, admin_email)
    outcomes = race("articles", winner_article["id"], [
        (FIBER, "PUT", f"/api/articles/{winner_article['id']}", {"title": winner_title, "body": "先行編修", "version": 1}, winner_token),
        (LARAVEL, "PUT", f"/api/members/{winner_account}/status", {"status": "disabled"}, laravel_admin),
    ], ["FROM articles WHERE id=", "accounts"])
    expect(outcomes[0], 200)
    expect(outcomes[1], 200)
    check(sql(f"SELECT body||':'||version FROM articles WHERE id={winner_article['id']}") == "先行編修:2", "先行寫入未提交")
    check(sql(f"SELECT status FROM accounts WHERE id={winner_account}") == "disabled"
          and sql(f"SELECT count(*) FROM login_sessions WHERE account_id={winner_account} AND revoked_at IS NULL") == "0", "Laravel 停用未撤銷 session")
    expect(call(FIBER, "PUT", f"/api/articles/{winner_article['id']}", {"title": winner_title, "body": "停用後不可編修", "version": 2}, winner_token), 403, "E0102")
    check(sql(f"SELECT body||':'||version FROM articles WHERE id={winner_article['id']}") == "先行編修:2", "停用後仍可寫入")
    # 反向順序讓寫入先持有帳號及 session 鎖，登出必須待寫入提交後才撤銷。
    expect(call(FIBER, "POST", "/api/articles", {"title": tag + "鎖文章", "body": "原文"}, owner), 201)
    locked_article = item(FIBER, "/api/me/articles", owner, "title", tag + "鎖文章")
    outcomes = race("articles", locked_article["id"], [
        (FIBER, "PUT", f"/api/articles/{locked_article['id']}", {"title": tag + "鎖文章", "body": "先寫入", "version": 1}, owner),
        (FIBER, "POST", "/api/auth/logout", {}, owner),
    ], ["FROM articles WHERE id=", "FROM accounts WHERE id="])
    expect(outcomes[0], 200)
    expect(outcomes[1], 200)
    check(sql(f"SELECT body FROM articles WHERE id={locked_article['id']}") == "先寫入", "寫入先行卻未提交")
    owner = token(FIBER, owner_email)
    # 父文章先下架：等待文章鎖的新增不可用舊公開狀態寫入。
    parent_title = tag + "父鎖"
    expect(call(FIBER, "POST", "/api/articles", {"title": parent_title, "body": "父文章"}, other), 201)
    parent = item(FIBER, "/api/me/articles", other, "title", parent_title)
    expect(call(FIBER, "POST", f"/api/articles/{parent['id']}/comments", {"body": tag + "待編修"}, owner), 201)
    parent_comment = item(FIBER, "/api/me/comments", owner, "body", tag + "待編修")
    blocked_body = tag + "不得新增"
    results = race("articles", parent["id"], [
        (FIBER, "POST", f"/api/articles/{parent['id']}/comments", {"body": blocked_body}, other),
        (FIBER, "PUT", f"/api/comments/{parent_comment['id']}", {"body": tag + "不得編修", "version": 1}, owner),
    ], ["FROM articles WHERE id=", "FROM articles WHERE id="],
        f"UPDATE articles SET visibility='hidden',restore_control='author',version=version+1,updated_at=clock_timestamp() WHERE id={parent['id']};")
    expect(results[0], 403, "E0003")
    expect(results[1], 403, "E0003")
    check(sql("SELECT count(*) FROM comments WHERE body='" + blocked_body + "'") == "0", "父文章下架後仍新增留言")
    check(sql(f"SELECT body||':'||version FROM comments WHERE id={parent_comment['id']}") == tag + "待編修:1", "父文章下架後仍編修留言")
    # 留言先持有父鎖時，下架請求須等待；留言提交後仍可隨父文章一起隱藏。
    reverse_title = tag + "反向父鎖"
    expect(call(FIBER, "POST", "/api/articles", {"title": reverse_title, "body": "父文章"}, other), 201)
    reverse_parent = item(FIBER, "/api/me/articles", other, "title", reverse_title)
    expect(call(FIBER, "POST", f"/api/articles/{reverse_parent['id']}/comments", {"body": tag + "先留言"}, other), 201)
    reverse_comment = item(FIBER, "/api/me/comments", other, "body", tag + "先留言")
    outcomes = race("comments", reverse_comment["id"], [
        (FIBER, "PUT", f"/api/comments/{reverse_comment['id']}", {"body": tag + "先改留言", "version": 1}, other),
        (FIBER, "PUT", f"/api/articles/{reverse_parent['id']}/visibility", {"visibility": "hidden", "version": 1}, admin),
    ], ["FROM comments WHERE id=", "FROM articles WHERE id="])
    expect(outcomes[0], 200)
    expect(outcomes[1], 200)
    check(sql(f"SELECT body FROM comments WHERE id={reverse_comment['id']}") == tag + "先改留言", "先行留言編修未提交")
    expect(call(FIBER, "GET", f"/api/comments/{reverse_comment['id']}", token=owner), 404, "E0004")
    expect(call(FIBER, "PUT", f"/api/comments/{reverse_comment['id']}", {"body": "不可再改", "version": 2}, other), 403, "E0003")
    query_evidence(owner_email, owner, comment_id)
    print("PASS Ticket32 HTTP: Fiber writes/reads, Laravel parity, versions, controlled races, actual SQL plans")


if __name__ == "__main__":
    main()
