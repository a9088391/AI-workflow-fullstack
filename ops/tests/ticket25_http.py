"""Ticket 25：只在隔離 Compose 資料庫驗證 Gin 的共同 HTTP 契約。"""

from contextlib import contextmanager

import json
import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

GIN = "http://127.0.0.1:8087"
LARAVEL = "http://127.0.0.1:8091"
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出", "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能", "E0004": "找不到指定資料或 API",
    "E0101": "帳號或密碼錯誤", "E0102": "此帳號已停用，請聯絡系統管理員",
    "E0104": "後台認證設定異常，請聯絡系統管理員",
    "E0105": "登入失敗次數過多，請稍後再試",
    "E0005": "系統暫時無法處理，請稍後再試",
}


# sql 僅透過已核對的隔離容器執行固定測試 SQL，回傳單值或查詢計畫。
def sql(query):
    command = ["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A", "-U", "articles_owner", "-d", "articles", "-c", query]
    return subprocess.check_output(command, text=True).strip()


# check 讓第一個契約偏差立即中止，避免後續寫入掩蓋原因。
def check(condition, message):
    if not condition:
        raise AssertionError(message)


# guard_backend 確認 HTTP 埠實際連到本票容器與唯讀金鑰，避免對其他專案發送寫入。
def guard_backend(service, port):
    ids = subprocess.check_output(["docker", "compose", "--profile", "gin", "ps", "-q", service], text=True).splitlines()
    check(len(ids) == 1, "隔離後端容器未唯一啟動：" + service)
    inspected = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    labels = inspected["Config"]["Labels"]
    check(labels.get("com.docker.compose.project") == "ticket25_test" and labels.get("com.docker.compose.service") == service, "HTTP 容器屬於其他專案：" + service)
    state = inspected["State"]
    check(state["Running"] and state.get("Health", {}).get("Status") == "healthy", "HTTP 容器未健康執行：" + service)
    # 核對實際 HostConfig，避免設定檔正確但測試網址指向其他容器。
    binding = inspected["HostConfig"]["PortBindings"].get("8000/tcp")
    check(binding == [{"HostIp": "127.0.0.1", "HostPort": port}], "HTTP 實際埠綁定不符：" + service)
    expected_source = os.path.realpath(f".local/ticket25-backend/{service}.key")
    destination = f"/run/secrets/{service}-jwt.key"
    check(any(mount.get("Type") == "bind" and os.path.realpath(mount.get("Source", "")) == expected_source and mount.get("Destination") == destination and mount.get("RW") is False for mount in inspected["Mounts"]), "HTTP 測試金鑰掛載不符：" + service)


