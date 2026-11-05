"""PERF01 固定合成資料與獨立契約 oracle；本模組不連網、不讀資料庫。"""

from datetime import datetime, timedelta
import json
from urllib.parse import urlencode

BACKENDS = (
    "laravel", "symfony", "aspnet-core", "django", "flask", "fastapi",
    "gin", "echo", "fiber", "express", "fastify", "nestjs",
)
LEVELS = {"small": (64, 128, 256), "medium": (2048, 4096, 8192)}
PASSWORD = "Abcd1234"
ADMIN_EMAIL = "perf01-admin@example.test"
MEMBER_EMAIL = "perf01-actor@example.test"
INVALID = "欄位格式或範圍不正確"
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
}
KEYWORDS = (
    None, "", "   ", "a", "aa", "needlelong", "nickonly", "emailonly",
    "z" * 254, "陳", "AsCiI", "ascii", "Ä", "ä", "%", "_", "\\",
    "%_\\", "no-such-perf01-match", "  nickonly  ", "\u00a0nickonly\u00a0",
)


# ascii_fold 僅摺疊 ASCII 大寫，避免 Python Unicode lower 擴大核准比對語意。
def ascii_fold(value):
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


# stamp 讓不同 ID 共用秒數且時間與 ID 不單調，實際驗出雙欄降冪。
def stamp(number):
    return (datetime(2026, 1, 1, 8) + timedelta(seconds=number % 17)).strftime("%Y-%m-%d %H:%M:%S")


