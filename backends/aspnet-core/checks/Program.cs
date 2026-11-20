using AspNetCore;

// 以 MVC 使用的 Web JSON 選項核對具名 DTO，保護 snake_case、null、整數精度及成功 envelope。
var responseAccount = new AccountResponse(9007199254740991, null, "作者😀", "member", "active", "2026-11-20 08:00:00");
CheckResponse(new LoginResponse("fixture", "Bearer", "2026-11-20 16:00:00", responseAccount),
    """{"access_token":"fixture","token_type":"Bearer","expires_at":"2026-11-20 16:00:00","user":{"id":9007199254740991,"email":null,"nickname":"作者😀","role":"member","status":"active","created_at":"2026-11-20 08:00:00"}}""");
CheckResponse(new ArticleResponse(1, "標題", "原文\r\n😀", new AuthorResponse(42, null), 2, "aspnet-core", "2026-11-20 08:00:00", "2026-11-20 09:00:00", "hidden", true),
    """{"id":1,"title":"標題","body":"原文\r\n😀","author":{"id":42,"nickname":null},"version":2,"created_backend":"aspnet-core","created_at":"2026-11-20 08:00:00","updated_at":"2026-11-20 09:00:00","visibility":"hidden","can_restore":true}""");
CheckResponse(new CommentResponse(3, "留言", new AuthorResponse(42, "作者"), 1, 4, "aspnet-core", "2026-11-20 08:00:00", "2026-11-20 09:00:00", "visible", false),
    """{"id":3,"body":"留言","author":{"id":42,"nickname":"作者"},"article_id":1,"version":4,"created_backend":"aspnet-core","created_at":"2026-11-20 08:00:00","updated_at":"2026-11-20 09:00:00","visibility":"visible","can_restore":false}""");
CheckResponse(new PaginationResponse(9007199254740991, 20, 0, 0),
    """{"page":9007199254740991,"per_page":20,"total":0,"total_pages":0}""");
CheckResponse(new LoginLockoutResponse(false, 5, 5, 300),
    """{"enabled":false,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}""");

// PHP 8.4 password_hash 產生的 PHC fixture，檢查此套件的跨後端驗密碼互通。
const string phpHash = "$argon2id$v=19$m=19456,t=2,p=1$OFJRRXJZdS5EVjVUbkFlRA$D7A0COna0A32je4cpl9dvej3v3f/iVLzQ1LQ9XO/04c";
Check(await PasswordHash.VerifyAsync("Abc1234", phpHash), "Laravel/PHP PHC should verify");
Check(!await PasswordHash.VerifyAsync("Wrong123", phpHash), "wrong password should fail");
// 損壞的 PHC 參數要走設定錯誤，而非被當成一般密碼錯誤。
await ExpectFailureAsync(async () => { await PasswordHash.VerifyAsync("Abc1234", phpHash.Replace("m=19456", "m=1024")); }, "E0104");
Check(Rules.ValidPassword("1234") && Rules.ValidPassword("Abcdefghijklmnop") && !Rules.ValidPassword("123") && !Rules.ValidPassword("Abc 1234"), "password boundaries");
Check(Rules.ValidEmail(Rules.NormalizeEmail(" Demo+Tag@Example.Test ")) && !Rules.ValidEmail("a..b@example.test"), "email normalization and syntax");
// Unicode Kelvin sign 不可摺成 ASCII k；註冊與登入共用的格式檢查都必須拒絕它。
Check(Rules.NormalizeEmail(" A+Tag@EXAMPLE.TEST ") == "a+tag@example.test"
    && Rules.NormalizeEmail("\u212Aprobe13@example.test") == "\u212Aprobe13@example.test"
    && !Rules.ValidEmail(Rules.NormalizeEmail("\u212Aprobe13@example.test")), "ASCII-only email case conversion");
