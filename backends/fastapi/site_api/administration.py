"""會員與鎖定設定管理；角色門禁先於輸入及目標查詢。"""

from .content import COMMENT_COLUMNS
from .core import ApiError, account_resource, authenticate, connect, content_resource, success
from .input import json_body, list_input, no_get_body, no_query, pagination, url_integer


# 管理端點先確認目前角色，避免在輸入錯誤時洩漏管理資源；寫入時須鎖後重驗。
def management_identity(request, connection):
    account, claims = authenticate(request, connection)
    if account["role"] != "admin":
        raise ApiError("E0003")
    return account, claims


# 僅搜尋一般會員；SQL 的 ASCII folding 和字面 strpos 共用於計數及分頁。
def members(request, _):
    with connect() as connection:
        management_identity(request, connection)
        page, per_page, keyword = list_input(request, allow_keyword=True)
        folded = keyword.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        from_sql = "FROM accounts WHERE role = 'member'"
        params = ()
        if folded:
            from_sql += (
                " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
            )
            params = (folded, folded)
        # count 與列表使用同一條件；超頁仍回原頁碼及完整 total，並以時間、ID 降冪排序。
        total = connection.execute("SELECT count(*) " + from_sql, params).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT id, email, nickname, role, status, created_at " + from_sql
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, per_page, offset),
        ).fetchall()
        return success({"list": [account_resource(row) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 會員詳情只回公開 Account 欄位，管理員 ID 一律當成不存在。
def member(request, member_id):
    with connect() as connection:
        # 先驗管理角色及輸入，才查一般會員，避免把管理員帳號當成可管理目標。
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


# 停用與撤銷全部登入同交易；兩帳號按 ID 鎖後鎖管理員 session 並重驗。
def member_status(request, member_id):
    with connect() as connection:
        manager, claims = management_identity(request, connection)
    # 角色門禁先於格式；輸入驗完才進入寫入交易，鎖後仍需再次查核管理員。
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
        # 管理員 ID 與不存在目標同為 404；同值不撤銷，恢復只改狀態而不復活舊登入。
        target = accounts.get(account_id)
        if target is None or target["role"] != "member":
            raise ApiError("E0004")
        if target["status"] != body["status"]:
            connection.execute("UPDATE accounts SET status = %s WHERE id = %s", (body["status"], account_id))
            if body["status"] == "disabled":
                # 帳號鎖擋住新登入及受保護寫入；舊登入全部在同次提交撤銷。
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = clock_timestamp() "
                    "WHERE account_id = %s AND revoked_at IS NULL", (account_id,),
                )
    return success()


# 管理列表包含個別及父文章下架留言，一次 JOIN 作者避免逐列查詢。
def management_comments(request, _):
    with connect() as connection:
        viewer, _ = management_identity(request, connection)
        page, per_page, _ = list_input(request)
        # 管理範圍不排除父文或留言下架；計數及列表共用 JOIN，不逐列查詢作者。
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


# 四欄必須一起提供，嚴格分辨 JSON boolean、integer 及範圍。
def lockout_body(request):
    fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
    return json_body(request, fields, field_types={
        "enabled": bool, "window_minutes": int, "failure_threshold": int, "lock_seconds": int,
    }, numeric_limits={"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400})


# 讀設定只回四欄；更新按設定、帳號 ID、session 鎖序原子提交。
def login_lockout_settings(request, _):
    if request.method == "GET":
        with connect() as connection:
            # 管理權先於 GET 輸入驗證，讀取只投影四個公開設定欄位。
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
    # 四欄先完整驗證，再鎖設定及帳號；外層角色檢查不能取代交易內重驗。
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
        # 關閉時鎖全部帳號阻止登入計數競爭；其餘更新只鎖管理員。
        if disabling:
            connection.execute("SELECT id FROM accounts ORDER BY id FOR UPDATE").fetchall()
        else:
            connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (manager["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        management_identity(request, connection)
        # 四欄以單次 UPDATE 保存；同值無副作用，單改數值不動現有鎖定截止。
        if any(settings[key] != body[key] for key in body):
            connection.execute(
                "UPDATE login_lockout_settings SET enabled = %s, window_minutes = %s, "
                "failure_threshold = %s, lock_seconds = %s WHERE id = 1",
                tuple(body[key] for key in ("enabled", "window_minutes", "failure_threshold", "lock_seconds")),
            )
            if disabling:
                # 換世代並清除未到期鎖，保留停用狀態及既有登入。
                connection.execute(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                    "WHERE role = 'member'"
                )
    return success()
