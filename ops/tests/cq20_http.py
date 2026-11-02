"""CQ20：沿用 Ticket 57 guard，以公開 HTTP／DB 驗證 Django 帳號與管理契約。"""

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from cq10_http import wait_line
import cq18_http as extra
import ticket57_http as shared


BACKEND = "django"
PEER = "flask"
TICKET = "cq20"
RECOVERY = extra.RECOVERIES[TICKET]
PARTS = ("query", "integer", "retry", "account")
FIELDS = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
LIMITS = {"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400}
RANGE = "欄位格式或範圍不正確"
TYPE = "欄位型態不正確"
NULL = "此欄位不可為 null"
UNKNOWN = "不支援此欄位"
DUPLICATE = "此參數不可重複"
DELAY = "cq20_http_login_delay"


# CLI 只接受固定案例與本票恢復路徑，讓錯誤參數在 guard／服務操作前停止。
def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", nargs="?", const="all", choices=("all", *PARTS),
                       help="只執行全部或指定公開回歸，不執行 full suite")
    modes.add_argument("--restore", type=Path, help="只恢復本票未完成的 recovery JSON")
    args = parser.parse_args()
    if args.restore is not None and (
        args.restore.parent != RECOVERY
        or re.fullmatch(r"recovery-cq20[0-9a-f]{8}\.json", args.restore.name) is None
    ):
        parser.error("--restore 只接受本票固定目錄及 recovery 檔名")
    return args


# 先核對 nonce 與共用暱稱的實際長度，避免合法驗證被測資本身的超界遮蔽。
def fixture_names(nonce):
    shared.check(re.fullmatch(extra.NONCES[TICKET], nonce) is not None, "CQ20 nonce 不符")
    shared.check(all(len(prefix + nonce + BACKEND) <= 30 for prefix in ("Aa%_\\Ä陳", "aa%_\\ä陳", "AaXY\\Ä陳")),
                 "共用搜尋暱稱測資超界")
    shared.check(PEER == shared.BACKENDS[(shared.BACKENDS.index(BACKEND) + 1) % len(shared.BACKENDS)],
                 "既有 shared peer 方向已改變，須先確認覆蓋")


# 共用同一隔離 DB 的前票若尚未恢復，拒絕疊加設定與會員鎖定變更。
def pending_recovery():
    for ticket, folder in extra.RECOVERIES.items():
        for path in folder.glob(f"recovery-{ticket}*.json"):
            packet = json.loads(path.read_text(encoding="utf-8"))
            extra.validate_packet(packet, ticket=ticket)
            shared.check(path.name == f"recovery-{packet['nonce']}.json" and packet["restored"],
                         "既有 recovery 未恢復或檔名不符，拒絕新測資")


# 帳號經公開註冊／登入建立，只有新測資管理員由既有隔離 helper 在 DB 提升角色。
def fixtures(nonce):
    return {"nonce": nonce, "member": shared.user(BACKEND, nonce + "q", "member"),
            "admin": shared.user(BACKEND, nonce + "q", "admin")}


# 完整讀齊本票帳號、sessions、失敗與內容及四設定，只留記憶體以核對拒絕後零副作用。
def snapshot(nonce):
    fixture_names(nonce)
    ids = f"SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%'"
    return shared.sql(
        "SELECT jsonb_build_object('settings',(SELECT to_jsonb(s) FROM login_lockout_settings s WHERE id=1),"
        f"'accounts',(SELECT jsonb_agg(a ORDER BY a.id) FROM accounts a WHERE a.id IN ({ids})),"
        f"'sessions',(SELECT jsonb_agg(s ORDER BY s.jti) FROM login_sessions s WHERE s.account_id IN ({ids})),"
        f"'failures',(SELECT jsonb_agg(f ORDER BY f.id) FROM login_failures f WHERE f.account_id IN ({ids})),"
        f"'articles',(SELECT jsonb_agg(a ORDER BY a.id) FROM articles a WHERE a.author_id IN ({ids})),"
        f"'comments',(SELECT jsonb_agg(c ORDER BY c.id) FROM comments c WHERE c.author_id IN ({ids})))::text"
    )


