"""Django 僅提供 HTTP 入口；業務表由共用 SQL 建立與維護。"""

import secrets

# 本服務不使用 Django session；隨機內部金鑰避免誤用固定開發字串。
SECRET_KEY = secrets.token_urlsafe(32)
DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "django"]
ROOT_URLCONF = "site_api.urls"
INSTALLED_APPS = []
MIDDLEWARE = []
DATABASES = {}
USE_TZ = True
TIME_ZONE = "Asia/Taipei"
DEFAULT_CHARSET = "utf-8"
# API log_id 的 IN／EX／OUT 送往容器標準輸出，避免記錄憑證或請求本文。
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"site_api": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
