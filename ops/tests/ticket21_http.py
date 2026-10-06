"""Ticket 21 隔離真實 HTTP、共用資料與搜尋計畫驗證。"""

import json
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from ticket20_http import (
    FLASK, LARAVEL, OPENER, PASSWORD, CheckFailure, call, created, expect, guard,
    must, owner_connection, resource, snapshot, wait_for_lock,
)

SETTINGS = "/api/settings/login-lockout"
MEMBERS = "/api/members?page=1&per_page=20"
CONFIG = {"enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 300}
FIELDS = {"id", "email", "nickname", "role", "status", "created_at"}


# 以真實註冊與登入建立獨特測資；只有隔離資料庫中的合成帳號可再由 owner 調整角色。
def account(base, namespace, label, nickname):
    email = f"t21-{namespace}-{label}@example.test"
    expect(call(base, "POST", "/api/auth/register", {
        "email": email, "password": PASSWORD, "nickname": nickname,
    }), 201, empty=True)
    data = expect(call(base, "POST", "/api/auth/login", {
        "email": email, "password": PASSWORD,
    }), 200)
    must(data["user"]["role"] == "member" and isinstance(data["access_token"], str),
         "合成帳號登入格式不符")
    return {"id": data["user"]["id"], "email": email,
            "nickname": nickname, "token": data["access_token"]}


# 讀取受控帳號的持久狀態，供同值、關閉世代及跨後端撤銷斷言使用。
def account_state(account_id):
    with owner_connection() as connection:
        row = connection.execute(
            "SELECT id, status, failure_epoch, locked_until FROM accounts WHERE id = %s", (account_id,)
        ).fetchone()
    must(row is not None, "合成帳號遺失")
    return row


# 精確比對 Ticket 20 helper 尚未覆蓋的登入錯誤，避免修改共用測試程式。
def auth_error(result, status, code):
    actual, payload = result
    messages = {"E0101": "帳號或密碼錯誤", "E0105": "登入失敗次數過多，請稍後再試"}
    must(actual == status and set(payload) == {"success", "message", "error_code", "log_id"},
         "登入錯誤狀態或欄位不符")
    must(payload["success"] is False and payload["error_code"] == code
         and payload["message"] == messages[code] and isinstance(payload["log_id"], str)
         and bool(payload["log_id"]), "登入錯誤文案或代碼不符")


# 設定只接受完整四欄；成功後重讀，避免漏掉部分提交。
def put_settings(admin, values):
    must(set(values) == set(CONFIG), "設定 fixture 不是完整四欄")
    expect(call(FLASK, "PUT", SETTINGS, values, admin["token"]), 200, empty=True)
    must(expect(call(FLASK, "GET", SETTINGS, token=admin["token"]), 200) == values,
         "設定未完整持久化")


# HEAD 不保證有 JSON 回應本文；仍以固定 Flask 目標與無 redirect opener 檢查狀態。
def head_settings_status(admin, body=None):
    raw = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": "Bearer " + admin["token"]}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    request = Request(FLASK + SETTINGS, data=raw, headers=headers, method="HEAD")
    try:
        with OPENER.open(request, timeout=20) as response:
            return response.status
    except HTTPError as response:
        response.close()
        return response.code
    except (URLError, TimeoutError):
        raise CheckFailure("Ticket 21 驗證失敗：HEAD 結果未知，停止重送") from None


# 驗證欄位限制、角色先後與管理員目標隱藏，不讓非法輸入影響 fixture。
def management_validation(admin, member):
    expect(call(FLASK, "GET", MEMBERS), 401, "E0002")
    expect(call(FLASK, "GET", "/api/members/bad?x=1", token=member["token"]), 403, "E0003")
    expect(call(FLASK, "PUT", "/api/members/bad/status?x=1", {"status": 1}, member["token"]),
           403, "E0003")
    expect(call(FLASK, "GET", "/api/comments?page=bad&per_page=20", token=member["token"]),
           403, "E0003")
    expect(call(FLASK, "GET", SETTINGS + "?x=1", token=member["token"]), 403, "E0003")
    expect(call(FLASK, "PUT", SETTINGS, {}, member["token"]), 403, "E0003")
    cases = (
        (MEMBERS + "&page=2", {"page": ["此參數不可重複"]}),
        (MEMBERS + "&x=1", {"x": ["不支援此欄位"]}),
        (MEMBERS + "&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y",
         {"filters.keyword": ["此參數不可重複"]}),
        (MEMBERS + "&filters%5Bkeyword%5D=" + "x" * 255,
         {"filters.keyword": ["欄位格式或範圍不正確"]}),
    )
    for path, errors in cases:
        expect(call(FLASK, "GET", path, token=admin["token"]), 422, "E0001", errors)
    expect(call(FLASK, "GET", MEMBERS, {}, admin["token"]), 422, "E0001",
           {"_body": ["此 API 不接受 Request Body"]})
    expect(call(FLASK, "GET", "/api/members/bad", token=admin["token"]), 422, "E0001",
           {"member_id": ["欄位格式或範圍不正確"]})
    for path in (f"/api/members/{admin['id']}", "/api/members/9007199254740991"):
        expect(call(FLASK, "GET", path, token=admin["token"]), 404, "E0004")
    expect(call(FLASK, "PUT", f"/api/members/{admin['id']}/status",
                {"status": "disabled"}, admin["token"]), 404, "E0004")
    status_path = f"/api/members/{member['id']}/status"
    for body, errors in (({}, {"status": ["此欄位為必填"]}),
                         ({"status": None}, {"status": ["此欄位不可為 null"]}),
                         ({"status": 1}, {"status": ["欄位型態不正確"]}),
                         ({"status": "paused"}, {"status": ["欄位格式或範圍不正確"]}),
                         ({"status": "active", "extra": 1}, {"extra": ["不支援此欄位"]})):
        expect(call(FLASK, "PUT", status_path, body, admin["token"]), 422, "E0001", errors)
    expect(call(FLASK, "GET", SETTINGS + "?x=1", token=admin["token"]), 422, "E0001",
           {"x": ["不支援此欄位"]})
    expect(call(FLASK, "GET", SETTINGS, {}, admin["token"]), 422, "E0001",
           {"_body": ["此 API 不接受 Request Body"]})
    for field in CONFIG:
        missing = CONFIG.copy()
        del missing[field]
        expect(call(FLASK, "PUT", SETTINGS, missing, admin["token"]), 422, "E0001",
               {field: ["此欄位為必填"]})
        expect(call(FLASK, "PUT", SETTINGS, CONFIG | {field: None}, admin["token"]),
               422, "E0001", {field: ["此欄位不可為 null"]})
    expect(call(FLASK, "PUT", SETTINGS, CONFIG | {"enabled": 1}, admin["token"]),
           422, "E0001", {"enabled": ["欄位型態不正確"]})
    expect(call(FLASK, "PUT", SETTINGS, CONFIG | {"lock_seconds": 86401}, admin["token"]),
           422, "E0001", {"lock_seconds": ["欄位格式或範圍不正確"]})


# 以 DB 的獨立碼點比較計算期望值，既有資料或前次執行不會污染精確 ID 與總數。
def expected_members(keyword, page, per_page):
    translation = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    folded = keyword.strip(" ").translate(translation)
    with owner_connection() as connection:
        rows = connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts WHERE role = 'member' "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    matches = [row for row in rows if not folded or any(
        folded in row[field].translate(translation) for field in ("email", "nickname")
    )]
    start = (page - 1) * per_page
    return matches[start:start + per_page], len(matches)


