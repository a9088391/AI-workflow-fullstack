<?php

declare(strict_types=1);

// Ticket 16 僅用專用隔離設定；Ticket 18 須先通過其容器守衛才重用本測試，避免污染展示資料。
if (
    ! ($GLOBALS['t18VerifiedTarget'] ?? false) && (
        ! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket16_')
        || ! str_contains(getenv('COMPOSE_FILE') ?: '', 'ops/tests/ticket16_compose.yaml')
    )
) {
    throw new RuntimeException('必須使用 ticket16_ 隔離 Compose 與 ticket16_compose.yaml');
}
$django = ($GLOBALS['t18VerifiedTarget'] ?? false) ? 'http://localhost:19384' : 'http://localhost:18084';
$laravel = ($GLOBALS['t18VerifiedTarget'] ?? false) ? 'http://localhost:19381' : 'http://localhost:18081';

/** 保留真實 HTTP status、Body、Headers，讓同一案例可比較不同後端契約。 */
function t16Request(string $base, string $method, string $path, array|string|null $body = null, ?string $token = null): array
{
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
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

    return [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
}

/** 逐字檢查安全錯誤 envelope 與 log_id，不把憑證或密碼輸出至測試記錄。 */
function t16Check(array $result, int $status, ?string $code = null, ?array $errors = null): void
{
    if ($result[0] !== $status || $result[1]['success'] !== ($code === null)) {
        throw new RuntimeException('HTTP 狀態或 success 不符：' . json_encode([$result[0], $result[1]['error_code'] ?? null]));
    }
    if ($code === null) {
        assert($result[1]['message'] === 'OK');
        assert(is_array($result[1]['data']));
        return;
    }
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API',
        'E0005' => '系統暫時無法處理，請稍後再試', 'E0101' => '帳號或密碼錯誤',
        'E0102' => '此帳號已停用，請聯絡系統管理員', 'E0104' => '後台認證設定異常，請聯絡系統管理員',
        'E0105' => '登入失敗次數過多，請稍後再試',
        'E9999' => '系統暫時無法處理，請稍後再試'];
    assert($result[1]['error_code'] === $code);
    assert($result[1]['message'] === $messages[$code]);
    assert(($result[1]['errors'] ?? null) === $errors);
    assert(preg_match('/\A[0-9a-f-]{36}\z/', $result[1]['log_id']) === 1);
}

/** 只在同一隔離 Compose 資料庫建立 fixture 與讀取副作用，絕不連展示 volume。 */
function t16Sql(string $sql): string
{
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('無法啟動隔離 psql');
    }
    fwrite($pipes[0], $sql);
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 SQL 失敗：' . $error);
    }
    return trim($output);
}

/** 只控制已通過前置守衛的隔離 Compose 服務，並回報命令失敗或安全輸出。 */
function t16Docker(array $arguments): string
{
    $process = proc_open(
        ['docker', 'compose', ...$arguments],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('無法控制隔離 Compose');
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 Compose 失敗：' . $output . $error);
    }
    return $output;
}

/** 依唯一 Email 取新 fixture ID，避免把其他執行留下的資料當成自己的資料。 */
function t16AccountId(string $email): int
{
    return (int) t16Sql("SELECT id FROM accounts WHERE email='" . str_replace("'", "''", $email) . "';");
}

/** 取得核准後端的 token，只驗證公開 JSON 欄位，不依賴 JWT 私有表示。 */
function t16Login(string $base, string $email): string
{
    $result = t16Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
    t16Check($result, 200);
    assert($result[1]['data']['token_type'] === 'Bearer');
    return $result[1]['data']['access_token'];
}

