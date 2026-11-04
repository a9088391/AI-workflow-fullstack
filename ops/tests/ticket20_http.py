"""Ticket 20 隔離 HTTP、持久資料與列鎖驗證；僅供 ticket20_test 容器執行。"""

import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row


PROJECT = "ticket20_test"
FLASK = "http://flask:8000"
LARAVEL = "http://laravel:8000"
PASSWORD = "Abc1234"
ARTICLE_COLUMNS = (
    "articles.id, articles.author_id, articles.title, articles.body, articles.visibility, "
    "articles.restore_control, articles.version, articles.created_backend, "
    "articles.created_at, articles.updated_at, accounts.nickname"
)
COMMENT_COLUMNS = (
    "comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, "
    "comments.restore_control, comments.version, comments.created_backend, "
    "comments.created_at, comments.updated_at, accounts.nickname"
)


# 固定服務目標不得經 HTTP redirect 改投其他主機或路徑。
class NoRedirect(HTTPRedirectHandler):
    # 遇到重新導向交回錯誤回應，由呼叫端停止，不轉送認證或寫入資料。
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
    "E0102": "此帳號已停用，請聯絡系統管理員",
    "E0201": "內容已更新，請確認最新內容後再送出",
}


# 只允許本腳本產生的受控診斷出現在錯誤輸出。
class CheckFailure(Exception):
    pass


# 不使用可被 -O 關閉的 assert，讓守衛與副作用檢查在所有執行方式都有效。
def must(condition, reason):
    if not condition:
        raise CheckFailure("Ticket 20 驗證失敗：" + reason)


# 僅連至隔離資料庫的固定 owner 身分；密碼只從執行環境讀取。
def owner_connection():
    return psycopg.connect(
        host="db", port=5432, dbname="articles", user="articles_owner",
        password=os.environ["DB_OWNER_PASSWORD"], row_factory=dict_row,
        connect_timeout=3, options="-c statement_timeout=20000",
    )


# 任何寫入前先核對專案與資料庫身分，再探測同網路兩個固定 API。
def guard():
    must(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT, "錯誤 Compose 專案")
    must(os.environ.get("DB_HOST", "db") == "db", "資料庫主機不符")
    must(os.environ.get("DB_DATABASE", "articles") == "articles", "資料庫名稱不符")
    must(os.environ.get("DB_OWNER_USER", "articles_owner") == "articles_owner", "owner 身分不符")
    must(bool(os.environ.get("DB_OWNER_PASSWORD")) and bool(os.environ.get("DB_APP_PASSWORD")), "隔離資料庫密碼未提供")
    # 連線本身也驗證憑證及實際資料庫，避免只信任環境變數。
    try:
        with owner_connection() as connection:
            row = connection.execute("SELECT current_database() AS db, current_user AS role").fetchone()
            must(row == {"db": "articles", "role": "articles_owner"}, "實際資料庫身分不符")
            tables = connection.execute(
                "SELECT count(*) AS n FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('accounts', 'login_sessions', 'articles', 'comments')"
            ).fetchone()["n"]
            must(tables == 4, "共用資料表不完整")
    except psycopg.Error:
        raise CheckFailure("Ticket 20 驗證失敗：隔離資料庫身分查核失敗") from None
    for base in (FLASK, LARAVEL):
        try:
            with OPENER.open(base + "/up", timeout=5) as response:
                must(response.status == 200 and response.read() == b"OK", "API 健康目標不符")
        except (HTTPError, URLError, TimeoutError):
            raise CheckFailure("Ticket 20 驗證失敗：固定 API 目標不可用") from None


# 使用固定 JSON envelope 呼叫 API；不記錄請求本文、憑證或錯誤原始回應。
def call(base, method, path, body=None, token=None):
    headers = {"Accept": "application/json"}
    raw = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(base + path, data=raw, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=20) as response:
            status, payload = response.status, response.read()
    except HTTPError as response:
        status, payload = response.code, response.read()
    except (URLError, TimeoutError):
        raise CheckFailure("Ticket 20 驗證失敗：API 呼叫結果未知，停止重送") from None
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError):
        raise CheckFailure("Ticket 20 驗證失敗：API 未回共同 JSON 格式") from None
    must(isinstance(result, dict), "API envelope 型態錯誤")
    return status, result