Check(Rules.ValidNickname("作者\n") && !Rules.ValidNickname("\u3000\t") && !Rules.ValidNickname("名稱\0"), "nickname codepoints");
Check(Rules.TryId("00042", out var id) && id == 42 && !Rules.TryId("+42", out _) && !Rules.TryId("9007199254740992", out _), "URL integer precision");
// 內容保留混合換行及補充平面字元；全空白、NUL、過長與非整數版本必須拒絕。
Check(Rules.ValidContent("😀\r\n文", 4) && !Rules.ValidContent("😀\r\n文", 3)
    && !Rules.ValidContent("\u3000\t", 200) && !Rules.ValidContent("a\0", 200), "content codepoints and whitespace");
Check(Rules.TryJsonVersion("9007199254740991", out var maxVersion) && maxVersion == 9007199254740991
    && !Rules.TryJsonVersion("0", out _) && !Rules.TryJsonVersion("1.0", out _)
    && !Rules.TryJsonVersion("1e0", out _) && !Rules.TryJsonVersion("9007199254740992", out _), "JSON version integer precision");
// 以 DefaultHttpContext 的 Request.Body 作輔助檢查；真正 HTTP／認證順序仍由隔離公開案例驗收。
var content = await ParseContentAsync("{\"title\":\"😀\\r\\n文\",\"body\":\"原文\",\"version\":1}", ["title", "body", "version"]);
Check(content.Text["title"] == "😀\r\n文" && content.Version == 1, "content JSON preservation");
await ExpectContentFailureAsync("{\"body\":\"留言\",\"version\":\"1\"}", ["body", "version"], "version", "欄位型態不正確");
await ExpectContentFailureAsync("{\"body\":\"留言\",\"version\":1.0}", ["body", "version"], "version", "欄位型態不正確");
// 超過 runtime 整數容量仍是 JSON 整數，必須依共同契約歸為範圍錯誤。
await ExpectContentFailureAsync("{\"body\":\"留言\",\"version\":9223372036854775808}", ["body", "version"], "version", "欄位格式或範圍不正確");
foreach (var raw in new[] { "-9223372036854775809", "9007199254740992", "-0" })
{
    await ExpectContentFailureAsync("{\"body\":\"留言\",\"version\":" + raw + "}", ["body", "version"], "version", "欄位格式或範圍不正確");
}
await ExpectContentFailureAsync("{\"body\":null,\"version\":1}", ["body", "version"], "body", "此欄位不可為 null");
// JSON object 的重複名稱由最後一筆決定；最後一筆仍須完整通過文字或版本驗證。
var lastBody = await ParseContentAsync("{\"body\":null,\"body\":\"最後\"}", ["body"]);
Check(lastBody.Text["body"] == "最後", "duplicate content uses final value");
await ExpectContentFailureAsync("{\"body\":\"先前\",\"body\":null}", ["body"], "body", "此欄位不可為 null");
var lastVersion = await ParseContentAsync("{\"body\":\"留言\",\"version\":0,\"version\":2}", ["body", "version"]);
Check(lastVersion.Version == 2, "duplicate version uses final value");
await ExpectContentFailureAsync("{\"body\":\"留言\",\"version\":2,\"version\":\"2\"}", ["body", "version"], "version", "欄位型態不正確");
await ExpectContentFailureAsync("{\"body\":\"a\",\"unexpected\":1}", ["body"], "unexpected", "不支援此欄位");
await ExpectContentFailureAsync("{\"body\":\"a\",\"unexpected\":1,\"unexpected\":2}", ["body"], "unexpected", "不支援此欄位");
await ExpectContentFailureAsync("{}", ["body"], "body", "此欄位為必填");
// 管理端點必須保留字面搜尋與嚴格 Body 型態；重複 JSON 鍵依最後一筆裁決。
var memberQuery = MemberQuery("?page=2&per_page=1&filters%5Bkeyword%5D=%20A%25_%5C%E9%99%B3%20");
Check(memberQuery.Page == 2 && memberQuery.PerPage == 1 && memberQuery.Filter == "A%_\\陳", "member keyword literal preservation");
await ExpectQueryFailureAsync("?page=1&per_page=20&filters%5Bkeyword%5D=a&filters%5Bkeyword%5D=b", "filters.keyword", "此參數不可重複");
await ExpectQueryFailureAsync("?page=1&per_page=20&filters%5Bkeyword%5D%5B0%5D=a", "filters.keyword", "欄位型態不正確");
// 僅保留 ASP.NET 搜尋 NUL 的歷史行為檢查；共同政策尚未定案，不能據此宣稱跨後端一致。
await ExpectQueryFailureAsync("?page=1&per_page=20&filters%5Bkeyword%5D=%00", "filters.keyword", "欄位格式或範圍不正確");
// 無效 UTF-8 位元組不可搜尋；合法字面百分號序列及 U+FFFD 仍保留原文。
await ExpectQueryFailureAsync("?page=1&per_page=20&filters%5Bkeyword%5D=%FF", "filters.keyword", "欄位格式或範圍不正確");
Check(MemberQuery("?page=1&per_page=20&filters%5Bkeyword%5D=%25FF").Filter == "%FF", "literal percent keyword remains valid");
Check(MemberQuery("?page=1&per_page=20&filters%5Bkeyword%5D=%EF%BF%BD").Filter == "\uFFFD", "valid replacement character keyword remains valid");
await ExpectQueryFailureAsync("?page=1&per_page=20&%FF=x", "%FF", "不支援此欄位");
// 無效 bytes 與字面百分號名稱只有顯示標籤相同，不可因此當成重複參數。
await ExpectQueryFailureAsync("?page=1&per_page=20&%FF=x&%25FF=y", "%FF", "不支援此欄位");
// 兩個公開解析入口共用 bytes 身分規則，真重複的優先性也不能受輸入順序影響。
foreach (var (query, errors) in new (string, Dictionary<string, string[]>)[]
{
    ("%25FF=x&%FF=y", new() { ["%FF"] = ["不支援此欄位"] }),
    ("%FF=x&%ff=y", new() { ["%FF"] = ["此參數不可重複"] }),
    ("%FF=x&%FF=y&%25FF=z", new() { ["%FF"] = ["此參數不可重複"] }),
    ("%25FF=x&%FF=y&%25FF=z", new() { ["%FF"] = ["此參數不可重複"] }),
    ("%ff=x&%25ff=y", new() { ["%FF"] = ["不支援此欄位"], ["%ff"] = ["不支援此欄位"] }),
    ("page%5B%FF%5D=x", new() { ["page%5B%FF%5D"] = ["不支援此欄位"] }),
    ("unexpected=x&%75nexpected=y", new() { ["unexpected"] = ["此參數不可重複"] })
})
{
    ExpectQueryErrors("?" + query, errors, false);
    ExpectQueryErrors("?page=1&per_page=20&" + query, errors, true);
}
await ExpectQueryFailureAsync("?page=1&per_page=20&filters%5Bkeyword%5D=" + new string('x', 255), "filters.keyword", "欄位格式或範圍不正確");
var status = await ParseContentAsync("{\"status\":\"active\",\"status\":\"disabled\"}", ["status"]);
Check(status.Text["status"] == "disabled", "member status final duplicate key");
await ExpectContentFailureAsync("{\"status\":\"blocked\"}", ["status"], "status", "欄位格式或範圍不正確");
var settings = await ParseSettingsAsync("{\"enabled\":true,\"enabled\":false,\"window_minutes\":5,\"failure_threshold\":5,\"lock_seconds\":300}");
Check(!settings.Enabled && settings.WindowMinutes == 5 && settings.FailureThreshold == 5 && settings.LockSeconds == 300, "settings final duplicate key and exact fields");
await ExpectSettingsFailureAsync("{\"enabled\":true,\"window_minutes\":1.0,\"failure_threshold\":5,\"lock_seconds\":300}", "window_minutes", "欄位型態不正確");
// 整數超過 Int32 或 Int64 都是範圍錯誤，公開型態不依各 runtime 的數字容量改變。
await ExpectSettingsFailureAsync("{\"enabled\":true,\"window_minutes\":2147483648,\"failure_threshold\":5,\"lock_seconds\":300}", "window_minutes", "欄位格式或範圍不正確");
await ExpectSettingsFailureAsync("{\"enabled\":true,\"window_minutes\":9223372036854775808,\"failure_threshold\":5,\"lock_seconds\":300}", "window_minutes", "欄位格式或範圍不正確");
await ExpectSettingsFailureAsync("{\"enabled\":true,\"window_minutes\":0,\"failure_threshold\":5,\"lock_seconds\":300}", "window_minutes", "欄位格式或範圍不正確");
await ExpectSettingsFailureAsync("{\"enabled\":true,\"window_minutes\":5,\"failure_threshold\":5,\"lock_seconds\":300,\"extra\":1}", "extra", "不支援此欄位");
await ExpectSettingsFailureAsync("{\"enabled\":null,\"window_minutes\":5,\"failure_threshold\":5,\"lock_seconds\":300}", "enabled", "此欄位不可為 null");
// 三個設定數字都保留 lexical type／range 差異，不只檢查代表欄位。
foreach (var field in new[] { "window_minutes", "failure_threshold", "lock_seconds" })
{
    foreach (var (raw, message) in new[]
    {
        ("9223372036854775808", "欄位格式或範圍不正確"),
        ("-9223372036854775809", "欄位格式或範圍不正確"),
        ("1e0", "欄位型態不正確")
    })
    {
        var json = "{\"enabled\":true,\"window_minutes\":" + (field == "window_minutes" ? raw : "5")
            + ",\"failure_threshold\":" + (field == "failure_threshold" ? raw : "5")
            + ",\"lock_seconds\":" + (field == "lock_seconds" ? raw : "300") + "}";
        await ExpectSettingsFailureAsync(json, field, message);
    }
}
// 巨大整數不能搶掉其他欄位的 null 錯誤；比對完整明細，避免只回其中一欄也通過。
await ExpectFailureAsync(async () => { await ParseContentAsync("{\"body\":null,\"version\":9223372036854775808}", ["body", "version"]); },
    "E0001", new() { ["body"] = ["此欄位不可為 null"], ["version"] = ["欄位格式或範圍不正確"] });
