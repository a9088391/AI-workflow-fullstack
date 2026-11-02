using System.Net;
using System.Text;
using System.Text.Json;

namespace AspNetCore;

/// <summary>讀取原始 HTTP 輸入以保留重複欄位與型態差異，不依賴 MVC 的寬鬆 model binding。</summary>
internal static class ApiInput
{
    /// <summary>先驗媒體型態與 JSON 語法，再逐欄回報固定驗證文案；登入格式錯誤不碰鎖定狀態。</summary>
    internal static async Task<Dictionary<string, string>> BodyAsync(HttpRequest request, string[] fields, bool emptyAllowed, CancellationToken cancellationToken)
    {
        // 先檢查媒體型態與 JSON 根型態，避免 model binding 將錯誤輸入隱式轉型。
        await using var stream = new MemoryStream();
        await request.Body.CopyToAsync(stream, cancellationToken);
        if (emptyAllowed && stream.Length == 0) return [];
        if (stream.Length == 0 || !string.Equals(request.ContentType?.Split(';', 2)[0].Trim(), "application/json", StringComparison.OrdinalIgnoreCase))
            throw Invalid("_body", "Request Body 必須為 JSON object");
        var document = DecodeDocument(stream.ToArray());
        using (document)
        {
            if (document.RootElement.ValueKind != JsonValueKind.Object) throw Invalid("_body", "Request Body 必須為 JSON object");
            var values = new Dictionary<string, string>(StringComparer.Ordinal);
            var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
            var properties = document.RootElement.EnumerateObject().ToArray();
            foreach (var field in fields)
            {
                // 逐筆比對解碼欄名，保留重複鍵，避免解析後覆寫使帳密驗證失效。
                var matches = properties.Where(property => property.Name == field).ToArray();
                if (matches.Length == 0) errors[field] = ["此欄位為必填"];
                else if (matches.Length > 1) errors[field] = ["此參數不可重複"];
                else if (matches[0].Value.ValueKind == JsonValueKind.Null) errors[field] = ["此欄位不可為 null"];
                else if (matches[0].Value.ValueKind != JsonValueKind.String) errors[field] = ["欄位型態不正確"];
                else values[field] = matches[0].Value.GetString() ?? "";
            }
            // 所有欄位先收集型態與未知鍵錯誤，再做內容格式驗證，一次回報固定欄位文案。
            foreach (var property in properties)
            {
                if (!fields.Contains(property.Name, StringComparer.Ordinal)) errors[property.Name] = ["不支援此欄位"];
            }
            if (values.TryGetValue("email", out var email))
            {
                email = Rules.NormalizeEmail(email);
                values["email"] = email;
                if (!Rules.ValidEmail(email)) errors["email"] = ["欄位格式或範圍不正確"];
            }
            if (values.TryGetValue("password", out var password) && !Rules.ValidPassword(password)) errors["password"] = ["欄位格式或範圍不正確"];
            if (values.TryGetValue("nickname", out var nickname) && !Rules.ValidNickname(nickname)) errors["nickname"] = ["欄位格式或範圍不正確"];
            if (errors.Count > 0) throw new ApiFailure("E0001", errors);
            return values;
        }
    }