# 同時檢查狀態、固定文案、必要欄位與成功空資料，避免只看 HTTP 代碼。
def expect(result, status, code=None, errors=None, empty=False):
    actual_status, payload = result
    must(actual_status == status, f"HTTP 狀態不符，預期 {status}，實際 {actual_status}")
    if code is None:
        must(set(payload) == {"success", "message", "data"}, "成功 envelope 欄位不符")
        must(payload["success"] is True and payload["message"] == "OK", "成功 envelope 值不符")
        must(isinstance(payload["data"], dict), "成功 data 必須為 object")
        if empty:
            must(payload["data"] == {}, "寫入成功資料應為空物件")
    else:
        expected = {"success", "message", "error_code", "log_id"}
        if errors is not None:
            expected.add("errors")
        must(set(payload) == expected, "失敗 envelope 欄位不符")
        must(payload["success"] is False and payload["error_code"] == code, "失敗代碼不符")
        must(payload["message"] == MESSAGES[code], "失敗固定文案不符")
        must(isinstance(payload["log_id"], str) and bool(payload["log_id"]), "失敗缺少 log_id")
        if errors is not None:
            must(payload["errors"] == errors, "欄位錯誤明細不符")
    return payload.get("data")


# 每次執行都建立唯一帳號，避免既有資料影響精確列表與重跑。
def member(base, namespace, label):
    email = f"t20-{namespace}-{label}@example.test"
    expect(call(base, "POST", "/api/auth/register", {
        "email": email, "password": PASSWORD, "nickname": label,
    }), 201, empty=True)
    data = expect(call(base, "POST", "/api/auth/login", {
        "email": email, "password": PASSWORD,
    }), 200)
    must(isinstance(data.get("access_token"), str), "登入未簽發憑證")
    return {"id": data["user"]["id"], "email": email, "token": data["access_token"]}


# 新增操作不回 ID；以唯一作者及測試文字從已提交資料查回，避免猜測序列值。
def created(table, author_id, column, value):
    must(table in ("articles", "comments") and column in ("title", "body"), "fixture 查詢欄位不受允許")
    with owner_connection() as connection:
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE author_id = %s AND {column} = %s", (author_id, value)
        ).fetchall()
    must(len(rows) == 1, "新增資料未唯一提交")
    return rows[0]


# 只比對指定資料列全部持久欄位，捕捉同值與拒絕操作的隱性副作用。
def snapshot(table, item_id):
    must(table in ("articles", "comments"), "快照資料表不受允許")
    with owner_connection() as connection:
        row = connection.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,)).fetchone()
    must(row is not None, "快照目標不存在")
    return row


# 依核准共用欄位組完整資源，精確比對跨後端序列化而不複製實作查詢。
def resource(table, row, nickname, can_restore=False):
    taipei = ZoneInfo("Asia/Taipei")
    # 將資料庫時區值轉成 API 使用的台北業務時間，避免依容器時區比對。
    def stamp(value):
        return value.astimezone(taipei).strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "id": row["id"], "author": {"id": row["author_id"], "nickname": nickname},
        "version": row["version"], "created_backend": row["created_backend"],
        "created_at": stamp(row["created_at"]), "updated_at": stamp(row["updated_at"]),
        "visibility": row["visibility"], "can_restore": can_restore,
        "body": row["body"],
    }
    if table == "articles":
        data["title"] = row["title"]
    else:
        data["article_id"] = row["article_id"]
    return data


# 寫入若回成功，必須維持共同空 data 形狀。
def write(base, method, path, body, token, status=200, code=None, errors=None):
    return expect(call(base, method, path, body, token), status, code, errors, empty=code is None)


# 以精確 ID、總數、排序及超頁結果查個人列表；隱藏內容仍須出現。
def check_list(kind, owner, expected_ids):
    path = "/api/me/" + kind
    data = expect(call(FLASK, "GET", path + "?page=1&per_page=20", token=owner["token"]), 200)
    must([item["id"] for item in data["list"]] == expected_ids, "個人列表 ID 或排序不符")
    must(data["pagination"] == {
        "page": 1, "per_page": 20, "total": len(expected_ids), "total_pages": 1 if expected_ids else 0,
    }, "個人列表精確計數不符")
    table = "articles" if kind == "articles" else "comments"
    with owner_connection() as connection:
        states = {row["id"]: row["visibility"] for row in connection.execute(
            f"SELECT id, visibility FROM {table} WHERE author_id = %s", (owner["id"],)
        ).fetchall()}
    for item in data["list"]:
        must(item["visibility"] == states[item["id"]], "個人列表隱藏狀態不符")
    far = expect(call(FLASK, "GET", path + "?page=99&per_page=20", token=owner["token"]), 200)
    must(far["list"] == [] and far["pagination"] == {
        "page": 99, "per_page": 20, "total": len(expected_ids), "total_pages": 1 if expected_ids else 0,
    }, "超頁列表仍須保留實際總數")