// 設定同樣逐欄聚合，不能因 enabled 為 null 就停止檢查其餘三個數字。
await ExpectFailureAsync(async () => { await ParseSettingsAsync("{\"enabled\":null,\"window_minutes\":9223372036854775808,\"failure_threshold\":null,\"lock_seconds\":0}"); },
    "E0001", new()
    {
        ["enabled"] = ["此欄位不可為 null"],
        ["window_minutes"] = ["欄位格式或範圍不正確"],
        ["failure_threshold"] = ["此欄位不可為 null"],
        ["lock_seconds"] = ["欄位格式或範圍不正確"]
    });

// 完整解碼 JSON 後才驗業務欄位，孤立 surrogate 不可被誤報為系統或欄位錯誤。
await ExpectFailureAsync(async () => { await ParseContentAsync("{\"body\":\"\\ud800\"}", ["body"]); }, "E0005");
foreach (var json in new[]
{
    "{\"\\udfff\":1}", "{\"body\":\"ok\",\"extra\":{\"value\":\"\\ud800\"}}",
    "{\"body\":\"ok\",\"extra\":[\"\\udfff\"]}", "{\"body\":\"\\ud800\",\"body\":\"ok\"}",
    "\"\\ud800\"", "[\"\\udfff\"]"
})
{
    // 帳密 parser 也須先解碼未知、巢狀及重複鍵的每個值，才判欄位錯誤。
    await ExpectFailureAsync(async () => { await ParseAccountAsync(json); }, "E0005");
    // 內容 parser 不可因採最後同名鍵就忽略較早的解碼錯誤。
    await ExpectFailureAsync(async () => { await ParseContentAsync(json, ["body"]); }, "E0005");
    // 設定 parser 的獨立入口須有相同的完整 JSON 解碼結果。
    await ExpectFailureAsync(async () => { await ParseSettingsAsync(json); }, "E0005");
}
var paired = await ParseContentAsync("{\"body\":\"\\ud83d\\ude00\"}", ["body"]);
Check(paired.Text["body"] == "😀", "valid surrogate pair is preserved");