# 每次拒絕都核對原始 bytes、精確錯誤與完整狀態；E0104 不改 shared 的固定錯誤表。
def rejected(fixture, observed, method, path, body, token, status, code, fields=None):
    before = snapshot(fixture["nonce"])
    result = extra.decoded(extra.raw_request(method, path, body, token, backend=BACKEND))
    if code == "E0104":
        payload = result[1]
        shared.check(result[0] == 500 and set(payload) == {"success", "message", "error_code", "log_id"}
                     and payload["success"] is False and payload["error_code"] == "E0104"
                     and payload["message"] == "後台認證設定異常，請聯絡系統管理員"
                     and str(uuid.UUID(payload["log_id"])) == payload["log_id"], "認證設定異常未回精確安全錯誤")
    else:
        shared.expect(result, status, code, fields)
    shared.check(result[2].get("Access-Control-Allow-Origin") == extra.ORIGIN, "拒絕回應缺 CORS")
    observed.append((result[1]["log_id"], method, path.split("?", 1)[0], status))
    shared.check(snapshot(fixture["nonce"]) == before, "拒絕請求改變本票資料或四設定")
    return result


# API10 判斷同一參數的身分，API15 才決定 errors 名稱；不同原始鍵撞名不能誤判重複。
def query_cases(fixture):
    token, observed = fixture["member"]["token"], []
    cases = (
        ("%FF=x&%25FF=y", {"%FF": [UNKNOWN]}),
        ("%25FF=x&%FF=y", {"%FF": [UNKNOWN]}),
        ("%FF=x&%ff=y", {"%FF": [DUPLICATE]}),
        ("%25FF=x&%25FF=y", {"%FF": [DUPLICATE]}),
        ("%FF=x&%FF=y&%25FF=z", {"%FF": [DUPLICATE]}),
        ("%FF=x&%25FF=y&%25FF=z", {"%FF": [DUPLICATE]}),
        ("%25FF=x&%FF=y&%25FF=z", {"%FF": [DUPLICATE]}),
        ("%ff=x&%25ff=y", {"%FF": [UNKNOWN], "%ff": [UNKNOWN]}),
        ("unexpected=x&%75nexpected=y", {"unexpected": [DUPLICATE]}),
        # 合法 U+FFFD 就是參數名稱，不可因字形像解碼替代字元而改成原始百分比編碼。
        ("%EF%BF%BD=x", {"\ufffd": [UNKNOWN]}),
        ("%EF%BF%BD=x&%ef%bf%bd=y", {"\ufffd": [DUPLICATE]}),
        ("%EF%BF%BD=x&%FF=y", {"\ufffd": [UNKNOWN], "%FF": [UNKNOWN]}),
    )
    endpoints = (("POST", "/api/auth/register", b"{", None),
                 ("POST", extra.LOGIN, b"{", None),
                 ("GET", "/api/me", None, token),
                 ("POST", "/api/auth/logout", b"{}", token),
                 ("GET", "/api/articles?page=1&per_page=20", None, token))
    for query, fields in cases:
        for method, path, body, credential in endpoints:
            path += ("&" if "?" in path else "?") + query
            rejected(fixture, observed, method, path, body, credential, 422, "E0001", fields)
    # 同一合法鍵的百分比別名仍是重複；管理角色與認證必須先於 Query 驗證。
    rejected(fixture, observed, "GET", "/api/articles?pa%67e=1&page=2&per_page=20", None,
             token, 422, "E0001", {"page": [DUPLICATE]})
    rejected(fixture, observed, "GET", "/api/me?%FF=x&%25FF=y", None, "invalid", 401, "E0002")
    rejected(fixture, observed, "GET", "/api/members?page=bad&%FF=x&%25FF=y", None,
             token, 403, "E0003")
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ20 Query 身分／標籤撞名、真重複與門禁優先", flush=True)


# 原始整數 lexeme 直接替換唯一字串占位，避免客戶端把小數或指數改寫成整數。
def integer_body(body, field, lexeme):
    marker = "__CQ20_INTEGER__"
    raw = json.dumps({**body, field: marker}, separators=(",", ":")).encode()
    placeholder = json.dumps(marker).encode()
    shared.check(raw.count(placeholder) == 1, "整數 fixture 占位不唯一")
    return raw.replace(placeholder, lexeme.encode("ascii"))