# 文章與留言共用版本、隱藏先後及接管規則，但各端點都走真實 HTTP。
def lifecycle(kind, item_id, author, outsider, admin, original):
    table = "articles" if kind == "article" else "comments"
    path = f"/api/{table}/{item_id}"
    current = snapshot(table, item_id)
    must(current["version"] == 1 and current["created_backend"] == original, "新內容初始來源或版本不符")
    edit = {"title": current["title"], "body": current["body"] + "\r\n修改", "version": 1} if kind == "article" else {
        "body": current["body"] + "\r\n修改", "version": 1,
    }
    # 在有權讀取的公開目標上，非作者先於版本收到 403；不合法版本先於查目標收到 422。
    write(FLASK, "PUT", path, edit, outsider["token"], 403, "E0003")
    write(FLASK, "PUT", path, edit, admin["token"], 403, "E0003")
    invalid = edit | {"version": True}
    write(FLASK, "PUT", path, invalid, author["token"], 422, "E0001", {"version": ["欄位型態不正確"]})
    write(FLASK, "PUT", path, edit, author["token"])
    changed = snapshot(table, item_id)
    must(changed["version"] == 2 and changed["body"] == edit["body"], "實際編修未增版或未保留文字")
    must(changed["created_backend"] == original and changed["author_id"] == author["id"], "建立來源或作者被改寫")
    write(FLASK, "PUT", path, edit, author["token"], 409, "E0201")
    same = edit | {"version": 2}
    before = snapshot(table, item_id)
    write(FLASK, "PUT", path, same, author["token"])
    must(snapshot(table, item_id) == before, "同值編修改了版本或時間")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "hidden", "version": 2}, author["token"])
    hidden = snapshot(table, item_id)
    must(hidden["visibility"] == "hidden" and hidden["restore_control"] == "author" and hidden["version"] == 3,
         "作者下架狀態不符")
    write(FLASK, "PUT", path, same, outsider["token"], 404, "E0004")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "visible", "version": 1}, outsider["token"], 404, "E0004")
    write(FLASK, "PUT", path, same, admin["token"], 403, "E0003")
    # 作者再次下架同值不增版；管理員對他人內容再次下架會接管恢復權。
    write(FLASK, "PUT", path + "/visibility", {"visibility": "hidden", "version": 3}, author["token"])
    must(snapshot(table, item_id) == hidden, "同值下架改了資料")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "hidden", "version": 3}, admin["token"])
    taken = snapshot(table, item_id)
    must(taken["restore_control"] == "admin" and taken["version"] == 4, "管理員未接管恢復權")
    owner_read = expect(call(FLASK, "GET", path, token=author["token"]), 200)
    must(owner_read["can_restore"] is False and owner_read["version"] == 4, "作者取得不當恢復資格")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "visible", "version": 1}, author["token"], 403, "E0003")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "visible", "version": 4}, author["token"], 403, "E0003")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "visible", "version": 3}, admin["token"], 409, "E0201")
    write(FLASK, "PUT", path + "/visibility", {"visibility": "visible", "version": 4}, admin["token"])
    restored = snapshot(table, item_id)
    must(restored["visibility"] == "visible" and restored["restore_control"] is None and restored["version"] == 5,
         "管理員恢復結果不符")
    return path


# 真實端點也須遵守先認證後參數驗證，且無效輸入不得觸碰已存在內容。
def validation_cases(article_id, parent):
    path = f"/api/articles/{article_id}"
    body = {"title": "任意", "body": "內文", "version": 1}
    before = snapshot("articles", article_id)
    expect(call(FLASK, "PUT", "/api/articles/bad?x=1", body), 401, "E0002")
    write(FLASK, "PUT", "/api/articles/bad", body, parent["token"], 422, "E0001",
          {"article_id": ["欄位格式或範圍不正確"]})
    write(FLASK, "PUT", path + "?x=1", body, parent["token"], 422, "E0001",
          {"x": ["不支援此欄位"]})
    write(FLASK, "PUT", path, body | {"created_backend": "gin"}, parent["token"], 422, "E0001",
          {"created_backend": ["不支援此欄位"]})
    write(FLASK, "POST", "/api/articles", {"title": "\u3000", "body": "內文"}, parent["token"],
          422, "E0001", {"title": ["欄位格式或範圍不正確"]})
    expect(call(FLASK, "GET", "/api/me/articles?page=1&page=2&per_page=20", token=parent["token"]),
           422, "E0001", {"page": ["此參數不可重複"]})
    must(snapshot("articles", article_id) == before, "驗證失敗改寫了文章")