// 驗證有效 JWT 的固定八小時、跨後端金鑰隔離與到期邊界。
var keyFile = Path.GetTempFileName();
try
{
    var firstKey = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    await File.WriteAllTextAsync(keyFile, Convert.ToBase64String(firstKey) + "\n");
    var jwt = new JwtToken(keyFile);
    var issued = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
    var jti = JwtToken.Encode(System.Security.Cryptography.RandomNumberGenerator.GetBytes(16));
    var token = jwt.Issue(42, jti, issued);
    var claims = jwt.Verify(token, issued);
    Check(claims.AccountId == 42 && claims.Jti == jti && claims.Expires == issued + 28800, "JWT claims");
    // 到期當秒須失效，不能偷偷延長八小時憑證。
    ExpectFailure(() => jwt.Verify(token, issued + 28800), "E0002");
    // 簽章位元組被改動時，驗證不得接受原 claims。
    ExpectFailure(() => jwt.Verify(token + "x", issued), "E0002");
    var validPayload = "{\"iss\":\"ai-workflow-fullstack:aspnet-core\",\"aud\":\"ai-workflow-fullstack-api:aspnet-core\",\"sub\":\"42\",\"jti\":\"" + jti
        + "\",\"iat\":" + issued + ",\"nbf\":" + issued + ",\"exp\":" + (issued + 28800) + "}";
    var wrongAlgorithm = SignedToken("{\"alg\":\"none\",\"typ\":\"JWT\"}", validPayload, firstKey);
    // 即使測試金鑰重新簽署，非 HS256 header 仍不得通過。
    ExpectFailure(() => jwt.Verify(wrongAlgorithm, issued), "E0002");
    var missingClaim = SignedToken("{\"alg\":\"HS256\",\"typ\":\"JWT\"}",
        "{\"iss\":\"ai-workflow-fullstack:aspnet-core\",\"aud\":\"ai-workflow-fullstack-api:aspnet-core\",\"sub\":\"42\",\"jti\":\"" + jti
        + "\",\"iat\":" + issued + ",\"nbf\":" + issued + "}", firstKey);
    // 缺少必要時間 claim 時不得補預設值接受。
    ExpectFailure(() => jwt.Verify(missingClaim, issued), "E0002");
    var malformedClaim = SignedToken("{\"alg\":\"HS256\",\"typ\":\"JWT\"}",
        "{\"iss\":\"ai-workflow-fullstack:aspnet-core\",\"aud\":\"ai-workflow-fullstack-api:aspnet-core\",\"sub\":\"\\ud800\",\"jti\":\"" + jti
        + "\",\"iat\":" + issued + ",\"nbf\":" + issued + ",\"exp\":" + (issued + 28800) + "}", firstKey);
    // 已驗簽仍須拒絕不能解碼的必要 claim，不能讓 JSON 例外變成系統錯誤。
    ExpectFailure(() => jwt.Verify(malformedClaim, issued), "E0002");
    foreach (var payload in new[]
    {
        validPayload.Replace("ai-workflow-fullstack:aspnet-core", "\\udfff"),
        validPayload.Replace(jti, "\\ud800"),
        validPayload.Replace("\"sub\":", "\"\\ud800\":")
    })
    {
        var malformed = SignedToken("{\"alg\":\"HS256\",\"typ\":\"JWT\"}", payload, firstKey);
        // 必要文字 claim 或名稱的解碼失敗都應拒絕為無效 JWT，不可向外拋原生例外。
        ExpectFailure(() => jwt.Verify(malformed, issued), "E0002");
    }
    await File.WriteAllTextAsync(keyFile, Convert.ToBase64String(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32)) + "\n");
    // 換成本後端另一把金鑰後，舊憑證應立即無效。
    ExpectFailure(() => new JwtToken(keyFile).Verify(token, issued), "E0002");
}
finally
{
    File.Delete(keyFile);
}
Console.WriteLine("ASP.NET contract checks passed");

