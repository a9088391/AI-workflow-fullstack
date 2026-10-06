"""Ticket 23：只在 ticket20_test 測 FastAPI 作者內容與跨後端讀取。"""

import json
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

from ticket22_http import (CheckFailure, FASTAPI, LARAVEL, OPENER, call, guard, login,
                           member, owner_connection, row)

FLASK = "http://flask:8000"
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
    "E0005": "系統暫時無法處理，請稍後再試",
    "E0201": "內容已更新，請確認最新內容後再送出",
}


# 驗證完整 envelope 與固定文案，避免只有 HTTP 狀態正確卻違反共同契約。
def check(result, status, code=None, errors=None):
    actual, body, _ = result
    require(actual == status, f"HTTP {actual}，預期 {status} / {code}")
    if code is None:
        require(set(body) == {"success", "message", "data"} and body["success"] is True
                and body["message"] == "OK" and isinstance(body["data"], dict), "成功 envelope")
        return body["data"]
    fields = {"success", "message", "error_code", "log_id"}
    if errors is not None:
        fields.add("errors")
    require(set(body) == fields and body["success"] is False and body["error_code"] == code
            and body["message"] == MESSAGES[code] and isinstance(body["log_id"], str)
            and bool(body["log_id"]), "錯誤 envelope 或文案")
    if errors is not None:
        require(body["errors"] == errors, "欄位 errors")
    return body


# 驗收失敗立即停止，避免後續寫入掩蓋第一個錯誤。
def require(condition, reason):
    if not condition:
        raise AssertionError("Ticket 23 驗證失敗：" + reason)


# 只讀取固定隔離 Flask 主機，使用同一無 redirect opener 比較公開資料。
def flask_get(path):
    require(path.startswith("/api/") or path == "/up", "不允許的 Flask 路徑")
    with OPENER.open(Request(FLASK + path, headers={"Accept": "application/json"}), timeout=5) as response:
        require(response.status == 200, "Flask 讀取狀態")
        return response.read() if path == "/up" else json.loads(response.read())


# 查詢唯一合成標記取得資料庫產生的 ID，不從回應或 sequence 推算。
def created(table, author_id, column, value):
    require(table in ("articles", "comments") and column in ("title", "body"), "不允許的查詢欄位")
    with owner_connection() as connection:
        rows = connection.execute(f"SELECT id FROM {table} WHERE author_id = %s AND {column} = %s",
                                  (author_id, value)).fetchall()
    require(len(rows) == 1, "新建資料無法唯一辨識")
    return rows[0]["id"]


# 驗證帳號、來源、時間、版本及文字原樣保存。
def created_state(table, item_id, author_id, value):
    item = row(table, item_id)
    require(item["author_id"] == author_id and item["created_backend"] == "fastapi"
            and item["version"] == 1 and item["visibility"] == "visible"
            and item["restore_control"] is None and item["created_at"] == item["updated_at"]
            and item["body"] == value, "建立狀態或來源")
    return item


# 個人列表按實際 SQL 順序核對完整 ID、總數與合法超頁。
def personal(kind, user, token, expected):
    path = f"/api/me/{kind}"
    data = check(call(FASTAPI, "GET", path + "?page=1&per_page=1", token=token), 200)
    require([item["id"] for item in data["list"]] == expected[:1]
            and data["pagination"] == {"page": 1, "per_page": 1, "total": len(expected),
                                       "total_pages": len(expected)}, "個人列表首頁")
    data = check(call(FASTAPI, "GET", path + "?page=2&per_page=1", token=token), 200)
    require([item["id"] for item in data["list"]] == expected[1:2], "個人列表次頁")
    data = check(call(FASTAPI, "GET", path + "?page=999&per_page=1", token=token), 200)
    require(data["list"] == [] and data["pagination"]["total"] == len(expected), "個人列表超頁")
    with owner_connection() as connection:
        count = connection.execute(f"SELECT count(*) AS n FROM {kind} WHERE author_id = %s",
                                   (user["id"],)).fetchone()["n"]
    require(count == len(expected), "個人 fixture 數量")


