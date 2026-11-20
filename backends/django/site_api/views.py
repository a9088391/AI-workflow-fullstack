"""Django 健康檢查與未知路由入口；功能路由直接引用用途模組。"""

from django.http import HttpResponse
import psycopg

from .core import ApiError, connect
from .http import api


# 健康檢查要確認共用業務表可讀，避免資料庫失聯時 Compose 仍宣稱就緒。
def health(request):
    try:
        with connect() as connection:
            connection.execute(
                "SELECT 1 FROM accounts, login_sessions, login_failures, "
                "login_lockout_settings, articles, comments LIMIT 0"
            )
    except psycopg.Error:
        return HttpResponse("unavailable", status=503)
    return HttpResponse("OK")


# 未知 API 固定回共同 404，避免落入 Django 的 HTML 錯誤回應。
@api("*")
def not_found(request):
    raise ApiError("E0004")
