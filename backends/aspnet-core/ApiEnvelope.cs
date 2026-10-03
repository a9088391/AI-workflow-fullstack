using Microsoft.AspNetCore.Mvc;

namespace AspNetCore;

/// <summary>將本票各端點的結果轉成共用成功與失敗 envelope，避免框架預設文案外洩。</summary>
internal static class ApiEnvelope
{
    private static readonly Dictionary<string, (int Status, string Message)> Failures = new()
    {
        ["E0001"] = (422, "欄位資料不正確，請確認後再送出"),
        ["E0002"] = (401, "登入已逾期，請重新登入"),
        ["E0003"] = (403, "沒有權限使用此功能"),
        ["E0004"] = (404, "找不到指定資料或 API"),
        ["E0005"] = (400, "系統暫時無法處理，請稍後再試"),
        ["E0101"] = (401, "帳號或密碼錯誤"),
        ["E0102"] = (403, "此帳號已停用，請聯絡系統管理員"),
        ["E0104"] = (500, "後台認證設定異常，請聯絡系統管理員"),
        ["E0105"] = (429, "登入失敗次數過多，請稍後再試"),
        ["E0201"] = (409, "內容已更新，請確認最新內容後再送出"),
        ["E9999"] = (500, "系統暫時無法處理，請稍後再試")
    };

    /// <summary>成功時固定 OK 並保持 data 為物件，讓前端不需猜測框架格式。</summary>
    internal static IActionResult Success(object data, int status = 200) => new ObjectResult(new { success = true, message = "OK", data }) { StatusCode = status };

    /// <summary>失敗時附逐請求 log_id；僅驗證錯誤包含 errors 並依鎖定回 Retry-After。</summary>
    internal static IActionResult Failure(HttpContext context, ApiFailure failure)
    {
        var (status, message) = Failures[failure.Code];
        context.Items["error_code"] = failure.Code;
        if (failure.RetryAfter is int seconds) context.Response.Headers.RetryAfter = seconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
        var logId = context.Items["log_id"]?.ToString() ?? Guid.NewGuid().ToString();
        object body = failure.Errors is null
            ? new { success = false, message, error_code = failure.Code, log_id = logId }
            : new { success = false, message, error_code = failure.Code, errors = failure.Errors, log_id = logId };
        return new ObjectResult(body) { StatusCode = failure.HttpStatus ?? status };
    }
}