/** 同時送往兩個後端，確認設定列鎖使同帳號失敗計數與門檻回應序列化。 */
function t16ParallelBadLogins(array $bases, string $email): array
{
    $multi = curl_multi_init();
    $handles = [];
    foreach ($bases as $base) {
        $curl = curl_init($base . '/api/auth/login');
        curl_setopt_array($curl, [CURLOPT_POST => true, CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => json_encode(['email' => $email, 'password' => 'Wrong123'], JSON_THROW_ON_ERROR),
            CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
        curl_multi_add_handle($multi, $curl);
        $handles[] = $curl;
    }
    do {
        $result = curl_multi_exec($multi, $running);
        if ($result !== CURLM_OK) {
            throw new RuntimeException('並行登入 multi 執行失敗');
        }
        if ($running) {
            curl_multi_select($multi, 1.0);
        }
    } while ($running);
    $responses = [];
    foreach ($handles as $curl) {
        $raw = curl_multi_getcontent($curl);
        if ($raw === false || curl_errno($curl) !== 0) {
            throw new RuntimeException('並行登入 HTTP 失敗：' . curl_error($curl));
        }
        $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
        $responses[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);
    return $responses;
}

/** 讓 Django 與 Laravel 同時註冊同一 Email，由共用唯一索引裁決唯一勝者。 */
function t16ParallelRegisters(array $bases, string $email): array
{
    $multi = curl_multi_init();
    $handles = [];
    foreach ($bases as $base) {
        $curl = curl_init($base . '/api/auth/register');
        curl_setopt_array($curl, [CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => json_encode(['email' => $email, 'password' => 'Abc1234', 'nickname' => '並行註冊'], JSON_THROW_ON_ERROR),
            CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
        curl_multi_add_handle($multi, $curl);
        $handles[] = $curl;
    }
    do {
        $result = curl_multi_exec($multi, $running);
        if ($result !== CURLM_OK) {
            throw new RuntimeException('並行註冊 multi 執行失敗');
        }
        if ($running) {
            curl_multi_select($multi, 1.0);
        }
    } while ($running);
    $responses = [];
    foreach ($handles as $curl) {
        $raw = curl_multi_getcontent($curl);
        if ($raw === false || curl_errno($curl) !== 0) {
            throw new RuntimeException('並行註冊 HTTP 失敗：' . curl_error($curl));
        }
        $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
        $responses[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);
    return $responses;
}

/** 用隔離金鑰重簽指定 claims，驗證 Django 的嚴格 JWT 期限與型態。 */
function t16TokenWithClaims(string $token, array $updates): string
{
    [$header, $payload] = explode('.', $token);
    $claims = json_decode(base64_decode(strtr($payload, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
    $claims = array_replace($claims, $updates);
    $payload = rtrim(strtr(base64_encode(json_encode($claims, JSON_THROW_ON_ERROR)), '+/', '-_'), '=');
    $signed = $header . '.' . $payload;
    $keyPath = ($GLOBALS['t18VerifiedTarget'] ?? false)
        ? '.local/ticket18-backend/django.key' : '.local/ticket16-backend/django.key';
    $key = base64_decode(trim(file_get_contents($keyPath)), true);
    assert($key !== false && strlen($key) === 32);
    return $signed . '.' . rtrim(strtr(base64_encode(hash_hmac('sha256', $signed, $key, true)), '+/', '-_'), '=');
}

/** 直接讀健康檢查狀態，確認 Compose 不會把失聯資料庫宣稱就緒。 */
function t16Health(string $base): int
{
    $curl = curl_init($base . '/up');
    curl_setopt_array($curl, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20]);
    if (curl_exec($curl) === false) {
        throw new RuntimeException('健康檢查 HTTP 失敗：' . curl_error($curl));
    }
    return curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
}

// 先固定隔離專案的設定，再用唯一名稱建立可重跑的帳號與文章。
t16Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE id=1;');
$suffix = bin2hex(random_bytes(5));
$author = 'django-' . $suffix . '@example.test';
$other = 'laravel-' . $suffix . '@example.test';
$admin = 'admin-' . $suffix . '@example.test';
$locked = 'locked-' . $suffix . '@example.test';
$parallel = 'parallel-' . $suffix . '@example.test';
$visibleBefore = (int) t16Sql("SELECT count(*) FROM articles WHERE visibility='visible';");
$allBefore = (int) t16Sql('SELECT count(*) FROM articles;');
// URL 整數可有大量前導零；列表須使用已驗證的數值，不能再解析原始長字串。
$zeroPrefix = str_repeat('0', 4300);
$longPage = t16Request($django, 'GET', '/api/articles?page=' . $zeroPrefix . '1&per_page=20');
t16Check($longPage, 200);
assert($longPage[1]['data']['pagination']['page'] === 1);
foreach ([$author, $admin, $locked, $parallel] as $email) {
    t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $email, 'password' => 'Abc1234', 'nickname' => 'Django 作者']), 201);
}
t16Check(t16Request($laravel, 'POST', '/api/auth/register', ['email' => $other, 'password' => 'Abc1234', 'nickname' => 'Laravel 作者']), 201);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => strtoupper($author), 'password' => 'Abc1234', 'nickname' => '重複']), 422, 'E0001', ['email' => ['此 Email 已註冊']]);
// 驗證 4／16 字元邊界、U+0000 及兩個不同後端的同 Email 並行。
$short = 'short-' . $suffix . '@example.test';
$long = 'long-' . $suffix . '@example.test';
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $short, 'password' => 'Abc1', 'nickname' => '短密碼']), 201);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $long, 'password' => 'Abcdefgh12345678', 'nickname' => '長密碼']), 201);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $short, 'password' => 'Abc1']), 200);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $long, 'password' => 'Abcdefgh12345678']), 200);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => 'nul-' . $suffix . '@example.test', 'password' => 'Abc1234', 'nickname' => "壞\0名"]), 422, 'E0001', ['nickname' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => 'invalid-' . $suffix . '@example.test', 'password' => 'Abcdefgh123456789', 'nickname' => '長密碼']), 422, 'E0001', ['password' => ['欄位格式或範圍不正確']]);
$race = 'race-' . $suffix . '@example.test';
$raceResults = t16ParallelRegisters([$django, $laravel], $race);
$raceStatuses = array_column($raceResults, 0);
sort($raceStatuses);
assert($raceStatuses === [201, 422]);
assert(t16Sql("SELECT count(*) FROM accounts WHERE email='$race';") === '1');
foreach ($raceResults as $result) {
    t16Check($result, $result[0], $result[0] === 422 ? 'E0001' : null, $result[0] === 422 ? ['email' => ['此 Email 已註冊']] : null);
}
// 非 ASCII Kelvin sign 不得摺為 k；同形 Email 的登入格式錯誤也不能增加既有帳號失敗次數。
$asciiTarget = 'kprobe' . $suffix . '@example.test';
$kelvinEmail = "\u{212A}probe" . $suffix . '@example.test';
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $kelvinEmail, 'password' => 'Abc1234', 'nickname' => '不應建立']), 422, 'E0001', ['email' => ['欄位格式或範圍不正確']]);
assert(t16AccountId($asciiTarget) === 0);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $asciiTarget, 'password' => 'Abc1234', 'nickname' => 'ASCII 目標']), 201);
$asciiTargetId = t16AccountId($asciiTarget);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $kelvinEmail, 'password' => 'Wrong123']), 422, 'E0001', ['email' => ['欄位格式或範圍不正確']]);
assert((int) t16Sql("SELECT count(*) FROM login_failures WHERE account_id=$asciiTargetId;") === 0);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => 'x@example.test', 'password' => '123', 'nickname' => '合法']), 422, 'E0001', ['password' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'POST', '/api/auth/register', '{'), 400, 'E0005');
t16Check(t16Request($django, 'POST', '/api/auth/register', '{"email":NaN,"password":"Abc1234","nickname":"壞"}'), 400, 'E0005');
t16Check(t16Request($django, 'POST', '/api/auth/register', '{"email":"x@example.test","password":"Abc1234","nickname":"\\ud800"}'), 400, 'E0005');
// PHP json_decode 只接受 UTF-8；UTF-16／32 即使可解成欄位也須以解析錯誤拒絕且不得建立帳號。
foreach (['UTF-16LE' => "\xff\xfe", 'UTF-32LE' => "\xff\xfe\x00\x00"] as $encoding => $bom) {
    $encodedEmail = strtolower($encoding) . '-' . $suffix . '@example.test';
    $json = json_encode(['email' => $encodedEmail, 'password' => 'Abc1234', 'nickname' => '編碼測試'], JSON_THROW_ON_ERROR);
    $converted = iconv('UTF-8', $encoding, $json);
    assert($converted !== false);
    $encodedBody = $bom . $converted;
    t16Check(t16Request($django, 'POST', '/api/auth/register', $encodedBody), 400, 'E0005');
    assert(t16AccountId($encodedEmail) === 0);
}
// PHP 預設深度 512：根 object 加 510 層 array 可解析，511 層為 400；更深也不能變 500。
$deepEmail = 'deep-' . $suffix . '@example.test';
$deepPrefix = json_encode(['email' => $deepEmail, 'password' => 'Abc1234', 'nickname' => '深度測試'], JSON_THROW_ON_ERROR);
$deepPrefix = substr($deepPrefix, 0, -1) . ',"unexpected":';
$withinDepth = $deepPrefix . str_repeat('[', 510) . '0' . str_repeat(']', 510) . '}';
t16Check(t16Request($django, 'POST', '/api/auth/register', $withinDepth), 422, 'E0001', ['unexpected' => ['不支援此欄位']]);
$overDepth = $deepPrefix . str_repeat('[', 511) . '0' . str_repeat(']', 511) . '}';
t16Check(t16Request($django, 'POST', '/api/auth/register', $overDepth), 400, 'E0005');
$recursiveBody = str_repeat('[', 10000) . '0' . str_repeat(']', 10000);
t16Check(t16Request($django, 'POST', '/api/auth/register', $recursiveBody), 400, 'E0005');
assert(t16AccountId($deepEmail) === 0);
// 引號內的大量括號與跳脫引號不增加 JSON 深度；正常欄位驗證仍回 422。
$quotedEmail = 'quoted-depth-' . $suffix . '@example.test';
$quotedBody = json_encode([
    'email' => $quotedEmail, 'password' => 'Abc1234', 'nickname' => 'Reader',
    'unexpected' => str_repeat('[', 600) . '"' . str_repeat(']', 600),
], JSON_THROW_ON_ERROR);
t16Check(t16Request($django, 'POST', '/api/auth/register', $quotedBody), 422, 'E0001', ['unexpected' => ['不支援此欄位']]);
assert(t16AccountId($quotedEmail) === 0);
// 被後一個同名鍵覆蓋的深層舊值仍須觸發 PHP 相同的 400，不得建立帳號。
$duplicateDeepEmail = 'duplicate-depth-' . $suffix . '@example.test';
$duplicatePrefix = json_encode(['email' => $duplicateDeepEmail, 'password' => 'Abc1234'], JSON_THROW_ON_ERROR);
$duplicatePrefix = substr($duplicatePrefix, 0, -1) . ',"nickname":';
$duplicateBody = $duplicatePrefix . str_repeat('[', 511) . '0' . str_repeat(']', 511) . ',"nickname":"Reader"}';
t16Check(t16Request($django, 'POST', '/api/auth/register', $duplicateBody), 400, 'E0005');
assert(t16AccountId($duplicateDeepEmail) === 0);
// 合法淺層重複鍵仍採最後一值，確認深度預掃沒有改變原本的 JSON 解析語意。
$duplicateShallowEmail = 'duplicate-shallow-' . $suffix . '@example.test';
$duplicateShallowBody = '{"email":"' . $duplicateShallowEmail
    . '","password":"Abc1234","nickname":null,"nickname":"Reader"}';
