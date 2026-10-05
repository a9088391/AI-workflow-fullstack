<?php

declare(strict_types=1);

// 本檔是獨立執行的測試腳本，輔助函式必須與隔離守衛及案例同檔提供。
// phpcs:disable PSR1.Files.SideEffects

// 所有寫入前先確認固定的隔離專案，連斷言功能關閉時也必須拒絕錯誤目標。
function t19Must(bool $condition, string $reason): void
{
    if (! $condition) {
        throw new RuntimeException('Ticket 19 驗證失敗：' . $reason);
    }
}

// 僅擷取 Docker 身分資訊；錯誤輸出可能含設定值，故不寫入測試記錄。
function t19Docker(array $arguments): string
{
    $process = proc_open($arguments, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    t19Must(is_resource($process), '無法執行 Docker 身分查核');
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    t19Must(proc_close($process) === 0, 'Docker 身分查核失敗');

    return trim($output);
}

// 同時核對 Compose 設定、容器標籤與實際掛載，防止固定埠誤連展示資料。
function t19Guard(string $project): void
{
    $root = dirname(__DIR__, 2);
    t19Must(
        getcwd() === $root && $project === 'ticket19_test'
        && getenv('COMPOSE_PROJECT_NAME') === 'ticket19_test'
        && getenv('COMPOSE_FILE') === 'compose.yaml:ops/tests/ticket19_compose.yaml'
        && getenv('COMPOSE_PROFILES') === 'flask',
        '須由 repo root 使用 ticket19_test 專用 Compose 設定'
    );
    foreach (['laravel.key', 'flask.key'] as $file) {
        t19Must(is_file($root . '/.local/ticket19-backend/' . $file), '專用 JWT 金鑰不存在');
    }
    t19Must(
        file_get_contents($root . '/.local/ticket19-backend/laravel.key')
            !== file_get_contents($root . '/.local/ticket19-backend/flask.key'),
        '兩後端不可共用 JWT 金鑰'
    );
    $configJson = t19Docker(['docker', 'compose', 'config', '--format', 'json']);
    $config = json_decode($configJson, true, 512, JSON_THROW_ON_ERROR);
    t19Must(
        ($config['name'] ?? null) === 'ticket19_test'
        && ($config['volumes']['postgres_data']['name'] ?? null) === 'ticket19_test_postgres_data'
        && ($config['services']['db']['environment']['POSTGRES_DB'] ?? null) === 'articles'
        && count($config['services']['db']['volumes'] ?? []) === 1
        && ($config['services']['db']['volumes'][0]['source'] ?? null) === 'postgres_data'
        && ($config['services']['db']['volumes'][0]['target'] ?? null) === '/var/lib/postgresql',
        'Compose 資料庫設定不符'
    );

    // 主機埠、金鑰與備份必須全數落在本票路徑，不能混用展示或其他票測資。
    foreach (['laravel' => 8081, 'flask' => 8085] as $service => $port) {
        $settings = $config['services'][$service] ?? [];
        $binding = $settings['ports'][0] ?? [];
        t19Must(
            count($settings['ports'] ?? []) === 1
            && ($binding['host_ip'] ?? null) === '127.0.0.1'
            && (int) ($binding['published'] ?? 0) === $port
            && (int) ($binding['target'] ?? 0) === 8000
            && ($settings['environment']['DB_HOST'] ?? null) === 'db'
            && ($settings['environment']['DB_DATABASE'] ?? null) === 'articles',
            'Compose 服務目標不符：' . $service
        );
        $mounts = array_column($settings['volumes'] ?? [], null, 'target');
        $key = $mounts['/run/secrets/' . $service . '-jwt.key'] ?? [];
        t19Must(
            ($key['source'] ?? null) === $root . '/.local/ticket19-backend/' . $service . '.key'
            && ($key['read_only'] ?? false) === true,
            'Compose 金鑰來源不符：' . $service
        );
    }
    $laravelMounts = array_column($config['services']['laravel']['volumes'] ?? [], null, 'target');
    $sqlMounts = array_column($config['services']['sql']['volumes'] ?? [], null, 'target');
    t19Must(
        ($laravelMounts['/srv/laravel/storage/logs']['source'] ?? null)
            === $root . '/.local/ticket19-backend/laravel-logs'
        && ($sqlMounts['/backups']['source'] ?? null) === $root . '/.local/ticket19-backend/backups',
        'Compose 記錄或備份來源不符'
    );

    // HTTP 服務與 psql 必須屬於同一正在運作的健康容器網路及專用 volume。
    foreach (['db', 'laravel', 'flask'] as $service) {
        $id = t19Docker(['docker', 'compose', 'ps', '-q', $service]);
        t19Must($id !== '' && ! str_contains($id, "\n"), '服務未單獨啟動：' . $service);
        $inspection = json_decode(t19Docker(['docker', 'inspect', $id]), true, 512, JSON_THROW_ON_ERROR)[0] ?? [];
        $labels = $inspection['Config']['Labels'] ?? [];
        t19Must(
            ($labels['com.docker.compose.project'] ?? null) === 'ticket19_test'
            && ($labels['com.docker.compose.service'] ?? null) === $service
            && ($labels['com.docker.compose.project.working_dir'] ?? null) === $root
            && ($labels['com.docker.compose.project.config_files'] ?? null)
                === $root . '/compose.yaml,' . $root . '/ops/tests/ticket19_compose.yaml'
            && ($inspection['State']['Running'] ?? false) === true
            && ($inspection['State']['Health']['Status'] ?? null) === 'healthy'
            && isset($inspection['NetworkSettings']['Networks']['ticket19_test_default']),
            '實際容器身分不符：' . $service
        );
        $mounts = array_column($inspection['Mounts'] ?? [], null, 'Destination');
        if ($service === 'db') {
            $volume = $mounts['/var/lib/postgresql'] ?? [];
            t19Must(
                count($mounts) === 1 && ($volume['Type'] ?? null) === 'volume'
                && ($volume['Name'] ?? null) === 'ticket19_test_postgres_data',
                '實際資料庫 volume 不符'
            );
            continue;
        }
        $binding = $inspection['NetworkSettings']['Ports']['8000/tcp'][0] ?? [];
        $key = $mounts['/run/secrets/' . $service . '-jwt.key'] ?? [];
        t19Must(
            ($binding['HostIp'] ?? null) === '127.0.0.1'
            && (int) ($binding['HostPort'] ?? 0) === ($service === 'flask' ? 8085 : 8081)
            && ($key['Source'] ?? null) === $root . '/.local/ticket19-backend/' . $service . '.key'
            && ($key['RW'] ?? true) === false,
            '實際服務埠或金鑰不符：' . $service
        );
        if ($service === 'laravel') {
            t19Must(
                ($mounts['/srv/laravel/storage/logs']['Source'] ?? null)
                    === $root . '/.local/ticket19-backend/laravel-logs',
                '實際 Laravel 記錄掛載不符'
            );
        }
    }
}

// 對守衛自身注入錯誤專案，確認任何寫入前會拒絕錯誤目標。
$rejectedWrongTarget = false;
try {
    t19Guard('ticket19_wrong');
} catch (RuntimeException $error) {
    $rejectedWrongTarget = str_contains($error->getMessage(), '專用 Compose 設定');
}
t19Must($rejectedWrongTarget, '錯誤目標拒絕檢查失敗');
t19Guard((string) getenv('COMPOSE_PROJECT_NAME'));

// 保留原始 status、標頭與 JSON，讓兩個後端的可見契約可逐欄比較。
function t19Request(
    string $base,
    string $method,
    string $path,
    array|string|null $body = null,
    ?string $token = null
): array {
    $headers = ['Accept: application/json', 'Origin: http://localhost:8080'];
    if ($body !== null) {
        $headers[] = 'Content-Type: application/json';
    }
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer ' . $token;
    }
    $curl = curl_init($base . $path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $headers,
        CURLOPT_POSTFIELDS => $body === null ? '' : (is_array($body) ? json_encode($body, JSON_THROW_ON_ERROR) : $body),
        CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
    $raw = curl_exec($curl);
    t19Must($raw !== false, 'HTTP 請求失敗：' . curl_error($curl));
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    curl_close($curl);

    return [$status, json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
}

// 嚴格檢查共用成功或錯誤格式，避免只憑 HTTP 狀態宣稱相容。
function t19Check(array $result, int $status, ?string $code = null, ?array $errors = null): void
{
    $body = $result[1];
    t19Must($result[0] === $status && ($body['success'] ?? null) === ($code === null), 'HTTP 狀態或 success 不符');
    if ($code === null) {
        t19Must(($body['message'] ?? null) === 'OK' && isset($body['data']), '成功 envelope 不符');
        return;
    }
    $messages = [
        'E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API',
        'E0005' => '系統暫時無法處理，請稍後再試', 'E0101' => '帳號或密碼錯誤',
        'E0102' => '此帳號已停用，請聯絡系統管理員', 'E0104' => '後台認證設定異常，請聯絡系統管理員',
        'E0105' => '登入失敗次數過多，請稍後再試',
    ];
    t19Must(
        ($body['error_code'] ?? null) === $code && ($body['message'] ?? null) === $messages[$code]
        && ($body['errors'] ?? null) === $errors
        && preg_match(
            '/\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/',
            $body['log_id'] ?? ''
        ) === 1,
        '錯誤 envelope 不符：' . $code
    );
}

// SQL 僅經已核對的隔離 db 容器執行；查詢錯誤須中止而不能返回空結果。
function t19Sql(string $sql): string
{
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles',
            '-v', 'ON_ERROR_STOP=1', '-At'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    t19Must(is_resource($process), '無法啟動隔離 psql');
    fwrite($pipes[0], $sql);
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    t19Must(proc_close($process) === 0, '隔離 SQL 失敗：' . $error);

    return trim($output);
}

// 只以本輪產生的 Email 查 ID，不把前一次執行或其他測試資料當成 fixture。
function t19AccountId(string $email): int
{
    return (int) t19Sql("SELECT id FROM accounts WHERE email='" . str_replace("'", "''", $email) . "';");
}

// 從各後端真實登入取得憑證，並確認登入回應不使用可變期限。
function t19Login(string $base, string $email, string $password = 'Abc1234'): array
{
    $result = t19Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => $password]);
    t19Check($result, 200);
    t19Must(($result[1]['data']['token_type'] ?? null) === 'Bearer', '登入 token_type 不符');

    return $result[1]['data'];
}

