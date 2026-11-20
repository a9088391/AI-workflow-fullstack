"""FastAPI HTTP 組裝與同步 worker 邊界；用途模組保留共同業務契約。"""

import logging
import uuid

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response
import anyio
import psycopg

from . import accounts, administration, content
from .core import ApiError, connect, failure, jwt_key

LOGGER = logging.getLogger("site_api")
# 每次請求的 IN／EX／OUT 必須可由同一 log_id 在容器 stderr 追查。
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    LOGGER.addHandler(handler)
LOGGER.propagate = False
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)


# 凍結已讀取的 ASGI 原始輸入，供同步資料庫函式執行而不阻塞 event loop。
class Input:
    # 已讀取的原始 Body、Query 與標頭可安全送進同步 worker。
    def __init__(self, request, body):
        self.method = request.method
        self.headers = request.headers
        self.query_string = request.scope.get("query_string", b"")
        self.content_type = request.headers.get("content-type")
        self.body = body

    # 對應共同驗證器讀取 Body 的介面；同一請求只讀一次。
    def get_data(self, cache=True):
        return self.body


# 健康檢查同時確認業務表與正式 JWT 金鑰，避免無法登入卻宣稱就緒。
def health():
    try:
        with connect() as connection:
            # 只檢查所需表可存取，不建立測資；金鑰也有效才回應就緒。
            connection.execute("SELECT 1 FROM accounts, login_sessions, login_failures, "
                               "login_lockout_settings, articles, comments LIMIT 0")
        jwt_key()
    except (psycopg.Error, ApiError):
        return PlainTextResponse("unavailable", status_code=503)
    return PlainTextResponse("OK")


# HTTP 邊界在 worker thread 執行同步 psycopg 與 Argon2，統一回應與 log_id。
async def dispatch(request, operation, article_id=None):
    log_id = str(uuid.uuid4())
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.url.path)
    try:
        # Body 先由 ASGI 讀一次，再凍結輸入交給同步 worker，避免 DB／雜湊阻塞 event loop。
        raw = await request.body()
        result = await anyio.to_thread.run_sync(operation, Input(request, raw), article_id)
        # 登入可在提交交易後回傳業務錯誤；統一序列化不能反過來使該交易 rollback。
        response = failure(result, log_id) if isinstance(result, ApiError) else result
    except ApiError as error:
        if error.code == "E0104":
            LOGGER.error("ERROR log_id=%s code=E0104", log_id)
        response = failure(error, log_id)
    except Exception:
        # DB 不可用等未預期錯誤保持安全的系統回應，不冒充憑證失效或成功。
        LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
        response = failure(ApiError("E9999"), log_id)
    # 成功與失敗都只向固定來源提供 CORS，並讓前端可讀取鎖定剩餘秒數。
    if request.headers.get("origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    # 同一 log_id 串起 IN、錯誤 EX 及 OUT，供共同契約追查實際回應。
    if response.status_code >= 400:
        LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response


# 將同一路徑的讀寫方法綁在一起，OPTIONS 回傳完整允許方法。
def bind(path, operations):
    # 路由 callback 保留原始 ID 給共同驗證器，避免 FastAPI 原生 422 搶先改寫契約。
    async def endpoint(request: Request):
        if request.method == "OPTIONS":
            # 預檢只宣告此路由的方法；實際業務請求仍須通過共同認證及驗證。
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Methods"] = ", ".join((*operations, "OPTIONS"))
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
            if request.headers.get("origin") == "http://localhost:8080":
                response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            return response
        # 三種 ID 都維持網址字串，格式與權限的先後交由對應 operation 裁決。
        content_id = next((request.path_params[key] for key in ("article_id", "comment_id", "member_id")
                           if key in request.path_params), None)
        return await dispatch(request, operations[request.method], content_id)
    app.add_api_route(path, endpoint, methods=[*operations, "OPTIONS"], include_in_schema=False)


bind("/api/auth/register", {"POST": accounts.register})
bind("/api/auth/login", {"POST": accounts.login})
bind("/api/auth/logout", {"POST": accounts.logout})
bind("/api/me", {"GET": accounts.me})
bind("/api/articles", {"GET": content.articles, "POST": content.create_article})
bind("/api/articles/{article_id}", {"GET": content.article, "PUT": content.update_article})
bind("/api/articles/{article_id}/visibility", {"PUT": content.article_visibility})
bind("/api/articles/{article_id}/comments", {"GET": content.comments, "POST": content.create_comment})
bind("/api/me/articles", {"GET": content.my_articles})
bind("/api/me/comments", {"GET": content.my_comments})
bind("/api/comments/{comment_id}", {"GET": content.comment, "PUT": content.update_comment})
bind("/api/comments/{comment_id}/visibility", {"PUT": content.comment_visibility})
bind("/api/members", {"GET": administration.members})
bind("/api/members/{member_id}", {"GET": administration.member})
bind("/api/members/{member_id}/status", {"PUT": administration.member_status})
bind("/api/comments", {"GET": administration.management_comments})
bind("/api/settings/login-lockout", {"GET": administration.login_lockout_settings, "PUT": administration.login_lockout_settings})
app.add_api_route("/up", health, methods=["GET"], include_in_schema=False)


# 不存在路由及不支援方法仍使用共同錯誤格式。
@app.exception_handler(HTTPException)
async def http_error(request, error):
    log_id = str(uuid.uuid4())
    response = failure(ApiError("E0004" if error.status_code == 404 else "E0005"), log_id)
    # 沿用實際 HTTP 狀態（例如 405），只把錯誤格式統一為共同 envelope。
    if error.status_code != 404:
        response.status_code = error.status_code
    # 路由層未進 dispatch，也要提供相同固定來源 CORS 與可追查記錄。
    if request.headers.get("origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.url.path)
    LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response