# 唯一 namespace 覆蓋符號、Unicode；短字與空搜尋依當次 DB 快照檢查全部會員。
def search_cases(admin, namespace, fixtures):
    marker = "T21" + namespace
    keywords = (marker, marker + "A", marker + "A_", marker + "A_B",
                marker + "A_B%", marker + "A_B%\\", marker + "É", marker + "é",
                marker + "中", marker + "Ｅ", "A", "AB", "", "無結果-" + namespace)
    for keyword in keywords:
        path = MEMBERS + "&filters%5Bkeyword%5D=" + quote(keyword, safe="")
        data = expect(call(FLASK, "GET", path, token=admin["token"]), 200)
        rows, total = expected_members(keyword, 1, 20)
        must([item["id"] for item in data["list"]] == [row["id"] for row in rows],
             "搜尋 ID、排序或字面語意不符")
        must(data["pagination"] == {"page": 1, "per_page": 20, "total": total,
                                    "total_pages": (total + 19) // 20}, "搜尋精確計數不符")
        for item in data["list"]:
            must(set(item) == FIELDS and item["role"] == "member", "會員列表欄位或角色外洩")
    for key, expected_id in ((marker + "A_B%\\", fixtures[0]["id"]),
                             (marker + "é", fixtures[1]["id"]),
                             (marker + "A_B%\\É", fixtures[0]["id"])):
        data = expect(call(FLASK, "GET", MEMBERS + "&filters%5Bkeyword%5D="
                           + quote(key, safe=""), token=admin["token"]), 200)
        must([item["id"] for item in data["list"]] == [expected_id]
             and data["pagination"]["total"] == 1, "受控 Unicode／符號 fixture 不唯一")
    for item in fixtures:
        detail = expect(call(FLASK, "GET", f"/api/members/{item['id']}", token=admin["token"]), 200)
        must(set(detail) == FIELDS and detail["id"] == item["id"]
             and detail["email"] == item["email"] and detail["nickname"] == item["nickname"],
             "會員詳情欄位或內容不符")
    for page, per_page in ((3, 1), (9007199254740991, 100)):
        data = expect(call(FLASK, "GET", f"/api/members?page={page}&per_page={per_page}",
                           token=admin["token"]), 200)
        rows, total = expected_members("", page, per_page)
        must([item["id"] for item in data["list"]] == [row["id"] for row in rows]
             and data["pagination"] == {"page": page, "per_page": per_page, "total": total,
                                        "total_pages": (total + per_page - 1) // per_page},
             "會員深頁或全域精確計數不符")


# 文章與留言由兩個真實後端建立；管理列表須含管理員自己的與父文章下架的留言。
def content_cases(namespace, admin, flask_member, laravel_member):
    title = "T21-parent-" + namespace
    expect(call(LARAVEL, "POST", "/api/articles", {"title": title, "body": "父文章"},
                laravel_member["token"]), 201, empty=True)
    article = created("articles", laravel_member["id"], "title", title)
    own_body = "T21-own-" + namespace
    other_body = "T21-other-" + namespace
    expect(call(FLASK, "POST", f"/api/articles/{article['id']}/comments", {"body": own_body},
                admin["token"]), 201, empty=True)
    own = created("comments", admin["id"], "body", own_body)
    expect(call(LARAVEL, "POST", f"/api/articles/{article['id']}/comments", {"body": other_body},
                laravel_member["token"]), 201, empty=True)
    other = created("comments", laravel_member["id"], "body", other_body)
    expect(call(LARAVEL, "PUT", f"/api/articles/{article['id']}/visibility",
                {"visibility": "hidden", "version": article["version"]},
                laravel_member["token"]), 200, empty=True)
    expect(call(LARAVEL, "PUT", f"/api/comments/{other['id']}/visibility",
                {"visibility": "hidden", "version": other["version"]},
                laravel_member["token"]), 200, empty=True)
    # 依已持久化全域排序計算 fixture 頁碼，不能假定資料庫只有本次新增內容。
    for path, table, ids in (("/api/comments", "comments", {own["id"], other["id"]}),
                             ("/api/articles", "articles", {article["id"]})):
        suffix = "&filters%5Bscope%5D=management" if table == "articles" else ""
        with owner_connection() as connection:
            rows = connection.execute(
                f"SELECT id FROM {table} ORDER BY created_at DESC, id DESC"
            ).fetchall()
        all_ids = [row["id"] for row in rows]
        for target in ids:
            page = all_ids.index(target) // 100 + 1
            data = expect(call(FLASK, "GET", f"{path}?page={page}&per_page=100{suffix}",
                               token=admin["token"]), 200)
            must(data["pagination"]["total"] == len(all_ids)
                 and [row["id"] for row in data["list"]] == all_ids[(page - 1) * 100:page * 100],
                 "管理內容列表總數或排序不符")
        if table == "articles":
            expect(call(FLASK, "GET", f"{path}?page=1&per_page=20{suffix}",
                        token=flask_member["token"]), 403, "E0003")
        else:
            for target, nickname, can_restore in ((own, admin["nickname"], False),
                                                   (snapshot("comments", other["id"]),
                                                    laravel_member["nickname"], True)):
                page = all_ids.index(target["id"]) // 100 + 1
                data = expect(call(FLASK, "GET", f"{path}?page={page}&per_page=100",
                                   token=admin["token"]), 200)
                item = next(row for row in data["list"] if row["id"] == target["id"])
                must(item == resource("comments", snapshot("comments", target["id"]), nickname,
                                      can_restore), "管理留言欄位、父下架或自身留言範圍不符")
    return article, own, other


# 停用同交易撤銷 Flask 與 Laravel 舊 session；恢復後舊憑證不能復活。
def status_cases(admin, member, article, comment):
    flask_token = expect(call(FLASK, "POST", "/api/auth/login", {
        "email": member["email"], "password": PASSWORD,
    }), 200)["access_token"]
    laravel_token = expect(call(LARAVEL, "POST", "/api/auth/login", {
        "email": member["email"], "password": PASSWORD,
    }), 200)["access_token"]
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = clock_timestamp() + interval '1 hour' "
                           "WHERE id = %s", (member["id"],))
    old_lock = account_state(member["id"])["locked_until"]
    content_before = (snapshot("articles", article["id"]), snapshot("comments", comment["id"]))
    path = f"/api/members/{member['id']}/status"
    with owner_connection() as connection:
        before_revoked = connection.execute(
            "SELECT count(*) AS n FROM login_sessions WHERE account_id = %s AND revoked_at IS NOT NULL",
            (member["id"],)
        ).fetchone()["n"]
    expect(call(FLASK, "PUT", path, {"status": "active"}, admin["token"]), 200, empty=True)
    with owner_connection() as connection:
        after_same = connection.execute(
            "SELECT count(*) AS n FROM login_sessions WHERE account_id = %s AND revoked_at IS NOT NULL",
            (member["id"],)
        ).fetchone()["n"]
    must(before_revoked == after_same, "同值 active 撤銷了登入")
    expect(call(FLASK, "PUT", path, {"status": "disabled"}, admin["token"]), 200, empty=True)
    must(account_state(member["id"])["status"] == "disabled", "停用未提交")
    with owner_connection() as connection:
        active = connection.execute(
            "SELECT count(*) AS n FROM login_sessions WHERE account_id = %s AND revoked_at IS NULL",
            (member["id"],)
        ).fetchone()["n"]
    must(active == 0 and account_state(member["id"])["locked_until"] == old_lock,
         "停用未原子撤銷或改了既有鎖定")
    expect(call(FLASK, "GET", "/api/me", token=flask_token), 403, "E0102")
    expect(call(LARAVEL, "GET", "/api/me", token=laravel_token), 403, "E0102")
    expect(call(FLASK, "PUT", path, {"status": "disabled"}, admin["token"]), 200, empty=True)
    expect(call(FLASK, "PUT", path, {"status": "active"}, admin["token"]), 200, empty=True)
    expect(call(FLASK, "GET", "/api/me", token=flask_token), 401, "E0002")
    expect(call(LARAVEL, "GET", "/api/me", token=laravel_token), 401, "E0002")
    must(account_state(member["id"])["locked_until"] == old_lock
         and (snapshot("articles", article["id"]), snapshot("comments", comment["id"])) == content_before,
         "恢復復活鎖定以外的舊身分或改動內容")


# 驗證四欄完整更新、同值、數值不延長舊鎖及真正關閉才清世代和暫時鎖。
def settings_cases(admin, member):
    original = expect(call(FLASK, "GET", SETTINGS, token=admin["token"]), 200)
    must(set(original) == set(CONFIG), "既有設定欄位不符")
    put_settings(admin, CONFIG)
    before = account_state(member["id"])
    put_settings(admin, CONFIG.copy())
    must(account_state(member["id"])["failure_epoch"] == before["failure_epoch"],
         "同值設定重做計數副作用")
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = clock_timestamp() + interval '30 minutes' "
                           "WHERE id = %s", (member["id"],))
    old_lock = account_state(member["id"])["locked_until"]
    numeric = {"enabled": True, "window_minutes": 7, "failure_threshold": 3, "lock_seconds": 600}
    put_settings(admin, numeric)
    must(account_state(member["id"])["locked_until"] == old_lock,
         "數值設定延長了已鎖到期時間")
    before = account_state(member["id"])
    with owner_connection() as connection:
        failures = connection.execute(
            "SELECT count(*) AS n FROM login_failures WHERE account_id = %s", (member["id"],)
        ).fetchone()["n"]
    off = numeric | {"enabled": False}
    put_settings(admin, off)
    after = account_state(member["id"])
    must(after["locked_until"] is None and after["failure_epoch"] == before["failure_epoch"] + 1,
         "真正關閉未清鎖或切換世代")
    with owner_connection() as connection:
        retained = connection.execute(
            "SELECT count(*) AS n FROM login_failures WHERE account_id = %s", (member["id"],)
        ).fetchone()["n"]
    must(retained == failures, "關閉設定刪除了歷史失敗事件")
    put_settings(admin, off)
    must(account_state(member["id"])["failure_epoch"] == after["failure_epoch"],
         "同值關閉重設了世代")
    put_settings(admin, original)
    return original


# 有 Body 的 HEAD 依 GET 規則拒絕且不可觸發設定關閉；空 HEAD 保持唯讀。
def settings_head_cases(admin, member):
    put_settings(admin, CONFIG)
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = clock_timestamp() + interval '30 minutes' "
                           "WHERE id = %s", (member["id"],))
    before_settings = expect(call(FLASK, "GET", SETTINGS, token=admin["token"]), 200)
    before_account = account_state(member["id"])
    must(head_settings_status(admin, CONFIG | {"enabled": False}) == 422,
         "完整關閉 payload 的 HEAD 未依 GET 拒絕")
    must(head_settings_status(admin) == 200, "空 HEAD 未維持唯讀成功狀態")
    must(expect(call(FLASK, "GET", SETTINGS, token=admin["token"]), 200) == before_settings
         and account_state(member["id"]) == before_account,
         "HEAD 改寫設定或受控會員鎖定／世代")


