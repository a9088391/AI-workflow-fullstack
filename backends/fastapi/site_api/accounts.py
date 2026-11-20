"""帳號與登入生命週期；交易、失敗計數及撤銷順序由各用途維持。"""

from datetime import timedelta
import secrets

import psycopg

from .core import (
    ApiError,
    account_resource,
    authenticate,
    b64url,
    business_time,
    connect,
    issue_token,
    password_hash,
    success,
    verify_password,
)
from .dto import LoginDto
from .input import json_body, no_get_body, no_query


# 註冊先驗證欄位；唯一索引裁決跨後端同時註冊。
def register(request, _):
    # 公開註冊不沿用舊 JWT；格式驗證須先於帳號查重與新增。
    no_query(request)
    body = json_body(request, ("email", "password", "nickname"))
    with connect() as connection:
        if connection.execute("SELECT 1 FROM accounts WHERE email = %s", (body["email"],)).fetchone():
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]})
        encoded = password_hash(body["password"])
        try:
            # 查重後仍可能有其他後端同時新增，最後由唯一索引裁決並轉為共同文案。
            connection.execute(
                "INSERT INTO accounts (email, password_hash, nickname, role, status) "
                "VALUES (%s, %s, %s, 'member', 'active')",
                (body["email"], encoded, body["nickname"]),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]}) from exc
    return success(status=201)


# 鎖定變更先提交，再以新的 DB 時鐘計算實際剩餘秒數。
def locked_error(connection, until):
    connection.commit()
    # COMMIT 的等待也會消耗鎖定時間；回應只提供原截止的剩餘秒數，不重新起算。
    fresh_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    retry = max(1, -((fresh_now - until) // timedelta(seconds=1)))
    return ApiError("E0105", retry_after=retry)


# 設定列、帳號列鎖定後取 DB 時刻，維持跨後端相同失敗視窗與提交順序。
def login(request, _):
    # 格式錯誤不進入鎖定計數；公開登入也不使用請求可能攜帶的舊 token。
    no_query(request)
    body = json_body(request, ("email", "password"))
    with connect() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
        if settings is None:
            raise ApiError("E0104")
        account = connection.execute("SELECT * FROM accounts WHERE email = %s FOR UPDATE", (body["email"],)).fetchone()
        # 排隊取得設定及帳號鎖後才取一次實際 DB 時間，避免等待時間扭曲視窗或到期判斷。
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if account is None:
            return ApiError("E0101")
        if account["status"] == "disabled":
            return ApiError("E0102")
        # 到期鎖重設世代；未到期鎖維持原截止，不再驗密碼。
        if account["role"] == "member" and account["locked_until"] is not None:
            until = account["locked_until"]
            if settings["enabled"] and now < until:
                return locked_error(connection, until)
            account["failure_epoch"] += 1
            connection.execute(
                "UPDATE accounts SET failure_epoch = %s, locked_until = NULL WHERE id = %s",
                (account["failure_epoch"], account["id"]),
            )
        # 停用與未到期鎖已先處理；只有啟用鎖定的一般會員密碼錯誤能新增事件。
        if not verify_password(body["password"], account["password_hash"]):
            if settings["enabled"] and account["role"] == "member":
                connection.execute(
                    "INSERT INTO login_failures (account_id, failure_epoch, occurred_at) VALUES (%s, %s, %s)",
                    (account["id"], account["failure_epoch"], now),
                )
                count = connection.execute(
                    "SELECT count(*) FROM login_failures WHERE account_id = %s AND failure_epoch = %s "
                    "AND occurred_at > %s AND occurred_at <= %s",
                    (account["id"], account["failure_epoch"], now - timedelta(minutes=settings["window_minutes"]), now),
                ).fetchone()["count"]
                # 本次事件已包含在左開右閉視窗內；達門檻當次就保存原截止並回 429。
                if count >= settings["failure_threshold"]:
                    until = now + timedelta(seconds=settings["lock_seconds"])
                    connection.execute("UPDATE accounts SET locked_until = %s WHERE id = %s", (until, account["id"]))
                    return locked_error(connection, until)
            # 回傳值由 HTTP 邊界轉錯誤，避免例外造成失敗事件 rollback。
            return ApiError("E0101")
        # 成功登入的世代與 session 同交易提交；JWT 與 DB 共用整秒起點及固定八小時。
        issued = int(now.timestamp())
        jti = b64url(secrets.token_bytes(16))
        token = issue_token(account["id"], jti, issued)
        connection.execute(
            "UPDATE accounts SET failure_epoch = failure_epoch + 1, locked_until = NULL WHERE id = %s",
            (account["id"],),
        )
        connection.execute(
            "INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) "
            "VALUES (%s, %s, 'fastapi', to_timestamp(%s), to_timestamp(%s))",
            (jti, account["id"], issued, issued + 28800),
        )
        data: LoginDto = {
            "access_token": token, "token_type": "Bearer",
            "expires_at": business_time(now.replace(microsecond=0) + timedelta(hours=8)),
            "user": account_resource(account),
        }
    return success(data)


# 登出按帳號及 session 順序加鎖並重驗，僅撤銷目前 jti。
def logout(request, _):
    with connect() as connection:
        # 認證先於輸入錯誤；空 Body 與空 object 均可登出。
        account, claims = authenticate(request, connection)
        no_query(request)
        json_body(request, (), empty_allowed=True)
        connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        # 等鎖期間可能已停用、撤銷或重設密碼，不能沿用鎖前的認證結果。
        authenticate(request, connection)
        connection.execute("UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE jti = %s", (claims["jti"],))
    return success()


# me 只讀目前帳號資料，不簽發或延長登入。
def me(request, _):
    with connect() as connection:
        # 先確認目前身分，再拒絕 GET Body 與多餘 Query；讀取不改 session。
        account, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        return success(account_resource(account))
