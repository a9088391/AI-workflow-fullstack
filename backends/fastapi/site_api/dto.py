"""HTTP 安全回應的欄位形狀；只作型別說明，不解析、轉型或改變驗證順序。"""

from typing import Literal, TypedDict


# 帳號回應只包含已核准欄位，密碼雜湊與鎖定內部狀態不在邊界內。
class AccountDto(TypedDict):
    id: int
    email: str
    nickname: str
    role: Literal["member", "admin"]
    status: Literal["active", "disabled"]
    created_at: str


# 內容作者只公開識別碼與暱稱，避免把 Account 的 Email 一起輸出。
class PublicAuthorDto(TypedDict):
    id: int
    nickname: str


# 文章與留言共用的讀取欄位；可讀性由用途先驗證，恢復旗標只供顯示。
class ContentDto(TypedDict):
    id: int
    body: str
    author: PublicAuthorDto
    version: int
    created_backend: str
    created_at: str
    updated_at: str
    visibility: Literal["visible", "hidden"]
    can_restore: bool


# 文章沿用完整內文與標題，列表和詳情使用相同欄位。
class ArticleDto(ContentDto):
    title: str


# 留言只帶父文章 ID，不附帶可能不可讀的父文章標題或內文。
class CommentDto(ContentDto):
    article_id: int


# 分頁保留原頁碼與實際總數，超頁不改寫成最後一頁。
class PaginationDto(TypedDict):
    page: int
    per_page: int
    total: int
    total_pages: int


# 登入回應描述同一次八小時登入，不增設 refresh 或延長期限。
class LoginDto(TypedDict):
    access_token: str
    token_type: Literal["Bearer"]
    expires_at: str
    user: AccountDto
