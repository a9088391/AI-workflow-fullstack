using System.Text.RegularExpressions;
using System.Text;

namespace AspNetCore;

/// <summary>集中共用帳號與 URL 型態規則，避免 Controllers 的框架預設改變十二後端契約。</summary>
internal static partial class Rules
{
    private const long MaxId = 9007199254740991;

    /// <summary>僅去除 Email 前後 ASCII 空格並將 A–Z 轉小寫；其他字元留給格式檢查拒絕。</summary>
    internal static string NormalizeEmail(string value)
    {
        var characters = value.Trim(' ').ToCharArray();
        // 不用 Unicode 大小寫摺疊，避免非 ASCII 字元變成可註冊的 ASCII Email。
        for (var i = 0; i < characters.Length; i++)
        {
            if (characters[i] is >= 'A' and <= 'Z') characters[i] = (char)(characters[i] + ('a' - 'A'));
        }
        return new string(characters);
    }

    /// <summary>依專案簡化 ASCII Email 契約驗證，不做 DNS 或信箱別名轉換。</summary>
    internal static bool ValidEmail(string value) => value.Length <= 254 && EmailPattern().IsMatch(value);

    /// <summary>限制原始密碼為 4 至 16 碼 ASCII 英數，格式錯誤不能累計登入失敗。</summary>
    internal static bool ValidPassword(string value) => PasswordPattern().IsMatch(value);

    /// <summary>依 Unicode 碼點及固定空白集合檢查暱稱，保留有效的原始文字。</summary>
    internal static bool ValidNickname(string value)
    {
        var count = 0;
        var hasText = false;
        // 同次碼點掃描檢查長度、NUL 與非空白文字，不改動要保存的暱稱。
        foreach (var rune in value.EnumerateRunes())
        {
            count++;
            if (rune.Value == 0 || count > 30) return false;
            if (!IsContractWhitespace(rune.Value)) hasText = true;
        }
        return count > 0 && hasText;
    }

    /// <summary>內容依 Unicode 碼點及固定空白集合驗證，保留原文並在進入 PostgreSQL 前拒絕 NUL。</summary>
    internal static bool ValidContent(string value, int maximum)
    {
        var count = 0;
        var hasText = false;
        // 按實際碼點累計，不把 UTF-16 長度當字數，也不裁切換行或前後空白。
        foreach (var rune in value.EnumerateRunes())
        {
            count++;
            if (rune.Value == 0 || count > maximum) return false;
            if (!IsContractWhitespace(rune.Value)) hasText = true;
        }
        return count > 0 && hasText;
    }

    /// <summary>只接受安全整數範圍的 ASCII 十進位路徑或分頁值，允許前導零。</summary>
    internal static bool TryUrlInteger(string? value, long maximum, out long result)
    {
        result = 0;
        if (string.IsNullOrEmpty(value)) return false;
        // 在乘十與加下一位前檢查上限，讓超長整數安全失敗而不溢位或經過浮點數。
        foreach (var c in value)
        {
            if (c < '0' || c > '9' || result > (maximum - (c - '0')) / 10) return false;
            result = result * 10 + c - '0';
        }
        return result >= 1 && result <= maximum;
    }

    /// <summary>將 API 資源 ID 限於 JavaScript 可精確表示的正整數。</summary>
    internal static bool TryId(string? value, out long result) => TryUrlInteger(value, MaxId, out result);

    /// <summary>只判定已解析 JSON number 的整數表示，讓小數／指數型態與數值越界分開回報。</summary>
    internal static bool IsJsonInteger(string raw) => raw.IndexOfAny(['.', 'e', 'E']) < 0;

    /// <summary>JSON version 不允許小數與指數表示法；上限避免前端數字失去精度。</summary>
    internal static bool TryJsonVersion(string raw, out long result)
    {
        result = 0;
        return raw.Length > 0 && raw[0] is >= '1' and <= '9' && TryUrlInteger(raw, MaxId, out result);
    }

    /// <summary>固定空白表必須跨語言一致，不能依賴各 runtime 的 Trim 判定。</summary>
    private static bool IsContractWhitespace(int c) => c is >= 0x9 and <= 0xD or 0x20 or 0x85 or 0xA0 or 0x1680 or >= 0x2000 and <= 0x200A or 0x2028 or 0x2029 or 0x202F or 0x205F or 0x3000;

    /// <summary>固定 Email 語法與段落長度，避免不同 runtime 的內建 Email 規則漂移。</summary>
    [GeneratedRegex(@"\A(?=.{1,64}@)[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\z", RegexOptions.CultureInvariant)]
    private static partial Regex EmailPattern();

    /// <summary>密碼只接受 ASCII 英數原文，避免 Unicode 類別判斷擴張格式。</summary>
    [GeneratedRegex(@"\A[A-Za-z0-9]{4,16}\z", RegexOptions.CultureInvariant)]
    private static partial Regex PasswordPattern();
}
