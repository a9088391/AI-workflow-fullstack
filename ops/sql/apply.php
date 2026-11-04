<?php

declare(strict_types=1);

// 本檔為單一 SQL 執行入口，受控例外與流程保留同檔以維持直接執行方式。
// phpcs:disable PSR1.Files.SideEffects

namespace Articles\Ops\Sql;

use PDO;
use RuntimeException;
use Throwable;

/** 集中 SQL 入口：鎖定、查版、必要備份並逐支交易套用，失敗即阻止應用就緒。 */

/** 只顯示符合版本命名的識別字，避免資料庫任意文字流入啟動日誌。 */
function safeVersionLabel(string $version): string
{
    return preg_match('/\A[0-9]{4}_[A-Za-z0-9_-]+\z/D', $version) === 1 ? $version : '[無效版本識別]';
}

/** 受控的可公開失敗原因；其餘外部例外一律只輸出所在階段。 */
class SqlEntryFailure extends RuntimeException
{
}

$stage = 'SQL 入口初始化失敗';
try {
    // 先驗證連線與角色所需設定，失敗時不輸出密碼或 PDO 原始診斷。
    $host = getenv('DB_HOST') ?: 'db';
    $port = getenv('DB_PORT') ?: '5432';
    $database = getenv('DB_DATABASE') ?: 'articles';
    $owner = getenv('DB_OWNER_USER') ?: 'articles_owner';
    $ownerPassword = getenv('DB_OWNER_PASSWORD');
    $appPassword = getenv('DB_APP_PASSWORD');
    if (! $ownerPassword || ! $appPassword) {
        throw new SqlEntryFailure('必要資料庫設定或 SQL 版本檔缺失');
    }
    $stage = '資料庫連線失敗';
    $pdo = new PDO(
        "pgsql:host=$host;port=$port;dbname=$database",
        $owner,
        $ownerPassword,
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
    );
    $stage = 'SQL 鎖定失敗';
    // 集中入口與應用連線使用同一業務時區，版本時間及備份前檢查才有一致語意。
    $pdo->exec("SET TIME ZONE 'Asia/Taipei'");
    // Session advisory lock 橫跨檢查、備份及交易，避免兩個入口並行判斷不同版本。
    $pdo->query('SELECT pg_advisory_lock(1907202601)');

    // 鎖後只讀每支 SQL 一次；備份期間檔案若變動，執行內容與 checksum 仍來自同一快照。
    $stage = 'SQL 版本檔讀取失敗';
    $dir = dirname(__DIR__, 2) . '/database/sql';
    $files = @glob($dir . '/[0-9][0-9][0-9][0-9]_*.sql') ?: [];
    sort($files, SORT_STRING);
    if ($files === []) {
        throw new SqlEntryFailure('必要資料庫設定或 SQL 版本檔缺失');
    }
    $versions = [];
    foreach ($files as $file) {
        $version = basename($file, '.sql');
        $contents = @file_get_contents($file);
        if ($contents === false) {
            throw new SqlEntryFailure('SQL 版本檔讀取失敗：' . safeVersionLabel($version));
        }
        $versions[$version] = ['contents' => $contents, 'checksum' => hash('sha256', $contents)];
    }

    $stage = 'SQL 版本檢查失敗';
    // 只有空庫可建立版本表；既有版本逐一比對鎖後快照，避免未知結構或已套用檔案變動。
    $hasTable = (bool) $pdo->query("SELECT to_regclass('public.schema_migrations') IS NOT NULL")->fetchColumn();
    if (! $hasTable) {
        $otherTables = (int) $pdo->query("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")->fetchColumn();
        if ($otherTables > 0) {
            throw new SqlEntryFailure('資料庫已有未知結構，拒絕初始化版本表');
        }
        $pdo->beginTransaction();
        $pdo->exec(
            ('CREATE TABLE schema_migrations (version text PRIMARY KEY, ' .
                'checksum char(64) NOT NULL, applied_at timestamptz NOT NULL ' .
                'DEFAULT now())')
        );
        $pdo->commit();
    }

    $applied = $pdo->query('SELECT version, checksum FROM schema_migrations ORDER BY version')
        ->fetchAll(PDO::FETCH_KEY_PAIR);
    foreach ($applied as $version => $checksum) {
        if (! isset($versions[$version])) {
            throw new SqlEntryFailure('未知資料庫版本：' . safeVersionLabel((string) $version));
        }
        if ($versions[$version]['checksum'] !== trim($checksum)) {
            throw new SqlEntryFailure('SQL checksum 不符：' . safeVersionLabel((string) $version));
        }
    }
    $pending = array_diff_key($versions, $applied);
    if ($pending === []) {
        fwrite(STDOUT, "SQL 已是目前版本\n");
        exit(0);
    }

    // 既有版本升級必須先停本專案寫入並保留完整備份；首次空庫不重複備份。
    if ($applied !== []) {
        if (getenv('PROJECT_WRITERS_STOPPED') !== '1') {
            throw new SqlEntryFailure('有待套用版本；尚未確認本專案寫入已停止');
        }
        $stage = 'pg_dump 備份失敗';
        $backupDir = getenv('BACKUP_DIR') ?: '/backups';
        if (! is_dir($backupDir) || ! is_writable($backupDir)) {
            throw new SqlEntryFailure('備份目錄不可寫');
        }
        $backup = $backupDir . '/articles-' . gmdate('Ymd-His') . '-' . bin2hex(random_bytes(4)) . '.dump';
        $command = [
            'pg_dump', '--format=custom', '--no-owner', '--host=' . $host, '--port=' . $port,
            '--username=' . $owner, '--dbname=' . $database, '--file=' . $backup,
        ];
        // 備份輸出不使用且可能很大；直接丟棄 stdout/stderr，避免 pipe 塞滿而卡住 SQL 入口。
        $process = @proc_open(
            $command,
            [0 => ['file', '/dev/null', 'r'], 1 => ['file', '/dev/null', 'w'], 2 => ['file', '/dev/null', 'w']],
            $pipes,
            null,
            ['PGPASSWORD' => $ownerPassword, 'PATH' => getenv('PATH') ?: '/usr/bin:/bin']
        );
        if (! is_resource($process)) {
            throw new SqlEntryFailure('無法啟動 pg_dump');
        }
        $backupStatus = @proc_close($process);
        if ($backupStatus !== 0 || ! is_file($backup) || @filesize($backup) <= 0) {
            @unlink($backup);
            throw new SqlEntryFailure('pg_dump 備份失敗');
        }
        fwrite(STDOUT, "已保留資料庫備份：$backup\n");
    }

    // 應用帳號僅取得 DML；DDL 只由此入口使用 owner 權限執行。
    $stage = '應用角色授權失敗';
    $roleExists = (bool) $pdo->query("SELECT 1 FROM pg_roles WHERE rolname = 'articles_app'")->fetchColumn();
    if (! $roleExists) {
        $pdo->exec('CREATE ROLE articles_app LOGIN PASSWORD ' . $pdo->quote($appPassword));
    }
    $pdo->exec('GRANT CONNECT ON DATABASE articles TO articles_app');
    $pdo->exec('GRANT USAGE ON SCHEMA public TO articles_app');
    foreach ($pending as $version => $migration) {
        $stage = 'SQL 套用失敗：' . safeVersionLabel((string) $version);
        $pdo->beginTransaction();
        try {
            // 同支 SQL 和 checksum 版本紀錄必須一起提交；失敗只回滾本支，先前成功版本保留。
            $pdo->exec($migration['contents']);
            $statement = $pdo->prepare('INSERT INTO schema_migrations (version, checksum) VALUES (?, ?)');
            $statement->execute([$version, $migration['checksum']]);
            $pdo->commit();
            fwrite(STDOUT, '已套用 SQL：' . safeVersionLabel((string) $version) . "\n");
        } catch (Throwable $exception) {
            $pdo->rollBack();
            throw new SqlEntryFailure($stage, 0, $exception);
        }
    }
} catch (Throwable $exception) {
    // 只輸出受控訊息或目前階段；PDO、pg_dump 與檔案原始例外不得進入啟動日誌。
    fwrite(STDERR, ($exception instanceof SqlEntryFailure ? $exception->getMessage() : $stage) . "\n");
    exit(1);
}
