"""CQ18：沿用 Ticket 57 guard，在專用隔離環境驗證 Laravel 公開 HTTP／DB 契約。"""

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote_to_bytes
from urllib.request import Request, urlopen

from cq10_http import create_article, create_comment, row
import ticket57_http as shared


BACKEND = "laravel"
PEER = "symfony"
ORIGIN = "http://localhost:8080"
LOGIN = "/api/auth/login"
SETTINGS = "/api/settings/login-lockout"
RECOVERY = Path(".scratch/code-quality/cq18")
RECOVERIES = {"cq18": RECOVERY, "cq19": Path(".scratch/code-quality/cq19"),
              "cq20": Path(".scratch/code-quality/cq20"), "cq21": Path(".scratch/code-quality/cq21"),
              "cq22": Path(".scratch/code-quality/cq22"), "cq23": Path(".scratch/code-quality/cq23")}
NONCES = {"cq18": r"cq18(?:[0-9a-f]{12}|[0-9a-f]{16})", "cq19": r"cq19[0-9a-f]{8}",
          "cq20": r"cq20[0-9a-f]{8}", "cq21": r"cq21[0-9a-f]{8}", "cq22": r"cq22[0-9a-f]{8}", "cq23": r"cq23[0-9a-f]{8}"}


# 先解析有限模式才觸碰 guard，讓未知參數及錯誤恢復路徑在任何服務操作前停止。
def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", action="store_true", help="只驗證 GET Body／Query 與錯誤 key 回歸")
    modes.add_argument("--restore", type=Path, help="只恢復本票先前留下的 recovery JSON")
    args = parser.parse_args()
    if args.restore is not None:
        # 新 nonce 縮至 12 hex 以符合共用暱稱長度；仍接受先前 16 hex recovery，避免中斷後無法恢復。
        if args.restore.parent != RECOVERY or re.fullmatch(r"recovery-cq18(?:[0-9a-f]{12}|[0-9a-f]{16})\.json", args.restore.name) is None:
            parser.error("--restore 只接受本票固定目錄及 recovery 檔名")
    return args


# 保留原始 bytes、Query 與 HEAD 空內容，避免客戶端替框架改寫測資。
def raw_request(method, path, body=None, token=None, origin=ORIGIN, extra_headers=None, *, backend=BACKEND):
    shared.check(shared.SAFE, "尚未核對隔離 HTTP 目標")
    shared.check(backend in ("laravel", "aspnet-core", "django", "flask"), "未核准的額外 HTTP 後端")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    headers.update(extra_headers or {})
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = Request(shared.BASES[backend] + path, data=body, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), response.headers


# 僅在契約要求 JSON 時解析，不把伺服器原始回應放進錯誤訊息或 artifact。
def decoded(result):
    status, raw, headers = result
    shared.check(headers.get_content_type() == "application/json", "回應不是 JSON")
    return status, json.loads(raw), headers


# 帳號、所有 session、失敗事件與內容只留記憶體，讓拒絕案例能核對完整副作用。
def state(account_id):
    return shared.sql(
        "SELECT jsonb_build_object('account',to_jsonb(a),"
        "'sessions',(SELECT jsonb_agg(s ORDER BY s.jti) FROM login_sessions s WHERE s.account_id=a.id),"
        "'failures',(SELECT jsonb_agg(f ORDER BY f.id) FROM login_failures f WHERE f.account_id=a.id),"
        "'articles',(SELECT jsonb_agg(p ORDER BY p.id) FROM articles p WHERE p.author_id=a.id),"
        "'comments',(SELECT jsonb_agg(c ORDER BY c.id) FROM comments c WHERE c.author_id=a.id))::text "
        f"FROM accounts a WHERE a.id={account_id}"
    )


# 固定物件若已存在就拒絕開始；不把未知來源的 trigger／function 當成本票殘留刪掉。
def clean_boundary():
    shared.check(shared.sql(
        "SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
        "WHERE tgrelid='public.login_failures'::regclass AND tgname='ticket57_http_boundary_shift') "
        "AND to_regprocedure('public.ticket57_http_boundary_shift()') IS NULL"
    ) == "t", "邊界 trigger／function 已存在，須先由 Main 查明來源")
    shared.check(shared.sql(
        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
        "AND pid<>pg_backend_pid() AND state LIKE 'idle in transaction%'"
    ) == "0", "隔離 DB 存在未結束的交易 holder")


# 恢復檔只准包含非秘密的四設定、既有會員鎖定欄位與唯一 marker，避免擴成任意 SQL 輸入。
def validate_packet(packet, *, ticket="cq18"):
    shared.check(ticket in RECOVERIES, "未核准的恢復 namespace")
    shared.check(set(packet) == {"nonce", "settings", "members", "account_times", "session_times", "restored"}, "恢復檔欄位不符")
    shared.check(re.fullmatch(NONCES[ticket], packet["nonce"]) is not None, "恢復 marker 不符")
    settings = packet["settings"]
    shared.check(set(settings) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}
                 and type(settings["enabled"]) is bool, "恢復設定欄位不符")
    for field, maximum in (("window_minutes", 60), ("failure_threshold", 100), ("lock_seconds", 86400)):
        shared.check(type(settings[field]) is int and 1 <= settings[field] <= maximum, "恢復設定範圍不符")
    shared.check(isinstance(packet["members"], list) and type(packet["restored"]) is bool, "恢復資料型態不符")
    identifiers = []
    for member in packet["members"]:
        shared.check(set(member) == {"id", "failure_epoch", "locked_until"}
                     and type(member["id"]) is int and member["id"] > 0
                     and type(member["failure_epoch"]) is int and member["failure_epoch"] >= 0,
                     "恢復會員欄位不符")
        locked = member["locked_until"]
        shared.check(locked is None or isinstance(locked, str), "恢復鎖定時間型態不符")
        if locked is not None:
            shared.check(datetime.fromisoformat(locked).tzinfo is not None, "恢復鎖定時間缺少時區")
        identifiers.append(member["id"])
    shared.check(identifiers == sorted(set(identifiers)), "恢復會員 ID 重複或順序不符")
    # 僅記錄本票時間 fixture 的原值；不保存帳密、雜湊或完整 JWT。
    for name, fields in (("account_times", {"id", "created_at"}),
                         ("session_times", {"id", "jti", "issued_at", "expires_at"})):
        shared.check(isinstance(packet[name], list), "時間恢復資料型態不符")
        for item in packet[name]:
            shared.check(set(item) == fields and type(item["id"]) is int and item["id"] > 0, "時間恢復欄位不符")
            for field in fields - {"id", "jti"}:
                shared.check(isinstance(item[field], str) and datetime.fromisoformat(item[field]).tzinfo is not None,
                             "時間恢復值缺少時區")
            if "jti" in fields:
                shared.check(re.fullmatch(r"[A-Za-z0-9_-]{22}", item["jti"]) is not None, "時間恢復 jti 不符")


