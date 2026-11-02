"""CQ19：沿用固定 Ticket 57 guard 與既有案例，驗證 ASP.NET Core 公開 HTTP／DB 契約。"""

import argparse
import base64
import json
from pathlib import Path
import re
import secrets
import sys

from cq10_http import create_article, create_comment, parent_overlap
import cq18_http as extra
import ticket57_http as shared


BACKEND = "aspnet-core"
PEER = "django"
TICKET = "cq19"
RECOVERY = extra.RECOVERIES[TICKET]
PARTS = ("route", "query", "json", "integer", "body", "jwt")
RANGE = "欄位格式或範圍不正確"
TYPE = "欄位型態不正確"
NULL = "此欄位不可為 null"
UNKNOWN = "不支援此欄位"
DUPLICATE = "此參數不可重複"
BODY = {"_body": ["此 API 不接受 Request Body"]}


# CLI 僅接受本票有限案例與固定恢復目錄，參數錯誤必須先於 guard／任何服務操作停止。
def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", nargs="?", const="all", choices=("all", *PARTS),
                       help="只建立本票新測資，執行全部或指定公開回歸")
    modes.add_argument("--restore", type=Path, help="只恢復本票尚未完成的 recovery JSON")
    args = parser.parse_args()
    if args.restore is not None and (args.restore.parent != RECOVERY or
            re.fullmatch(r"recovery-cq19[0-9a-f]{8}\.json", args.restore.name) is None):
        parser.error("--restore 只接受本票固定目錄及 recovery 檔名")
    return args


# 在首次寫入前驗證共用 helper 實際組出的值，避免框架名稱較長讓合法測資超過暱稱上限。
def fixture_names(nonce):
    shared.check(re.fullmatch(extra.NONCES[TICKET], nonce) is not None, "CQ19 nonce 不符")
    for prefix in ("Aa%_\\Ä陳", "aa%_\\ä陳", "AaXY\\Ä陳"):
        nickname = prefix + nonce + BACKEND
        shared.check(len(nickname) <= 30, "共用搜尋暱稱測資超界")
    shared.check(PEER == shared.BACKENDS[(shared.BACKENDS.index(BACKEND) + 1) % len(shared.BACKENDS)],
                 "既有 shared peer 方向已變更，須先重新確認覆蓋")


# 兩票共用同一隔離資料庫；任何尚未恢復的 baseline 都必須先處理，不能疊加設定變更。
def pending_recovery():
    for ticket, folder in extra.RECOVERIES.items():
        for path in folder.glob(f"recovery-{ticket}*.json"):
            packet = json.loads(path.read_text(encoding="utf-8"))
            extra.validate_packet(packet, ticket=ticket)
            shared.check(path.name == f"recovery-{packet['nonce']}.json" and packet["restored"],
                         "既有 recovery 未恢復或檔名不符，拒絕建立新測資")


# 透過公開 API 建立帳號與內容，管理員由隔離 fixture SQL 提升，以驗證角色門禁。
def fixtures(nonce):
    member = shared.user(BACKEND, nonce + "r", "member")
    admin = shared.user(BACKEND, nonce + "r", "admin")
    title = f"t57-{nonce}-regression"
    article = create_article(BACKEND, member, title)
    comment_body = f"t57-{nonce}-comment"
    comment = create_comment(BACKEND, member, article, comment_body)
    return {"nonce": nonce, "member": member, "admin": admin, "title": title,
            "article": article, "comment": comment, "comment_body": comment_body}


