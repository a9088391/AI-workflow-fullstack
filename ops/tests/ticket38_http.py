"""Ticket 38：在專用 Compose 資料卷驗證 Fastify 作者內容與跨後端讀取。"""

import json
import os
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

from ticket37_http import call, check
from ticket37_query_count import db_logs, statements

FASTIFY = "http://127.0.0.1:18383"
LARAVEL = "http://127.0.0.1:18382"
ERRORS = {
    "E0001": "欄位資料不正確，請確認後再送出", "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能", "E0004": "找不到指定資料或 API",
    "E0201": "內容已更新，請確認最新內容後再送出",
}


# compose 只在呼叫者已選定的 ticket38_test 專案執行，避免測試觸及展示資料。
def compose(*args, input_text=None):
    return subprocess.run(["docker", "compose", *args], input=input_text, text=True,
                          capture_output=True, check=True).stdout.strip()


# guard 核對來源檔、實際容器、資料卷、金鑰與埠，通過前不得寫入測資。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket38_test", "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == "compose.yaml:ops/tests/ticket38_compose.yaml", "錯誤 Compose 檔")
    config = json.loads(compose("--profile", "fastify", "config", "--format", "json"))
    check(config["name"] == "ticket38_test" and
          config["volumes"]["postgres_data"]["name"] == "ticket38_test_postgres_data", "非專用資料卷")
    check(config["services"]["db"]["environment"]["TICKET38_FIXTURE"] == "isolated-ticket38", "fixture 標記不符")
    for service, port in (("laravel", "18382"), ("fastify", "18383")):
        ids = compose("--profile", "fastify", "ps", "-q", service).splitlines()
        check(len(ids) == 1, f"{service} 容器未唯一啟動")
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == "ticket38_test", "容器專案不符")
        check(item["State"]["Running"] and item["State"]["Health"]["Status"] == "healthy", "後端不健康")
        check(item["HostConfig"]["PortBindings"]["8000/tcp"] ==
              [{"HostIp": "127.0.0.1", "HostPort": port}], "實際後端埠不符")
        key = Path(f".local/ticket38-backend/{service}.key").resolve()
        demo = Path(f".local/keys/{service}.key")
        check(key.read_bytes() != demo.read_bytes(), "測試與展示金鑰相同")
        check(any(Path(m.get("Source", "/")).resolve() == key and
                  m.get("Destination") == f"/run/secrets/{service}-jwt.key" and not m.get("RW")
                  for m in item["Mounts"]), "測試金鑰掛載不符")
    ids = compose("ps", "-q", "db").splitlines()
    check(len(ids) == 1, "DB 容器未唯一啟動")
    db = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    check(db["Config"]["Labels"].get("com.docker.compose.project") == "ticket38_test", "DB 專案不符")
    check(any(m.get("Name") == "ticket38_test_postgres_data" for m in db["Mounts"]), "實際 DB 資料卷不符")
    check(not db["HostConfig"]["PortBindings"], "DB 不應發布埠")
    for base in (FASTIFY, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")
    print("PASS ticket38 guard: project, volume, keys, ports, health")


# sql 只在已核對的隔離 DB 執行測試讀取與查詢計畫。
def sql(query):
    return compose("exec", "-T", "db", "psql", "-X", "-q", "-t", "-A", "-U", "articles_owner", "-d", "articles", "-c", query)


# expect 精確比對共同 envelope、狀態與錯誤文案。
def expect(result, status, code=None, fields=None):
    actual, payload, _ = result
    check(actual == status, f"HTTP {actual}，預期 {status}/{code}: {payload}")
    if code is None:
        check(set(payload) == {"success", "message", "data"} and payload["success"] is True and
              payload["message"] == "OK", "成功 envelope 不符")
        return payload["data"]
    keys = {"success", "message", "error_code", "log_id"}
    if fields is not None:
        keys.add("errors")
    check(set(payload) == keys and payload["success"] is False and payload["error_code"] == code and
          payload["message"] == ERRORS[code] and payload["log_id"], "錯誤 envelope 不符")
    if fields is not None:
        check(payload["errors"] == fields, f"errors 不符：{payload['errors']}")
    return payload


# parity_error 排除每次必然不同的 log_id，逐字比較兩後端同前提的錯誤。
def parity_error(path, method, body, fastify_token, laravel_token, status, code, fields):
    fastify = expect(call(FASTIFY, method, path, body, token=fastify_token), status, code, fields)
    laravel = expect(call(LARAVEL, method, path, body, token=laravel_token), status, code, fields)
    check({key: value for key, value in fastify.items() if key != "log_id"} ==
          {key: value for key, value in laravel.items() if key != "log_id"}, "跨後端錯誤不一致")


# ensure_admin 只在全新專用資料庫建立固定測試管理員，重跑時不改其密碼。
def ensure_admin():
    count = sql("SELECT count(*) FROM accounts WHERE role='admin'")
    if count == "0":
        compose("exec", "-T", "laravel", "php", "artisan", "app:create-initial-admin",
                input_text="ticket38-admin@example.test\n測試管理員\nAbcd1234\n")
    check(sql("SELECT email FROM accounts WHERE role='admin'") == "ticket38-admin@example.test", "管理員 fixture 不符")
    return expect(call(FASTIFY, "POST", "/api/auth/login",
                       {"email": "ticket38-admin@example.test", "password": "Abcd1234"}), 200)["access_token"]


# member 每次建立唯一帳號，讓同一資料卷可重跑且不需刪除舊資料。
def member(tag):
    email = f"ticket38-{tag}@example.test"
    expect(call(FASTIFY, "POST", "/api/auth/register",
                {"email": email, "password": "Abcd1234", "nickname": "測試作者"}), 201)
    fastify = expect(call(FASTIFY, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    return fastify["access_token"], laravel["access_token"], fastify["user"]["id"]


# created_id 透過我的列表取新 ID；一般寫入仍按契約回空 data。
def created_id(path, token, field, value):
    rows = expect(call(FASTIFY, "GET", path + "?page=1&per_page=20", token=token), 200)["list"]
    matches = [row["id"] for row in rows if row[field] == value]
    check(len(matches) == 1, "新資料未出現在本人列表")
    return matches[0]


# main 依已確認的隔離目標檢查九個操作的狀態、持久結果與跨後端讀取。
def main():
    guard()
    admin = ensure_admin()
    tag = secrets.token_hex(5)
    owner, laravel_owner, owner_id = member(tag + "-owner")
    other, _, _ = member(tag + "-other")
    title = "Ticket38 " + tag
    body = "保留\r\n原始換行"
    check(expect(call(FASTIFY, "POST", "/api/articles", {"title": title, "body": body}, token=owner), 201) == {}, "發文回應")
    article_id = created_id("/api/me/articles", owner, "title", title)
    article = expect(call(LARAVEL, "GET", f"/api/articles/{article_id}"), 200)
    check(article["created_backend"] == "fastify" and article["author"]["id"] == owner_id and
          article["body"] == body and article["version"] == 1, "跨後端文章建立讀取")
    parity_error("/api/articles", "POST", {"title": "\u200a", "body": "x"}, owner, laravel_owner,
                 422, "E0001", {"title": ["欄位格式或範圍不正確"]})
    parity_error("/api/articles", "POST", {"title": "x", "body": "\u0000"}, owner, laravel_owner,
                 422, "E0001", {"body": ["欄位格式或範圍不正確"]})
    parity_error(f"/api/articles/{article_id}", "PUT", '{"title":"x","body":"x","version":1e0}',
                 owner, laravel_owner, 422, "E0001", {"version": ["欄位型態不正確"]})
    parity_error(f"/api/articles/{article_id}", "PUT", '{"title":"x","body":"x","version":9007199254740992}',
                 owner, laravel_owner, 422, "E0001", {"version": ["欄位格式或範圍不正確"]})
    parity_error("/api/me/articles?page=1&per_page=20&page=2", "GET", None,
                 owner, laravel_owner, 422, "E0001", {"page": ["此參數不可重複"]})
    forged = {"value": 1, "source": "1"}
    for path, invalid in (
        (f"/api/articles/{article_id}", {"title": "偽造修改", "body": "偽造修改", "version": forged}),
        (f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": forged}),
    ):
        parity_error(path, "PUT", invalid, owner, laravel_owner, 422, "E0001", {"version": ["欄位型態不正確"]})
    check(sql(f"SELECT version||'|'||visibility FROM articles WHERE id={article_id}") == "1|visible", "偽造文章版本不得寫入")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}", {"title": title, "body": body, "version": 1}, token=owner), 200)
    check(expect(call(FASTIFY, "GET", f"/api/articles/{article_id}", token=owner), 200)["version"] == 1, "同值不得增加版本")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}", {"title": title, "body": body, "version": 2}, token=owner), 409, "E0201")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "更新", "version": 1}, token=other), 403, "E0003")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "更新", "version": 1}, token=owner), 200)
    check(expect(call(LARAVEL, "GET", f"/api/articles/{article_id}"), 200)["version"] == 2, "文章版本未跨後端更新")
    expect(call(FASTIFY, "POST", f"/api/articles/{article_id}/comments", {"body": "留言 " + tag}, token=owner), 201)
    comment_id = created_id("/api/me/comments", owner, "body", "留言 " + tag)
    comment = expect(call(LARAVEL, "GET", f"/api/comments/{comment_id}", token=laravel_owner), 200)
    check(comment["created_backend"] == "fastify" and comment["article_id"] == article_id, "跨後端留言讀取")
    for path, invalid in (
        (f"/api/comments/{comment_id}", {"body": "偽造修改", "version": forged}),
        (f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": forged}),
    ):
        parity_error(path, "PUT", invalid, owner, laravel_owner, 422, "E0001", {"version": ["欄位型態不正確"]})
    check(sql(f"SELECT version||'|'||visibility FROM comments WHERE id={comment_id}") == "1|visible", "偽造留言版本不得寫入")
    expect(call(FASTIFY, "GET", f"/api/comments/{comment_id}", token=other), 403, "E0003")
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}", {"body": "編修 " + tag, "version": 1}, token=owner), 200)
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}", {"body": "編修 " + tag, "version": 1}, token=owner), 409, "E0201")
    check(expect(call(LARAVEL, "GET", f"/api/comments/{comment_id}", token=laravel_owner), 200)["body"] == "編修 " + tag, "留言編修未跨後端可見")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 2}, token=owner), 200)
    expect(call(FASTIFY, "GET", f"/api/articles/{article_id}"), 404, "E0004")
    expect(call(FASTIFY, "GET", f"/api/comments/{comment_id}", token=owner), 200)
    expect(call(FASTIFY, "GET", f"/api/comments/{comment_id}", token=other), 404, "E0004")
    expect(call(FASTIFY, "POST", f"/api/articles/{article_id}/comments", {"body": "不可新增"}, token=owner), 403, "E0003")
    expect(call(FASTIFY, "POST", f"/api/articles/{article_id}/comments", {"body": "不可新增"}, token=other), 404, "E0004")
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}", {"body": "不可修改", "version": 2}, token=owner), 403, "E0003")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 3}, token=admin), 200)
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "visible", "version": 4}, token=owner), 403, "E0003")
    check(sql(f"SELECT restore_control||'|'||version FROM articles WHERE id={article_id}") == "admin|4", "管理員接管未持久化")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "visible", "version": 4}, token=admin), 200)
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 2}, token=owner), 200)
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 3}, token=admin), 200)
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "visible", "version": 4}, token=owner), 403, "E0003")
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "visible", "version": 4}, token=admin), 200)
    check(sql(f"SELECT created_backend FROM articles WHERE id={article_id}") == "fastify" and
          sql(f"SELECT created_backend FROM comments WHERE id={comment_id}") == "fastify", "建立來源遭改寫")
    # Laravel 建立的同帳號文章再由 Fastify 讀取，補足相反方向的跨後端寫讀。
    expect(call(LARAVEL, "POST", "/api/articles", {"title": "Laravel " + tag, "body": "共享"}, token=laravel_owner), 201)
    laravel_article = created_id("/api/me/articles", owner, "title", "Laravel " + tag)
    check(expect(call(FASTIFY, "GET", f"/api/articles/{laravel_article}"), 200)["created_backend"] == "laravel", "跨後端來源讀取")
    print("PASS ticket38 HTTP: 06,08-11,13-16; 401/403/404/409; same-value; takeover; cross-backend")
    # 同一版本並行編修只能有一筆提交，另一筆依最新版本回 409。
    for kind, resource_id, path, first, second in (
        ("article", article_id, f"/api/articles/{article_id}",
         {"title": title, "body": "並行甲", "version": 5}, {"title": title, "body": "並行乙", "version": 5}),
        ("comment", comment_id, f"/api/comments/{comment_id}",
         {"body": "並行甲", "version": 5}, {"body": "並行乙", "version": 5}),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(call, FASTIFY, "PUT", path, first, owner)
            b = pool.submit(call, FASTIFY, "PUT", path, second, owner)
            outcomes = [a.result(timeout=20), b.result(timeout=20)]
        check(sorted(item[0] for item in outcomes) == [200, 409], f"{kind} 版本競爭結果不符")
        for item in outcomes:
            expect(item, item[0], "E0201" if item[0] == 409 else None)
        table = "articles" if kind == "article" else "comments"
        check(sql(f"SELECT version||'|'||body FROM {table} WHERE id={resource_id}") in
              ("6|並行甲", "6|並行乙"), f"{kind} 版本競爭持久結果不符")
    print("PASS ticket38 article/comment concurrent version writes: one 200, one 409 each")
    # 同時提交另一帳號留言與管理員下架，結果只能符合先取得文章鎖的一方。
    expect(call(FASTIFY, "POST", "/api/articles", {"title": "Race " + tag, "body": "共享"}, token=owner), 201)
    race_id = created_id("/api/me/articles", owner, "title", "Race " + tag)
    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(call, FASTIFY, "POST", f"/api/articles/{race_id}/comments", {"body": "Race留言 " + tag}, other)
        hide = pool.submit(call, FASTIFY, "PUT", f"/api/articles/{race_id}/visibility", {"visibility": "hidden", "version": 1}, admin)
        created = create.result(timeout=20)
        hidden = hide.result(timeout=20)
    expect(hidden, 200)
    check(created[0] in (201, 404), f"父文章競爭結果不符：{created[0]}")
    count = sql(f"SELECT count(*) FROM comments WHERE article_id={race_id} AND body='Race留言 {tag}'")
    check(count == ("1" if created[0] == 201 else "0"), "父文章競爭留言持久結果不符")
    print("PASS ticket38 parent concurrency: hide=200, comment=" + str(created[0]))
    # 同一帳號的登出與發文共用帳號/session 鎖；同時請求只能對應一種持久結果。
    race_token, _, race_author = member(tag + "-logout")
    race_title = "Logout race " + tag
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(call, FASTIFY, "POST", "/api/articles", {"title": race_title, "body": "測試"}, race_token)
        logout = pool.submit(call, FASTIFY, "POST", "/api/auth/logout", {}, race_token)
        written = write.result(timeout=20)
        logged_out = logout.result(timeout=20)
    expect(logged_out, 200)
    check(written[0] in (201, 401), f"撤銷競爭狀態不符：{written[0]}")
    persisted = sql(f"SELECT count(*) FROM articles WHERE author_id={race_author} AND title='{race_title}'")
    check(persisted == ("1" if written[0] == 201 else "0"), "撤銷競爭持久結果不符")
    expect(call(FASTIFY, "POST", "/api/articles", {"title": "After logout " + tag, "body": "測試"}, token=race_token), 401, "E0002")
    print("PASS ticket38 logout/write concurrency: logout=200, write=" + str(written[0]))
    # 實際 HTTP SQL 日誌與查詢計畫都須表明列表在 DB 完成計數、排序與分頁。
    for table, path in (("articles", "/api/me/articles"), ("comments", "/api/me/comments")):
        before = db_logs()
        expect(call(FASTIFY, "GET", path + "?page=1&per_page=20", token=owner), 200)
        emitted = statements(before, db_logs())
        check(len(emitted) == 3 and "SELECT count(*)::bigint" in emitted[1] and
              f"FROM {table} JOIN accounts" in emitted[1] and "LIMIT" in emitted[2] and
              "OFFSET" in emitted[2], f"{table} 個人列表 SQL 次數或形狀不符：{emitted}")
        print(f"PASS {table} personal list SQL count=3")
        # 計畫使用 API 實際選取的所有內容與作者欄位，避免只查 ID 得到不相干的索引計畫。
        fields = ["id", "author_id", *(["title"] if table == "articles" else ["article_id"]),
                  "body", "visibility", "restore_control", "version", "created_backend", "created_at", "updated_at"]
        projection = ",".join(f"{table}.{field}" for field in fields) + ",accounts.nickname"
        count = sql(f"EXPLAIN (ANALYZE, BUFFERS) SELECT count(*)::bigint FROM {table} "
                    f"JOIN accounts ON accounts.id={table}.author_id WHERE {table}.author_id={owner_id}")
        listed = sql(f"EXPLAIN (ANALYZE, BUFFERS) SELECT {projection} FROM {table} "
                     f"JOIN accounts ON accounts.id={table}.author_id WHERE {table}.author_id={owner_id} "
                     f"ORDER BY {table}.created_at DESC,{table}.id DESC LIMIT 20 OFFSET 0")
        check(all("Execution Time" in plan and "Buffers" in plan for plan in (count, listed)), "列表計畫缺漏")
        print("PLAN", table + "-count", count.replace("\n", " | "))
        print("PLAN", table + "-list", listed.replace("\n", " | "))


if __name__ == "__main__":
    main()
