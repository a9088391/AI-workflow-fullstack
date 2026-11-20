using System.Security.Cryptography;
using Microsoft.AspNetCore.Mvc;
using Npgsql;

namespace AspNetCore;

/// <summary>實作共同註冊、登入、登出及本人查核，保留帳號鎖定和撤銷的跨後端順序。</summary>
/// <param name="jwt">注入本後端固定簽章服務，註冊或請求處理時不另生金鑰。</param>
[ApiController]
public sealed class AuthController(JwtToken jwt) : ApiControllerBase(jwt)
{
    /// <summary>註冊固定 member/active，Email 唯一索引裁決跨後端並行重複。</summary>
    [HttpPost("/api/auth/register")]
    public async Task<IActionResult> Register(CancellationToken cancellationToken)
    {
        // 先拒絕非法輸入並查既有 Email，避免無效請求進行昂貴雜湊或資料寫入。
        ApiInput.NoQuery(Request);
        var body = await ApiInput.BodyAsync(Request, ["email", "password", "nickname"], false, cancellationToken);
        await using var db = await Database.OpenAsync(cancellationToken);
        var existing = await db.ScalarAsync("SELECT 1 FROM accounts WHERE email = @email", new() { ["email"] = body["email"] }, cancellationToken);
        if (existing is not null) throw new ApiFailure("E0001", new() { ["email"] = ["此 Email 已註冊"] });
        var hash = await PasswordHash.CreateAsync(body["password"]);
        // 預查不能阻止並行註冊；最終由唯一索引裁決，仍回相同欄位錯誤。
        try
        {
            await db.ExecuteAsync("INSERT INTO accounts (email, password_hash, nickname, role, status) VALUES (@email, @hash, @nickname, 'member', 'active')",
                new() { ["email"] = body["email"], ["hash"] = hash, ["nickname"] = body["nickname"] }, cancellationToken);
        }
        catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
        {
            throw new ApiFailure("E0001", new() { ["email"] = ["此 Email 已註冊"] });
        }
        return ApiEnvelope.Success(new { }, 201);
    }