# 一次 SQL 讀齊本票資料與四設定；快照只留記憶體，拒絕後可核對交易沒有部分提交。
def snapshot(nonce):
    shared.check(re.fullmatch(extra.NONCES[TICKET], nonce) is not None, "快照 marker 不符")
    account_ids = f"SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%'"
    return shared.sql(
        "SELECT jsonb_build_object('settings',(SELECT to_jsonb(s) FROM login_lockout_settings s WHERE id=1),"
        f"'accounts',(SELECT jsonb_agg(a ORDER BY a.id) FROM accounts a WHERE a.id IN ({account_ids})),"
        f"'sessions',(SELECT jsonb_agg(s ORDER BY s.jti) FROM login_sessions s WHERE s.account_id IN ({account_ids})),"
        f"'failures',(SELECT jsonb_agg(f ORDER BY f.id) FROM login_failures f WHERE f.account_id IN ({account_ids})),"
        f"'articles',(SELECT jsonb_agg(a ORDER BY a.id) FROM articles a WHERE a.author_id IN ({account_ids})),"
        f"'comments',(SELECT jsonb_agg(c ORDER BY c.id) FROM comments c WHERE c.author_id IN ({account_ids})))::text")


# 保留 bytes／chunk iterable，逐字驗證共同錯誤與 log_id；錯誤診斷不輸出可能含 JWT 的回應。
def rejected(fixture, observed, method, path, body, token, status, code, fields=None, *, origin=extra.ORIGIN):
    before = snapshot(fixture["nonce"])
    result = extra.decoded(extra.raw_request(method, path, body, token, origin=origin, backend=BACKEND))
    shared.expect(result, status, code, fields)
    observed.append((result[1]["log_id"], method, path.split("?", 1)[0], status))
    shared.check(snapshot(fixture["nonce"]) == before, "拒絕請求改變本票帳號／session／內容或管理設定")
    return result


# 每個 Path 欄位都先驗認證／角色再驗格式；空白不能被 MVC 自動改成 ProblemDetails。
def route_cases(fixture):
    member, admin = fixture["member"]["token"], fixture["admin"]["token"]
    observed = []
    routes = (
        ("GET", "/api/articles/%20", "article_id", False, False),
        ("GET", "/api/articles/%20/comments?page=1&per_page=20", "article_id", False, False),
        ("PUT", "/api/articles/%20", "article_id", True, False),
        ("PUT", "/api/articles/%20/visibility", "article_id", True, False),
        ("POST", "/api/articles/%20/comments", "article_id", True, False),
        ("GET", "/api/comments/%20", "comment_id", True, False),
        ("PUT", "/api/comments/%20", "comment_id", True, False),
        ("PUT", "/api/comments/%20/visibility", "comment_id", True, False),
        ("GET", "/api/members/%20", "member_id", True, True),
        ("PUT", "/api/members/%20/status", "member_id", True, True),
    )
    for method, path, field, protected, management in routes:
        # malformed Body 與 Path 並存，仍須按門禁、Path、Body 順序回應。
        body = None if method == "GET" else b"{"
        for token in (None, "invalid", member, admin):
            if token == "invalid" or (protected and token is None):
                status, code, fields = 401, "E0002", None
            elif management and token == member:
                status, code, fields = 403, "E0003", None
            else:
                status, code, fields = 422, "E0001", {field: [RANGE]}
            rejected(fixture, observed, method, path, body, token, status, code, fields)
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ19 Path 空白與各 PP 的認證／角色／格式順序", flush=True)


# raw FF 與字面 %FF 可撞 errors 標籤但不是同一參數；真重複及百分比 alias 仍須拒絕為重複。
def query_cases(fixture):
    token = fixture["member"]["token"]
    observed = []
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
    rejected(fixture, observed, "GET", "/api/articles?pa%67e=1&page=2&per_page=20", None,
             token, 422, "E0001", {"page": [DUPLICATE]})
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ19 Query 身分撞名、真重複與 decoded alias", flush=True)