# 更新恢復記錄先寫同目錄暫存再原子替換，避免中斷留下被截斷的唯一 baseline。
def save_packet(packet, path):
    temporary = path.with_suffix(".tmp-" + secrets.token_hex(4))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(packet, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# 首次服務寫入前同步落盤 baseline；中斷後不需帳密或 JWT 也能精確恢復既有會員鎖定欄位。
def capture_baseline(nonce, *, ticket="cq18"):
    shared.check(ticket in RECOVERIES and re.fullmatch(NONCES[ticket], nonce) is not None, "恢復 marker 不符")
    recovery = RECOVERIES[ticket]
    clean_boundary()
    recovery.mkdir(parents=True, exist_ok=True)
    for path in recovery.glob(f"recovery-{ticket}*.json"):
        old = json.loads(path.read_text(encoding="utf-8"))
        validate_packet(old, ticket=ticket)
        shared.check(old["restored"], f"上次 {ticket.upper()} 尚未恢復，先用 --restore 核對處理")
    shared.check(shared.sql(f"SELECT count(*) FROM accounts WHERE email LIKE 't57-{nonce}%' ") == "0",
                 "本票 marker 已存在")
    packet = {
        "nonce": nonce,
        "settings": json.loads(shared.sql(
            "SELECT json_build_object('enabled',enabled,'window_minutes',window_minutes,"
            "'failure_threshold',failure_threshold,'lock_seconds',lock_seconds)::text "
            "FROM login_lockout_settings WHERE id=1")),
        "members": json.loads(shared.sql(
            "SELECT coalesce(json_agg(m ORDER BY id),'[]'::json)::text FROM "
            "(SELECT id,failure_epoch,locked_until FROM accounts WHERE role='member') m")),
        "account_times": [],
        "session_times": [],
        "restored": False,
    }
    validate_packet(packet, ticket=ticket)
    path = recovery / f"recovery-{nonce}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(packet, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    print(f"RECOVERY {path}", flush=True)
    return packet, path


# baseline 可能已有數千會員；恢復 SQL 由 stdin 傳入固定隔離 psql，避免單一 argv 的大小上限。
def restore_sql(statement):
    shared.check(shared.SAFE, "尚未核對隔離恢復資料庫")
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
         "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles"],
        input=statement, text=True, capture_output=True, check=True, timeout=30,
    )
    return result.stdout.strip()


