<?php

declare(strict_types=1);

// 測試會改會員、內容與鎖定設定；Ticket 15 另須核對 Compose 設定及實際資料庫容器。
$ticket15 = defined('T10_TARGET') && T10_TARGET === 'ticket15';
$ticket18 = ($GLOBALS['t10Target'] ?? null) === 'ticket18';
if ($ticket18) {
    if (! isset($t18Guard) || ! is_callable($t18Guard)) {
        throw new RuntimeException('Ticket 18 缺少隔離容器守衛');
    }
    $t18Guard();
} elseif ($ticket15) {
    t10Ticket15Guard();
} elseif ((getenv('COMPOSE_PROJECT_NAME') ?: '') !== 'ticket10_test') {
    throw new RuntimeException('請使用 ticket10_test 隔離 Compose project');
}
$subject = $ticket18 ? 'http://localhost:19384' : ($ticket15 ? 'http://localhost:19283' : 'http://localhost:19182');
$laravel = $ticket18 ? 'http://localhost:19381' : ($ticket15 ? 'http://localhost:19281' : 'http://localhost:19181');

/** 執行只讀的 Docker 身分查核，失敗時不洩漏可能含密碼的設定輸出。 */
function t10Docker(array $command): string
{
    $pipes = [];
    $process = proc_open($command, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (! is_resource($process)) {
        throw new RuntimeException('無法執行 Docker 身分查核');
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('Docker 身分查核失敗');
    }

    return trim($output);
}

/** 在第一筆 HTTP 或 SQL 寫入前，確認專用設定、埠位與實際 PostgreSQL volume。 */
function t10Ticket15Guard(): void
{
    $root = dirname(__DIR__, 2);
    if (
        getcwd() !== $root || getenv('COMPOSE_PROJECT_NAME') !== 'ticket15_test'
        || getenv('COMPOSE_FILE') !== 'compose.yaml:ops/tests/ticket15_compose.yaml'
        || (int) ini_get('zend.assertions') !== 1 || ! filter_var(ini_get('assert.active'), FILTER_VALIDATE_BOOLEAN)
    ) {
        throw new RuntimeException('Ticket 15 須從 repo root 以指定 Compose project/file 與啟用 PHP assertions 執行');
    }

    // 同時比對 Compose 解出的設定與已啟動容器，避免環境變數正確但服務連到其他資料庫。
    $config = json_decode(t10Docker(['docker', 'compose', 'config', '--format', 'json']), true, 512, JSON_THROW_ON_ERROR);
    $ports = ['laravel' => [19281, 8000], 'symfony' => [19282, 8000],
        'aspnet-core' => [19283, 8080], 'nginx' => [19280, 80]];
    if (
        ($config['name'] ?? null) !== 'ticket15_test'
        || ($config['volumes']['postgres_data']['name'] ?? null) !== 'ticket15_test_postgres_data'
        || ($config['services']['db']['environment']['POSTGRES_DB'] ?? null) !== 'articles'
    ) {
        throw new RuntimeException('Ticket 15 Compose project 或資料庫 volume 不符');
    }
    foreach ($ports as $service => [$published, $target]) {
        $bindings = $config['services'][$service]['ports'] ?? [];
        if (
            count($bindings) !== 1 || (int) ($bindings[0]['published'] ?? 0) !== $published
            || (int) ($bindings[0]['target'] ?? 0) !== $target
            || ($bindings[0]['host_ip'] ?? null) !== '127.0.0.1'
            || ($service !== 'nginx' && (
                ($config['services'][$service]['environment']['DB_HOST'] ?? null) !== 'db'
                || ($config['services'][$service]['environment']['DB_DATABASE'] ?? null) !== 'articles'
            ))
        ) {
            throw new RuntimeException('Ticket 15 Compose 服務埠位或資料庫來源不符：' . $service);
        }
    }
    $dbVolumes = $config['services']['db']['volumes'] ?? [];
    if (
        count($dbVolumes) !== 1 || ($dbVolumes[0]['source'] ?? null) !== 'postgres_data'
        || ($dbVolumes[0]['target'] ?? null) !== '/var/lib/postgresql'
    ) {
        throw new RuntimeException('Ticket 15 資料庫掛載不符');
    }

    foreach (['db', 'laravel', 'aspnet-core'] as $service) {
        $id = t10Docker(['docker', 'compose', 'ps', '-q', $service]);
        if ($id === '' || str_contains($id, "\n")) {
            throw new RuntimeException('Ticket 15 服務未單獨啟動：' . $service);
        }
        $containers = json_decode(t10Docker(['docker', 'inspect', $id]), true, 512, JSON_THROW_ON_ERROR);
        $container = $containers[0] ?? [];
        if (
            ($container['Config']['Labels']['com.docker.compose.project'] ?? null) !== 'ticket15_test'
            || ($container['Config']['Labels']['com.docker.compose.service'] ?? null) !== $service
            || ($container['State']['Running'] ?? false) !== true
            || ! isset($container['NetworkSettings']['Networks']['ticket15_test_default'])
        ) {
            throw new RuntimeException('Ticket 15 實際容器身分不符：' . $service);
        }
        if ($service === 'db') {
            $mounts = $container['Mounts'] ?? [];
            if (
                count($mounts) !== 1 || ($mounts[0]['Type'] ?? null) !== 'volume'
                || ($mounts[0]['Name'] ?? null) !== 'ticket15_test_postgres_data'
                || ($mounts[0]['Destination'] ?? null) !== '/var/lib/postgresql'
            ) {
                throw new RuntimeException('Ticket 15 實際資料庫 volume 不符');
            }
            continue;
        }
        [$published, $target] = $ports[$service];
        $binding = $container['NetworkSettings']['Ports'][$target . '/tcp'][0] ?? [];
        if (
            ($binding['HostIp'] ?? null) !== '127.0.0.1'
            || (int) ($binding['HostPort'] ?? 0) !== $published
        ) {
            throw new RuntimeException('Ticket 15 實際服務埠位不符：' . $service);
        }
    }
}

/** 由兩個後端的公開 HTTP 介面送出相同案例，保留狀態、資料與標頭供比對。 */
function t10Request(string $base, string $method, string $path, array|string|null $body = null, ?string $token = null): array
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
        CURLOPT_POSTFIELDS => is_array($body) ? json_encode($body, JSON_THROW_ON_ERROR) : ($body ?? ''),
        CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 20]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
    $result = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
    curl_close($curl);

    return $result;
}

