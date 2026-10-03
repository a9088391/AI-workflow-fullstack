using AspNetCore;

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
    var wrongAlgorithm = SignedToken("{\"alg\":\"none\",\"typ\":\"JWT\"}", "{\"iss\":\"ai-workflow-fullstack:aspnet-core\",\"aud\":\"ai-workflow-fullstack-api:aspnet-core\",\"sub\":\"42\",\"jti\":\"" + jti + "\",\"iat\":" + issued + ",\"nbf\":" + issued + ",\"exp\":" + (issued + 28800) + "}", firstKey);
    // 即使測試金鑰重新簽署，非 HS256 header 仍不得通過。
    ExpectFailure(() => jwt.Verify(wrongAlgorithm, issued), "E0002");
    var missingClaim = SignedToken("{\"alg\":\"HS256\",\"typ\":\"JWT\"}", "{\"iss\":\"ai-workflow-fullstack:aspnet-core\",\"aud\":\"ai-workflow-fullstack-api:aspnet-core\",\"sub\":\"42\",\"jti\":\"" + jti + "\",\"iat\":" + issued + ",\"nbf\":" + issued + "}", firstKey);
    // 缺少必要時間 claim 時不得補預設值接受。
    ExpectFailure(() => jwt.Verify(missingClaim, issued), "E0002");
    await File.WriteAllTextAsync(keyFile, Convert.ToBase64String(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32)) + "\n");
    // 換成本後端另一把金鑰後，舊憑證應立即無效。
    ExpectFailure(() => new JwtToken(keyFile).Verify(token, issued), "E0002");
}
finally { File.Delete(keyFile); }
Console.WriteLine("ASP.NET contract checks passed");

/// <summary>在最小 console harness 中保留失敗出口，避免邏輯退化仍回零。</summary>
static void Check(bool condition, string name)
{
    if (!condition) throw new Exception(name);
}

/// <summary>驗證安全路徑拒絕而非只確認方法回傳值。</summary>
static void ExpectFailure(Action action, string code)
{
    try { action(); }
    catch (ApiFailure failure) when (failure.Code == code) { return; }
    throw new Exception("Expected " + code);
}

/// <summary>用真正完成的非同步結果確認損壞雜湊不落入一般錯密碼路徑。</summary>
static async Task ExpectFailureAsync(Func<Task> action, string code)
{
    try { await action(); }
    catch (ApiFailure failure) when (failure.Code == code) { return; }
    throw new Exception("Expected " + code);
}

/// <summary>以測試金鑰簽自訂 claims，驗證固定 header 與必填欄位確實由 verifier 拒絕。</summary>
static string SignedToken(string header, string payload, byte[] key)
{
    var input = JwtToken.Encode(System.Text.Encoding.UTF8.GetBytes(header)) + "." + JwtToken.Encode(System.Text.Encoding.UTF8.GetBytes(payload));
    return input + "." + JwtToken.Encode(System.Security.Cryptography.HMACSHA256.HashData(key, System.Text.Encoding.ASCII.GetBytes(input)));
}