// 兩個不同後端同時提交，檢查唯一鍵與鎖定交易的實際跨後端競爭。
function t19Parallel(string $path, array $bases, array $body): array
{
    $multi = curl_multi_init();
    $handles = [];
    foreach ($bases as $base) {
        $curl = curl_init($base . $path);
        curl_setopt_array($curl, [CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json',
                'Origin: http://localhost:8080'],
            CURLOPT_POSTFIELDS => json_encode($body, JSON_THROW_ON_ERROR),
            CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
        curl_multi_add_handle($multi, $curl);
        $handles[] = $curl;
    }
    do {
        $code = curl_multi_exec($multi, $running);
        t19Must($code === CURLM_OK, '並行 HTTP 執行失敗');
        if ($running) {
            curl_multi_select($multi, 1.0);
        }
    } while ($running);
    $responses = [];
    foreach ($handles as $curl) {
        $raw = curl_multi_getcontent($curl);
        t19Must($raw !== false && curl_errno($curl) === 0, '並行 HTTP 請求失敗');
        $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
        $responses[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
            json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);

    return $responses;
}

// 用專用金鑰重新簽指定 claims，驗證 Flask 不接受錯型別、期限與演算法。
function t19Resign(string $token, array $updates = [], ?array $headerOverride = null): string
{
    [$headerPart, $payloadPart] = explode('.', $token);
    $header = json_decode(base64_decode(strtr($headerPart, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
    $claims = json_decode(base64_decode(strtr($payloadPart, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
    $header = $headerOverride ?? $header;
    $claims = array_replace($claims, $updates);
    // 重簽只改指定欄位，保留原 session 身分以隔離 claims 驗證。
    $encode = static fn (array $value): string => rtrim(
        strtr(base64_encode(json_encode($value, JSON_THROW_ON_ERROR)), '+/', '-_'),
        '='
    );
    $signed = $encode($header) . '.' . $encode($claims);
    $key = base64_decode(trim((string) file_get_contents('.local/ticket19-backend/flask.key')), true);
    t19Must($key !== false && strlen($key) === 32, 'Flask 專用金鑰長度不符');

    return $signed . '.' . rtrim(strtr(base64_encode(hash_hmac('sha256', $signed, $key, true)), '+/', '-_'), '=');
}

// 逐頁取完列表，驗證整體順序、精確 total 與超頁行為。
function t19AllIds(string $base, string $path, ?string $token): array
{
    $ids = [];
    $page = 1;
    do {
        $separator = str_contains($path, '?') ? '&' : '?';
        $result = t19Request($base, 'GET', $path . $separator . 'page=' . $page . '&per_page=100', null, $token);
        t19Check($result, 200);
        $pagination = $result[1]['data']['pagination'];
        t19Must(
            $pagination['page'] === $page && $pagination['per_page'] === 100
            && $pagination['total_pages'] === (int) ceil($pagination['total'] / 100),
            '分頁欄位不符'
        );
        array_push($ids, ...array_column($result[1]['data']['list'], 'id'));
        $page++;
    } while ($page <= $pagination['total_pages']);
    t19Must(count($ids) === $pagination['total'], '列表完整筆數不符');

    return $ids;
}

// 每次執行用隨機前綴建立獨立帳號；同一資料庫可連跑而不需清空既有資料。
$flask = 'http://localhost:8085';
$laravel = 'http://localhost:8081';
$suffix = bin2hex(random_bytes(6));
$author = 'flask-author-' . $suffix . '@example.test';
$other = 'laravel-author-' . $suffix . '@example.test';
$adminEmail = 'flask-admin-' . $suffix . '@example.test';
$locked = 'flask-locked-' . $suffix . '@example.test';
$parallel = 'flask-parallel-' . $suffix . '@example.test';
$oldSettings = t19Sql(
    'SELECT enabled::int,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1;'
);
t19Must(preg_match('/\A[01]\|[0-9]+\|[0-9]+\|[0-9]+\z/', $oldSettings) === 1, '既有鎖定設定不完整');
[$oldEnabled, $oldWindow, $oldThreshold, $oldLock] = array_map('intval', explode('|', $oldSettings));
t19Sql(
    'UPDATE login_lockout_settings SET enabled=true,window_minutes=5,failure_threshold=5,lock_seconds=300 WHERE id=1;'
);

// 4／16 碼邊界可註冊及跨後端登入；3／17 碼和 NUL 暱稱不能增加任何帳號。
foreach ([$author, $adminEmail, $locked, $parallel] as $email) {
    t19Check(t19Request(
        $flask,
        'POST',
        '/api/auth/register',
        ['email' => $email, 'password' => 'Abc1234', 'nickname' => 'Flask 作者']
    ), 201);
}
t19Check(t19Request(
    $laravel,
    'POST',
    '/api/auth/register',
    ['email' => $other, 'password' => 'Abc1234', 'nickname' => 'Laravel 作者']
), 201);
t19Check(
    t19Request(
        $flask,
        'POST',
        '/api/auth/register',
        ['email' => '  ' . strtoupper($author) . '  ', 'password' => 'Abc1234', 'nickname' => '重複']
    ),
    422,
    'E0001',
    ['email' => ['此 Email 已註冊']]
);
$short = 'flask-short-' . $suffix . '@example.test';
$long = 'flask-long-' . $suffix . '@example.test';
foreach ([[$short, '1234'], [$long, 'Abcdefgh12345678']] as [$email, $password]) {
    t19Check(t19Request(
        $flask,
        'POST',
        '/api/auth/register',
        ['email' => $email, 'password' => $password, 'nickname' => '邊界']
    ), 201);
    t19Login($laravel, $email, $password);
}
foreach (['Abc', 'Abcdefgh123456789'] as $password) {
    $invalid = 'flask-invalid-' . $suffix . '-' . strlen($password) . '@example.test';
    t19Check(
        t19Request(
            $flask,
            'POST',
            '/api/auth/register',
            ['email' => $invalid, 'password' => $password, 'nickname' => '不應建立']
        ),
        422,
        'E0001',
        ['password' => ['欄位格式或範圍不正確']]
    );
    t19Must(t19AccountId($invalid) === 0, '無效密碼建立了帳號');
}
t19Check(
    t19Request(
        $flask,
        'POST',
        '/api/auth/register',
        ['email' => 'flask-nul-' . $suffix . '@example.test', 'password' => 'Abc1234', 'nickname' => "壞\0名"]
    ),
    422,
    'E0001',
    ['nickname' => ['欄位格式或範圍不正確']]
);
$race = 'flask-race-' . $suffix . '@example.test';
$raceResults = t19Parallel(
    '/api/auth/register',
    [$flask, $laravel],
    ['email' => $race, 'password' => 'Abc1234', 'nickname' => '並行註冊']
);
$raceStatuses = array_column($raceResults, 0);
sort($raceStatuses);
t19Must($raceStatuses === [201, 422], '跨後端重複註冊結果不符');
foreach ($raceResults as $result) {
    t19Check(
        $result,
        $result[0],
        $result[0] === 422 ? 'E0001' : null,
        $result[0] === 422 ? ['email' => ['此 Email 已註冊']] : null
    );
}
t19Must(t19Sql("SELECT count(*) FROM accounts WHERE email='$race';") === '1', '跨後端重複帳號持久化');
echo "PASS registration, normalization, password boundaries and cross-backend duplicate race\n";

// 驗證原生 Argon2id PHC 的成本與 salt 長度，且互登不重寫另一後端產生的雜湊。
$authorId = t19AccountId($author);
$otherId = t19AccountId($other);
$adminId = t19AccountId($adminEmail);
t19Must($authorId > 0 && $otherId > 0 && $adminId > 0, '帳號 fixture ID 缺漏');
$hashes = [];
foreach ([$authorId, $otherId] as $id) {
    $hash = t19Sql("SELECT password_hash FROM accounts WHERE id=$id;");
    $hashes[$id] = $hash;
    t19Must(preg_match(
        '/\A\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+\/]+)\$([A-Za-z0-9+\/]+)\z/',
        $hash,
        $matches
    ) === 1, 'Argon2id PHC 參數不符');
    t19Must(strlen((string) base64_decode($matches[1], true)) === 16
        && strlen((string) base64_decode($matches[2], true)) === 32, 'PHC salt 或輸出長度不符');
}
$otherFlask = t19Login($flask, $other);
$authorLaravel = t19Login($laravel, $author);
$authorFlask = t19Login($flask, $author);
foreach ([$authorId, $otherId] as $id) {
    $after = t19Sql("SELECT password_hash FROM accounts WHERE id=$id;");
    t19Must($after === $hashes[$id], '跨登入重寫了原有雜湊');
}

// Flask JWT 必須固定 HS256、獨立 issuer/audience、八小時與無敏感 claims。
$token = $authorFlask['access_token'];
[$headerPart, $claimsPart] = explode('.', $token);
$header = json_decode(base64_decode(strtr($headerPart, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
$claims = json_decode(base64_decode(strtr($claimsPart, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
t19Must($header === ['alg' => 'HS256', 'typ' => 'JWT'], 'JWT header 不符');
t19Must($claims['iss'] === 'ai-workflow-fullstack:flask'
    && $claims['aud'] === 'ai-workflow-fullstack-api:flask'
    && $claims['sub'] === (string) $authorId
    && is_int($claims['iat']) && $claims['nbf'] === $claims['iat']
    && $claims['exp'] === $claims['iat'] + 28800
    && preg_match('/\A[A-Za-z0-9_-]{22}\z/', $claims['jti']) === 1
    && strlen((string) base64_decode(strtr($claims['jti'], '-_', '+/'), true)) === 16
    && ! array_key_exists('email', $claims)
    && ! array_key_exists('role', $claims)
    && ! array_key_exists('password', $claims), 'JWT claims 不符');
$expectedExpiry = (new DateTimeImmutable('@' . $claims['exp']))
    ->setTimezone(new DateTimeZone('Asia/Taipei'))->format('Y-m-d H:i:s');
t19Must($authorFlask['expires_at'] === $expectedExpiry, '登入回應期限不等於 JWT exp');
$sessionCount = t19Sql("SELECT count(*) FROM login_sessions WHERE account_id=$authorId AND backend_id='flask';");
$me = t19Request($flask, 'GET', '/api/me', null, $token);
t19Check($me, 200);
t19Must(
    $me[1]['data']['id'] === $authorId && $me[1]['data']['email'] === $author
    && ! isset($me[1]['data']['access_token'], $me[1]['data']['expires_at'])
    && t19Sql("SELECT count(*) FROM login_sessions "
        . "WHERE account_id=$authorId AND backend_id='flask';") === $sessionCount,
    'GET /api/me 變更登入或回應'
);
foreach ([$flask => $authorLaravel['access_token'], $laravel => $token] as $base => $foreignToken) {
    t19Check(t19Request($base, 'GET', '/api/me', null, $foreignToken), 401, 'E0002');
}
$expiredIssued = time() - 30000;
foreach (
    [
    t19Resign($token, ['sub' => $authorId]),
    t19Resign($token, ['iss' => 'ai-workflow-fullstack:laravel']),
    t19Resign($token, ['aud' => ['ai-workflow-fullstack-api:flask']]),
    t19Resign($token, ['iat' => $expiredIssued, 'nbf' => $expiredIssued, 'exp' => $expiredIssued + 28800]),
    t19Resign($token, ['jti' => null]),
    t19Resign($token, [], ['alg' => 'none', 'typ' => 'JWT']),
    ] as $invalidToken
) {
    t19Check(t19Request($flask, 'GET', '/api/me', null, $invalidToken), 401, 'E0002');
}
t19Check(
    t19Request($flask, 'GET', '/api/articles?page=1&per_page=20', null, $authorLaravel['access_token']),
    401,
    'E0002'
);
t19Check(t19Request($flask, 'GET', '/api/articles?page=bad&per_page=20', null, 'invalid'), 401, 'E0002');
echo "PASS Argon2id cross-login, JWT boundary, claims and /me no renewal\n";

// 原始 Query 與 Body 分辨未知、重複、陣列、語法和方法錯誤，檢查精確欄位訊息。
foreach ([$flask, $laravel] as $base) {
    t19Check(
        t19Request($base, 'GET', '/api/articles?page=1&page=2&per_page=20'),
        422,
        'E0001',
        ['page' => ['此參數不可重複']]
    );
    t19Check(
        t19Request($base, 'GET', '/api/articles?page%5B%5D=1&per_page=20'),
        422,
        'E0001',
        ['page' => ['欄位型態不正確']]
    );
    t19Check(
        t19Request($base, 'GET', '/api/articles?page=1&per_page=20&foo=1'),
        422,
        'E0001',
        ['foo' => ['不支援此欄位']]
    );
    t19Check(
        t19Request($base, 'GET', '/api/articles?page=1&per_page=20', '{}'),
        422,
        'E0001',
        ['_body' => ['此 API 不接受 Request Body']]
    );
    t19Check(t19Request($base, 'POST', '/api/auth/register', '{'), 400, 'E0005');
    t19Check(t19Request($base, 'GET', '/api/not-a-route'), 404, 'E0004');
    t19Check(t19Request($base, 'DELETE', '/api/articles/1'), 405, 'E0005');
}
t19Check(
    t19Request($flask, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'),
    401,
    'E0002'
);
t19Check(t19Request(
    $flask,
    'GET',
    '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management',
    null,
    $token
), 403, 'E0003');
echo "PASS query/body/method errors and authorization order\n";

// 格式錯密碼不計數；預設第五次當次鎖定，並讓前端讀到 Retry-After。
$lockedId = t19AccountId($locked);
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $locked, 'password' => 'abc']
), 422, 'E0001', ['password' => ['欄位格式或範圍不正確']]);
t19Must(t19Sql("SELECT count(*) FROM login_failures WHERE account_id=$lockedId;") === '0', '格式錯誤增加鎖定計數');
for ($attempt = 1; $attempt < 5; $attempt++) {
    t19Check(t19Request(
        $flask,
        'POST',
        '/api/auth/login',
        ['email' => $locked, 'password' => 'Wrong123']
    ), 401, 'E0101');
}
$fifth = t19Request($flask, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Wrong123']);
t19Check($fifth, 429, 'E0105');
t19Must(
    preg_match('/\r?\nRetry-After: ([1-9][0-9]*)\r?\n/i', $fifth[2]) === 1
    && stripos($fifth[2], 'Access-Control-Expose-Headers: Retry-After') !== false
    && stripos($fifth[2], 'Access-Control-Allow-Origin: http://localhost:8080') !== false
    && t19Sql("SELECT count(*) FROM login_failures WHERE account_id=$lockedId;") === '5',
    '鎖定門檻、持久化或 CORS 標頭不符'
);
t19Check(t19Request(
    $laravel,
    'POST',
    '/api/auth/login',
    ['email' => $locked, 'password' => 'Abc1234']
), 429, 'E0105');
t19Sql("UPDATE accounts SET status='disabled' WHERE id=$lockedId;");
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $locked, 'password' => 'Abc1234']
), 403, 'E0102');
t19Sql("UPDATE accounts SET status='active' WHERE id=$lockedId;");
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $locked, 'password' => 'Abc1234']
), 429, 'E0105');

// 更改共用設定後，Laravel 與 Flask 的兩個錯密碼請求必須合計觸發同一次門檻。
t19Sql('UPDATE login_lockout_settings SET failure_threshold=2 WHERE id=1;');
$parallelResults = t19Parallel(
    '/api/auth/login',
    [$flask, $laravel],
    ['email' => $parallel, 'password' => 'Wrong123']
);
$statuses = array_column($parallelResults, 0);
sort($statuses);
t19Must($statuses === [401, 429], '跨後端鎖定結果未序列化');
foreach ($parallelResults as $result) {
    t19Check($result, $result[0], $result[0] === 429 ? 'E0105' : 'E0101');
}
$parallelId = t19AccountId($parallel);
t19Must(
    t19Sql("SELECT count(*) FROM login_failures WHERE account_id=$parallelId;") === '2'
    && t19Sql("SELECT (locked_until IS NOT NULL)::int FROM accounts WHERE id=$parallelId;") === '1',
    '跨後端鎖定未持久化'
);
t19Sql('UPDATE login_lockout_settings SET enabled=false WHERE id=1;');
t19Login($flask, $parallel);
t19Must(
    t19Sql("SELECT (locked_until IS NULL)::int FROM accounts WHERE id=$parallelId;") === '1',
    '關閉共用設定後未清除鎖定'
);
t19Sql('UPDATE login_lockout_settings SET enabled=true,failure_threshold=5 WHERE id=1;');

// 認證設定或雜湊毀損須安全失敗，不當成一般密碼錯誤累計。
$corrupt = 'flask-corrupt-' . $suffix . '@example.test';
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/register',
    ['email' => $corrupt, 'password' => 'Abc1234', 'nickname' => '雜湊測試']
), 201);
$corruptId = t19AccountId($corrupt);
t19Sql("UPDATE accounts SET password_hash='broken' WHERE id=$corruptId;");
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $corrupt, 'password' => 'Abc1234']
), 500, 'E0104');
t19Must(
    t19Sql("SELECT count(*) FROM login_failures WHERE account_id=$corruptId;") === '0',
    '毀損雜湊增加錯密碼計數'
);

// 縮短共用視窗時舊事件不計數；鎖定到期後成功登入切換計數世代。
$window = 'flask-window-' . $suffix . '@example.test';
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/register',
    ['email' => $window, 'password' => 'Abc1234', 'nickname' => '視窗測試']
), 201);
$windowId = t19AccountId($window);
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $window, 'password' => 'Wrong123']
), 401, 'E0101');
t19Sql("UPDATE login_failures SET occurred_at=clock_timestamp()-interval '2 minutes' WHERE account_id=$windowId;");
t19Sql('UPDATE login_lockout_settings SET window_minutes=1,failure_threshold=2 WHERE id=1;');
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $window, 'password' => 'Wrong123']
), 401, 'E0101');
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $window, 'password' => 'Wrong123']
), 429, 'E0105');
t19Sql("UPDATE accounts SET locked_until=clock_timestamp()-interval '1 second' WHERE id=$windowId;");
$epochBefore = (int) t19Sql("SELECT failure_epoch FROM accounts WHERE id=$windowId;");
t19Login($flask, $window);
t19Must(
    (int) t19Sql("SELECT failure_epoch FROM accounts WHERE id=$windowId;") === $epochBefore + 2,
    '鎖定到期清除與成功登入未各轉換計數世代'
);
t19Check(t19Request(
    $flask,
    'POST',
    '/api/auth/login',
    ['email' => $window, 'password' => 'Wrong123']
), 401, 'E0101');
t19Sql('UPDATE login_lockout_settings SET window_minutes=5,failure_threshold=5 WHERE id=1;');
echo "PASS default/shared lockout, concurrency and corrupt PHC\n";

