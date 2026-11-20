using System.Globalization;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Mvc;

namespace AspNetCore;

/// <summary>共用身分鎖序、資源輸出與時間格式，避免各端點偏離核准契約。</summary>
/// <param name="jwt">共用啟動時載入的簽章服務，讓各控制器使用同一後端金鑰。</param>
public abstract class ApiControllerBase(JwtToken jwt) : ControllerBase
{
    protected JwtToken Jwt { get; } = jwt;

    /// <summary>保留本次交易驗證過的帳號及登入識別，供授權與目前登入撤銷共用。</summary>
    protected sealed record Identity(Dictionary<string, object?> Account, string Jti);

    /// <summary>可選登入只有完全缺少 Authorization 才視為訪客；有值但無效必須回 401。</summary>
    private protected async Task<Identity?> AuthenticateAsync(Database db, bool optional, CancellationToken cancellationToken)
    {
        // 只有完全缺少標頭才允許訪客；先驗 Bearer 格式與簽章，才用可信識別查資料庫。
        if (!Request.Headers.TryGetValue("Authorization", out var header))
        {
            if (optional) return null;
            throw new ApiFailure("E0002");
        }
        if (header.Count != 1) throw new ApiFailure("E0002");
        var match = Regex.Match(header[0] ?? "", @"\ABearer ([^\s]+)\z", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        if (!match.Success) throw new ApiFailure("E0002");
        var (accountId, jti, issued, expires) = Jwt.Verify(match.Groups[1].Value, DateTimeOffset.UtcNow.ToUnixTimeSeconds());

        // 帳號→登入紀錄依共同鎖序取得，停用與撤銷以同一穩定狀態裁決。
        var account = await db.RowAsync("SELECT * FROM accounts WHERE id = @id FOR UPDATE", new() { ["id"] = accountId }, cancellationToken);
        var session = await db.RowAsync("SELECT * FROM login_sessions WHERE jti = @jti FOR UPDATE", new() { ["jti"] = jti }, cancellationToken);
        var now = new DateTimeOffset(await db.NowAsync(cancellationToken)).ToUnixTimeSeconds();
        // 等鎖後再驗時效及登入關聯，避免使用等待前的時間；身分可確認後，停用優先於撤銷。
        if (now < issued || now >= expires || account is null || session is null
            || Long(session, "account_id") != accountId || String(session, "backend_id") != "aspnet-core"
            || Unix(session, "issued_at") != issued || Unix(session, "expires_at") != expires) throw new ApiFailure("E0002");
        if (String(account, "status") == "disabled") throw new ApiFailure("E0102");
        if (session["revoked_at"] is not null) throw new ApiFailure("E0002");
        return new Identity(account, jti);
    }

    /// <summary>帳號輸出只含本人可見的六個欄位，不輸出雜湊、鎖定與登入紀錄。</summary>
    protected static object AccountResource(Dictionary<string, object?> account) => new AccountResponse(
        Long(account, "id"),
        String(account, "email"),
        String(account, "nickname"),
        String(account, "role"),
        String(account, "status"),
        Database.DisplayTime(Date(account, "created_at")));

    /// <summary>文章只包含公開作者及核准的狀態欄位，恢復能力依目前身分計算。</summary>
    protected static object ArticleResource(Dictionary<string, object?> row, Identity? viewer)
    {
        var own = viewer is not null && Long(viewer.Account, "id") == Long(row, "author_id");
        return new ArticleResponse(
            Long(row, "id"),
            String(row, "title"),
            String(row, "body"),
            new AuthorResponse(Long(row, "author_id"), String(row, "nickname")),
            Long(row, "version"),
            String(row, "created_backend"),
            Database.DisplayTime(Date(row, "created_at")),
            Database.DisplayTime(Date(row, "updated_at")),
            String(row, "visibility"),
            String(row, "visibility") == "hidden" && (viewer is not null && (String(viewer.Account, "role") == "admin" || own && String(row, "restore_control") == "author")));
    }

    /// <summary>留言不附父文章或作者 Email，避免列表擴張公開資料。</summary>
    protected static object CommentResource(Dictionary<string, object?> row, Identity? viewer)
    {
        var own = viewer is not null && Long(viewer.Account, "id") == Long(row, "author_id");
        return new CommentResponse(
            Long(row, "id"),
            String(row, "body"),
            new AuthorResponse(Long(row, "author_id"), String(row, "nickname")),
            Long(row, "article_id"),
            Long(row, "version"),
            String(row, "created_backend"),
            Database.DisplayTime(Date(row, "created_at")),
            Database.DisplayTime(Date(row, "updated_at")),
            String(row, "visibility"),
            String(row, "visibility") == "hidden" && (viewer is not null && (String(viewer.Account, "role") == "admin" || own && String(row, "restore_control") == "author")));
    }

    /// <summary>分頁保留原頁碼及資料庫精確總數，超頁回空列表而不修改 total。</summary>
    protected static object Pagination(long page, int perPage, long total) => new PaginationResponse(page, perPage, total, total / perPage + (total % perPage == 0 ? 0 : 1));

    /// <summary>取欄位整數時維持 PostgreSQL bigint，不經浮點數而丟失安全整數精度。</summary>
    protected static long Long(Dictionary<string, object?> row, string name) => Convert.ToInt64(row[name], CultureInfo.InvariantCulture);

    /// <summary>只在內部查詢結果取文字；NULL 狀態欄位由呼叫者判斷。</summary>
    protected static string? String(Dictionary<string, object?> row, string name) => row[name] as string;

    /// <summary>PostgreSQL timestamptz 轉 UTC DateTime，供 JWT 時間與臺北輸出共用。</summary>
    protected static DateTime Date(Dictionary<string, object?> row, string name) => (DateTime)(row[name] ?? throw new InvalidOperationException(name));

    /// <summary>登入紀錄秒數需與 JWT claims 完全一致，不能用回應格式反推。</summary>
    private static long Unix(Dictionary<string, object?> row, string name) => new DateTimeOffset(Date(row, name)).ToUnixTimeSeconds();

    /// <summary>統一產生失敗 IActionResult，供已提交業務錯誤使用。</summary>
    protected IActionResult Failure(string code, int? retryAfter = null) => ApiEnvelope.Failure(HttpContext, new ApiFailure(code, retryAfter: retryAfter));
}
