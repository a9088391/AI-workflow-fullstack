"""會員與鎖定設定管理；角色門禁先於輸入及目標查詢。"""

from .content import COMMENT_COLUMNS
from .core import ApiError, account_resource, authenticate, connect, content_resource, success
from .http import api
from .input import json_body, list_input, no_get_body, no_query, pagination, url_integer


# 管理端點先確認目前角色，再驗輸入；寫入交易取得鎖後仍須重驗。
def management_identity(request, connection):
    account, claims = authenticate(request, connection)
    if account["role"] != "admin":
        raise ApiError("E0003")
    return account, claims


# 僅搜尋一般會員；資料庫以 ASCII folding 和字面 strpos 完成計數及分頁。
@api("GET")
def members(request):
    with connect() as connection:
        management_identity(request, connection)
        page, per_page, keyword = list_input(request, allow_keyword=True)
        folded = keyword.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        where = "FROM accounts WHERE role = 'member'"
        params = ()
        if folded:
            where += (
                " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
            )
            params = (folded, folded)
        total = connection.execute("SELECT count(*) " + where, params).fetchone()["count"]
        offset = (page - 1) * per_page
        # 已超出總數的頁碼不再執行注定為空的列表查詢。
        rows = [] if offset >= total else connection.execute(
            "SELECT id, email, nickname, role, status, created_at " + where
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, per_page, offset),
        ).fetchall()
        return success({"list": [account_resource(row) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 會員詳情不揭露管理員存在，也不輸出認證與鎖定欄位。
@api("GET")
def member(request, member_id):
    with connect() as connection:
        management_identity(request, connection)
        no_get_body(request)
        no_query(request)
        account_id = url_integer(member_id, "member_id")
        row = connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts "
            "WHERE id = %s AND role = 'member'", (account_id,),
        ).fetchone()
        if row is None:
            raise ApiError("E0004")
        return success(account_resource(row))


# 停用與撤銷登入同交易；先按 ID 鎖兩帳號，後鎖管理員 session 並重驗。
@api("PUT")
def member_status(request, member_id):
    with connect() as connection:
        manager, claims = management_identity(request, connection)
    # 初驗角色先於輸入；實際交易內還會在列鎖後重驗，避免等待時權限已失效。
    no_query(request)
    account_id = url_integer(member_id, "member_id")
    body = json_body(request, ("status",))
    with connect() as connection:
        locked = connection.execute(
            "SELECT id, role, status FROM accounts WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
            (manager["id"], account_id),
        ).fetchall()
        accounts = {row["id"]: row for row in locked}
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        management_identity(request, connection)
        # 管理員不屬於可操作會員集合；同值不再撤銷 session，恢復只改 status。
        target = accounts.get(account_id)
        if target is None or target["role"] != "member":
            raise ApiError("E0004")
        if target["status"] != body["status"]:
            connection.execute("UPDATE accounts SET status = %s WHERE id = %s", (body["status"], account_id))
            if body["status"] == "disabled":
                # 帳號列鎖阻止新登入穿越停用提交；既有登入全部同時撤銷。
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = clock_timestamp() "
                    "WHERE account_id = %s AND revoked_at IS NULL", (account_id,),
                )
    return success()


# 管理留言包含個別及父文章下架內容，JOIN 作者一次取齊完整資源。
@api("GET")
def management_comments(request):
    with connect() as connection:
        viewer, _ = management_identity(request, connection)
        page, per_page, _ = list_input(request)
        # 管理列表不套用父文或留言 visibility 篩選，計數與分頁使用相同完整範圍。
        from_sql = "FROM comments JOIN accounts ON accounts.id = comments.author_id"
        total = connection.execute("SELECT count(*) " + from_sql).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + COMMENT_COLUMNS + " " + from_sql
            + " ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, "comment") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 設定寫入必須提供完整四欄，沿用共同 JSON 解析及逐欄錯誤格式。
def lockout_body(request):
    fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
    return json_body(request, fields, field_types={
        "enabled": bool, "window_minutes": int, "failure_threshold": int, "lock_seconds": int,
    }, numeric_limits={"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400})


# 管理設定讀取只回公開四欄；更新依設定→帳號 ID→session 鎖序提交。
@api(("GET", "PUT"))
def login_lockout_settings(request):
    # Flask 自動開放 HEAD；它與 GET 共用唯讀驗證，不能落入 PUT 寫入。
    if request.method in ("GET", "HEAD"):
        with connect() as connection:
            management_identity(request, connection)
            no_get_body(request)
            no_query(request)
            row = connection.execute(
                "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
                "FROM login_lockout_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise ApiError("E0104")
            return success({key: row[key] for key in ("enabled", "window_minutes", "failure_threshold", "lock_seconds")})
    with connect() as connection:
        manager, claims = management_identity(request, connection)
    # 完整四欄先驗證，才開始更新交易；預先驗過角色不能取代鎖後的再次查核。
    no_query(request)
    body = lockout_body(request)
    with connect() as connection:
        settings = connection.execute(
            "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
            "FROM login_lockout_settings WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if settings is None:
            raise ApiError("E0104")
        disabling = settings["enabled"] and not body["enabled"]
        # 關閉須先鎖全部帳號；一般修改只鎖管理員，避免反向鎖序。
        if disabling:
            connection.execute("SELECT id FROM accounts ORDER BY id FOR UPDATE").fetchall()
        else:
            connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (manager["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        management_identity(request, connection)
        # 四欄以同一 UPDATE 提交；同值不重做清鎖，數值更新不改任何現有鎖定期限。
        if any(settings[key] != body[key] for key in body):
            connection.execute(
                "UPDATE login_lockout_settings SET enabled = %s, window_minutes = %s, "
                "failure_threshold = %s, lock_seconds = %s WHERE id = 1",
                tuple(body[key] for key in ("enabled", "window_minutes", "failure_threshold", "lock_seconds")),
            )
            if disabling:
                # 換世代使舊失敗事件不再計數；保留登入與停用狀態。
                connection.execute(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                    "WHERE role = 'member'"
                )
    return success()
