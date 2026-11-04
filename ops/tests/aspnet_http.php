<?php

declare(strict_types=1);

// 本檔為獨立執行的 HTTP 驗收入口，輔助函式與案例保留同檔以便直接執行。
// phpcs:disable PSR1.Files.SideEffects

require_once __DIR__ . '/http_fixture.php';
// 所有 fixture 操作之前先確認斷言有效，不能在測試檢查被關閉時仍送出寫入。
assertHttpFixtureAssertions();

// 測試只對已確認的 Ticket 13 隔離專案及固定埠寫入，避免污染正式展示資料。
if (
    ! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket13_')
    || ! str_contains(getenv('COMPOSE_FILE') ?: '', 'ops/tests/aspnet_compose.yaml')
) {
    throw new RuntimeException('必須使用 ticket13_ 隔離 Compose 與 aspnet_compose.yaml');
}
$aspnet = 'http://localhost:18083';
$laravel = 'http://localhost:18081';
$symfony = 'http://localhost:18082';
assertHttpFixture(['aspnet-core' => $aspnet, 'laravel' => $laravel, 'symfony' => $symfony]);

/** 保留真實 HTTP status、Body、Headers，讓同一案例可比較不同後端契約。 */
function t13Request(
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
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

    return [
        curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
        json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR),
        substr($raw, 0, $length)
    ];
}

/** 逐字檢查安全錯誤 envelope 與 log_id，不把憑證或密碼輸出至測試記錄。 */
function t13Check(array $result, int $status, ?string $code = null, ?array $errors = null): void
{
    if ($result[0] !== $status || $result[1]['success'] !== ($code === null)) {
        throw new RuntimeException(
            'HTTP 狀態或 success 不符：' . json_encode([$result[0], $result[1]['error_code'] ?? null])
        );
    }
    if ($code === null) {
        assert($result[1]['message'] === 'OK');
        assert(is_array($result[1]['data']));
        return;
    }
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API',
        'E0005' => '系統暫時無法處理，請稍後再試', 'E0101' => '帳號或密碼錯誤',
        'E0102' => '此帳號已停用，請聯絡系統管理員', 'E0105' => '登入失敗次數過多，請稍後再試'];
    assert($result[1]['error_code'] === $code);
    assert($result[1]['message'] === $messages[$code]);
    assert(($result[1]['errors'] ?? null) === $errors);
    assert(preg_match('/\A[0-9a-f-]{36}\z/', $result[1]['log_id']) === 1);
}