/** 共同錯誤需逐字一致，且每次錯誤須有可追查的 log_id。 */
function t10Check(array $result, int $status, ?string $code = null, ?array $errors = null): void
{
    assert($result[0] === $status, 'status ' . $status . ' got ' . $result[0] . ' ' . json_encode($result[1], JSON_UNESCAPED_UNICODE));
    assert($result[1]['success'] === ($code === null));
    if ($code === null) {
        assert($result[1]['message'] === 'OK');

        return;
    }
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API', 'E0101' => '帳號或密碼錯誤',
        'E0102' => '此帳號已停用，請聯絡系統管理員', 'E0105' => '登入失敗次數過多，請稍後再試',
        'E9999' => '系統暫時無法處理，請稍後再試'];
    assert($result[1]['error_code'] === $code && $result[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
    assert(preg_match('/\A[0-9a-f-]{36}\z/', $result[1]['log_id']) === 1);
    if ($errors !== null) {
        assert($result[1]['errors'] === $errors, json_encode($result[1]['errors'], JSON_UNESCAPED_UNICODE));
    }
}

/** 比對兩後端完整錯誤契約；每次請求獨立產生的 log_id 只驗格式，不比值。 */
function t10SameError(array $subjectResult, array $laravelResult): void
{
    assert($subjectResult[0] === $laravelResult[0]);
    assert(t10CanonicalJson(array_diff_key($subjectResult[1], ['log_id' => true]))
        === t10CanonicalJson(array_diff_key($laravelResult[1], ['log_id' => true])));
}

/** JSON object 鍵順序無語意；僅排序 object，保留陣列順序、欄位和值的嚴格比較。 */
function t10CanonicalJson(mixed $value): mixed
{
    if (! is_array($value)) {
        return $value;
    }
    if (! array_is_list($value)) {
        ksort($value);
    }
    foreach ($value as $key => $item) {
        $value[$key] = t10CanonicalJson($item);
    }

    return $value;
}

/** fixture 僅寫入隔離 Compose 資料庫；公開結果仍由 HTTP 案例驗證。 */
function t10Sql(string $sql): string
{
    $pipes = [];
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At', '-c', $sql],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    assert(is_resource($process));
    fclose($pipes[0]);
    $out = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 SQL 失敗：' . $error);
    }

    return trim($out);
}

