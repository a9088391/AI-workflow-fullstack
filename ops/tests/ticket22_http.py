"""Ticket 22 隔離真實 HTTP、共用登入交易與公開閱讀驗證。"""

import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row


FASTAPI = "http://fastapi:8000"
LARAVEL = "http://laravel:8000"
PASSWORD = "Abc1234"
WRONG = "Wrong1234"
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
    "E0005": "系統暫時無法處理，請稍後再試",
    "E0101": "帳號或密碼錯誤",
    "E0102": "此帳號已停用，請聯絡系統管理員",
    "E0105": "登入失敗次數過多，請稍後再試",
}


# 固定內部主機及路徑；redirect 視為錯誤，避免把 JWT 送至其他目標。
class NoRedirect(HTTPRedirectHandler):
    # 保留 30x 為失敗結果，不跟隨可能攜帶憑證的請求。
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


# 僅輸出安全的受控驗證訊息，不印出密碼、JWT 或原始例外。
class CheckFailure(Exception):
    pass


# 明確拋出受控失敗，避免 Python -O 關閉驗收檢查。
def must(condition, reason):
    if not condition:
        raise CheckFailure("Ticket 22 驗證失敗：" + reason)


# 只用 ticket20_test 網路內的固定 owner 身分查核及建立合成資料。
def owner_connection():
    return psycopg.connect(
        host="db", port=5432, dbname="articles", user="articles_owner",
        password=os.environ["DB_OWNER_PASSWORD"], row_factory=dict_row,
        connect_timeout=3, options="-c statement_timeout=20000",
    )


# 所有寫入前核對 Compose 專案、實際資料庫身分、schema 與兩個固定健康端點。
def guard():
    must(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket20_test", "錯誤 Compose 專案")
    for key, value in (("DB_HOST", "db"), ("DB_DATABASE", "articles"),
                       ("DB_OWNER_USER", "articles_owner")):
        must(os.environ.get(key) == value, key + " 不符")
    must(bool(os.environ.get("DB_OWNER_PASSWORD")) and bool(os.environ.get("DB_APP_PASSWORD")),
         "隔離資料庫密碼缺失")
    try:
        with owner_connection() as connection:
            row = connection.execute("SELECT current_database() AS db, current_user AS role").fetchone()
            must(row == {"db": "articles", "role": "articles_owner"}, "資料庫實際身分不符")
            tables = connection.execute(
                "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_name IN ('accounts','login_sessions','login_failures',"
                "'login_lockout_settings','articles','comments')"
            ).fetchone()["n"]
            must(tables == 6, "共用資料表不完整")
    except psycopg.Error:
        raise CheckFailure("Ticket 22 驗證失敗：資料庫身分查核失敗") from None
    for base in (FASTAPI, LARAVEL):
        try:
            with OPENER.open(base + "/up", timeout=5) as response:
                must(response.status == 200 and response.read() == b"OK", "固定 API 健康內容不符")
        except (HTTPError, URLError, TimeoutError):
            raise CheckFailure("Ticket 22 驗證失敗：固定 API 健康目標不可用") from None


# 保留原始 Body/Content-Type 測框架差異，並拒絕未知網路結果自動重送。
def call(base, method, path, body=None, token=None, content_type="application/json", origin=None):
    must(base in (FASTAPI, LARAVEL) and path.startswith("/"), "HTTP 目標不受允許")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if origin:
        headers["Origin"] = origin
    raw = None
    if body is not None:
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        if content_type:
            headers["Content-Type"] = content_type
    request = Request(base + path, data=raw, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=20) as response:
            status, payload, response_headers = response.status, response.read(), response.headers
    except HTTPError as response:
        status, payload, response_headers = response.code, response.read(), response.headers
    except (URLError, TimeoutError):
        raise CheckFailure("Ticket 22 驗證失敗：HTTP 結果未知，停止重送") from None
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError):
        raise CheckFailure("Ticket 22 驗證失敗：API 未回 JSON") from None
    must(isinstance(result, dict), "API envelope 非 object")
    return status, result, response_headers