/// <summary>比對完整成功回應的 JSON 結構，避免 records 新增、漏掉或重新命名公開欄位。</summary>
static void CheckResponse(object data, string expectedData)
{
    var result = (Microsoft.AspNetCore.Mvc.ObjectResult)ApiEnvelope.Success(data);
    var actual = System.Text.Json.JsonSerializer.SerializeToNode(result.Value, new System.Text.Json.JsonSerializerOptions(System.Text.Json.JsonSerializerDefaults.Web));
    var expected = System.Text.Json.Nodes.JsonNode.Parse("{\"success\":true,\"message\":\"OK\",\"data\":" + expectedData + "}");
    Check(result.StatusCode == 200 && System.Text.Json.Nodes.JsonNode.DeepEquals(actual, expected), "response JSON contract");
}

/// <summary>在最小 console harness 中保留失敗出口，避免邏輯退化仍回零。</summary>
static void Check(bool condition, string name)
{
    if (!condition) throw new Exception(name);
}

/// <summary>驗證安全路徑拒絕而非只確認方法回傳值。</summary>
static void ExpectFailure(Action action, string code)
{
    try
    {
        action();
    }
    catch (ApiFailure failure) when (failure.Code == code)
    {
        return;
    }
    throw new Exception("Expected " + code);
}

