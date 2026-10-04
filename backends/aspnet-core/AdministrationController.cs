using Microsoft.AspNetCore.Mvc;

namespace AspNetCore;

/// <summary>管理員以共同交易與資源格式管理會員、留言及登入鎖定設定。</summary>
[ApiController]
public sealed class AdministrationController(JwtToken jwt) : ApiControllerBase(jwt)
{
    /// <summary>僅列一般會員；篩選、精確計數及分頁在 PostgreSQL 完成。</summary>
    [HttpGet("/api/members")]
    public async Task<IActionResult> Members(CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        await AdminAsync(db, cancellationToken);
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        var (page, perPage, keyword) = ApiInput.ListQuery(Request, false, true);
        // translate 僅折疊 ASCII；strpos 把 %, _ 與反斜線當文字，不解讀成 LIKE 萬用字元。
        const string folded = "translate(@keyword, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')";
        var where = " FROM accounts WHERE role = 'member'" + (string.IsNullOrEmpty(keyword) ? "" :
            $" AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {folded}) > 0 OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {folded}) > 0)");
        var parameters = new Dictionary<string, object?>();
        if (!string.IsNullOrEmpty(keyword)) parameters["keyword"] = keyword;
        var total = Convert.ToInt64(await db.ScalarAsync("SELECT count(*)" + where, parameters, cancellationToken));
        var list = new List<object>();
        // 保留精確總數；深頁超出資料或 OFFSET 可表示範圍時，空列表不必再查詢。
        if (page - 1 <= long.MaxValue / perPage && (page - 1) * perPage < total)
        {
            parameters["limit"] = perPage;
            parameters["offset"] = (page - 1) * perPage;
            var rows = await db.RowsAsync("SELECT id, email, nickname, role, status, created_at" + where + " ORDER BY created_at DESC, id DESC LIMIT @limit OFFSET @offset", parameters, cancellationToken);
            foreach (var row in rows) list.Add(AccountResource(row));
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { list, pagination = Pagination(page, perPage, total) });
    }

    /// <summary>管理員只可讀一般會員；管理員 ID 與不存在的 ID 都隱藏為 404。</summary>
    [HttpGet("/api/members/{member_id}")]
    public async Task<IActionResult> Member(string member_id, CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        await AdminAsync(db, cancellationToken);
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(member_id, "member_id");
        var account = await db.RowAsync("SELECT id, email, nickname, role, status, created_at FROM accounts WHERE id = @id AND role = 'member'", new() { ["id"] = id }, cancellationToken);
        if (account is null) throw new ApiFailure("E0004");
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(AccountResource(account));
    }

    /// <summary>狀態寫入先驗權及輸入，再按 ID 鎖雙方帳號與 session 重驗；停用才撤銷舊登入。</summary>
    [HttpPut("/api/members/{member_id}/status")]
    public async Task<IActionResult> MemberStatus(string member_id, CancellationToken cancellationToken)
    {
        long adminId;
        await using (var preflight = await Database.OpenAsync(cancellationToken))
        {
            await preflight.BeginAsync(cancellationToken);
            adminId = Long((await AdminAsync(preflight, cancellationToken)).Account, "id");
            await preflight.CommitAsync(cancellationToken);
        }
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(member_id, "member_id");
        var (body, _) = await ApiInput.ContentBodyAsync(Request, ["status"], cancellationToken);

        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        // 先鎖兩個帳號中較小的 ID；AuthenticateAsync 再鎖管理員 session，避免反向鎖序。
        foreach (var accountId in new[] { adminId, id }.Distinct().Order())
            await db.RowAsync("SELECT id FROM accounts WHERE id = @id FOR UPDATE", new() { ["id"] = accountId }, cancellationToken);
        await AdminAsync(db, cancellationToken);
        var target = await db.RowAsync("SELECT id, role, status FROM accounts WHERE id = @id", new() { ["id"] = id }, cancellationToken);
        if (target is null || String(target, "role") != "member") throw new ApiFailure("E0004");
        if (String(target, "status") != body["status"])
        {
            await db.ExecuteAsync("UPDATE accounts SET status = @status WHERE id = @id", new() { ["id"] = id, ["status"] = body["status"] }, cancellationToken);
            if (body["status"] == "disabled")
                await db.ExecuteAsync("UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE account_id = @id AND revoked_at IS NULL", new() { ["id"] = id }, cancellationToken);
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { });
    }

