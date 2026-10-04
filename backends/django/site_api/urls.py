"""依共同契約列出本票實作的路由，其他操作不得偽裝成功。"""

from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^up$", views.health),
    re_path(r"^api/auth/register/?$", views.register),
    re_path(r"^api/auth/login/?$", views.login),
    re_path(r"^api/auth/logout/?$", views.logout),
    re_path(r"^api/me/?$", views.me),
    re_path(r"^api/me/articles/?$", views.my_articles),
    re_path(r"^api/me/comments/?$", views.my_comments),
    re_path(r"^api/articles/?$", views.articles),
    re_path(r"^api/articles/(?P<article_id>[^/]+)/visibility/?$", views.article_visibility),
    re_path(r"^api/articles/(?P<article_id>[^/]+)/comments/?$", views.comments),
    re_path(r"^api/articles/(?P<article_id>[^/]+)/?$", views.article),
    re_path(r"^api/comments/(?P<comment_id>[^/]+)/visibility/?$", views.comment_visibility),
    re_path(r"^api/comments/(?P<comment_id>[^/]+)/?$", views.comment),
    re_path(r"^api/?$", views.not_found),
    re_path(r"^api/.*$", views.not_found),
]