# 留言可由另一後端讀取來源，JWT 則只由簽發後端接受。
def cross_backend(namespace, parent, commenter, laravel_author):
    # 兩個方向各建立一篇，再由另一後端讀取共享內容與建立來源。
    title = "Flask-" + namespace
    write(FLASK, "POST", "/api/articles", {"title": title, "body": "Flask 原文"}, parent["token"], 201)
    flask_article = created("articles", parent["id"], "title", title)
    must(flask_article["created_backend"] == "flask", "Flask 文章來源未持久化")
    from_laravel = expect(call(LARAVEL, "GET", f"/api/articles/{flask_article['id']}"), 200)
    must(from_laravel == resource("articles", flask_article, "parent"), "Laravel 未完整讀出 Flask 文章")

    laravel_title = "Laravel-" + namespace
    write(LARAVEL, "POST", "/api/articles", {"title": laravel_title, "body": "Laravel 原文"},
          laravel_author["token"], 201)
    laravel_article = created("articles", laravel_author["id"], "title", laravel_title)
    must(laravel_article["created_backend"] == "laravel", "Laravel 文章來源未持久化")
    from_flask = expect(call(FLASK, "GET", f"/api/articles/{laravel_article['id']}"), 200)
    must(from_flask == resource("articles", laravel_article, "laravel"), "Flask 未完整讀出 Laravel 文章")

    # 留言同樣交叉建立與讀取；各自先取得簽發後端的登入憑證。
    flask_body = "Flask 留言-" + namespace
    write(FLASK, "POST", f"/api/articles/{flask_article['id']}/comments", {"body": flask_body},
          commenter["token"], 201)
    flask_comment = created("comments", commenter["id"], "body", flask_body)
    commenter_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {
        "email": commenter["email"], "password": PASSWORD,
    }), 200)["access_token"]
    read_comment = expect(call(LARAVEL, "GET", f"/api/comments/{flask_comment['id']}", token=commenter_laravel), 200)
    must(read_comment == resource("comments", flask_comment, "commenter"), "Laravel 未完整讀出 Flask 留言")
    laravel_body = "Laravel 留言-" + namespace
    write(LARAVEL, "POST", f"/api/articles/{flask_article['id']}/comments", {"body": laravel_body},
          commenter_laravel, 201)
    laravel_comment = created("comments", commenter["id"], "body", laravel_body)
    read_comment = expect(call(FLASK, "GET", f"/api/comments/{laravel_comment['id']}", token=commenter["token"]), 200)
    must(read_comment == resource("comments", laravel_comment, "commenter"), "Flask 未完整讀出 Laravel 留言")
    return flask_article, laravel_article, flask_comment, laravel_comment


# 觀察 PostgreSQL 真正的 Lock wait，避免用固定延遲假定請求已進入交易。
def wait_for_lock(fragment, count=1):
    deadline = time.monotonic() + 8
    with owner_connection() as observer:
        observer.autocommit = True
        while time.monotonic() < deadline:
            waiting = observer.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = 'articles' "
                "AND usename = 'articles_app' AND state = 'active' "
                "AND wait_event_type = 'Lock' AND position(%s in query) > 0",
                (fragment,),
            ).fetchone()["n"]
            if waiting >= count:
                return
            time.sleep(0.05)
    raise CheckFailure("Ticket 20 驗證失敗：HTTP 未等待指定資料列鎖")


# fixture 先持鎖，觀察各 HTTP 等鎖後提交指定轉移；失敗時連線關閉會回滾。
def locked_race(lock_sql, lock_params, requests, wait_fragment, mutation=None):
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        with owner_connection() as hold:
            hold.execute(lock_sql, lock_params).fetchone()
            futures = [pool.submit(call, *request) for request in requests]
            wait_for_lock(wait_fragment, len(requests))
            if mutation is not None:
                mutation(hold)
            hold.commit()
        return [future.result(timeout=25) for future in futures]