# 兩個完整設定同時寫入，只允許其中一份完整值成為最後提交。
def concurrent_settings(admin):
    left = {"enabled": True, "window_minutes": 7, "failure_threshold": 11, "lock_seconds": 333}
    right = {"enabled": True, "window_minutes": 9, "failure_threshold": 13, "lock_seconds": 555}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, FLASK, "PUT", SETTINGS, values, admin["token"])
                   for values in (left, right)]
        results = [future.result(timeout=25) for future in futures]
    for result in results:
        expect(result, 200, empty=True)
    final = expect(call(FLASK, "GET", SETTINGS, token=admin["token"]), 200)
    must(final in (left, right), "並行設定混合了不同完整提交")


# DB 左邊界前後的舊事件與兩後端新失敗共用一個世代；到期後須換世代。
def lockout_cases(admin, member):
    values = {"enabled": True, "window_minutes": 5, "failure_threshold": 3, "lock_seconds": 60}
    put_settings(admin, values)
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                           "WHERE id = %s", (member["id"],))
        outside = connection.execute(
            "INSERT INTO login_failures(account_id, failure_epoch, occurred_at) "
            "SELECT id, failure_epoch, clock_timestamp() - interval '5 minutes 2 seconds' "
            "FROM accounts WHERE id = %s RETURNING occurred_at", (member["id"],)
        ).fetchone()["occurred_at"]
        inside = connection.execute(
            "INSERT INTO login_failures(account_id, failure_epoch, occurred_at) "
            "SELECT id, failure_epoch, clock_timestamp() - interval '4 minutes 58 seconds' "
            "FROM accounts WHERE id = %s RETURNING occurred_at", (member["id"],)
        ).fetchone()["occurred_at"]
    wrong = {"email": member["email"], "password": "Wrong123"}
    auth_error(call(LARAVEL, "POST", "/api/auth/login", wrong), 401, "E0101")
    epoch = account_state(member["id"])["failure_epoch"]
    with owner_connection() as connection:
        first_time = connection.execute(
            "SELECT max(occurred_at) AS t FROM login_failures WHERE account_id = %s "
            "AND failure_epoch = %s", (member["id"], epoch)
        ).fetchone()["t"]
    must(outside <= first_time - timedelta(minutes=5) < inside,
         "DB 實際登入時間未夾住 t-N 左邊界")
    auth_error(call(FLASK, "POST", "/api/auth/login", wrong), 429, "E0105")
    locked = account_state(member["id"])
    must(locked["locked_until"] is not None, "跨後端累計未鎖定")
    auth_error(call(LARAVEL, "POST", "/api/auth/login", {
        "email": member["email"], "password": PASSWORD,
    }), 429, "E0105")
    must(account_state(member["id"])["locked_until"] == locked["locked_until"],
         "未到期鎖定被延長")
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = clock_timestamp() - interval '1 second' "
                           "WHERE id = %s", (member["id"],))
    data = expect(call(LARAVEL, "POST", "/api/auth/login", {
        "email": member["email"], "password": PASSWORD,
    }), 200)
    must(data["user"]["id"] == member["id"], "到期後跨後端登入未成功")
    after = account_state(member["id"])
    must(after["locked_until"] is None and after["failure_epoch"] > locked["failure_epoch"],
         "到期未清鎖與換世代")
    auth_error(call(FLASK, "POST", "/api/auth/login", wrong), 401, "E0101")
    # 停用優先於錯誤計數與鎖定；恢復後必須另以新登入身分進入。
    expect(call(FLASK, "PUT", f"/api/members/{member['id']}/status",
                {"status": "disabled"}, admin["token"]), 200, empty=True)
    expect(call(LARAVEL, "POST", "/api/auth/login", wrong), 403, "E0102")
    expect(call(FLASK, "PUT", f"/api/members/{member['id']}/status",
                {"status": "active"}, admin["token"]), 200, empty=True)