# NEXT04 的整數值超出業務範圍仍屬範圍錯誤；逐欄保留原始型態與 null 彙整證據。
def integer_cases(fixture):
    admin, observed = fixture["admin"]["token"], []
    original = shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200)
    for field, maximum in LIMITS.items():
        cases = (("9223372036854775808", RANGE), ("-9223372036854775809", RANGE),
                 ("9" * 20, RANGE), ("-" + "9" * 20, RANGE), ("9" * 4301, RANGE),
                 ("0", RANGE), (str(maximum + 1), RANGE),
                 ('"1"', TYPE), ("true", TYPE), ("1.0", TYPE), ("1e0", TYPE))
        for lexeme, message in cases:
            rejected(fixture, observed, "PUT", extra.SETTINGS, integer_body(original, field, lexeme),
                     admin, 422, "E0001", {field: [message]})
        nulls = dict.fromkeys(FIELDS)
        errors = {name: [RANGE if name == field else NULL] for name in FIELDS}
        rejected(fixture, observed, "PUT", extra.SETTINGS,
                 integer_body(nulls, field, "9223372036854775808"), admin, 422, "E0001", errors)
        shared.trace_errors(BACKEND, observed)
        observed.clear()
    # 正常 duplicate key 仍採最後值，不把數值分類修正擴大為重複鍵拒絕政策。
    before = snapshot(fixture["nonce"])
    raw = b'{"enabled":null,' + json.dumps(original, separators=(",", ":")).encode()[1:]
    shared.expect(extra.decoded(extra.raw_request("PUT", extra.SETTINGS, raw, admin, backend=BACKEND)), 200)
    shared.check(snapshot(fixture["nonce"]) == before, "正常 duplicate 最後值或同值更新語意改變")
    # 合法上下限逐一寫入並完整讀回四欄；finally 不依測試是否成功都恢復原設定。
    try:
        for field, maximum in LIMITS.items():
            for value in (1, maximum):
                expected = {**original, field: value}
                shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, expected, admin), 200)
                shared.check(shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200)
                             == expected, "合法設定界線未完整讀回")
    finally:
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, original, admin), 200)
    print("PASS CQ20 設定整數 range／lexical type、null 彙整與合法界線", flush=True)


# 固定名稱物件若已存在就拒絕新 run，避免把未知來源的 DDL 當成自己的測資。
def delay_absent():
    shared.check(shared.sql(
        "SELECT to_regprocedure('public.cq20_http_login_delay()') IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM pg_trigger WHERE tgrelid='public.login_failures'::regclass "
        "AND tgname='cq20_http_login_delay')"
    ) == "t", "CQ20 延遲物件已存在，須先核對其 recovery 所有權")


# 固定三秒只延遲此新帳號的 COMMIT，讓 Retry-After 必須扣除提交等待而非回完整設定秒數。
def delay_source(account_id):
    shared.check(type(account_id) is int and account_id > 0, "延遲帳號 ID 不符")
    return f"BEGIN IF NEW.account_id = {account_id} THEN PERFORM pg_sleep(3); END IF; RETURN NEW; END;"


# 建立與註記在同一交易；先以 SQL 核對精確 nonce email，避免延遲既有帳號或其他登入。
def install_delay(nonce, account_id):
    fixture_names(nonce)
    delay_absent()
    shared.check(shared.sql(f"SELECT email='t57-{nonce}r-{BACKEND}-retry@example.test' "
                            f"FROM accounts WHERE id={account_id}") == "t", "延遲 fixture 所有權不符")
    marker = f"{nonce}:{account_id}"
    shared.sql(
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='10s'; "
        f"CREATE FUNCTION public.{DELAY}() RETURNS trigger LANGUAGE plpgsql "
        f"SET search_path=pg_catalog,public AS $$ {delay_source(account_id)} $$; "
        f"COMMENT ON FUNCTION public.{DELAY}() IS '{marker}'; "
        f"CREATE CONSTRAINT TRIGGER {DELAY} AFTER INSERT ON public.login_failures "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.{DELAY}(); "
        f"COMMENT ON TRIGGER {DELAY} ON public.login_failures IS '{marker}'; COMMIT;"
    )


# finally／--restore 只刪完整所有權與內容皆吻合的延遲物件；未知或部分物件保留並報錯。
def cleanup_delay(nonce):
    fixture_names(nonce)
    metadata = json.loads(shared.sql(
        "SELECT json_build_object('function',(SELECT json_build_object('marker',obj_description(p.oid,'pg_proc'),"
        "'source',btrim(p.prosrc),'owner',pg_get_userbyid(p.proowner),'safe',NOT p.prosecdef "
        "AND p.prorettype='trigger'::regtype AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')) "
        "FROM pg_proc p WHERE p.oid=to_regprocedure('public.cq20_http_login_delay()')),"
        "'trigger',(SELECT json_build_object('marker',obj_description(t.oid,'pg_trigger'),'safe',"
        "t.tgdeferrable AND t.tginitdeferred AND NOT t.tgisinternal AND t.tgtype=5 "
        "AND t.tgfoid=to_regprocedure('public.cq20_http_login_delay()')) FROM pg_trigger t "
        "WHERE t.tgrelid='public.login_failures'::regclass AND t.tgname='cq20_http_login_delay'))"
    ))
    function, trigger = metadata["function"], metadata["trigger"]
    if function is None and trigger is None:
        return
    shared.check(function is not None and trigger is not None, "延遲物件不完整，拒絕刪除未知物件")
    match = re.fullmatch(re.escape(nonce) + r":([1-9][0-9]*)", function["marker"] or "")
    shared.check(match is not None, "延遲 function marker 非本票所有")
    account_id = int(match.group(1))
    shared.check(function["owner"] == "articles_owner" and function["safe"] and trigger["safe"]
                 and trigger["marker"] == function["marker"] and function["source"] == delay_source(account_id),
                 "延遲物件定義或權限不符，拒絕清理")
    shared.check(shared.sql(f"SELECT email='t57-{nonce}r-{BACKEND}-retry@example.test' "
                            f"FROM accounts WHERE id={account_id}") == "t", "延遲物件帳號所有權不符")
    shared.sql(f"BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='10s'; "
               f"DROP TRIGGER {DELAY} ON public.login_failures; DROP FUNCTION public.{DELAY}(); COMMIT;")
    delay_absent()