// 登出只撤銷當前 Flask session；停用優先於已撤銷憑證，恢復後不復活舊憑證。
$secondToken = t19Login($flask, $author)['access_token'];
t19Check(t19Request($flask, 'POST', '/api/auth/logout', '{}', $token), 200);
t19Check(t19Request($flask, 'GET', '/api/me', null, $token), 401, 'E0002');
t19Check(t19Request($flask, 'GET', '/api/me', null, $secondToken), 200);
t19Sql("UPDATE accounts SET role='admin' WHERE id=$adminId;");
$adminFlask = t19Login($flask, $adminEmail)['access_token'];
$adminLaravel = t19Login($laravel, $adminEmail)['access_token'];
t19Check(t19Request(
    $laravel,
    'PUT',
    "/api/members/$authorId/status",
    ['status' => 'disabled'],
    $adminLaravel
), 200);
t19Check(t19Request($flask, 'GET', '/api/me', null, $secondToken), 403, 'E0102');
t19Check(t19Request($flask, 'GET', '/api/me', null, $token), 403, 'E0102');
t19Check(
    t19Request(
        $flask,
        'GET',
        '/api/me',
        null,
        t19Resign($token, ['iat' => $expiredIssued, 'nbf' => $expiredIssued,
        'exp' => $expiredIssued + 28800])
    ),
    401,
    'E0002'
);
t19Check(t19Request(
    $laravel,
    'PUT',
    "/api/members/$authorId/status",
    ['status' => 'active'],
    $adminLaravel
), 200);
t19Check(t19Request($flask, 'GET', '/api/me', null, $secondToken), 401, 'E0002');
$authorFlask = t19Login($flask, $author)['access_token'];
$authorLaravel = t19Login($laravel, $author)['access_token'];
echo "PASS logout, independent session and disable/re-enable priority\n";

