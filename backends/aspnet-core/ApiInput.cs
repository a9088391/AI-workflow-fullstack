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
        JsonDocument document;
        try { document = JsonDocument.Parse(stream.ToArray()); }
        catch (JsonException) { throw new ApiFailure("E0005"); }
        using (document)
        {
            if (document.RootElement.ValueKind != JsonValueKind.Object) throw Invalid("_body", "Request Body 必須為 JSON object");
            var values = new Dictionary<string, string>(StringComparer.Ordinal);
            var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
            var properties = document.RootElement.EnumerateObject().ToArray();
            foreach (var field in fields)
            {
                // 比對原始欄名，保留重複鍵，避免 JSON 解析後覆寫使契約驗證失效。
                var matches = properties.Where(property => property.Name == field).ToArray();
                if (matches.Length == 0) errors[field] = ["此欄位為必填"];
                else if (matches.Length > 1) errors[field] = ["此參數不可重複"];
                else if (matches[0].Value.ValueKind == JsonValueKind.Null) errors[field] = ["此欄位不可為 null"];
                else if (matches[0].Value.ValueKind != JsonValueKind.String) errors[field] = ["欄位型態不正確"];
                else values[field] = matches[0].Value.GetString() ?? "";
            }
            // 所有欄位先收集型態與未知鍵錯誤，再做內容格式驗證，一次回報固定欄位文案。
            foreach (var property in properties) if (!fields.Contains(property.Name, StringComparer.Ordinal)) errors[property.Name] = ["不支援此欄位"];
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
        await using var stream = new MemoryStream();
        await request.Body.CopyToAsync(stream, cancellationToken);
        if (stream.Length == 0 || !string.Equals(request.ContentType?.Split(';', 2)[0].Trim(), "application/json", StringComparison.OrdinalIgnoreCase))
            throw Invalid("_body", "Request Body 必須為 JSON object");
        JsonDocument document;
        try { document = JsonDocument.Parse(stream.ToArray()); }
        catch (JsonException) { throw new ApiFailure("E0005"); }
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
                    if (value.ValueKind != JsonValueKind.Number) errors[field] = ["欄位型態不正確"];
                    else if (!value.TryGetInt64(out _)) errors[field] = ["欄位型態不正確"];
                    else if (!Rules.TryJsonVersion(value.GetRawText(), out version)) errors[field] = ["欄位格式或範圍不正確"];
                }
                else if (value.ValueKind != JsonValueKind.String) errors[field] = ["欄位型態不正確"];
                else
                {
                    var raw = value.GetString() ?? "";
                    text[field] = raw;
                    var maximum = field == "title" ? 200 : fields.Contains("title", StringComparer.Ordinal) ? 20000 : 2000;
                    if (field is "title" or "body" && !Rules.ValidContent(raw, maximum)) errors[field] = ["欄位格式或範圍不正確"];
                    if (field == "visibility" && raw is not ("visible" or "hidden")) errors[field] = ["欄位格式或範圍不正確"];
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
        foreach (var (name, _) in Pairs(request))
        {
            var key = ErrorKey(name);
            errors[key] = [seen.Add(name) ? "不支援此欄位" : "此參數不可重複"];
        }
        if (errors.Count > 0) throw new ApiFailure("E0001", errors);
    }

    /// <summary>管理範圍先執行角色門禁，避免格式錯誤洩漏管理資料。</summary>
    internal static bool ManagementRequested(HttpRequest request)
    {
        // 只看原始 Query 中明確提出的管理範圍，不讓其他格式錯誤搶先回應。
        return Pairs(request).Any(pair => pair.Name == "filters[scope]" && pair.Value == "management");
    }

    /// <summary>列表必填 page/per_page；重複、未知、陣列與不合法 scope 均按共同文案回報。</summary>
    internal static (long Page, int PerPage, string? Scope) ListQuery(HttpRequest request, bool allowScope)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);
        // 先辨識巢狀欄名與重複參數，避免框架把陣列 Query 當成合法純文字。
        foreach (var (name, value) in Pairs(request))
        {
            var canonical = name.StartsWith("page[", StringComparison.Ordinal) ? "page" : name.StartsWith("per_page[", StringComparison.Ordinal) ? "per_page" : name.StartsWith("filters[scope][", StringComparison.Ordinal) ? "filters[scope]" : name;
            var allowed = canonical is "page" or "per_page" || allowScope && canonical == "filters[scope]";
            var key = ErrorKey(canonical);
            if (values.ContainsKey(canonical)) errors[key] = ["此參數不可重複"];
            else if (canonical != name && allowed) errors[key] = ["欄位型態不正確"];
            else if (!allowed) errors[key] = ["不支援此欄位"];
            values[canonical] = value;
        }
        // 再驗必填數字與可選 scope；不合法 Query 一律不進入資料庫列表查詢。
        foreach (var name in new[] { "page", "per_page" })
        {
            if (!values.TryGetValue(name, out var value)) errors[name] = ["此欄位為必填"];
            else if (!errors.ContainsKey(name) && (!Rules.TryId(value, out var number) || name == "per_page" && number > 100)) errors[name] = ["欄位格式或範圍不正確"];
        }
        if (allowScope && values.TryGetValue("filters[scope]", out var scope) && !errors.ContainsKey("filters.scope") && scope != "management") errors["filters.scope"] = ["欄位格式或範圍不正確"];
        if (errors.Count > 0) throw new ApiFailure("E0001", errors);
        Rules.TryId(values["page"], out var page);
        Rules.TryId(values["per_page"], out var perPage);
        return (page, (int)perPage, values.GetValueOrDefault("filters[scope]"));
    }

    /// <summary>路徑 ID 的無效字元與超出安全整數範圍都回欄位驗證錯誤。</summary>
    internal static long Id(string raw, string field)
    {
        if (!Rules.TryId(raw, out var id)) throw Invalid(field, "欄位格式或範圍不正確");
        return id;
    }

    /// <summary>原始 Query 逐對解碼，保留重複欄位並將無效 UTF-8 名稱轉為安全錯誤鍵。</summary>
    private static IEnumerable<(string Name, string Value)> Pairs(HttpRequest request)
    {
        var raw = request.QueryString.Value?.TrimStart('?') ?? "";
        if (raw == "") yield break;
        foreach (var pair in raw.Split('&'))
        {
            var parts = pair.Split('=', 2);
            yield return (Decode(parts[0]), Decode(parts.Length == 2 ? parts[1] : ""));
        }
    }

    /// <summary>Query 使用表單 URL 解碼；無效 bytes 保留原百分號文字供安全顯示。</summary>
    private static string Decode(string encoded)
    {
        var decoded = WebUtility.UrlDecode(encoded);
        return decoded.Contains('\uFFFD') ? encoded : decoded;
    }

    /// <summary>巢狀 filters 名稱在錯誤物件中使用點號，其餘維持原名稱。</summary>
    private static string ErrorKey(string name)
    {
        // 僅接受單層方括號；深層名稱保留原文供未知欄位錯誤定位。
        return name.StartsWith("filters[", StringComparison.Ordinal) && name.EndsWith(']') && name.Count(c => c == '[') == 1 ? "filters." + name[8..^1] : name;
    }

    /// <summary>建立單欄共同驗證錯誤，避免每端點自行拼裝訊息。</summary>
    private static ApiFailure Invalid(string field, string message) => new("E0001", new Dictionary<string, string[]> { [field] = [message] });
}