    /// <summary>管理留言涵蓋個別下架及父文章下架內容，不從父文章可見性過濾。</summary>
    [HttpGet("/api/comments")]
    public async Task<IActionResult> Comments(CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AdminAsync(db, cancellationToken);
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        var (page, perPage, _) = ApiInput.ListQuery(Request, false);
        const string from = " FROM comments JOIN accounts ON accounts.id = comments.author_id";
        var total = Convert.ToInt64(await db.ScalarAsync("SELECT count(*)" + from, cancellationToken: cancellationToken));
        var list = new List<object>();
        // 保留精確總數；深頁超出資料或 OFFSET 可表示範圍時，空列表不必再查詢。
        if (page - 1 <= long.MaxValue / perPage && (page - 1) * perPage < total)
        {
            var rows = await db.RowsAsync("SELECT comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, comments.restore_control, comments.version, comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname" + from + " ORDER BY comments.created_at DESC, comments.id DESC LIMIT @limit OFFSET @offset",
                new() { ["limit"] = perPage, ["offset"] = (page - 1) * perPage }, cancellationToken);
            foreach (var row in rows) list.Add(CommentResource(row, viewer));
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { list, pagination = Pagination(page, perPage, total) });
    }

    /// <summary>只回共用登入鎖定設定的四個公開欄位，不洩漏其他資料。</summary>
    [HttpGet("/api/settings/login-lockout")]
    public async Task<IActionResult> LoginLockout(CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        await AdminAsync(db, cancellationToken);
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        ApiInput.NoQuery(Request);
        var row = await db.RowAsync("SELECT enabled, window_minutes, failure_threshold, lock_seconds FROM login_lockout_settings WHERE id = 1", cancellationToken: cancellationToken) ?? throw new ApiFailure("E0104");
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(SettingsResource(row));
    }

    /// <summary>完整覆寫設定；真正關閉時在同一交易重設會員鎖定與失敗世代。</summary>
    [HttpPut("/api/settings/login-lockout")]
    public async Task<IActionResult> UpdateLoginLockout(CancellationToken cancellationToken)
    {
        await using (var preflight = await Database.OpenAsync(cancellationToken))
        {
            await preflight.BeginAsync(cancellationToken);
            await AdminAsync(preflight, cancellationToken);
            await preflight.CommitAsync(cancellationToken);
        }
        ApiInput.NoQuery(Request);
        var next = await ApiInput.SettingsBodyAsync(Request, cancellationToken);
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        // 鎖序固定為設定→帳號 ID 遞增→登入紀錄；第二次驗權防止短交易後角色或憑證變動。
        var current = await db.RowAsync("SELECT enabled, window_minutes, failure_threshold, lock_seconds FROM login_lockout_settings WHERE id = 1 FOR UPDATE", cancellationToken: cancellationToken) ?? throw new ApiFailure("E0104");
        var disabling = (bool)(current["enabled"] ?? false) && !next.Enabled;
        if (disabling)
        {
            await db.RowsAsync("SELECT id FROM accounts ORDER BY id FOR UPDATE", cancellationToken: cancellationToken);
        }
        await AdminAsync(db, cancellationToken);
        if ((bool)(current["enabled"] ?? false) != next.Enabled || Convert.ToInt32(current["window_minutes"]) != next.WindowMinutes
            || Convert.ToInt32(current["failure_threshold"]) != next.FailureThreshold || Convert.ToInt32(current["lock_seconds"]) != next.LockSeconds)
        {
            await db.ExecuteAsync("UPDATE login_lockout_settings SET enabled = @enabled, window_minutes = @window, failure_threshold = @threshold, lock_seconds = @seconds WHERE id = 1",
                new() { ["enabled"] = next.Enabled, ["window"] = next.WindowMinutes, ["threshold"] = next.FailureThreshold, ["seconds"] = next.LockSeconds }, cancellationToken);
            if (disabling) await db.ExecuteAsync("UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 WHERE role = 'member'", cancellationToken: cancellationToken);
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { });
    }

    /// <summary>管理門禁必須在參數與目標檢查前完成，避免一般會員探測管理資料。</summary>
    private async Task<Identity> AdminAsync(Database db, CancellationToken cancellationToken)
    {
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        if (String(viewer.Account, "role") != "admin") throw new ApiFailure("E0003");
        return viewer;
    }

    /// <summary>設定資源只含四個共同契約欄位，維持 JSON 布林與整數型態。</summary>
    private static object SettingsResource(Dictionary<string, object?> row) => new
    {
        enabled = (bool)(row["enabled"] ?? false),
        window_minutes = Convert.ToInt32(row["window_minutes"]),
        failure_threshold = Convert.ToInt32(row["failure_threshold"]),
        lock_seconds = Convert.ToInt32(row["lock_seconds"])
    };
}