# 父文章先下架，等待中的留言修改須重查父狀態且不得留下副作用。
def parent_hide_race(article_id, comment_id, parent, commenter):
    before = snapshot("comments", comment_id)
    result = locked_race(
        "SELECT id FROM articles WHERE id = %s FOR UPDATE", (article_id,),
        [(FLASK, "PUT", f"/api/comments/{comment_id}", {"body": "等鎖不可改", "version": before["version"]}, commenter["token"])],
        "FROM articles WHERE id",
        # callback 父文鎖內先提交下架，讓排隊中的留言修改必須讀到最新父狀態。
        lambda hold: hold.execute(
            "UPDATE articles SET visibility = 'hidden', restore_control = 'author', "
            "version = version + 1, updated_at = clock_timestamp() WHERE id = %s", (article_id,)
        ),
    )[0]
    expect(result, 403, "E0003")
    must(snapshot("comments", comment_id) == before, "父文章先下架仍修改了留言")
    article = snapshot("articles", article_id)
    write(FLASK, "PUT", f"/api/articles/{article_id}/visibility",
          {"visibility": "visible", "version": article["version"]}, parent["token"])


# 同版本兩個寫入在帳號鎖後依序提交，只能有一個成功且另一個 409。
def version_race(table, item_id, author):
    before = snapshot(table, item_id)
    path = f"/api/{table}/{item_id}"
    if table == "articles":
        bodies = [
            {"title": "競爭 A", "body": "A", "version": before["version"]},
            {"title": "競爭 B", "body": "B", "version": before["version"]},
        ]
    else:
        bodies = [{"body": "競爭 A", "version": before["version"]},
                  {"body": "競爭 B", "version": before["version"]}]
    results = locked_race(
        "SELECT id FROM accounts WHERE id = %s FOR UPDATE", (author["id"],),
        [(FLASK, "PUT", path, body, author["token"]) for body in bodies],
        "FROM accounts WHERE id",
    )
    must(sorted(result[0] for result in results) == [200, 409], "同版本競爭未產生單一勝者")
    for result in results:
        expect(result, result[0], "E0201" if result[0] == 409 else None, empty=result[0] == 200)
    after = snapshot(table, item_id)
    must(after["version"] == before["version"] + 1 and after["body"] in ("A", "B", "競爭 A", "競爭 B"),
         "同版本競爭持久結果不符")