    /// <summary>內容欄位保留原文，version 僅接受 JSON 安全正整數；不改動帳密端點既有解析規則。</summary>
    internal static async Task<(Dictionary<string, string> Text, long Version)> ContentBodyAsync(HttpRequest request, string[] fields, CancellationToken cancellationToken)
    {
        // 先完整解碼 JSON，才區分 object 結構與逐欄業務錯誤，避免延遲解碼破壞錯誤順序。
        await using var stream = new MemoryStream();
        await request.Body.CopyToAsync(stream, cancellationToken);
        if (stream.Length == 0 || !string.Equals(request.ContentType?.Split(';', 2)[0].Trim(), "application/json", StringComparison.OrdinalIgnoreCase))
            throw Invalid("_body", "Request Body 必須為 JSON object");
        var document = DecodeDocument(stream.ToArray());
        using (document)
        {
            if (document.RootElement.ValueKind != JsonValueKind.Object) throw Invalid("_body", "Request Body 必須為 JSON object");
            var properties = document.RootElement.EnumerateObject().ToArray();
            var text = new Dictionary<string, string>(StringComparer.Ordinal);
            var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
            long version = 0;
            foreach (var field in fields)
            {
                // JSON object 重複名稱沿用最後一筆，並只對該值查核型態與範圍。
                var found = false;
                JsonElement value = default;
                foreach (var property in properties)
                {
                    if (property.Name != field) continue;
                    found = true;
                    value = property.Value;
                }
                if (!found) errors[field] = ["此欄位為必填"];
                else if (value.ValueKind == JsonValueKind.Null) errors[field] = ["此欄位不可為 null"];
                else if (field == "version")
                {
                    // 整數表示與安全數值範圍分開，避免 runtime 容量決定公開錯誤文案。
                    if (value.ValueKind != JsonValueKind.Number || !Rules.IsJsonInteger(value.GetRawText())) errors[field] = ["欄位型態不正確"];
                    else if (!Rules.TryJsonVersion(value.GetRawText(), out version)) errors[field] = ["欄位格式或範圍不正確"];
                }
                else if (value.ValueKind != JsonValueKind.String) errors[field] = ["欄位型態不正確"];
                else
                {
                    // 各操作傳入固定欄位集合，用它選擇文章／留言長度，不裁切或轉換原文。
                    var raw = value.GetString() ?? "";
                    text[field] = raw;
                    var maximum = field == "title" ? 200 : fields.Contains("title", StringComparer.Ordinal) ? 20000 : 2000;
                    if (field is "title" or "body" && !Rules.ValidContent(raw, maximum)) errors[field] = ["欄位格式或範圍不正確"];
                    if (field == "visibility" && raw is not ("visible" or "hidden")) errors[field] = ["欄位格式或範圍不正確"];
                    if (field == "status" && raw is not ("active" or "disabled")) errors[field] = ["欄位格式或範圍不正確"];
                }
            }
            // 未知名稱即使重複仍是未支援欄位，不把 JSON Body 當作 Query 重複參數。
            foreach (var property in properties)
            {
                if (fields.Contains(property.Name, StringComparer.Ordinal)) continue;
                errors[property.Name] = ["不支援此欄位"];
            }
            if (errors.Count > 0) throw new ApiFailure("E0001", errors);
            return (text, version);
        }
    }

    /// <summary>GET 不接受非空 Body，即使 Content-Length 未提供也實際讀取檢查。</summary>
    internal static async Task NoGetBodyAsync(HttpRequest request, CancellationToken cancellationToken)
    {
        using var buffer = new MemoryStream();
        await request.Body.CopyToAsync(buffer, cancellationToken);
        if (buffer.Length > 0) throw Invalid("_body", "此 API 不接受 Request Body");
    }