// Laravel 真實 HTTP 寫入三篇文章，Flask 從共用資料庫讀取；未來時間只調整本輪 ID 以固定順序。
$visibleBefore = (int) t19Sql("SELECT count(*) FROM articles WHERE visibility='visible';");
$allBefore = (int) t19Sql('SELECT count(*) FROM articles;');
$articleIds = [];
for ($index = 1; $index <= 3; $index++) {
    $title = "Ticket19 $suffix $index";
    t19Check(t19Request(
        $laravel,
        'POST',
        '/api/articles',
        ['title' => $title, 'body' => "文章內容 $index"],
        $authorLaravel
    ), 201);
    $articleIds[] = (int) t19Sql("SELECT id FROM articles WHERE author_id=$authorId AND title='$title';");
}
[$a1, $a2, $a3] = $articleIds;
t19Must($a1 > 0 && $a2 > $a1 && $a3 > $a2, 'Laravel 文章 fixture 未建立');
t19Sql("UPDATE articles SET created_at='2100-01-01 00:00:00+00' WHERE id IN ($a1,$a2,$a3);");
$public = t19Request($flask, 'GET', '/api/articles?page=1&per_page=2');
t19Check($public, 200);
t19Must(
    array_column($public[1]['data']['list'], 'id') === [$a3, $a2]
    && $public[1]['data']['pagination'] === ['page' => 1, 'per_page' => 2,
        'total' => $visibleBefore + 3, 'total_pages' => (int) ceil(($visibleBefore + 3) / 2)]
    && $public[1]['data']['list'][0]['author'] === ['id' => $authorId, 'nickname' => 'Flask 作者']
    && ! isset($public[1]['data']['list'][0]['author']['email'])
    && $public[1]['data']['list'][0]['created_backend'] === 'laravel',
    '公開文章順序、筆數或作者隱私不符'
);
$secondPage = t19Request($flask, 'GET', '/api/articles?page=2&per_page=2');
t19Check($secondPage, 200);
t19Must(($secondPage[1]['data']['list'][0]['id'] ?? null) === $a1, '文章跨頁順序不符');
$beyond = t19Request($flask, 'GET', '/api/articles?page=9007199254740991&per_page=20');
t19Check($beyond, 200);
t19Must($beyond[1]['data']['list'] === []
    && $beyond[1]['data']['pagination']['total'] === $visibleBefore + 3, '文章超頁結果不符');