    /// <summary>先驗格式，再依設定列→帳號列取鎖，以鎖後 DB 時間提交失敗計數或八小時登入。</summary>
    [HttpPost("/api/auth/login")]
    public async Task<IActionResult> Login(CancellationToken cancellationToken)
    {
        // 帳密格式先於任何計數或加鎖；鎖定設定列序列化登入與設定更新。
        ApiInput.NoQuery(Request);
        var body = await ApiInput.BodyAsync(Request, ["email", "password"], false, cancellationToken);
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var settings = await db.RowAsync("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE", cancellationToken: cancellationToken) ?? throw new ApiFailure("E0104");
        var account = await db.RowAsync("SELECT * FROM accounts WHERE email = @email FOR UPDATE", new() { ["email"] = body["email"] }, cancellationToken);
        var now = await db.NowAsync(cancellationToken);
        // 未知或停用帳號不驗密碼、不累計，停用也必須優先於既有暫時鎖定。
        if (account is null)
        {
            await db.CommitAsync(cancellationToken);
            return Failure("E0101");
        }
        if (String(account, "status") == "disabled")
        {
            await db.CommitAsync(cancellationToken);
            return Failure("E0102");
        }

        // 到期或關閉設定的舊鎖先清除世代；有效鎖絕不驗密碼或延長期限。
        var member = String(account, "role") == "member";
        var enabled = (bool)(settings["enabled"] ?? false);
        var epoch = Long(account, "failure_epoch");
        if (member && account["locked_until"] is DateTime until)
        {
            if (enabled && now < until)
            {
                await db.CommitAsync(cancellationToken);
                return Failure("E0105", SecondsLeft(now, until));
            }
            epoch++;
            await db.ExecuteAsync("UPDATE accounts SET failure_epoch = @epoch, locked_until = NULL WHERE id = @id", new() { ["epoch"] = epoch, ["id"] = Long(account, "id") }, cancellationToken);
        }
        // 只有啟用鎖定的一般會員錯密碼才加入目前世代，管理員及格式錯誤雜湊不累計。
        if (!await PasswordHash.VerifyAsync(body["password"], String(account, "password_hash") ?? ""))
        {
            if (enabled && member)
            {
                await db.ExecuteAsync("INSERT INTO login_failures (account_id, failure_epoch, occurred_at) VALUES (@id, @epoch, @now)",
                    new() { ["id"] = Long(account, "id"), ["epoch"] = epoch, ["now"] = now }, cancellationToken);
                var windowMinutes = Convert.ToInt32(settings["window_minutes"]);
                // 滑動視窗左開右閉且含本次失敗；達門檻當次先提交鎖定，再回 429。
                var count = Convert.ToInt64(await db.ScalarAsync("SELECT count(*) FROM login_failures WHERE account_id = @id AND failure_epoch = @epoch AND occurred_at > @start AND occurred_at <= @now",
                    new() { ["id"] = Long(account, "id"), ["epoch"] = epoch, ["start"] = now.AddMinutes(-windowMinutes), ["now"] = now }, cancellationToken));
                if (count >= Convert.ToInt32(settings["failure_threshold"]))
                {
                    until = now.AddSeconds(Convert.ToInt32(settings["lock_seconds"]));
                    await db.ExecuteAsync("UPDATE accounts SET locked_until = @until WHERE id = @id", new() { ["until"] = until, ["id"] = Long(account, "id") }, cancellationToken);
                    await db.CommitAsync(cancellationToken);
                    return Failure("E0105", SecondsLeft(now, until));
                }
            }
            await db.CommitAsync(cancellationToken);
            return Failure("E0101");
        }

        // session 寫入與計數重設共用交易；簽章金鑰錯誤不得留下成功登入。
        var issued = new DateTimeOffset(now).ToUnixTimeSeconds();
        var expires = issued + 28800;
        var jti = JwtToken.Encode(RandomNumberGenerator.GetBytes(16));
        var token = Jwt.Issue(Long(account, "id"), jti, issued);
        await db.ExecuteAsync("UPDATE accounts SET failure_epoch = @epoch, locked_until = NULL WHERE id = @id", new() { ["epoch"] = epoch + 1, ["id"] = Long(account, "id") }, cancellationToken);
        await db.ExecuteAsync("INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) VALUES (@jti, @id, 'aspnet-core', @issued, @expires)",
            new() { ["jti"] = jti, ["id"] = Long(account, "id"), ["issued"] = DateTimeOffset.FromUnixTimeSeconds(issued).UtcDateTime, ["expires"] = DateTimeOffset.FromUnixTimeSeconds(expires).UtcDateTime }, cancellationToken);
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new LoginResponse(token, "Bearer", Database.DisplayTime(DateTimeOffset.FromUnixTimeSeconds(expires).UtcDateTime), AccountResource(account)));
    }

    /// <summary>撤銷目前 jti 並提交後回成功，不影響同帳號其他獨立登入。</summary>
    [HttpPost("/api/auth/logout")]
    public async Task<IActionResult> Logout(CancellationToken cancellationToken)
    {
        // 身分查核與撤銷持有相同行鎖；非法 Body 不能讓登出意外部分完成。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var identity = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        ApiInput.NoQuery(Request);
        await ApiInput.BodyAsync(Request, [], true, cancellationToken);
        await db.ExecuteAsync("UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE jti = @jti", new() { ["jti"] = identity.Jti }, cancellationToken);
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { });
    }

    /// <summary>以目前共用帳號狀態回本人資料，既有 token 與到期時間都不更動。</summary>
    [HttpGet("/api/me")]
    public async Task<IActionResult> Me(CancellationToken cancellationToken)
    {
        // 在交易內讀取目前身分與帳號狀態，查核成功也不新建或延長 session。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var identity = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        // GET 非空 Body 在認證後優先拒絕，不能讓未知 Query 蓋過共同的 Body 錯誤。
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        ApiInput.NoQuery(Request);
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(AccountResource(identity.Account));
    }

    /// <summary>Retry-After 向上取整且至少一秒，前端倒數以伺服器鎖定到期為準。</summary>
    private static int SecondsLeft(DateTime now, DateTime until) => Math.Max(1, (int)Math.Ceiling((until - now).TotalSeconds));
}