t16Check(t16Request($django, 'POST', '/api/auth/register', $duplicateShallowBody), 201);
assert(t16AccountId($duplicateShallowEmail) > 0);
t16Check(t16Request($django, 'POST', '/api/auth/register', '[]'), 422, 'E0001', ['_body' => ['Request Body 必須為 JSON object']]);
t16Check(t16Request($django, 'POST', '/api/auth/login'), 422, 'E0001', ['_body' => ['Request Body 必須為 JSON object']]);
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => null, 'password' => 'Abc1234', 'nickname' => '合法']), 422, 'E0001', ['email' => ['此欄位不可為 null']]);
t16Check(t16Request($django, 'POST', '/api/auth/register?foo=1', ['email' => 'x@example.test', 'password' => 'Abc1234', 'nickname' => '合法']), 422, 'E0001', ['foo' => ['不支援此欄位']]);
$invalidQuery = t16Request($django, 'POST', '/api/auth/register?%FF=1', '{}');
t16Check($invalidQuery, 422, 'E0001', ['%FF' => ['不支援此欄位']]);

// 預檢只允許已核准的同源前端、方法與標頭。
$preflight = curl_init($django . '/api/auth/login');
curl_setopt_array($preflight, [CURLOPT_CUSTOMREQUEST => 'OPTIONS', CURLOPT_HTTPHEADER => [
    'Origin: http://localhost:8080', 'Access-Control-Request-Method: POST', 'Access-Control-Request-Headers: authorization,content-type'],
    CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 20]);