# 以提交後 DB 時間核對剩餘值，另驗重試不延長、不新增失敗且原有效 session 不受暫鎖影響。
def retry_cases(fixture):
    nonce, admin = fixture["nonce"], fixture["admin"]["token"]
    actor = shared.user(BACKEND, nonce + "r", "retry")
    original = shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200)
    settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 12}
    try:
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, settings, admin), 200)
        install_delay(nonce, actor["id"])
        started = time.monotonic()
        result = shared.request(BACKEND, "POST", extra.LOGIN, {"email": actor["email"], "password": "Wrong1234"})
        elapsed = time.monotonic() - started
        shared.expect(result, 429, "E0105")
        committed = json.loads(shared.sql(
            "SELECT json_build_object('failures',(SELECT count(*) FROM login_failures WHERE account_id=a.id),"
            "'remaining',greatest(0,ceil(extract(epoch FROM locked_until-clock_timestamp())))::integer,"
            "'duration',(SELECT extract(epoch FROM a.locked_until-f.occurred_at) FROM login_failures f "
            "WHERE f.account_id=a.id)) FROM accounts a " + f"WHERE a.id={actor['id']}"
        ))
        header = result[2].get("Retry-After", "")
        shared.check(elapsed >= 3 and committed["failures"] == 1 and committed["duration"] == 12,
                     "延遲未在提交執行，或失敗事件／既定鎖定時間未提交")
        shared.check(header.isdecimal() and 1 <= int(header) <= 10
                     and committed["remaining"] <= int(header) <= committed["remaining"] + 2,
                     "Retry-After 未扣除三秒 COMMIT 等待")
        before = extra.state(actor["id"])
        repeated = shared.request(BACKEND, "POST", extra.LOGIN,
                                  {"email": actor["email"], "password": "Abcd1234"})
        shared.retry_remaining(BACKEND, actor["id"], repeated)
        shared.check(int(repeated[2]["Retry-After"]) <= int(header) and extra.state(actor["id"]) == before,
                     "既有鎖重試延長到期時間或新增事件")
        shared.expect(shared.request(BACKEND, "GET", "/api/me", token=actor["token"]), 200)
    finally:
        cleanup_delay(nonce)
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, original, admin), 200)
    print("PASS CQ20 延遲 COMMIT 剩餘秒數、重試零副作用與原 session 可用", flush=True)


# 兩個新註冊測資以 DB 內複製 PHC 保存原值；恢復不讀出雜湊，也不碰既有帳號。
def cleanup_phc(nonce):
    fixture_names(nonce)
    target = f"t57-{nonce}a-{BACKEND}-phc@example.test"
    backup = f"t57-{nonce}a-{BACKEND}-phcbackup@example.test"
    invalid = f"cq20-invalid-{nonce}"
    # 註冊到一半或已恢復時沒有異常 marker，不要求兩個獨立隨機 PHC 原本就相同。
    if shared.sql(f"SELECT count(*) FROM accounts WHERE email='{target}' AND password_hash='{invalid}'") == "0":
        return
    restored = shared.sql("UPDATE accounts a SET password_hash=b.password_hash FROM accounts b "
                          f"WHERE a.email='{target}' AND b.email='{backup}' AND a.password_hash='{invalid}' RETURNING a.id")
    shared.check(restored.isdecimal(), "缺少唯一的目標／備份 PHC fixture，拒絕宣稱恢復")
    shared.check(shared.sql(f"SELECT count(*) FROM accounts WHERE email='{target}' "
                            f"AND password_hash='{invalid}'") == "0", "PHC 異常 fixture 尚未恢復")
    # 備份帳號的隨機 PHC 會被目標原值取代並保留；只承諾目標與 DB 備份精確一致。
    shared.check(shared.sql(f"SELECT a.password_hash=b.password_hash FROM accounts a "
                            f"JOIN accounts b ON b.email='{backup}' WHERE a.email='{target}'") == "t",
                 "目標 PHC 與資料庫內原值備份不一致")


