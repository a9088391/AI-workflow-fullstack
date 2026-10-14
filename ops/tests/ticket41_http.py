"""Ticket 41：在專用 Compose 資料卷驗證 NestJS 作者內容與跨後端讀取。"""

import json
import os
import secrets
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

from ticket40_http import call, check

NESTJS = "http://127.0.0.1:18413"
LARAVEL = "http://127.0.0.1:18412"
ERRORS = {
    "E0001": "欄位資料不正確，請確認後再送出", "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能", "E0004": "找不到指定資料或 API",
    "E0201": "內容已更新，請確認最新內容後再送出",
}


# compose 只在呼叫者已選定的 ticket41_test 專案執行，避免測試觸及展示資料。
def compose(*args, input_text=None):
    return subprocess.run(["docker", "compose", *args], input=input_text, text=True,
                          capture_output=True, check=True).stdout.strip()


# guard 核對來源檔、實際容器、資料卷、金鑰與埠，通過前不得寫入測資。
def guard():
    # 先驗呼叫者與合併後宣告，目標不符時完全不碰實際服務。
    check(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket41_test", "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == "compose.yaml:ops/tests/ticket41_compose.yaml", "錯誤 Compose 檔")
    config = json.loads(compose("--profile", "nestjs", "config", "--format", "json"))
    check(config["name"] == "ticket41_test" and
          config["volumes"]["postgres_data"]["name"] == "ticket41_test_postgres_data", "非專用資料卷")
    check(config["services"]["db"]["environment"]["TICKET41_FIXTURE"] == "isolated-ticket41", "fixture 標記不符")
    check(config["services"]["db"]["command"][-2:] == ["-c", "log_line_prefix=%m [%p] %u@%d "], "DB 日誌設定不符")
    check(config["services"]["sql"]["volumes"][-1]["source"] == str(Path(".local/ticket41-backend/backups").resolve()), "備份目錄不符")
    check(config["services"]["nestjs"]["environment"]["CORS_ORIGIN"] == "http://localhost:18411", "CORS 來源不符")
    check(config["services"]["frontend-vue"]["build"]["context"] == str(Path(".local/ticket41-frontend").resolve()), "前端建置來源不符")
    check(config["services"]["nginx"]["ports"] == [{"mode": "ingress", "target": 80, "published": "18411", "protocol": "tcp", "host_ip": "127.0.0.1"}], "NGINX 宣告埠不符")
    # 再核對正在執行的後端映像、資料連線、金鑰與實際綁定，避免宣告與執行目標不同。
    for service, port in (("laravel", "18412"), ("nestjs", "18413")):
        ids = compose("--profile", "nestjs", "ps", "-q", service).splitlines()
        check(len(ids) == 1, f"{service} 容器未唯一啟動")
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == "ticket41_test", "容器專案不符")
        check(item["State"]["Running"] and item["State"]["Health"]["Status"] == "healthy", "後端不健康")
        check("ticket41_test_default" in item["NetworkSettings"]["Networks"] and
              {"DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"}.issubset(item["Config"]["Env"]), "後端未連隔離 DB")
        check(item["HostConfig"]["PortBindings"]["8000/tcp"] ==
              [{"HostIp": "127.0.0.1", "HostPort": port}], "實際後端埠不符")
        key = Path(f".local/ticket41-backend/{service}.key").resolve()
        demo = Path(f".local/keys/{service}.key")
        check(key.read_bytes() != demo.read_bytes(), "測試與展示金鑰相同")
        check(any(Path(m.get("Source", "/")).resolve() == key and
                  m.get("Destination") == f"/run/secrets/{service}-jwt.key" and not m.get("RW")
                  for m in item["Mounts"]), "測試金鑰掛載不符")
        if service == "laravel":
            for source, destination, readonly in (
                (".local/ticket41-backend/cors.php", "/srv/laravel/config/cors.php", True),
                (".local/ticket41-backend/laravel-logs", "/srv/laravel/storage/logs", False),
            ):
                check(any(Path(m.get("Source", "/")).resolve() == Path(source).resolve() and
                          m.get("Destination") == destination and m.get("RW") is not readonly
                          for m in item["Mounts"]), "Laravel 隔離掛載不符：" + destination)
        image = compose("--profile", "nestjs", "images", "-q", service)
        check("sha256:" + image == item["Image"], "執行中容器不是目前 Compose 映像")
    ids = compose("ps", "-q", "db").splitlines()
    check(len(ids) == 1, "DB 容器未唯一啟動")
    db = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    check(db["Config"]["Labels"].get("com.docker.compose.project") == "ticket41_test" and
          "ticket41_test_default" in db["NetworkSettings"]["Networks"] and
          "TICKET41_FIXTURE=isolated-ticket41" in db["Config"]["Env"] and
          db["State"]["Running"] and db["State"]["Health"]["Status"] == "healthy", "DB 容器身分不符")
    check(any(m.get("Name") == "ticket41_test_postgres_data" for m in db["Mounts"]), "實際 DB 資料卷不符")
    check(not db["HostConfig"]["PortBindings"], "DB 不應發布埠")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 連線身分不符")
    # 前端與入口必須同屬本票網路；入口實際發布埠也須與核准網址一致。
    for service in ("frontend-vue", "nginx"):
        ids = compose("ps", "-q", service).splitlines()
        check(len(ids) == 1, "前端／入口容器未唯一啟動：" + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == "ticket41_test" and
              "ticket41_test_default" in item["NetworkSettings"]["Networks"] and
              item["State"]["Running"] and item["State"]["Health"]["Status"] == "healthy", "前端／入口容器身分不符")
        if service == "nginx":
            check(item["HostConfig"]["PortBindings"]["80/tcp"] ==
                  [{"HostIp": "127.0.0.1", "HostPort": "18411"}], "入口實際埠不符")
    for base in (NESTJS, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")
    print("PASS ticket41 guard: project, volume, keys, ports, health")


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
    check(str(uuid.UUID(payload["log_id"])) == payload["log_id"], "log_id 非標準 UUID")
    if fields is not None:
        check(payload["errors"] == fields, f"errors 不符：{payload['errors']}")
    return payload


# parity_error 排除每次必然不同的 log_id，逐字比較兩後端同前提的錯誤。
def parity_error(path, method, body, nestjs_token, laravel_token, status, code, fields):
    nestjs = expect(call(NESTJS, method, path, body, token=nestjs_token), status, code, fields)
    laravel = expect(call(LARAVEL, method, path, body, token=laravel_token), status, code, fields)
    check({key: value for key, value in nestjs.items() if key != "log_id"} ==
          {key: value for key, value in laravel.items() if key != "log_id"}, "跨後端錯誤不一致")


# ensure_admin 只在全新專用資料庫建立固定測試管理員，重跑時不改其密碼。
def ensure_admin():
    count = sql("SELECT count(*) FROM accounts WHERE role='admin'")
    if count == "0":
        compose("exec", "-T", "laravel", "php", "artisan", "app:create-initial-admin",
                input_text="ticket41-admin@example.test\n測試管理員\nAbcd1234\n")
    check(sql("SELECT email FROM accounts WHERE role='admin'") == "ticket41-admin@example.test", "管理員 fixture 不符")
    return expect(call(NESTJS, "POST", "/api/auth/login",
                       {"email": "ticket41-admin@example.test", "password": "Abcd1234"}), 200)["access_token"]


# member 每次建立唯一帳號，讓同一資料卷可重跑且不需刪除舊資料。
def member(tag):
    email = f"ticket41-{tag}@example.test"
    expect(call(NESTJS, "POST", "/api/auth/register",
                {"email": email, "password": "Abcd1234", "nickname": "測試作者"}), 201)
    nestjs = expect(call(NESTJS, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    return nestjs["access_token"], laravel["access_token"], nestjs["user"]["id"]


# created_id 透過我的列表取新 ID；一般寫入仍按契約回空 data。
def created_id(path, token, field, value):
    rows = expect(call(NESTJS, "GET", path + "?page=1&per_page=20", token=token), 200)["list"]
    matches = [row["id"] for row in rows if row[field] == value]
    check(len(matches) == 1, "新資料未出現在本人列表")
    return matches[0]


# db_logs 只讀專用資料庫的 PostgreSQL 日誌，供實際 HTTP SQL 次數核對。
def db_logs():
    return compose("logs", "--no-color", "--no-log-prefix", "db")


# statements 從呼叫前後的日誌差額取 app SQL，並保留實際語句供 EXPLAIN。
def statements(before, after):
    check(after.startswith(before), "資料庫日誌順序改變，無法量測")
    lines = after[len(before):].splitlines()
    return [line.split("execute <unnamed>: ", 1)[-1] if "execute <unnamed>: " in line else line.split("statement: ", 1)[-1]
            for line in lines if "articles_app@articles LOG:" in line and
            ("execute <unnamed>: " in line or "statement: " in line)]


# list_query_plans 用真實請求發出的 count/list SQL 與相同參數取計畫，避免手寫近似查詢。
def list_query_plans(table, path, token, owner_id):
    before = db_logs()
    expect(call(NESTJS, "GET", path + "?page=1&per_page=20", token=token), 200)
    emitted = statements(before, db_logs())
    check(len(emitted) == 3 and "SELECT count(*)::bigint" in emitted[1] and
          f"FROM {table} JOIN accounts" in emitted[1] and
          f"FROM {table} JOIN accounts" in emitted[2] and
          "LIMIT $2 OFFSET $3" in emitted[2], f"{table} 個人列表 SQL 次數或形狀不符：{emitted}")
    print(f"PASS {table} personal list SQL count=3")
    # 實際查詢只含三個已知數值 bind；逐一替換後執行相同 projection/條件的計畫。
    for label, statement in (("count", emitted[1]), ("list", emitted[2])):
        check(statement.lstrip().startswith("SELECT ") and statement.count(";") == 0, "非預期 SQL 計畫來源")
        planned = statement.replace("$1", str(owner_id)).replace("$2", "20").replace("$3", "0")
        check("$" not in planned, "列表 SQL 出現未預期 bind")
        plan = sql("EXPLAIN (ANALYZE, BUFFERS) " + planned)
        check("Execution Time" in plan and "Buffers" in plan, "列表計畫缺漏")
        print("PLAN", table + "-" + label, plan.replace("\n", " | "))


# main 依已確認的隔離目標檢查九個操作的狀態、持久結果與跨後端讀取。
def main():
    guard()
    # 每次以唯一帳號與標題建立測資，可在同一隔離卷重跑而不刪除舊紀錄。
    admin = ensure_admin()
    tag = secrets.token_hex(5)
    owner, laravel_owner, owner_id = member(tag + "-owner")
    other, laravel_other, _ = member(tag + "-other")
    title = "Ticket41 " + tag
    body = "保留\r\n原始換行"
    check(expect(call(NESTJS, "POST", "/api/articles", {"title": title, "body": body}, token=owner), 201) == {}, "發文回應")
    article_id = created_id("/api/me/articles", owner, "title", title)
    article = expect(call(LARAVEL, "GET", f"/api/articles/{article_id}"), 200)
    check(article["created_backend"] == "nestjs" and article["author"]["id"] == owner_id and
          article["body"] == body and article["version"] == 1, "跨後端文章建立讀取")
    check(expect(call(NESTJS, "GET", f"/api/articles/{article_id}"), 200) == article, "跨後端文章詳情不一致")
    check(expect(call(NESTJS, "GET", "/api/me/articles?page=1&per_page=20", token=other), 200)["pagination"]["total"] == 0,
          "本人列表不可包含他人文章")
    # 使用共同錯誤文案驗證原始 JSON 數字、控制字元、重複 Query 與欄位優先序。
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
    # 認證先於欄位檢查；已登入後再依契約處理必填、null、型態與未知欄位。
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}", {"version": None}), 401, "E0002")
    for invalid, fields in (
        ({"title": None, "body": 3, "version": "1"}, {"title": ["此欄位不可為 null"], "body": ["欄位型態不正確"], "version": ["欄位型態不正確"]}),
        ({"title": "x", "body": "x"}, {"version": ["此欄位為必填"]}),
        ({"title": "x", "body": "x", "version": 1, "created_backend": "laravel"}, {"created_backend": ["不支援此欄位"]}),
    ):
        parity_error(f"/api/articles/{article_id}", "PUT", invalid, owner, laravel_owner, 422, "E0001", fields)
    parity_error("/api/me/comments?page=1&per_page=20&filters%5Bscope%5D=management", "GET", None,
                 owner, laravel_owner, 422, "E0001", {"filters.scope": ["不支援此欄位"]})
    parity_error(f"/api/comments/9999999999", "PUT", {"body": None, "version": 1},
                 owner, laravel_owner, 422, "E0001", {"body": ["此欄位不可為 null"]})
    forged = {"value": 1, "source": "1"}
    for path, invalid in (
        (f"/api/articles/{article_id}", {"title": "偽造修改", "body": "偽造修改", "version": forged}),
        (f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": forged}),
    ):
        parity_error(path, "PUT", invalid, owner, laravel_owner, 422, "E0001", {"version": ["欄位型態不正確"]})
    check(sql(f"SELECT version||'|'||visibility FROM articles WHERE id={article_id}") == "1|visible", "偽造文章版本不得寫入")
    # 同值操作不動版本；可讀但非作者先回 403，作者過期版本才回 409。
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}", {"title": title, "body": body, "version": 1}, token=owner), 200)
    check(expect(call(NESTJS, "GET", f"/api/articles/{article_id}", token=owner), 200)["version"] == 1, "同值不得增加版本")
    parity_error(f"/api/articles/{article_id}", "PUT", {"title": title, "body": body, "version": 2}, owner, laravel_owner, 409, "E0201", None)
    parity_error(f"/api/articles/{article_id}", "PUT", {"title": title, "body": "更新", "version": 1}, other, laravel_other, 403, "E0003", None)
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}", {"title": title, "body": "更新", "version": 1}, token=owner), 200)
    check(expect(call(LARAVEL, "GET", f"/api/articles/{article_id}"), 200)["version"] == 2, "文章版本未跨後端更新")
    # 新增與編修留言後由 Laravel 讀取，檢查共享資料與不可偽造的版本。
    expect(call(NESTJS, "POST", f"/api/articles/{article_id}/comments", {"body": "留言 " + tag}, token=owner), 201)
    comment_id = created_id("/api/me/comments", owner, "body", "留言 " + tag)
    comment = expect(call(LARAVEL, "GET", f"/api/comments/{comment_id}", token=laravel_owner), 200)
    check(comment["created_backend"] == "nestjs" and comment["article_id"] == article_id, "跨後端留言讀取")
    check(expect(call(NESTJS, "GET", f"/api/comments/{comment_id}", token=owner), 200) == comment, "跨後端留言詳情不一致")
    for path, invalid in (
        (f"/api/comments/{comment_id}", {"body": "偽造修改", "version": forged}),
        (f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": forged}),
    ):
        parity_error(path, "PUT", invalid, owner, laravel_owner, 422, "E0001", {"version": ["欄位型態不正確"]})
    check(sql(f"SELECT version||'|'||visibility FROM comments WHERE id={comment_id}") == "1|visible", "偽造留言版本不得寫入")
    expect(call(NESTJS, "GET", f"/api/comments/{comment_id}", token=other), 403, "E0003")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}", {"body": "編修 " + tag, "version": 1}, token=owner), 200)
    check(sql(f"SELECT version FROM comments WHERE id={comment_id}") == "2", "留言編修版本未增加")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}", {"body": "編修 " + tag, "version": 2}, token=owner), 200)
    check(sql(f"SELECT version FROM comments WHERE id={comment_id}") == "2", "同值留言不得增加版本")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}", {"body": "編修 " + tag, "version": 1}, token=owner), 409, "E0201")
    check(expect(call(LARAVEL, "GET", f"/api/comments/{comment_id}", token=laravel_owner), 200)["body"] == "編修 " + tag, "留言編修未跨後端可見")
    # 父文章下架先隱藏存在；本人仍能讀自己的留言，管理員接管會改恢復權。
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 2}, token=owner), 200)
    unchanged = sql(f"SELECT version||'|'||updated_at::text FROM articles WHERE id={article_id}")
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 3}, token=owner), 200)
    check(sql(f"SELECT version||'|'||updated_at::text FROM articles WHERE id={article_id}") == unchanged, "同值文章下架不得變動版本／時間")
    expect(call(NESTJS, "GET", f"/api/articles/{article_id}"), 404, "E0004")
    parity_error(f"/api/articles/{article_id}", "PUT", {"title": title, "body": "看不到", "version": 3}, other, laravel_other, 404, "E0004", None)
    check(expect(call(NESTJS, "GET", f"/api/comments/{comment_id}", token=owner), 200) ==
          expect(call(LARAVEL, "GET", f"/api/comments/{comment_id}", token=laravel_owner), 200), "下架父文章之本人留言詳情不一致")
    expect(call(NESTJS, "GET", f"/api/comments/{comment_id}", token=other), 404, "E0004")
    expect(call(NESTJS, "POST", f"/api/articles/{article_id}/comments", {"body": "不可新增"}, token=owner), 403, "E0003")
    expect(call(NESTJS, "POST", f"/api/articles/{article_id}/comments", {"body": "不可新增"}, token=other), 404, "E0004")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}", {"body": "不可修改", "version": 2}, token=owner), 403, "E0003")
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "hidden", "version": 3}, token=admin), 200)
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "visible", "version": 4}, token=owner), 403, "E0003")
    check(sql(f"SELECT restore_control||'|'||version FROM articles WHERE id={article_id}") == "admin|4", "管理員接管未持久化")
    expect(call(NESTJS, "PUT", f"/api/articles/{article_id}/visibility", {"visibility": "visible", "version": 4}, token=admin), 200)
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 2}, token=owner), 200)
    unchanged = sql(f"SELECT version||'|'||updated_at::text FROM comments WHERE id={comment_id}")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 3}, token=owner), 200)
    check(sql(f"SELECT version||'|'||updated_at::text FROM comments WHERE id={comment_id}") == unchanged, "同值留言下架不得變動版本／時間")
    check(expect(call(NESTJS, "GET", "/api/me/comments?page=1&per_page=20", token=owner), 200)["list"][0]["can_restore"] is True, "作者應可恢復自行下架留言")
    expect(call(NESTJS, "GET", f"/api/comments/{comment_id}", token=other), 404, "E0004")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "hidden", "version": 3}, token=admin), 200)
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "visible", "version": 4}, token=owner), 403, "E0003")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment_id}/visibility", {"visibility": "visible", "version": 4}, token=admin), 200)
    check(sql(f"SELECT created_backend FROM articles WHERE id={article_id}") == "nestjs" and
          sql(f"SELECT created_backend FROM comments WHERE id={comment_id}") == "nestjs", "建立來源遭改寫")
    # Laravel 建立的同帳號文章再由 NestJS 讀取，補足相反方向的跨後端寫讀。
    check(expect(call(LARAVEL, "POST", "/api/articles", {"title": "Laravel " + tag, "body": "共享"}, token=laravel_owner), 201) == {}, "Laravel 發文回應")
    laravel_article = created_id("/api/me/articles", owner, "title", "Laravel " + tag)
    check(expect(call(NESTJS, "GET", f"/api/articles/{laravel_article}"), 200)["created_backend"] == "laravel", "跨後端來源讀取")
    expect(call(LARAVEL, "PUT", f"/api/articles/{laravel_article}",
                {"title": "Laravel " + tag, "body": "Laravel 編修", "version": 1}, token=laravel_owner), 200)
    check(expect(call(NESTJS, "GET", f"/api/articles/{laravel_article}"), 200)["body"] == "Laravel 編修", "反向跨後端文章編修")
    check(expect(call(LARAVEL, "POST", f"/api/articles/{laravel_article}/comments", {"body": "Laravel 留言 " + tag}, token=laravel_owner), 201) == {}, "Laravel 留言回應")
    laravel_comment = created_id("/api/me/comments", owner, "body", "Laravel 留言 " + tag)
    check(expect(call(NESTJS, "GET", f"/api/comments/{laravel_comment}", token=owner), 200)["created_backend"] == "laravel", "反向跨後端留言讀取")
    # 同帳號兩篇文章／兩則留言分頁；第二頁與超頁的 ID、總數和原頁碼必須精確。
    for path, expected in (("/api/me/articles", [laravel_article, article_id]),
                           ("/api/me/comments", [laravel_comment, comment_id])):
        for page in (1, 2, 3):
            data = expect(call(NESTJS, "GET", f"{path}?page={page}&per_page=1", token=owner), 200)
            check([row["id"] for row in data["list"]] == (expected[page - 1:page] if page < 3 else []) and
                  data["pagination"] == {"page": page, "per_page": 1, "total": 2, "total_pages": 2}, "本人列表分頁不符")
            check(data == expect(call(LARAVEL, "GET", f"{path}?page={page}&per_page=1", token=laravel_owner), 200), "本人列表跨後端不一致")
    print("PASS ticket41 HTTP: 06,08-11,13-16; 401/403/404/409; same-value; takeover; cross-backend")
    # 同一版本並行編修只能有一筆提交，另一筆依最新版本回 409。
    for kind, resource_id, path, first, second in (
        ("article", article_id, f"/api/articles/{article_id}",
         {"title": title, "body": "並行甲", "version": 5}, {"title": title, "body": "並行乙", "version": 5}),
        ("comment", comment_id, f"/api/comments/{comment_id}",
         {"body": "並行甲", "version": 5}, {"body": "並行乙", "version": 5}),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(call, NESTJS, "PUT", path, first, owner)
            b = pool.submit(call, NESTJS, "PUT", path, second, owner)
            outcomes = [a.result(timeout=20), b.result(timeout=20)]
        check(sorted(item[0] for item in outcomes) == [200, 409], f"{kind} 版本競爭結果不符")
        for item in outcomes:
            expect(item, item[0], "E0201" if item[0] == 409 else None)
        table = "articles" if kind == "article" else "comments"
        check(sql(f"SELECT version||'|'||body FROM {table} WHERE id={resource_id}") in
              ("6|並行甲", "6|並行乙"), f"{kind} 版本競爭持久結果不符")
    print("PASS ticket41 article/comment concurrent version writes: one 200, one 409 each")
    # 同時提交另一帳號留言與管理員下架，結果只能符合先取得文章鎖的一方。
    expect(call(NESTJS, "POST", "/api/articles", {"title": "Race " + tag, "body": "共享"}, token=owner), 201)
    race_id = created_id("/api/me/articles", owner, "title", "Race " + tag)
    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(call, NESTJS, "POST", f"/api/articles/{race_id}/comments", {"body": "Race留言 " + tag}, other)
        hide = pool.submit(call, LARAVEL, "PUT", f"/api/articles/{race_id}/visibility", {"visibility": "hidden", "version": 1}, laravel_owner)
        created = create.result(timeout=20)
        hidden = hide.result(timeout=20)
    expect(hidden, 200)
    check(created[0] in (201, 404), f"父文章競爭結果不符：{created[0]}")
    count = sql(f"SELECT count(*) FROM comments WHERE article_id={race_id} AND body='Race留言 {tag}'")
    check(count == ("1" if created[0] == 201 else "0"), "父文章競爭留言持久結果不符")
    print("PASS ticket41 parent concurrency: hide=200, comment=" + str(created[0]))
    # 同一帳號的登出與發文共用帳號/session 鎖；同時請求只能對應一種持久結果。
    race_token, _, race_author = member(tag + "-logout")
    race_title = "Logout race " + tag
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(call, NESTJS, "POST", "/api/articles", {"title": race_title, "body": "測試"}, race_token)
        logout = pool.submit(call, NESTJS, "POST", "/api/auth/logout", {}, race_token)
        written = write.result(timeout=20)
        logged_out = logout.result(timeout=20)
    expect(logged_out, 200)
    check(written[0] in (201, 401), f"撤銷競爭狀態不符：{written[0]}")
    persisted = sql(f"SELECT count(*) FROM articles WHERE author_id={race_author} AND title='{race_title}'")
    check(persisted == ("1" if written[0] == 201 else "0"), "撤銷競爭持久結果不符")
    expect(call(NESTJS, "POST", "/api/articles", {"title": "After logout " + tag, "body": "測試"}, token=race_token), 401, "E0002")
    print("PASS ticket41 logout/write concurrency: logout=200, write=" + str(written[0]))
    # 實際 HTTP SQL 日誌與查詢計畫都須表明列表在 DB 完成計數、排序與分頁。
    for table, path in (("articles", "/api/me/articles"), ("comments", "/api/me/comments")):
        list_query_plans(table, path, owner, owner_id)


if __name__ == "__main__":
    main()