/** 各後端各自簽發 JWT；帳號資料共用，但 token 仍不可跨後端使用。 */
function t10Identity(string $base, string $nickname, bool $admin = false): array
{
    $email = 't10-' . bin2hex(random_bytes(6)) . '@example.test';
    t10Check(t10Request($base, 'POST', '/api/auth/register', ['email' => $email, 'password' => 'Abc1234', 'nickname' => $nickname]), 201);
    if ($admin) {
        t10Sql("UPDATE accounts SET role='admin' WHERE email='$email'");
    }
    $login = t10Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
    t10Check($login, 200);

    return [$login[1]['data']['user']['id'], $login[1]['data']['access_token'], $email];
}

/** 以同一 curl_multi 同時送出各自的後端、方法與憑證，保留回應供競爭結果驗證。 */
function t10ParallelRequests(array $requests): array
{
    $multi = curl_multi_init();
    $handles = [];
    try {
        // 先加入全部請求再驅動事件迴圈，避免依呼叫順序固定勝者。
        foreach ($requests as [$base, $method, $path, $body, $token]) {
            $headers = ['Accept: application/json', 'Content-Type: application/json'];
            if ($token !== null) {
                $headers[] = 'Authorization: Bearer ' . $token;
            }
            $curl = curl_init($base . $path);
            curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method,
                CURLOPT_POSTFIELDS => is_array($body) ? json_encode($body, JSON_THROW_ON_ERROR) : $body,
                CURLOPT_HTTPHEADER => $headers, CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 20]);
            curl_multi_add_handle($multi, $curl);
            $handles[] = $curl;
        }
        do {
            if (curl_multi_exec($multi, $running) !== CURLM_OK) {
                throw new RuntimeException('並行 HTTP 傳輸失敗');
            }
            if ($running > 0) {
                curl_multi_select($multi, 1);
            }
        } while ($running > 0);

        // 每個回應都保留 HTTP 狀態、完整 JSON 及標頭，讓 401／429 與 Retry-After 可逐一檢查。
        $results = [];
        foreach ($handles as $curl) {
            if (curl_errno($curl) !== 0) {
                throw new RuntimeException(curl_error($curl));
            }
            $raw = curl_multi_getcontent($curl);
            $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
            $results[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
                json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
        }

        return $results;
    } finally {
        foreach ($handles as $curl) {
            curl_multi_remove_handle($multi, $curl);
            curl_close($curl);
        }
        curl_multi_close($multi);
    }
}

/** Ticket 10 仍以同一後端送兩筆管理 PUT，並確認兩筆都成功。 */
function t10ParallelPut(string $base, string $path, array $a, array $b, string $token): void
{
    foreach (t10ParallelRequests([[$base, 'PUT', $path, $a, $token], [$base, 'PUT', $path, $b, $token]]) as $result) {
        t10Check($result, 200);
    }
}

$defaults = ['enabled' => true, 'window_minutes' => 5, 'failure_threshold' => 5, 'lock_seconds' => 300];
// 可重跑時先將隔離資料庫設定還原；先前測試帳號保留作稽核，不清空資料。
t10Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE id=1');
[$adminId, $admin] = t10Identity($subject, '管理員', true);
$laravelAdmin = t10Request($laravel, 'POST', '/api/auth/login', ['email' => t10Sql("SELECT email FROM accounts WHERE id=$adminId"), 'password' => 'Abc1234']);
t10Check($laravelAdmin, 200);
$laravelToken = $laravelAdmin[1]['data']['access_token'];
[$memberId, $member, $memberEmail] = t10Identity($subject, 'A_B%\\É中' . bin2hex(random_bytes(4)));
[$otherId, $other] = t10Identity($laravel, '普通會員é');
$otherSubject = t10Request($subject, 'POST', '/api/auth/login', ['email' => t10Sql("SELECT email FROM accounts WHERE id=$otherId"), 'password' => 'Abc1234']);
t10Check($otherSubject, 200);
$path = '/api/members?page=1&per_page=100';