# 公開註冊涵蓋帳密／Unicode 邊界，並用兩個後端登入證明正規化與原生 PHC 可互通。
def registration_cases(fixture):
    nonce, observed = fixture["nonce"], []
    for index, (password, nickname) in enumerate((("Abcd", "陳"), ("1234", "😀" * 30),
                                                ("Ab12" * 4, " e\u0301\r\n陳 "))):
        email = f"t57-{nonce}v{index}-{BACKEND}-member@example.test"
        body = {"email": "  " + email.upper() + "  ", "password": password, "nickname": nickname}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        shared.check(json.loads(raw) == body, "Unicode／CRLF 註冊測資被序列化改寫")
        shared.check(shared.expect(extra.decoded(extra.raw_request(
            "POST", "/api/auth/register", raw, "stale", backend=BACKEND)), 201) == {},
            "合法註冊或忽略舊 JWT 失敗")
        identifier = shared.sql(f"SELECT id FROM accounts WHERE email='{email}'")
        shared.check(identifier.isdecimal(), "正規化後未恰好建立一個帳號")
        account_id = int(identifier)
        shared.check(shared.sql(f"SELECT role||'|'||status FROM accounts WHERE id={account_id}") == "member|active"
                     and shared.sql(f"SELECT count(*) FROM login_sessions WHERE account_id={account_id}") == "0",
                     "註冊角色／狀態錯誤或擅自建立登入")

        # 雜湊只留記憶體比較，不印出或落盤；跨後端登入不可改寫原 PHC。
        encoded = shared.sql(f"SELECT password_hash FROM accounts WHERE id={account_id}")
        for backend in (BACKEND, PEER):
            login = shared.expect(shared.request(backend, "POST", extra.LOGIN,
                {"email": body["email"], "password": password}, "stale"), 200)
            extra.login_claims(login, account_id, "member", backend=backend)
            viewer = shared.expect(shared.request(backend, "GET", "/api/me", token=login["access_token"]), 200)
            shared.check(viewer == login["user"] and viewer["email"] == email and viewer["nickname"] == nickname,
                         "跨後端本人讀回遺失正規化 Email 或暱稱原文")
        shared.check(shared.sql(f"SELECT password_hash FROM accounts WHERE id={account_id}") == encoded,
                     "跨後端登入改寫儲存的 PHC")

    # 每個候選 Email 仍屬本票 nonce；拒絕後完整快照不變即證明未新增帳號／登入或失敗事件。
    invalid_email = f"t57-{nonce}-invalid@example.test"
    invalid_passwords = ("Ab1", "A" * 17, " Ab1", "Ab1 ", "Ab!1", "中文12")
    for field, values in (("password", invalid_passwords), ("nickname", ("😀" * 31, "\t\u3000\u00a0")),
                          ("email", (f"t57-{nonce}-bad..name@example.test", f"t57-{nonce}-陳@example.test"))):
        for value in values:
            body = {"email": invalid_email, "password": "Abcd1234", "nickname": "邊界", field: value}
            rejected(fixture, observed, "POST", "/api/auth/register", json.dumps(body).encode(), "stale",
                     422, "E0001", {field: [RANGE]})
    # 改用剛建立的有效帳號，避免不存在帳號掩蓋格式錯誤不計數的要求。
    for password in invalid_passwords:
        rejected(fixture, observed, "POST", extra.LOGIN,
                 json.dumps({"email": email, "password": password}).encode(), "stale",
                 422, "E0001", {"password": [RANGE]})
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ20 註冊 4／16 碼英數、Unicode 原文、跨後端登入與無效格式零副作用", flush=True)