# 以同一交易恢復 baseline 並收束本次角色／sessions；有額外 fixture 的呼叫端須完成清理才標記整體恢復。
def restore_baseline(packet, path, *, ticket="cq18", mark_restored=True):
    validate_packet(packet, ticket=ticket)
    clean_boundary()
    nonce, settings = packet["nonce"], packet["settings"]
    members_json = json.dumps(packet["members"], separators=(",", ":")).replace("'", "''")
    member_rows = (
        f"json_to_recordset('{members_json}'::json) "
        "AS m(id bigint,failure_epoch bigint,locked_until timestamptz)"
    )
    shared.check(restore_sql(
        f"SELECT count(*) FROM {member_rows} JOIN accounts a ON a.id=m.id AND a.role='member'"
    ) == str(len(packet["members"])), "既有會員集合／角色已改變，拒絕覆蓋")
    restore_sql(
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='15s'; "
        f"UPDATE login_lockout_settings SET enabled={'true' if settings['enabled'] else 'false'},"
        f"window_minutes={settings['window_minutes']},failure_threshold={settings['failure_threshold']},"
        f"lock_seconds={settings['lock_seconds']} WHERE id=1; "
        # 設定先鎖、帳號依 ID、session 最後，與被測 API 保持相容鎖序。
        f"SELECT a.id FROM accounts a WHERE a.id IN (SELECT m.id FROM {member_rows}) "
        f"OR a.email LIKE 't57-{nonce}%' ORDER BY a.id FOR UPDATE; "
        f"UPDATE accounts a SET failure_epoch=m.failure_epoch,locked_until=m.locked_until FROM {member_rows} WHERE a.id=m.id; "
        f"UPDATE accounts SET role='member' WHERE email LIKE 't57-{nonce}%' AND role='admin'; "
        "UPDATE login_sessions SET revoked_at=coalesce(revoked_at,clock_timestamp()) "
        f"WHERE account_id IN (SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%'); COMMIT;"
    )
    # 時間只恢復已落盤且由本次 marker 擁有的 fixture；中斷後 --restore 也使用相同檢查。
    for item in packet["account_times"]:
        identifier = item["id"]
        timestamp = item["created_at"].replace("'", "''")
        shared.check(shared.sql(
            f"UPDATE accounts SET created_at='{timestamp}'::timestamptz WHERE id={identifier} "
            f"AND email LIKE 't57-{nonce}%' RETURNING id") == str(identifier), "帳號時間 fixture 所有權不符")
        actual = shared.sql(f"SELECT (created_at='{timestamp}'::timestamptz)::text FROM accounts WHERE id={identifier}")
        shared.check(actual == "true", "帳號建立時間未恢復")
    for item in packet["session_times"]:
        identifier, jti = item["id"], item["jti"]
        issued, expires = (item[field].replace("'", "''") for field in ("issued_at", "expires_at"))
        shared.check(shared.sql(
            f"UPDATE login_sessions s SET issued_at='{issued}'::timestamptz,expires_at='{expires}'::timestamptz "
            f"WHERE account_id={identifier} AND jti='{jti}' AND EXISTS (SELECT 1 FROM accounts a "
            f"WHERE a.id=s.account_id AND a.email LIKE 't57-{nonce}%') RETURNING s.jti") == jti,
            "session 時間 fixture 所有權不符")
        shared.check(shared.sql(f"SELECT (issued_at='{issued}'::timestamptz AND expires_at='{expires}'::timestamptz)::text "
                                f"FROM login_sessions WHERE jti='{jti}'") == "true", "session 時間未恢復")
    restored = json.loads(restore_sql(
        "SELECT coalesce(json_agg(m ORDER BY id),'[]'::json)::text FROM "
        "(SELECT id,failure_epoch,locked_until FROM accounts WHERE id IN "
        f"(SELECT m.id FROM {member_rows})) m"))
    shared.check(restored == packet["members"], "既有會員 epoch／locked_until 未精確恢復")
    current = json.loads(shared.sql(
        "SELECT json_build_object('enabled',enabled,'window_minutes',window_minutes,"
        "'failure_threshold',failure_threshold,'lock_seconds',lock_seconds)::text "
        "FROM login_lockout_settings WHERE id=1"))
    shared.check(current == settings, "四設定未精確恢復")
    shared.check(shared.sql(
        f"SELECT count(*) FROM accounts WHERE email LIKE 't57-{nonce}%' AND role='admin'"
    ) == "0" and shared.sql(
        "SELECT count(*) FROM login_sessions s JOIN accounts a ON a.id=s.account_id "
        f"WHERE a.email LIKE 't57-{nonce}%' AND s.revoked_at IS NULL"
    ) == "0", "本票管理員或有效 session 尚未收束")
    clean_boundary()
    packet["restored"] = mark_restored
    save_packet(packet, path)
    print("PASS CQ18 baseline 四設定／會員鎖定欄位恢復，臨時管理員及 sessions 已收束", flush=True)