# 修改先持父文章鎖等留言，後送下架也須等父文章；兩筆完成的效果都保留。
def comment_first_race(article_id, comment_id, parent, commenter):
    article_before = snapshot("articles", article_id)
    comment_before = snapshot("comments", comment_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with owner_connection() as hold:
            hold.execute("SELECT id FROM comments WHERE id = %s FOR UPDATE", (comment_id,)).fetchone()
            edit = pool.submit(call, FLASK, "PUT", f"/api/comments/{comment_id}",
                               {"body": "編修先完成", "version": comment_before["version"]}, commenter["token"])
            wait_for_lock("FROM comments WHERE id")
            hide = pool.submit(call, FLASK, "PUT", f"/api/articles/{article_id}/visibility",
                               {"visibility": "hidden", "version": article_before["version"]}, parent["token"])
            wait_for_lock("FROM articles WHERE id")
            hold.commit()
        edit_result, hide_result = edit.result(timeout=25), hide.result(timeout=25)
    expect(edit_result, 200, empty=True)
    expect(hide_result, 200, empty=True)
    comment_after = snapshot("comments", comment_id)
    article_after = snapshot("articles", article_id)
    must(comment_after["body"] == "編修先完成" and comment_after["version"] == comment_before["version"] + 1,
         "先完成的留言編修遺失")
    must(article_after["visibility"] == "hidden" and article_after["version"] == article_before["version"] + 1,
         "後完成的文章下架遺失")
    write(FLASK, "PUT", f"/api/articles/{article_id}/visibility",
          {"visibility": "visible", "version": article_after["version"]}, parent["token"])


# 停用與撤銷先提交，等待帳號鎖的寫入須按最新狀態拒絕且不改資料。
def identity_races(namespace):
    # 停用和 session 撤銷同交易提交，等待帳號鎖的文章編修不得發生。
    disabled = member(FLASK, namespace, "disable")
    title = "停用競爭-" + namespace
    write(FLASK, "POST", "/api/articles", {"title": title, "body": "原文"}, disabled["token"], 201)
    article = created("articles", disabled["id"], "title", title)
    before = snapshot("articles", article["id"])
    result = locked_race(
        "SELECT id FROM accounts WHERE id = %s FOR UPDATE", (disabled["id"],),
        [(FLASK, "PUT", f"/api/articles/{article['id']}",
          {"title": title, "body": "停用不可改", "version": 1}, disabled["token"])],
        "FROM accounts WHERE id",
        # callback 帳號鎖內同時停用與撤銷 session，確認等待中的寫入不能沿用舊身分。
        lambda hold: (
            hold.execute("UPDATE accounts SET status = 'disabled' WHERE id = %s", (disabled["id"],)),
            hold.execute("UPDATE login_sessions SET revoked_at = clock_timestamp() "
                         "WHERE account_id = %s AND revoked_at IS NULL", (disabled["id"],)),
        ),
    )[0]
    expect(result, 403, "E0102")
    must(snapshot("articles", article["id"]) == before, "停用後寫入仍改了文章")

    # 另一組獨立帳號只撤銷 session，確認留言編修回登入逾期而非停用。
    revoked = member(FLASK, namespace, "revoke")
    parent_title = "撤銷父文章-" + namespace
    write(FLASK, "POST", "/api/articles", {"title": parent_title, "body": "原文"}, revoked["token"], 201)
    parent = created("articles", revoked["id"], "title", parent_title)
    comment_body = "撤銷留言-" + namespace
    write(FLASK, "POST", f"/api/articles/{parent['id']}/comments", {"body": comment_body}, revoked["token"], 201)
    comment = created("comments", revoked["id"], "body", comment_body)
    before = snapshot("comments", comment["id"])
    result = locked_race(
        "SELECT id FROM accounts WHERE id = %s FOR UPDATE", (revoked["id"],),
        [(FLASK, "PUT", f"/api/comments/{comment['id']}",
          {"body": "撤銷不可改", "version": 1}, revoked["token"])],
        "FROM accounts WHERE id",
        # callback 帳號鎖內只撤銷 session，區分登入逾期與停用錯誤。
        lambda hold: hold.execute(
            "UPDATE login_sessions SET revoked_at = clock_timestamp() "
            "WHERE account_id = %s AND revoked_at IS NULL", (revoked["id"],)
        ),
    )[0]
    expect(result, 401, "E0002")
    must(snapshot("comments", comment["id"]) == before, "撤銷後寫入仍改了留言")


# 下架父文章不能授予讀取其他留言的能力，但自己留言仍可獨立管理可見性。
def hidden_parent_rules(article, comment, parent, commenter, outsider, admin):
    article_path = f"/api/articles/{article['id']}"
    comment_path = f"/api/comments/{comment['id']}"
    article_before = snapshot("articles", article["id"])
    comment_before = snapshot("comments", comment["id"])
    with owner_connection() as connection:
        count_before = connection.execute(
            "SELECT count(*) AS n FROM comments WHERE article_id = %s", (article["id"],)
        ).fetchone()["n"]
    write(FLASK, "PUT", article_path + "/visibility",
          {"visibility": "hidden", "version": article_before["version"]}, parent["token"])
    detail = expect(call(FLASK, "GET", comment_path, token=commenter["token"]), 200)
    must(detail["article_id"] == article["id"] and "title" not in detail,
         "獨立留言詳情洩漏父文章內文")
    expect(call(FLASK, "GET", comment_path, token=outsider["token"]), 404, "E0004")
    write(FLASK, "POST", article_path + "/comments", {"body": "禁止新增"}, outsider["token"], 404, "E0004")
    write(FLASK, "POST", article_path + "/comments", {"body": "禁止新增"}, parent["token"], 403, "E0003")
    write(FLASK, "POST", article_path + "/comments", {"body": "禁止新增"}, admin["token"], 403, "E0003")
    with owner_connection() as connection:
        count_after = connection.execute(
            "SELECT count(*) AS n FROM comments WHERE article_id = %s", (article["id"],)
        ).fetchone()["n"]
    must(count_after == count_before, "父文章隱藏後仍新增了留言")
    edit = {"body": "父文章隱藏不可改", "version": comment_before["version"]}
    write(FLASK, "PUT", comment_path, edit, commenter["token"], 403, "E0003")
    write(FLASK, "PUT", comment_path, edit, outsider["token"], 404, "E0004")
    must(snapshot("comments", comment["id"]) == comment_before, "隱藏父文章期間留言被改寫")
    write(FLASK, "PUT", comment_path + "/visibility",
          {"visibility": "hidden", "version": comment_before["version"]}, commenter["token"])
    hidden = snapshot("comments", comment["id"])
    must(hidden["visibility"] == "hidden" and hidden["version"] == comment_before["version"] + 1,
         "父文章隱藏時自己留言未能下架")
    own_detail = expect(call(FLASK, "GET", comment_path, token=commenter["token"]), 200)
    must(own_detail["can_restore"] is True and "title" not in own_detail,
         "隱藏留言詳情或恢復資格不符")
    write(FLASK, "PUT", comment_path + "/visibility",
          {"visibility": "visible", "version": hidden["version"]}, commenter["token"])
    expect(call(FLASK, "GET", article_path + "/comments?page=1&per_page=20", token=outsider["token"]),
           404, "E0004")
    # 再下架自己留言，供個人列表驗證個別下架與父文章下架同時存在。
    restored = snapshot("comments", comment["id"])
    write(FLASK, "PUT", comment_path + "/visibility",
          {"visibility": "hidden", "version": restored["version"]}, commenter["token"])


# 用已提交 fixture 的完整 ID 集合決定預期排序，不讓舊資料或無關作者混入。
def ordered_ids(table, author_id, expected):
    must(table in ("articles", "comments"), "列表資料表不受允許")
    with owner_connection() as connection:
        rows = connection.execute(
            f"SELECT id FROM {table} WHERE author_id = %s ORDER BY created_at DESC, id DESC", (author_id,)
        ).fetchall()
    ids = [row["id"] for row in rows]
    must(len(ids) == len(expected) and set(ids) == set(expected), "隔離作者 fixture 集合不符")
    return ids


# 管理員操作自己的內容仍按作者下架，恢復權為 author 而非 admin 接管。
def admin_own_rule(namespace, admin):
    title = "管理員自有-" + namespace
    write(FLASK, "POST", "/api/articles", {"title": title, "body": "自有內文"}, admin["token"], 201)
    article = created("articles", admin["id"], "title", title)
    write(FLASK, "PUT", f"/api/articles/{article['id']}/visibility",
          {"visibility": "hidden", "version": 1}, admin["token"])
    must(snapshot("articles", article["id"])["restore_control"] == "author", "管理員自有文章恢復權不符")
    write(FLASK, "PUT", f"/api/articles/{article['id']}/visibility",
          {"visibility": "visible", "version": 2}, admin["token"])
    body = "管理員自有留言-" + namespace
    write(FLASK, "POST", f"/api/articles/{article['id']}/comments", {"body": body}, admin["token"], 201)
    comment = created("comments", admin["id"], "body", body)
    # 管理員自己的留言也不能在父文章下架時改內文。
    write(FLASK, "PUT", f"/api/articles/{article['id']}/visibility",
          {"visibility": "hidden", "version": 3}, admin["token"])
    write(FLASK, "PUT", f"/api/comments/{comment['id']}",
          {"body": "不可改", "version": 1}, admin["token"], 403, "E0003")
    write(FLASK, "PUT", f"/api/articles/{article['id']}/visibility",
          {"visibility": "visible", "version": 4}, admin["token"])
    write(FLASK, "PUT", f"/api/comments/{comment['id']}/visibility",
          {"visibility": "hidden", "version": 1}, admin["token"])
    must(snapshot("comments", comment["id"])["restore_control"] == "author", "管理員自有留言恢復權不符")
    write(FLASK, "PUT", f"/api/comments/{comment['id']}/visibility",
          {"visibility": "visible", "version": 2}, admin["token"])


# 實際執行計數、個人列表及父文章留言查詢計畫；只記方案及時間，不造門檻。
def query_plans(parent_id, commenter_id, article_id):
    cases = (
        ("my_articles_count", "SELECT count(*) FROM articles WHERE author_id = %s", (parent_id,)),
        ("my_articles_page", "SELECT " + ARTICLE_COLUMNS + " FROM articles JOIN accounts ON accounts.id = articles.author_id "
         "WHERE articles.author_id = %s ORDER BY articles.created_at DESC, articles.id DESC LIMIT %s OFFSET %s",
         (parent_id, 20, 0)),
        ("my_comments_count", "SELECT count(*) FROM comments WHERE author_id = %s", (commenter_id,)),
        ("my_comments_page", "SELECT " + COMMENT_COLUMNS + " FROM comments JOIN accounts ON accounts.id = comments.author_id "
         "WHERE comments.author_id = %s ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s",
         (commenter_id, 20, 0)),
        ("article_comments_count", "SELECT count(*) FROM comments JOIN accounts ON accounts.id = comments.author_id "
         "WHERE comments.article_id = %s AND comments.visibility = 'visible'", (article_id,)),
        ("article_comments_page", "SELECT " + COMMENT_COLUMNS + " FROM comments JOIN accounts ON accounts.id = comments.author_id "
         "WHERE comments.article_id = %s AND comments.visibility = 'visible' "
         "ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s", (article_id, 20, 0)),
    )
    with owner_connection() as connection:
        article_count = connection.execute("SELECT count(*) AS n FROM articles WHERE author_id = %s", (parent_id,)).fetchone()["n"]
        comment_count = connection.execute("SELECT count(*) AS n FROM comments WHERE author_id = %s", (commenter_id,)).fetchone()["n"]
        must(2 <= article_count <= 20 and 2 <= comment_count <= 20, "查詢計畫 fixture 筆數不符")
        for label, query, params in cases:
            plan = connection.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params).fetchone()["QUERY PLAN"]
            if isinstance(plan, str):
                plan = json.loads(plan)
            print(json.dumps({"case": label, "explain": plan[0]}, ensure_ascii=False))


