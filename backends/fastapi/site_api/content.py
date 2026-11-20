"""文章與留言用途；在既有交易內裁決可讀性、操作權與版本。"""

from .core import ApiError, authenticate, authenticate_write, connect, content_resource, success
from .input import (
    json_body,
    list_input,
    management_requested,
    no_get_body,
    no_query,
    pagination,
    url_integer,
)


ARTICLE_COLUMNS = (
    "articles.id, articles.author_id, articles.title, articles.body, "
    "articles.visibility, articles.restore_control, articles.version, "
    "articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname"
)

COMMENT_COLUMNS = (
    "comments.id, comments.author_id, comments.article_id, comments.body, "
    "comments.visibility, comments.restore_control, comments.version, "
    "comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname"
)


# 公開列表在 SQL 完成權限篩選、精確計數、排序及分頁。
def articles(request, _):
    with connect() as connection:
        # 明示管理範圍先驗角色，再驗分頁；管理員未指定 scope 仍只看公開文章。
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        where = "" if scope == "management" else "WHERE articles.visibility = 'visible'"
        from_sql = "FROM articles JOIN accounts ON accounts.id = articles.author_id " + where
        # 計數與列表共用範圍，JOIN 一次取齊作者；超頁不再查注定為空的列表。
        total = connection.execute("SELECT count(*) " + from_sql).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + ARTICLE_COLUMNS + " " + from_sql
            + " ORDER BY articles.created_at DESC, articles.id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, "article") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 文章詳情只向作者或管理員顯示下架內容。
