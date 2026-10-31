"""CQ09：沿用 Ticket 57 隔離保護，驗證 Symfony 帳號整理前後的公開契約。"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import ticket57_http as shared


BACKEND = "symfony"
PEER = "aspnet-core"
ORIGIN = "http://localhost:8080"
REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
SETTINGS = "/api/settings/login-lockout"


# 原樣送出 bytes 並保留回應 bytes，避免 JSON 客戶端改寫解析錯誤或掩蓋 HEAD 的內容。
def raw_request(method, path, body=None, token=None, origin=ORIGIN, extra_headers=None):
    shared.check(shared.SAFE, "尚未核對隔離 HTTP 目標")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "Origin": origin}
    headers.update(extra_headers or {})
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = Request(shared.BASES[BACKEND] + path, data=body, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), response.headers


# 只在需要共同 envelope 時解析原始回應；失敗訊息不印出帳密、JWT 或伺服器內容。
def decoded(result):
    status, raw, headers = result
    shared.check(headers.get_content_type() == "application/json", "回應不是 JSON")
    return status, json.loads(raw), headers


# 讀取單一測資的狀態與登入紀錄快照，確認拒絕請求及 /me 沒有隱藏副作用。
def state(account_id):
    return shared.sql(
        "SELECT json_build_object('status',a.status,'epoch',a.failure_epoch,'locked',a.locked_until,"
        "'failures',(SELECT count(*) FROM login_failures WHERE account_id=a.id),"
        "'sessions',(SELECT json_agg(s ORDER BY s.jti) FROM login_sessions s WHERE s.account_id=a.id))::text "
        f"FROM accounts a WHERE a.id={account_id}"
    )


# 由實際登入驗證八小時 claims 與 DB 紀錄，回傳 claims 供選擇性撤銷與時間 fixture 使用。
def login_claims(data, account_id, role):
    parts = data["access_token"].split(".")
    shared.check(len(parts) == 3, "JWT 區段數不符")
    header, claims = [json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                      for part in parts[:2]]
    shared.check(header == {"alg": "HS256", "typ": "JWT"} and
                 set(claims) == {"iss", "aud", "sub", "jti", "iat", "nbf", "exp"} and
                 claims["iss"] == "ai-workflow-fullstack:symfony" and
                 claims["aud"] == "ai-workflow-fullstack-api:symfony" and
                 claims["sub"] == str(account_id) and
                 all(type(claims[field]) is int for field in ("iat", "nbf", "exp")) and
                 claims["nbf"] == claims["iat"] and claims["exp"] - claims["iat"] == 28800,
                 "JWT header／claims／八小時期限不符")
    shared.check(len(base64.urlsafe_b64decode(claims["jti"] + "==")) == 16 and
                 re.fullmatch(r"[A-Za-z0-9_-]{22}", claims["jti"]) is not None and
                 data["token_type"] == "Bearer" and data["user"]["id"] == account_id and
                 data["user"]["role"] == role and data["user"]["status"] == "active" and
                 set(data) == {"access_token", "token_type", "expires_at", "user"} and
                 set(data["user"]) == {"id", "email", "nickname", "role", "status", "created_at"} and
                 data["expires_at"] == datetime.fromtimestamp(
                     claims["exp"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                 "登入資料或到期時間不符")
    shared.check(shared.sql(
        "SELECT account_id||'|'||backend_id||'|'||extract(epoch FROM issued_at)::bigint||'|'||"
        "extract(epoch FROM expires_at)::bigint||'|'||(revoked_at IS NULL)::text "
        f"FROM login_sessions WHERE jti='{claims['jti']}'") ==
        f"{account_id}|symfony|{claims['iat']}|{claims['exp']}|true", "JWT 與 session 未一致提交")
    return claims


# 註冊有效／無效等價類及跨後端唯一性，避免為帳號測試重播不相關的內容競爭案例。
def registration_cases(nonce):
    for index, (password, nickname) in enumerate((("Abcd", "陳"), ("1234", "😀" * 30),
                                                ("Ab12" * 4, " e\u0301\r\n陳 "))):
        email = f"cq09-{nonce}-valid{index}@example.test"
        body = {"email": "  " + email.upper() + "  ", "password": password, "nickname": nickname}
        shared.check(shared.expect(shared.request(BACKEND, "POST", REGISTER, body, "stale"), 201) == {},
                     "合法註冊或忽略舊 JWT 失敗")
        account_id = shared.sql(f"SELECT id FROM accounts WHERE email='{email}'")
        shared.check(account_id.isdecimal(), "合法註冊未恰好建立一個帳號")
        account_id = int(account_id)
        shared.check(shared.sql(f"SELECT role||'|'||status FROM accounts WHERE id={account_id}") ==
                     "member|active" and shared.sql(
                         f"SELECT count(*) FROM login_sessions WHERE account_id={account_id}") == "0",
                     "註冊自行登入或角色不符")
        login = shared.expect(shared.request(BACKEND, "POST", LOGIN,
            {"email": body["email"], "password": password}, "stale"), 200)
        login_claims(login, account_id, "member")
        shared.check(login["user"]["email"] == email and login["user"]["nickname"] == nickname,
                     "Email 正規化或暱稱原文遺失")
        peer = shared.expect(shared.request(PEER, "POST", LOGIN,
            {"email": email, "password": password}), 200)
        shared.check(peer["user"]["id"] == account_id, "跨後端 Argon2id 登入失敗")
        shared.expect(shared.request(PEER, "GET", "/api/me", token=login["access_token"]), 401, "E0002")
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=peer["access_token"]), 401, "E0002")

    # 每個拒絕輸入均核對未新增帳號；格式錯誤密碼另在已存在帳號上驗證不計失敗。
    email = f"cq09-{nonce}-invalid@example.test"
    for field, values in (("password", ("Ab1", "A" * 17, " Ab1", "Ab1 ", "Ab!1", "中文12")),
                          ("nickname", ("", "😀" * 31, "壞\x00名", "\t\u3000\u00a0")),
                          ("email", ("\t" + email, "bad..name@example.test", "陳@example.test"))):
        for value in values:
            body = {"email": email, "password": "Abcd1234", "nickname": "邊界", field: value}
            shared.expect(shared.request(BACKEND, "POST", REGISTER, body), 422, "E0001",
                          {field: ["欄位格式或範圍不正確"]})
            shared.check(shared.sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "0",
                         "拒絕註冊留下帳號")

    # 共同 helper 已提供 Barrier 與原始公開 request；只保留本票需要的唯一性競爭部分。
    email = f"cq09-{nonce}-race@example.test"
    body = {"email": " " + email.upper() + " ", "password": "Abcd1234", "nickname": "並行作者"}
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(shared.synchronized_request, backend, "POST", REGISTER, body, None, barrier)
                   for backend in (BACKEND, PEER)]
        results = [future.result(timeout=20) for future in futures]
    shared.check(sorted(result[0] for result in results) == [201, 422], "跨後端註冊唯一性競爭失敗")
    for result in results:
        shared.expect(result, result[0], "E0001" if result[0] == 422 else None,
                      {"email": ["此 Email 已註冊"]} if result[0] == 422 else None)
    shared.check(shared.sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "1",
                 "競爭註冊未恰好提交一個帳號")
    for backend in (BACKEND, PEER):
        shared.expect(shared.request(backend, "POST", LOGIN,
            {"email": email, "password": "Abcd1234"}), 200)
    print("PASS CQ09 註冊正規化、密碼／Unicode 邊界、跨後端唯一性與 JWT 隔離", flush=True)


# 對帳號操作送原始 JSON 與錯誤 Query；驗證認證優先及所有拒絕均無 session／計數副作用。
def input_cases(nonce):
    member = shared.user(BACKEND, nonce + "i", "member")
    before = state(member["id"])
    observed = []
    for path in (REGISTER, LOGIN):
        shared.expect(decoded(raw_request("POST", path + "?extra=1", b"{")), 422, "E0001",
                      {"extra": ["不支援此欄位"]})
        shared.expect(decoded(raw_request("POST", path, b"")), 422, "E0001",
                      {"_body": ["Request Body 必須為 JSON object"]})
        for raw in (b"{", b'{"email":"\xff"}'):
            result = decoded(raw_request("POST", path, raw))
            shared.expect(result, 400, "E0005")
            observed.append((result[1]["log_id"], "POST", path, 400))
        for raw in (b"null", b"true", b"123", b'"text"'):
            shared.expect(decoded(raw_request("POST", path, raw)), 422, "E0001",
                          {"_body": ["Request Body 必須為 JSON object"]})
        expected = {"email": ["此欄位不可為 null"], "password": ["欄位型態不正確"],
                    "extra": ["不支援此欄位"]}
        if path == REGISTER:
            expected["nickname"] = ["此欄位為必填"]
        shared.expect(shared.request(BACKEND, "POST", path,
            {"email": None, "password": False, "extra": 1}), 422, "E0001", expected)
        shared.expect(shared.request(BACKEND, "POST", path,
            {"email": "bad", "password": "!", **({"nickname": ""} if path == REGISTER else {})}),
            422, "E0001", {field: ["欄位格式或範圍不正確"]
                           for field in (("email", "password", "nickname") if path == REGISTER
                                         else ("email", "password"))})
    for password in ("Ab1", "A" * 17, " Ab1", "Ab!1", "中文12"):
        shared.expect(shared.request(BACKEND, "POST", LOGIN,
            {"email": member["email"], "password": password}), 422, "E0001",
            {"password": ["欄位格式或範圍不正確"]})

    # 同時有壞 Query／Body 時，未登入仍先 401；有效身分才進參數驗證。
    for method, path, raw in (("GET", "/api/me", b"{"), ("POST", "/api/auth/logout", b"{")):
        for token in (None, "stale"):
            shared.expect(decoded(raw_request(method, path + "?extra=1", raw, token)), 401, "E0002")
        shared.expect(decoded(raw_request(method, path + "?extra=1", raw, member["token"])),
                      422, "E0001", {"extra": ["不支援此欄位"]})
        shared.expect(decoded(raw_request(method, path + "?extra=1&extra=2", token=member["token"])),
                      422, "E0001", {"extra": ["此參數不可重複"]})
    shared.expect(decoded(raw_request("POST", "/api/auth/logout", b"{", member["token"])), 400, "E0005")
    shared.expect(decoded(raw_request("GET", "/api/me", b"{", member["token"])), 422, "E0001",
                  {"_body": ["此 API 不接受 Request Body"]})
    shared.check(state(member["id"]) == before, "輸入拒絕改變帳號／session／失敗計數")
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ09 原始 JSON／多欄錯誤、認證優先及拒絕無副作用", flush=True)


# 簽署隔離時間／錯誤 claims 測資；只讀 guard 已核對的金鑰，不改正式時鐘或輸出秘密。
def signed_token(claims, header=None):
    shared.check(shared.SAFE, "尚未核對隔離金鑰")
    key = base64.b64decode(Path(".local/keys/symfony.key").read_bytes().rstrip(b"\n"), validate=True)
    shared.check(len(key) == 32, "隔離金鑰長度不符")
    parts = [base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
             for value in (header or {"alg": "HS256", "typ": "JWT"}, claims)]
    message = b".".join(parts)
    return (message + b"." + base64.urlsafe_b64encode(
        hmac.new(key, message, hashlib.sha256).digest()).rstrip(b"=")).decode()


# 驗證本人讀取不續期、獨立登入選擇性撤銷，並以同一帳號精確核對 JWT 拒絕無副作用。
def session_cases(nonce):
    for role in ("member", "admin"):
        actor = shared.user(BACKEND, nonce + "s", role)
        login = shared.expect(shared.request(BACKEND, "POST", LOGIN,
            {"email": actor["email"], "password": "Abcd1234"}, "stale"), 200)
        claims = login_claims(login, actor["id"], role)
        before = state(actor["id"])
        shared.check(shared.expect(shared.request(BACKEND, "GET", "/api/me", token=login["access_token"]),
                                   200) == login["user"] and state(actor["id"]) == before,
                     "/me 改變身分／session／原到期時間")
        shared.check(login["access_token"] != actor["token"], "獨立登入重用 JWT")
        shared.expect(shared.request(BACKEND, "POST", "/api/auth/logout", {}, actor["token"]), 200)
        shared.expect(shared.request(BACKEND, "POST", "/api/auth/logout", {}, actor["token"]), 401, "E0002")
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=actor["token"]), 401, "E0002")
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=login["access_token"]), 200)
        shared.check(shared.sql(f"SELECT count(*) FROM login_sessions WHERE account_id={actor['id']} "
                                "AND revoked_at IS NOT NULL") == "1", "登出影響其他登入")
        before = state(actor["id"])
        missing = dict(claims)
        del missing["sub"]
        for token in (signed_token(claims, {"alg": "none", "typ": "JWT"}), signed_token(missing),
                      signed_token({**claims, "iat": str(claims["iat"])}),
                      signed_token({**claims, "aud": "ai-workflow-fullstack-api:aspnet-core"})):
            shared.expect(shared.request(BACKEND, "GET", "/api/me", token=token), 401, "E0002")
        shared.check(state(actor["id"]) == before, "JWT 拒絕留下副作用")
    print("PASS CQ09 會員／管理員八小時、/me 不續期、多登入選擇性登出及 claims 拒絕", flush=True)


# SQL 只調整新建 session 的時間；真實 HTTP 在 DB 秒數前後及相等時驗證 nbf／exp。
def expiry_cases(nonce):
    actor = shared.user(BACKEND, nonce + "t", "member")
    parts = actor["token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    jti = claims["jti"]
    original = (claims["iat"], claims["exp"])
    try:
        for boundary in ("nbf", "exp"):
            target = int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) + 5
            issued = target if boundary == "nbf" else target - 28800
            changed = {**claims, "iat": issued, "nbf": issued, "exp": issued + 28800}
            shared.sql(f"UPDATE login_sessions SET issued_at=to_timestamp({issued}), "
                       f"expires_at=to_timestamp({issued + 28800}) WHERE jti='{jti}' AND account_id={actor['id']}")
            token = signed_token(changed)
            before = state(actor["id"])
            status = 401 if boundary == "nbf" else 200
            shared.expect(shared.request(BACKEND, "GET", "/api/me", token=token), status,
                          "E0002" if status == 401 else None)
            shared.check(int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) < target,
                         "時間 fixture 已越過前側，不能聲稱涵蓋邊界")

            # 輪詢有固定上限；請求前後 DB 均在同一目標秒，才能證明 HTTP 確實命中相等。
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                now = int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"))
                if now >= target:
                    break
                time.sleep(0.05)
            shared.check(now == target, "未命中精確 JWT 邊界秒")
            status = 200 if boundary == "nbf" else 401
            shared.expect(shared.request(BACKEND, "GET", "/api/me", token=token), status,
                          "E0002" if status == 401 else None)
            shared.check(int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) == target,
                         "HTTP 越過 JWT 邊界秒，精確相等證據不足")
            shared.check(state(actor["id"]) == before, "JWT 時間查核改變 session／計數")
    finally:
        shared.sql(f"UPDATE login_sessions SET issued_at=to_timestamp({original[0]}), "
                   f"expires_at=to_timestamp({original[1]}) WHERE jti='{jti}' AND account_id={actor['id']}")
    print("PASS CQ09 真實 HTTP nbf 前／相等與 exp 前／相等拒絕、時間 fixture 已恢復", flush=True)


# 補鎖定資格與錯誤優先順序；恢復雜湊及設定，拒絕時核對完整帳號／session 狀態。
def account_failure_cases(nonce):
    admin = shared.user(BACKEND, nonce + "f", "admin")
    member = shared.user(BACKEND, nonce + "f", "member")
    original = shared.expect(shared.request(BACKEND, "GET", SETTINGS, token=admin["token"]), 200)
    try:
        settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 30}
        shared.expect(shared.request(BACKEND, "PUT", SETTINGS, settings, admin["token"]), 200)
        before = state(admin["id"])
        for _ in range(3):
            shared.expect(shared.request(BACKEND, "POST", LOGIN,
                {"email": admin["email"], "password": "Wrong1234"}), 401, "E0101")
        shared.check(state(admin["id"]) == before, "管理員錯密碼被計數或鎖定")
        shared.expect(shared.request(BACKEND, "POST", LOGIN,
            {"email": f"cq09-{nonce}-missing@example.test", "password": "Abcd1234"}), 401, "E0101")
        wrong = {"email": member["email"], "password": "Wrong1234"}
        shared.expect(shared.request(BACKEND, "POST", LOGIN, wrong), 401, "E0101")
        shared.check(shared.sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}") == "1",
                     "401 未提交失敗事件")
        locked = decoded(raw_request("POST", LOGIN, json.dumps(wrong).encode()))
        shared.retry_remaining(BACKEND, member["id"], locked)
        shared.check(locked[2].get("Access-Control-Allow-Origin") == ORIGIN and
                     "retry-after" in locked[2].get("Access-Control-Expose-Headers", "").lower(),
                     "429 CORS 未開放 Retry-After")
        before = state(member["id"])
        shared.retry_remaining(BACKEND, member["id"], shared.request(BACKEND, "POST", LOGIN, wrong))
        shared.check(state(member["id"]) == before, "鎖定重試延長期限或新增失敗")
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=member["token"]), 200)
        shared.expect(shared.request(BACKEND, "PUT", f"/api/members/{member['id']}/status",
            {"status": "disabled"}, admin["token"]), 200)
        before = state(member["id"])
        shared.expect(shared.request(BACKEND, "POST", LOGIN, wrong), 403, "E0102")
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=member["token"]), 403, "E0102")
        shared.check(state(member["id"]) == before, "停用拒絕改變鎖定或計數")
        shared.expect(shared.request(BACKEND, "PUT", f"/api/members/{member['id']}/status",
            {"status": "active"}, admin["token"]), 200)
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=member["token"]), 401, "E0002")
        shared.retry_remaining(BACKEND, member["id"], shared.request(BACKEND, "POST", LOGIN,
            {"email": member["email"], "password": "Abcd1234"}))

        # 毀損雜湊使用另一個未鎖定帳號，避免既有 429 擋住真正的設定錯誤路徑。
        corrupt = shared.user(BACKEND, nonce + "h", "member")
        # 相同密碼的本次 admin 可在 DB 內提供還原值，避免把 PHC 放進命令列或錯誤輸出。
        restore_hash = ("UPDATE accounts SET password_hash=(SELECT password_hash FROM accounts "
                        f"WHERE id={admin['id']}) WHERE id={corrupt['id']}")
        shared.sql(restore_hash)
        try:
            shared.sql(f"UPDATE accounts SET password_hash='cq09-invalid-phc' WHERE id={corrupt['id']}")
            before = state(corrupt["id"])
            result = shared.request(BACKEND, "POST", LOGIN,
                {"email": corrupt["email"], "password": "Abcd1234"})
            payload = result[1]
            shared.check(result[0] == 500 and set(payload) == {"success", "message", "error_code", "log_id"}
                         and payload["success"] is False and payload["error_code"] == "E0104"
                         and payload["message"] == "後台認證設定異常，請聯絡系統管理員"
                         and str(uuid.UUID(payload["log_id"])) == payload["log_id"], "雜湊異常 envelope 不符")
            shared.check(state(corrupt["id"]) == before and shared.sql(
                f"SELECT password_hash='cq09-invalid-phc' FROM accounts WHERE id={corrupt['id']}") == "t",
                "毀損雜湊被重寫或留下登入／失敗計數")
            shared.trace_errors(BACKEND, [(payload["log_id"], "POST", LOGIN, 500)])
            lines = "\n".join(line for line in shared.api_log_lines(BACKEND) if payload["log_id"] in line)
            shared.check(all(value not in lines for value in (
                "cq09-invalid-phc", "$argon2id$", "Abcd1234", corrupt["token"], corrupt["email"])),
                "認證設定錯誤日誌洩漏敏感值")
        finally:
            shared.sql(restore_hash)
        shared.expect(shared.request(BACKEND, "POST", LOGIN,
            {"email": corrupt["email"], "password": "Abcd1234"}), 200)
    finally:
        shared.expect(shared.request(BACKEND, "PUT", SETTINGS, original, admin["token"]), 200)
        shared.check(shared.expect(shared.request(BACKEND, "GET", SETTINGS, token=admin["token"]), 200) == original,
                     "CQ09 鎖定設定恢復失敗")
    print("PASS CQ09 失敗提交、門檻／CORS、停用優先、管理員免計數與毀損雜湊", flush=True)


# 入口錯誤必須保持 JSON；HEAD／OPTIONS 以原始 bytes 核對空內容與 CORS，不偽造 JSON。
def entry_cases(nonce):
    observed = []
    for method, path, status, code in (("GET", "/api/cq09-missing-" + nonce, 404, "E0004"),
                                       ("DELETE", LOGIN, 405, "E0005")):
        result = decoded(raw_request(method, path))
        shared.expect(result, status, code)
        shared.check(result[2].get("Access-Control-Allow-Origin") == ORIGIN, "錯誤入口缺 CORS")
        observed.append((result[1]["log_id"], method, path, status))
    member = shared.user(BACKEND, nonce + "r", "member")
    for token, status in ((None, 401), (member["token"], 200)):
        result = raw_request("HEAD", "/api/me", token=token)
        shared.check(result[0] == status and result[1] == b"" and
                     result[2].get_content_type() == "application/json", "HEAD 狀態或空內容不符")
    for origin in (ORIGIN, "http://untrusted.example.test"):
        result = raw_request("OPTIONS", LOGIN, origin=origin, extra_headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        shared.check(result[0] == 204 and result[1] == b"" and
                     {"GET", "POST", "PUT", "OPTIONS"} <= set(
                         result[2].get("Access-Control-Allow-Methods", "").replace(" ", "").split(",")) and
                     {"authorization", "content-type", "accept"} <= set(
                         result[2].get("Access-Control-Allow-Headers", "").lower().replace(" ", "").split(",")),
                     "OPTIONS 預檢狀態／允許方法／標頭不符")
        for response in (result, raw_request("GET", "/api/me", token=member["token"], origin=origin)):
            shared.check(response[2].get("Access-Control-Allow-Origin") == (ORIGIN if origin == ORIGIN else None),
                         "CORS origin 允許範圍不符")
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ09 404／405 JSON 與追蹤、HEAD 空內容、OPTIONS／CORS origin", flush=True)


# 直接呼叫未修改的 guard 與現有案例；任一缺漏／失敗都中止，只有全部完成才印整體 PASS。
def main():
    shared.guard()
    shared.clear_boundary_trigger()
    nonce = secrets.token_hex(5)
    entry_cases(nonce)
    registration_cases(nonce)
    input_cases(nonce)
    session_cases(nonce)
    expiry_cases(nonce)
    account_failure_cases(nonce)
    shared.backend_cases(BACKEND, nonce)
    shared.boundary_cases(BACKEND, nonce)
    shared.lockout_cases(BACKEND, nonce)
    shared.lockout_edge_cases(BACKEND, nonce)
    print("PASS CQ09 Symfony 帳號生命週期、22 操作與相關跨後端競爭", flush=True)
    print("範圍限制：僅 Symfony／ASP.NET Core 帳號互通；瀏覽器、效能及其他後端完整矩陣另行驗證")


if __name__ == "__main__":
    main()