# 先讓登入在設定列等待，再交錯完整設定寫入；兩種可序列化結果均須保留。
def settings_login_race(admin, member):
    first = {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 60}
    second = first | {"enabled": False}
    put_settings(admin, first)
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                           "WHERE id = %s", (member["id"],))
    with ThreadPoolExecutor(max_workers=2) as pool:
        with owner_connection() as hold:
            hold.execute("SELECT id FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
            login_future = pool.submit(call, LARAVEL, "POST", "/api/auth/login",
                                       {"email": member["email"], "password": "Wrong123"})
            wait_for_lock("login_lockout_settings")
            setting_future = pool.submit(call, FLASK, "PUT", SETTINGS, second, admin["token"])
            wait_for_lock("login_lockout_settings", 2)
            hold.commit()
        login_result = login_future.result(timeout=25)
        expect(setting_future.result(timeout=25), 200, empty=True)
    must(expect(call(FLASK, "GET", SETTINGS, token=admin["token"]), 200) == second,
         "交錯設定未完整提交")
    must(login_result[0] in (401, 429), "交錯登入沒有合法序列化結果")
    auth_error(login_result, login_result[0], "E0101" if login_result[0] == 401 else "E0105")
    must(account_state(member["id"])["locked_until"] is None,
         "關閉後仍保留交錯登入鎖定")


# 依 Flask 實際深頁分支記錄 count 與必要的 page 計畫，避免對未執行 SQL 宣稱耗時。
def search_plans(namespace):
    marker = "T21" + namespace
    cases = (("short", "A", 1, 20), ("two", "AB", 1, 20),
             ("symbol", marker + "A_B%\\", 1, 20), ("unicode", marker + "é", 1, 20),
             ("deep", "", 9007199254740991, 100))
    with owner_connection() as connection:
        counts = connection.execute(
            "SELECT role, count(*) AS n FROM accounts GROUP BY role ORDER BY role"
        ).fetchall()
        print(json.dumps({"kind": "member-search-data", "accounts_by_role": counts,
                          "source": "flask.members"}, ensure_ascii=False))
        for label, keyword, page, per_page in cases:
            folded = keyword.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
            where = "FROM accounts WHERE role = 'member'"
            params = ()
            if folded:
                where += (
                    " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                    " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
                )
                params = (folded, folded)
            count_query = "SELECT count(*) " + where
            total = connection.execute(count_query, params).fetchone()["count"]
            offset = (page - 1) * per_page
            page_runs = offset < total
            if label == "deep":
                must(not page_runs, "深頁計畫 fixture 意外落在資料範圍內")
            print(json.dumps({"kind": "member-search-case", "case": label,
                              "matching_rows": total, "page_sql_executed": page_runs,
                              "business_queries_per_request": 2 if page_runs else 1}, ensure_ascii=False))
            statements = [("count", count_query, params)]
            if page_runs:
                statements.append((
                    "page", "SELECT id, email, nickname, role, status, created_at " + where
                    + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (*params, per_page, offset),
                ))
            for kind, query, values in statements:
                plan = connection.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, values
                ).fetchone()["QUERY PLAN"]
                if isinstance(plan, str):
                    plan = json.loads(plan)
                print(json.dumps({"kind": "member-search-plan", "case": label, "query": kind,
                                  "plan": plan[0]}, ensure_ascii=False, default=str))