# 對每個案例檢查逐字文案、envelope、欄位錯誤和 Retry-After。
def expect(result, status, code=None, errors=None):
    actual, payload, headers = result
    must(actual == status, f"HTTP 狀態預期 {status} 實際 {actual}，code={payload.get('error_code')}")
    if code is None:
        must(set(payload) == {"success", "message", "data"}, "成功 envelope 欄位不符")
        must(payload["success"] is True and payload["message"] == "OK", "成功 envelope 值不符")
        must(isinstance(payload["data"], dict), "成功 data 型態不符")
        return payload["data"]
    fields = {"success", "message", "error_code", "log_id"}
    if errors is not None:
        fields.add("errors")
    must(set(payload) == fields, "失敗 envelope 欄位不符")
    must(payload["success"] is False and payload["error_code"] == code and
         payload["message"] == MESSAGES[code], "失敗代碼或逐字文案不符")
    must(isinstance(payload["log_id"], str) and bool(payload["log_id"]), "缺少 log_id")
    if errors is not None:
        must(payload["errors"] == errors, "欄位 errors 不符")
    if code == "E0105":
        must(headers.get("Retry-After", "").isdigit() and int(headers["Retry-After"]) >= 1,
             "Retry-After 缺失或無效")
    return payload


# 每次執行建立新帳號；兩種雜湊來源都由另一後端登入驗證。
def member(base, namespace, label):
    email = f"t22-{namespace}-{label}@example.test"
    expect(call(base, "POST", "/api/auth/register", {
        "email": email, "password": PASSWORD, "nickname": label,
    }), 201)
    with owner_connection() as connection:
        account = connection.execute("SELECT id, password_hash FROM accounts WHERE email = %s", (email,)).fetchone()
    must(account is not None and account["password_hash"].startswith("$argon2id$v=19$m=19456,t=2,p=1$"),
         "註冊雜湊格式不符")
    return {"email": email, "id": account["id"], "nickname": label}


# 登入成功才回憑證，並核對回應帳號與固定 token 類型。
def login(base, user, password=PASSWORD):
    data = expect(call(base, "POST", "/api/auth/login", {
        "email": user["email"], "password": password,
    }), 200)
    must(data["token_type"] == "Bearer" and data["user"]["id"] == user["id"] and
         data["user"]["email"] == user["email"] and isinstance(data["access_token"], str),
         "登入資料不符")
    return data["access_token"]


# SQL 僅插入本次帳號擁有的文章與留言，不刪除或覆寫既有內容。
def content(author, article_id, label, visibility="visible"):
    with owner_connection() as connection:
        if article_id is None:
            return connection.execute(
                "INSERT INTO articles (author_id,title,body,visibility,restore_control,created_backend,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,'laravel',clock_timestamp(),clock_timestamp()) RETURNING id",
                (author["id"], label, "內文 " + label, visibility,
                 "author" if visibility == "hidden" else None),
            ).fetchone()["id"]
        return connection.execute(
            "INSERT INTO comments (author_id,article_id,body,visibility,restore_control,created_backend,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'laravel',clock_timestamp(),clock_timestamp()) RETURNING id",
            (author["id"], article_id, "留言 " + label, visibility,
             "author" if visibility == "hidden" else None),
        ).fetchone()["id"]


# 比對同一資料列在兩後端輸出的全部公共欄位，時間一律按台北業務時區。
def resource(table, row, nickname, can_restore=False):
    # callback 將持久時間轉成共同台北格式，避免主機時區改變公共資源 oracle。
    stamp = lambda value: value.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    data = {"id": row["id"], "author": {"id": row["author_id"], "nickname": nickname},
            "body": row["body"], "visibility": row["visibility"], "version": row["version"],
            "created_backend": row["created_backend"], "created_at": stamp(row["created_at"]),
            "updated_at": stamp(row["updated_at"]), "can_restore": can_restore}
    if table == "articles":
        data["title"] = row["title"]
    else:
        data["article_id"] = row["article_id"]
    return data


# 只容許兩個內容資料表，以固定 ID 取得持久化快照。
def row(table, item_id):
    must(table in ("articles", "comments"), "資料表不受允許")
    with owner_connection() as connection:
        return connection.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,)).fetchone()