/// <summary>等待完整非同步結果並核對共同錯誤，必要時比對全部欄位以防驗證漏報。</summary>
static async Task ExpectFailureAsync(Func<Task> action, string code, Dictionary<string, string[]>? errors = null)
{
    try
    {
        await action();
    }
    catch (ApiFailure failure) when (failure.Code == code)
    {
        if (errors is not null) CheckErrors(failure, errors);
        return;
    }
    throw new Exception("Expected " + code);
}

/// <summary>比對完整錯誤集合與文案，欄位順序不影響結果，但多報或漏報都必須失敗。</summary>
static void CheckErrors(ApiFailure failure, Dictionary<string, string[]> expected)
{
    if (failure.Errors is not { } actual) throw new Exception("Expected validation errors");
    Check(actual.Count == expected.Count, "validation error field count");
    foreach (var (field, messages) in expected)
    {
        Check(actual.TryGetValue(field, out var found) && found.SequenceEqual(messages), "validation error " + field);
    }
}

/// <summary>以測試金鑰簽自訂 claims，驗證固定 header 與必填欄位確實由 verifier 拒絕。</summary>
static string SignedToken(string header, string payload, byte[] key)
{
    var input = JwtToken.Encode(System.Text.Encoding.UTF8.GetBytes(header)) + "." + JwtToken.Encode(System.Text.Encoding.UTF8.GetBytes(payload));
    return input + "." + JwtToken.Encode(System.Security.Cryptography.HMACSHA256.HashData(key, System.Text.Encoding.ASCII.GetBytes(input)));
}

/// <summary>以 DefaultHttpContext 測試內容解析並釋放 Body stream，不冒充真實 HTTP／DB 驗收。</summary>
static async Task<(Dictionary<string, string> Text, long Version)> ParseContentAsync(string json, string[] fields)
{
    var context = new Microsoft.AspNetCore.Http.DefaultHttpContext();
    context.Request.ContentType = "application/json";
    using var stream = new MemoryStream(System.Text.Encoding.UTF8.GetBytes(json));
    context.Request.Body = stream;
    return await ApiInput.ContentBodyAsync(context.Request, fields, CancellationToken.None);
}