t19Check(t19Request($flask, 'GET', "/api/articles/$a3"), 200);
t19Check(t19Request($flask, 'GET', "/api/articles/$a3", null, $otherFlask['access_token']), 200);

// 留言一筆個別下架後，訪客／作者只見兩筆，管理員仍見三筆。
$commentIds = [];
for ($index = 1; $index <= 3; $index++) {
    $body = "Ticket19 $suffix 留言 $index";
    t19Check(t19Request($laravel, 'POST', "/api/articles/$a3/comments", ['body' => $body], $authorLaravel), 201);
    $commentIds[] = (int) t19Sql("SELECT id FROM comments WHERE article_id=$a3 AND body='$body';");
}
[$c1, $c2, $c3] = $commentIds;
t19Must($c1 > 0 && $c2 > $c1 && $c3 > $c2, 'Laravel 留言 fixture 未建立');
t19Sql("UPDATE comments SET created_at='2100-01-01 00:00:00+00' WHERE id IN ($c1,$c2,$c3);");
t19Check(t19Request(
    $laravel,
    'PUT',
    "/api/comments/$c2/visibility",
    ['visibility' => 'hidden', 'version' => 1],
    $authorLaravel
), 200);
$publicComments = t19Request($flask, 'GET', "/api/articles/$a3/comments?page=1&per_page=2");
t19Check($publicComments, 200);
t19Must(
    array_column($publicComments[1]['data']['list'], 'id') === [$c3, $c1]
    && $publicComments[1]['data']['pagination']['total'] === 2
    && $publicComments[1]['data']['list'][0]['author'] === ['id' => $authorId, 'nickname' => 'Flask 作者'],
    '公開留言順序、筆數或作者隱私不符'
);
$adminComments = t19Request($flask, 'GET', "/api/articles/$a3/comments?page=1&per_page=3", null, $adminFlask);
t19Check($adminComments, 200);
t19Must(array_column($adminComments[1]['data']['list'], 'id') === [$c3, $c2, $c1]
    && $adminComments[1]['data']['pagination']['total'] === 3, '管理員留言範圍不符');
