"""CQ11：沿用原隔離 guard，以公開 HTTP／實際 DB 驗證會員管理與鎖定設定。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import json
import secrets
import subprocess
import sys
import time
from urllib.parse import quote

import cq09_http
from cq10_http import create_article, create_comment, login, row, wait_line
import ticket57_http as shared


BACKEND = "symfony"
PEER = "aspnet-core"
SETTINGS = "/api/settings/login-lockout"
MEMBERS = "/api/members"
FIELDS = {"id", "email", "nickname", "role", "status", "created_at"}
LIMITS = {"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400}
ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


# snapshot 保留完整帳號、session、失敗紀錄與設定，避免只比較筆數而漏掉部分更新。
def snapshot(identifiers):
    shared.check(identifiers and all(type(value) is int for value in identifiers), "快照 ID 不合法")
    ids = ",".join(map(str, identifiers))
    return json.loads(shared.sql(
        "SELECT json_build_object('accounts',(SELECT json_agg(a ORDER BY a.id) FROM accounts a "
        f"WHERE id IN ({ids})),'sessions',(SELECT json_agg(s ORDER BY s.jti) FROM login_sessions s "
        f"WHERE account_id IN ({ids})),'failures',(SELECT json_agg(f ORDER BY f.id) FROM login_failures f "
        f"WHERE account_id IN ({ids})),'settings',(SELECT to_jsonb(t) FROM login_lockout_settings t WHERE id=1))"))


# settings 讀回公開四欄並檢查實際 JSON 型態，不能讓 bool 與 int 的 Python 相等性掩蓋錯誤。
def settings(backend, token):
    value = shared.expect(shared.request(backend, "GET", SETTINGS, token=token), 200)
    shared.check(set(value) == {"enabled", *LIMITS} and type(value["enabled"]) is bool and
                 all(type(value[key]) is int for key in LIMITS), "設定公開欄位或型態不符")
    return value


# put_settings 同時核對空 data 與公開讀回，讓設定恢復也具有可觀察的完成證據。
def put_settings(backend, token, value):
    shared.check(shared.expect(shared.request(backend, "PUT", SETTINGS, value, token), 200) == {},
                 "設定寫入 data 不為空")
    shared.check(settings(backend, token) == value, "設定未完整提交")


# account 只接受核准 Account 六欄，逐項型態檢查可發現私密資料與 JSON 數值型態退步。
def account(value):
    shared.check(set(value) == FIELDS and type(value["id"]) is int and 1 <= value["id"] <= 9007199254740991 and
                 all(type(value[field]) is str for field in FIELDS - {"id"}) and
                 value["role"] == "member" and value["status"] in ("active", "disabled"),
                 "Account 欄位、型態或管理員排除不符")
    shared.check(datetime.strptime(value["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S") ==
                 value["created_at"], "Account 建立時間格式不符")


# reject 只輸出案例標籤與安全錯誤，完整敏感快照留在記憶體；每次拒絕均核對無資料副作用。
def reject(method, path, body, token, status, code, fields, identifiers, label):
    before = snapshot(identifiers)
    try:
        shared.expect(shared.request(BACKEND, method, path, body, token), status, code, fields)
        shared.check(snapshot(identifiers) == before, "拒絕請求改變完整帳號／session／失敗紀錄／設定")
    except AssertionError as error:
        raise AssertionError(f"CQ11 {label}: {error}") from error


# authorization_cases 在五端點分開驗證訪客、壞 JWT、會員、停用與撤銷，錯誤輸入不能越過角色門禁。
def authorization_cases(nonce):
    target = shared.user(BACKEND, nonce + "auth", "member")
    admin = shared.user(BACKEND, nonce + "auth", "admin")
    disabled = shared.user(BACKEND, nonce + "auth", "disabled")
    revoked = shared.user(BACKEND, nonce + "auth", "revoked")
    shared.expect(shared.request(BACKEND, "PUT", f"{MEMBERS}/{disabled['id']}/status",
                  {"status": "disabled"}, admin["token"]), 200)
    shared.expect(shared.request(BACKEND, "POST", "/api/auth/logout", {}, revoked["token"]), 200)
    identifiers = [actor["id"] for actor in (target, admin, disabled, revoked)]
    original = settings(BACKEND, admin["token"])
    operations = (("GET", MEMBERS + "?page=1&per_page=20", None),
                  ("GET", f"{MEMBERS}/{target['id']}", None),
                  ("PUT", f"{MEMBERS}/{target['id']}/status", {"status": "active"}),
                  ("GET", SETTINGS, None), ("PUT", SETTINGS, original))
    actors = (("guest", None, 401, "E0002"), ("invalid", "invalid", 401, "E0002"),
              ("member", target["token"], 403, "E0003"),
              ("disabled", disabled["token"], 403, "E0102"),
              ("revoked", revoked["token"], 401, "E0002"))
    for method, path, body in operations:
        for name, token, status, code in actors:
            reject(method, path, body, token, status, code, None, identifiers, name + " valid-input")
            bad_path = path.replace(str(target["id"]), "bad") if path.startswith(MEMBERS + "/") else path
            bad_path += ("&" if "?" in bad_path else "?") + "unknown=1"
            reject(method, bad_path, "{", token, status, code, None, identifiers, name + " bad-input")
        # 管理員的有效讀取與同值寫入也不應建立 session、清鎖或更動帳號。
        before = snapshot(identifiers)
        result = shared.expect(shared.request(BACKEND, method, path, body, admin["token"]), 200)
        if method == "PUT":
            shared.check(result == {}, "管理員同值寫入 data")
        elif path.startswith(MEMBERS + "/"):
            account(result)
        shared.check(snapshot(identifiers) == before, "管理員唯讀／同值操作改變完整資料")
    for identifier in (admin["id"], 9007199254740991):
        for method, suffix, body in (("GET", "", None), ("PUT", "/status", {"status": "disabled"})):
            reject(method, f"{MEMBERS}/{identifier}{suffix}", body, admin["token"], 404, "E0004",
                   None, identifiers, "admin-or-missing target")
    print("PASS CQ11 五管理端點身分／錯誤先後、管理員目標 404、完整拒絕快照", flush=True)


# member_list 從 DB 公開投影取得獨立預期，再由 Python 字面包含計算搜尋，避免複製產品 SQL 判定。
def member_list(token, keyword=None, page=1, per_page=100):
    expected = json.loads(shared.sql(
        "SELECT coalesce(json_agg(t ORDER BY sort_time DESC,id DESC),'[]') FROM "
        "(SELECT id,email,nickname,role,status,to_char(created_at AT TIME ZONE 'Asia/Taipei',"
        "'YYYY-MM-DD HH24:MI:SS') AS created_at,created_at AS sort_time FROM accounts WHERE role='member') t"))
    for item in expected:
        item.pop("sort_time")
    if keyword is not None and keyword.strip(" "):
        needle = keyword.strip(" ").translate(ASCII_FOLD)
        expected = [item for item in expected if any(needle in item[field].translate(ASCII_FOLD)
                                                    for field in ("nickname", "email"))]
    path = f"{MEMBERS}?page={page}&per_page={per_page}"
    if keyword is not None:
        path += "&filters%5Bkeyword%5D=" + quote(keyword, safe="")
    value = shared.expect(shared.request(BACKEND, "GET", path, token=token), 200)
    total = len(expected)
    shared.check(value == {"list": expected[(page - 1) * per_page:page * per_page], "pagination":
                 {"page": page, "per_page": per_page, "total": total, "total_pages": (total + per_page - 1) // per_page}},
                 "會員完整集合、穩定排序、精確 total 或分頁不符")
    for item in value["list"]:
        account(item)
    return value


# list_cases 用同時間與不同 ID 的唯一 fixture 證明排序及跨頁，既有資料仍納入未篩選的精確總數。
def list_cases(nonce):
    admin = shared.user(BACKEND, nonce + "list", "admin")
    members = [shared.user(BACKEND, nonce + "list", kind, nickname)
               for kind, nickname in (("a", "Aa%_\\Ä陳" + nonce), ("b", "aa%_\\ä陳" + nonce),
                                      ("c", "AaXY\\Ä陳" + nonce))]
    shared.expect(shared.request(BACKEND, "PUT", f"{MEMBERS}/{members[1]['id']}/status",
                  {"status": "disabled"}, admin["token"]), 200)
    ids = ",".join(str(member["id"]) for member in members)
    times = json.loads(shared.sql(f"SELECT json_object_agg(id,created_at) FROM accounts WHERE id IN ({ids})"))
    try:
        # 時間 fixture 只改本輪三帳號，finally 恢復完整精度，保留測資供重跑查核。
        shared.sql(f"UPDATE accounts SET created_at='2026-01-01 00:00:00+08' WHERE id IN ({ids})")
        value = member_list(admin["token"], nonce + "list", per_page=2)
        shared.check([item["id"] for item in value["list"]] == sorted((member["id"] for member in members), reverse=True)[:2]
                     and value["pagination"]["total"] == 3, "唯一集合或同時間 ID 降冪不符")
        for page in (2, 3, 9007199254740991):
            member_list(admin["token"], nonce + "list", page, 2 if page < 4 else 100)
        for keyword in (None, "", "   ", "A", "Aa", "陳", "Ä陳", "aa%_\\Ä陳" + nonce,
                        "AA%_\\ä陳" + nonce, " " + nonce + "list ", "界" * 254, "no-match-" + nonce):
            member_list(admin["token"], keyword)
        # Email 與暱稱各自能命中；這些結果不能因 disabled 或 admin fixture 混入而偏差。
        member_list(admin["token"], members[0]["email"].upper())
        first = member_list(admin["token"], per_page=1)
        leading = shared.expect(shared.request(BACKEND, "GET", MEMBERS + "?page=0001&per_page=0001",
                                               token=admin["token"]), 200)
        shared.check(leading == first, "合法前導零 Query 未解析為相同分頁")
        for member in members:
            detail = shared.expect(shared.request(BACKEND, "GET", f"{MEMBERS}/{member['id']}", token=admin["token"]), 200)
            account(detail)
            shared.check(detail["email"] == member["email"], "會員詳情與建立帳號不同")
            shared.check(shared.expect(shared.request(BACKEND, "GET", f"{MEMBERS}/000{member['id']}",
                         token=admin["token"]), 200) == detail, "合法前導零 Path 未解析為相同會員")
    finally:
        for identifier, timestamp in times.items():
            shared.check(identifier.isdecimal() and "'" not in timestamp, "時間 fixture 不合法")
            shared.sql(f"UPDATE accounts SET created_at='{timestamp}' WHERE id={identifier}")
        shared.check(json.loads(shared.sql(f"SELECT json_object_agg(id,created_at) FROM accounts WHERE id IN ({ids})")) == times,
                     "會員時間 fixture 未恢復")
    print("PASS CQ11 會員 active／disabled、排除 admin、完整 Account、精確集合／排序／超頁／搜尋邊界", flush=True)


# validation_cases 補齊五端點原始輸入等價類，所有拒絕都核對完整 DB 快照與共同 errors 文案。
def validation_cases(nonce):
    member = shared.user(BACKEND, nonce + "validation", "member")
    admin = shared.user(BACKEND, nonce + "validation", "admin")
    token, identifiers = admin["token"], [member["id"], admin["id"]]
    original = settings(BACKEND, token)
    detail = f"{MEMBERS}/{member['id']}"
    operations = (("GET", MEMBERS + "?page=1&per_page=20", None), ("GET", detail, None),
                  ("PUT", detail + "/status", {"status": "active"}),
                  ("GET", SETTINGS, None), ("PUT", SETTINGS, original))
    for method, path, body in operations:
        reject(method, path + ("&" if "?" in path else "?") + "extra=1", body, token, 422,
               "E0001", {"extra": ["不支援此欄位"]}, identifiers, "unknown-query")
        if method == "GET":
            reject(method, path, "{", token, 422, "E0001", {"_body": ["此 API 不接受 Request Body"]},
                   identifiers, "GET-body-before-JSON")
        else:
            for root in (None, "null", "true", "123", '"text"', []):
                reject(method, path, root, token, 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]},
                       identifiers, "write-root")
            reject(method, path, "{", token, 400, "E0005", None, identifiers, "malformed-JSON")
            reject(method, path, {**body, "extra": 1}, token, 422, "E0001", {"extra": ["不支援此欄位"]},
                   identifiers, "unknown-body")
    for field in ("page", "per_page"):
        other = "per_page=20" if field == "page" else "page=1"
        reject("GET", MEMBERS + "?" + other, None, token, 422, "E0001", {field: ["此欄位為必填"]}, identifiers, "page-missing")
        for value in ("", "0", "-1", "+1", "1.0", "1e1", "%201", "%EF%BC%91"):
            reject("GET", f"{MEMBERS}?{other}&{field}={value}", None, token, 422, "E0001",
                   {field: ["欄位格式或範圍不正確"]}, identifiers, "page-format")
        reject("GET", f"{MEMBERS}?{other}&{field}=bad&{field}=1", None, token, 422, "E0001",
               {field: ["此參數不可重複"]}, identifiers, "page-duplicate")
        reject("GET", f"{MEMBERS}?{other}&{field}%5B%5D=1", None, token, 422, "E0001",
               {field: ["欄位型態不正確"]}, identifiers, "page-array")
    for query, field, message in (("per_page=101", "per_page", "欄位格式或範圍不正確"),
            ("filters%5Bkeyword%5D=" + "x" * 255, "filters.keyword", "欄位格式或範圍不正確"),
            ("filters%5Bkeyword%5D=a&filters%5Bkeyword%5D=b", "filters.keyword", "此參數不可重複"),
            ("filters%5Bkeyword%5D%5B%5D=x", "filters.keyword", "欄位型態不正確"),
            ("filters%5Bother%5D=x", "filters.other", "不支援此欄位")):
        base = "?page=1&" if query.startswith("per_page") else "?page=1&per_page=20&"
        reject("GET", MEMBERS + base + query, None, token, 422, "E0001", {field: [message]}, identifiers, "filter-input")
    for method, suffix, body in (("GET", "", None), ("PUT", "/status", {"status": "active"})):
        for value in ("bad", "0", "-1", "+1", "1.5", "1e1", "9007199254740992"):
            reject(method, f"{MEMBERS}/{value}{suffix}", body, token, 422, "E0001",
                   {"member_id": ["欄位格式或範圍不正確"]}, identifiers, "member-id")
    for body, message in (({}, "此欄位為必填"), ({"status": None}, "此欄位不可為 null"),
                          ({"status": False}, "欄位型態不正確"), ({"status": 1}, "欄位型態不正確"),
                          ({"status": []}, "欄位型態不正確"), ({"status": {}}, "欄位型態不正確"),
                          ({"status": ""}, "欄位格式或範圍不正確"), ({"status": "ACTIVE"}, "欄位格式或範圍不正確")):
        reject("PUT", detail + "/status", body, token, 422, "E0001", {"status": [message]}, identifiers, "status-input")
    # 關閉狀態仍驗證所有數值；合法上下界均須完整儲存，不得以 disabled 為由略過。
    try:
        for enabled in (True, False):
            valid = {"enabled": enabled, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 300}
            put_settings(BACKEND, token, valid)
            for field in ("enabled", *LIMITS):
                for kind, value, message in (("missing", None, "此欄位為必填"), ("null", None, "此欄位不可為 null"),
                        ("type", "true" if field == "enabled" else "1", "欄位型態不正確")):
                    body = {key: item for key, item in valid.items() if key != field} if kind == "missing" else {**valid, field: value}
                    reject("PUT", SETTINGS, body, token, 422, "E0001", {field: [message]}, identifiers, "settings-" + kind)
                wrong_types = (0, 1, 1.0, [], {}) if field == "enabled" else (True, False, 1.0, [], {})
                for value in wrong_types:
                    reject("PUT", SETTINGS, {**valid, field: value}, token, 422, "E0001",
                           {field: ["欄位型態不正確"]}, identifiers, "settings-type")
                if field in LIMITS:
                    for value in (0, -1, LIMITS[field] + 1):
                        reject("PUT", SETTINGS, {**valid, field: value}, token, 422, "E0001",
                               {field: ["欄位格式或範圍不正確"]}, identifiers, "settings-range")
            reject("PUT", SETTINGS, {"enabled": None, "window_minutes": "1", "failure_threshold": 0, "extra": 1},
                   token, 422, "E0001", {"enabled": ["此欄位不可為 null"], "window_minutes": ["欄位型態不正確"],
                   "failure_threshold": ["欄位格式或範圍不正確"], "lock_seconds": ["此欄位為必填"],
                   "extra": ["不支援此欄位"]}, identifiers, "aggregate-settings")
            for boundary in (1, None):
                put_settings(BACKEND, token, {"enabled": enabled, **{key: boundary or limit for key, limit in LIMITS.items()}})
    finally:
        put_settings(BACKEND, token, original)
    print("PASS CQ11 五端點 Query／Path／JSON、四設定欄位完整等價類與開關兩態邊界、拒絕零部分寫入", flush=True)


# lifecycle_cases 以十二後端各自合法登入證明停用撤銷全部 session，且恢復不改內容、不解除暫時鎖定。
def lifecycle_cases(nonce):
    member = shared.user(BACKEND, nonce + "life", "member")
    admin = shared.user(BACKEND, nonce + "life", "admin")
    credentials = {backend: member["token"] if backend == BACKEND else login(backend, member) for backend in shared.BACKENDS}
    article = create_article(BACKEND, member, "cq11-" + nonce + "-life")
    comment = create_comment(BACKEND, member, article, "cq11-" + nonce + "-comment")
    content = (row("articles", article), row("comments", comment))
    path = f"{MEMBERS}/{member['id']}/status"
    before = snapshot([member["id"]])
    old_lock = before["accounts"][0]["locked_until"]
    try:
        shared.sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '10 minutes' WHERE id={member['id']}")
        locked = snapshot([member["id"]])
        previous = locked
        for status in ("disabled", "active"):
            shared.expect(shared.request(BACKEND, "PUT", path, {"status": status}, admin["token"]), 200)
            current = snapshot([member["id"]])
            expected_account = {**locked["accounts"][0], "status": status}
            shared.check(current["accounts"] == [expected_account] and current["failures"] == locked["failures"] and
                         current["settings"] == locked["settings"] and len(current["sessions"]) == len(locked["sessions"]),
                         "停用／恢復更動鎖定世代、到期或其他帳號欄位")
            for old, new in zip(locked["sessions"], current["sessions"]):
                shared.check(new["revoked_at"] is not None and {**new, "revoked_at": old["revoked_at"]} == old,
                             "停用未撤銷全部後端 session 或更動其他 session 欄位")
            if status == "active":
                shared.check(current["sessions"] == previous["sessions"], "恢復帳號重寫既有撤銷時間")
            shared.expect(shared.request(BACKEND, "PUT", path, {"status": status}, admin["token"]), 200)
            shared.check(snapshot([member["id"]]) == current, "同值會員狀態重寫撤銷時間／鎖定／世代")
            for backend, token in credentials.items():
                shared.expect(shared.request(backend, "GET", "/api/me", token=token),
                              403 if status == "disabled" else 401, "E0102" if status == "disabled" else "E0002")
            shared.expect(shared.request(BACKEND, "POST", "/api/auth/login",
                          {"email": member["email"], "password": "Abcd1234"}),
                          403 if status == "disabled" else 429, "E0102" if status == "disabled" else "E0105")
            previous = current
        shared.check(content == (row("articles", article), row("comments", comment)), "停用／恢復改寫文章或留言")
    finally:
        literal = "NULL" if old_lock is None else "'" + old_lock + "'"
        shared.sql(f"UPDATE accounts SET locked_until={literal} WHERE id={member['id']}")
        shared.check(snapshot([member["id"]])["accounts"][0]["locked_until"] == old_lock, "生命週期時間 fixture 未恢復")
    new_token = login(BACKEND, member)
    shared.expect(shared.request(BACKEND, "GET", "/api/me", token=new_token), 200)
    for backend, token in credentials.items():
        shared.expect(shared.request(backend, "GET", "/api/me", token=token), 401, "E0002")
    print("PASS CQ11 停用／恢復：十二後端 session 撤銷、內容不變、保留鎖定、同值無副作用及新登入", flush=True)


# settings_effect_cases 區分數值更新、真正關閉、同值關閉與重新啟用，保留帳號 disabled 及既有失敗紀錄。
def settings_effect_cases(nonce):
    admin = shared.user(BACKEND, nonce + "effect", "admin")
    members = [shared.user(BACKEND, nonce + "effect", kind) for kind in ("member", "disabled")]
    identifiers = [member["id"] for member in members]
    original = settings(BACKEND, admin["token"])
    old_locks = {item["id"]: item["locked_until"] for item in snapshot(identifiers)["accounts"]}
    try:
        base = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 300}
        put_settings(BACKEND, admin["token"], base)
        for member in members:
            wrong = {"email": member["email"], "password": "Wrong1234"}
            shared.expect(shared.request(BACKEND, "POST", "/api/auth/login", wrong), 401, "E0101")
            shared.expect(shared.request(BACKEND, "POST", "/api/auth/login", wrong), 429, "E0105")
        shared.expect(shared.request(BACKEND, "PUT", f"{MEMBERS}/{members[1]['id']}/status",
                      {"status": "disabled"}, admin["token"]), 200)
        before = snapshot(identifiers)
        changed = {"enabled": True, "window_minutes": 7, "failure_threshold": 3, "lock_seconds": 60}
        put_settings(BACKEND, admin["token"], changed)
        current = snapshot(identifiers)
        shared.check({**current, "settings": before["settings"]} == before, "數值更新重算鎖到期、世代或帳號資料")
        off = {**changed, "enabled": False}
        put_settings(BACKEND, admin["token"], off)
        disabled = snapshot(identifiers)
        expected = [{**item, "locked_until": None, "failure_epoch": item["failure_epoch"] + 1} for item in current["accounts"]]
        shared.check(disabled["accounts"] == expected and disabled["sessions"] == current["sessions"] and
                     disabled["failures"] == current["failures"], "關閉未恰好清鎖／換世代一次，或改變停用／session／失敗事件")
        # 在已關閉狀態放回本輪時間 fixture；同值 false 不得再次清鎖或換世代。
        shared.sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '10 minutes' WHERE id IN ({','.join(map(str, identifiers))})")
        same_before = snapshot(identifiers)
        put_settings(BACKEND, admin["token"], off)
        shared.check(snapshot(identifiers) == same_before, "同值 false 重做清鎖或世代副作用")
        put_settings(BACKEND, admin["token"], changed)
        enabled = snapshot(identifiers)
        shared.check({**enabled, "settings": same_before["settings"]} == same_before, "重啟未保留數值／狀態／世代／鎖定")
        shared.expect(shared.request(BACKEND, "POST", "/api/auth/login",
                      {"email": members[1]["email"], "password": "Abcd1234"}), 403, "E0102")
    finally:
        put_settings(BACKEND, admin["token"], original)
        for identifier, timestamp in old_locks.items():
            literal = "NULL" if timestamp is None else "'" + timestamp + "'"
            shared.sql(f"UPDATE accounts SET locked_until={literal} WHERE id={identifier}")
        shared.check({item["id"]: item["locked_until"] for item in snapshot(identifiers)["accounts"]} == old_locks,
                     "設定效果時間 fixture 未恢復")
    print("PASS CQ11 數值不重算鎖到期／世代、關閉清鎖一次、false 同值不清鎖、重啟保值及 disabled 保留", flush=True)


# holder 只鎖既有設定／本輪帳號列；所有退出路徑釋放交易並確認連線清除，避免測試留下阻塞。
@contextmanager
def holder(table, identifier, name):
    shared.check(shared.SAFE and table in ("accounts", "login_lockout_settings") and type(identifier) is int,
                 "holder 目標不合法")
    command = ["docker", "compose", "--profile", "*", "exec", "-T", "-e", "PGAPPNAME=" + name,
               "db", "psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    pid = None
    try:
        process.stdin.write("BEGIN;\nSET LOCAL idle_in_transaction_session_timeout='20s';\n"
                            f"SELECT id FROM {table} WHERE id={identifier} FOR UPDATE;\n\\echo cq11-locked\n")
        process.stdin.flush()
        wait_line(process, "cq11-locked")
        pid = int(shared.sql(f"SELECT pid FROM pg_stat_activity WHERE application_name='{name}'"))
        yield pid
    finally:
        try:
            if process.poll() is None:
                process.stdin.write("ROLLBACK;\n\\q\n")
                process.stdin.flush()
            process.communicate(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            # 即使握手前失敗也依唯一 appname 找回本輪連線，不能遺留未知 PID 的鎖。
            shared.sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='{name}'")
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        shared.check(shared.sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{name}'") == "0",
                     "CQ11 holder 未清除")


# waiting 只認實際 app row-lock 等待，沿 holder 鎖鏈確認兩個 API 已重疊，不能以 sleep 假裝並行。
def waiting(pid, table, previous=()):
    shared.check(table in ("accounts", "login_lockout_settings"), "等待資料表不合法")
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        rows = json.loads(shared.sql(
            "SELECT coalesce(json_agg(json_build_object('pid',pid,'blockers',pg_blocking_pids(pid))),'[]') "
            "FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' AND state='active' "
            f"AND wait_event_type='Lock' AND query ~* 'SELECT.*{table}.*FOR UPDATE'"))
        for item in rows:
            if item["pid"] not in previous and any(blocker in (pid, *previous) for blocker in item["blockers"]):
                return item["pid"]
        time.sleep(0.025)
    raise AssertionError("未觀察到 CQ11 API 實際 row-lock 等待")


# overlap_cases 先觀察兩個不同管理員的 row-lock 排隊，再釋放 holder，雙方向核對最後完整提交。
def overlap_cases(nonce):
    for first, second in ((BACKEND, PEER), (PEER, BACKEND)):
        suffix = nonce + first
        # target 先建立確保 ID 最小；memberStatus 依 ID 取鎖時，兩個 actor 都可先到 target 等待。
        target = shared.user(BACKEND, suffix + "overlap", "member")
        actors = [shared.user(backend, suffix + "overlap" + str(index), "admin")
                  for index, backend in enumerate((first, second))]
        peer_tokens = [login(second if index == 0 else first, actor) for index, actor in enumerate(actors)]
        original = settings(first, actors[0]["token"])
        values = ({"enabled": True, "window_minutes": 7, "failure_threshold": 3, "lock_seconds": 31},
                  {"enabled": True, "window_minutes": 8, "failure_threshold": 4, "lock_seconds": 32})
        try:
            for table, identifier in (("login_lockout_settings", 1), ("accounts", target["id"])):
                before = snapshot([target["id"]])
                futures = []
                with ThreadPoolExecutor(max_workers=2) as pool:
                    with holder(table, identifier, "cq11-" + suffix + table) as pid:
                        previous = []
                        for index, backend in enumerate((first, second)):
                            path = SETTINGS if table == "login_lockout_settings" else f"{MEMBERS}/{target['id']}/status"
                            body = values[index] if table == "login_lockout_settings" else {"status": ("disabled", "active")[index]}
                            futures.append(pool.submit(shared.request, backend, "PUT", path, body, actors[index]["token"]))
                            previous.append(waiting(pid, table, tuple(previous)))
                        shared.check(len(set(previous)) == 2 and not any(future.done() for future in futures), "API 未同時等待列鎖")
                    for future in futures:
                        shared.check(shared.expect(future.result(timeout=20), 200) == {}, "並行管理寫入 data")
                committed = snapshot([target["id"]])
                if table == "login_lockout_settings":
                    shared.check(settings(first, actors[0]["token"]) == values[1] == settings(second, peer_tokens[0]) and
                                 {key: committed["settings"][key] for key in values[1]} == values[1], "最後完整設定提交／雙端讀回不一致")
                    shared.check({**committed, "settings": before["settings"]} == before, "數值設定競爭改變會員資料")
                else:
                    shared.check(committed["accounts"] == before["accounts"] and committed["failures"] == before["failures"] and
                                 committed["settings"] == before["settings"] and
                                 all(session["revoked_at"] is not None for session in committed["sessions"]),
                                 "會員競爭最後狀態、鎖定或 session 撤銷不符")
                    for backend, token in ((first, actors[0]["token"]), (second, peer_tokens[0])):
                        value = shared.expect(shared.request(backend, "GET", f"{MEMBERS}/{target['id']}", token=token), 200)
                        account(value)
                        shared.check(value["status"] == "active", "會員最後提交未由雙端讀回")
            print(f"PASS CQ11 真 row-lock overlap：{first}→{second} 設定完整值／會員最後狀態、DB／雙端讀回", flush=True)
        finally:
            put_settings(first, actors[0]["token"], original)


# revalidation_cases 先證明外層認證後的寫入正在等鎖，再撤銷或降權；交易內必須重驗且不能部分寫入。
def revalidation_cases(nonce):
    restorer = shared.user(BACKEND, nonce + "restore", "admin")
    for table in ("login_lockout_settings", "accounts"):
        for action in ("logout", "demote"):
            suffix = nonce + table + action
            target = shared.user(BACKEND, suffix, "member")
            actor = shared.user(BACKEND, suffix, "admin")
            original = settings(BACKEND, actor["token"])
            identifiers = [target["id"], actor["id"]]
            path = SETTINGS if table == "login_lockout_settings" else f"{MEMBERS}/{target['id']}/status"
            body = {**original, "lock_seconds": 43 if original["lock_seconds"] != 43 else 44} if table == "login_lockout_settings" else {"status": "disabled"}
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with holder(table, 1 if table == "login_lockout_settings" else target["id"], "cq11-" + suffix) as pid:
                        future = pool.submit(shared.request, BACKEND, "PUT", path, body, actor["token"])
                        waiting(pid, table)
                        shared.check(not future.done(), "寫入未停在認證後的列鎖等待")
                        if action == "logout":
                            shared.expect(shared.request(BACKEND, "POST", "/api/auth/logout", {}, actor["token"]), 200)
                        else:
                            shared.check(shared.sql(f"UPDATE accounts SET role='member' WHERE id={actor['id']} RETURNING role") == "member",
                                         "降權 fixture 未生效")
                        before = snapshot(identifiers)
                    shared.expect(future.result(timeout=20), 401 if action == "logout" else 403,
                                  "E0002" if action == "logout" else "E0003")
                shared.check(snapshot(identifiers) == before, "等待後拒絕留下部分寫入或額外撤銷")
                print(f"PASS CQ11 交易內重驗：{table} 等待後 {action}，真 HTTP 拒絕且完整資料不變", flush=True)
            finally:
                if action == "demote":
                    shared.check(shared.sql(f"UPDATE accounts SET role='admin' WHERE id={actor['id']} RETURNING role") == "admin",
                                 "降權 fixture 未恢復")
                # 預期拒絕仍保留恢復路徑，產品若誤提交也不能讓測試遺留改動後的設定。
                put_settings(BACKEND, restorer["token"], original)


# main 只用既有 guard 與標準庫；共用套件和本票缺口全部成功才標示已驗範圍，未決 NUL 搜尋另列。
def main():
    shared.check(len(sys.argv) == 1, "CQ11 不接受改寫目標或略過案例的參數")
    shared.guard()
    nonce = secrets.token_hex(5)
    cq09_http.entry_cases(nonce)
    shared.backend_cases(BACKEND, nonce)
    shared.contract_cases(BACKEND, nonce)
    shared.lockout_cases(BACKEND, nonce)
    shared.lockout_edge_cases(BACKEND, nonce)
    shared.ordered_cases(BACKEND, nonce)
    authorization_cases(nonce)
    list_cases(nonce)
    validation_cases(nonce)
    lifecycle_cases(nonce)
    settings_effect_cases(nonce)
    overlap_cases(nonce)
    revalidation_cases(nonce)
    print("PASS CQ11 已定案 HTTP／DB 範圍：Symfony 22 操作、管理會員／設定及受控跨後端並行", flush=True)
    print("PENDING CQ33：搜尋 U+0000 政策未決，未 probe／未標 PASS；本票未改 SQL，不新增手寫計畫冒充 runtime 證據", flush=True)


if __name__ == "__main__":
    main()