# 驗證輸入型態、原始 JSON、唯讀欄位與認證優先順序。
def validation(article_id, token):
    endpoint = f"/api/articles/{article_id}"
    valid = {"title": "題", "body": "文", "version": 1}
    check(call(FASTAPI, "PUT", endpoint, b"{broken"), 401, "E0002")
    check(call(FASTAPI, "PUT", "/api/articles/+1", valid, token), 422, "E0001",
          {"article_id": ["欄位格式或範圍不正確"]})
    check(call(FASTAPI, "PUT", endpoint, b"{broken", token), 400, "E0005")
    for body, errors in ((valid | {"version": True}, {"version": ["欄位型態不正確"]}),
                         (valid | {"version": "1"}, {"version": ["欄位型態不正確"]}),
                         (valid | {"version": 9007199254740992}, {"version": ["欄位格式或範圍不正確"]}),
                         (valid | {"body": None}, {"body": ["此欄位不可為 null"]}),
                         ({"title": "題", "version": 1}, {"body": ["此欄位為必填"]}),
                         (valid | {"created_backend": "laravel"}, {"created_backend": ["不支援此欄位"]}),
                         (valid | {"title": "\u3000\r\n"}, {"title": ["欄位格式或範圍不正確"]}),
                         (valid | {"title": "x" * 201}, {"title": ["欄位格式或範圍不正確"]})):
        check(call(FASTAPI, "PUT", endpoint, body, token), 422, "E0001", errors)
    check(call(FASTAPI, "POST", f"/api/articles/{article_id}/comments", {"body": "x" * 2001}, token),
          422, "E0001", {"body": ["欄位格式或範圍不正確"]})
    check(call(FASTAPI, "GET", "/api/me/articles?page=1&page=2&per_page=20", token=token),
          422, "E0001", {"page": ["此參數不可重複"]})


# 等待請求實際阻塞於資料庫列鎖，讓競爭順序可重現。
def wait_for_lock(fragment, minimum=1):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with owner_connection() as connection:
            count = connection.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = 'articles_app' "
                "AND wait_event_type = 'Lock' AND query LIKE %s", ("%" + fragment + "%",)
            ).fetchone()["n"]
        if count >= minimum:
            return
        time.sleep(0.05)
    raise AssertionError("Ticket 23 驗證失敗：未觀察到受控列鎖等待")


# 父文章先下架時，等待中的留言新增不得根據舊公開狀態插入。
def parent_race(article_id, commenter):
    marker = "競爭留言-" + secrets.token_hex(4)
    with owner_connection() as connection, ThreadPoolExecutor(max_workers=1) as pool:
        connection.execute("SELECT id FROM articles WHERE id = %s FOR UPDATE", (article_id,))
        pending = pool.submit(call, FASTAPI, "POST", f"/api/articles/{article_id}/comments",
                              {"body": marker}, commenter["token"])
        wait_for_lock("FROM articles WHERE id =")
        connection.execute(
            "UPDATE articles SET visibility = 'hidden', restore_control = 'author', "
            "version = version + 1, updated_at = clock_timestamp() WHERE id = %s", (article_id,)
        )
        connection.commit()
    check(pending.result(), 404, "E0004")
    with owner_connection() as connection:
        count = connection.execute("SELECT count(*) AS n FROM comments WHERE body = %s", (marker,)).fetchone()["n"]
    require(count == 0, "父文章下架後仍新增留言")


# 撤銷登入先提交時，等待帳號鎖的寫入須重驗並保持原內容。
def revocation_race(article_id, author):
    before = row("articles", article_id)
    with owner_connection() as connection, ThreadPoolExecutor(max_workers=1) as pool:
        connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (author["id"],))
        pending = pool.submit(call, FASTAPI, "PUT", f"/api/articles/{article_id}",
                              {"title": "不應保存", "body": before["body"], "version": before["version"]},
                              author["token"])
        wait_for_lock("FROM accounts WHERE id =")
        connection.execute("UPDATE login_sessions SET revoked_at = clock_timestamp() "
                           "WHERE account_id = %s AND backend_id = 'fastapi' AND revoked_at IS NULL",
                           (author["id"],))
        connection.commit()
    check(pending.result(), 401, "E0002")
    require(row("articles", article_id)["title"] == before["title"], "撤銷後仍寫入")