# JSON 解碼須涵蓋未知／巢狀／被覆蓋值；孤 surrogate 是解析錯誤，正常配對仍保留 Unicode。
def json_cases(fixture):
    nonce, token = fixture["nonce"], fixture["member"]["token"]
    observed = []
    email = f"t57-{nonce}-json-rejected@example.test"
    prefix = json.dumps({"email": email, "password": "Abcd1234"}).encode()[:-1] + b","
    fragments = (
        b'"nickname":"\\ud800"}', b'"nickname":"\\udfff"}',
        b'"nickname":"ok","\\ud800":1}',
        b'"nickname":"ok","extra":{"value":"\\ud800"}}',
        b'"nickname":"ok","extra":{"\\udfff":1}}',
        b'"nickname":"ok","extra":["\\ud800"]}',
        b'"nickname":"\\ud800","nickname":"ok"}',
    )
    for fragment in fragments:
        raw = prefix + fragment
        shared.check(b"\\ud800" in raw or b"\\udfff" in raw, "JSON surrogate 測資被改寫")
        rejected(fixture, observed, "POST", "/api/auth/register", raw, None, 400, "E0005")
        shared.check(shared.sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "0",
                     "JSON 解析失敗留下帳號")
    for raw in (b'"\\ud800"', b'["\\udfff"]'):
        rejected(fixture, observed, "POST", "/api/auth/register", raw, None, 400, "E0005")
    rejected(fixture, observed, "POST", extra.LOGIN,
             b'{"email":"\\ud800","password":"Abcd1234"}', None, 400, "E0005")
    article_path = f"/api/articles/{fixture['article']}"
    raw = b'{"title":"\\ud800","body":"ok","version":1}'
    rejected(fixture, observed, "PUT", article_path, raw, token, 400, "E0005")
    rejected(fixture, observed, "PUT", article_path, raw, None, 401, "E0002")
    rejected(fixture, observed, "PUT", extra.SETTINGS, raw, token, 403, "E0003")
    # 用真管理員通過角色門禁，確認獨立的設定 Body decoder 也完整檢查未知巢狀字串。
    admin = fixture["admin"]["token"]
    settings = shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200)
    settings_raw = json.dumps(settings).encode()[:-1] + b',"extra":{"value":"\\ud800"}}'
    rejected(fixture, observed, "PUT", extra.SETTINGS, settings_raw, admin, 400, "E0005")
    rejected(fixture, observed, "POST", "/api/auth/register", b'["\\ud83d\\ude00"]', None,
             422, "E0001", {"_body": ["Request Body 必須為 JSON object"]})
    # 合法 surrogate pair 必須真的建立相同碼點，不能用全面拒絕 escape 讓負例通過。
    pair_email = f"t57-{nonce}-json-pair@example.test"
    raw = json.dumps({"email": pair_email, "password": "Abcd1234"}).encode()[:-1] + b',"nickname":"\\ud83d\\ude00"}'
    shared.check(json.loads(raw)["nickname"] == "\U0001f600", "正常 Unicode pair 測資不符")
    shared.expect(extra.decoded(extra.raw_request("POST", "/api/auth/register", raw, backend=BACKEND)), 201)
    shared.check(json.loads(shared.sql(f"SELECT to_json(nickname) FROM accounts WHERE email='{pair_email}'"))
                 == "\U0001f600", "正常 Unicode pair 未原樣儲存")
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ19 完整 JSON surrogate 解析與正常 Unicode pair", flush=True)


# 以唯一字串占位再放入原始數字 lexeme，避免 JSON 客戶端將 1e0／1.0 正規化成整數。
def integer_body(body, field, lexeme):
    marker = "__CQ19_INTEGER__"
    raw = json.dumps({**body, field: marker}, ensure_ascii=True, separators=(",", ":")).encode()
    shared.check(raw.count(json.dumps(marker).encode()) == 1, "整數 fixture 占位不唯一")
    return raw.replace(json.dumps(marker).encode(), lexeme.encode("ascii"))


