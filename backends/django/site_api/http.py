"""集中方法、CORS、日誌與安全錯誤回應，業務用途共用同一邊界。"""

import logging
import uuid

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .core import ApiError, failure

LOGGER = logging.getLogger("site_api")


# 對每次 API 呼叫統一處理方法、CORS、log_id 與安全錯誤格式。
def api(method):
    # 包裝各端點，集中記錄 IN/EX/OUT 而不記錄密碼、token 或 Body。
    def decorate(operation):
        # 捕捉業務與系統錯誤，並保持同一 log_id 供伺服器追查。
        @csrf_exempt
        def wrapped(request, *args, **kwargs):
            log_id = str(uuid.uuid4())
            request.api_log_id = log_id
            origin = request.headers.get("Origin")
            LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.path)
            try:
                # 同一路徑可共用 GET 與寫入方法；其餘方法仍回共同 405 格式。
                methods = (method,) if isinstance(method, str) else method
                if request.method == "OPTIONS":
                    response = HttpResponse(status=204)
                    response["Access-Control-Allow-Methods"] = (
                        "GET, POST, PUT, DELETE" if method == "*" else ", ".join(methods)
                    ) + ", OPTIONS"
                    response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
                elif method != "*" and request.method not in methods:
                    error = ApiError("E0005")
                    response = failure(error, log_id)
                    response.status_code = 405
                else:
                    response = operation(request, *args, **kwargs)
            except ApiError as error:
                # 毀損 PHC 等認證設定異常只記固定代碼與 log_id，不記密碼或雜湊。
                if error.code == "E0104":
                    LOGGER.error("ERROR log_id=%s code=E0104", log_id)
                response = failure(error, log_id)
            except Exception:
                LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
                response = failure(ApiError("E9999"), log_id)
            # 只有固定前端入口可讀跨來源回應；倒數秒數由同一 Retry-After 標頭提供。
            if origin == "http://localhost:8080":
                response["Access-Control-Allow-Origin"] = origin
                response["Vary"] = "Origin"
                response["Access-Control-Expose-Headers"] = "Retry-After"
            # 業務錯誤也要留下同次 EX／OUT，讓前端回報的 log_id 可追查。
            if response.status_code >= 400:
                LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
            LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
            return response
        return wrapped
    return decorate
