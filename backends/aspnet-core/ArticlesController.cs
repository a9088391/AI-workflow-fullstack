using Microsoft.AspNetCore.Mvc;

namespace AspNetCore;

/// <summary>公開閱讀文章與留言，帶有效 JWT 才擴張作者預覽或管理員範圍。</summary>
[ApiController]
public sealed class ArticlesController(JwtToken jwt) : ApiControllerBase(jwt)
{
    /// <summary>先查核可選登入與管理角色，再在 PostgreSQL 精確計數、排序及分頁。</summary>
    [HttpGet("/api/articles")]
    public async Task<IActionResult> List(CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var management = ApiInput.ManagementRequested(Request);
        var viewer = await AuthenticateAsync(db, !management, cancellationToken);
        if (management && (viewer is null || String(viewer.Account, "role") != "admin")) throw new ApiFailure("E0003");
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        var (page, perPage, scope) = ApiInput.ListQuery(Request, true);
        if (scope == "management" && (viewer is null || String(viewer.Account, "role") != "admin")) throw new ApiFailure("E0003");

        // 管理員未明確指定 scope 也只取得公開內容；SQL WHERE 與 count 使用相同範圍。
        var where = scope == "management" ? "" : " WHERE articles.visibility = 'visible'";
        var from = " FROM articles JOIN accounts ON accounts.id = articles.author_id" + where;
        var total = Convert.ToInt64(await db.ScalarAsync("SELECT count(*)" + from, cancellationToken: cancellationToken));
        var list = new List<object>();
        if ((page - 1) <= (long.MaxValue / perPage) && (page - 1) * perPage < total)
        {
            var rows = await db.RowsAsync("SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname" + from + " ORDER BY articles.created_at DESC, articles.id DESC LIMIT @limit OFFSET @offset",
                new() { ["limit"] = perPage, ["offset"] = (page - 1) * perPage }, cancellationToken);
            foreach (var row in rows) list.Add(ArticleResource(row, viewer));
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { list, pagination = Pagination(page, perPage, total) });
    }

    /// <summary>隱藏文章只有作者或管理員可預覽；其他人與不存在均回相同 404。</summary>
    [HttpGet("/api/articles/{article_id}")]
    public async Task<IActionResult> Detail(string article_id, CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, true, cancellationToken);
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(article_id, "article_id");
        var article = await db.RowAsync("SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname FROM articles JOIN accounts ON accounts.id = articles.author_id WHERE articles.id = @id",
            new() { ["id"] = id }, cancellationToken);
        if (article is null || !Readable(article, viewer)) throw new ApiFailure("E0004");
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(ArticleResource(article, viewer));
    }

    /// <summary>先驗父文章可讀，作者預覽仍僅列公開留言；管理員可查看個別下架留言。</summary>
    [HttpGet("/api/articles/{article_id}/comments")]
    public async Task<IActionResult> Comments(string article_id, CancellationToken cancellationToken)
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, true, cancellationToken);
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        var (page, perPage, _) = ApiInput.ListQuery(Request, false);
        var id = ApiInput.Id(article_id, "article_id");
        var parent = await db.RowAsync("SELECT id, author_id, visibility FROM articles WHERE id = @id", new() { ["id"] = id }, cancellationToken);
        if (!Readable(parent, viewer)) throw new ApiFailure("E0004");

        // 留言可見性在資料庫篩選，count 與完整 ID 頁面共用同一條件。
        var where = " WHERE comments.article_id = @id" + (viewer is not null && String(viewer.Account, "role") == "admin" ? "" : " AND comments.visibility = 'visible'");
        var from = " FROM comments JOIN accounts ON accounts.id = comments.author_id" + where;
        var total = Convert.ToInt64(await db.ScalarAsync("SELECT count(*)" + from, new() { ["id"] = id }, cancellationToken));
        var list = new List<object>();
        if ((page - 1) <= long.MaxValue / perPage && (page - 1) * perPage < total)
        {
            var rows = await db.RowsAsync("SELECT comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, comments.restore_control, comments.version, comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname" + from + " ORDER BY comments.created_at DESC, comments.id DESC LIMIT @limit OFFSET @offset",
                new() { ["id"] = id, ["limit"] = perPage, ["offset"] = (page - 1) * perPage }, cancellationToken);
            foreach (var row in rows) list.Add(CommentResource(row, viewer));
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { list, pagination = Pagination(page, perPage, total) });
    }

    /// <summary>以相同規則判定文章是否存在且可讀，不向無權者透露下架資料。</summary>
    private static bool Readable(Dictionary<string, object?>? article, Identity? viewer) => article is not null &&
        (String(article, "visibility") == "visible" || viewer is not null &&
            (String(viewer.Account, "role") == "admin" || Long(viewer.Account, "id") == Long(article, "author_id")));
}
