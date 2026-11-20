using System.Text.Json.Serialization;

namespace AspNetCore;

// 固定本人與管理員可見的帳號欄位，不讓資料庫憑證或鎖定資料進入回應。
internal sealed record AccountResponse(
    [property: JsonPropertyName("id")] long Id,
    [property: JsonPropertyName("email")] string? Email,
    [property: JsonPropertyName("nickname")] string? Nickname,
    [property: JsonPropertyName("role")] string? Role,
    [property: JsonPropertyName("status")] string? Status,
    [property: JsonPropertyName("created_at")] string CreatedAt);

// 公開內容只附作者識別與暱稱，避免沿用完整帳號而洩漏 Email。
internal sealed record AuthorResponse(
    [property: JsonPropertyName("id")] long Id,
    [property: JsonPropertyName("nickname")] string? Nickname);

// 文章輸出固定公開欄位；恢復資格仍由控制器依目前身分計算。
internal sealed record ArticleResponse(
    [property: JsonPropertyName("id")] long Id,
    [property: JsonPropertyName("title")] string? Title,
    [property: JsonPropertyName("body")] string? Body,
    [property: JsonPropertyName("author")] AuthorResponse Author,
    [property: JsonPropertyName("version")] long Version,
    [property: JsonPropertyName("created_backend")] string? CreatedBackend,
    [property: JsonPropertyName("created_at")] string CreatedAt,
    [property: JsonPropertyName("updated_at")] string UpdatedAt,
    [property: JsonPropertyName("visibility")] string? Visibility,
    [property: JsonPropertyName("can_restore")] bool CanRestore);

// 留言只保留父文章識別，不展開文章內容或其他作者資料。
internal sealed record CommentResponse(
    [property: JsonPropertyName("id")] long Id,
    [property: JsonPropertyName("body")] string? Body,
    [property: JsonPropertyName("author")] AuthorResponse Author,
    [property: JsonPropertyName("article_id")] long ArticleId,
    [property: JsonPropertyName("version")] long Version,
    [property: JsonPropertyName("created_backend")] string? CreatedBackend,
    [property: JsonPropertyName("created_at")] string CreatedAt,
    [property: JsonPropertyName("updated_at")] string UpdatedAt,
    [property: JsonPropertyName("visibility")] string? Visibility,
    [property: JsonPropertyName("can_restore")] bool CanRestore);

// 分頁整數維持既有精度與欄位名稱，不讓序列化命名慣例改變共同契約。
internal sealed record PaginationResponse(
    [property: JsonPropertyName("page")] long Page,
    [property: JsonPropertyName("per_page")] int PerPage,
    [property: JsonPropertyName("total")] long Total,
    [property: JsonPropertyName("total_pages")] long TotalPages);

// 登入回應沿用帳號投影及固定到期文字，不公開 session 或簽章設定。
internal sealed record LoginResponse(
    [property: JsonPropertyName("access_token")] string AccessToken,
    [property: JsonPropertyName("token_type")] string TokenType,
    [property: JsonPropertyName("expires_at")] string ExpiresAt,
    [property: JsonPropertyName("user")] object User);

// 設定只輸出核准的四個值，保留 JSON 布林及整數型態。
internal sealed record LoginLockoutResponse(
    [property: JsonPropertyName("enabled")] bool Enabled,
    [property: JsonPropertyName("window_minutes")] int WindowMinutes,
    [property: JsonPropertyName("failure_threshold")] int FailureThreshold,
    [property: JsonPropertyName("lock_seconds")] int LockSeconds);
