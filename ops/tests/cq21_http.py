"""CQ21：保留 Ticket 57 guard，在隔離 HTTP／DB 驗證 Django 內容契約。"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import traceback
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from cq10_http import changed, create_article, create_comment, login, parent_overlap, row
import cq18_http as extra
import ticket57_http as shared


BACKEND = "django"
PEER = "symfony"
TICKET = "cq21"
RECOVERY = extra.RECOVERIES[TICKET]
PARTS = ("scope", "lifecycle", "input", "race")
GUARD_SHA = "c3a9b9a52176008c06221befceeae0da86f36f77e0f2c3b6138c4a4bf4c21fb6"
RANGE = "欄位格式或範圍不正確"
TYPE = "欄位型態不正確"
BASE_FIELDS = {"id", "author", "body", "version", "created_backend", "created_at",
               "updated_at", "visibility", "can_restore"}


# 只允許本票案例與固定恢復路徑，錯誤參數在任何服務操作前停止。
def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", choices=("all", *PARTS), nargs="?", const="all")
    modes.add_argument("--restore", type=Path)
    modes.add_argument("--self-check", action="store_true", help="只驗本機序列化及 recovery 相容性，不連服務")
    args = parser.parse_args()
    if args.restore is not None and (
        args.restore.parent != RECOVERY
        or re.fullmatch(r"recovery-cq21[0-9a-f]{8}\.json", args.restore.name) is None
    ):
        parser.error("--restore 只接受本票固定目錄及 recovery 檔名")
    return args


# 固定 guard 來源不可被本票改寫；仍由原 guard 查核全部十二 API、volume、network 與金鑰。
def guard_source():
    shared.check(hashlib.sha256(Path(shared.__file__).read_bytes()).hexdigest() == GUARD_SHA,
                 "Ticket 57 guard 來源不同，須 Main 重新核對")


# 所有前票恢復記錄都須完成；可傳暫存路徑離線驗 pending，但 live 入口固定使用完整預設。
def pending_recovery(*, recoveries=extra.RECOVERIES):
    for ticket, folder in recoveries.items():
        for path in folder.glob(f"recovery-{ticket}*.json"):
            packet = json.loads(path.read_text(encoding="utf-8"))
            extra.validate_packet(packet, ticket=ticket)
            shared.check(path.name == f"recovery-{packet['nonce']}.json" and packet["restored"],
                         "既有 recovery 未恢復或檔名不符，拒絕新測資")


# 快照只留記憶體，以明示票號的帳號、登入、事件、內容與設定證明拒絕沒有部分寫入。
def snapshot(nonce, *, ticket=TICKET):
    shared.check(re.fullmatch(extra.NONCES[ticket], nonce) is not None, "內容測試 nonce 不符")
    ids = f"SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%'"
    return shared.sql(
        "SELECT jsonb_build_object('settings',(SELECT to_jsonb(s) FROM login_lockout_settings s WHERE id=1),"
        f"'accounts',(SELECT jsonb_agg(a ORDER BY a.id) FROM accounts a WHERE a.id IN ({ids})),"
        f"'sessions',(SELECT jsonb_agg(s ORDER BY s.jti) FROM login_sessions s WHERE s.account_id IN ({ids})),"
        f"'failures',(SELECT jsonb_agg(f ORDER BY f.id) FROM login_failures f WHERE f.account_id IN ({ids})),"
        f"'articles',(SELECT jsonb_agg(a ORDER BY a.id) FROM articles a WHERE a.author_id IN ({ids})),"
        f"'comments',(SELECT jsonb_agg(c ORDER BY c.id) FROM comments c WHERE c.author_id IN ({ids})))::text"
    )


# 原始 bytes 避免客戶端改寫無效 JSON；每個拒絕逐字驗 envelope 並比較完整 DB 快照。
def rejected(nonce, observed, method, path, payload, token, status, code, fields=None, *,
             backend=BACKEND, ticket=TICKET):
    before = snapshot(nonce, ticket=ticket)
    raw = payload if isinstance(payload, bytes) or payload is None else json.dumps(payload).encode()
    result = extra.decoded(extra.raw_request(method, path, raw, token, backend=backend))
    shared.expect(result, status, code, fields)
    shared.check(result[2].get("Access-Control-Allow-Origin") == extra.ORIGIN, "拒絕回應缺 CORS")
    # Fiber／Express／Fastify 日誌保留百分比編碼路徑；其他後端沿用解碼對應，只調整追查期望、不改 HTTP URL。
    log_path = path.split("?", 1)[0]
    observed.append((result[1]["log_id"], method, log_path if backend in ("fiber", "express", "fastify") else unquote(log_path), status))
    shared.check(snapshot(nonce, ticket=ticket) == before, "拒絕請求留下部分資料變更")


# 讀取物件須為完整共同欄位，時間與版本對照 DB，不以另一框架回應充當契約。
def resource(data, table, identifier, actor_id, backend):
    persisted = row(table, identifier)
    fields = BASE_FIELDS | ({"title"} if table == "articles" else {"article_id"})
    shared.check(set(data) == fields and set(data["author"]) == {"id", "nickname"}, "讀取欄位集合不符")
    shared.check(type(data["id"]) is int and data["id"] == identifier
                 and type(data["version"]) is int and data["version"] == persisted["version"]
                 and type(data["author"]["id"]) is int and data["author"]["id"] == actor_id
                 and data["author"]["nickname"] == shared.sql(f"SELECT nickname FROM accounts WHERE id={actor_id}")
                 and data["created_backend"] == backend
                 and type(data["can_restore"]) is bool and data["visibility"] == persisted["visibility"]
                 and data["body"] == persisted["body"], "讀取型態、來源、內容或狀態不符")
    for name in ("created_at", "updated_at"):
        expected = datetime.fromisoformat(persisted[name]).astimezone(ZoneInfo("Asia/Taipei"))
        shared.check(data[name] == expected.strftime("%Y-%m-%d %H:%M:%S"), "內容業務時間不符")
    field = "title" if table == "articles" else "article_id"
    shared.check(data[field] == persisted[field], "文章標題或留言父 ID 不符")


# 以固定 SQL 範圍驗第一／末／超頁，兼顧全部既有資料的排序總數及可重跑性。
def exact_list(path, token, table, where, *, backend=BACKEND):
    shared.check(table in ("articles", "comments"), "列表表名不符")
    total = int(shared.sql(f"SELECT count(*) FROM {table} {where}"))
    separator = "&" if "?" in path else "?"
    last = max(1, (total + 99) // 100)
    for page in sorted({1, last, last + 1}):
        data = shared.expect(shared.request(backend, "GET", path + separator + f"page={page}&per_page=100",
                                            token=token), 200)
        expected = shared.sql(f"SELECT id FROM {table} {where} ORDER BY created_at DESC,id DESC "
                              f"LIMIT 100 OFFSET {(page - 1) * 100}")
        expected_ids = [int(value) for value in expected.splitlines()] if expected else []
        shared.check(data["pagination"] == {"page": page, "per_page": 100, "total": total,
                     "total_pages": (total + 99) // 100}
                     and [item["id"] for item in data["list"]] == expected_ids, "列表範圍、排序或超頁不符")


# 唯一作者與管理員區分公開、本人及管理範圍；重用已參數化父文案例保留作者例外。
def scope_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    owner = shared.user(backend, nonce + "s", "member")
    other = shared.user(backend, nonce + "s", "other")
    admin = shared.user(backend, nonce + "s", "admin")
    article_id = create_article(backend, owner, f"cq21-{nonce}-scope")
    comment_id = create_comment(backend, owner, article_id, f"cq21-{nonce}-scope-comment")
    article_path, comment_path = f"/api/articles/{article_id}", f"/api/comments/{comment_id}"
    observed = []

    # 可讀的公開目標無作者權即 403；先驗認證及格式，不能以舊版本取代這些錯誤。
    for path, body in ((article_path, {"title": "標題", "body": "內容", "version": 999}),
                       (comment_path, {"body": "內容", "version": 999})):
        rejected(nonce, observed, "PUT", path, {**body, "version": 0}, None, 401, "E0002",
                 backend=backend, ticket=ticket)
        rejected(nonce, observed, "PUT", path, {**body, "version": 0}, other["token"], 422, "E0001",
                 {"version": [RANGE]}, backend=backend, ticket=ticket)
        for actor in (other, admin):
            rejected(nonce, observed, "PUT", path, body, actor["token"], 403, "E0003", backend=backend, ticket=ticket)
    rejected(nonce, observed, "GET", comment_path, None, other["token"], 403, "E0003", backend=backend, ticket=ticket)
    for path in (article_path, comment_path):
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "hidden", "version": 1}, owner["token"]), 200)
        rejected(nonce, observed, "PUT", path + "/visibility", {"visibility": "visible", "version": 999},
                 other["token"], 404, "E0004", backend=backend, ticket=ticket)
        rejected(nonce, observed, "GET", path, None, other["token"], 404, "E0004", backend=backend, ticket=ticket)
        body = {"body": "內容", "version": 999}
        if path == article_path:
            body["title"] = "標題"
        rejected(nonce, observed, "PUT", path, body, other["token"], 404, "E0004", backend=backend, ticket=ticket)
    for token, status, code in ((None, 401, "E0002"), (other["token"], 403, "E0003")):
        for path in ("/api/comments?page=bad&per_page=100",
                     "/api/articles?filters%5Bscope%5D=management&page=bad&per_page=100"):
            rejected(nonce, observed, "GET", path, None, token, status, code, backend=backend, ticket=ticket)
    for path in ("/api/comments?page=bad&per_page=100",
                 "/api/articles?filters%5Bscope%5D=management&page=bad&per_page=100"):
        rejected(nonce, observed, "GET", path, None, admin["token"], 422, "E0001", {"page": [RANGE]},
                 backend=backend, ticket=ticket)
    for path in ("/api/articles/not-an-id?unknown=1", "/api/articles/not-an-id/comments?page=bad&per_page=20"):
        rejected(nonce, observed, "GET", path, None, "invalid", 401, "E0002", backend=backend, ticket=ticket)
    rejected(nonce, observed, "GET", "/api/articles/not-an-id", None, None, 422, "E0001", {"article_id": [RANGE]},
             backend=backend, ticket=ticket)
    for token in (None, owner["token"], admin["token"]):
        exact_list("/api/articles", token, "articles", "WHERE visibility='visible'", backend=backend)
    exact_list("/api/articles?filters%5Bscope%5D=management", admin["token"], "articles", "", backend=backend)
    exact_list("/api/comments", admin["token"], "comments", "", backend=backend)
    exact_list("/api/me/articles", owner["token"], "articles", f"WHERE author_id={owner['id']}", backend=backend)
    exact_list("/api/me/comments", owner["token"], "comments", f"WHERE author_id={owner['id']}", backend=backend)
    shared.trace_errors(backend, observed)
    extra.hidden_parent_cases(nonce, backend=backend)
    print(f"PASS {ticket.upper()} scope：登入／格式／404／403順序、公開／本人／管理範圍與父文例外", flush=True)


# 同值與過期都比較整列含微秒時間；管理員接管視為變更，作者無法藉重下架奪回恢復權。
def lifecycle_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    owner = shared.user(backend, nonce + "l", "member")
    admin = shared.user(backend, nonce + "l", "admin")
    peer_token = login(PEER, owner)
    parent = create_article(backend, owner, f"cq21-{nonce}-parent")
    article_id = create_article(backend, owner, f"cq21-{nonce}-life")
    comment_id = create_comment(backend, owner, parent, f"cq21-{nonce}-life-comment")
    observed = []
    for table, identifier in (("articles", article_id), ("comments", comment_id)):
        path = f"/api/{table}/{identifier}"
        initial = row(table, identifier)
        shared.check(initial["version"] == 1 and initial["created_at"] == initial["updated_at"], "建立版本／時間不符")
        data = shared.expect(shared.request(backend, "GET", path, token=owner["token"]), 200)
        resource(data, table, identifier, owner["id"], backend)
        shared.check(data["visibility"] == "visible" and data["can_restore"] is False, "建立可見性或恢復資格不符")
        body = {"body": initial["body"], "version": 1}
        if table == "articles":
            body["title"] = initial["title"]
        shared.check(shared.expect(shared.request(backend, "PUT", path, body, owner["token"]), 200) == {}, "同值 data 不符")
        shared.check(row(table, identifier) == initial, "正確版本同值更動整列")
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "visible", "version": 1}, owner["token"]), 200)
        shared.check(row(table, identifier) == initial, "公開同值可見性更動整列")
        body["body"] = " 修改😀e\u0301\r\n<script>純文字</script>\r\n "
        shared.expect(shared.request(backend, "PUT", path, body, owner["token"]), 200)
        updated = row(table, identifier)
        changed(initial, updated, "修改版本／微秒時間未遞增")
        rejected(nonce, observed, "PUT", path, body, owner["token"], 409, "E0201", backend=backend, ticket=ticket)
        body["version"] = updated["version"]
        shared.expect(shared.request(backend, "PUT", path, body, owner["token"]), 200)
        shared.check(row(table, identifier) == updated, "修改後同值更動整列")
        rejected(nonce, observed, "PUT", path + "/visibility", {"visibility": "visible", "version": 1},
                 owner["token"], 409, "E0201", backend=backend, ticket=ticket)

        # 作者自己下架後仍可改文，原下架狀態與恢復權必須保留；留言父文保持公開。
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "hidden", "version": updated["version"]}, owner["token"]), 200)
        hidden = row(table, identifier)
        changed(updated, hidden, "作者下架未遞增版本／時間")
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "visible", "version": hidden["version"]}, owner["token"]), 200)
        restored_by_author = row(table, identifier)
        changed(hidden, restored_by_author, "作者恢復未遞增版本／時間")
        shared.check(restored_by_author["visibility"] == "visible" and restored_by_author["restore_control"] is None,
                     "作者恢復未清除控制者")
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "hidden", "version": restored_by_author["version"]}, owner["token"]), 200)
        hidden = row(table, identifier)
        body.update(body="下架內容編修\r\n😀", version=hidden["version"])
        shared.expect(shared.request(backend, "PUT", path, body, owner["token"]), 200)
        edited = row(table, identifier)
        changed(hidden, edited, "下架編修未遞增版本／時間")
        shared.check(edited["visibility"] == "hidden" and edited["restore_control"] == "author", "編修擅改下架控制")
        rejected(nonce, observed, "PUT", path + "/visibility", {"visibility": "hidden", "version": hidden["version"]},
                 owner["token"], 409, "E0201", backend=backend, ticket=ticket)
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "hidden", "version": edited["version"]}, owner["token"]), 200)
        shared.check(row(table, identifier) == edited, "作者同值下架更動整列")
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "hidden", "version": edited["version"]}, admin["token"]), 200)
        takeover = row(table, identifier)
        changed(edited, takeover, "管理員接管未遞增版本／時間")
        shared.check(takeover["restore_control"] == "admin", "管理員未接管恢復權")
        for actor, can_restore in ((owner, False), (admin, True)):
            shared.check(shared.expect(shared.request(backend, "GET", path, token=actor["token"]), 200)
                         ["can_restore"] is can_restore, "接管後 can_restore 不符")
            shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                         {"visibility": "hidden", "version": takeover["version"]}, actor["token"]), 200)
            shared.check(row(table, identifier) == takeover, "重下架更動整列或奪回控制")
        for version in (1, takeover["version"]):
            rejected(nonce, observed, "PUT", path + "/visibility", {"visibility": "visible", "version": version},
                     owner["token"], 403, "E0003", backend=backend, ticket=ticket)
        shared.expect(shared.request(backend, "PUT", path + "/visibility",
                                     {"visibility": "visible", "version": takeover["version"]}, admin["token"]), 200)
        restored = row(table, identifier)
        changed(takeover, restored, "管理員恢復未遞增版本／時間")
        shared.check(restored["visibility"] == "visible" and restored["restore_control"] is None, "恢復未清控制者")
        primary = shared.expect(shared.request(backend, "GET", path, token=owner["token"]), 200)
        resource(primary, table, identifier, owner["id"], backend)
        shared.check(primary == shared.expect(shared.request(PEER, "GET", path, token=peer_token), 200), "跨後端讀回不符")

    # 管理員自己的文章／留言按作者規則，不能把所有管理員下架一律當成接管。
    own_article = create_article(backend, admin, f"cq21-{nonce}-admin-own")
    own_comment = create_comment(backend, admin, parent, f"cq21-{nonce}-admin-comment")
    for table, identifier in (("articles", own_article), ("comments", own_comment)):
        path = f"/api/{table}/{identifier}/visibility"
        shared.expect(shared.request(backend, "PUT", path, {"visibility": "hidden", "version": 1}, admin["token"]), 200)
        hidden = row(table, identifier)
        shared.check(hidden["restore_control"] == "author" and hidden["version"] == 2, "管理員自己下架控制者不符")
        shared.expect(shared.request(backend, "PUT", path, {"visibility": "hidden", "version": 2}, admin["token"]), 200)
        shared.check(row(table, identifier) == hidden, "管理員自己重下架更動整列")
        shared.expect(shared.request(backend, "PUT", path, {"visibility": "visible", "version": 2}, admin["token"]), 200)
        shared.check(row(table, identifier)["restore_control"] is None, "管理員自己恢復未清控制者")
    shared.trace_errors(backend, observed)
    print(f"PASS {ticket.upper()} lifecycle：文章／留言完整同值、過期、下架編修、接管／恢復與跨後端讀回", flush=True)


# 建立特殊原文時用 UTF-8 hex 精確定位，不猜最新 ID，也不把內容插入 SQL 字串。
def create_text(actor, title, body, *, backend=BACKEND):
    shared.check(shared.expect(shared.request(backend, "POST", "/api/articles",
                                             {"title": title, "body": body}, actor["token"]), 201) == {}, "新增文章 data 不符")
    identifier = shared.sql(f"SELECT id FROM articles WHERE author_id={actor['id']} "
                            f"AND encode(convert_to(title,'UTF8'),'hex')='{title.encode().hex()}'")
    shared.check(identifier.isdecimal(), "特殊原文文章未唯一建立")
    return int(identifier)


# 逐欄覆蓋建立／修改長度與型態，版本、ID、未知欄位及原始 JSON 另按共同錯誤順序驗證。
def input_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    actor = shared.user(backend, nonce + "i", "member")
    observed = []
    article_id = create_article(backend, actor, f"cq21-{nonce}-input")
    comment_id = create_comment(backend, actor, article_id, f"cq21-{nonce}-input-comment")
    article_path, comment_path = f"/api/articles/{article_id}", f"/api/comments/{comment_id}"
    endpoints = (
        ("POST", "/api/articles", {"title": "新標題", "body": "新內文"}, {"title": 200, "body": 20000}),
        ("PUT", article_path, {"title": "標題", "body": "內文", "version": 1}, {"title": 200, "body": 20000}),
        ("POST", article_path + "/comments", {"body": "留言"}, {"body": 2000}),
        ("PUT", comment_path, {"body": "留言", "version": 1}, {"body": 2000}),
        ("PUT", article_path + "/visibility", {"visibility": "hidden", "version": 1}, {}),
        ("PUT", comment_path + "/visibility", {"visibility": "hidden", "version": 1}, {}),
    )
    for method, path, base, limits in endpoints:
        # 必填、null、型態及內容格式分開，避免一欄錯誤遮蔽另一欄的實際驗證。
        for field in base:
            missing = dict(base)
            del missing[field]
            rejected(nonce, observed, method, path, missing, actor["token"], 422, "E0001", {field: ["此欄位為必填"]},
                     backend=backend, ticket=ticket)
            for value, message in ((None, "此欄位不可為 null"), ([], TYPE), ({}, TYPE), (True, TYPE), (1.5, TYPE)):
                rejected(nonce, observed, method, path, {**base, field: value}, actor["token"], 422, "E0001", {field: [message]},
                         backend=backend, ticket=ticket)
        for field, limit in limits.items():
            # 將契約固定空白集合全部放入同一字串；任一字元誤判非空白都會使此例失敗。
            whitespace = "\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
            for value in ("", whitespace, "😀" * (limit + 1), "前\0後"):
                payload = {**base, field: value}
                shared.check(json.loads(json.dumps(payload))[field] == value, "特殊內容序列化失真")
                rejected(nonce, observed, method, path, payload, actor["token"], 422, "E0001", {field: [RANGE]},
                         backend=backend, ticket=ticket)
        if "version" in base:
            for value, message in (("1", TYPE), (1.0, TYPE), (0, RANGE), (-1, RANGE), (9007199254740992, RANGE)):
                rejected(nonce, observed, method, path, {**base, "version": value}, actor["token"], 422, "E0001",
                         {"version": [message]}, backend=backend, ticket=ticket)
            rejected(nonce, observed, method, path, {**base, "version": 9007199254740991}, actor["token"], 409, "E0201",
                     backend=backend, ticket=ticket)
        if "visibility" in base:
            rejected(nonce, observed, method, path, {**base, "visibility": "VISIBLE"}, actor["token"], 422, "E0001",
                     {"visibility": [RANGE]}, backend=backend, ticket=ticket)
        for field in ("id", "author", "created_backend", "created_at", "updated_at", "can_restore"):
            rejected(nonce, observed, method, path, {**base, field: "readonly"}, actor["token"], 422, "E0001",
                     {field: ["不支援此欄位"]}, backend=backend, ticket=ticket)
        rejected(nonce, observed, method, path, {**dict.fromkeys(base), "extra": 1}, actor["token"], 422, "E0001",
                 {**{field: ["此欄位不可為 null"] for field in base}, "extra": ["不支援此欄位"]}, backend=backend, ticket=ticket)
        for raw, status, code, errors in ((b"{", 400, "E0005", None),
                                          (b"[]", 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]}),
                                          (None, 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]})):
            rejected(nonce, observed, method, path, raw, actor["token"], status, code, errors,
                     backend=backend, ticket=ticket)
        rejected(nonce, observed, method, path + "?unknown=1", base, actor["token"], 422, "E0001",
                 {"unknown": ["不支援此欄位"]}, backend=backend, ticket=ticket)

    # 每個內容讀取入口都驗 GET Body／未知 Query；公開端點攜無效 token 也不能降為訪客。
    list_paths = ("/api/articles", "/api/me/articles", "/api/me/comments", article_path + "/comments")
    for path in (*list_paths, article_path, comment_path):
        query = "?page=1&per_page=20" if path in list_paths else ""
        rejected(nonce, observed, "GET", path + query, b"{}", actor["token"], 422, "E0001",
                 {"_body": ["此 API 不接受 Request Body"]}, backend=backend, ticket=ticket)
        rejected(nonce, observed, "GET", path + query + ("&" if query else "?") + "unknown=1",
                 None, actor["token"], 422, "E0001", {"unknown": ["不支援此欄位"]}, backend=backend, ticket=ticket)
    for path in ("/api/articles?page=bad&per_page=20", article_path + "?unknown=1",
                 article_path + "/comments?page=bad&per_page=20"):
        rejected(nonce, observed, "GET", path, None, "invalid", 401, "E0002", backend=backend, ticket=ticket)
    for path in list_paths:
        for suffix, fields in (("", {"page": ["此欄位為必填"], "per_page": ["此欄位為必填"]}),
                               ("?page=1&page=2&per_page=20", {"page": ["此參數不可重複"]}),
                               ("?page=1&per_page=101", {"per_page": [RANGE]}),
                               ("?page%5B%5D=1&per_page=20", {"page": [TYPE]})):
            rejected(nonce, observed, "GET", path + suffix, None, actor["token"], 422, "E0001", fields,
                     backend=backend, ticket=ticket)

    # 鍵的 UTF-8 bytes 身分與顯示標籤分開；合法 U+FFFD、非法 bytes 及百分比字面標籤不能誤合併。
    for suffix, fields in (
        ("%EF%BF%BD=x", {"�": ["不支援此欄位"]}),
        ("%EF%BF%BD=x&%EF%BF%BD=y", {"�": ["此參數不可重複"]}),
        ("%FF=x&%EF%BF%BD=y", {"%FF": ["不支援此欄位"], "�": ["不支援此欄位"]}),
        ("%FF=x&%25FF=y", {"%FF": ["不支援此欄位"]}),
        ("%25FF=x&%FF=y", {"%FF": ["不支援此欄位"]}),
        ("%FF=x&%ff=y&%25FF=z", {"%FF": ["此參數不可重複"]}),
        ("%25FF=x&%FF=y&%25FF=z", {"%FF": ["此參數不可重複"]}),
    ):
        rejected(nonce, observed, "GET", "/api/articles?page=1&per_page=20&" + suffix,
                 None, actor["token"], 422, "E0001", fields, backend=backend, ticket=ticket)

    # 最小與最大碼點都經建立及修改；非 BMP、組合字、HTML／Markdown 與 CRLF 保留原文。
    texts = (("😀", "😀", "😀"), ("😀" * 200, "😀" * 20000, "😀" * 2000),
             (" 標題e\u0301\r\n ", " <script>文字</script>\r\n**原文**\r尾\n😀 ", " 留言e\u0301\r\n😀 "))
    for title, body, comment_body in texts:
        shared.check(json.loads(json.dumps([title, body, comment_body], ensure_ascii=False).encode())
                     == [title, body, comment_body], "建立／修改特殊原文序列化失真")
        created = create_text(actor, title, body, backend=backend)
        path = f"/api/articles/{created}"
        detail = shared.expect(shared.request(backend, "GET", path), 200)
        resource(detail, "articles", created, actor["id"], backend)
        shared.check(detail["title"] == title and detail["body"] == body, "建立文章原文失真")
        shared.expect(shared.request(backend, "PUT", article_path, {"title": title, "body": body,
            "version": row("articles", article_id)["version"]}, actor["token"]), 200)
        detail = shared.expect(shared.request(backend, "GET", article_path), 200)
        shared.check(detail["title"] == title and detail["body"] == body, "修改文章原文失真")
        shared.expect(shared.request(backend, "POST", path + "/comments", {"body": comment_body}, actor["token"]), 201)
        identifier = int(shared.sql(f"SELECT id FROM comments WHERE article_id={created} AND author_id={actor['id']}"))
        detail = shared.expect(shared.request(backend, "GET", f"/api/comments/{identifier}", token=actor["token"]), 200)
        resource(detail, "comments", identifier, actor["id"], backend)
        shared.check(detail["body"] == comment_body, "建立留言原文失真")
        shared.expect(shared.request(backend, "PUT", comment_path, {"body": comment_body,
            "version": row("comments", comment_id)["version"]}, actor["token"]), 200)
        rejected(nonce, observed, "GET", comment_path, None, None, 401, "E0002", backend=backend, ticket=ticket)
        shared.check(shared.expect(shared.request(backend, "GET", comment_path, token=actor["token"]), 200)
                     ["body"] == comment_body, "修改留言原文失真")

    # Path 的前導零可用；JSON 版本字串不可用，合法不存在 ID 與非法格式須分清 404／422。
    for table, identifier in (("articles", article_id), ("comments", comment_id)):
        good = f"/api/{table}/{identifier}"
        shared.check(shared.expect(shared.request(backend, "GET", f"/api/{table}/000{identifier}", token=actor["token"]), 200)
                     == shared.expect(shared.request(backend, "GET", good, token=actor["token"]), 200), "前導零 ID 不相同")
        for invalid in ("0", "-1", "+1", "1.0", "1e0", " 1", "１", "9007199254740992"):
            field = "article_id" if table == "articles" else "comment_id"
            rejected(nonce, observed, "GET", f"/api/{table}/{quote(invalid, safe='')}", None, actor["token"],
                     422, "E0001", {field: [RANGE]}, backend=backend, ticket=ticket)
        rejected(nonce, observed, "GET", f"/api/{table}/9007199254740991", None, actor["token"], 404, "E0004",
                 backend=backend, ticket=ticket)
    shared.trace_errors(backend, observed)
    print(f"PASS {ticket.upper()} input：內容逐欄／型態／唯讀／NUL、Unicode上下限、原文、ID與version", flush=True)


# 只重用可參數化的父列平台；兩方向、兩操作、兩順序均須觀察雙 API 等待及提交後讀回。
def race_cases(nonce, *, backend=BACKEND):
    for hide_backend, comment_backend in ((backend, PEER), (PEER, backend)):
        for operation in ("create", "update"):
            for comment_first in (False, True):
                parent_overlap(nonce, hide_backend, comment_backend, operation, comment_first)


# 中斷後只終止本 nonce 的既有 holder；不清未知連線、trigger、資料或其他票 fixture。
def cleanup_holders(nonce, *, ticket=TICKET):
    shared.check(re.fullmatch(extra.NONCES[ticket], nonce) is not None, "holder marker 不符")
    shared.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() "
               f"AND application_name LIKE 'cq10-{nonce}-%' AND pid<>pg_backend_pid()")
    shared.check(shared.sql("SELECT count(*) FROM pg_stat_activity "
                            f"WHERE application_name LIKE 'cq10-{nonce}-%'") == "0", "本票 holder 未清理")


# holder 清理與 baseline 恢復都完成才標記成功；失敗保留 pending 並讓原案例錯誤繼續傳遞。
def recover(packet, path):
    original = sys.exception()
    failures = []
    try:
        cleanup_holders(packet["nonce"])
    except (Exception, KeyboardInterrupt) as error:
        failures.append(error)
        print(f"RECOVERY FAIL holder: {type(error).__name__}", file=sys.stderr)
    try:
        extra.restore_baseline(packet, path, ticket=TICKET, mark_restored=False)
    except (Exception, KeyboardInterrupt) as error:
        failures.append(error)
        print(f"RECOVERY FAIL baseline: {type(error).__name__}", file=sys.stderr)
    if not failures:
        packet["restored"] = True
        try:
            extra.save_packet(packet, path)
        except (Exception, KeyboardInterrupt) as error:
            packet["restored"] = False
            failures.append(error)
    if failures:
        print(f"RECOVERY pending: {path}", file=sys.stderr)
        if original is None:
            raise failures[0]


# 離線檢查只用非秘密合成值，確保新 namespace 不改 cq18 預設且特殊字元確實往返。
def self_check():
    guard_source()
    packet = {"nonce": "cq18abcdef123456", "settings": {"enabled": True, "window_minutes": 5,
              "failure_threshold": 5, "lock_seconds": 300}, "members": [], "account_times": [],
              "session_times": [], "restored": False}
    extra.validate_packet(packet)
    for ticket, nonce in (("cq18", "cq18abcdef1234567890"), ("cq19", "cq19abcdef12"), ("cq20", "cq20abcdef12")):
        packet["nonce"] = nonce
        extra.validate_packet(packet, ticket=ticket)
    packet["nonce"] = "cq21abcdef12"
    extra.validate_packet(packet, ticket=TICKET)
    try:
        extra.validate_packet(packet)
    except AssertionError:
        pass
    else:
        raise AssertionError("cq18 預設錯誤接受 CQ21 marker")
    for value in ("😀e\u0301\r\n<script>原文</script>", "前\0後"):
        shared.check(json.loads(json.dumps({"body": value}).encode())["body"] == value, "序列化未保留碼點")
    print("PASS CQ21 offline：guard SHA、cq18預設／cq21隔離與Unicode／CRLF／NUL序列化", flush=True)


# 首次 API 寫入前新擷取並 fsync baseline；所有案例及 finally 恢復完成才可輸出整體 PASS。
def main():
    args = arguments()
    if args.self_check:
        self_check()
        return
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        extra.validate_packet(packet, ticket=TICKET)
        shared.check(args.restore.name == f"recovery-{packet['nonce']}.json" and not packet["restored"],
                     "恢復檔名不符或已完成，拒絕覆蓋 baseline")
    guard_source()
    shared.guard()
    if packet is not None:
        recover(packet, args.restore)
        return
    pending_recovery()
    nonce = TICKET + secrets.token_hex(4)
    packet, path = extra.capture_baseline(nonce, ticket=TICKET)
    try:
        cases = {"scope": scope_cases, "lifecycle": lifecycle_cases, "input": input_cases, "race": race_cases}
        for name in PARTS if args.regression in (None, "all") else (args.regression,):
            print(f"RUN CQ21 {name}", flush=True)
            cases[name](nonce)
    finally:
        recover(packet, path)
    print("PASS CQ21 Django 內容公開 HTTP／DB suite" if args.regression in (None, "all")
          else f"PASS CQ21 partial {args.regression}；其餘案例未執行", flush=True)
    print("限制：未驗瀏覽器、搜尋U+0000、SQL效能或其他十一後端完整矩陣", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # 舊 seam 例外可能包含回應或憑證；只保留程式位置，不印例外內容、來源行或區域變數。
        frames = traceback.extract_tb(error.__traceback__)
        locations = " -> ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
        print(f"FAIL CQ21 {type(error).__name__} at {locations}；核對 RUN 階段與 recovery 檔", file=sys.stderr)
        sys.exit(1)