# 先執行固定專案／實際 DB 身分守衛，guard-only 在任何合成資料寫入前返回。
def run():
    guard()
    if len(sys.argv) == 2 and sys.argv[1] == "--guard-only":
        print("PASS ticket20_test read-only guard")
        return
    must(len(sys.argv) == 1, "不支援此參數")
    namespace = secrets.token_hex(4)
    marker = "T21" + namespace
    first = account(FLASK, namespace, "first", marker + "A_B%\\É中")
    second = account(LARAVEL, namespace, "second", marker + "é")
    third = account(FLASK, namespace, "third", marker + "A1B")
    admin = account(FLASK, namespace, "admin", "管理-" + marker)
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET role = 'admin' WHERE id = %s", (admin["id"],))
    admin_login = expect(call(FLASK, "POST", "/api/auth/login", {
        "email": admin["email"], "password": PASSWORD,
    }), 200)
    must(admin_login["user"]["role"] == "admin", "合成管理員角色未生效")
    admin["token"] = admin_login["access_token"]
    management_validation(admin, first)
    search_cases(admin, namespace, (first, second, third))
    print("PASS Ticket21 會員角色、格式、欄位、字面搜尋與精確分頁")
    article, own, other = content_cases(namespace, admin, first, second)
    status_cases(admin, second, article, other)
    print("PASS Ticket21 管理內容範圍、跨後端停用撤銷與內容保存")
    original_settings = settings_cases(admin, third)
    settings_head_cases(admin, third)
    concurrent_settings(admin)
    lockout_cases(admin, third)
    settings_login_race(admin, third)
    put_settings(admin, original_settings)
    search_plans(namespace)
    # 只公開合成 email 供隔離瀏覽器驗收；再登入確認管理員仍 active。
    ready = expect(call(FLASK, "POST", "/api/auth/login", {
        "email": admin["email"], "password": PASSWORD,
    }), 200)
    must(ready["user"]["role"] == "admin" and account_state(admin["id"])["status"] == "active",
         "瀏覽器 fixture 管理員不可登入")
    print("PASS Ticket21 設定、跨後端計數、交錯、時間窗與搜尋計畫")
    print("BROWSER_ADMIN_EMAIL=" + admin["email"])


# 控制診斷輸出，避免例外內容洩漏憑證或合成帳號資訊。
if __name__ == "__main__":
    try:
        run()
    except CheckFailure as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("Ticket 21 驗證失敗：未預期 " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