# 格式錯誤不計失敗；有效錯密碼由兩後端共同累加，門檻當次立即鎖定。
def lockout(namespace):
    user = member(FASTAPI, namespace, "lock")
    login(FASTAPI, user)
    with owner_connection() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1").fetchone()
        before = connection.execute("SELECT failure_epoch FROM accounts WHERE id = %s", (user["id"],)).fetchone()
    must(settings["enabled"] and settings["failure_threshold"] >= 2, "鎖定設定不適合受控驗證")
    bad = {"email": user["email"], "password": "bad!"}
    expect(call(FASTAPI, "POST", "/api/auth/login", bad), 422, "E0001",
           {"password": ["欄位格式或範圍不正確"]})
    with owner_connection() as connection:
        n = connection.execute("SELECT count(*) AS n FROM login_failures WHERE account_id = %s", (user["id"],)).fetchone()["n"]
    must(n == 0, "無效格式累計失敗")
    for index in range(settings["failure_threshold"]):
        base = FASTAPI if index % 2 == 0 else LARAVEL
        result = call(base, "POST", "/api/auth/login", {"email": user["email"], "password": WRONG},
                      origin="http://localhost:8080")
        code = "E0105" if index == settings["failure_threshold"] - 1 else "E0101"
        expect(result, 429 if code == "E0105" else 401, code)
        if code == "E0105":
            must(result[2].get("Access-Control-Allow-Origin") == "http://localhost:8080" and
                 result[2].get("Access-Control-Expose-Headers") == "Retry-After", "鎖定 CORS 標頭不符")
    expect(call(FASTAPI, "POST", "/api/auth/login", {"email": user["email"], "password": PASSWORD}),
           429, "E0105")
    with owner_connection() as connection:
        state = connection.execute("SELECT failure_epoch,locked_until FROM accounts WHERE id = %s", (user["id"],)).fetchone()
        count = connection.execute("SELECT count(*) AS n FROM login_failures WHERE account_id = %s AND failure_epoch = %s",
                                   (user["id"], state["failure_epoch"])).fetchone()["n"]
    must(count == settings["failure_threshold"] and state["locked_until"] is not None,
         "跨後端鎖定持久狀態不符")
    # 對本次帳號模擬到期，不等待完整鎖定秒數；下一次成功須切換世代。
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = clock_timestamp() - interval '1 second' WHERE id = %s",
                           (user["id"],))
    login(FASTAPI, user)
    with owner_connection() as connection:
        after = connection.execute("SELECT failure_epoch,locked_until FROM accounts WHERE id = %s", (user["id"],)).fetchone()
    must(after["failure_epoch"] == state["failure_epoch"] + 2 and after["locked_until"] is None,
         "到期及成功未各重設世代")
    # 移出本次帳號的一筆事件到視窗外；原始事件仍留存，但不能提前觸發下一次鎖定。
    expect(call(FASTAPI, "POST", "/api/auth/login", {"email": user["email"], "password": WRONG}),
           401, "E0101")
    with owner_connection() as connection:
        connection.execute(
            "UPDATE login_failures SET occurred_at = clock_timestamp() - (%s * interval '1 minute') "
            "WHERE account_id = %s AND failure_epoch = %s",
            (settings["window_minutes"] + 1, user["id"], after["failure_epoch"]),
        )
    for index in range(settings["failure_threshold"] - 1):
        base = LARAVEL if index % 2 == 0 else FASTAPI
        expect(call(base, "POST", "/api/auth/login", {"email": user["email"], "password": WRONG}),
               401, "E0101")
    with owner_connection() as connection:
        window = connection.execute(
            "SELECT count(*) AS n FROM login_failures WHERE account_id = %s AND failure_epoch = %s "
            "AND occurred_at > clock_timestamp() - (%s * interval '1 minute')",
            (user["id"], after["failure_epoch"], settings["window_minutes"]),
        ).fetchone()["n"]
    must(window == settings["failure_threshold"] - 1, "滑動視窗未排除到期事件")
    login(LARAVEL, user)
    with owner_connection() as connection:
        reset = connection.execute("SELECT failure_epoch,locked_until FROM accounts WHERE id = %s", (user["id"],)).fetchone()
        count = connection.execute("SELECT count(*) AS n FROM login_failures WHERE account_id = %s", (user["id"],)).fetchone()["n"]
    must(reset["failure_epoch"] == after["failure_epoch"] + 1 and reset["locked_until"] is None,
         "普通成功未重設世代")
    # 共用設定只在受控獨立專案內短暫關閉，finally 恢復原值；只檢查本次帳號事件。
    try:
        with owner_connection() as connection:
            connection.execute("UPDATE login_lockout_settings SET enabled = false WHERE id = 1")
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": user["email"], "password": WRONG}),
               401, "E0101")
        with owner_connection() as connection:
            unchanged = connection.execute("SELECT failure_epoch,locked_until FROM accounts WHERE id = %s",
                                           (user["id"],)).fetchone()
            count_after = connection.execute("SELECT count(*) AS n FROM login_failures WHERE account_id = %s",
                                             (user["id"],)).fetchone()["n"]
        must(unchanged == reset and count_after == count, "關閉鎖定仍計數或改狀態")
    finally:
        with owner_connection() as connection:
            connection.execute("UPDATE login_lockout_settings SET enabled = %s WHERE id = 1", (settings["enabled"],))
    print(f"PASS lockout cross-backend threshold={settings['failure_threshold']} retry/CORS invalid/window/expiry/off/epoch")


