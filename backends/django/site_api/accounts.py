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
    failure,
    issue_token,
    password_hash,
    success,
    verify_password,
)
from .dto import LoginDto
from .http import api
from .input import json_body, no_get_body, no_query


# 註冊先驗欄位再檢查唯一 Email；DB unique index 裁決跨後端並行。
@api("POST")
def register(request):
    # 公開註冊不沿用舊 JWT；格式錯誤必須先於 Email 唯一性檢查。
    no_query(request)
    body = json_body(request, ("email", "password", "nickname"))
    with connect() as connection:
        if connection.execute("SELECT 1 FROM accounts WHERE email = %s", (body["email"],)).fetchone():
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]})
        encoded = password_hash(body["password"])
        try:
            # 前置查重不能阻止其他後端同時新增；唯一索引的衝突仍轉為共同 Email 文案。
            connection.execute(
                "INSERT INTO accounts (email, password_hash, nickname, role, status) "
                "VALUES (%s, %s, %s, 'member', 'active')",
                (body["email"], encoded, body["nickname"]),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]}) from exc
    return success(status=201)


# 先完成登入交易才計算鎖定剩餘秒數，避免雜湊或 COMMIT 等待讓 Retry-After 多算。
def locked_response(connection, until, log_id):
    connection.commit()
    # 新 DB 時間只供回應倒數；外層 with 之後只需結束這個讀取交易，不重算登入業務時間。
    now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    retry = max(1, -((now - until) // timedelta(seconds=1)))
    return failure(ApiError("E0105", retry_after=retry), log_id)


# 登入在設定列→帳號列鎖後取 DB 時間，序列化跨後端失敗計數。
@api("POST")
def login(request):
    # 格式錯誤不屬錯誤密碼事件，先完整驗證才進入共用鎖定交易。
    no_query(request)
    body = json_body(request, ("email", "password"))
    with connect() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
        if settings is None:
            raise ApiError("E0104")
        account = connection.execute(
            "SELECT * FROM accounts WHERE email = %s FOR UPDATE", (body["email"],)
        ).fetchone()
        # 鎖後只取一次業務時間 t；帳號不存在及停用先判定，不進入計數或驗密碼。
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if account is None:
            return failure(ApiError("E0101"), request.api_log_id)
        if account["status"] == "disabled":
            return failure(ApiError("E0102"), request.api_log_id)
        # 到期鎖與關閉的鎖先清除並換世代；未到期鎖不驗密碼也不延長。
        if account["role"] == "member" and account["locked_until"] is not None:
            until = account["locked_until"]
            if settings["enabled"] and now < until:
                return locked_response(connection, until, request.api_log_id)
            account["failure_epoch"] += 1
            connection.execute(
                "UPDATE accounts SET failure_epoch = %s, locked_until = NULL WHERE id = %s",
                (account["failure_epoch"], account["id"]),
            )
        if not verify_password(body["password"], account["password_hash"]):
            if settings["enabled"] and account["role"] == "member":
                # 只計目前世代的 (t-N, t]；門檻當次先持久化，再回 429。
                connection.execute(
                    "INSERT INTO login_failures (account_id, failure_epoch, occurred_at) VALUES (%s, %s, %s)",
                    (account["id"], account["failure_epoch"], now),
                )
                count = connection.execute(
                    "SELECT count(*) FROM login_failures WHERE account_id = %s AND failure_epoch = %s "
                    "AND occurred_at > %s AND occurred_at <= %s",
                    (account["id"], account["failure_epoch"], now - timedelta(minutes=settings["window_minutes"]), now),
                ).fetchone()["count"]
                if count >= settings["failure_threshold"]:
                    until = now + timedelta(seconds=settings["lock_seconds"])
                    connection.execute("UPDATE accounts SET locked_until = %s WHERE id = %s", (until, account["id"]))
                    return locked_response(connection, until, request.api_log_id)
            # 正常 return 會讓 with 提交失敗事件；若拋業務例外反而會回滾計數。
            return failure(ApiError("E0101"), request.api_log_id)
        # 成功時換世代並保存與 JWT 同秒數的登入紀錄；金鑰失敗會回滾。
        issued = int(now.timestamp())
        jti = b64url(secrets.token_bytes(16))
        token = issue_token(account["id"], jti, issued)
        connection.execute(
            "UPDATE accounts SET failure_epoch = failure_epoch + 1, locked_until = NULL WHERE id = %s",
            (account["id"],),
        )
        connection.execute(
            "INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) "
            "VALUES (%s, %s, 'django', to_timestamp(%s), to_timestamp(%s))",
            (jti, account["id"], issued, issued + 28800),
        )
        data: LoginDto = {
            "access_token": token, "token_type": "Bearer",
            "expires_at": business_time(now.replace(microsecond=0) + timedelta(hours=8)),
            "user": account_resource(account),
        }
    return success(data)


# 登出只撤銷目前 jti；帳號與 session 依共同順序鎖定到提交。
@api("POST")
def logout(request):
    with connect() as connection:
        identity = authenticate(request, connection)
        no_query(request)
        json_body(request, (), empty_allowed=True)
        account, claims = identity
        # 鎖後重驗，讓登出與其他受保護寫入依同一帳號→session 順序完成。
        connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        authenticate(request, connection)
        connection.execute(
            "UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE jti = %s", (claims["jti"],)
        )
    return success()


# me 以目前資料庫角色與狀態回應，不建立登入或延長到期。
@api("GET")
def me(request):
    with connect() as connection:
        account, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        return success(account_resource(account))