$preflightResponse = curl_exec($preflight);
assert($preflightResponse !== false && curl_getinfo($preflight, CURLINFO_RESPONSE_CODE) === 204);
assert(stripos($preflightResponse, 'Access-Control-Allow-Origin: http://localhost:8080') !== false);
assert(stripos($preflightResponse, 'Access-Control-Allow-Headers:') !== false);

// 跨語言 Argon2id：Django 能驗 Laravel 雜湊，Laravel 能驗 Django 建立的 PHC。
$otherToken = t16Login($django, $other);
$authorLaravelToken = t16Login($laravel, $author);
$authorToken = t16Login($django, $author);
t16Check(t16Request($django, 'GET', '/api/me', null, $authorToken), 200);
// token 只帶固定 issuer/audience、整數時間與單次 jti；重簽過期或錯型別也拒絕。
[$jwtHeader, $jwtPayload] = explode('.', $authorToken);
$headerValues = json_decode(base64_decode(strtr($jwtHeader, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
$claims = json_decode(base64_decode(strtr($jwtPayload, '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
assert($headerValues === ['alg' => 'HS256', 'typ' => 'JWT']);
assert($claims['iss'] === 'ai-workflow-fullstack:django' && $claims['aud'] === 'ai-workflow-fullstack-api:django');
assert(is_int($claims['iat']) && $claims['nbf'] === $claims['iat'] && $claims['exp'] === $claims['iat'] + 28800);
assert(strlen(base64_decode(strtr($claims['jti'], '-_', '+/'))) === 16);
assert(! array_key_exists('email', $claims) && ! array_key_exists('role', $claims));
// 共用資料表先拒絕半秒登入紀錄；即使驗證失敗也由交易回滾，不污染隔離 fixture。
assert(preg_match('/^[A-Za-z0-9_-]{22}$/', $claims['jti']) === 1);
$sessionJti = $claims['jti'];
foreach (['issued_at', 'expires_at'] as $timeColumn) {
    $rejected = false;
    try {
        t16Sql(
            "BEGIN; UPDATE login_sessions SET $timeColumn=$timeColumn+interval '500 milliseconds' "
            . "WHERE jti='$sessionJti'; ROLLBACK;"
        );
    } catch (RuntimeException $error) {
        $rejected = str_contains($error->getMessage(), 'violates check constraint');
    }
    assert($rejected);
}
t16Check(t16Request($django, 'GET', '/api/me', null, $authorToken), 200);
$expiredIssued = time() - 30000;
t16Check(t16Request($django, 'GET', '/api/me', null, t16TokenWithClaims($authorToken, ['iat' => $expiredIssued, 'nbf' => $expiredIssued, 'exp' => $expiredIssued + 28800])), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/me', null, t16TokenWithClaims($authorToken, ['sub' => (int) $claims['sub']])), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/me', null, 'x' . substr($authorToken, 1)), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/articles?page=1&per_page=20', null, $authorLaravelToken), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/articles?page=bad&per_page=20', null, 'invalid'), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', null, $authorToken), 403, 'E0003');
t16Check(t16Request($django, 'GET', '/api/articles?page=bad&per_page=20&filters%5Bscope%5D=management', null, $authorToken), 403, 'E0003');
t16Check(t16Request($django, 'GET', '/api/me?foo=1'), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/articles?page=1&page=2&per_page=20'), 422, 'E0001', ['page' => ['此參數不可重複']]);
t16Check(t16Request($django, 'GET', '/api/articles?page%5B%5D=1&per_page=20'), 422, 'E0001', ['page' => ['欄位型態不正確']]);
t16Check(t16Request($django, 'GET', '/api/articles?page=0&per_page=20'), 422, 'E0001', ['page' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'GET', '/api/articles?page=1&per_page=101'), 422, 'E0001', ['per_page' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=bogus'), 422, 'E0001', ['filters.scope' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'GET', '/api/articles?page=1&per_page=20&foo=management'), 422, 'E0001', ['foo' => ['不支援此欄位']]);
t16Check(t16Request($django, 'GET', '/api/articles/nope'), 422, 'E0001', ['article_id' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'GET', '/api/articles/+1'), 422, 'E0001', ['article_id' => ['欄位格式或範圍不正確']]);
t16Check(t16Request($django, 'DELETE', '/api/articles/1'), 405, 'E0005');
t16Check(t16Request($django, 'GET', '/api/not-a-route'), 404, 'E0004');
t16Check(t16Request($django, 'POST', '/api/not-a-route', '{}'), 404, 'E0004');
t16Check(t16Request($django, 'GET', '/api/articles?page=1&per_page=20', '{}'), 422, 'E0001', ['_body' => ['此 API 不接受 Request Body']]);

// 格式錯誤不計數；第五次合法格式錯密碼當次即鎖定且前端可讀 Retry-After。
$lockedId = t16AccountId($locked);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'abc']), 422, 'E0001', ['password' => ['欄位格式或範圍不正確']]);
assert((int) t16Sql("SELECT count(*) FROM login_failures WHERE account_id=$lockedId;") === 0);
for ($i = 1; $i <= 4; $i++) {
    t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Wrong123']), 401, 'E0101');
}
$fifth = t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Wrong123']);
t16Check($fifth, 429, 'E0105');
assert(preg_match('/\r?\nRetry-After: ([1-9][0-9]*)\r?\n/i', $fifth[2]) === 1);
assert(stripos($fifth[2], 'Access-Control-Expose-Headers: Retry-After') !== false);
assert((int) t16Sql("SELECT count(*) FROM login_failures WHERE account_id=$lockedId;") === 5);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Abc1234']), 429, 'E0105');
// 已鎖會員同時停用時先回停用；恢復後沿用原鎖期限。
t16Sql("UPDATE accounts SET status='disabled' WHERE id=$lockedId;");
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Abc1234']), 403, 'E0102');
t16Sql("UPDATE accounts SET status='active' WHERE id=$lockedId;");
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Abc1234']), 429, 'E0105');
// 共用設定關閉後，下一次登入須清除既有鎖而不延長；毀損 PHC 則為 E0104 且不累計。
t16Sql('UPDATE login_lockout_settings SET enabled=false WHERE id=1;');
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Abc1234']), 200);
assert(t16Sql("SELECT (locked_until IS NULL)::int FROM accounts WHERE id=$lockedId;") === '1');
t16Sql('UPDATE login_lockout_settings SET enabled=true WHERE id=1;');
$corrupt = 'corrupt-' . $suffix . '@example.test';
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $corrupt, 'password' => 'Abc1234', 'nickname' => '損壞雜湊']), 201);
$corruptId = t16AccountId($corrupt);
t16Sql("UPDATE accounts SET password_hash='broken' WHERE id=$corruptId;");
$corruptResponse = t16Request($django, 'POST', '/api/auth/login', ['email' => $corrupt, 'password' => 'Abc1234']);
t16Check($corruptResponse, 500, 'E0104');
$corruptLogs = t16Docker(['logs', '--no-color', '--tail', '100', 'django']);
$corruptLogId = preg_quote($corruptResponse[1]['log_id'], '/');
assert(preg_match('/^.*ERROR log_id=' . $corruptLogId . ' code=E0104.*$/m', $corruptLogs, $corruptLine) === 1);
assert(! str_contains($corruptLine[0], 'broken') && ! str_contains($corruptLine[0], 'Abc1234'));
assert(t16Sql("SELECT count(*) FROM login_failures WHERE account_id=$corruptId;") === '0');
// 設定視窗改短時重算既有事件；到期鎖與成功登入皆換世代。
$window = 'window-' . $suffix . '@example.test';
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $window, 'password' => 'Abc1234', 'nickname' => '視窗測試']), 201);
$windowId = t16AccountId($window);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $window, 'password' => 'Wrong123']), 401, 'E0101');
t16Sql("UPDATE login_failures SET occurred_at=clock_timestamp()-interval '2 minutes' WHERE account_id=$windowId;");
t16Sql('UPDATE login_lockout_settings SET window_minutes=1, failure_threshold=2 WHERE id=1;');
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $window, 'password' => 'Wrong123']), 401, 'E0101');
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $window, 'password' => 'Wrong123']), 429, 'E0105');
t16Sql("UPDATE accounts SET locked_until=clock_timestamp()-interval '1 second' WHERE id=$windowId;");
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $window, 'password' => 'Abc1234']), 200);
t16Check(t16Request($django, 'POST', '/api/auth/login', ['email' => $window, 'password' => 'Wrong123']), 401, 'E0101');
t16Sql('UPDATE login_lockout_settings SET window_minutes=5, failure_threshold=5 WHERE id=1;');