# 持有兩端登入第一個共用設定列鎖，確認都等待，再檢查序列化事件數。
def concurrent_failures(namespace):
    with owner_connection() as connection:
        settings = connection.execute("SELECT enabled,failure_threshold FROM login_lockout_settings WHERE id=1").fetchone()
    must(settings["enabled"] and settings["failure_threshold"] >= 2, "鎖定設定不適合並行驗證")
    user = member(FASTAPI, namespace, "parallel")
    body = {"email": user["email"], "password": WRONG}
    with ThreadPoolExecutor(max_workers=2) as pool:
        with owner_connection() as hold:
            hold.execute("SELECT id FROM login_lockout_settings WHERE id = 1 FOR UPDATE")
            futures = [pool.submit(call, base, "POST", "/api/auth/login", body)
                       for base in (FASTAPI, LARAVEL)]
            deadline = time.monotonic() + 8
            with owner_connection() as observer:
                observer.autocommit = True
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = 'articles' "
                        "AND usename = 'articles_app' AND wait_event_type = 'Lock' "
                        "AND query LIKE '%login_lockout_settings%'"
                    ).fetchone()["n"]
                    if waiting >= 2:
                        break
                    time.sleep(0.05)
                else:
                    raise CheckFailure("Ticket 22 驗證失敗：兩後端未共同等待設定列鎖")
            hold.commit()
        results = [future.result(timeout=25) for future in futures]
    # 門檻當次必須 429；較高門檻則兩次都尚未鎖定，兩事件仍須提交。
    expected_statuses = [401, 429] if settings["failure_threshold"] == 2 else [401, 401]
    must(sorted(result[0] for result in results) == expected_statuses, "並行失敗鎖定狀態不符")
    for result in results:
        expect(result, result[0], "E0105" if result[0] == 429 else "E0101")
    with owner_connection() as connection:
        count = connection.execute("SELECT count(*) AS n FROM login_failures WHERE account_id = %s", (user["id"],)).fetchone()["n"]
    must(count == 2, "並行失敗事件遺失")
    print("PASS parallel FastAPI/Laravel settings row lock and two committed failures")