# guard 寫入前核對專案、專用 volume、服務來源、fixture 標記與 HTTP 身分。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket25_test", "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == "compose.yaml:ops/tests/ticket25_compose.yaml", "錯誤 Compose 檔")
    config = json.loads(subprocess.check_output(["docker", "compose", "--profile", "gin", "config", "--format", "json"], text=True))
    check(config["name"] == "ticket25_test" and config["volumes"]["postgres_data"]["name"] == "ticket25_test_postgres_data", "非隔離資料卷")
    for service, port in (("gin", "8087"), ("laravel", "8091"), ("nginx", "8090")):
        configured = config["services"][service]["ports"][0]
        check(configured["published"] == port and configured["host_ip"] == "127.0.0.1", "服務埠不符：" + service)
    container = subprocess.check_output(["docker", "compose", "--profile", "gin", "ps", "-q", "db"], text=True).strip()
    check(bool(container), "隔離 DB 容器未啟動")
    inspected = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
    check(inspected["Config"]["Labels"]["com.docker.compose.project"] == "ticket25_test", "DB 容器屬於其他專案")
    check(any(mount.get("Name") == "ticket25_test_postgres_data" for mount in inspected["Mounts"]), "DB 實際資料卷不符")
    for service, port in (("gin", "8087"), ("laravel", "8091")):
        guard_backend(service, port)
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "資料庫身分不符")
    check(config["services"]["db"]["environment"]["TICKET25_FIXTURE"] == "isolated-ticket25", "fixture 標記不符")
    for name in ("gin", "laravel"):
        with open(f".local/ticket25-backend/{name}.key", "rb") as test_key, open(f".local/keys/{name}.key", "rb") as demo_key:
            check(test_key.read() != demo_key.read(), "測試與展示共用 JWT 金鑰：" + name)
    for base in (GIN, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")


# call 保留原始 Body/Query，測試解析與 CORS 時不讓客戶端替我們正規化。
def call(base, method, path, body=None, token=None, headers=None):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = "Bearer " + token
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode()
        request_headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        body = body.encode()
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(base + path, data=body, headers=request_headers, method=method)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, response.headers


# expect 精確比對 HTTP、共用文案、errors 與 envelope 欄位。
def expect(result, status, code=None, fields=None):
    actual, payload, _ = result
    check(actual == status, f"HTTP {actual}，預期 {status}/{code}: {payload}")
    if code is None:
        check(set(payload) == {"success", "message", "data"} and payload["success"] is True and payload["message"] == "OK", "成功 envelope")
        return payload["data"]
    keys = {"success", "message", "error_code", "log_id"}
    if fields is not None:
        keys.add("errors")
    check(set(payload) == keys and payload["success"] is False and payload["message"] == MESSAGES[code] and payload["error_code"] == code and payload["log_id"], "錯誤 envelope")
    if fields is not None:
        check(payload["errors"] == fields, f"errors 不符: {payload['errors']}")
    return payload


# register 建立唯一測試帳號，並回傳 Gin 登入資料供後續互通驗證。
def register(tag, nickname="Ticket25"):
    email = f"{tag}@example.test"
    expect(call(GIN, "POST", "/api/auth/register", {"email": " " + email.upper() + " ", "password": "abcd1234", "nickname": nickname}), 201)
    data = expect(call(GIN, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"}), 200)
    check(data["user"]["email"] == email and data["user"]["nickname"] == nickname, "Email 正規化或暱稱")
    return email, data


# sign_token 僅使用 fixture 專用金鑰製造錯誤 claims，驗證伺服器不接受變體。
def sign_token(header, payload):
    with open(".local/ticket25-backend/gin.key", "rb") as key_file:
        key = base64.b64decode(key_file.read().strip())
    # 測試簽章需保留指定 JSON 的位元組，才能驗證重複鍵情境。
    def segment(value):
        raw = value.encode() if isinstance(value, str) else json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")
    unsigned = segment(header) + b"." + segment(payload)
    signature = base64.urlsafe_b64encode(hmac.new(key, unsigned, hashlib.sha256).digest()).rstrip(b"=")
    return (unsigned + b"." + signature).decode()


# settings_restore 沿用原 SQL capture／boolean 白名單／finally，不把還原邏輯複寫到 driver。
@contextmanager
def settings_restore(*, execute=None):
    if execute is None:
        execute = sql
    original = execute("SELECT enabled||','||window_minutes||','||failure_threshold||','||lock_seconds FROM login_lockout_settings WHERE id=1")
    try:
        yield original
    finally:
        enabled, window, threshold, seconds = original.split(",")
        # 串接 SQL 的 boolean 輸出是 true/false；拒絕未知值，才能精確還原原設定。
        check(enabled in ("true", "false"), "原鎖定設定 boolean 格式不符")
        execute(f"UPDATE login_lockout_settings SET enabled={enabled},window_minutes={int(window)},failure_threshold={int(threshold)},lock_seconds={int(seconds)} WHERE id=1")


# main 用兩個後端及隔離 SQL fixture 驗證輸入、登入、鎖定與內容可見性。
def main():
    guard()
    nonce = secrets.token_hex(5)
    email = f"gin{nonce}@example.test"
    initial = sql("SELECT count(*) FROM accounts")
    bad = call(GIN, "POST", "/api/auth/register?unknown=1", {"email": email, "password": "a", "nickname": "ok"})
    expect(bad, 422, "E0001", {"unknown": ["不支援此欄位"]})
    expect(call(GIN, "POST", "/api/auth/register", {"email": email, "password": "a", "nickname": "\u0000", "other": 1}), 422, "E0001", {"password": ["欄位格式或範圍不正確"], "nickname": ["欄位格式或範圍不正確"], "other": ["不支援此欄位"]})
    # JSON object 的最後同名值參與完整驗證；重複 Query 仍另行拒絕。
    expect(call(GIN, "POST", "/api/auth/login", '{"email":"bad","email":"absent-ticket25@example.test","password":"abcd"}'), 401, "E0101")
    expect(call(GIN, "POST", "/api/auth/login", '{"email":"absent-ticket25@example.test","email":"bad","password":"abcd"}'), 422, "E0001", {"email": ["欄位格式或範圍不正確"]})
    expect(call(GIN, "POST", "/api/auth/register", '{"email":"a@example.test","email":null,"password":true,"other":1}'), 422, "E0001", {"email": ["此欄位不可為 null"], "password": ["欄位型態不正確"], "nickname": ["此欄位為必填"], "other": ["不支援此欄位"]})
    expect(call(GIN, "POST", "/api/auth/register", '{bad'), 400, "E0005")
    expect(call(GIN, "POST", "/api/auth/register", '{"email":"a@example.test","password":"abcd","nickname":"\\ud800"}'), 400, "E0005")
    check(sql("SELECT count(*) FROM accounts") == initial, "無效輸入改變帳號")
    email, user = register("gin" + nonce, "  保留 😀  ")
    # U+212A 不可由 Unicode 小寫規則偽裝成 ASCII k，也不可增加既有帳號的失敗次數。
    kelvin_email, kelvin_user = register("k" + nonce)
    kelvin_id = kelvin_user["user"]["id"]
    invalid_kelvin = "K" + kelvin_email[1:]
    account_count = sql("SELECT count(*) FROM accounts")
    failures_before = sql(f"SELECT count(*) FROM login_failures WHERE account_id={kelvin_id}")
    expect(call(GIN, "POST", "/api/auth/register", {"email": invalid_kelvin, "password": "abcd1234", "nickname": "Kelvin"}), 422, "E0001", {"email": ["欄位格式或範圍不正確"]})
    expect(call(GIN, "POST", "/api/auth/login", {"email": invalid_kelvin, "password": "wrong123"}), 422, "E0001", {"email": ["欄位格式或範圍不正確"]})
    check(sql("SELECT count(*) FROM accounts") == account_count and sql(f"SELECT count(*) FROM login_failures WHERE account_id={kelvin_id}") == failures_before, "非 ASCII Email 改變帳號或失敗計數")
    first = user["access_token"]
    expect(call(GIN, "GET", "/api/me", token=first), 200)
    expect(call(GIN, "GET", "/api/me?x=1", token=first), 422, "E0001", {"x": ["不支援此欄位"]})
    expect(call(GIN, "GET", "/api/me", body="{}", token=first), 422, "E0001", {"_body": ["此 API 不接受 Request Body"]})
    expect(call(GIN, "POST", "/api/auth/register", {"email": email, "password": "abcd1234", "nickname": "ok"}), 422, "E0001", {"email": ["此 Email 已註冊"]})
    laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"}), 200)
    expect(call(GIN, "GET", "/api/me", token=laravel["access_token"]), 401, "E0002")
    second = expect(call(GIN, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"}), 200)["access_token"]
    claims = json.loads(base64.urlsafe_b64decode(second.split(".")[1] + "=="))
    for changed_header, changed_claims in (({"alg": "none", "typ": "JWT"}, claims),
                                           ({"alg": "HS256", "typ": "JWT"}, {**claims, "aud": "wrong"}),
                                           ({"alg": "HS256", "typ": "JWT"}, {key: value for key, value in claims.items() if key != "exp"}),
                                           ({"alg": "HS256", "typ": "JWT"}, {**claims, "exp": float(claims["exp"])})):
        expect(call(GIN, "GET", "/api/me", token=sign_token(changed_header, changed_claims)), 401, "E0002")
    duplicate_claims = json.dumps(claims, separators=(",", ":"))[:-1] + ',"sub":"999"}'
    expect(call(GIN, "GET", "/api/me", token=sign_token({"alg": "HS256", "typ": "JWT"}, duplicate_claims)), 401, "E0002")
    # 已簽署的 sub 仍須使用無前導零的正規十進位字串。
    expect(call(GIN, "GET", "/api/me", token=sign_token({"alg": "HS256", "typ": "JWT"}, {**claims, "sub": "0" + claims["sub"]})), 401, "E0002")
    expect(call(GIN, "POST", "/api/auth/logout", body={}, token=first), 200)
    expect(call(GIN, "GET", "/api/me", token=first), 401, "E0002")
    expect(call(GIN, "GET", "/api/me", token=second), 200)
    account_id = user["user"]["id"]
    sql(f"UPDATE accounts SET status='disabled' WHERE id={account_id}")
    expect(call(GIN, "GET", "/api/me", token=second), 403, "E0102")
    expect(call(GIN, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"}), 403, "E0102")
    sql(f"UPDATE accounts SET status='active' WHERE id={account_id}")
    sql(f"UPDATE accounts SET password_hash='broken' WHERE id={account_id}")
    expect(call(GIN, "POST", "/api/auth/login", {"email": email, "password": "abcd1234"}), 500, "E0104")
    # 以新帳號測鎖定，避免直接修復故意毀損的跨後端雜湊。
    lock_email, lock_user = register("lock" + nonce)
    lock_id = lock_user["user"]["id"]
    with settings_restore():
        sql("UPDATE login_lockout_settings SET enabled=true,window_minutes=5,failure_threshold=2,lock_seconds=120 WHERE id=1")
        wrong = {"email": lock_email, "password": "wrong123"}
        gin_barrier = threading.Barrier(2)
        # 同時送兩次有效格式的錯密碼，驗證設定列與帳號列的序列化效果。
        def wrong_login(_):
            gin_barrier.wait(timeout=5)
            return call(GIN, "POST", "/api/auth/login", wrong)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(wrong_login, range(2)))
        check(sorted(item[0] for item in results) == [401, 429], "並行門檻應只有 401 與 429")
        locked = next(item for item in results if item[0] == 429)
        expect(locked, 429, "E0105")
        check(int(locked[2]["Retry-After"]) > 0, "鎖定缺少 Retry-After")
        expect(call(GIN, "POST", "/api/auth/login", {"email": lock_email, "password": "abcd1234"}), 429, "E0105")
        cors_lock = call(GIN, "POST", "/api/auth/login", {"email": lock_email, "password": "abcd1234"}, headers={"Origin": "http://localhost:8090"})
        check(cors_lock[0] == 429 and cors_lock[2]["Access-Control-Expose-Headers"] == "Retry-After", "鎖定 CORS 標頭")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={lock_id}") == "2", "並行失敗事件數")
        cross_email, cross_user = register("cross" + nonce)
        cross_id = cross_user["user"]["id"]
        cross_barrier = threading.Barrier(2)
        # 兩個後端同時錯密碼，須共用設定列／帳號列鎖並只保存兩筆事件。
        def cross_wrong_login(base):
            cross_barrier.wait(timeout=5)
            return call(base, "POST", "/api/auth/login", {"email": cross_email, "password": "wrong123"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            cross_results = list(pool.map(cross_wrong_login, (GIN, LARAVEL)))
        check(sorted(item[0] for item in cross_results) == [401, 429], "跨後端並行門檻")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={cross_id}") == "2", "跨後端失敗事件數")
        sql("UPDATE login_lockout_settings SET enabled=false WHERE id=1")
        expect(call(GIN, "POST", "/api/auth/login", wrong), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={lock_id}") == "2", "停用鎖定仍計數")
        expect(call(GIN, "POST", "/api/auth/login", {"email": lock_email, "password": "abcd1234"}), 200)
    # 文章與留言由隔離資料庫建立，驗證 Gin 僅讀取核准欄位與範圍。
    article = sql(f"INSERT INTO articles (author_id,title,body,visibility,created_backend,created_at,updated_at) VALUES ({lock_id},'公開','本文','visible','laravel',clock_timestamp(),clock_timestamp()) RETURNING id").splitlines()[-1]
    hidden = sql(f"INSERT INTO articles (author_id,title,body,visibility,restore_control,created_backend,created_at,updated_at) VALUES ({lock_id},'隱藏','本文','hidden','author','laravel',clock_timestamp(),clock_timestamp()) RETURNING id").splitlines()[-1]
    comment = sql(f"INSERT INTO comments (author_id,article_id,body,visibility,created_backend,created_at,updated_at) VALUES ({lock_id},{article},'公開留言','visible','laravel',clock_timestamp(),clock_timestamp()) RETURNING id").splitlines()[-1]
    sql(f"INSERT INTO comments (author_id,article_id,body,visibility,restore_control,created_backend,created_at,updated_at) VALUES ({lock_id},{article},'隱藏留言','hidden','author','laravel',clock_timestamp(),clock_timestamp())")
    author = expect(call(GIN, "POST", "/api/auth/login", {"email": lock_email, "password": "abcd1234"}), 200)["access_token"]
    admin_email, admin_data = register("admin" + nonce)
    admin_id = admin_data["user"]["id"]
    sql(f"UPDATE accounts SET role='admin' WHERE id={admin_id}")
    admin = admin_data["access_token"]
    visible = expect(call(GIN, "GET", "/api/articles?page=1&per_page=20"), 200)
    check(all(item["visibility"] == "visible" for item in visible["list"]), "訪客列表洩漏下架")
    default_admin = expect(call(GIN, "GET", "/api/articles?page=1&per_page=20", token=admin), 200)
    check(default_admin["pagination"]["total"] == visible["pagination"]["total"], "管理員預設範圍")
    expect(call(GIN, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management"), 401, "E0002")
    expect(call(GIN, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=x&filters%5Bscope%5D=management"), 401, "E0002")
    expect(call(GIN, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management", token=author), 403, "E0003")
    managed = expect(call(GIN, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management", token=admin), 200)
    check(any(item["id"] == int(hidden) for item in managed["list"]), "管理範圍未含下架")
    expect(call(GIN, "GET", f"/api/articles/{hidden}"), 404, "E0004")
    check(expect(call(GIN, "GET", f"/api/articles/{hidden}", token=author), 200)["can_restore"] is True, "作者下架可讀")
    expect(call(GIN, "GET", f"/api/articles/{hidden}/comments?page=1&per_page=20"), 404, "E0004")
    expect(call(GIN, "GET", f"/api/articles/{hidden}/comments?page=1&per_page=20", token=author), 200)
    guest_comments = expect(call(GIN, "GET", f"/api/articles/{article}/comments?page=1&per_page=20"), 200)
    check([item["id"] for item in guest_comments["list"]] == [int(comment)], "訪客看到個別下架留言")
    admin_comments = expect(call(GIN, "GET", f"/api/articles/{article}/comments?page=1&per_page=20", token=admin), 200)
    check(admin_comments["pagination"]["total"] == 2, "管理員留言範圍")
    expect(call(GIN, "GET", "/api/articles?page=9007199254740991&per_page=20"), 200)
    expect(call(GIN, "GET", "/api/articles?per_page=20"), 422, "E0001", {"page": ["此欄位為必填"]})
    expect(call(GIN, "GET", "/api/articles?page=1&page=2&per_page=20"), 422, "E0001", {"page": ["此參數不可重複"]})
    expect(call(GIN, "GET", "/api/articles/?page=1&per_page=20"), 404, "E0004")
    expect(call(GIN, "GET", "/api/articles?page[]=1&per_page=20"), 422, "E0001", {"page": ["欄位型態不正確"]})
    expect(call(GIN, "GET", "/api/articles/1.5"), 422, "E0001", {"article_id": ["欄位格式或範圍不正確"]})
    expect(call(GIN, "GET", f"/api/articles/{article}", token="bad"), 401, "E0002")
    expect(call(GIN, "GET", f"/api/articles/{article}", headers={"Authorization": ""}), 401, "E0002")
    expect(call(GIN, "DELETE", f"/api/articles/{article}"), 405, "E0005")
    preflight = call(GIN, "OPTIONS", "/api/articles", headers={"Origin": "http://localhost:8090", "Access-Control-Request-Method": "GET"})
    check(preflight[0] == 204 and preflight[2]["Access-Control-Expose-Headers"] == "Retry-After" and {"GET", "POST", "PUT", "OPTIONS"} <= set(preflight[2]["Access-Control-Allow-Methods"].replace(" ", "").split(",")), "CORS preflight")
    # 擴充隔離測資並更新統計，量測與實際列表相同的 WHERE/JOIN/ORDER BY/COUNT。
    sql(f"INSERT INTO articles (author_id,title,body,visibility,created_backend,created_at,updated_at) SELECT {lock_id},'測試文章'||g,'本文','visible','laravel',clock_timestamp()-g*interval '1 second',clock_timestamp() FROM generate_series(1,256) AS g")
    sql(f"INSERT INTO comments (author_id,article_id,body,visibility,created_backend,created_at,updated_at) SELECT {lock_id},{article},'測試留言'||g,'visible','laravel',clock_timestamp()-g*interval '1 second',clock_timestamp() FROM generate_series(1,256) AS g")
    sql("ANALYZE articles; ANALYZE comments")
    print("FIXTURE counts:", sql("SELECT (SELECT count(*) FROM articles)||' articles, '||(SELECT count(*) FROM comments)||' comments'"))
    print("PASS ticket25 HTTP: validation, Argon2 cross-backend, JWT/session, lockout concurrency, public visibility, pagination, CORS")
    print("PLAN articles count:", sql("EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible'"))
    print("PLAN articles list:", sql("EXPLAIN (ANALYZE, BUFFERS) SELECT articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible' ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0"))
    print("PLAN comments count:", sql(f"EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id={article} AND comments.visibility='visible'"))
    print("PLAN comments list:", sql(f"EXPLAIN (ANALYZE, BUFFERS) SELECT comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id={article} AND comments.visibility='visible' ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0"))


if __name__ == "__main__":
    main()