# 非會員、停用與格式錯誤不計數；同值管理更新不得重做副作用，恢復不復活舊登入。
def account_cases(fixture):
    registration_cases(fixture)
    nonce, admin, observed = fixture["nonce"], fixture["admin"], []
    actor = shared.user(BACKEND, nonce + "a", "member")
    original = shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin["token"]), 200)
    enabled = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 30}
    try:
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, enabled, admin["token"]), 200)
        for body, status, code, errors in (
            ({"email": f"t57-{nonce}-unknown@example.test", "password": "Wrong1234"}, 401, "E0101", None),
            ({"email": admin["email"], "password": "Wrong1234"}, 401, "E0101", None),
            ({"email": actor["email"], "password": "Bad!"}, 422, "E0001", {"password": [RANGE]}),
            ({"email": "bad", "password": "Wrong1234"}, 422, "E0001", {"email": [RANGE]}),
        ):
            rejected(fixture, observed, "POST", extra.LOGIN, json.dumps(body).encode(), None, status, code, errors)
        status_path = f"/api/members/{actor['id']}/status"
        before = snapshot(nonce)
        shared.expect(shared.request(BACKEND, "PUT", status_path, {"status": "active"}, admin["token"]), 200)
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, enabled, admin["token"]), 200)
        shared.check(snapshot(nonce) == before, "同值 active／enabled 更新重做副作用")
        shared.expect(shared.request(BACKEND, "PUT", status_path, {"status": "disabled"}, admin["token"]), 200)
        before = snapshot(nonce)
        shared.expect(shared.request(BACKEND, "PUT", status_path, {"status": "disabled"}, admin["token"]), 200)
        shared.check(snapshot(nonce) == before, "同值 disabled 重新撤銷或改變資料")
        rejected(fixture, observed, "POST", extra.LOGIN,
                 json.dumps({"email": actor["email"], "password": "Wrong1234"}).encode(), None, 403, "E0102")
        rejected(fixture, observed, "GET", "/api/me", None, actor["token"], 403, "E0102")
        shared.expect(shared.request(BACKEND, "PUT", status_path, {"status": "active"}, admin["token"]), 200)
        rejected(fixture, observed, "GET", "/api/me", None, actor["token"], 401, "E0002")

        # 將原 PHC 直接複製至另一個相同公開測資密碼的新帳號，雜湊不經 Python 或 artifact。
        target = shared.user(BACKEND, nonce + "a", "phc")
        backup = shared.user(BACKEND, nonce + "a", "phcbackup")
        shared.check(target["email"] == f"t57-{nonce}a-{BACKEND}-phc@example.test"
                     and backup["email"] == f"t57-{nonce}a-{BACKEND}-phcbackup@example.test", "PHC email 所有權不符")
        shared.sql("BEGIN; "
                   f"UPDATE accounts b SET password_hash=a.password_hash FROM accounts a WHERE b.id={backup['id']} "
                   f"AND b.email='{backup['email']}' AND a.id={target['id']} AND a.email='{target['email']}'; "
                   f"UPDATE accounts SET password_hash='cq20-invalid-{nonce}' WHERE id={target['id']} "
                   f"AND email='{target['email']}'; COMMIT;")
        try:
            rejected(fixture, observed, "POST", extra.LOGIN,
                     json.dumps({"email": target["email"], "password": "Abcd1234"}).encode(), None, 500, "E0104")
        finally:
            cleanup_phc(nonce)
        shared.expect(shared.request(BACKEND, "POST", extra.LOGIN,
                                     {"email": target["email"], "password": "Abcd1234"}), 200)
        settings_cases(fixture, actor)
    finally:
        try:
            cleanup_phc(nonce)
        finally:
            # PHC 清理失敗仍須恢復原設定；外層 recover 會再收束其他獨立資料並保留 pending。
            shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, original, admin["token"]), 200)
    shared.trace_errors(BACKEND, observed)
    settings_races(fixture)
    print("PASS CQ20 計數資格、PHC 異常恢復、同值管理更新與停用／恢復", flush=True)


# 數值修改不延長既有鎖；關閉只清一次，重新啟用沿用四設定並從新世代累計。
def settings_cases(fixture, actor):
    admin = fixture["admin"]["token"]
    settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 30}
    shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, settings, admin), 200)
    wrong = {"email": actor["email"], "password": "Wrong1234"}
    shared.expect(shared.request(BACKEND, "POST", extra.LOGIN, wrong), 429, "E0105")
    locked = extra.state(actor["id"])
    changed = {**settings, "window_minutes": 6, "failure_threshold": 3, "lock_seconds": 31}
    shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, changed, admin), 200)
    shared.check(extra.state(actor["id"]) == locked, "數值更新改變既有 locked_until 或失敗世代")
    disabled = {**changed, "enabled": False}
    epoch = int(shared.sql(f"SELECT failure_epoch FROM accounts WHERE id={actor['id']}"))
    shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, disabled, admin), 200)
    shared.check(shared.sql(f"SELECT locked_until IS NULL AND failure_epoch={epoch + 1} "
                            f"FROM accounts WHERE id={actor['id']}") == "t", "關閉沒有解除鎖或換世代")
    before = snapshot(fixture["nonce"])
    shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, disabled, admin), 200)
    shared.expect(shared.request(BACKEND, "POST", extra.LOGIN, wrong), 401, "E0101")
    shared.check(snapshot(fixture["nonce"]) == before, "同值關閉或關閉期間錯誤登入改變計數")
    shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, changed, admin), 200)
    shared.check(shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200) == changed,
                 "重新啟用未保留三個數值")
    shared.expect(shared.request(BACKEND, "POST", extra.LOGIN, wrong), 401, "E0101")
    shared.check(shared.sql(f"SELECT locked_until IS NULL AND failure_epoch={epoch + 1} AND "
                            f"(SELECT count(*) FROM login_failures WHERE account_id=a.id AND failure_epoch=a.failure_epoch)=1 "
                            f"FROM accounts a WHERE id={actor['id']}") == "t", "重啟未從新世代的一次失敗開始")