# 真實身份生命週期與跨後端 Argon2id，撤銷只影響當次 token。
def identity(namespace):
    fast = member(FASTAPI, namespace, "fast")
    laravel = member(LARAVEL, namespace, "laravel")
    fast_token = login(FASTAPI, fast)
    second = login(FASTAPI, fast)
    laravel_token = login(LARAVEL, fast)
    laravel_user_token = login(FASTAPI, laravel)
    must(expect(call(FASTAPI, "GET", "/api/me", token=fast_token), 200)["id"] == fast["id"],
         "me 帳號錯誤")
    expect(call(FASTAPI, "GET", "/api/me", token=laravel_token), 401, "E0002")
    expect(call(LARAVEL, "GET", "/api/me", token=fast_token), 401, "E0002")
    must(expect(call(FASTAPI, "GET", "/api/me", token=laravel_user_token), 200)["id"] == laravel["id"],
         "Laravel hash 無法登入 FastAPI")
    expect(call(FASTAPI, "POST", "/api/auth/logout", token=fast_token), 200)
    expect(call(FASTAPI, "GET", "/api/me", token=fast_token), 401, "E0002")
    expect(call(FASTAPI, "GET", "/api/me", token=second), 200)
    with owner_connection() as connection:
        sessions = connection.execute(
            "SELECT backend_id, issued_at, expires_at, revoked_at FROM login_sessions WHERE account_id = %s",
            (fast["id"],)
        ).fetchall()
    must(len(sessions) == 3 and sum(s["revoked_at"] is not None for s in sessions) == 1,
         "登出撤銷範圍不符")
    must(all(s["expires_at"] - s["issued_at"] == timedelta(hours=8) for s in sessions),
         "登入紀錄不是固定八小時")
    # 模擬既有停用管理交易：停用與所有本帳號舊登入撤銷必須一起提交。
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET status = 'disabled' WHERE id = %s", (fast["id"],))
        connection.execute(
            "UPDATE login_sessions SET revoked_at = clock_timestamp() "
            "WHERE account_id = %s AND revoked_at IS NULL", (fast["id"],)
        )
    expect(call(FASTAPI, "GET", "/api/me", token=second), 403, "E0102")
    expect(call(FASTAPI, "POST", "/api/auth/login", {"email": fast["email"], "password": PASSWORD}),
           403, "E0102")
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET status = 'active' WHERE id = %s", (fast["id"],))
    expect(call(FASTAPI, "GET", "/api/me", token=second), 401, "E0002")
    fresh = login(FASTAPI, fast)
    print("PASS Argon2id both directions, distinct JWT, me/logout/revoke/disable/restore old-token rejection")
    return fast, laravel, fresh


# public_article_ids 依共同雙鍵排序讀同一公開頁，避免把 ID 當成建立時間。
def public_article_ids():
    with owner_connection() as connection:
        return [row["id"] for row in connection.execute(
            "SELECT id FROM articles WHERE visibility='visible' ORDER BY created_at DESC,id DESC LIMIT 100 OFFSET 0"
        ).fetchall()]


