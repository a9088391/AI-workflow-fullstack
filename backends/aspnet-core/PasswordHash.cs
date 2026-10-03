using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Konscious.Security.Cryptography;

namespace AspNetCore;

/// <summary>以核准的 Argon2id 成本和 PHC 表示互通十二後端，不使用 ASP.NET 預設密碼格式。</summary>
internal static partial class PasswordHash
{
    /// <summary>每次註冊產生新 salt，並保存不含 padding 的標準 PHC 字串。</summary>
    internal static async Task<string> CreateAsync(string password)
    {
        var salt = RandomNumberGenerator.GetBytes(16);
        var hash = await DeriveAsync(password, salt);
        return "$argon2id$v=19$m=19456,t=2,p=1$" + Convert.ToBase64String(salt).TrimEnd('=') + "$" + Convert.ToBase64String(hash).TrimEnd('=');
    }

    /// <summary>先驗存量雜湊的版本、成本與正規編碼，損壞時不可誤計為錯密碼。</summary>
    internal static async Task<bool> VerifyAsync(string password, string phc)
    {
        var match = PhcPattern().Match(phc);
        if (!match.Success || !TryDecode(match.Groups[1].Value, 16, out var salt) || !TryDecode(match.Groups[2].Value, 32, out var expected)) throw new ApiFailure("E0104");
        var actual = await DeriveAsync(password, salt);
        return CryptographicOperations.FixedTimeEquals(actual, expected);
    }

    /// <summary>使用已核准函式庫計算 v19 Argon2id，輸入是原始 ASCII 密碼 bytes。</summary>
    private static async Task<byte[]> DeriveAsync(string password, byte[] salt)
    {
        using var argon = new Argon2id(Encoding.UTF8.GetBytes(password)) { Salt = salt, MemorySize = 19456, Iterations = 2, DegreeOfParallelism = 1 };
        return await argon.GetBytesAsync(32);
    }

    /// <summary>只接受可往返的無 padding PHC Base64，避免同一雜湊多種表示。</summary>
    private static bool TryDecode(string encoded, int length, out byte[] bytes)
    {
        bytes = [];
        try
        {
            bytes = Convert.FromBase64String(encoded + new string('=', (4 - encoded.Length % 4) % 4));
            return bytes.Length == length && Convert.ToBase64String(bytes).TrimEnd('=') == encoded;
        }
        catch (FormatException) { return false; }
    }

    /// <summary>嚴格限定已核准 PHC 版本、參數與無 padding 欄位長度。</summary>
    [GeneratedRegex(@"\A\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+/]{22})\$([A-Za-z0-9+/]{43})\z", RegexOptions.CultureInvariant)]
    private static partial Regex PhcPattern();
}
