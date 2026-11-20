"""會員與鎖定設定管理；角色門禁先於輸入及目標查詢。"""

from .content import COMMENT_COLUMNS
from .core import (
    ApiError,
    account_resource,
    authenticate,
    authenticate_write,
    connect,
    content_resource,
    success,
)
from .http import api
from .input import json_body, list_input, no_get_body, no_query, pagination, url_integer


# 管理員只搜尋一般會員；SQL 同時負責字面包含、精確計數與排序分頁。
@api("GET")
def members(request):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, keyword = list_input(request, allow_keyword=True)
        # translate 僅折疊 ASCII，strpos 將百分號、底線及反斜線視為普通字元。
        folded = (keyword or "").translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        condition = "WHERE role = 'member'"
        params = ()
        if folded:
            condition += (
                " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
            )
            params = (folded, folded)
        total = connection.execute("SELECT count(*) FROM accounts " + condition, params).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts " + condition
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", (*params, per_page, offset),
        ).fetchall()
        return success({"list": [account_resource(row) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 會員詳情先驗管理員與輸入；管理員 ID 及不存在目標統一 404，避免透露管理帳號。
@api("GET")
def member(request, member_id):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        no_get_body(request)
        no_query(request)
        target_id = url_integer(member_id, "member_id")
        row = connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts "
            "WHERE id = %s AND role = 'member'", (target_id,),
        ).fetchone()
        if row is None:
            raise ApiError("E0004")
        return success(account_resource(row))


# 狀態變更先驗權與輸入，寫入交易再依 ID 鎖帳號及 session 重驗權限。
@api("PUT")
def member_status(request, member_id):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        admin_id = viewer["id"]
    no_query(request)
    target_id = url_integer(member_id, "member_id")
    body = json_body(request, ("status",), content_limits={"status": 0})
    with connect() as connection:
        # 目標與管理員帳號一律依 ID 鎖定，防止停用、登入與寫入交錯。
        rows = connection.execute(
            "SELECT id, role, status FROM accounts WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
            (admin_id, target_id),
        ).fetchall()
        accounts = {row["id"]: row for row in rows}
        # 外層認證只決定是否可驗輸入；實際修改前仍須在帳號與 session 鎖內重驗。
        current, claims = authenticate(request, connection)
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        current, _ = authenticate(request, connection)
        if current["role"] != "admin":
            raise ApiError("E0003")
        target = accounts.get(target_id)
        if target is None or target["role"] != "member":
            raise ApiError("E0004")
        # 同值不重新撤銷；只有 active→disabled 的變更才同交易撤銷所有後端登入。
        if target["status"] != body["status"]:
            connection.execute("UPDATE accounts SET status = %s WHERE id = %s", (body["status"], target_id))
            if body["status"] == "disabled":
                # 帳號鎖阻止新登入；同一提交撤銷全部後端的既有 session。
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = clock_timestamp() "
                    "WHERE account_id = %s AND revoked_at IS NULL", (target_id,),
                )
    return success()


# 管理員留言列表包含個別及父文章下架的留言，不藉文章狀態省略資料。
@api("GET")
def admin_comments(request):
    with connect() as connection:
        # 管理角色先於分頁格式；僅此入口可列全部留言，不因父文章或個別下架而略過。
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, _ = list_input(request)
        total = connection.execute("SELECT count(*) FROM comments").fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + COMMENT_COLUMNS + " FROM comments JOIN accounts ON accounts.id = comments.author_id "
            "ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s", (per_page, offset),
        ).fetchall()
        return success({
            "list": [content_resource(row, viewer, "comment") for row in rows],
            "pagination": pagination(page, per_page, total),
        })


# 管理員讀取或完整更新四個共用鎖定設定欄位；設定缺失屬認證設定錯誤。
@api(("GET", "PUT"))
def login_lockout_settings(request):
    if request.method == "GET":
        with connect() as connection:
            viewer, _ = authenticate(request, connection)
            if viewer["role"] != "admin":
                raise ApiError("E0003")
            no_get_body(request)
            no_query(request)
            row = connection.execute(
                "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
                "FROM login_lockout_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise ApiError("E0104")
            return success(row)
    # PUT 先驗角色再驗完整四欄，不能讓無權者先取得輸入或設定資訊。
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
    no_query(request)
    fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
    body = json_body(
        request, fields, field_types={"enabled": bool, "window_minutes": int,
                                      "failure_threshold": int, "lock_seconds": int},
        content_limits={"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400},
    )
    with connect() as connection:
        # 登入也先鎖這筆設定；真正關閉時依序鎖全部帳號，最後重驗管理員 session。
        row = connection.execute(
            "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
            "FROM login_lockout_settings WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise ApiError("E0104")
        disabling = row["enabled"] and not body["enabled"]
        if disabling:
            connection.execute("SELECT id FROM accounts ORDER BY id FOR UPDATE").fetchall()
        current = authenticate_write(request, connection)
        if current["role"] != "admin":
            raise ApiError("E0003")
        # 最後提交的整組值為準；同值不更新，也不重做關閉鎖定的副作用。
        if any(row[field] != body[field] for field in fields):
            connection.execute(
                "UPDATE login_lockout_settings SET enabled = %s, window_minutes = %s, "
                "failure_threshold = %s, lock_seconds = %s WHERE id = 1",
                tuple(body[field] for field in fields),
            )
            if disabling:
                # 保留歷史失敗事件，但切換世代並解除會員目前的暫時鎖定。
                connection.execute(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                    "WHERE role = 'member'"
                )
    return success()