# 唯一帳號隔離已確認的輸入缺陷；每次拒絕後比較完整快照，避免錯誤優先修正造成寫入。
def regression_cases(nonce, *, backend=BACKEND):
    actor = shared.user(backend, nonce + "r", "member")
    before = state(actor["id"])
    observed = []
    body_error = {"_body": ["此 API 不接受 Request Body"]}
    queries = (("unexpected=1", {"unexpected": ["不支援此欄位"]}),
               ("unexpected=1&unexpected=2", {"unexpected": ["此參數不可重複"]}))
    for query, query_error in queries:
        for body in (b"{}", b"{"):
            for token, status in ((actor["token"], 422), (None, 401), ("invalid", 401)):
                path = "/api/me?" + query
                result = decoded(raw_request("GET", path, body, token, backend=backend))
                shared.expect(result, status, "E0001" if status == 422 else "E0002",
                              body_error if status == 422 else None)
                observed.append((result[1]["log_id"], "GET", "/api/me", status))
                shared.check(state(actor["id"]) == before, "GET Body／Query 拒絕改變帳號或 session")
            control = decoded(raw_request("GET", "/api/articles?page=bad&per_page=20&" + query,
                                          body, actor["token"], backend=backend))
            shared.expect(control, 422, "E0001", body_error)
        for body in (None, b""):
            shared.expect(decoded(raw_request("GET", "/api/me?" + query, body, actor["token"], backend=backend)),
                          422, "E0001", query_error)

    # 未知 Query 一律拒絕；非法 UTF-8 key 僅採已確認的百分比表示，不延伸到搜尋字元政策。
    unicode_key = "未知陳"
    shared.check([ord(char) for char in unicode_key] == [0x672A, 0x77E5, 0x9673]
                 and unquote_to_bytes(quote(unicode_key)) == unicode_key.encode("utf-8"), "UTF-8 key 測資不符")
    endpoints = (
        ("POST", "/api/auth/register", b"{", None),
        ("POST", LOGIN, b"{", None),
        ("GET", "/api/me", None, actor["token"]),
        ("POST", "/api/auth/logout", b"{}", actor["token"]),
        ("GET", "/api/articles?page=1&per_page=20", None, actor["token"]),
    )
    for encoded, key in (("%FF", "%FF"), (quote(unicode_key), unicode_key), ("unexpected", "unexpected")):
        for duplicate in (False, True):
            query = encoded + "=x" + ("&" + encoded + "=y" if duplicate else "")
            fields = {key: ["此參數不可重複" if duplicate else "不支援此欄位"]}
            for method, path, body, token in endpoints:
                full_path = path + ("&" if "?" in path else "?") + query
                result = decoded(raw_request(method, full_path, body, token, backend=backend))
                shared.expect(result, 422, "E0001", fields)
                observed.append((result[1]["log_id"], method, path.split("?", 1)[0], 422))
                shared.check(state(actor["id"]) == before, "未知 key 拒絕改變帳號或 session")

    # decoded 名稱才決定是否重複；非法位元組與字面 %FF 可共用顯示標籤，但不能合併身分。
    mixed_queries = (
        ("%FF=x&%25FF=y", {"%FF": ["不支援此欄位"]}),
        ("%25FF=x&%FF=y", {"%FF": ["不支援此欄位"]}),
        ("%FF=x&%ff=y", {"%FF": ["此參數不可重複"]}),
        ("%25FF=x&%25FF=y", {"%FF": ["此參數不可重複"]}),
        ("%FF=x&%FF=y&%25FF=z", {"%FF": ["此參數不可重複"]}),
        ("%FF=x&%25FF=y&%FF=z", {"%FF": ["此參數不可重複"]}),
        ("%25FF=x&%FF=y&%FF=z", {"%FF": ["此參數不可重複"]}),
        ("%25FF=x&%25FF=y&%FF=z", {"%FF": ["此參數不可重複"]}),
        ("%25FF=x&%FF=y&%25FF=z", {"%FF": ["此參數不可重複"]}),
        ("%FF=x&%25FF=y&%25FF=z", {"%FF": ["此參數不可重複"]}),
        ("%FF=x&%25FF=y&%ff=z&%25FF=w", {"%FF": ["此參數不可重複"]}),
        ("%25FF=x&%FF=y&%25FF=z&%ff=w", {"%FF": ["此參數不可重複"]}),
        ("%ff=x&%25ff=y", {"%FF": ["不支援此欄位"], "%ff": ["不支援此欄位"]}),
    )
    for query, fields in mixed_queries:
        for method, path, body, token in endpoints:
            full_path = path + ("&" if "?" in path else "?") + query
            result = decoded(raw_request(method, full_path, body, token, backend=backend))
            shared.expect(result, 422, "E0001", fields)
            observed.append((result[1]["log_id"], method, path.split("?", 1)[0], 422))
            shared.check(state(actor["id"]) == before, "Query 身分判定拒絕改變帳號或 session")

    # 相鄰公開入口保留已觀察的 UTF-8 錯誤標籤與 page 陣列正規化，不自行增加巢狀名稱政策。
    adjacent_queries = (
        ("page%5B%FF%5D=x", {"page%5B%FF%5D": ["不支援此欄位"]}, {"page%5B%FF%5D": ["不支援此欄位"]}),
        ("filters%5B%FF%5D=x", {"filters%5B%FF%5D": ["不支援此欄位"]}, {"filters%5B%FF%5D": ["不支援此欄位"]}),
        ("page%5B0%5D=x", {"page[0]": ["不支援此欄位"]}, {"page": ["此參數不可重複"]}),
        ("page%5B0%5D=x&page%5B1%5D=y", {"page[0]": ["不支援此欄位"], "page[1]": ["不支援此欄位"]},
         {"page": ["此參數不可重複"]}),
    )
    for query, auth_fields, article_fields in adjacent_queries:
        for method, path, body, token in endpoints:
            if path in ("/api/me", "/api/auth/logout"):
                continue
            full_path = path + ("&" if "?" in path else "?") + query
            result = decoded(raw_request(method, full_path, body, token, backend=backend))
            shared.expect(result, 422, "E0001", article_fields if method == "GET" else auth_fields)
            observed.append((result[1]["log_id"], method, path.split("?", 1)[0], 422))
            shared.check(state(actor["id"]) == before, "相鄰 Query control 拒絕改變帳號或 session")
    shared.check(state(actor["id"]) == before, "回歸 control 改變帳號或 session")
    shared.trace_errors(backend, observed)
    print("PASS CQ18 GET Body／認證優先、Query 身分／JSON 標籤與相鄰 control 回歸", flush=True)


# 原始入口錯誤、HEAD 與 CORS 各驗實際 HTTP，避免把空內容當成 JSON 或聲稱瀏覽器驗證。
def entry_cases(nonce, *, backend=BACKEND):
    observed = []
    for method, path, status, code in (("GET", "/api/cq18-missing-" + nonce, 404, "E0004"),
                                       ("DELETE", LOGIN, 405, "E0005")):
        result = decoded(raw_request(method, path, backend=backend))
        shared.expect(result, status, code)
        shared.check(result[2].get("Access-Control-Allow-Origin") == ORIGIN, "錯誤入口缺 CORS")
        if backend != BACKEND:
            shared.check("retry-after" in result[2].get("Access-Control-Expose-Headers", "").lower().replace(" ", "").split(","),
                         "錯誤入口未 expose Retry-After")
        observed.append((result[1]["log_id"], method, path, status))
    actor = shared.user(backend, nonce + "h", "member")
    before = state(actor["id"])
    head_cases = ((None, 401), (actor["token"], 200)) if backend in (BACKEND, "flask") else ((None, 405), (actor["token"], 405))
    for token, status in head_cases:
        result = raw_request("HEAD", "/api/me", token=token, backend=backend)
        shared.check(result[0] == status and result[1] == b""
                     and result[2].get_content_type() == "application/json", "HEAD 狀態／空內容不符")
    for origin in (ORIGIN, "http://untrusted.example.test"):
        result = raw_request("OPTIONS", LOGIN, origin=origin, extra_headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}, backend=backend)
        # Laravel 保留既有允許清單；其他版本不向非信任 Origin 提供 CORS 許可。
        shared.check(result[0] == 204 and result[1] == b"", "OPTIONS 預檢狀態／空內容不符")
        if backend != BACKEND and origin != ORIGIN:
            shared.check(result[2].get("Access-Control-Allow-Origin") is None, "非信任 Origin 不得取得 CORS 許可")
        else:
            methods = {"GET", "POST", "PUT", "OPTIONS"} if backend == BACKEND else {"GET", "POST", "PUT"}
            # Django／Flask 依個別路由宣告方法，登入預檢不能要求其他路由的寫入方法。
            if backend in ("django", "flask"):
                methods = {"POST", "OPTIONS"}
                shared.check(set(result[2].get("Access-Control-Allow-Methods", "").replace(" ", "").split(","))
                             == methods, "登入預檢不得宣告其他路由方法")
            shared.check(methods <= set(
                result[2].get("Access-Control-Allow-Methods", "").replace(" ", "").split(",")) and
                {"authorization", "content-type", "accept"} <= set(
                    result[2].get("Access-Control-Allow-Headers", "").lower().replace(" ", "").split(",")),
                "OPTIONS 預檢狀態／允許方法／標頭不符")
        for response in (result, raw_request("GET", "/api/me", token=actor["token"], origin=origin, backend=backend)):
            # 單一白名單可固定回 ORIGIN；不信任來源仍不得取得反射值、萬用值或其他來源的許可。
            shared.check(response[2].get("Access-Control-Allow-Origin") in ((ORIGIN,) if origin == ORIGIN else (None, ORIGIN)),
                         "CORS origin 允許範圍不符")
            if backend != BACKEND and origin != ORIGIN:
                shared.check(response[2].get("Access-Control-Allow-Origin") is None,
                             "非信任 Origin 的實際請求不應取得 CORS 許可")
    shared.check(state(actor["id"]) == before, "HEAD／OPTIONS／CORS 查核留下副作用")
    shared.trace_errors(backend, observed)
    print("PASS CQ18 404／405 log_id、HEAD 空內容、OPTIONS／CORS HTTP", flush=True)


