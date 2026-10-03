using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AspNetCore;

/// <summary>固定本後端 HS256 金鑰、issuer 與 audience，防止跨後端憑證被沿用。</summary>
public sealed class JwtToken
{
    private const string Issuer = "ai-workflow-fullstack:aspnet-core";
    private const string Audience = "ai-workflow-fullstack-api:aspnet-core";
    private readonly byte[] key;

    /// <summary>只載入已初始化的持久金鑰；缺失或毀損須明確失敗，不能重生。</summary>
    internal JwtToken(string path)
    {
        try
        {
            var encoded = File.ReadAllText(path).TrimEnd('\n');
            key = Convert.FromBase64String(encoded);
            if (key.Length != 32 || Convert.ToBase64String(key) != encoded) throw new FormatException();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or FormatException)
        {
            throw new ApiFailure("E0104");
        }
    }

    /// <summary>以交易內的資料庫簽發秒數建立八小時憑證，角色仍由資料庫即時查核。</summary>
    internal string Issue(long accountId, string jti, long issued)
    {
        var header = Encode(Encoding.UTF8.GetBytes("{\"alg\":\"HS256\",\"typ\":\"JWT\"}"));
        var payload = Encode(JsonSerializer.SerializeToUtf8Bytes(new { iss = Issuer, aud = Audience, sub = accountId.ToString(CultureInfo.InvariantCulture), jti, iat = issued, nbf = issued, exp = issued + 28800 }));
        var signingInput = header + "." + payload;
        return signingInput + "." + Encode(HMACSHA256.HashData(key, Encoding.ASCII.GetBytes(signingInput)));
    }

    /// <summary>先驗固定演算法與簽章，再逐一確認 claims 型態、關係及無寬限時間界線。</summary>
    internal (long AccountId, string Jti, long Issued, long Expires) Verify(string token, long now)
    {
        // 先檢查三段與 HMAC，未驗簽前不信任任何 header 或 claims。
        var parts = token.Split('.');
        if (parts.Length != 3 || !TryDecode(parts[0], out var headerBytes) || !TryDecode(parts[1], out var payloadBytes)
            || !TryDecode(parts[2], out var signature) || signature.Length != 32) throw new ApiFailure("E0002");
        var expected = HMACSHA256.HashData(key, Encoding.ASCII.GetBytes(parts[0] + "." + parts[1]));
        if (!CryptographicOperations.FixedTimeEquals(signature, expected)) throw new ApiFailure("E0002");
        try
        {
            // 固定 header、來源與型態，拒絕重複 claim 與寬鬆 JSON 數字表示。
            using var header = JsonDocument.Parse(headerBytes);
            using var payload = JsonDocument.Parse(payloadBytes);
            if (header.RootElement.ValueKind != JsonValueKind.Object || header.RootElement.EnumerateObject().Count() != 2
                || !UniqueProperties(header.RootElement) || !StringProperty(header.RootElement, "alg", "HS256")
                || !StringProperty(header.RootElement, "typ", "JWT") || payload.RootElement.ValueKind != JsonValueKind.Object
                || !UniqueProperties(payload.RootElement) || !StringProperty(payload.RootElement, "iss", Issuer)
                || !StringProperty(payload.RootElement, "aud", Audience)
                || !payload.RootElement.TryGetProperty("sub", out var sub) || sub.ValueKind != JsonValueKind.String
                || !TryCanonicalId(sub.GetString(), out var accountId)
                || !payload.RootElement.TryGetProperty("jti", out var jtiElement) || jtiElement.ValueKind != JsonValueKind.String
                || !TryDecode(jtiElement.GetString() ?? "", out var jtiBytes) || jtiBytes.Length != 16
                || !IntegerProperty(payload.RootElement, "iat", out var issued) || issued < 0 || issued > long.MaxValue - 28800
                || !IntegerProperty(payload.RootElement, "nbf", out var notBefore) || notBefore != issued
                || !IntegerProperty(payload.RootElement, "exp", out var expires) || expires != issued + 28800
                || now < notBefore || now >= expires) throw new ApiFailure("E0002");
            // jti 必須為正規十六位元組識別；帳號與登入狀態會在資料庫另行確認。
            var jti = jtiElement.GetString();
            if (jti is null) throw new ApiFailure("E0002");
            return (accountId, jti, issued, expires);
        }
        catch (JsonException)
        {
            throw new ApiFailure("E0002");
        }
    }

    /// <summary>使用正規無 padding base64url，拒絕多種文字表示對應同一 jti。</summary>
    internal static string Encode(byte[] bytes) => Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    /// <summary>只接受可往返的 base64url；非法字元與非正規 padding 都是無效憑證。</summary>
    private static bool TryDecode(string text, out byte[] bytes)
    {
        bytes = [];
        // 先限制 base64url 字母表，再往返編碼確認同一位元組只有一種文字表示。
        if (text.Length == 0 || text.Any(c => !((c is >= 'A' and <= 'Z') || (c is >= 'a' and <= 'z') || (c is >= '0' and <= '9') || c is '-' or '_'))) return false;
        try
        {
            bytes = Convert.FromBase64String(text.Replace('-', '+').Replace('_', '/') + new string('=', (4 - text.Length % 4) % 4));
            return Encode(bytes) == text;
        }
        catch (FormatException)
        {
            return false;
        }
    }

    /// <summary>明確拒絕重複 JSON 名稱，避免跨語言解析器對簽署 claims 得出不同結果。</summary>
    private static bool UniqueProperties(JsonElement element)
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject()) if (!names.Add(property.Name)) return false;
        return true;
    }

    /// <summary>固定文字 claims 必須是 JSON string，不能採用框架的寬鬆型態轉換。</summary>
    private static bool StringProperty(JsonElement element, string name, string expected) => element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String && value.GetString() == expected;

    /// <summary>NumericDate 僅接受非負 JSON 整數文字，不接受小數、指數、布林或字串。</summary>
    private static bool IntegerProperty(JsonElement element, string name, out long value)
    {
        value = 0;
        return element.TryGetProperty(name, out var raw) && raw.ValueKind == JsonValueKind.Number
            && long.TryParse(raw.GetRawText(), NumberStyles.None, CultureInfo.InvariantCulture, out value);
    }

    /// <summary>JWT sub 必須是無前導零的帳號 ID 字串，資源 ID 不使用浮點數。</summary>
    private static bool TryCanonicalId(string? text, out long id) => Rules.TryId(text, out id) && id.ToString(CultureInfo.InvariantCulture) == text;
}