$commentBeyond = t19Request($flask, 'GET', "/api/articles/$a3/comments?page=999&per_page=20");
t19Check($commentBeyond, 200);
t19Must($commentBeyond[1]['data']['list'] === []
    && $commentBeyond[1]['data']['pagination']['total'] === 2, '留言超頁結果不符');

// 父文章下架後，訪客與非作者不可藉留言端點探知內容；作者可預覽，管理員可見個別下架留言。
t19Check(t19Request(
    $laravel,
    'PUT',
    "/api/articles/$a3/visibility",
    ['visibility' => 'hidden', 'version' => 1],
    $authorLaravel
), 200);
foreach ([null, $otherFlask['access_token']] as $viewer) {
    t19Check(t19Request($flask, 'GET', "/api/articles/$a3", null, $viewer), 404, 'E0004');
    t19Check(
        t19Request($flask, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $viewer),
        404,
        'E0004'
    );
}
$authorPreview = t19Request($flask, 'GET', "/api/articles/$a3", null, $authorFlask);
t19Check($authorPreview, 200);
t19Must($authorPreview[1]['data']['visibility'] === 'hidden'
    && $authorPreview[1]['data']['can_restore'] === true, '作者下架預覽不符');
$authorComments = t19Request($flask, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $authorFlask);
t19Check($authorComments, 200);
t19Must(array_column($authorComments[1]['data']['list'], 'id') === [$c3, $c1], '作者預覽洩漏個別下架留言');
$adminHidden = t19Request($flask, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $adminFlask);
t19Check($adminHidden, 200);
t19Must(array_column($adminHidden[1]['data']['list'], 'id') === [$c3, $c2, $c1], '管理員下架父文章留言範圍不符');
$management = t19Request(
    $flask,
    'GET',
    '/api/articles?page=1&per_page=2&filters%5Bscope%5D=management',
    null,
    $adminFlask
);
t19Check($management, 200);
t19Must(array_column($management[1]['data']['list'], 'id') === [$a3, $a2]
    && $management[1]['data']['pagination']['total'] === $allBefore + 3, '管理文章範圍或筆數不符');