# fixture 產生固定 ID 清單；中量只增加尾段，既有資料內容不會因級別改變。
def fixture(level):
    members, articles, comments = LEVELS[level]
    names = ("a", "aa", "needlelong", "nickonly", "陳", "AsCiI", "Ä", "ä", "%_\\", "暱" * 30)
    accounts = [{"id": 1, "email": ADMIN_EMAIL, "nickname": "a aa nickonly 陳 AsCiI Ä ä %_\\",
                 "role": "admin", "status": "active", "created_at": stamp(0)}]
    for number in range(1, members + 1):
        # nickname 與 Email 各自命中並有重疊，admin 同關鍵字仍須被排除。
        email = MEMBER_EMAIL if number == 1 else f"perf01-{number}-{'emailonly' if number % 3 else 'nickonly'}@example.test"
        accounts.append({"id": number + 1, "email": email,
                         "nickname": names[(number - 1) % len(names)], "role": "member",
                         "status": "active" if number % 2 else "disabled", "created_at": stamp(number)})
    result = {"accounts": accounts, "articles": [], "comments": []}
    for kind, count in (("articles", articles), ("comments", comments)):
        for number in range(1, count + 1):
            visible = number % 4 != 0
            # 會員 2 集中擁有前 40 篇文章及第 49–96 則留言，讓 own 列表跨過每頁 20 筆；
            # 其餘作者以取模分散，留言另扣除每四則隱藏列，讓可見列表仍涵蓋多位作者。
            author = 2 if (kind == "articles" and number <= 40) or (kind == "comments" and 49 <= number <= 96) else 2 + ((number - number // 4 - 1) % 20 if kind == "comments" else number % 20)
            row = {"id": number, "author_id": author, "visibility": "visible" if visible else "hidden",
                   "restore_control": None if visible else ("admin" if number % 8 == 0 else "author"),
                   "version": 1, "created_backend": BACKENDS[number % 12],
                   "created_at": stamp(number), "updated_at": stamp(number)}
            if kind == "articles":
                row.update(title="題" * 200 if number % 11 == 0 else f"文章 {number}",
                           body="文" * 20000 if number % 13 == 0 else f"本文 {number}\n保留換行")
            else:
                row.update(article_id=1 if number <= 64 else 1 + number % 128,
                           body="言" * 2000 if number % 13 == 0 else f"留言 {number}\n保留換行")
            result[kind].append(row)
    return result


# expected_rows 從清單套用公開／擁有者／管理員規則，完全不借用受測 SQL 或回應。
def expected_rows(data, kind, actor, keyword=None, article=None):
    viewer = {"guest": None, "member": 2, "admin": 1}[actor]
    source = data["accounts" if kind == "members" else "articles" if "articles" in kind else "comments"]
    rows = []
    for item in source:
        if kind == "members":
            needle = ascii_fold((keyword or "").strip(" "))
            if item["role"] != "member" or not (needle in ascii_fold(item["nickname"]) or needle in ascii_fold(item["email"])):
                continue
            rows.append(dict(item))
            continue
        if kind.startswith("own-") and item["author_id"] != viewer:
            continue
        if kind == "public-articles" and item["visibility"] != "visible":
            continue
        if kind == "article-comments" and (item["article_id"] != article or (actor != "admin" and item["visibility"] != "visible")):
            continue
        author = data["accounts"][item["author_id"] - 1]
        row = {key: value for key, value in item.items() if key not in ("author_id", "restore_control")}
        row["author"] = {"id": author["id"], "nickname": author["nickname"]}
        row["can_restore"] = item["visibility"] == "hidden" and (actor == "admin" or (viewer == item["author_id"] and item["restore_control"] == "author"))
        rows.append(row)
    # 固定鍵排序也能找出只按 ID 或只按建立時間的錯誤實作。
    return sorted(rows, key=order_key, reverse=True)


# order_key 明示第二排序欄，供 oracle 與測試共用同一核准規則。
def order_key(row):
    return row["created_at"], row["id"]


# cases 展開固定矩陣並保存完整預期集合，案例 ID 可跨十二後端逐一追蹤。
def cases(data):
    variants = [
        ("members", "admin", "/api/members", None),
        ("public-articles", "guest", "/api/articles", None),
        ("public-articles", "member", "/api/articles", None),
        ("public-articles", "admin", "/api/articles", None),
        ("management-articles", "admin", "/api/articles", None),
        ("own-articles", "member", "/api/me/articles", None),
        ("own-comments", "member", "/api/me/comments", None),
        ("management-comments", "admin", "/api/comments", None),
        ("article-comments", "guest", "/api/articles/1/comments", 1),
        ("article-comments", "member", "/api/articles/1/comments", 1),
        ("article-comments", "admin", "/api/articles/1/comments", 1),
        ("article-comments", "member", "/api/articles/4/comments", 4),
        ("article-comments", "admin", "/api/articles/4/comments", 4),
    ]
    for kind, actor, path, article in variants:
        for index, keyword in enumerate(KEYWORDS if kind == "members" else (None,)):
            rows = expected_rows(data, kind, actor, keyword, article)
            for size in (1, 20):
                total_pages = (len(rows) + size - 1) // size
                # 每個 keyword 都做 1/20 首頁；20 筆分頁另覆蓋所有未篩選列表及高／低／零命中代表。
                pages = {1}
                if size == 20 and (keyword is None or keyword in ("a", "needlelong", "no-such-perf01-match")):
                    pages |= {2, max(1, total_pages), total_pages + 1, 999999}
                for page in sorted(pages):
                    query = [("page", page), ("per_page", size)]
                    if kind == "management-articles":
                        query.append(("filters[scope]", "management"))
                    if keyword is not None:
                        query.append(("filters[keyword]", keyword))
                    identity = f"{kind}/{actor}/{article or 0}/keyword-{index}/{size}/{page}"
                    yield {"id": identity, "actor": actor, "kind": kind, "path": path + "?" + urlencode(query),
                           "page": page, "per_page": size, "status": 200, "expected_ids": [row["id"] for row in rows],
                           "expected": {"list": rows[(page - 1) * size:page * size], "pagination": {
                               "page": page, "per_page": size, "total": len(rows), "total_pages": total_pages}}}
    # 拒絕路徑刻意混用格式錯誤，驗出認證／角色先於格式的契約。
    for actor, path, status, code, errors in (
        ("guest", "/api/members?page=bad&per_page=20", 401, "E0002", None),
        ("member", "/api/members?page=bad&per_page=20", 403, "E0003", None),
        ("guest", "/api/articles?page=bad&per_page=20&filters[scope]=management", 401, "E0002", None),
        ("member", "/api/articles?page=bad&per_page=20&filters[scope]=management", 403, "E0003", None),
        ("admin", "/api/members?page=1&per_page=20&unknown=1", 422, "E0001", {"unknown": ["不支援此欄位"]}),
        ("admin", "/api/members?page=bad&per_page=20", 422, "E0001", {"page": [INVALID]}),
        ("admin", "/api/members?page=0&per_page=20", 422, "E0001", {"page": [INVALID]}),
        ("admin", "/api/members?page=1&per_page=101", 422, "E0001", {"per_page": [INVALID]}),
        ("admin", "/api/members?page=1&per_page=0", 422, "E0001", {"per_page": [INVALID]}),
        ("admin", "/api/members?page=1&per_page=20&filters[keyword]=a&filters[keyword]=aa", 422, "E0001", {"filters.keyword": ["此參數不可重複"]}),
        ("admin", "/api/members?page=1&per_page=20&filters[keyword]=%00", 422, "E0001", {"filters.keyword": [INVALID]}),
        ("guest", "/api/articles/4/comments?page=1&per_page=20", 404, "E0004", None),
    ):
        yield {"id": "invalid/" + actor + "/" + path, "actor": actor, "path": path, "status": status,
               "error_code": code, "errors": errors, "expected_ids": []}


# require_response 讓最佳化模式也執行 oracle，失敗只回固定契約診斷。
def require_response(condition, message):
    if not condition:
        raise AssertionError(message)


# verify_response 檢查完整欄位和值；log_id 僅驗格式，不把動態識別字當固定業務值。
def verify_response(case, status, body):
    require_response(status == case["status"], "HTTP status 與契約不符")
    require_response(isinstance(body, dict), "HTTP envelope 必須為 JSON object")
    if status == 200:
        require_response(body.keys() == {"success", "message", "data"}, "成功 envelope 欄位不符")
        require_response(body.get("success") is True and body.get("message") == "OK", "成功 envelope 不符")
        # 依 JSON 型別比較，避免 Python 把 True、1 與 1.0 視為同一個分頁值。
        require_response(json.dumps(body["data"], sort_keys=True) == json.dumps(case["expected"], sort_keys=True), "完整列表／分頁與 fixture oracle 不符")
    else:
        keys = {"success", "message", "error_code", "log_id"}
        if case["errors"] is not None:
            keys.add("errors")
        require_response(body.keys() == keys, "錯誤 envelope 欄位不符")
        require_response(isinstance(body.get("log_id"), str) and body["log_id"], "缺少 log_id")
        code = case["error_code"]
        require_response(body.get("success") is False and body.get("error_code") == code and body.get("message") == MESSAGES[code], "錯誤 envelope 不符")
        require_response(body.get("errors") == case["errors"], "驗證 errors 或非驗證錯誤欄位不符")