# 唯一父文章區分作者預覽、管理員與留言作者例外，防止下架後誤開父文或遺失自己的留言。
def hidden_parent_cases(nonce, *, backend=BACKEND):
    owner = shared.user(backend, nonce + "p", "member")
    other = shared.user(backend, nonce + "p", "other")
    admin = shared.user(backend, nonce + "p", "admin")
    article_id = create_article(backend, owner, f"t57-{nonce}-parent")
    path = f"/api/articles/{article_id}"
    visible = create_comment(backend, other, article_id, f"t57-{nonce}-visible")
    hidden = create_comment(backend, other, article_id, f"t57-{nonce}-hidden")
    admin_comment = create_comment(backend, admin, article_id, f"t57-{nonce}-admin")
    shared.expect(shared.request(backend, "PUT", f"/api/comments/{hidden}/visibility",
                                {"visibility": "hidden", "version": 1}, other["token"]), 200)
    shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                {"visibility": "hidden", "version": 1}, owner["token"]), 200)
    parent_before = row("articles", article_id)
    fields = {"id", "body", "author", "article_id", "version", "created_backend",
              "created_at", "updated_at", "visibility", "can_restore"}

    # 父文章作者預覽僅 visible；管理員可見全部，非父文作者仍無法讀父文與其列表。
    for actor, expected in ((owner, [admin_comment, visible]), (admin, [admin_comment, hidden, visible])):
        data = shared.expect(shared.request(backend, "GET", path + "/comments?page=1&per_page=100",
                                            token=actor["token"]), 200)
        shared.check([item["id"] for item in data["list"]] == expected and data["pagination"] == {
            "page": 1, "per_page": 100, "total": len(expected), "total_pages": 1}, "父文預覽留言範圍不符")
    for token in (None, other["token"]):
        for suffix in ("", "/comments?page=1&per_page=100"):
            shared.expect(shared.request(backend, "GET", path + suffix, token=token), 404, "E0004")
    mine = shared.expect(shared.request(backend, "GET", "/api/me/comments?page=1&per_page=100",
                                       token=other["token"]), 200)
    shared.check([item["id"] for item in mine["list"]] == [hidden, visible]
                 and mine["pagination"] == {"page": 1, "per_page": 100, "total": 2, "total_pages": 1},
                 "父文隱藏後我的 visible／hidden 留言集合不符")
    for item in mine["list"]:
        detail = shared.expect(shared.request(backend, "GET", f"/api/comments/{item['id']}", token=other["token"]), 200)
        shared.check(detail == item and set(detail) == fields and detail["article_id"] == article_id
                     and set(detail["author"]) == {"id", "nickname"}
                     and detail["visibility"] == ("hidden" if item["id"] == hidden else "visible"),
                     "自己的留言詳情遺失、洩漏父文或作者私密欄位")

    # 可讀父文者仍不得新增；自己的留言也不得改內文，包括管理員自己留言，拒絕後整列不變。
    for actor, status, code in ((owner, 403, "E0003"), (admin, 403, "E0003"), (other, 404, "E0004")):
        before = state(actor["id"])
        shared.expect(shared.request(backend, "POST", path + "/comments", {"body": "下架不得新增"}, actor["token"]),
                      status, code)
        shared.check(state(actor["id"]) == before, "下架父文新增留言留下部分寫入")
    for identifier, actor in ((visible, other), (hidden, other), (admin_comment, admin)):
        before = row("comments", identifier)
        shared.expect(shared.request(backend, "PUT", f"/api/comments/{identifier}",
            {"body": "下架不得修改", "version": before["version"]}, actor["token"]), 403, "E0003")
        shared.check(row("comments", identifier) == before, "下架父文修改留言留下部分寫入")
    shared.expect(shared.request(backend, "PUT", f"/api/comments/{hidden}/visibility",
        {"visibility": "visible", "version": 2}, other["token"]), 200)
    shared.check(row("comments", hidden)["visibility"] == "visible" and row("articles", article_id) == parent_before,
                 "合法恢復留言意外恢復父文章")
    shared.expect(shared.request(backend, "GET", path, token=other["token"]), 404, "E0004")
    print("PASS CQ18 下架父文預覽／自己的兩種留言、禁止新增改文與獨立可見性恢復", flush=True)


