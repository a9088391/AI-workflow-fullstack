"""文章與留言用途；在既有交易內裁決可讀性、操作權與版本。"""

from .core import ApiError, authenticate, authenticate_write, connect, content_resource, success
from .http import api
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


# 先比版本，即使提交同值也拒絕過期；實際變更才增加版本與時間。
def save_content(connection, table, row, version, changes):
    # table／changes 只來自下方兩種內容操作；呼叫端已鎖目標列並裁決讀取與操作權。
    if row["version"] != version:
        raise ApiError("E0201")
    if all(row[key] == value for key, value in changes.items()):
        return
    # 不改作者、建立來源或建立時間；條件 UPDATE 再守住版本，實際變更只遞增一次。
    columns = list(changes)
    assignments = ", ".join(f"{column} = %s" for column in columns)
    result = connection.execute(
        f"UPDATE {table} SET {assignments}, version = version + 1, updated_at = clock_timestamp() "
        "WHERE id = %s AND version = %s",
        (*[changes[column] for column in columns], row["id"], version),
    )
    if result.rowcount != 1:
        raise ApiError("E0201")


# 作者只能恢復自行下架內容；管理員下架他人內容會取得恢復權。
def visibility_changes(row, viewer, visibility):
    own = viewer["id"] == row["author_id"]
    if visibility == "visible" and row["visibility"] == "hidden" and viewer["role"] != "admin" and row["restore_control"] != "author":
        raise ApiError("E0003")
    # 恢復會清除控制者；重下架保留既有管理限制，管理員自己的內容仍視為作者操作。
    control = None if visibility == "visible" else row["restore_control"] or "author"
    if visibility == "hidden" and viewer["role"] == "admin" and not own:
        control = "admin"
    return {"visibility": visibility, "restore_control": control}


# 公開列表維持原有範圍；POST 則在重驗登入的交易建立作者文章。
@api(("GET", "POST"))
def articles(request):
    if request.method == "POST":
        with connect() as connection:
            viewer = authenticate_write(request, connection)
            no_query(request)
            body = json_body(request, ("title", "body"), content_limits={"title": 200, "body": 20000})
            # 同一資料庫時刻建立兩個時間欄，作者和來源由後端指定。
            connection.execute(
                "INSERT INTO articles (author_id, title, body, visibility, restore_control, version, "
                "created_backend, created_at, updated_at) "
                "SELECT %s, %s, %s, 'visible', NULL, 1, 'flask', now, now "
                "FROM (SELECT clock_timestamp() AS now) AS stamp",
                (viewer["id"], body["title"], body["body"]),
            )
        return success(status=201)
    with connect() as connection:
        # 明確 management 才啟用管理門禁；即使登入管理員，省略 scope 仍只列公開文章。
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        if scope == "management" and (viewer is None or viewer["role"] != "admin"):
            raise ApiError("E0003")
        where = "" if scope == "management" else "WHERE articles.visibility = 'visible'"
        # 計數與列表共用範圍，超頁仍保留真實總數；作者 JOIN 避免逐筆讀取暱稱。
        from_sql = "FROM articles JOIN accounts ON accounts.id = articles.author_id " + where
        total = connection.execute("SELECT count(*) " + from_sql).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + ARTICLE_COLUMNS + " " + from_sql
            + " ORDER BY articles.created_at DESC, articles.id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        return success({
            "list": [content_resource(row, viewer, "article") for row in rows],
            "pagination": pagination(page, per_page, total),
        })