# FastAPI 與 Laravel 對同版本同列競爭，只能有一次提交與一次 409。
def version_race(article_id, author, laravel_token):
    before = row("articles", article_id)
    with owner_connection() as connection, ThreadPoolExecutor(max_workers=2) as pool:
        connection.execute("SELECT id FROM articles WHERE id = %s FOR UPDATE", (article_id,))
        requests = [
            pool.submit(call, FASTAPI, "PUT", f"/api/articles/{article_id}",
                        {"title": "FastAPI 勝", "body": before["body"], "version": before["version"]},
                        author["token"]),
            pool.submit(call, LARAVEL, "PUT", f"/api/articles/{article_id}",
                        {"title": "Laravel 勝", "body": before["body"], "version": before["version"]},
                        laravel_token),
        ]
        # 同帳號先鎖 account/session，第二個請求可能停在帳號鎖而非文章鎖。
        wait_for_lock("", minimum=2)
        connection.commit()
    results = [request.result() for request in requests]
    require(sorted(result[0] for result in results) == [200, 409], "跨後端版本競爭結果")
    check(next(result for result in results if result[0] == 200), 200)
    check(next(result for result in results if result[0] == 409), 409, "E0201")
    require(row("articles", article_id)["version"] == before["version"] + 1,
            "跨後端版本僅能增加一次")


# 執行與正式個人列表同形的 SQL 計畫，記錄小型隔離測資的查詢型態。
def query_plans(author_id, commenter_id):
    columns = {
        "articles": "articles.id, articles.author_id, articles.title, articles.body, articles.visibility, "
                    "articles.restore_control, articles.version, articles.created_backend, "
                    "articles.created_at, articles.updated_at, accounts.nickname",
        "comments": "comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, "
                    "comments.restore_control, comments.version, comments.created_backend, "
                    "comments.created_at, comments.updated_at, accounts.nickname",
    }
    with owner_connection() as connection:
        for table, user_id in (("articles", author_id), ("comments", commenter_id)):
            statements = (
                f"SELECT count(*) FROM {table} WHERE author_id = %s",
                f"SELECT {columns[table]} FROM {table} "
                f"JOIN accounts ON accounts.id = {table}.author_id "
                f"WHERE {table}.author_id = %s ORDER BY {table}.created_at DESC, {table}.id DESC "
                "LIMIT 20 OFFSET 0",
            )
            for name, sql in zip(("count", "page"), statements):
                plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, (user_id,)).fetchone()["QUERY PLAN"][0]
                nodes = []
                stack = [plan["Plan"]]
                while stack:
                    node = stack.pop()
                    nodes.append(node["Node Type"])
                    stack.extend(node.get("Plans", ()))
                print(f"PLAN {table} {name} nodes={','.join(nodes)} execution_ms={plan['Execution Time']}")