// 跨 Laravel/Django 的兩個同時錯密碼請求，應恰好一個 401 與門檻當次一個 429。
t16Sql('UPDATE login_lockout_settings SET failure_threshold=2 WHERE id=1;');
$parallelResults = t16ParallelBadLogins([$django, $laravel], $parallel);
$parallelStatuses = array_column($parallelResults, 0);
sort($parallelStatuses);
assert($parallelStatuses === [401, 429]);
foreach ($parallelResults as $result) {
    t16Check($result, $result[0], $result[0] === 429 ? 'E0105' : 'E0101');
}
$parallelId = t16AccountId($parallel);
assert((int) t16Sql("SELECT count(*) FROM login_failures WHERE account_id=$parallelId;") === 2);
assert(t16Sql("SELECT (locked_until IS NOT NULL)::int FROM accounts WHERE id=$parallelId;") === '1');
t16Sql('UPDATE login_lockout_settings SET failure_threshold=5 WHERE id=1;');

// 停用優先於撤銷；恢復後已撤銷登入不得復活，另一個獨立登入仍有效。
$secondToken = t16Login($django, $author);
t16Check(t16Request($django, 'POST', '/api/auth/logout', '{}', $authorToken), 200);
t16Check(t16Request($django, 'GET', '/api/me', null, $authorToken), 401, 'E0002');
t16Check(t16Request($django, 'GET', '/api/me', null, $secondToken), 200);
$bodylessToken = t16Login($django, $author);
t16Check(t16Request($django, 'POST', '/api/auth/logout', null, $bodylessToken), 200);
t16Check(t16Request($django, 'GET', '/api/me', null, $bodylessToken), 401, 'E0002');
$authorId = t16AccountId($author);
$adminId = t16AccountId($admin);
t16Sql("UPDATE accounts SET role='admin' WHERE id=$adminId;");
$adminToken = t16Login($django, $admin);
$adminLaravelToken = t16Login($laravel, $admin);
t16Check(t16Request($laravel, 'PUT', "/api/members/$authorId/status", ['status' => 'disabled'], $adminLaravelToken), 200);
t16Check(t16Request($django, 'GET', '/api/me', null, $secondToken), 403, 'E0102');
t16Check(t16Request($laravel, 'PUT', "/api/members/$authorId/status", ['status' => 'active'], $adminLaravelToken), 200);
t16Check(t16Request($django, 'GET', '/api/me', null, $secondToken), 401, 'E0002');
$restoredToken = t16Login($django, $author);
$authorLaravelToken = t16Login($laravel, $author);
t16Check(t16Request($django, 'GET', '/api/me', null, $restoredToken), 200);