    /// <summary>禁止無 Query 端點隱含接受未知或重複參數。</summary>
    internal static void NoQuery(HttpRequest request)
    {
        var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (name, _, _, identity) in Pairs(request))
        {
            var key = ErrorKey(name);
            // 顯示標籤可撞名；只有相同 decoded bytes 重複，且此結果不可被後續未知名稱蓋掉。
            if (!seen.Add(identity)) errors[key] = ["此參數不可重複"];
            else errors.TryAdd(key, ["不支援此欄位"]);
        }
        if (errors.Count > 0) throw new ApiFailure("E0001", errors);
    }

    /// <summary>管理範圍先執行角色門禁，避免格式錯誤洩漏管理資料。</summary>
    internal static bool ManagementRequested(HttpRequest request)
    {
        // 只辨識解碼後明確提出的管理範圍，不讓其他格式錯誤搶先於角色門禁回應。
        return Pairs(request).Any(pair => pair.Name == "filters[scope]" && pair.Value == "management");
    }

    /// <summary>列表必填 page/per_page；管理 scope 與會員 keyword 各依端點白名單和共同文案驗證。</summary>
    internal static (long Page, int PerPage, string? Filter) ListQuery(HttpRequest request, bool allowScope, bool allowKeyword = false)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        // 先辨識巢狀欄名與重複參數，避免框架把陣列 Query 當成合法純文字。
        foreach (var (name, value, validValue, nameIdentity) in Pairs(request))
        {
            var canonical = name.StartsWith("page[", StringComparison.Ordinal) ? "page"
                : name.StartsWith("per_page[", StringComparison.Ordinal) ? "per_page"
                : name.StartsWith("filters[scope][", StringComparison.Ordinal) ? "filters[scope]"
                : name.StartsWith("filters[keyword][", StringComparison.Ordinal) ? "filters[keyword]" : name;
            var allowed = canonical is "page" or "per_page"
                || allowScope && canonical == "filters[scope]"
                || allowKeyword && canonical == "filters[keyword]";
            var key = ErrorKey(canonical);
            // 合法陣列名稱沿用既有 canonical 規則，其餘以 bytes 辨識，避免安全標籤合併不同參數。
            var identity = canonical == name ? nameIdentity : Convert.ToHexString(Encoding.UTF8.GetBytes(canonical));
            if (!seen.Add(identity)) errors[key] = ["此參數不可重複"];
            else if (canonical != name && allowed) errors[key] = ["欄位型態不正確"];
            else if (!allowed) errors.TryAdd(key, ["不支援此欄位"]);
            else if (allowKeyword && canonical == "filters[keyword]" && !validValue) errors[key] = ["欄位格式或範圍不正確"];
            values[canonical] = value;
        }
        // 再驗必填數字與可選篩選值；不合法 Query 一律不進入資料庫列表查詢。
        foreach (var name in new[] { "page", "per_page" })
        {
            if (!values.TryGetValue(name, out var value)) errors[name] = ["此欄位為必填"];
            else if (!errors.ContainsKey(name) && (!Rules.TryId(value, out var number) || name == "per_page" && number > 100)) errors[name] = ["欄位格式或範圍不正確"];
        }
        if (allowScope && values.TryGetValue("filters[scope]", out var scope) && !errors.ContainsKey("filters.scope") && scope != "management") errors["filters.scope"] = ["欄位格式或範圍不正確"];
        if (allowKeyword && values.TryGetValue("filters[keyword]", out var keyword) && !errors.ContainsKey("filters.keyword"))
        {
            // 只裁切 U+0020，拒絕 PostgreSQL text 無法儲存的 NUL 與超長關鍵字。
            keyword = keyword.Trim(' ');
            if (keyword.Contains('\0') || keyword.EnumerateRunes().Count() > 254) errors["filters.keyword"] = ["欄位格式或範圍不正確"];
            values["filters[keyword]"] = keyword;
        }
        if (errors.Count > 0) throw new ApiFailure("E0001", errors);
        Rules.TryId(values["page"], out var page);
        Rules.TryId(values["per_page"], out var perPage);
        return (page, (int)perPage, values.GetValueOrDefault(allowKeyword ? "filters[keyword]" : "filters[scope]"));
    }

    /// <summary>管理設定以 JSON 最後同名鍵為準，嚴格驗四欄原生型態及範圍。</summary>
    internal static async Task<(bool Enabled, int WindowMinutes, int FailureThreshold, int LockSeconds)> SettingsBodyAsync(HttpRequest request, CancellationToken cancellationToken)
    {
        // 先確認 JSON 可完整解碼且為 object，才驗四欄，讓解析錯誤優先於業務欄位錯誤。
        await using var stream = new MemoryStream();
        await request.Body.CopyToAsync(stream, cancellationToken);
        if (stream.Length == 0 || !string.Equals(request.ContentType?.Split(';', 2)[0].Trim(), "application/json", StringComparison.OrdinalIgnoreCase))
            throw Invalid("_body", "Request Body 必須為 JSON object");
        var document = DecodeDocument(stream.ToArray());
        using (document)
        {
            if (document.RootElement.ValueKind != JsonValueKind.Object) throw Invalid("_body", "Request Body 必須為 JSON object");
            var properties = document.RootElement.EnumerateObject().ToArray();
            var fields = new[] { "enabled", "window_minutes", "failure_threshold", "lock_seconds" };
            var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
            var values = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
            // JSON object 同名鍵採最後一筆；每個欄位仍依必填、null、型態、範圍先後裁決。
            foreach (var property in properties)
            {
                if (fields.Contains(property.Name, StringComparer.Ordinal)) values[property.Name] = property.Value;
                else errors[property.Name] = ["不支援此欄位"];
            }
            foreach (var field in fields)
            {
                if (!values.TryGetValue(field, out var value)) errors[field] = ["此欄位為必填"];
                else if (value.ValueKind == JsonValueKind.Null) errors[field] = ["此欄位不可為 null"];
                else if (field == "enabled")
                {
                    // 開關只接受原生布林值，不能讓字串或數字隱式轉型。
                    if (value.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) errors[field] = ["欄位型態不正確"];
                }
                else if (value.ValueKind != JsonValueKind.Number || !Rules.IsJsonInteger(value.GetRawText())) errors[field] = ["欄位型態不正確"];
                else
                {
                    // 超過 Int64 的整數仍屬範圍錯誤，不能用 runtime 容量改變公開 JSON 型態。
                    var maximum = field == "window_minutes" ? 60 : field == "failure_threshold" ? 100 : 86400;
                    if (!value.TryGetInt64(out var number) || number < 1 || number > maximum) errors[field] = ["欄位格式或範圍不正確"];
                }
            }
            if (errors.Count > 0) throw new ApiFailure("E0001", errors);
            return (values["enabled"].GetBoolean(), values["window_minutes"].GetInt32(), values["failure_threshold"].GetInt32(), values["lock_seconds"].GetInt32());
        }
    }

    /// <summary>先完整解碼所有 JSON 字串，讓未知、巢狀或被覆蓋的無效字串也走解析錯誤。</summary>
    private static JsonDocument DecodeDocument(byte[] bytes)
    {
        try
        {
            // JsonDocument 延遲解碼字串；先掃描全部 token，保留既有語法、深度與重複鍵判定。
            var reader = new Utf8JsonReader(bytes);
            while (reader.Read())
            {
                if (reader.TokenType is JsonTokenType.PropertyName or JsonTokenType.String) _ = reader.GetString();
            }
            return JsonDocument.Parse(bytes);
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException)
        {
            // 捕捉範圍只涵蓋 JSON 解碼，不能把後續業務錯誤一併改成用戶端輸入錯誤。
            throw new ApiFailure("E0005");
        }
    }

    /// <summary>MVC 轉成 null 的空白路徑也交共同驗證，讓認證先完成，再回 ID 欄位錯誤。</summary>
    internal static long Id(string? raw, string field)
    {
        if (!Rules.TryId(raw, out var id)) throw Invalid(field, "欄位格式或範圍不正確");
        return id;
    }

    /// <summary>Query 逐對保留 decoded bytes 身分及安全名稱，讓重複判定不受錯誤標籤撞名影響。</summary>
    private static IEnumerable<(string Name, string Value, bool ValueValid, string Identity)> Pairs(HttpRequest request)
    {
        var raw = request.QueryString.Value?.TrimStart('?') ?? "";
        if (raw == "") yield break;
        foreach (var pair in raw.Split('&'))
        {
            // 名稱 bytes 不經 Unicode 替代；無效名稱整段編碼，才不會被誤認成合法巢狀欄位。
            var parts = pair.Split('=', 2);
            var encodedValue = parts.Length == 2 ? parts[1] : "";
            var encodedName = Encoding.UTF8.GetBytes(parts[0]);
            var nameBytes = WebUtility.UrlDecodeToBytes(encodedName, 0, encodedName.Length);
            var name = ValidUtf8(parts[0]) ? WebUtility.UrlDecode(parts[0]) : EncodedName(nameBytes);
            yield return (name, Decode(encodedValue), ValidUtf8(encodedValue), Convert.ToHexString(nameBytes));
        }
    }

    /// <summary>無效 UTF-8 名稱以整段安全百分號編碼顯示，不讓替代字元或陣列外觀改變欄位判定。</summary>
    private static string EncodedName(byte[] bytes)
    {
        var encoded = new StringBuilder();
        foreach (var value in bytes)
        {
            if (value is >= (byte)'A' and <= (byte)'Z' or >= (byte)'a' and <= (byte)'z'
                or >= (byte)'0' and <= (byte)'9' or (byte)'-' or (byte)'_' or (byte)'.' or (byte)'~') encoded.Append((char)value);
            else encoded.Append('%').Append(value.ToString("X2", System.Globalization.CultureInfo.InvariantCulture));
        }
        return encoded.ToString();
    }

    /// <summary>Query 值依表單 URL 解碼；無效 bytes 保留百分號文字，交既有欄位驗證拒絕。</summary>
    private static string Decode(string encoded)
    {
        return ValidUtf8(encoded) ? WebUtility.UrlDecode(encoded) : encoded;
    }

    /// <summary>驗證百分號編碼後的 UTF-8 bytes，區分無效位元組與合法的 U+FFFD、字面 %FF。</summary>
    private static bool ValidUtf8(string encoded)
    {
        // 先還原百分號 bytes，保留原文字的 Unicode 碼點，不靜默接受孤立 surrogate。
        var bytes = new List<byte>();
        for (var i = 0; i < encoded.Length; i++)
        {
            if (encoded[i] == '%' && i + 2 < encoded.Length && Uri.IsHexDigit(encoded[i + 1]) && Uri.IsHexDigit(encoded[i + 2]))
            {
                bytes.Add(Convert.ToByte(encoded.Substring(i + 1, 2), 16));
                i += 2;
            }
            else
            {
                if (!Rune.TryGetRuneAt(encoded, i, out var rune)) return false;
                bytes.AddRange(Encoding.UTF8.GetBytes(rune.ToString()));
                i += rune.Utf16SequenceLength - 1;
            }
        }
        // 嚴格解碼可區分無效 bytes 與合法 U+FFFD，避免替代字元改變搜尋或名稱語意。
        try
        {
            new UTF8Encoding(false, true).GetString([.. bytes]);
            return true;
        }
        catch (DecoderFallbackException)
        {
            return false;
        }
    }

    /// <summary>巢狀 filters 名稱在錯誤物件中使用點號，其餘維持原名稱。</summary>
    private static string ErrorKey(string name)
    {
        // 計算左方括號數，只將單層 filters 名稱轉點號；深層名稱保留供未知欄位定位。
        return name.StartsWith("filters[", StringComparison.Ordinal) && name.EndsWith(']') && name.Count(c => c == '[') == 1 ? "filters." + name[8..^1] : name;
    }

    /// <summary>建立單欄共同驗證錯誤，避免每端點自行拼裝訊息。</summary>
    private static ApiFailure Invalid(string field, string message) => new("E0001", new Dictionary<string, string[]> { [field] = [message] });
}