# 所有 version 與設定數值各驗 lexical type／range 及 null 聚合；合法上限不能誤報型態。
def integer_cases(fixture):
    token, admin = fixture["member"]["token"], fixture["admin"]["token"]
    observed = []
    article, comment = f"/api/articles/{fixture['article']}", f"/api/comments/{fixture['comment']}"
    original = shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200)
    targets = (
        (article, {"title": fixture["title"], "body": "原始文章", "version": 1}, "version", token),
        (article + "/visibility", {"visibility": "visible", "version": 1}, "version", token),
        (comment, {"body": fixture["comment_body"], "version": 1}, "version", token),
        (comment + "/visibility", {"visibility": "visible", "version": 1}, "version", token),
        *((extra.SETTINGS, original, field, admin) for field in
          ("window_minutes", "failure_threshold", "lock_seconds")),
    )
    for path, body, field, credential in targets:
        for lexeme, message in (("9223372036854775808", RANGE), ("-9223372036854775809", RANGE),
                                ("9007199254740992", RANGE), ("0", RANGE),
                                ("1.0", TYPE), ("1e0", TYPE), ('"1"', TYPE), ("true", TYPE)):
            rejected(fixture, observed, "PUT", path, integer_body(body, field, lexeme), credential,
                     422, "E0001", {field: [message]})
        # 每欄只回自己的第一個錯誤；其他 required 欄位的 null 不因巨大整數而被漏掉。
        nulls = dict.fromkeys(body)
        fields = {name: [RANGE if name == field else NULL] for name in body}
        for lexeme in ("9223372036854775808", "-9223372036854775809"):
            rejected(fixture, observed, "PUT", path, integer_body(nulls, field, lexeme), credential,
                     422, "E0001", fields)
        if field == "version":
            rejected(fixture, observed, "PUT", path, integer_body(body, field, "9007199254740991"),
                     credential, 409, "E0201")
            before = snapshot(fixture["nonce"])
            shared.expect(shared.request(BACKEND, "PUT", path, body, credential), 200)
            shared.check(snapshot(fixture["nonce"]) == before, "合法 version 同值更新改變版本／時間")
        # 每欄完成即核對全部錯誤，避免既有 2,000 行日誌視窗淘汰較早的 IN／EX／OUT。
        shared.trace_errors(BACKEND, observed)
        observed.clear()
    # 合法設定上下限經真 API 寫回；不切換 enabled，finally 恢復原四欄。
    try:
        for field, maximum in (("window_minutes", 60), ("failure_threshold", 100), ("lock_seconds", 86400)):
            for value in (1, maximum):
                settings = {**original, field: value}
                shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, settings, admin), 200)
                shared.check(shared.expect(shared.request(BACKEND, "GET", extra.SETTINGS, token=admin), 200)
                             == settings, "合法設定上下限未完整提交")
    finally:
        shared.expect(shared.request(BACKEND, "PUT", extra.SETTINGS, original, admin), 200)
    print("PASS CQ19 巨大 JSON 整數 range／lexical type、null 聚合與合法界線", flush=True)


# GET Body 含 chunked 都先依門禁及 Body 規則拒絕；空 Body 與無 Origin 保留正常查詢行為。
def body_cases(fixture):
    token, admin = fixture["member"]["token"], fixture["admin"]["token"]
    observed = []
    for path, credential in (("/api/me", token), ("/api/articles?page=1&per_page=20", None),
                              ("/api/members?page=1&per_page=20", admin)):
        for body in (b"{}", b"{", (b"{", b"}")):
            rejected(fixture, observed, "GET", path + ("&" if "?" in path else "?") + "unexpected=1",
                     body, credential, 422, "E0001", BODY)
        for body in (None, b""):
            before = snapshot(fixture["nonce"])
            result = extra.decoded(extra.raw_request("GET", path, body, credential, origin=None, backend=BACKEND))
            shared.expect(result, 200)
            shared.check(snapshot(fixture["nonce"]) == before, "空 Body／無 Origin 讀取留下副作用")
    for token, status, code in ((None, 401, "E0002"), ("invalid", 401, "E0002")):
        rejected(fixture, observed, "GET", "/api/me?unexpected=1", (b"{", b"}"), token, status, code)
    rejected(fixture, observed, "GET", "/api/members?page=bad", (b"{", b"}"),
             fixture["member"]["token"], 403, "E0003")
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ19 GET bytes／chunked／空 Body、門禁優先與無 Origin", flush=True)