// Laravel 真實 HTTP 建立內容；已建內容透過 Django 實際讀取相同共用資料。
$articleIds = [];
for ($i = 1; $i <= 3; $i++) {
    t16Check(t16Request($laravel, 'POST', '/api/articles', ['title' => "Ticket16 $suffix $i", 'body' => "內容 $i"], $authorLaravelToken), 201);
    $articleIds[] = (int) t16Sql("SELECT id FROM articles WHERE title='Ticket16 $suffix $i' AND author_id=$authorId;");
}
[$a1, $a2, $a3] = $articleIds;
t16Sql("UPDATE articles SET created_at='2099-01-01 00:00:00+00' WHERE id IN ($a1,$a2,$a3);");
t16Check(t16Request($django, 'GET', '/api/articles/00' . $a3), 200);
$public = t16Request($django, 'GET', '/api/articles?page=1&per_page=2');
t16Check($public, 200);
assert(array_column($public[1]['data']['list'], 'id') === [$a3, $a2]);
assert($public[1]['data']['pagination']['total'] === $visibleBefore + 3);
assert($public[1]['data']['pagination']['total_pages'] === (int) ceil(($visibleBefore + 3) / 2));
$third = t16Request($django, 'GET', '/api/articles?page=3&per_page=1');
assert(array_column($third[1]['data']['list'], 'id') === [$a1]);
$beyond = t16Request($django, 'GET', '/api/articles?page=9007199254740991&per_page=20');
assert($beyond[1]['data']['list'] === [] && $beyond[1]['data']['pagination']['total'] === $visibleBefore + 3);
assert($public[1]['data']['list'][0]['author'] === ['id' => $authorId, 'nickname' => 'Django 作者']);
assert($public[1]['data']['list'][0]['created_backend'] === 'laravel');