# 只接受實際等待同一設定列的 DB 連線；後來者可被 holder 或先到請求阻擋。
def waiting_settings(holder_pid, previous=()):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        rows = json.loads(shared.sql(
            "SELECT coalesce(json_agg(json_build_object('pid',pid,'blockers',pg_blocking_pids(pid))),'[]') "
            "FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' "
            "AND state='active' AND wait_event_type='Lock' "
            "AND query ~* 'SELECT.*login_lockout_settings.*FOR UPDATE'"
        ))
        for row in rows:
            if row["pid"] not in previous and any(pid in (holder_pid, *previous) for pid in row["blockers"]):
                return row["pid"]
        time.sleep(0.025)
    raise AssertionError("未觀察到設定列的實際鎖等待")


# 持有設定列並觀察兩個 HTTP 依序排隊，釋放後由真交易決定關閉與登入的提交結果。
def settings_races(fixture):
    nonce, admin = fixture["nonce"], fixture["admin"]
    peer_admin = shared.expect(shared.request(PEER, "POST", extra.LOGIN,
        {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
    original = shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin["token"]), 200)
    settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 30}
    try:
        for login_first in (False, True):
            actor = shared.user(BACKEND, nonce + "z" + str(int(login_first)), "race")
            shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, settings, admin["token"]), 200)
            epoch = int(shared.sql(f"SELECT failure_epoch FROM accounts WHERE id={actor['id']}"))
            application = f"{nonce}-settings-{int(login_first)}"
            holder = subprocess.Popen(
                ["docker", "compose", "--profile", "*", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
                 "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            try:
                holder.stdin.write((f"SET application_name='{application}'; SET idle_in_transaction_session_timeout='12s'; "
                                    "BEGIN; SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE;\n"
                                    "\\echo cq20-settings-locked\n").encode())
                holder.stdin.flush()
                wait_line(holder, "cq20-settings-locked")
                holder_pid = int(shared.sql(f"SELECT pid FROM pg_stat_activity WHERE application_name='{application}'"))
                actions = (
                    (PEER, "PUT", extra.SETTINGS, {**settings, "enabled": False}, peer_admin),
                    (BACKEND, "POST", extra.LOGIN, {"email": actor["email"], "password": "Wrong1234"}, None),
                )
                if login_first:
                    actions = actions[::-1]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    try:
                        first = pool.submit(shared.request, *actions[0])
                        first_pid = waiting_settings(holder_pid)
                        second = pool.submit(shared.request, *actions[1])
                        waiting_settings(holder_pid, (first_pid,))
                    finally:
                        # 先釋放 holder 才等待 futures，避免 fixture 自己造成 HTTP timeout。
                        holder.stdin.write(b"ROLLBACK;\n\\q\n")
                        holder.stdin.flush()
                    results = (first.result(timeout=15), second.result(timeout=15))
                for action, result in zip(actions, results):
                    status = 200 if action[1] == "PUT" else 429 if login_first else 401
                    shared.expect(result, status, "E0105" if status == 429 else "E0101" if status == 401 else None)
                shared.check(shared.sql(
                    f"SELECT failure_epoch={epoch + 1} AND locked_until IS NULL AND "
                    f"(SELECT count(*) FROM login_failures WHERE account_id=a.id)={int(login_first)} "
                    f"FROM accounts a WHERE id={actor['id']}"
                ) == "t", "關閉／登入競爭留下鎖定、漏算或關閉後新增失敗")
                shared.check(shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin["token"]), 200)
                             == {**settings, "enabled": False}, "競爭後設定不是完整關閉值")
                shared.expect(shared.request(BACKEND, "GET", "/api/me", token=actor["token"]), 200)
            finally:
                if holder.poll() is None:
                    try:
                        holder.stdin.write(b"ROLLBACK;\n\\q\n")
                        holder.stdin.flush()
                        holder.wait(timeout=3)
                    except (BrokenPipeError, subprocess.TimeoutExpired):
                        holder.kill()
                        holder.wait(timeout=3)
                holder.stdin.close()
                holder.stdout.close()
    finally:
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, original, admin["token"]), 200)
    print("PASS CQ20 設定列真等待：關閉／登入兩個提交順序及既有 session", flush=True)