# 真實登入作有效前提，只簽署已核對 key 的錯誤 claims；不能讓 lazy JSON 解碼逃成 500。
def jwt_cases(fixture):
    token = fixture["member"]["token"]
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    observed = []
    before = snapshot(fixture["nonce"])
    valid = extra.signed_token(claims, backend=BACKEND)
    shared.expect(shared.request(BACKEND, "GET", "/api/me", token=valid), 200)
    shared.check(snapshot(fixture["nonce"]) == before, "有效 JWT control 改變 session")
    for field in ("iss", "sub", "jti"):
        malformed = extra.signed_token({**claims, field: "\ud800"}, backend=BACKEND)
        shared.check(b"\\ud800" in base64.urlsafe_b64decode(malformed.split(".")[1] + "=="),
                     "JWT surrogate 測資未保留原始 escape")
        parts = malformed.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        for candidate in (malformed, ".".join(parts)):
            rejected(fixture, observed, "GET", "/api/me", None, candidate, 401, "E0002")
    shared.trace_errors(BACKEND, observed)
    print("PASS CQ19 簽署 JWT 孤 surrogate claims 與無效簽章對照", flush=True)


# 每個模式皆使用新 fixture，可單獨留下某缺陷的 RED，不被更早的另一缺陷遮住。
def regression_cases(nonce, part):
    fixture = fixtures(nonce)
    cases = {"route": route_cases, "query": query_cases, "json": json_cases,
             "integer": integer_cases, "body": body_cases, "jwt": jwt_cases}
    for name in PARTS if part == "all" else (part,):
        print(f"RUN CQ19 regression {name}", flush=True)
        cases[name](fixture)


# 原 shared／CQ18 契約不複製；額外父列競爭在兩個方向觀察真等待並讀回提交結果。
def full_cases(nonce, packet, path):
    for case in (extra.entry_cases, extra.session_cases, extra.hidden_parent_cases):
        print(f"RUN CQ19 reuse {case.__name__}", flush=True)
        case(nonce, backend=BACKEND)
    extra.expiry_cases(nonce, packet, path, backend=BACKEND)
    extra.member_list_cases(nonce, packet, path, backend=BACKEND)
    records = shared.backend_cases(BACKEND, nonce)
    for case in (shared.contract_cases, shared.boundary_cases, shared.lockout_cases,
                 shared.lockout_edge_cases, shared.database_cases):
        print(f"RUN CQ19 shared {case.__name__}", flush=True)
        case(BACKEND, nonce)
    counts = shared.extended_invalid_cases(BACKEND, nonce, {})
    shared.check(len(counts) == 284, "既有 284 無效輸入案例缺漏")
    shared.ordered_cases(BACKEND, nonce)
    for hide, comment in ((BACKEND, PEER), (PEER, BACKEND)):
        for operation in ("create", "update"):
            for comment_first in (False, True):
                parent_overlap(nonce, hide, comment, operation, comment_first)
    extra.readback(records, backend=BACKEND)
    print("RUN CQ19 DB unavailable", flush=True)
    extra.unavailable_cases(nonce, backend=BACKEND)


# 原 guard 後先落盤 baseline；整體 PASS 只能在所選案例及 finally 精確恢復全部完成後出現。
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
        extra.restore_baseline(packet, args.restore, ticket=TICKET)
        return
    nonce = TICKET + secrets.token_hex(4)
    fixture_names(nonce)
    pending_recovery()
    packet, path = extra.capture_baseline(nonce, ticket=TICKET)
    try:
        regression_cases(nonce, args.regression or "all")
        if args.regression is None:
            full_cases(nonce, packet, path)
    finally:
        extra.restore_baseline(packet, path, ticket=TICKET)
    print(f"PASS CQ19 regression {args.regression}" if args.regression is not None
          else "PASS CQ19 ASP.NET Core 公開 HTTP／DB full suite", flush=True)
    print("範圍限制：未驗瀏覽器、未決搜尋 U+0000 政策與其他十一後端完整矩陣", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # shared 診斷可能含實際回應；只留下安全定位文字，不把帳密／JWT 寫入 artifact。
        detail = str(error).split("actual=", 1)[0].split("expected=", 1)[0][:300] if isinstance(error, AssertionError) else ""
        print(f"FAIL CQ19 {type(error).__name__}: {detail}；核對 RUN 階段與 recovery 檔", file=sys.stderr)
        sys.exit(1)