/** 只在同一隔離 Compose 資料庫建立 fixture 與讀取副作用，絕不連展示 volume。 */
function t13Sql(string $sql): string
{
    $process = proc_open(
        [
            'docker',
            'compose',
            'exec',
            '-T',
            'db',
            'psql',
            '-U',
            'articles_owner',
            '-d',
            'articles',
            '-v',
            'ON_ERROR_STOP=1',
            '-At'
        ],
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

/** 依唯一 Email 取新 fixture ID，避免把其他執行留下的資料當成自己的資料。 */
function t13AccountId(string $email): int
{
    return (int) t13Sql("SELECT id FROM accounts WHERE email='" . str_replace("'", "''", $email) . "';");
}

/** 取得核准後端的 token，只驗證公開 JSON 欄位，不依賴 JWT 私有表示。 */
function t13Login(string $base, string $email): string
{
    $result = t13Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
    t13Check($result, 200);
    assert($result[1]['data']['token_type'] === 'Bearer');
    return $result[1]['data']['access_token'];
}

/** 同時送往兩個後端，確認設定列鎖使同帳號失敗計數與門檻回應序列化。 */
function t13ParallelBadLogins(array $bases, string $email): array
{
    $multi = curl_multi_init();
    $handles = [];
    foreach ($bases as $base) {
        $curl = curl_init($base . '/api/auth/login');
        curl_setopt_array($curl, [CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json'],
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
        $responses[] = [
            curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
            json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR),
            substr($raw, 0, $length)
        ];
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);
    return $responses;
}

// 先固定隔離專案的設定，再用唯一名稱建立可重跑的帳號與文章。
t13Sql(
    ('UPDATE login_lockout_settings SET enabled=true, ' .
        'window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE ' .
        'id=1;')
);
$suffix = bin2hex(random_bytes(5));
$author = 'aspnet-' . $suffix . '@example.test';
$other = 'laravel-' . $suffix . '@example.test';
$admin = 'admin-' . $suffix . '@example.test';
$locked = 'locked-' . $suffix . '@example.test';
$parallel = 'parallel-' . $suffix . '@example.test';
$visibleBefore = (int) t13Sql("SELECT count(*) FROM articles WHERE visibility='visible';");
$allBefore = (int) t13Sql('SELECT count(*) FROM articles;');
foreach ([$author, $admin, $locked, $parallel] as $email) {
    t13Check(
        t13Request(
            $aspnet,
            'POST',
            '/api/auth/register',
            ['email' => $email, 'password' => 'Abc1234', 'nickname' => 'ASP 作者']
        ),
        201
    );
}
t13Check(
    t13Request(
        $laravel,
        'POST',
        '/api/auth/register',
        ['email' => $other, 'password' => 'Abc1234', 'nickname' => 'Laravel 作者']
    ),
    201
);
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/register',
        ['email' => strtoupper($author), 'password' => 'Abc1234', 'nickname' => '重複']
    ),
    422,
    'E0001',
    ['email' => ['此 Email 已註冊']]
);
// 非 ASCII Kelvin sign 不得摺為 k；同形 Email 的登入格式錯誤也不能增加既有帳號失敗次數。
$asciiTarget = 'kprobe' . $suffix . '@example.test';
$kelvinEmail = "\u{212A}probe" . $suffix . '@example.test';
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/register',
        ['email' => $kelvinEmail, 'password' => 'Abc1234', 'nickname' => '不應建立']
    ),
    422,
    'E0001',
    ['email' => ['欄位格式或範圍不正確']]
);
assert(t13AccountId($asciiTarget) === 0);
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/register',
        ['email' => $asciiTarget, 'password' => 'Abc1234', 'nickname' => 'ASCII 目標']
    ),
    201
);
$asciiTargetId = t13AccountId($asciiTarget);
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/login',
        ['email' => $kelvinEmail, 'password' => 'Wrong123']
    ),
    422,
    'E0001',
    ['email' => ['欄位格式或範圍不正確']]
);
assert((int) t13Sql("SELECT count(*) FROM login_failures WHERE account_id=$asciiTargetId;") === 0);
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/register',
        ['email' => 'x@example.test', 'password' => '123', 'nickname' => '合法']
    ),
    422,
    'E0001',
    ['password' => ['欄位格式或範圍不正確']]
);
t13Check(t13Request($aspnet, 'POST', '/api/auth/register', '{'), 400, 'E0005');
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/register',
        ['email' => null, 'password' => 'Abc1234', 'nickname' => '合法']
    ),
    422,
    'E0001',
    ['email' => ['此欄位不可為 null']]
);
t13Check(
    t13Request(
        $aspnet,
        'POST',
        '/api/auth/register?foo=1',
        ['email' => 'x@example.test', 'password' => 'Abc1234', 'nickname' => '合法']
    ),
    422,
    'E0001',
    ['foo' => ['不支援此欄位']]
);
$invalidQuery = t13Request($aspnet, 'POST', '/api/auth/register?%FF=1', '{}');
t13Check($invalidQuery, 422, 'E0001', ['%FF' => ['不支援此欄位']]);

// 預檢只允許已核准的同源前端、方法與標頭。
$preflight = curl_init($aspnet . '/api/auth/login');
curl_setopt_array($preflight, [CURLOPT_CUSTOMREQUEST => 'OPTIONS', CURLOPT_HTTPHEADER => [
    'Origin: http://localhost:8080', 'Access-Control-Request-Method: POST',
        'Access-Control-Request-Headers: authorization,content-type'],
    CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 20]);
$preflightResponse = curl_exec($preflight);
assert($preflightResponse !== false && curl_getinfo($preflight, CURLINFO_RESPONSE_CODE) === 204);
assert(stripos($preflightResponse, 'Access-Control-Allow-Origin: http://localhost:8080') !== false);
assert(stripos($preflightResponse, 'Access-Control-Allow-Headers:') !== false);

