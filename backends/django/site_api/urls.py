"""依共同契約列出本票實作的路由，其他操作不得偽裝成功。"""

from django.urls import re_path

from . import accounts, administration, content, views

urlpatterns = [
    re_path(r"^up$", views.health),
    re_path(r"^api/auth/register/?$", accounts.register),
    re_path(r"^api/auth/login/?$", accounts.login),
    re_path(r"^api/auth/logout/?$", accounts.logout),
    re_path(r"^api/me/?$", accounts.me),
    re_path(r"^api/me/articles/?$", content.my_articles),
    re_path(r"^api/me/comments/?$", content.my_comments),
    re_path(r"^api/members/?$", administration.members),
    re_path(r"^api/members/(?P<member_id>[^/]+)/status/?$", administration.member_status),
    re_path(r"^api/members/(?P<member_id>[^/]+)/?$", administration.member),
    re_path(r"^api/settings/login-lockout/?$", administration.login_lockout_settings),
    re_path(r"^api/articles/?$", content.articles),
    re_path(r"^api/comments/?$", administration.admin_comments),
    re_path(r"^api/articles/(?P<article_id>[^/]+)/visibility/?$", content.article_visibility),
    re_path(r"^api/articles/(?P<article_id>[^/]+)/comments/?$", content.comments),
    re_path(r"^api/articles/(?P<article_id>[^/]+)/?$", content.article),
    re_path(r"^api/comments/(?P<comment_id>[^/]+)/visibility/?$", content.comment_visibility),
    re_path(r"^api/comments/(?P<comment_id>[^/]+)/?$", content.comment),
    re_path(r"^api/?$", views.not_found),
    re_path(r"^api/.*$", views.not_found),
]