$commentIds = [];
for ($i = 1; $i <= 3; $i++) {
    t16Check(t16Request($laravel, 'POST', "/api/articles/$a3/comments", ['body' => "留言 $i"], $authorLaravelToken), 201);
    $commentIds[] = (int) t16Sql("SELECT id FROM comments WHERE article_id=$a3 AND body='留言 $i';");
}
[$c1, $c2, $c3] = $commentIds;
t16Sql("UPDATE comments SET created_at='2099-01-01 00:00:00+00' WHERE id IN ($c1,$c2,$c3);");
t16Check(t16Request($laravel, 'PUT', "/api/comments/$c2/visibility", ['visibility' => 'hidden', 'version' => 1], $authorLaravelToken), 200);
$comments = t16Request($django, 'GET', "/api/articles/$a3/comments?page=1&per_page=20");
t16Check($comments, 200);
assert(array_column($comments[1]['data']['list'], 'id') === [$c3, $c1] && $comments[1]['data']['pagination']['total'] === 2);
// 留言列表的 per_page 同樣不能把合法的長前導零字串重新交給 Python int。
$longPerPage = t16Request($django, 'GET', "/api/articles/$a3/comments?page=1&per_page=" . $zeroPrefix . '20');
t16Check($longPerPage, 200);
assert($longPerPage[1]['data']['pagination']['per_page'] === 20);
$adminComments = t16Request($django, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $adminToken);
assert(array_column($adminComments[1]['data']['list'], 'id') === [$c3, $c2, $c1] && $adminComments[1]['data']['pagination']['total'] === 3);