// 跨語言 Argon2id：ASP.NET 能驗 Laravel 雜湊，Laravel 能驗 ASP.NET 建立的 PHC。
$otherToken = t13Login($aspnet, $other);
$authorLaravelToken = t13Login($laravel, $author);
$authorToken = t13Login($aspnet, $author);
t13Check(t13Request($aspnet, 'GET', '/api/me', null, $authorToken), 200);
t13Check(t13Request($aspnet, 'GET', '/api/articles?page=1&per_page=20', null, $authorLaravelToken), 401, 'E0002');
t13Check(t13Request($aspnet, 'GET', '/api/articles?page=bad&per_page=20', null, 'invalid'), 401, 'E0002');
t13Check(
    t13Request(
        $aspnet,
        'GET',
        '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management',
        null,
        $authorToken
    ),
    403,
    'E0003'
);
t13Check(
    t13Request(
        $aspnet,
        'GET',
        '/api/articles?page=bad&per_page=20&filters%5Bscope%5D=management',
        null,
        $authorToken
    ),
    403,
    'E0003'
);
t13Check(t13Request($aspnet, 'GET', '/api/me?foo=1'), 401, 'E0002');
t13Check(t13Request($aspnet, 'GET', '/api/articles?page=1&page=2&per_page=20'), 422, 'E0001', ['page' => ['此參數不可重複']]);
t13Check(t13Request($aspnet, 'GET', '/api/articles?page%5B%5D=1&per_page=20'), 422, 'E0001', ['page' => ['欄位型態不正確']]);
t13Check(t13Request($aspnet, 'GET', '/api/articles/nope'), 422, 'E0001', ['article_id' => ['欄位格式或範圍不正確']]);
t13Check(t13Request($aspnet, 'DELETE', '/api/articles/1'), 405, 'E0005');
t13Check(t13Request($aspnet, 'GET', '/api/not-a-route'), 404, 'E0004');
t13Check(
    t13Request($aspnet, 'GET', '/api/articles?page=1&per_page=20', '{}'),
    422,
    'E0001',
    ['_body' => ['此 API 不接受 Request Body']]
);