# 重用既有全部 22 操作與錯誤案例，讓共用輸入修正不漏驗內容與其他管理呼叫端。
def full_cases(nonce, packet, path):
    for case in (extra.entry_cases, extra.session_cases):
        print(f"RUN CQ20 reuse {case.__name__}", flush=True)
        case(nonce, backend=BACKEND)
    extra.expiry_cases(nonce, packet, path, backend=BACKEND)
    extra.member_list_cases(nonce, packet, path, backend=BACKEND)
    shared.backend_cases(BACKEND, nonce)
    for case in (shared.contract_cases, shared.boundary_cases, shared.lockout_cases,
                 shared.lockout_edge_cases, shared.database_cases):
        print(f"RUN CQ20 shared {case.__name__}", flush=True)
        case(BACKEND, nonce)
    counts = shared.extended_invalid_cases(BACKEND, nonce, {})
    shared.check(len(counts) == 284, "既有 284 無效輸入案例缺漏")
    shared.ordered_cases(BACKEND, nonce)
    print("RUN CQ20 DB unavailable", flush=True)
    extra.unavailable_cases(nonce, backend=BACKEND)


# 各 fixture 與 baseline 分別嘗試恢復；任何一項失敗保持 pending，不能讓後續 run 誤用未清完的環境。
def recover(packet, path):
    original = sys.exception()
    failures = []
    for cleanup in (cleanup_delay, cleanup_phc):
        try:
            cleanup(packet["nonce"])
        except (Exception, KeyboardInterrupt) as error:
            failures.append(error)
            print(f"RECOVERY FAIL {cleanup.__name__}: {type(error).__name__}", file=sys.stderr)
    try:
        # 共用 helper 只確認 baseline；原檔維持 false，避免清理未完成時短暫寫入整體成功。
        extra.restore_baseline(packet, path, ticket=TICKET, mark_restored=False)
    except (Exception, KeyboardInterrupt) as error:
        failures.append(error)
        print(f"RECOVERY FAIL baseline: {type(error).__name__}", file=sys.stderr)
    if not failures:
        try:
            packet["restored"] = True
            extra.save_packet(packet, path)
        except (Exception, KeyboardInterrupt) as error:
            packet["restored"] = False
            failures.append(error)
            print(f"RECOVERY FAIL completion: {type(error).__name__}", file=sys.stderr)
    if failures:
        print(f"RECOVERY pending: {path}", file=sys.stderr)
        # 若案例已失敗，讓原例外繼續傳遞；恢復失敗另以上方安全階段資訊報告。
        if original is None:
            raise failures[0]


# 原 guard 通過才落盤 baseline；所有結果都須等 fixture 清理與原狀恢復後才能宣告通過。
def main():
    args = arguments()
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        extra.validate_packet(packet, ticket=TICKET)
        shared.check(args.restore.name == f"recovery-{packet['nonce']}.json" and not packet["restored"],
                     "恢復檔名不符或已恢復，拒絕再次覆蓋 baseline")
    shared.guard()
    if packet is not None:
        recover(packet, args.restore)
        return
    nonce = TICKET + secrets.token_hex(4)
    fixture_names(nonce)
    pending_recovery()
    delay_absent()
    packet, path = extra.capture_baseline(nonce, ticket=TICKET)
    try:
        fixture = fixtures(nonce)
        cases = {"query": query_cases, "integer": integer_cases, "retry": retry_cases, "account": account_cases}
        for name in PARTS if args.regression in (None, "all") else (args.regression,):
            print(f"RUN CQ20 regression {name}", flush=True)
            cases[name](fixture)
        if args.regression is None:
            full_cases(nonce, packet, path)
    finally:
        recover(packet, path)
    print(f"PASS CQ20 regression {args.regression}" if args.regression is not None
          else "PASS CQ20 Django 公開 HTTP／DB full suite", flush=True)
    print("範圍限制：未驗瀏覽器、未決搜尋 U+0000 政策、SQL 效能或其他十一後端完整矩陣", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # 舊 helper 診斷可能含實際回應；只保留安全定位文字，避免輸出帳密、雜湊或 JWT。
        detail = str(error).split("actual=", 1)[0].split("expected=", 1)[0][:300] if isinstance(error, AssertionError) else ""
        print(f"FAIL CQ20 {type(error).__name__}: {detail}；核對 RUN 階段與 recovery 檔", file=sys.stderr)
        sys.exit(1)