// 先驗角色再驗 Query；會員搜尋只折疊 ASCII，所有萬用符號均為字面內容。
t10Check(t10Request($subject, 'GET', $path), 401, 'E0002');
t10Check(t10Request($subject, 'GET', '/api/members?page=bad&per_page=20', null, $member), 403, 'E0003');
foreach (['a_b', '%', '\\', 'É', '中'] as $keyword) {
    $result = t10Request($subject, 'GET', $path . '&filters%5Bkeyword%5D=' . rawurlencode($keyword), null, $admin);
    t10Check($result, 200);
    assert(in_array($memberId, array_column($result[1]['data']['list'], 'id'), true));
}
$different = t10Request($subject, 'GET', $path . '&filters%5Bkeyword%5D=' . rawurlencode('é'), null, $admin);
t10Check($different, 200);
assert(in_array($otherId, array_column($different[1]['data']['list'], 'id'), true));
$needle = 'T10' . bin2hex(random_bytes(6));
[$literalId] = t10Identity($subject, $needle . '_X%\\É');
[$decoyId] = t10Identity($subject, $needle . 'AXYé');
[$upperId] = t10Identity($subject, $needle . 'É');
[$lowerId] = t10Identity($subject, $needle . 'é');
// 唯一前綴讓萬用符號與 Unicode 反例可斷言完整集合和精確總數，並對照 Laravel。
foreach (
    [[$needle . '_', $literalId], [$needle . '_X%', $literalId], [$needle . '_X%\\', $literalId],
    [strtolower($needle) . '_x', $literalId], [$needle . 'É', $upperId], [$needle . 'é', $lowerId],
    [$needle . 'AXY', $decoyId]] as [$keyword, $expectedId]
) {
    $searchPath = $path . '&filters%5Bkeyword%5D=' . rawurlencode($keyword);
    $fromSubject = t10Request($subject, 'GET', $searchPath, null, $admin);
    $fromLaravel = t10Request($laravel, 'GET', $searchPath, null, $laravelToken);
    t10Check($fromSubject, 200);
    t10Check($fromLaravel, 200);
    assert(array_column($fromSubject[1]['data']['list'], 'id') === [$expectedId], $keyword);
    assert($fromSubject[1]['data']['pagination']['total'] === 1, $keyword);
    assert(t10CanonicalJson($fromSubject[1]['data']) === t10CanonicalJson($fromLaravel[1]['data']), $keyword);
}
$repeatedSubject = t10Request($subject, 'GET', $path . '&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y', null, $admin);
$repeatedLaravel = t10Request($laravel, 'GET', $path . '&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y', null, $laravelToken);
t10Check($repeatedSubject, 422, 'E0001', ['filters.keyword' => ['此參數不可重複']]);
t10Check($repeatedLaravel, 422, 'E0001', ['filters.keyword' => ['此參數不可重複']]);
t10SameError($repeatedSubject, $repeatedLaravel);
t10Check(t10Request($subject, 'GET', $path, '{}', $admin), 422, 'E0001', ['_body' => ['此 API 不接受 Request Body']]);
t10Check(t10Request($subject, 'GET', '/api/members/' . $adminId, null, $admin), 404, 'E0004');
$detail = t10Request($subject, 'GET', '/api/members/' . $memberId, null, $admin);
t10Check($detail, 200);
assert(array_keys($detail[1]['data']) === ['id', 'email', 'nickname', 'role', 'status', 'created_at']);
assert($detail[1]['data']['email'] === $memberEmail);
$deepPath = '/api/members?page=9007199254740991&per_page=100&filters%5Bkeyword%5D=' . $needle;
$deep = t10Request($subject, 'GET', $deepPath, null, $admin);
$deepLaravel = t10Request($laravel, 'GET', $deepPath, null, $laravelToken);
t10Check($deep, 200);
t10Check($deepLaravel, 200);
assert($deep[1]['data']['list'] === [] && $deep[1]['data']['pagination']['total'] === 4);
assert(t10CanonicalJson($deep[1]['data']) === t10CanonicalJson($deepLaravel[1]['data']));
echo "PASS member authorization, literal search and pagination\n";