def article(request, article_id):
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        no_get_body(request)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        row = connection.execute(
            "SELECT " + ARTICLE_COLUMNS + " FROM articles JOIN accounts ON accounts.id = articles.author_id "
            "WHERE articles.id = %s", (content_id,),
        ).fetchone()
        # 先隱藏不可讀資源的存在，再輸出完整資源；公開讀取的無效憑證已在前面拒絕。
        if row is None or (
            row["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != row["author_id"]))
        ):
            raise ApiError("E0004")
        return success(content_resource(row, viewer, "article"))


# 留言先檢查父文章可讀，再於 SQL 限定個別下架留言的管理範圍。
def comments(request, article_id):
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        page, per_page, _ = list_input(request)
        content_id = url_integer(article_id, "article_id")
        parent = connection.execute("SELECT author_id, visibility FROM articles WHERE id = %s", (content_id,)).fetchone()
        if parent is None or (
            parent["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != parent["author_id"]))
        ):
            raise ApiError("E0004")
        # 作者預覽下架文章仍只看公開留言；自己的下架留言由本人列表／詳情讀取。
        where = "WHERE comments.article_id = %s"
        if viewer is None or viewer["role"] != "admin":
            where += " AND comments.visibility = 'visible'"
        from_sql = "FROM comments JOIN accounts ON accounts.id = comments.author_id " + where
        total = connection.execute("SELECT count(*) " + from_sql, (content_id,)).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + COMMENT_COLUMNS + " " + from_sql
            + " ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s",
            (content_id, per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, "comment") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 正確版本但同值不更新；實際變更才增加版本及資料庫時間。
def save_content(connection, table, row, version, changes):
    # 呼叫端已鎖列並驗操作權；過期同值仍衝突，只有正確版本的同值可略過 UPDATE。
    if row["version"] != version:
        raise ApiError("E0201")
    if all(row[key] == value for key, value in changes.items()):
        return
    columns = list(changes)
    # 表名與欄名僅來自內部呼叫；值一律綁定，條件版本更新保留共同原子寫入契約。
    assignments = ", ".join(f"{column} = %s" for column in columns)
    result = connection.execute(
        f"UPDATE {table} SET {assignments}, version = version + 1, updated_at = clock_timestamp() "
        "WHERE id = %s AND version = %s",
        (*[changes[column] for column in columns], row["id"], version),
    )
    if result.rowcount != 1:
        raise ApiError("E0201")


# 作者自行下架可自復；管理員下架他人內容會接管其恢復權。
def visibility_changes(row, viewer, visibility):
    own = viewer["id"] == row["author_id"]
    if (
        visibility == "visible" and row["visibility"] == "hidden"
        and viewer["role"] != "admin" and row["restore_control"] != "author"
    ):
        raise ApiError("E0003")
    # 作者重下架不能移除管理員限制；管理員自己的內容仍依作者規則處理。
    control = None if visibility == "visible" else row["restore_control"] or "author"
    if visibility == "hidden" and viewer["role"] == "admin" and not own:
        control = "admin"
    return {"visibility": visibility, "restore_control": control}


# 作者與建立來源由可信後端指定，回應不猜資料庫產生的文章 ID。
def create_article(request, _):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        body = json_body(request, ("title", "body"), content_limits={"title": 200, "body": 20000})
        # 同一 DB 時刻填入建立與更新時間，作者及來源不接受客戶端指定。
        connection.execute(
            "INSERT INTO articles (author_id, title, body, visibility, restore_control, version, "
            "created_backend, created_at, updated_at) "
            "SELECT %s, %s, %s, 'visible', NULL, 1, 'fastapi', now, now "
            "FROM (SELECT clock_timestamp() AS now) AS stamp",
            (viewer["id"], body["title"], body["body"]),
        )
    return success(status=201)


# 我的內容在 SQL 篩選、計數及排序，包含本人下架與父文章下架的留言。
def my_content(request, table, columns, kind):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        page, per_page, _ = list_input(request)
        # table／columns／kind 由下方兩個固定入口提供；本人範圍不受父文章或留言狀態排除。
        total = connection.execute(f"SELECT count(*) FROM {table} WHERE author_id = %s", (viewer["id"],)).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            f"SELECT {columns} FROM {table} JOIN accounts ON accounts.id = {table}.author_id "
            f"WHERE {table}.author_id = %s ORDER BY {table}.created_at DESC, {table}.id DESC "
            "LIMIT %s OFFSET %s", (viewer["id"], per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, kind) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 我的文章使用完整文章欄位，讓下架與恢復權可在列表呈現。
def my_articles(request, _):
    return my_content(request, "articles", ARTICLE_COLUMNS, "article")


# 我的留言只帶父文章 ID，不因列表查詢洩漏其標題或讀取權。
def my_comments(request, _):
    return my_content(request, "comments", COMMENT_COLUMNS, "comment")


# 文章列鎖保護版本、內文與下架狀態，並與留言寫入依同一父列排序。
def mutate_article(request, article_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        fields = ("visibility", "version") if visibility else ("title", "body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"title": 200, "body": 20000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 帳號→session→文章在同一交易加鎖；不可讀先 404，再驗操作權與恢復權，最後才比版本。
        row = connection.execute("SELECT * FROM articles WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
        if row is None or (row["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != row["author_id"]):
            raise ApiError("E0004")
        if viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin"):
            raise ApiError("E0003")
        changes = (
            visibility_changes(row, viewer, body["visibility"])
            if visibility else {"title": body["title"], "body": body["body"]}
        )
        save_content(connection, "articles", row, body["version"], changes)
    return success()


# 文章內文僅作者可改，含作者自己下架的文章。
def update_article(request, article_id):
    return mutate_article(request, article_id, visibility=False)


# 管理員可下架他人文章；恢復資格在比較版本前檢查。
def article_visibility(request, article_id):
    return mutate_article(request, article_id, visibility=True)


# 新增留言鎖父文章，確保公開狀態與 INSERT 在同一交易裁決。
def create_comment(request, article_id):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        body = json_body(request, ("body",), content_limits={"body": 2000})
        # 與文章下架鎖同一父列，取得鎖後才判公開狀態，避免依等待前的狀態新增。
        parent = connection.execute(
            "SELECT author_id, visibility FROM articles WHERE id = %s FOR UPDATE", (content_id,)
        ).fetchone()
        if parent is None or (parent["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != parent["author_id"]):
            raise ApiError("E0004")
        if parent["visibility"] == "hidden":
            raise ApiError("E0003")
        # 有預覽權也不能對下架父文新增；通過後才寫入同時刻、固定來源的留言。
        connection.execute(
            "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, version, "
            "created_backend, created_at, updated_at) "
            "SELECT %s, %s, %s, 'visible', NULL, 1, 'fastapi', now, now "
            "FROM (SELECT clock_timestamp() AS now) AS stamp",
            (content_id, viewer["id"], body["body"]),
        )
    return success(status=201)


# 留言作者可讀自己的下架留言，即使父文章不可讀；他人按可見性回 403/404。
def comment(request, comment_id):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        row = connection.execute(
            "SELECT " + COMMENT_COLUMNS + ", articles.visibility AS article_visibility "
            "FROM comments JOIN accounts ON accounts.id = comments.author_id "
            "JOIN articles ON articles.id = comments.article_id WHERE comments.id = %s", (content_id,),
        ).fetchone()
        if row is None:
            raise ApiError("E0004")
        # 作者例外只授予自己的留言；其他人先按父文及留言隱藏性裁決 404，再回操作權 403。
        if viewer["role"] != "admin" and viewer["id"] != row["author_id"]:
            if row["visibility"] == "hidden" or row["article_visibility"] == "hidden":
                raise ApiError("E0004")
            raise ApiError("E0003")
        return success(content_resource(row, viewer, "comment"))


# 留言先找父 ID，再依文章、留言順序鎖列；父文章下架時禁改文字但允許合法狀態操作。
def mutate_comment(request, comment_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        fields = ("visibility", "version") if visibility else ("body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"body": 2000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 先讀父 ID 而不鎖留言，再依文章→留言加鎖，鎖後複核關聯以保持共同鎖序。
        initial = connection.execute("SELECT article_id FROM comments WHERE id = %s", (content_id,)).fetchone()
        parent = None if initial is None else connection.execute(
            "SELECT id, visibility FROM articles WHERE id = %s FOR UPDATE", (initial["article_id"],)
        ).fetchone()
        row = None if parent is None else connection.execute(
            "SELECT * FROM comments WHERE id = %s FOR UPDATE", (content_id,)
        ).fetchone()
        if row is None or row["article_id"] != parent["id"] or (
            viewer["role"] != "admin" and viewer["id"] != row["author_id"]
            and (row["visibility"] == "hidden" or parent["visibility"] == "hidden")
        ):
            raise ApiError("E0004")
        # 下架父文禁止任何人改留言內文，包含管理員本人；合法狀態操作仍可繼續。
        if (viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin")) or (
            not visibility and parent["visibility"] == "hidden"
        ):
            raise ApiError("E0003")
        changes = (
            visibility_changes(row, viewer, body["visibility"])
            if visibility else {"body": body["body"]}
        )
        save_content(connection, "comments", row, body["version"], changes)
    return success()


# 留言內文僅作者且父文章公開時可改，包含自己個別下架的留言。
def update_comment(request, comment_id):
    return mutate_comment(request, comment_id, visibility=False)


# 留言個別下架與恢復沿用父文章鎖及恢復權限判定。
def comment_visibility(request, comment_id):
    return mutate_comment(request, comment_id, visibility=True)