// 格式錯誤不計數；第五次合法格式錯密碼當次即鎖定且前端可讀 Retry-After。
$lockedId = t13AccountId($locked);
t13Check(
    t13Request($aspnet, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'abc']),
    422,
    'E0001',
    ['password' => ['欄位格式或範圍不正確']]
);
assert((int) t13Sql("SELECT count(*) FROM login_failures WHERE account_id=$lockedId;") === 0);
for ($i = 1; $i <= 4; $i++) {
    t13Check(
        t13Request(
            $aspnet,
            'POST',
            '/api/auth/login',
            ['email' => $locked, 'password' => 'Wrong123']
        ),
        401,
        'E0101'
    );
}
$fifth = t13Request($aspnet, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Wrong123']);
t13Check($fifth, 429, 'E0105');
assert(preg_match('/\r?\nRetry-After: ([1-9][0-9]*)\r?\n/i', $fifth[2]) === 1);
assert(stripos($fifth[2], 'Access-Control-Expose-Headers: Retry-After') !== false);
assert((int) t13Sql("SELECT count(*) FROM login_failures WHERE account_id=$lockedId;") === 5);
t13Check(t13Request($aspnet, 'POST', '/api/auth/login', ['email' => $locked, 'password' => 'Abc1234']), 429, 'E0105');

// 跨 Laravel/ASP.NET 的兩個同時錯密碼請求，應恰好一個 401 與門檻當次一個 429。
t13Sql('UPDATE login_lockout_settings SET failure_threshold=2 WHERE id=1;');
$parallelResults = t13ParallelBadLogins([$aspnet, $laravel], $parallel);
$parallelStatuses = array_column($parallelResults, 0);
sort($parallelStatuses);
assert($parallelStatuses === [401, 429]);
foreach ($parallelResults as $result) {
    t13Check($result, $result[0], $result[0] === 429 ? 'E0105' : 'E0101');
}
$parallelId = t13AccountId($parallel);
assert((int) t13Sql("SELECT count(*) FROM login_failures WHERE account_id=$parallelId;") === 2);
assert(t13Sql("SELECT (locked_until IS NOT NULL)::int FROM accounts WHERE id=$parallelId;") === '1');
t13Sql('UPDATE login_lockout_settings SET failure_threshold=5 WHERE id=1;');

// 停用優先於撤銷；恢復後已撤銷登入不得復活，另一個獨立登入仍有效。
$secondToken = t13Login($aspnet, $author);
t13Check(t13Request($aspnet, 'POST', '/api/auth/logout', '{}', $authorToken), 200);
t13Check(t13Request($aspnet, 'GET', '/api/me', null, $authorToken), 401, 'E0002');
t13Check(t13Request($aspnet, 'GET', '/api/me', null, $secondToken), 200);
$authorId = t13AccountId($author);
$adminId = t13AccountId($admin);
t13Sql("UPDATE accounts SET role='admin' WHERE id=$adminId;");
$adminToken = t13Login($aspnet, $admin);
$adminLaravelToken = t13Login($laravel, $admin);
t13Check(
    t13Request(
        $laravel,
        'PUT',
        "/api/members/$authorId/status",
        ['status' => 'disabled'],
        $adminLaravelToken
    ),
    200
);
t13Check(t13Request($aspnet, 'GET', '/api/me', null, $secondToken), 403, 'E0102');
t13Check(t13Request($laravel, 'PUT', "/api/members/$authorId/status", ['status' => 'active'], $adminLaravelToken), 200);
t13Check(t13Request($aspnet, 'GET', '/api/me', null, $secondToken), 401, 'E0002');
$restoredToken = t13Login($aspnet, $author);
$authorLaravelToken = t13Login($laravel, $author);
t13Check(t13Request($aspnet, 'GET', '/api/me', null, $restoredToken), 200);

// Laravel 真實 HTTP 建立內容；已建內容透過 ASP.NET 實際讀取相同共用資料。
$articleIds = [];
for ($i = 1; $i <= 3; $i++) {
    t13Check(
        t13Request(
            $laravel,
            'POST',
            '/api/articles',
            ['title' => "Ticket13 $suffix $i", 'body' => "內容 $i"],
            $authorLaravelToken
        ),
        201
    );
    $articleIds[] = (int) t13Sql("SELECT id FROM articles WHERE title='Ticket13 $suffix $i' AND author_id=$authorId;");
}
[$a1, $a2, $a3] = $articleIds;
t13Sql("UPDATE articles SET created_at='2099-01-01 00:00:00+00' WHERE id IN ($a1,$a2,$a3);");
$public = t13Request($aspnet, 'GET', '/api/articles?page=1&per_page=2');
t13Check($public, 200);
assert(array_column($public[1]['data']['list'], 'id') === [$a3, $a2]);
assert($public[1]['data']['pagination']['total'] === $visibleBefore + 3);
assert($public[1]['data']['pagination']['total_pages'] === (int) ceil(($visibleBefore + 3) / 2));
$third = t13Request($aspnet, 'GET', '/api/articles?page=3&per_page=1');
assert(array_column($third[1]['data']['list'], 'id') === [$a1]);
$beyond = t13Request($aspnet, 'GET', '/api/articles?page=9007199254740991&per_page=20');
assert($beyond[1]['data']['list'] === [] && $beyond[1]['data']['pagination']['total'] === $visibleBefore + 3);
assert($public[1]['data']['list'][0]['author'] === ['id' => $authorId, 'nickname' => 'ASP 作者']);
assert($public[1]['data']['list'][0]['created_backend'] === 'laravel');

$commentIds = [];
for ($i = 1; $i <= 3; $i++) {
    t13Check(t13Request($laravel, 'POST', "/api/articles/$a3/comments", ['body' => "留言 $i"], $authorLaravelToken), 201);
    $commentIds[] = (int) t13Sql("SELECT id FROM comments WHERE article_id=$a3 AND body='留言 $i';");
}
[$c1, $c2, $c3] = $commentIds;
t13Sql("UPDATE comments SET created_at='2099-01-01 00:00:00+00' WHERE id IN ($c1,$c2,$c3);");
t13Check(
    t13Request(
        $laravel,
        'PUT',
        "/api/comments/$c2/visibility",
        ['visibility' => 'hidden', 'version' => 1],
        $authorLaravelToken
    ),
    200
);
$comments = t13Request($aspnet, 'GET', "/api/articles/$a3/comments?page=1&per_page=20");
t13Check($comments, 200);
assert(
    array_column($comments[1]['data']['list'], 'id') === [$c3, $c1] && $comments[1]['data']['pagination']['total'] === 2
);
$adminComments = t13Request($aspnet, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $adminToken);
assert(
    array_column($adminComments[1]['data']['list'], 'id') === [$c3, $c2, $c1]
        && $adminComments[1]['data']['pagination']['total'] === 3
);

t13Check(
    t13Request(
        $laravel,
        'PUT',
        "/api/articles/$a3/visibility",
        ['visibility' => 'hidden', 'version' => 1],
        $authorLaravelToken
    ),
    200
);
t13Check(t13Request($aspnet, 'GET', "/api/articles/$a3"), 404, 'E0004');
t13Check(t13Request($aspnet, 'GET', "/api/articles/$a3", null, $otherToken), 404, 'E0004');
$authorPreview = t13Request($aspnet, 'GET', "/api/articles/$a3", null, $restoredToken);
t13Check($authorPreview, 200);
assert($authorPreview[1]['data']['can_restore'] === true);
t13Check(t13Request($aspnet, 'GET', "/api/articles/$a3/comments?page=1&per_page=20"), 404, 'E0004');
$previewComments = t13Request($aspnet, 'GET', "/api/articles/$a3/comments?page=1&per_page=20", null, $restoredToken);
assert(array_column($previewComments[1]['data']['list'], 'id') === [$c3, $c1]);
$management = t13Request(
    $aspnet,
    'GET',
    '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management',
    null,
    $adminToken
);
t13Check($management, 200);
assert($management[1]['data']['pagination']['total'] === $allBefore + 3);
assert($management[1]['data']['list'][0]['id'] === $a3);
$publicAfter = t13Request($aspnet, 'GET', '/api/articles?page=1&per_page=20');
assert($publicAfter[1]['data']['pagination']['total'] === $visibleBefore + 2);

// 用控制器實際 SELECT/count 形狀留下完整 DB 計畫；代表性資料量另由隔離效能驗證補足。
$plans = [
    'public articles' => ("SELECT articles.id, articles.author_id, articles.title, " .
        "articles.body, articles.visibility, articles.restore_control, " .
        "articles.version, articles.created_backend, " .
        "articles.created_at, articles.updated_at, accounts.nickname " .
        "FROM articles JOIN accounts ON accounts.id=articles.author_id " .
        "WHERE articles.visibility='visible' ORDER BY " .
        "articles.created_at DESC, articles.id DESC LIMIT 20 OFFSET 0"),
    'public article count' => ("SELECT count(*) FROM articles JOIN accounts ON " .
        "accounts.id=articles.author_id WHERE " .
        "articles.visibility='visible'"),
    'management articles' => ('SELECT articles.id, articles.author_id, articles.title, ' .
        'articles.body, articles.visibility, articles.restore_control, ' .
        'articles.version, articles.created_backend, ' .
        'articles.created_at, articles.updated_at, accounts.nickname ' .
        'FROM articles JOIN accounts ON accounts.id=articles.author_id ' .
        'ORDER BY articles.created_at DESC, articles.id DESC LIMIT 20 ' .
        'OFFSET 0'),
    'article comments' => ("SELECT comments.id, comments.author_id, comments.article_id, " .
        "comments.body, comments.visibility, comments.restore_control, " .
        "comments.version, comments.created_backend, " .
        "comments.created_at, comments.updated_at, accounts.nickname " .
        "FROM comments JOIN accounts ON accounts.id=comments.author_id " .
        "WHERE comments.article_id=$a3 AND comments.visibility='visible' " .
        "ORDER BY comments.created_at DESC, comments.id DESC LIMIT 20 " .
        "OFFSET 0"),
    'article comment count' => ("SELECT count(*) FROM comments JOIN accounts ON " .
        "accounts.id=comments.author_id WHERE comments.article_id=$a3 " .
        "AND comments.visibility='visible'"),
];
echo "Ticket 13 ASP.NET HTTP/parity checks passed; complete query plans follow:\n";
foreach ($plans as $label => $query) {
    $plan = t13Sql("EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS) $query;");
    assert(str_contains($plan, 'Plan'));
    echo "$label:\n$plan\n";
}