// 既有作者操作建立內容，管理列表需含隱藏內容且只回核准欄位。
t10Check(t10Request($subject, 'POST', '/api/articles', ['title' => '管理文章', 'body' => '內容'], $member), 201);
$articleId = (int) t10Sql("SELECT max(id) FROM articles WHERE author_id=$memberId");
t10Check(t10Request($subject, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '留言'], $member), 201);
$commentId = (int) t10Sql("SELECT max(id) FROM comments WHERE article_id=$articleId");
t10Check(t10Request($subject, 'PUT', '/api/articles/' . $articleId . '/visibility', ['visibility' => 'hidden', 'version' => 1], $admin), 200);
$articles = t10Request($subject, 'GET', '/api/articles?page=1&per_page=100&filters%5Bscope%5D=management', null, $admin);
t10Check($articles, 200);
assert(in_array($articleId, array_column($articles[1]['data']['list'], 'id'), true));
$comments = t10Request($subject, 'GET', '/api/comments?page=1&per_page=100', null, $admin);
t10Check($comments, 200);
assert(in_array($commentId, array_column($comments[1]['data']['list'], 'id'), true));
t10Check(t10Request($subject, 'GET', '/api/comments?page=1&per_page=20', null, $member), 403, 'E0003');
echo "PASS management articles and comments\n";

