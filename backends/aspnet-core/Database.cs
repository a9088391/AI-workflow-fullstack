using Npgsql;

namespace AspNetCore;

/// <summary>直接使用既有共用 SQL schema，交易與參數化查詢不建立第二套 migration。</summary>
internal sealed class Database : IAsyncDisposable
{
    private readonly NpgsqlConnection connection;
    private NpgsqlTransaction? transaction;

    /// <summary>接管已開啟的請求連線，讓同次交易與查詢共用並由此物件負責釋放。</summary>
    private Database(NpgsqlConnection connection) => this.connection = connection;

    /// <summary>每個請求使用獨立連線並固定台北 session 時區，供跨後端時間一致性查核。</summary>
    internal static async Task<Database> OpenAsync(CancellationToken cancellationToken)
    {
        var builder = new NpgsqlConnectionStringBuilder
        {
            Host = Environment.GetEnvironmentVariable("DB_HOST") ?? "db",
            Port = int.TryParse(Environment.GetEnvironmentVariable("DB_PORT"), out var port) ? port : 5432,
            Database = Environment.GetEnvironmentVariable("DB_DATABASE") ?? "articles",
            Username = Environment.GetEnvironmentVariable("DB_USERNAME") ?? "articles_app",
            Password = Environment.GetEnvironmentVariable("DB_PASSWORD") ?? "",
            Timezone = "Asia/Taipei"
        };
        var connection = new NpgsqlConnection(builder.ConnectionString);
        await connection.OpenAsync(cancellationToken);
        return new Database(connection);
    }

    /// <summary>登入與身分查核以同一交易持有既定行鎖，直到完成結果或回滾。</summary>
    internal async Task BeginAsync(CancellationToken cancellationToken) => transaction = await connection.BeginTransactionAsync(cancellationToken);

    /// <summary>先提交需要保存的業務狀態，再允許控制器回傳 401 或 429。</summary>
    internal async Task CommitAsync(CancellationToken cancellationToken)
    {
        if (transaction is null) return;
        await transaction.CommitAsync(cancellationToken);
        await transaction.DisposeAsync();
        transaction = null;
    }

    /// <summary>讀取資料庫目前時間而非等待行鎖前的交易開始時間。</summary>
    internal async Task<DateTime> NowAsync(CancellationToken cancellationToken) => (DateTime)(await ScalarAsync("SELECT clock_timestamp()", cancellationToken: cancellationToken) ?? throw new InvalidOperationException("資料庫時間缺漏"));

    /// <summary>用參數化 SQL 讀取整列，避免內容或查詢值變成 SQL 語法。</summary>
    internal async Task<List<Dictionary<string, object?>>> RowsAsync(string sql, Dictionary<string, object?>? parameters = null, CancellationToken cancellationToken = default)
    {
        await using var command = Command(sql, parameters);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        var rows = new List<Dictionary<string, object?>>();
        // 逐列保留資料庫原生型態，只將 DBNull 轉 null，避免自行轉字串而改變輸出與比較。
        while (await reader.ReadAsync(cancellationToken))
        {
            var row = new Dictionary<string, object?>(StringComparer.Ordinal);
            for (var i = 0; i < reader.FieldCount; i++) row[reader.GetName(i)] = await reader.IsDBNullAsync(i, cancellationToken) ? null : reader.GetValue(i);
            rows.Add(row);
        }
        return rows;
    }

    /// <summary>回傳單筆或 null，供目前帳號、設定及父文章查核。</summary>
    internal async Task<Dictionary<string, object?>?> RowAsync(string sql, Dictionary<string, object?>? parameters = null, CancellationToken cancellationToken = default) => (await RowsAsync(sql, parameters, cancellationToken)).FirstOrDefault();

    /// <summary>在資料庫完成精確計數，不把整張表載入應用程式。</summary>
    internal async Task<object?> ScalarAsync(string sql, Dictionary<string, object?>? parameters = null, CancellationToken cancellationToken = default)
    {
        await using var command = Command(sql, parameters);
        return await command.ExecuteScalarAsync(cancellationToken);
    }

    /// <summary>執行受限欄位寫入，交易提交與錯誤映射由呼叫端決定。</summary>
    internal async Task<int> ExecuteAsync(string sql, Dictionary<string, object?>? parameters = null, CancellationToken cancellationToken = default)
    {
        await using var command = Command(sql, parameters);
        return await command.ExecuteNonQueryAsync(cancellationToken);
    }

    /// <summary>集中綁定參數與目前交易，避免查詢意外離開鎖保護範圍。</summary>
    private NpgsqlCommand Command(string sql, Dictionary<string, object?>? parameters)
    {
        var command = new NpgsqlCommand(sql, connection, transaction);
        if (parameters is not null) foreach (var (name, value) in parameters) command.Parameters.AddWithValue(name, value ?? DBNull.Value);
        return command;
    }

    /// <summary>結束請求時未提交的交易由驅動回滾，避免部分認證狀態落地。</summary>
    public async ValueTask DisposeAsync()
    {
        if (transaction is not null) await transaction.DisposeAsync();
        await connection.DisposeAsync();
    }

    /// <summary>業務時間統一回臺北秒級文字，JWT 仍使用 UTC NumericDate。</summary>
    internal static string DisplayTime(DateTime value) => TimeZoneInfo.ConvertTimeFromUtc(DateTime.SpecifyKind(value, DateTimeKind.Utc), TimeZoneInfo.FindSystemTimeZoneById("Asia/Taipei")).ToString("yyyy-MM-dd HH:mm:ss", System.Globalization.CultureInfo.InvariantCulture);
}