$publicAfter = t19Request($flask, 'GET', '/api/articles?page=1&per_page=2');
t19Check($publicAfter, 200);
t19Must(
    array_column($publicAfter[1]['data']['list'], 'id') === [$a2, $a1]
    && $publicAfter[1]['data']['pagination']['total'] === $visibleBefore + 2,
    '下架文章出現在公開列表'
);
echo "PASS shared article/comment visibility, pagination and privacy\n";

// 只替本輪作者與文章加入代表性 SQL 測資，讓計畫反映非空列表與精確計數。
$articleSeed = t19Sql("INSERT INTO articles
    (author_id,title,body,visibility,restore_control,version,created_backend,created_at,updated_at)
    SELECT $authorId,'Ticket19 plan $suffix '||n,'計畫文章','visible',NULL,1,'laravel',
    '2024-01-01 00:00:00+00'::timestamptz+n*interval '1 second',
    '2024-01-01 00:00:00+00'::timestamptz+n*interval '1 second'
    FROM generate_series(1,300) AS n;");
$commentSeed = t19Sql("INSERT INTO comments
    (author_id,article_id,body,visibility,restore_control,version,created_backend,created_at,updated_at)
    SELECT $authorId,$a3,'Ticket19 plan $suffix '||n,'visible',NULL,1,'laravel',
    '2024-01-01 00:00:00+00'::timestamptz+n*interval '1 second',
    '2024-01-01 00:00:00+00'::timestamptz+n*interval '1 second'
    FROM generate_series(1,300) AS n;");
t19Must($articleSeed === 'INSERT 0 300' && $commentSeed === 'INSERT 0 300', '代表性 SQL 測資未完整建立');
t19Sql('ANALYZE articles; ANALYZE comments;');

// 以資料庫完整排序 ID 當真值，兩後端皆逐頁讀盡並檢查 total，避免只看首頁包含 fixture。
$publicIds = t19Sql("SELECT string_agg(id::text,',' ORDER BY created_at DESC,id DESC)
    FROM articles WHERE visibility='visible';");
$allArticleIds = t19Sql("SELECT string_agg(id::text,',' ORDER BY created_at DESC,id DESC) FROM articles;");
$visibleCommentIds = t19Sql("SELECT string_agg(id::text,',' ORDER BY created_at DESC,id DESC)
    FROM comments WHERE article_id=$a3 AND visibility='visible';");
$allCommentIds = t19Sql("SELECT string_agg(id::text,',' ORDER BY created_at DESC,id DESC)
    FROM comments WHERE article_id=$a3;");
$expectedPublic = $publicIds === '' ? [] : array_map('intval', explode(',', $publicIds));
$expectedArticles = $allArticleIds === '' ? [] : array_map('intval', explode(',', $allArticleIds));
$expectedComments = $visibleCommentIds === '' ? [] : array_map('intval', explode(',', $visibleCommentIds));
$expectedAdminComments = $allCommentIds === '' ? [] : array_map('intval', explode(',', $allCommentIds));
t19Must(
    ! in_array($a3, $expectedPublic, true) && in_array($a3, $expectedArticles, true)
    && ! in_array($c2, $expectedComments, true) && in_array($c2, $expectedAdminComments, true),
    '下架 fixture 未符合清單前提'
);
foreach ([$flask, $laravel] as $base) {
    $adminToken = $base === $flask ? $adminFlask : $adminLaravel;
    $authorToken = $base === $flask ? $authorFlask : $authorLaravel;
    t19Must(
        t19AllIds($base, '/api/articles', null) === $expectedPublic,
        '公開文章完整列表不符'
    );
    t19Must(t19AllIds($base, '/api/articles?filters%5Bscope%5D=management', $adminToken)
        === $expectedArticles, '管理文章完整列表不符');
    t19Must(
        t19AllIds($base, "/api/articles/$a3/comments", $authorToken) === $expectedComments,
        '作者留言完整列表不符'
    );
    t19Must(
        t19AllIds($base, "/api/articles/$a3/comments", $adminToken) === $expectedAdminComments,
        '管理員留言完整列表不符'
    );
}

// 保留 Flask 控制器實際 SELECT/count 形狀的完整 JSON 計畫，供索引與耗時審閱。
$articleColumns = 'articles.id,articles.author_id,articles.title,articles.body,articles.visibility,'
    . 'articles.restore_control,articles.version,articles.created_backend,articles.created_at,'
    . 'articles.updated_at,accounts.nickname';
$commentColumns = 'comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,'
    . 'comments.restore_control,comments.version,comments.created_backend,comments.created_at,'
    . 'comments.updated_at,accounts.nickname';
$articleJoin = 'FROM articles JOIN accounts ON accounts.id=articles.author_id';
$commentJoin = 'FROM comments JOIN accounts ON accounts.id=comments.author_id';
$plans = [
    'public articles page' => "SELECT $articleColumns $articleJoin WHERE articles.visibility='visible' "
        . 'ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0',
    'public articles count' => "SELECT count(*) $articleJoin WHERE articles.visibility='visible'",
    'management articles page' => "SELECT $articleColumns $articleJoin "
        . 'ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0',
    'management articles count' => "SELECT count(*) $articleJoin",
    'public comments page' => "SELECT $commentColumns $commentJoin "
        . "WHERE comments.article_id=$a3 AND comments.visibility='visible' "
        . 'ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0',
    'public comments count' => "SELECT count(*) $commentJoin "
        . "WHERE comments.article_id=$a3 AND comments.visibility='visible'",
    'management comments page' => "SELECT $commentColumns $commentJoin WHERE comments.article_id=$a3 "
        . 'ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0',
    'management comments count' => "SELECT count(*) $commentJoin WHERE comments.article_id=$a3",
];
echo "Ticket 19 plan fixture: author_id=$authorId article_id=$a3 article_rows=300 comment_rows=300\n";
foreach ($plans as $label => $query) {
    $plan = t19Sql('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' . $query . ';');
    $parsed = json_decode($plan, true, 512, JSON_THROW_ON_ERROR);
    t19Must(
        isset($parsed[0]['Plan']['Node Type'], $parsed[0]['Execution Time']),
        'SQL 計畫格式不完整：' . $label
    );
    echo $label . ":\n" . $plan . "\n";
}

// 完成後還原隔離資料庫原有共用設定；每次執行均保留自己的測資供重跑核對。
t19Sql("UPDATE login_lockout_settings SET enabled=" . ($oldEnabled ? 'true' : 'false')
    . ",window_minutes=$oldWindow,failure_threshold=$oldThreshold,lock_seconds=$oldLock WHERE id=1;");
echo "Ticket 19 Flask HTTP/SQL parity: PASS\n";
