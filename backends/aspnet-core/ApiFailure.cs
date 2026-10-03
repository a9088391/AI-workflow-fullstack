namespace AspNetCore;

/// <summary>保留共用 API 業務錯誤及安全的欄位明細，供單一入口產生一致回應。</summary>
internal sealed class ApiFailure(string code, Dictionary<string, string[]>? errors = null, int? retryAfter = null, int? httpStatus = null) : Exception(code)
{
    internal string Code { get; } = code;
    internal Dictionary<string, string[]>? Errors { get; } = errors;
    internal int? RetryAfter { get; } = retryAfter;
    internal int? HttpStatus { get; } = httpStatus;
}