/// <summary>直接檢查帳密 parser 的 JSON 解碼，不需要帳號或資料庫即可重現解析錯誤。</summary>
static async Task<Dictionary<string, string>> ParseAccountAsync(string json)
{
    var context = new Microsoft.AspNetCore.Http.DefaultHttpContext();
    context.Request.ContentType = "application/json";
    using var stream = new MemoryStream(System.Text.Encoding.UTF8.GetBytes(json));
    context.Request.Body = stream;
    return await ApiInput.BodyAsync(context.Request, ["email", "password", "nickname"], false, CancellationToken.None);
}

/// <summary>逐字比對固定驗證明細，避免錯誤欄位或文案在跨後端漂移。</summary>
static async Task ExpectContentFailureAsync(string json, string[] fields, string field, string message)
{
    try
    {
        await ParseContentAsync(json, fields);
    }
    catch (ApiFailure failure) when (failure.Code == "E0001" && failure.Errors?.TryGetValue(field, out var messages) == true && messages.SequenceEqual([message]))
    {
        return;
    }
    throw new Exception("Expected content error " + field + ": " + message);
}

/// <summary>以原始 QueryString 驗證會員搜尋解析，避免測試工具先摺疊重複參數。</summary>
static (long Page, int PerPage, string? Filter) MemberQuery(string query)
{
    var context = new Microsoft.AspNetCore.Http.DefaultHttpContext();
    context.Request.QueryString = new Microsoft.AspNetCore.Http.QueryString(query);
    return ApiInput.ListQuery(context.Request, false, true);
}

/// <summary>逐字比對會員 Query 的第一項欄位錯誤。</summary>
static Task ExpectQueryFailureAsync(string query, string field, string message)
{
    try
    {
        MemberQuery(query);
    }
    catch (ApiFailure failure) when (failure.Code == "E0001" && failure.Errors?.TryGetValue(field, out var messages) == true && messages.SequenceEqual([message]))
    {
        return Task.CompletedTask;
    }
    throw new Exception("Expected member query error " + field + ": " + message);
}

/// <summary>兩種 Query 入口都比較完整錯誤集合，避免標籤撞名或重複優先性只在單一入口正確。</summary>
static void ExpectQueryErrors(string query, Dictionary<string, string[]> errors, bool list)
{
    var context = new Microsoft.AspNetCore.Http.DefaultHttpContext();
    context.Request.QueryString = new Microsoft.AspNetCore.Http.QueryString(query);
    try
    {
        if (list) ApiInput.ListQuery(context.Request, false, true);
        else ApiInput.NoQuery(context.Request);
    }
    catch (ApiFailure failure) when (failure.Code == "E0001")
    {
        CheckErrors(failure, errors);
        return;
    }
    throw new Exception("Expected query validation errors");
}

/// <summary>直接餵原始 JSON Body，確認設定型態未被 MVC model binding 轉換。</summary>
static async Task<(bool Enabled, int WindowMinutes, int FailureThreshold, int LockSeconds)> ParseSettingsAsync(string json)
{
    var context = new Microsoft.AspNetCore.Http.DefaultHttpContext();
    context.Request.ContentType = "application/json";
    using var stream = new MemoryStream(System.Text.Encoding.UTF8.GetBytes(json));
    context.Request.Body = stream;
    return await ApiInput.SettingsBodyAsync(context.Request, CancellationToken.None);
}

/// <summary>逐字比對設定 Body 的固定欄位錯誤，涵蓋 null、型態與未知欄位。</summary>
static async Task ExpectSettingsFailureAsync(string json, string field, string message)
{
    try
    {
        await ParseSettingsAsync(json);
    }
    catch (ApiFailure failure) when (failure.Code == "E0001" && failure.Errors?.TryGetValue(field, out var messages) == true && messages.SequenceEqual([message]))
    {
        return;
    }
    throw new Exception("Expected settings error " + field + ": " + message);
}