# 先完成端點與持久化，再以獨立新 fixture 覆蓋互鎖與帳號撤銷競爭。
def run():
    guard()
    if len(sys.argv) == 2 and sys.argv[1] == "--guard-only":
        print("PASS ticket20_test read-only guard")
        return
    must(len(sys.argv) == 1, "不支援此參數")
    namespace = secrets.token_hex(6)
    parent = member(FLASK, namespace, "parent")
    commenter = member(FLASK, namespace, "commenter")
    outsider = member(FLASK, namespace, "outsider")
    admin = member(FLASK, namespace, "admin")
    laravel_author = member(LARAVEL, namespace, "laravel")
    # 管理員測資只在獨立資料庫升權；新登入確保伺服器讀到目前角色。
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET role = 'admin' WHERE id = %s", (admin["id"],))
    admin_login = expect(call(FLASK, "POST", "/api/auth/login", {
        "email": admin["email"], "password": PASSWORD,
    }), 200)
    must(admin_login["user"]["role"] == "admin", "管理員角色未生效")
    admin["token"] = admin_login["access_token"]

    article, _, comment, laravel_comment = cross_backend(namespace, parent, commenter, laravel_author)
    validation_cases(article["id"], parent)
    lifecycle("article", article["id"], parent, outsider, admin, "flask")
    lifecycle("comment", comment["id"], commenter, outsider, admin, "flask")
    hidden_parent_rules(article, comment, parent, commenter, outsider, admin)
    admin_own_rule(namespace, admin)
    second_title = "我的第二篇-" + namespace
    write(FLASK, "POST", "/api/articles", {"title": second_title, "body": "第二篇"}, parent["token"], 201)
    second_article = created("articles", parent["id"], "title", second_title)
    check_list("articles", parent, ordered_ids("articles", parent["id"], {article["id"], second_article["id"]}))
    check_list("comments", commenter, ordered_ids("comments", commenter["id"], {comment["id"], laravel_comment["id"]}))
    must(expect(call(FLASK, "GET", f"/api/me/comments?page=1&per_page=20", token=commenter["token"]), 200)
         ["list"][0]["article_id"] == article["id"], "個人留言列表父 ID 不符")
    print("PASS Ticket20 HTTP、跨後端來源、隱藏與精確列表")

    # 競爭案例另建可寫父子內容，不重用上段刻意保持隱藏的列表 fixture。
    race_title = "競爭文章-" + namespace
    write(FLASK, "POST", "/api/articles", {"title": race_title, "body": "原文"}, parent["token"], 201)
    race_article = created("articles", parent["id"], "title", race_title)
    race_body = "競爭留言-" + namespace
    write(FLASK, "POST", f"/api/articles/{race_article['id']}/comments", {"body": race_body}, commenter["token"], 201)
    race_comment = created("comments", commenter["id"], "body", race_body)
    version_race("articles", race_article["id"], parent)
    version_race("comments", race_comment["id"], commenter)
    parent_hide_race(race_article["id"], race_comment["id"], parent, commenter)
    comment_first_race(race_article["id"], race_comment["id"], parent, commenter)
    identity_races(namespace)
    print("PASS Ticket20 父子、版本、停用與撤銷列鎖競爭")
    query_plans(parent["id"], commenter["id"], race_article["id"])
    print("PASS Ticket20 查詢計畫已記錄")


# 只顯示受控診斷；憑證、密碼及原始 HTTP／SQL 例外不寫到標準輸出。
if __name__ == "__main__":
    try:
        run()
    except CheckFailure as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("Ticket 20 驗證失敗：未預期 " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