# 直接用合成資料檢查公開/管理範圍、隱藏父子、跨後端完整資源及超頁。
def reading(namespace, author, outsider, author_token):
    with owner_connection() as connection:
        baseline_public = connection.execute("SELECT count(*) AS n FROM articles WHERE visibility = 'visible'").fetchone()["n"]
        baseline_all = connection.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
    public = content(author, None, "t22-public-" + namespace)
    hidden = content(author, None, "t22-hidden-" + namespace, "hidden")
    visible_comment = content(outsider, public, "t22-visible-" + namespace)
    hidden_comment = content(outsider, public, "t22-hidden-" + namespace, "hidden")
    parent_comment = content(outsider, hidden, "t22-parent-" + namespace)
    admin = member(FASTAPI, namespace, "admin")
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET role = 'admin' WHERE id = %s", (admin["id"],))
    admin_token = login(FASTAPI, admin)
    outsider_token = login(FASTAPI, outsider)
    page = "?page=1&per_page=100"
    for base in (FASTAPI, LARAVEL):
        anon = expect(call(base, "GET", "/api/articles" + page), 200)
        ids = [item["id"] for item in anon["list"]]
        must(public in ids and hidden not in ids and anon["pagination"]["total"] == baseline_public + 1,
             "公開列表可見性或總數不符")
        # ID 與建立時間不必同序；用同一頁的 DB 雙鍵排序校對完整結果。
        must(ids == public_article_ids(), "公開列表排序不符")
        far = expect(call(base, "GET", "/api/articles?page=999999&per_page=20"), 200)
        must(far["list"] == [] and far["pagination"]["total"] == baseline_public + 1 and
             far["pagination"]["page"] == 999999, "文章超頁不符")
        must(expect(call(base, "GET", f"/api/articles/{public}"), 200) ==
             resource("articles", row("articles", public), author["nickname"]), "跨後端文章不一致")
        expect(call(base, "GET", f"/api/articles/{hidden}"), 404, "E0004")
        comments = expect(call(base, "GET", f"/api/articles/{public}/comments" + page), 200)
        must([item["id"] for item in comments["list"]] == [visible_comment] and
             comments["pagination"]["total"] == 1 and comments["list"][0] ==
             resource("comments", row("comments", visible_comment), outsider["nickname"]),
             "公開留言或跨後端資料不一致")
        far = expect(call(base, "GET", f"/api/articles/{public}/comments?page=999999&per_page=20"), 200)
        must(far["list"] == [] and far["pagination"]["total"] == 1, "留言超頁不符")
        expect(call(base, "GET", f"/api/articles/{hidden}/comments" + page), 404, "E0004")
    expect(call(FASTAPI, "GET", "/api/articles" + page + "&filters%5Bscope%5D=management"),
           401, "E0002")
    expect(call(FASTAPI, "GET", "/api/articles" + page + "&filters%5Bscope%5D=management",
                token=outsider_token), 403, "E0003")
    managed = expect(call(FASTAPI, "GET", "/api/articles" + page + "&filters%5Bscope%5D=management",
                          token=admin_token), 200)
    must({public, hidden}.issubset({item["id"] for item in managed["list"]}) and
         managed["pagination"]["total"] == baseline_all + 2, "管理範圍列表不符")
    hidden_detail = expect(call(FASTAPI, "GET", f"/api/articles/{hidden}", token=author_token), 200)
    must(hidden_detail == resource("articles", row("articles", hidden), author["nickname"], True),
         "作者隱藏文章詳情或恢復資格不符")
    expect(call(FASTAPI, "GET", f"/api/articles/{hidden}", token=outsider_token), 404, "E0004")
    own_parent = expect(call(FASTAPI, "GET", f"/api/articles/{hidden}/comments" + page,
                             token=author_token), 200)
    must([item["id"] for item in own_parent["list"]] == [parent_comment], "作者預覽父文章留言不符")
    admin_comments = expect(call(FASTAPI, "GET", f"/api/articles/{public}/comments" + page,
                                 token=admin_token), 200)
    must({item["id"] for item in admin_comments["list"]} == {visible_comment, hidden_comment} and
         admin_comments["pagination"]["total"] == 2, "管理員隱藏留言範圍不符")
    print("PASS public/detail/comments hidden parent-child, management, pagination, Laravel parity")
    return public


# 原始輸入測試 Content-Type、Body、Query、URL 與無效 JWT 優先順序。
def validation(public, token):
    # 預檢與鎖定回應都須允許同源 Vue 讀取 Retry-After，不需要認證。
    request = Request(FASTAPI + "/api/auth/login", headers={
        "Origin": "http://localhost:8080", "Access-Control-Request-Method": "POST",
    }, method="OPTIONS")
    with OPENER.open(request, timeout=5) as response:
        must(response.status == 204 and response.headers.get("Access-Control-Allow-Origin") ==
             "http://localhost:8080" and response.headers.get("Access-Control-Expose-Headers") ==
             "Retry-After" and "POST" in response.headers.get("Access-Control-Allow-Methods", ""),
             "CORS 預檢標頭不符")
    expect(call(FASTAPI, "POST", "/api/auth/login", b"{}", content_type="text/plain"),
           422, "E0001", {"_body": ["Request Body 必須為 JSON object"]})
    expect(call(FASTAPI, "POST", "/api/auth/login", b"{", content_type="application/json"),
           400, "E0005")
    expect(call(FASTAPI, "GET", "/api/me", body=b"{}", token=token),
           422, "E0001", {"_body": ["此 API 不接受 Request Body"]})
    expect(call(FASTAPI, "GET", "/api/articles?page=1&page=2&per_page=20"),
           422, "E0001", {"page": ["此參數不可重複"]})
    expect(call(FASTAPI, "GET", "/api/articles?page=1&per_page=20&x=1"),
           422, "E0001", {"x": ["不支援此欄位"]})
    expect(call(FASTAPI, "GET", "/api/articles/bad"),
           422, "E0001", {"article_id": ["欄位格式或範圍不正確"]})
    expect(call(FASTAPI, "GET", f"/api/articles/{public}?x=1"),
           422, "E0001", {"x": ["不支援此欄位"]})
    expect(call(FASTAPI, "GET", "/api/articles?page=1&per_page=20", token="invalid"),
           401, "E0002")
    expect(call(FASTAPI, "GET", f"/api/articles/{public}/comments?page=1&per_page=20", token="invalid"),
           401, "E0002")
    print("PASS CORS preflight, raw Content-Type/Body/Query/URL/invalid-JWT and exact errors")


