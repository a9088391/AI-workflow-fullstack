"""建立 Flask 應用與路由；帳號、內容、管理用途各自維護流程。"""

import logging

from flask import Flask, Response
import psycopg

from . import accounts, administration, content
from .core import ApiError, connect
from .http import LOGGER, route_error


# 健康檢查確認共用業務表可讀，避免資料庫失聯時仍宣稱就緒。
def health():
    try:
        with connect() as connection:
            connection.execute(
                "SELECT 1 FROM accounts, login_sessions, login_failures, "
                "login_lockout_settings, articles, comments LIMIT 0"
            )
    except psycopg.Error:
        return Response("unavailable", status=503)
    return Response("OK")


# Flask 路由明確列出已實作端點，OPTIONS 也經過共同 CORS 邊界。
def create_app():
    app = Flask(__name__)
    # Flask 預設不輸出本模組 INFO；只安裝一次 stderr handler 並避免 root 重複輸出。
    LOGGER.setLevel(logging.INFO)
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        LOGGER.addHandler(handler)
    LOGGER.propagate = False
    app.add_url_rule("/up", view_func=health)
    routes = (
        ("/api/auth/register", accounts.register, ("POST",)),
        ("/api/auth/login", accounts.login, ("POST",)),
        ("/api/auth/logout", accounts.logout, ("POST",)),
        ("/api/me", accounts.me, ("GET",)),
        ("/api/articles", content.articles, ("GET", "POST")),
        ("/api/articles/<article_id>", content.article, ("GET", "PUT")),
        ("/api/articles/<article_id>/visibility", content.article_visibility, ("PUT",)),
        ("/api/articles/<article_id>/comments", content.comments, ("GET", "POST")),
        ("/api/me/articles", content.my_articles, ("GET",)),
        ("/api/me/comments", content.my_comments, ("GET",)),
        ("/api/comments/<comment_id>", content.comment, ("GET", "PUT")),
        ("/api/comments/<comment_id>/visibility", content.comment_visibility, ("PUT",)),
        ("/api/members", administration.members, ("GET",)),
        ("/api/members/<member_id>", administration.member, ("GET",)),
        ("/api/members/<member_id>/status", administration.member_status, ("PUT",)),
        ("/api/comments", administration.management_comments, ("GET",)),
        ("/api/settings/login-lockout", administration.login_lockout_settings, ("GET", "PUT")),
    )
    for path, view, methods in routes:
        app.add_url_rule(path, view_func=view, methods=(*methods, "OPTIONS"), provide_automatic_options=False)

    # 不存在路徑仍使用共同錯誤 envelope，避免框架 HTML 錯誤頁。
    @app.errorhandler(404)
    def missing_route(error):
        return route_error(ApiError("E0004"))

    # 方法不支援時保留 HTTP 405，並輸出共同錯誤欄位。
    @app.errorhandler(405)
    def unsupported_method(error):
        return route_error(ApiError("E0005"), status=405)

    return app
