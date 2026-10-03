using AspNetCore;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddControllers();
// 啟動時載入既存 ASP.NET 專屬金鑰，缺漏時立即失敗，避免運行中默默換金鑰。
builder.Services.AddSingleton(new JwtToken("/run/secrets/aspnet-core-jwt.key"));
// 設定可讀取 API 回應的瀏覽器來源；CORS 不是存取控制，端點仍須驗證憑證與權限。
builder.Services.AddCors(options =>
{
    // 允許展示入口的預檢，並讓登入頁讀取鎖定倒數標頭。
    options.AddPolicy("frontend", policy => policy
        .WithOrigins("http://localhost:8080")
        .WithMethods("GET", "POST", "PUT")
        .WithHeaders("Authorization", "Content-Type", "Accept")
        .WithExposedHeaders("Retry-After"));
});
var app = builder.Build();

// 每次請求建立可追查識別，集中捕捉安全錯誤與不可預期例外。
app.Use(async (context, next) =>
{
    // 每次請求產生獨立識別，只記錄路徑與結果，不把憑證或 Body 寫入日誌。
    context.Items["log_id"] = Guid.NewGuid().ToString();
    var logId = context.Items["log_id"];
    app.Logger.LogInformation("API IN {LogId} {Method} {Path}", logId, context.Request.Method, context.Request.Path);
    try
    {
        await next(context);
    }
    catch (ApiFailure failure)
    {
        await ((Microsoft.AspNetCore.Mvc.ObjectResult)ApiEnvelope.Failure(context, failure)).ExecuteResultAsync(new Microsoft.AspNetCore.Mvc.ActionContext { HttpContext = context });
    }
    catch (Exception exception)
    {
        // 未預期例外只對外給共同錯誤，內部日誌保留例外類型便於追查。
        app.Logger.LogError("API EX {LogId} {Exception}", logId, exception.GetType().FullName);
        await ((Microsoft.AspNetCore.Mvc.ObjectResult)ApiEnvelope.Failure(context, new ApiFailure("E9999"))).ExecuteResultAsync(new Microsoft.AspNetCore.Mvc.ActionContext { HttpContext = context });
    }
    // 路由結果的 404／405 仍需共用 envelope；已開始的業務回應不可覆寫。
    if ((context.Response.StatusCode is 404 or 405) && !context.Response.HasStarted)
    {
        var status = context.Response.StatusCode;
        var routeCode = status == 404 ? "E0004" : "E0005";
        await ((Microsoft.AspNetCore.Mvc.ObjectResult)ApiEnvelope.Failure(context, new ApiFailure(routeCode, httpStatus: status))).ExecuteResultAsync(new Microsoft.AspNetCore.Mvc.ActionContext { HttpContext = context });
    }
    // 統一記錄錯誤代碼與 HTTP 狀態，避免各控制器漏記或印出敏感資料。
    if (context.Items.TryGetValue("error_code", out var code)) app.Logger.LogWarning("API EX {LogId} {Code}", logId, code);
    app.Logger.LogInformation("API OUT {LogId} {Status}", logId, context.Response.StatusCode);
});
app.UseCors("frontend");
app.MapControllers();

// 健康檢查實際讀取共用資料表，資料庫失聯時不得讓 Compose 宣稱服務就緒。
app.MapGet("/up", async (CancellationToken cancellationToken) =>
{
    try
    {
        await using var db = await Database.OpenAsync(cancellationToken);
        await db.RowsAsync("SELECT 1 FROM accounts, login_sessions, login_failures, login_lockout_settings, articles, comments LIMIT 0", cancellationToken: cancellationToken);
        return Results.Json(new { status = "ok" });
    }
    catch (Exception)
    {
        return Results.Json(new { status = "unavailable" }, statusCode: 503);
    }
});

app.Run();