# 從受守衛的隔離專案建立唯一測資，覆蓋來源、權限、版本與並行交易。
def run():
    guard()
    require(flask_get("/up") == b"OK", "隔離 Flask 健康目標")
    if len(sys.argv) == 2 and sys.argv[1] == "--guard-only":
        print("PASS ticket20_test read-only guard")
        return
    require(len(sys.argv) == 1, "不支援此參數")
    namespace = secrets.token_hex(6)
    print("FIXTURE t23-" + namespace)
    author = member(FASTAPI, namespace, "author")
    commenter = member(FASTAPI, namespace, "commenter")
    outsider = member(FASTAPI, namespace, "outsider")
    admin = member(FASTAPI, namespace, "admin")
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET role = 'admin' WHERE id = %s", (admin["id"],))
    for person in (author, commenter, outsider, admin):
        person["token"] = login(FASTAPI, person)
    laravel_author_token = login(LARAVEL, author)
    title = "FastAPI 文章-" + namespace
    original = "😀\r\n文章原文"
    require(check(call(FASTAPI, "POST", "/api/articles", {"title": title, "body": original},
                       author["token"]), 201) == {}, "發文不應猜 ID")
    article_id = created("articles", author["id"], "title", title)
    created_state("articles", article_id, author["id"], original)
    article_path = f"/api/articles/{article_id}"
    article_data = check(call(LARAVEL, "GET", article_path), 200)
    require(article_data["created_backend"] == "fastapi" and
            article_data == flask_get(article_path)["data"] == check(call(FASTAPI, "GET", article_path), 200),
            "Laravel／Flask 跨後端文章讀取一致")
    validation(article_id, author["token"])
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": title, "body": original, "version": 1}, author["token"]), 200)
    require(row("articles", article_id)["version"] == 1 and
            row("articles", article_id)["updated_at"] == row("articles", article_id)["created_at"],
            "同值文章版本與時間")
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": title, "body": "已編輯", "version": 1}, author["token"]), 200)
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": title, "body": "已編輯", "version": 1}, author["token"]), 409, "E0201")
    require(row("articles", article_id)["created_backend"] == "fastapi", "修改不可改來源")
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": "他人", "body": "他人", "version": 2}, outsider["token"]), 403, "E0003")
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": "管理員", "body": "管理員", "version": 2}, admin["token"]), 403, "E0003")
    second_title = "第二篇-" + namespace
    check(call(FASTAPI, "POST", "/api/articles", {"title": second_title, "body": "第二篇"},
               author["token"]), 201)
    second_article_id = created("articles", author["id"], "title", second_title)
    personal("articles", author, author["token"], [second_article_id, article_id])

    comment_text = "😀\r\n留言-" + namespace
    require(check(call(FASTAPI, "POST", f"/api/articles/{article_id}/comments",
                       {"body": comment_text}, commenter["token"]), 201) == {}, "新增留言空資料")
    comment_id = created("comments", commenter["id"], "body", comment_text)
    created_state("comments", comment_id, commenter["id"], comment_text)
    comments_path = f"/api/articles/{article_id}/comments?page=1&per_page=20"
    comments_data = check(call(LARAVEL, "GET", comments_path), 200)
    require(comments_data["list"][0]["id"] == comment_id and
            comments_data == flask_get(comments_path)["data"] == check(call(FASTAPI, "GET", comments_path), 200),
            "Laravel／Flask 跨後端留言讀取一致")
    require(check(call(FASTAPI, "GET", f"/api/comments/{comment_id}", token=commenter["token"]), 200)
            ["article_id"] == article_id, "留言詳情父 ID")
    check(call(FASTAPI, "GET", f"/api/comments/{comment_id}", token=outsider["token"]), 403, "E0003")
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}",
               {"body": comment_text, "version": 1}, commenter["token"]), 200)
    require(row("comments", comment_id)["version"] == 1 and
            row("comments", comment_id)["updated_at"] == row("comments", comment_id)["created_at"],
            "同值留言版本與時間")
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}",
               {"body": "已編輯", "version": 1}, commenter["token"]), 200)
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}",
               {"body": "已編輯", "version": 1}, commenter["token"]), 409, "E0201")
    second_comment_text = "第二則留言-" + namespace
    check(call(FASTAPI, "POST", f"/api/articles/{article_id}/comments",
               {"body": second_comment_text}, commenter["token"]), 201)
    second_comment_id = created("comments", commenter["id"], "body", second_comment_text)
    personal("comments", commenter, commenter["token"], [second_comment_id, comment_id])
    print("PASS 建立／編修／驗證／列表／跨後端來源")

    # 管理員接管他人自行下架內容，作者仍可編修但不可恢復。
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}/visibility",
               {"visibility": "hidden", "version": 2}, author["token"]), 200)
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}/visibility",
               {"visibility": "hidden", "version": 3}, admin["token"]), 200)
    require(row("articles", article_id)["restore_control"] == "admin" and
            row("articles", article_id)["version"] == 4, "管理員接管文章恢復權")
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}/visibility",
               {"visibility": "visible", "version": 3}, author["token"]), 403, "E0003")
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": "無權", "body": "無權", "version": 4}, outsider["token"]), 404, "E0004")
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
               {"title": title, "body": "下架仍能編修", "version": 4}, author["token"]), 200)
    require(row("articles", article_id)["visibility"] == "hidden", "編修不可自動恢復")
    check(call(FASTAPI, "POST", f"/api/articles/{article_id}/comments",
               {"body": "禁止"}, commenter["token"]), 404, "E0004")
    check(call(FASTAPI, "POST", f"/api/articles/{article_id}/comments",
               {"body": "禁止"}, admin["token"]), 403, "E0003")
    require(check(call(FASTAPI, "GET", f"/api/comments/{comment_id}", token=commenter["token"]), 200)
            ["body"] == "已編輯", "父文章下架仍可讀自己留言")
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}",
               {"body": "禁止", "version": 2}, commenter["token"]), 403, "E0003")
    check(call(FASTAPI, "GET", f"/api/comments/{comment_id}", token=outsider["token"]), 404, "E0004")
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}/visibility",
               {"visibility": "hidden", "version": 2}, commenter["token"]), 200)
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}/visibility",
               {"visibility": "hidden", "version": 3}, admin["token"]), 200)
    require(row("comments", comment_id)["restore_control"] == "admin", "管理員接管留言恢復權")
    require(check(call(FASTAPI, "GET", f"/api/comments/{comment_id}", token=commenter["token"]), 200)
            ["can_restore"] is False, "隱藏父文章下作者仍可讀但不可恢復管理員下架留言")
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}/visibility",
               {"visibility": "visible", "version": 3}, commenter["token"]), 403, "E0003")
    check(call(FASTAPI, "PUT", f"/api/comments/{comment_id}/visibility",
               {"visibility": "visible", "version": 4}, admin["token"]), 200)
    check(call(FASTAPI, "PUT", f"/api/articles/{article_id}/visibility",
               {"visibility": "visible", "version": 5}, admin["token"]), 200)
    # 狀態已公開時重送正確版本應成功，但不可改動版本或時間。
    for table, item_id, version, token in (("comments", comment_id, 5, commenter["token"]),
                                           ("articles", article_id, 6, author["token"])):
        before = row(table, item_id)
        endpoint = f"/api/{table}/{item_id}/visibility"
        check(call(FASTAPI, "PUT", endpoint, {"visibility": "visible", "version": version}, token), 200)
        require(row(table, item_id)["version"] == before["version"] and
                row(table, item_id)["updated_at"] == before["updated_at"], "同值下架狀態不改版本與時間")
    require(check(call(LARAVEL, "GET", f"/api/articles/{article_id}", token=laravel_author_token), 200)
            ["created_backend"] == "fastapi", "恢復後跨後端來源保持")
    admin_title = "管理員自己的文章-" + namespace
    check(call(FASTAPI, "POST", "/api/articles", {"title": admin_title, "body": "管理員原文"}, admin["token"]), 201)
    admin_article = created("articles", admin["id"], "title", admin_title)
    check(call(FASTAPI, "PUT", f"/api/articles/{admin_article}/visibility",
               {"visibility": "hidden", "version": 1}, admin["token"]), 200)
    require(row("articles", admin_article)["restore_control"] == "author", "管理員自己的內容按作者規則")
    check(call(FASTAPI, "PUT", f"/api/articles/{admin_article}",
               {"title": admin_title, "body": "管理員自改", "version": 2}, admin["token"]), 200)
    check(call(FASTAPI, "PUT", f"/api/articles/{admin_article}/visibility",
               {"visibility": "visible", "version": 3}, admin["token"]), 200)
    print("PASS 下架／隱藏讀取／父子權限／管理員接管")

    # 每項競爭使用新的公開文章，避免前段狀態變化影響判定。
    for label in ("version-race", "parent-race", "revoke-race"):
        race_title = label + namespace
        check(call(FASTAPI, "POST", "/api/articles", {"title": race_title, "body": "原文"},
                   author["token"]), 201)
        race_id = created("articles", author["id"], "title", race_title)
        if label == "version-race":
            version_race(race_id, author, laravel_author_token)
        elif label == "parent-race":
            parent_race(race_id, commenter)
        else:
            revocation_race(race_id, author)
    print("PASS 跨後端版本／父文章／登入撤銷與寫入競爭")
    query_plans(author["id"], commenter["id"])


# 保留受控失敗摘要，避免輸出憑證、密碼或資料庫例外內容。
if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(str(error) if isinstance(error, (AssertionError, CheckFailure)) else
              "Ticket 23 驗證失敗：未預期 " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