# 三會員與一管理員共用唯一搜尋 marker；同時間 fixture 精確證明 ID 降冪、分頁及 admin 排除。
def member_list_cases(nonce, packet, recovery_path, *, backend=BACKEND):
    marker = nonce + "list"
    admin = shared.user(backend, marker, "admin")
    members = [shared.user(backend, marker, kind) for kind in ("a", "b", "c")]
    shared.expect(shared.request(backend, "PUT", f"/api/members/{members[1]['id']}/status",
                                {"status": "disabled"}, admin["token"]), 200)
    identifiers = sorted((member["id"] for member in members), reverse=True)
    ids = ",".join(map(str, identifiers))
    times = json.loads(shared.sql(
        f"SELECT json_agg(m ORDER BY id)::text FROM (SELECT id,created_at FROM accounts WHERE id IN ({ids})) m"))
    packet["account_times"].extend(times)
    save_packet(packet, recovery_path)
    try:
        shared.sql(f"UPDATE accounts SET created_at='2026-01-01 00:00:00+08' WHERE id IN ({ids})")
        for page, per_page in ((1, 1), (1, 2), (2, 2), (3, 2), (1, 100), (2, 100)):
            data = shared.expect(shared.request(backend, "GET",
                f"/api/members?page={page}&per_page={per_page}&filters%5Bkeyword%5D={marker}", token=admin["token"]), 200)
            expected = identifiers[(page - 1) * per_page:page * per_page]
            shared.check([item["id"] for item in data["list"]] == expected and data["pagination"] == {
                "page": page, "per_page": per_page, "total": 3, "total_pages": (3 + per_page - 1) // per_page},
                "會員同時間排序／精確集合／分頁不符")
            for item in data["list"]:
                shared.check(set(item) == {"id", "email", "nickname", "role", "status", "created_at"}
                             and item["role"] == "member" and item["id"] != admin["id"]
                             and item["created_at"] == "2026-01-01 00:00:00"
                             and item["status"] == ("disabled" if item["id"] == members[1]["id"] else "active"),
                             "會員欄位／停用會員包含／管理員排除不符")
    finally:
        for item in times:
            timestamp = item["created_at"].replace("'", "''")
            shared.sql(f"UPDATE accounts SET created_at='{timestamp}'::timestamptz WHERE id={item['id']}")
        shared.check(json.loads(shared.sql(
            f"SELECT json_agg(m ORDER BY id)::text FROM (SELECT id,created_at FROM accounts WHERE id IN ({ids})) m")) == times,
            "三會員建立時間未精確恢復")
    print("PASS CQ18 三會員同時間 ID 降冪、active／disabled、admin 排除與每頁 1／100／超頁", flush=True)


# 登入 claims 與 session 必須同為八小時；只解碼記憶體中的 JWT，不輸出憑證。
def login_claims(login, account_id, role, *, backend=BACKEND):
    parts = login["access_token"].split(".")
    shared.check(len(parts) == 3, "JWT 區段數不符")
    header, claims = [json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts[:2]]
    shared.check(header == {"alg": "HS256", "typ": "JWT"}
                 and set(claims) == {"iss", "aud", "sub", "jti", "iat", "nbf", "exp"}
                 and claims["iss"] == f"ai-workflow-fullstack:{backend}"
                 and claims["aud"] == f"ai-workflow-fullstack-api:{backend}" and claims["sub"] == str(account_id)
                 and all(type(claims[field]) is int for field in ("iat", "nbf", "exp"))
                 and claims["nbf"] == claims["iat"] and claims["exp"] - claims["iat"] == 28800,
                 "JWT header／claims／八小時期限不符")
    shared.check(re.fullmatch(r"[A-Za-z0-9_-]{22}", claims["jti"]) is not None
                 and len(base64.urlsafe_b64decode(claims["jti"] + "==")) == 16
                 and login["token_type"] == "Bearer" and login["user"]["id"] == account_id
                 and login["user"]["role"] == role and login["user"]["status"] == "active"
                 and login["expires_at"] == datetime.fromtimestamp(
                     claims["exp"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                 "登入資料或到期時間不符")
    shared.check(shared.sql(
        "SELECT account_id||'|'||backend_id||'|'||extract(epoch FROM issued_at)::bigint||'|'||"
        "extract(epoch FROM expires_at)::bigint||'|'||(revoked_at IS NULL)::text "
        f"FROM login_sessions WHERE jti='{claims['jti']}'") ==
        f"{account_id}|{backend}|{claims['iat']}|{claims['exp']}|true", "JWT 與 session 身分／時間不符")
    return claims


# 僅在原 guard 通過後讀對應後端已核對的 key，簽署 fixture；key、JWT 不落盤或印出。
def signed_token(claims, header=None, *, backend=BACKEND):
    shared.check(shared.SAFE, "尚未核對隔離金鑰")
    shared.check(backend in ("laravel", "aspnet-core", "django", "flask"), "未核准的簽署 fixture 後端")
    key = base64.b64decode(Path(f".local/keys/{backend}.key").read_bytes().rstrip(b"\n"), validate=True)
    shared.check(len(key) == 32, "隔離金鑰長度不符")
    parts = [base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
             for value in (header or {"alg": "HS256", "typ": "JWT"}, claims)]
    message = b".".join(parts)
    return (message + b"." + base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).rstrip(b"=")).decode()


# 兩次真登入驗證不續期與選擇性撤銷；錯誤 claims 及跨後端 token 拒絕也必須無副作用。
def session_cases(nonce, *, backend=BACKEND):
    peer = PEER if backend == BACKEND else shared.BACKENDS[(shared.BACKENDS.index(backend) + 1) % len(shared.BACKENDS)]
    for role in ("member", "admin"):
        actor = shared.user(backend, nonce + "s", role)
        login = shared.expect(shared.request(backend, "POST", LOGIN,
            {"email": actor["email"], "password": "Abcd1234"}, "stale"), 200)
        claims = login_claims(login, actor["id"], role, backend=backend)
        before = state(actor["id"])
        shared.check(shared.expect(shared.request(backend, "GET", "/api/me", token=login["access_token"]), 200)
                     == login["user"] and state(actor["id"]) == before, "/me 續期或改變帳號／session")
        shared.check(login["access_token"] != actor["token"], "獨立登入重用 JWT")
        shared.expect(shared.request(backend, "POST", "/api/auth/logout", {}, actor["token"]), 200)
        shared.expect(shared.request(backend, "POST", "/api/auth/logout", {}, actor["token"]), 401, "E0002")
        shared.expect(shared.request(backend, "GET", "/api/me", token=actor["token"]), 401, "E0002")
        shared.expect(shared.request(backend, "GET", "/api/me", token=login["access_token"]), 200)
        shared.check(shared.sql(f"SELECT count(*) FROM login_sessions WHERE account_id={actor['id']} "
                                "AND revoked_at IS NOT NULL") == "1", "登出影響其他登入")
        peer_token = shared.expect(shared.request(peer, "POST", LOGIN,
            {"email": actor["email"], "password": "Abcd1234"}), 200)["access_token"]
        before = state(actor["id"])
        missing = dict(claims)
        del missing["sub"]
        for token in (peer_token, signed_token(claims, {"alg": "none", "typ": "JWT"}, backend=backend), signed_token(missing, backend=backend),
                      signed_token({**claims, "iat": str(claims["iat"])}, backend=backend),
                      signed_token({**claims, "aud": f"ai-workflow-fullstack-api:{peer}"}, backend=backend)):
            shared.expect(shared.request(backend, "GET", "/api/me", token=token), 401, "E0002")
        shared.check(state(actor["id"]) == before, "JWT 拒絕留下副作用")
    print("PASS CQ18 八小時 claims／session、/me 不續期、獨立登入撤銷與 JWT 拒絕", flush=True)


# 只改新 fixture session 時間；以 DB 前後讀數包住真 HTTP，精確證明 nbf／exp 的前側與相等。
def expiry_cases(nonce, packet, path, *, backend=BACKEND):
    actor = shared.user(backend, nonce + "t", "member")
    claims = json.loads(base64.urlsafe_b64decode(actor["token"].split(".")[1] + "=="))
    jti = claims["jti"]
    shared.check(re.fullmatch(r"[A-Za-z0-9_-]{22}", jti) is not None, "時間 fixture jti 不符")
    original = (claims["iat"], claims["exp"])
    packet["session_times"].append(json.loads(shared.sql(
        "SELECT json_build_object('id',account_id,'jti',jti,'issued_at',issued_at,'expires_at',expires_at)::text "
        f"FROM login_sessions WHERE jti='{jti}' AND account_id={actor['id']}")))
    save_packet(packet, path)
    try:
        for boundary in ("nbf", "exp"):
            target = int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) + 5
            issued = target if boundary == "nbf" else target - 28800
            changed = {**claims, "iat": issued, "nbf": issued, "exp": issued + 28800}
            shared.sql(f"UPDATE login_sessions SET issued_at=to_timestamp({issued}),expires_at=to_timestamp({issued + 28800}) "
                       f"WHERE jti='{jti}' AND account_id={actor['id']}")
            token, before = signed_token(changed, backend=backend), state(actor["id"])
            shared.check(int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) < target,
                         "JWT 前側 fixture 已越界")
            status = 401 if boundary == "nbf" else 200
            shared.expect(shared.request(backend, "GET", "/api/me", token=token), status,
                          "E0002" if status == 401 else None)
            shared.check(int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) < target,
                         "JWT 前側 HTTP 已越界")

            # 固定上限等待真實秒數；任何跨秒都失敗，不能用重試掩蓋精確邊界證據不足。
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                now = int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"))
                if now >= target:
                    break
                time.sleep(0.05)
            shared.check(now == target, "未命中精確 JWT 邊界秒")
            status = 200 if boundary == "nbf" else 401
            shared.expect(shared.request(backend, "GET", "/api/me", token=token), status,
                          "E0002" if status == 401 else None)
            shared.check(int(shared.sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) == target,
                         "HTTP 越過 JWT 邊界秒，不能聲稱涵蓋相等")
            shared.check(state(actor["id"]) == before, "JWT 時間查核改變帳號／session")
    finally:
        shared.sql(f"UPDATE login_sessions SET issued_at=to_timestamp({original[0]}),expires_at=to_timestamp({original[1]}) "
                   f"WHERE jti='{jti}' AND account_id={actor['id']}")
    print("PASS CQ18 真 HTTP nbf 前拒絕／相等接受、exp 前接受／相等拒絕，時間已恢復", flush=True)