# 僅記真實計畫節點、索引名稱及小 fixture 時間，供人工判讀，不設假性能門檻。
def query_plans(article_id):
    article_columns = ("articles.id, articles.author_id, articles.title, articles.body, "
                       "articles.visibility, articles.restore_control, articles.version, "
                       "articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname")
    comment_columns = ("comments.id, comments.author_id, comments.article_id, comments.body, "
                       "comments.visibility, comments.restore_control, comments.version, "
                       "comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname")
    cases = (
        ("public_count", "SELECT count(*) FROM articles JOIN accounts ON accounts.id = articles.author_id "
         "WHERE articles.visibility = 'visible'", ()),
        ("public_page", "SELECT " + article_columns + " FROM articles JOIN accounts "
         "ON accounts.id = articles.author_id WHERE articles.visibility = 'visible' "
         "ORDER BY articles.created_at DESC,articles.id DESC LIMIT %s OFFSET %s", (20, 0)),
        ("comments_count", "SELECT count(*) FROM comments JOIN accounts ON accounts.id = comments.author_id "
         "WHERE comments.article_id = %s AND comments.visibility = 'visible'", (article_id,)),
        ("comments_page", "SELECT " + comment_columns + " FROM comments JOIN accounts "
         "ON accounts.id = comments.author_id WHERE comments.article_id = %s AND comments.visibility = 'visible' "
         "ORDER BY comments.created_at DESC,comments.id DESC LIMIT %s OFFSET %s", (article_id, 20, 0)),
    )
    with owner_connection() as connection:
        indexes = connection.execute(
            "SELECT tablename,indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename IN ('articles','comments') ORDER BY tablename,indexname"
        ).fetchall()
        must(any(item["indexname"] == "articles_public_order_idx" for item in indexes) and
             any(item["indexname"] == "comments_public_article_order_idx" for item in indexes),
             "公開查詢索引缺失")
        print("INDEX " + json.dumps(indexes, ensure_ascii=False))
        for label, sql, params in cases:
            plan = connection.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()["QUERY PLAN"]
            if isinstance(plan, str):
                plan = json.loads(plan)
            root = plan[0]
            nodes = []
            # 只保留可解釋執行路徑的節點與索引名稱，不輸出 SQL 參數。
            def visit(node):
                nodes.append({"type": node["Node Type"], "index": node.get("Index Name")})
                for child in node.get("Plans", []):
                    visit(child)
            visit(root["Plan"])
            print("PLAN " + json.dumps({"case": label, "nodes": nodes,
                                         "execution_ms": root["Execution Time"]}))


# 守衛先跑，且測試兩次各自用唯一 namespace，不清空任何既有資料。
def run():
    guard()
    if len(sys.argv) == 2 and sys.argv[1] == "--guard-only":
        print("PASS ticket20_test read-only guard")
        return
    must(len(sys.argv) == 1, "不支援此參數")
    namespace = secrets.token_hex(7)
    print("FIXTURE t22-" + namespace)
    author, outsider, token = identity(namespace)
    lockout(namespace)
    concurrent_failures(namespace)
    article_id = reading(namespace, author, outsider, token)
    validation(article_id, token)
    query_plans(article_id)
    print("PASS Ticket22 HTTP/SQL integration")


if __name__ == "__main__":
    try:
        run()
    except CheckFailure as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("Ticket 22 驗證失敗：未預期 " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
