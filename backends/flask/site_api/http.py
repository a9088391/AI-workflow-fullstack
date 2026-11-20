"""集中方法、CORS、日誌與安全錯誤回應，業務用途共用同一邊界。"""

from functools import wraps
import logging
import uuid

from flask import g, make_response, request

from .core import ApiError, failure

LOGGER = logging.getLogger("site_api")


# 對每次 API 呼叫統一處理方法、CORS、log_id 與安全錯誤格式。
def api(method):
    # 包裝各端點，集中記錄 IN/EX/OUT 而不記錄密碼、token 或 Body。
    def decorate(operation):
        # 捕捉業務與系統錯誤，保持同一 log_id 供伺服器追查。
        @wraps(operation)
        def wrapped(**kwargs):
            log_id = str(uuid.uuid4())
            g.api_log_id = log_id
            origin = request.headers.get("Origin")
            LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.path)
            try:
                if request.method == "OPTIONS":
                    response = make_response("", 204)
                    response.headers["Access-Control-Allow-Methods"] = ", ".join((method,) if isinstance(method, str) else method) + ", OPTIONS"
                    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
                else:
                    response = operation(request, **kwargs)
            except ApiError as error:
                # 認證設定錯誤只記代碼，不記密碼、token 或雜湊。
                if error.code == "E0104":
                    LOGGER.error("ERROR log_id=%s code=E0104", log_id)
                response = failure(error, log_id)
            except Exception:
                LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
                response = failure(ApiError("E9999"), log_id)
            # 成功與失敗共用來源白名單，讓登入鎖定的 Retry-After 可由前端讀取。
            if origin == "http://localhost:8080":
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            if response.status_code >= 400:
                LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
            LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
            return response
        return wrapped
    return decorate


# 路由層錯誤也留下可追查 log_id，並讓已允許來源取得相同 CORS 標頭。
def route_error(error, status=None):
    log_id = str(uuid.uuid4())
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.path)
    response = failure(error, log_id)
    if status is not None:
        response.status_code = status
    if request.headers.get("Origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response