# 詳情隱藏無權讀取的下架文章；PUT 進入作者編修交易。
@api(("GET", "PUT"))
def article(request, article_id):
    if request.method == "PUT":
        return mutate_article(request, article_id, visibility=False)
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        no_get_body(request)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        # 先驗登入與輸入才查資源；無權讀下架內容時和不存在使用同一 404。
        row = connection.execute(
            "SELECT " + ARTICLE_COLUMNS
            + " FROM articles JOIN accounts ON accounts.id = articles.author_id WHERE articles.id = %s",
            (content_id,),
        ).fetchone()
        if row is None or (
            row["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != row["author_id"]))
        ):
            raise ApiError("E0004")
        return success(content_resource(row, viewer, "article"))


# GET 先確認父文章可讀；POST 鎖同一父文章以和下架序列化。
@api(("GET", "POST"))
def comments(request, article_id):
    if request.method == "POST":
        with connect() as connection:
            viewer = authenticate_write(request, connection)
            no_query(request)
            content_id = url_integer(article_id, "article_id")
            body = json_body(request, ("body",), content_limits={"body": 2000})
            # 帳號與 session 已鎖定；再鎖父文章，讓下架和新增依父列取得順序完成。
            parent = connection.execute(
                "SELECT author_id, visibility FROM articles WHERE id = %s FOR UPDATE", (content_id,)
            ).fetchone()
            if parent is None or (parent["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != parent["author_id"]):
                raise ApiError("E0004")
            if parent["visibility"] == "hidden":
                raise ApiError("E0003")
            # 父文章仍公開才在相同交易新增，避免與並行下架交錯。
            connection.execute(
                "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, version, "
                "created_backend, created_at, updated_at) "
                "SELECT %s, %s, %s, 'visible', NULL, 1, 'flask', now, now "
                "FROM (SELECT clock_timestamp() AS now) AS stamp",
                (content_id, viewer["id"], body["body"]),
            )
        return success(status=201)
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        page, per_page, _ = list_input(request)
        content_id = url_integer(article_id, "article_id")
        # 父文預覽只授予父文章作者／管理員；留言作者身分本身不取得父文讀取權。
        parent = connection.execute(
            "SELECT author_id, visibility FROM articles WHERE id = %s", (content_id,)
        ).fetchone()
        if parent is None or (
            parent["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != parent["author_id"]))
        ):
            raise ApiError("E0004")
        where = "WHERE comments.article_id = %s"
        # 父文作者的預覽仍排除個別下架留言；管理員才可在這個列表讀取全部。
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
        return success({
            "list": [content_resource(row, viewer, "comment") for row in rows],
            "pagination": pagination(page, per_page, total),
        })


# 我的文章只按目前帳號篩選，包含自行或管理員下架內容。
@api("GET")
def my_articles(request):
    return my_content(request, "articles", ARTICLE_COLUMNS, "article")


# 我的留言不讀父文章內文；父文章下架仍可列自己的留言。
@api("GET")
def my_comments(request):
    return my_content(request, "comments", COMMENT_COLUMNS, "comment")


# 精確計數及排序分頁留給資料庫，避免整表載入或逐筆查詢。
def my_content(request, table, columns, kind):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        page, per_page, _ = list_input(request)
        # table／columns／kind 僅由兩個本人列表指定；不接受 Query 決定 SQL 識別字。
        total = connection.execute(f"SELECT count(*) FROM {table} WHERE author_id = %s", (viewer["id"],)).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            f"SELECT {columns} FROM {table} JOIN accounts ON accounts.id = {table}.author_id "
            f"WHERE {table}.author_id = %s ORDER BY {table}.created_at DESC, {table}.id DESC "
            "LIMIT %s OFFSET %s", (viewer["id"], per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, kind) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 文章下架和留言新增、修改同鎖文章列，權限與版本在交易內裁決。
@api("PUT")
def article_visibility(request, article_id):
    return mutate_article(request, article_id, visibility=True)


# 文章內文限作者編修；管理員可透過下架操作處理他人文章。
def mutate_article(request, article_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        fields = ("visibility", "version") if visibility else ("title", "body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"title": 200, "body": 20000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 帳號→session→文章鎖在同一交易；先隱藏不可讀目標，再驗操作權，最後才驗版本。
        row = connection.execute("SELECT * FROM articles WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
        if row is None or (row["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != row["author_id"]):
            raise ApiError("E0004")
        if viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin"):
            raise ApiError("E0003")
        # 恢復資格須先於版本判定；同值與過期判定由共用儲存流程處理。
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"title": body["title"], "body": body["body"]}
        save_content(connection, "articles", row, body["version"], changes)
    return success()


# 留言作者即使父文章下架仍可讀自己的詳情；他人先依可見性隱藏存在。
@api(("GET", "PUT"))
def comment(request, comment_id):
    if request.method == "PUT":
        return mutate_comment(request, comment_id, visibility=False)
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
        # 作者可讀自己的下架留言，管理員可讀全部；其他人先依留言及父文隱藏性決定 404 或 403。
        if viewer["role"] != "admin" and viewer["id"] != row["author_id"]:
            if row["visibility"] == "hidden" or row["article_visibility"] == "hidden":
                raise ApiError("E0004")
            raise ApiError("E0003")
        return success(content_resource(row, viewer, "comment"))


# 留言下架與恢復不因父文章下架而禁止；留言恢復也不會改變父文章的隱藏狀態。
@api("PUT")
def comment_visibility(request, comment_id):
    return mutate_comment(request, comment_id, visibility=True)


# 父文章、留言依序取得列鎖；修改內文必須確認父文章仍公開。
def mutate_comment(request, comment_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        fields = ("visibility", "version") if visibility else ("body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"body": 2000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 先讀父 ID 而不鎖留言，之後先鎖文章再鎖留言；鎖後複核關聯。
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
        # 作者或管理員可獨立管理留言狀態；改內文則必須是本人且父文仍公開。
        if (viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin")) or (
            not visibility and parent["visibility"] == "hidden"
        ):
            raise ApiError("E0003")
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"body": body["body"]}
        save_content(connection, "comments", row, body["version"], changes)
    return success()