# DB outage 必須是真實 500/E9999；獨立核對共同 envelope，避免修改 shared 的常數或 guard。
def unavailable_error(result):
    status, payload, headers = result
    shared.check(status == 500 and set(payload) == {"success", "message", "error_code", "log_id"}
                 and payload["success"] is False and payload["error_code"] == "E9999"
                 and payload["message"] == "系統暫時無法處理，請稍後再試"
                 and str(uuid.UUID(payload["log_id"])) == payload["log_id"]
                 and headers.get("Access-Control-Allow-Origin") == ORIGIN, "DB 不可用未回共同安全錯誤")


# 僅操作 guard 已確認的專用 db 服務，命令有上限且不用 shell，避免擴及日常 demo。
def database_command(*args):
    shared.check(shared.SAFE, "尚未核對隔離 DB 服務")
    subprocess.run(["docker", "compose", "--profile", "*", *args], check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# 只輪詢既有容器健康，固定上限後失敗；不把連線 timeout 或啟動命令成功當成恢復證據。
def wait_ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            for service in ("db", *shared.BACKENDS):
                shared.container(service)
            shared.guard()
            return
        except (AssertionError, subprocess.CalledProcessError, URLError, TimeoutError):
            time.sleep(0.5)
    raise AssertionError("隔離 DB／後端未於期限內恢復健康")


# 真停專用 DB 後讀寫皆不得放行；finally 啟回並重跑 guard，再用同 token 驗證恢復與零部分寫入。
def unavailable_cases(nonce, *, backend=BACKEND):
    actor = shared.user(backend, nonce + "u", "member")
    before = state(actor["id"])
    title = f"t57-{nonce}-{backend}-unavailable"
    observed = []
    shared.container("db")
    try:
        database_command("stop", "-t", "5", "db")
        for method, path, body in (("GET", "/api/me", None),
                                   ("POST", LOGIN, json.dumps({"email": actor["email"], "password": "Abcd1234"}).encode()),
                                   ("POST", "/api/articles", json.dumps({"title": title, "body": "不得部分寫入"}).encode())):
            result = decoded(raw_request(method, path, body, actor["token"], backend=backend))
            unavailable_error(result)
            observed.append((result[1]["log_id"], method, path, 500))
    finally:
        # 只恢復原 DB 容器，不執行 SQL 相依服務、重建容器或清除資料卷。
        database_command("up", "-d", "--no-deps", "--no-recreate", "db")
        wait_ready()
    shared.check(state(actor["id"]) == before and shared.sql(
        f"SELECT count(*) FROM articles WHERE title='{title}'") == "0", "DB 不可用留下部分 session／內容寫入")
    shared.check(shared.expect(shared.request(backend, "GET", "/api/me", token=actor["token"]), 200)["id"] == actor["id"],
                 "DB 恢復後同 token 無法繼續使用")
    shared.trace_errors(backend, observed)
    print("PASS CQ18 真 DB 不可用 500/E9999、讀寫拒絕與同 token 恢復", flush=True)


# 指定後端建立的文章／留言仍由十二後端讀回，保留 created_backend 與內容差異的精確契約。
def readback(records, *, backend=BACKEND):
    article_id, comment_id = records
    for reader in shared.BACKENDS:
        article = shared.expect(shared.request(reader, "GET", f"/api/articles/{article_id}"), 200)
        comments = shared.expect(shared.request(reader, "GET", f"/api/articles/{article_id}/comments?page=1&per_page=100"), 200)
        comment = next((item for item in comments["list"] if item["id"] == comment_id), None)
        shared.check(article["created_backend"] == backend and article["id"] == article_id
                     and article["body"] == "更新後內文" and comment is not None and comment["created_backend"] == backend
                     and comment["id"] == comment_id and comment["body"] == "更新後留言", "十二後端讀回來源／內容不符")
    print("PASS CQ18 Laravel 建立資料由十二後端精確讀回" if backend == BACKEND
          else f"PASS {backend} 建立資料由十二後端精確讀回", flush=True)


# 保留原 shared guard 與全部必要案例；整體 PASS 只在 full suite 及 finally 恢復均完成後輸出。
def main():
    args = arguments()
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        validate_packet(packet)
        shared.check(args.restore.name == f"recovery-{packet['nonce']}.json", "恢復檔名與 marker 不符")
        shared.check(not packet["restored"], "此恢復檔已完成，拒絕再次覆蓋 baseline")
    shared.guard()
    if packet is not None:
        restore_baseline(packet, args.restore)
        return
    # 共用搜尋暱稱會再加七碼前綴及 laravel；root 保留 16 碼，合計恰為暱稱上限 30 碼。
    nonce = "cq18" + secrets.token_hex(6)
    packet, path = capture_baseline(nonce)
    try:
        print("RUN CQ18 regression", flush=True)
        regression_cases(nonce)
        if not args.regression:
            for case in (entry_cases, session_cases, hidden_parent_cases):
                print(f"RUN CQ18 {case.__name__}", flush=True)
                case(nonce)
            print("RUN CQ18 expiry_cases", flush=True)
            expiry_cases(nonce, packet, path)
            print("RUN CQ18 member_list_cases", flush=True)
            member_list_cases(nonce, packet, path)
            print("RUN CQ18 backend_cases", flush=True)
            records = shared.backend_cases(BACKEND, nonce)
            for case in (shared.contract_cases, shared.boundary_cases, shared.lockout_cases,
                         shared.lockout_edge_cases, shared.database_cases):
                print(f"RUN CQ18 {case.__name__}", flush=True)
                case(BACKEND, nonce)
            print("RUN CQ18 extended_invalid_cases", flush=True)
            counts = shared.extended_invalid_cases(BACKEND, nonce, {})
            shared.check(len(counts) == 284, "既有 284 無效輸入案例缺漏")
            print("RUN CQ18 ordered_cases", flush=True)
            shared.ordered_cases(BACKEND, nonce)
            readback(records)
            print("RUN CQ18 unavailable_cases", flush=True)
            unavailable_cases(nonce)
    finally:
        restore_baseline(packet, path)
    print("PASS CQ18 regression" if args.regression else "PASS CQ18 Laravel 公開 HTTP／DB full suite", flush=True)
    print("範圍限制：未驗瀏覽器、未決搜尋 U+0000 政策與其他十一後端完整矩陣", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # 保留可定位的 assertion prefix；shared 的 actual／expected payload 可能含 JWT，須截掉。
        detail = str(error).split("actual=", 1)[0].split("expected=", 1)[0][:300] if isinstance(error, AssertionError) else ""
        print(f"FAIL CQ18 {type(error).__name__}: {detail}；核對 RUN 階段與 recovery 檔", file=sys.stderr)
        sys.exit(1)