t16Check(t16Request($laravel, 'PUT', "/api/articles/$a3/visibility", ['visibility' => 'hidden', 'version' => 1], $authorLaravelToken), 200);
t16Check(t16Request($django, 'GET', "/api/articles/$a3"), 404, 'E0004');
t16Check(t16Request($django, 'GET', "/api/articles/$a3", null, $otherToken), 404, 'E0004');
$authorPreview = t16Request($django, 'GET', "/api/articles/$a3", null, $restoredToken);
t16Check($authorPreview, 200);
assert($authorPreview[1]['data']['can_restore'] === true);
t16Check(t16Request($django, 'GET', "/api/articles/$a3/comments?page=1&per_page=20"), 404, 'E0004');
$previewComments = t16Request($django, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $restoredToken);
assert(array_column($previewComments[1]['data']['list'], 'id') === [$c3, $c1]);
$management = t16Request($django, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', null, $adminToken);
t16Check($management, 200);
assert($management[1]['data']['pagination']['total'] === $allBefore + 3);
assert($management[1]['data']['list'][0]['id'] === $a3);
$adminDefault = t16Request($django, 'GET', '/api/articles?page=1&per_page=20', null, $adminToken);
t16Check($adminDefault, 200);
assert($adminDefault[1]['data']['pagination']['total'] === $visibleBefore + 2);
$adminHidden = t16Request($django, 'GET', "/api/articles/$a3", null, $adminToken);
t16Check($adminHidden, 200);
assert($adminHidden[1]['data']['id'] === $a3);
$publicAfter = t16Request($django, 'GET', '/api/articles?page=1&per_page=20');
assert($publicAfter[1]['data']['pagination']['total'] === $visibleBefore + 2);

// 只在隔離資料庫批次建立代表性讀取資料，讓 EXPLAIN 不是空表計畫。
$articleSeed = t16Sql("INSERT INTO articles (author_id,title,body,visibility,restore_control,version,created_backend,created_at,updated_at)
    SELECT $authorId, 'Plan $suffix '||n, '效能樣本', CASE WHEN n % 2 = 0 THEN 'visible' ELSE 'hidden' END,
    CASE WHEN n % 2 = 0 THEN NULL ELSE 'author' END, 1, 'laravel',
    '2024-01-01 00:00:00+00'::timestamptz + n * interval '1 second',
    '2024-01-01 00:00:00+00'::timestamptz + n * interval '1 second'
    FROM generate_series(1, 600) AS n;");
$commentSeed = t16Sql("INSERT INTO comments (author_id,article_id,body,visibility,restore_control,version,created_backend,created_at,updated_at)
    SELECT $authorId, $a3, 'Plan $suffix '||n, 'visible', NULL, 1, 'laravel',
    '2024-01-01 00:00:00+00'::timestamptz + n * interval '1 second',
    '2024-01-01 00:00:00+00'::timestamptz + n * interval '1 second'
    FROM generate_series(1, 600) AS n;");
assert($articleSeed === 'INSERT 0 600' && $commentSeed === 'INSERT 0 600');
t16Sql('ANALYZE articles; ANALYZE comments;');
assert(t16Request($django, 'GET', '/api/articles?page=1&per_page=20')[1]['data']['pagination']['total'] === $visibleBefore + 302);
assert(t16Request($django, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $restoredToken)[1]['data']['pagination']['total'] === 602);

// 用控制器實際 SELECT/count 形狀留下完整 DB 計畫，觀察索引、掃描與耗時。
$plans = [
    'public articles' => "SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible' ORDER BY articles.created_at DESC, articles.id DESC LIMIT 20 OFFSET 0",
    'public article count' => "SELECT count(*) FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible'",
    'management articles' => 'SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id ORDER BY articles.created_at DESC, articles.id DESC LIMIT 20 OFFSET 0',
    'article comments' => "SELECT comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, comments.restore_control, comments.version, comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id=$a3 AND comments.visibility='visible' ORDER BY comments.created_at DESC, comments.id DESC LIMIT 20 OFFSET 0",
    'article comment count' => "SELECT count(*) FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id=$a3 AND comments.visibility='visible'",
];
echo "Ticket 16 Django HTTP/parity checks passed; complete query plans follow:\n";
foreach ($plans as $label => $query) {
    $plan = t16Sql("EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS) $query;");
    assert(str_contains($plan, 'Plan'));
    echo "$label:\n$plan\n";
}

// 共享認證資料庫不可用時不得把有效 JWT 偽稱逾期；恢復 DB 後同 token 仍可用。
$outage = 'outage-' . $suffix . '@example.test';
t16Check(t16Request($django, 'POST', '/api/auth/register', ['email' => $outage, 'password' => 'Abc1234', 'nickname' => '資料庫恢復']), 201);
$outageToken = t16Login($django, $outage);
t16Check(t16Request($django, 'GET', '/api/me', null, $outageToken), 200);
assert(t16Health($django) === 200);
if (! ($GLOBALS['t18VerifiedTarget'] ?? false)) {
    t16Docker(['stop', 'db']);
    try {
        assert(t16Health($django) === 503);
        t16Check(t16Request($django, 'GET', '/api/me', null, $outageToken), 500, 'E9999');
    } finally {
        t16Docker(['up', '-d', '--wait', 'db']);
    }
    assert(t16Health($django) === 200);
    t16Check(t16Request($django, 'GET', '/api/me', null, $outageToken), 200);
}