// 設定完整覆寫、關閉清鎖及同值無副作用，並由 Laravel 讀同一份設定。
$settingsPath = '/api/settings/login-lockout';
t10Check(t10Request($subject, 'GET', $settingsPath, null, $member), 403, 'E0003');
$initial = t10Request($subject, 'GET', $settingsPath, null, $admin);
t10Check($initial, 200);
assert($initial[1]['data'] === $defaults);
$sameVersion = t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1');
t10Check(t10Request($subject, 'PUT', $settingsPath, array_reverse($defaults, true), $admin), 200);
assert(t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1') === $sameVersion);
$missingSubject = t10Request($subject, 'PUT', $settingsPath, ['enabled' => true], $admin);
$missingLaravel = t10Request($laravel, 'PUT', $settingsPath, ['enabled' => true], $laravelToken);
t10Check($missingSubject, 422, 'E0001');
t10Check($missingLaravel, 422, 'E0001');
t10SameError($missingSubject, $missingLaravel);
$next = ['enabled' => true, 'window_minutes' => 7, 'failure_threshold' => 2, 'lock_seconds' => 120];
t10Check(t10Request($subject, 'PUT', $settingsPath, $next, $admin), 200);
assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $next);
assert(t10Request($laravel, 'GET', $settingsPath, null, $laravelToken)[1]['data'] === $next);
// 同一份既有資料以兩種 JWT 各自讀取，逐欄比較管理列表及詳情。
foreach ([$path, '/api/members/' . $memberId, '/api/comments?page=1&per_page=100', '/api/articles?page=1&per_page=100&filters%5Bscope%5D=management'] as $readPath) {
    $fromSubject = t10Request($subject, 'GET', $readPath, null, $admin);
    $fromLaravel = t10Request($laravel, 'GET', $readPath, null, $laravelToken);
    t10Check($fromSubject, 200);
    t10Check($fromLaravel, 200);
    if (t10CanonicalJson($fromSubject[1]['data']) !== t10CanonicalJson($fromLaravel[1]['data'])) {
        $subjectRows = $fromSubject[1]['data']['list'] ?? [];
        $laravelRows = $fromLaravel[1]['data']['list'] ?? [];
        foreach ($subjectRows as $index => $row) {
            if (t10CanonicalJson($row) !== t10CanonicalJson($laravelRows[$index] ?? null)) {
                throw new RuntimeException('管理資料跨後端不一致：' . $readPath . ' 首筆差異=' . json_encode([
                    'index' => $index,
                    'django' => $row,
                    'laravel' => $laravelRows[$index] ?? null,
                ], JSON_UNESCAPED_UNICODE));
            }
        }
        throw new RuntimeException('管理資料跨後端不一致：' . $readPath . ' pagination=' . json_encode([
            $fromSubject[1]['data']['pagination'] ?? null,
            $fromLaravel[1]['data']['pagination'] ?? null,
        ]));
    }
}
$wrong = ['email' => $memberEmail, 'password' => 'Wrong123'];
t10Check(t10Request($laravel, 'POST', '/api/auth/login', $wrong), 401, 'E0101');
$locked = t10Request($subject, 'POST', '/api/auth/login', $wrong);
t10Check($locked, 429, 'E0105');
assert(preg_match('/Retry-After: [1-9][0-9]*/i', $locked[2]) === 1);
$until = t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId");
t10Check(t10Request($subject, 'PUT', $settingsPath, array_replace($next, ['lock_seconds' => 300]), $admin), 200);
assert(t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId") === $until);
$epoch = (int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$memberId");
$off = array_replace($next, ['enabled' => false]);
t10Check(t10Request($subject, 'PUT', $settingsPath, $off, $admin), 200);
assert(t10Sql("SELECT locked_until IS NULL FROM accounts WHERE id=$memberId") === 't');
assert((int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === $epoch + 1);
assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $off);
t10Check(t10Request($subject, 'PUT', $settingsPath, $off, $admin), 200);
assert((int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === $epoch + 1);
// 清帳號失敗時，四欄設定與鎖定資料都要回滾，不得留下半套更新。
t10Check(t10Request($subject, 'PUT', $settingsPath, $next, $admin), 200);
t10Sql("UPDATE accounts SET locked_until=clock_timestamp()+interval '1 hour' WHERE id=$memberId");
$beforeRollback = t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId");
t10Sql("CREATE FUNCTION t10_fail_clear() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated clear failure'; END; $$; CREATE TRIGGER t10_fail_clear BEFORE UPDATE ON accounts FOR EACH ROW WHEN (NEW.role = 'member') EXECUTE FUNCTION t10_fail_clear();");
try {
    t10Check(t10Request($subject, 'PUT', $settingsPath, $off, $admin), 500, 'E9999');
    assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $next);
    assert(t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId") === $beforeRollback);
} finally {
    t10Sql('DROP TRIGGER t10_fail_clear ON accounts; DROP FUNCTION t10_fail_clear();');
}
t10Check(t10Request($subject, 'PUT', $settingsPath, $off, $admin), 200);
$a = ['enabled' => false, 'window_minutes' => 7, 'failure_threshold' => 11, 'lock_seconds' => 333];
$b = ['enabled' => true, 'window_minutes' => 9, 'failure_threshold' => 13, 'lock_seconds' => 555];
t10ParallelPut($subject, $settingsPath, $a, $b, $admin);
$finalSettings = t10Request($subject, 'GET', $settingsPath, null, $admin);
t10Check($finalSettings, 200);
assert($finalSettings[1]['data'] === $a || $finalSettings[1]['data'] === $b);
echo "PASS shared settings, cross-backend lockout and disable transition\n";

// 停用應撤銷受測後端與 Laravel 的所有舊登入；恢復不可讓舊 JWT 復活。
$memberLaravel = t10Request($laravel, 'POST', '/api/auth/login', ['email' => $memberEmail, 'password' => 'Abc1234']);
t10Check($memberLaravel, 200);
$statusPath = '/api/members/' . $memberId . '/status';
t10Check(t10Request($subject, 'PUT', $statusPath, ['status' => 'disabled'], $member), 403, 'E0003');
$invalidStatusSubject = t10Request($subject, 'PUT', $statusPath, ['status' => 'retired'], $admin);
$invalidStatusLaravel = t10Request($laravel, 'PUT', $statusPath, ['status' => 'retired'], $laravelToken);
t10Check($invalidStatusSubject, 422, 'E0001', ['status' => ['欄位格式或範圍不正確']]);
t10Check($invalidStatusLaravel, 422, 'E0001', ['status' => ['欄位格式或範圍不正確']]);
t10SameError($invalidStatusSubject, $invalidStatusLaravel);
$adminTargetSubject = t10Request($subject, 'PUT', '/api/members/' . $adminId . '/status', ['status' => 'disabled'], $admin);
$adminTargetLaravel = t10Request($laravel, 'PUT', '/api/members/' . $adminId . '/status', ['status' => 'disabled'], $laravelToken);
t10Check($adminTargetSubject, 404, 'E0004');
t10Check($adminTargetLaravel, 404, 'E0004');
t10SameError($adminTargetSubject, $adminTargetLaravel);
t10Check(t10Request($subject, 'PUT', $statusPath, ['status' => 'disabled'], $admin), 200);
t10Check(t10Request($subject, 'GET', '/api/me', null, $member), 403, 'E0102');
t10Check(t10Request($laravel, 'GET', '/api/me', null, $memberLaravel[1]['data']['access_token']), 403, 'E0102');
t10Check(t10Request($subject, 'PUT', $statusPath, ['status' => 'active'], $admin), 200);
t10Check(t10Request($subject, 'GET', '/api/me', null, $member), 401, 'E0002');
t10Check(t10Request($laravel, 'GET', '/api/me', null, $memberLaravel[1]['data']['access_token']), 401, 'E0002');
assert(t10Sql("SELECT visibility FROM articles WHERE id=$articleId") === 'hidden');
// 兩筆狀態寫入可依提交順序決定最終值，但任何實際停用都必須撤銷舊登入。
[$raceId, $raceToken] = t10Identity($subject, '並行會員');
t10ParallelPut($subject, '/api/members/' . $raceId . '/status', ['status' => 'active'], ['status' => 'disabled'], $admin);
assert(in_array(t10Sql("SELECT status FROM accounts WHERE id=$raceId"), ['active', 'disabled'], true));
assert(t10Sql("SELECT count(*) FROM login_sessions WHERE account_id=$raceId AND revoked_at IS NULL") === '0');
if (t10Sql("SELECT status FROM accounts WHERE id=$raceId") === 'active') {
    t10Check(t10Request($subject, 'GET', '/api/me', null, $raceToken), 401, 'E0002');
} else {
    t10Check(t10Request($subject, 'GET', '/api/me', null, $raceToken), 403, 'E0102');
}
echo "PASS status, cross-backend revocation and content preservation\n";

// 同一會員逐次重置世代，將歷史事件對齊到新事件的視窗左界與 +1μs，重播兩後端。
t10Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=2, lock_seconds=300 WHERE id=1');
[$boundaryId, , $boundaryEmail] = t10Identity($subject, '邊界會員');
foreach (['0 microseconds' => 401, '1 microsecond' => 429] as $offset => $status) {
    $responses = [];
    $lockStates = [];
    foreach ([$subject, $laravel] as $base) {
        t10Sql("UPDATE accounts SET locked_until=NULL, failure_epoch=failure_epoch+1 WHERE id=$boundaryId;
            INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '10 minutes' FROM accounts WHERE id=$boundaryId;
            CREATE FUNCTION t10_boundary() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS \$\$
            BEGIN
                IF NEW.account_id=$boundaryId THEN
                    UPDATE public.login_failures SET occurred_at=NEW.occurred_at-interval '5 minutes'+interval '$offset' WHERE account_id=NEW.account_id AND failure_epoch=NEW.failure_epoch;
                END IF;
                RETURN NEW;
            END; \$\$;
            CREATE TRIGGER t10_boundary BEFORE INSERT ON login_failures FOR EACH ROW EXECUTE FUNCTION t10_boundary();");
        try {
            $response = t10Request($base, 'POST', '/api/auth/login', ['email' => $boundaryEmail, 'password' => 'Wrong123']);
            t10Check($response, $status, $status === 401 ? 'E0101' : 'E0105');
            $responses[] = $response;
            $lockStates[] = t10Sql("SELECT locked_until IS NOT NULL FROM accounts WHERE id=$boundaryId");
        } finally {
            t10Sql('DROP TRIGGER t10_boundary ON login_failures; DROP FUNCTION t10_boundary();');
        }
    }
    t10SameError($responses[0], $responses[1]);
    assert($lockStates === ($status === 401 ? ['f', 'f'] : ['t', 't']));
}
t10Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE id=1');
echo "PASS cross-backend error and microsecond boundary parity\n";
if (! $ticket15 && ! $ticket18) {
    echo "Ticket 10 Symfony management HTTP: PASS\n";
}
