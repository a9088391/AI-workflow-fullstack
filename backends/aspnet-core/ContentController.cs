using Microsoft.AspNetCore.Mvc;

namespace AspNetCore;

/// <summary>作者內容操作共用交易內身分、父文章及內容列鎖，維持跨後端一致的可見性與版本規則。</summary>
/// <param name="jwt">共用固定簽章服務，寫入仍須在交易內重驗帳號與登入紀錄。</param>
[ApiController]
public sealed class ContentController(JwtToken jwt) : ApiControllerBase(jwt)
{
    /// <summary>以交易內登入身分建立公開文章，來源與初始版本由後端決定。</summary>
    [HttpPost("/api/articles")]
    public async Task<IActionResult> CreateArticle(CancellationToken cancellationToken)
    {
        // 先在交易內固定身分，再驗寫入欄位；用戶端不能指定作者、版本或建立來源。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        ApiInput.NoQuery(Request);
        var (text, _) = await ApiInput.ContentBodyAsync(Request, ["title", "body"], cancellationToken);
        // 建立時間只取一次資料庫實際時鐘，使 created_at 與 updated_at 完全相同。
        var now = await db.NowAsync(cancellationToken);
        await db.ExecuteAsync("INSERT INTO articles (author_id, title, body, visibility, restore_control, version, created_backend, created_at, updated_at) VALUES (@author, @title, @body, 'visible', NULL, 1, 'aspnet-core', @now, @now)",
            new() { ["author"] = Long(viewer.Account, "id"), ["title"] = text["title"], ["body"] = text["body"], ["now"] = now }, cancellationToken);
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { }, 201);
    }

    /// <summary>列出自己的公開與下架文章，讓新建文章可從確實回傳的列表取得 ID。</summary>
    [HttpGet("/api/me/articles")]
    public Task<IActionResult> MyArticles(CancellationToken cancellationToken) => MyContentAsync(false, cancellationToken);

    /// <summary>列出自己的留言，即使父文章下架也只回留言及 article_id。</summary>
    [HttpGet("/api/me/comments")]
    public Task<IActionResult> MyComments(CancellationToken cancellationToken) => MyContentAsync(true, cancellationToken);

    /// <summary>只讓作者修改文章內文；隱藏文章先依可讀性裁決，再檢查操作權與版本。</summary>
    [HttpPut("/api/articles/{article_id}")]
    public Task<IActionResult> UpdateArticle(string? article_id, CancellationToken cancellationToken) => MutateArticleAsync(article_id, false, cancellationToken);

    /// <summary>作者或管理員按恢復控制權變更狀態；父文章列鎖同時阻止留言競爭寫入。</summary>
    [HttpPut("/api/articles/{article_id}/visibility")]
    public Task<IActionResult> ArticleVisibility(string? article_id, CancellationToken cancellationToken) => MutateArticleAsync(article_id, true, cancellationToken);

    /// <summary>鎖住父文章直到留言提交，防止與父文章下架交錯產生違規留言。</summary>
    [HttpPost("/api/articles/{article_id}/comments")]
    public async Task<IActionResult> CreateComment(string? article_id, CancellationToken cancellationToken)
    {
        // 認證先於路徑及 Body，格式合法後才鎖父文章，不向無權者透露隱藏目標。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(article_id, "article_id");
        var (text, _) = await ApiInput.ContentBodyAsync(Request, ["body"], cancellationToken);
        // 先鎖父列並確認當下可見性；父下架先提交時不能再新增留言。
        var article = await db.RowAsync("SELECT id, author_id, visibility FROM articles WHERE id = @id FOR UPDATE", new() { ["id"] = id }, cancellationToken);
        if (article is null || !ReadableArticle(article, viewer)) throw new ApiFailure("E0004");
        if (String(article, "visibility") != "visible") throw new ApiFailure("E0003");
        var now = await db.NowAsync(cancellationToken);
        await db.ExecuteAsync("INSERT INTO comments (author_id, article_id, body, visibility, restore_control, version, created_backend, created_at, updated_at) VALUES (@author, @article, @body, 'visible', NULL, 1, 'aspnet-core', @now, @now)",
            new() { ["author"] = Long(viewer.Account, "id"), ["article"] = id, ["body"] = text["body"], ["now"] = now }, cancellationToken);
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { }, 201);
    }

    /// <summary>作者與管理員可獨立讀取留言；其他會員先按父子可見性回 404 或 403。</summary>
    [HttpGet("/api/comments/{comment_id}")]
    public async Task<IActionResult> CommentDetail(string? comment_id, CancellationToken cancellationToken)
    {
        // 本端點須登入；空白路徑也先驗權，再交共同 ID 驗證，避免 MVC 自動回原生 400。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(comment_id, "comment_id");
        // 單則詳情不授予其他會員私有內容；公開父子內容仍先判定可讀再回 403。
        var comment = await db.RowAsync(CommentSelect + " WHERE comments.id = @id", new() { ["id"] = id }, cancellationToken);
        var article = comment is null ? null : await db.RowAsync("SELECT id, visibility FROM articles WHERE id = @id", new() { ["id"] = Long(comment, "article_id") }, cancellationToken);
        comment = CheckCommentReadable(comment, article, viewer);
        if (Long(comment, "author_id") != Long(viewer.Account, "id") && String(viewer.Account, "role") != "admin") throw new ApiFailure("E0003");
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(CommentResource(comment, viewer));
    }

    /// <summary>修改自己的留言時鎖父文章與留言，父文章下架後即拒絕內文寫入。</summary>
    [HttpPut("/api/comments/{comment_id}")]
    public Task<IActionResult> UpdateComment(string? comment_id, CancellationToken cancellationToken) => MutateCommentAsync(comment_id, false, cancellationToken);

    /// <summary>下架或恢復留言時仍鎖父文章；父下架只影響對外可見性，不阻止合法狀態操作。</summary>
    [HttpPut("/api/comments/{comment_id}/visibility")]
    public Task<IActionResult> CommentVisibility(string? comment_id, CancellationToken cancellationToken) => MutateCommentAsync(comment_id, true, cancellationToken);

    private const string ArticleSelect = "SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname FROM articles JOIN accounts ON accounts.id = articles.author_id";
    private const string CommentSelect = "SELECT comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, comments.restore_control, comments.version, comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname FROM comments JOIN accounts ON accounts.id = comments.author_id";

    /// <summary>個人列表沿用相同分頁及資源格式，不以父文章可讀性過濾自己的留言。</summary>
    private async Task<IActionResult> MyContentAsync(bool comments, CancellationToken cancellationToken)
    {
        // 以目前交易內身分固定作者條件；內部布林只選兩個固定表名，不採用用戶端 SQL 片段。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        await ApiInput.NoGetBodyAsync(Request, cancellationToken);
        var (page, perPage, _) = ApiInput.ListQuery(Request, false);
        var table = comments ? "comments" : "articles";
        // count 與列表以同一作者條件查詢，自己的留言不因父文章下架而消失。
        var total = Convert.ToInt64(await db.ScalarAsync($"SELECT count(*) FROM {table} WHERE author_id = @author", new() { ["author"] = Long(viewer.Account, "id") }, cancellationToken));
        var list = new List<object>();
        if ((page - 1) <= long.MaxValue / perPage && (page - 1) * perPage < total)
        {
            var rows = await db.RowsAsync((comments ? CommentSelect : ArticleSelect) + $" WHERE {table}.author_id = @author ORDER BY {table}.created_at DESC, {table}.id DESC LIMIT @limit OFFSET @offset",
                new() { ["author"] = Long(viewer.Account, "id"), ["limit"] = perPage, ["offset"] = (page - 1) * perPage }, cancellationToken);
            foreach (var row in rows) list.Add(comments ? CommentResource(row, viewer) : ArticleResource(row, viewer));
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { list, pagination = Pagination(page, perPage, total) });
    }

    /// <summary>文章更新依可讀→權限→版本順序，在持有列鎖時完成條件更新與提交。</summary>
    private async Task<IActionResult> MutateArticleAsync(string? rawId, bool visibility, CancellationToken cancellationToken)
    {
        // 認證與全部格式先完成，才讀寫目標；nullable 僅保留共同拒絕空白路徑的順序。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(rawId, "article_id");
        var (text, version) = await ApiInput.ContentBodyAsync(Request, visibility ? ["visibility", "version"] : ["title", "body", "version"], cancellationToken);
        // 認證列鎖已持有，再鎖文章以固定停用／撤銷與內容寫入的先後。
        var article = await db.RowAsync("SELECT id, author_id, title, body, visibility, restore_control, version FROM articles WHERE id = @id FOR UPDATE", new() { ["id"] = id }, cancellationToken);
        if (article is null || !ReadableArticle(article, viewer)) throw new ApiFailure("E0004");
        var own = Long(article, "author_id") == Long(viewer.Account, "id");
        if (!own && (!visibility || String(viewer.Account, "role") != "admin")) throw new ApiFailure("E0003");
        if (visibility) await SaveVisibilityAsync(db, "articles", article, viewer, text["visibility"], version, cancellationToken);
        else
        {
            // 權限通過後才比較版本；正確版本的同值更新保留版本與 updated_at。
            CheckVersion(article, version);
            if (String(article, "title") != text["title"] || String(article, "body") != text["body"])
                await ExecuteVersionedAsync(db, "UPDATE articles SET title = @title, body = @body, version = version + 1, updated_at = @now WHERE id = @id AND version = @version",
                    new() { ["title"] = text["title"], ["body"] = text["body"], ["id"] = id, ["version"] = version, ["now"] = await db.NowAsync(cancellationToken) }, cancellationToken);
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { });
    }

    /// <summary>探索父 ID 後才依文章→留言加鎖，重讀留言避免在錯誤鎖序下修改舊資料。</summary>
    private async Task<IActionResult> MutateCommentAsync(string? rawId, bool visibility, CancellationToken cancellationToken)
    {
        // 先驗身分與完整輸入，避免在目標可讀性之前透露內容或版本。
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.BeginAsync(cancellationToken);
        var viewer = await AuthenticateAsync(db, false, cancellationToken) ?? throw new ApiFailure("E0002");
        ApiInput.NoQuery(Request);
        var id = ApiInput.Id(rawId, "comment_id");
        var (text, version) = await ApiInput.ContentBodyAsync(Request, visibility ? ["visibility", "version"] : ["body", "version"], cancellationToken);
        // 探索父 ID 不先取得留言列鎖；父文章與留言依共同順序鎖定後重查關聯。
        var parentId = await db.ScalarAsync("SELECT article_id FROM comments WHERE id = @id", new() { ["id"] = id }, cancellationToken);
        var article = parentId is null ? null : await db.RowAsync("SELECT id, visibility FROM articles WHERE id = @id FOR UPDATE", new() { ["id"] = parentId }, cancellationToken);
        var comment = article is null ? null : await db.RowAsync("SELECT id, author_id, article_id, body, visibility, restore_control, version FROM comments WHERE id = @id FOR UPDATE", new() { ["id"] = id }, cancellationToken);
        if (article is null || comment is null || Long(comment, "article_id") != Long(article, "id")) throw new ApiFailure("E0004");
        comment = CheckCommentReadable(comment, article, viewer);
        var own = Long(comment, "author_id") == Long(viewer.Account, "id");
        // 內文只允許作者且父文章須公開；狀態操作則保留作者與管理員各自的恢復權規則。
        if (!own && (!visibility || String(viewer.Account, "role") != "admin")
            || !visibility && String(article, "visibility") != "visible") throw new ApiFailure("E0003");
        if (visibility) await SaveVisibilityAsync(db, "comments", comment, viewer, text["visibility"], version, cancellationToken);
        else
        {
            // 先拒絕過期版本，再判同值；正確且未變更的內文不觸發寫入或更新時間。
            CheckVersion(comment, version);
            if (String(comment, "body") != text["body"])
                await ExecuteVersionedAsync(db, "UPDATE comments SET body = @body, version = version + 1, updated_at = @now WHERE id = @id AND version = @version",
                    new() { ["body"] = text["body"], ["id"] = id, ["version"] = version, ["now"] = await db.NowAsync(cancellationToken) }, cancellationToken);
        }
        await db.CommitAsync(cancellationToken);
        return ApiEnvelope.Success(new { });
    }

    /// <summary>先核實恢復權，再比較版本；同值保留時間，管理員接管他人下架則必須更新版本。</summary>
    private static async Task SaveVisibilityAsync(Database db, string table, Dictionary<string, object?> content, Identity viewer, string requested, long version, CancellationToken cancellationToken)
    {
        // 作者不能用新版本繞過管理員恢復限制；版本檢查必須在恢復資格通過後。
        var own = Long(content, "author_id") == Long(viewer.Account, "id");
        var admin = String(viewer.Account, "role") == "admin";
        if (requested == "visible" && String(content, "visibility") == "hidden" && !admin && String(content, "restore_control") != "author") throw new ApiFailure("E0003");
        CheckVersion(content, version);
        // 管理員對他人的再次下架會接管恢復權；作者再次下架保留既有管理限制。
        var control = requested == "visible" ? null : !own && admin ? "admin" : String(content, "restore_control") ?? "author";
        if (String(content, "visibility") == requested && String(content, "restore_control") == control) return;
        await ExecuteVersionedAsync(db, $"UPDATE {table} SET visibility = @visibility, restore_control = @control, version = version + 1, updated_at = @now WHERE id = @id AND version = @version",
            new() { ["visibility"] = requested, ["control"] = control, ["id"] = Long(content, "id"), ["version"] = version, ["now"] = await db.NowAsync(cancellationToken) }, cancellationToken);
    }

    /// <summary>即使已有列鎖，SQL 仍以舊版本作條件，避免未來查詢改動造成過期覆寫。</summary>
    private static async Task ExecuteVersionedAsync(Database db, string sql, Dictionary<string, object?> parameters, CancellationToken cancellationToken)
    {
        if (await db.ExecuteAsync(sql, parameters, cancellationToken) != 1) throw new ApiFailure("E0201");
    }

    /// <summary>可讀性在操作權與版本前裁決，對非作者隱藏下架文章的存在。</summary>
    private static bool ReadableArticle(Dictionary<string, object?>? article, Identity viewer) => article is not null &&
        (String(article, "visibility") == "visible" || String(viewer.Account, "role") == "admin" || Long(article, "author_id") == Long(viewer.Account, "id"));

    /// <summary>留言作者即使父文章下架仍可讀自己的留言；其他會員只可判知公開父子內容。</summary>
    private static Dictionary<string, object?> CheckCommentReadable(Dictionary<string, object?>? comment, Dictionary<string, object?>? article, Identity viewer)
    {
        if (comment is null || article is null || String(viewer.Account, "role") != "admin" && Long(comment, "author_id") != Long(viewer.Account, "id")
            && (String(comment, "visibility") != "visible" || String(article, "visibility") != "visible")) throw new ApiFailure("E0004");
        return comment;
    }

    /// <summary>先拒絕過期版本，即使送來的內容與目前值完全相同亦不得當作成功。</summary>
    private static void CheckVersion(Dictionary<string, object?> content, long version)
    {
        if (Long(content, "version") != version) throw new ApiFailure("E0201");
    }
}
