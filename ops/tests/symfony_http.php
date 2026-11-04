<?php

declare(strict_types=1);

// 本檔為獨立執行的 HTTP 驗收入口，輔助函式與案例保留同檔以便直接執行。
// phpcs:disable PSR1.Files.SideEffects

require_once __DIR__ . '/http_fixture.php';
// 所有 fixture 操作之前先確認斷言有效，不能在測試檢查被關閉時仍送出寫入。
assertHttpFixtureAssertions();

// 只對 Ticket 08 隔離 Compose 專案送出寫入，避免污染展示帳號與內容。
if (! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket08_')) {
    throw new RuntimeException('請使用 ticket08_ 開頭的隔離 Compose project');
}
$symfony = getenv('SYMFONY_API_BASE') ?: 'http://localhost:18082';
$laravel = getenv('LARAVEL_API_BASE') ?: 'http://localhost:18081';
assertHttpFixture(['symfony' => $symfony, 'laravel' => $laravel]);

/** 經真實 HTTP 保留 status、JSON 與回應標頭，驗證共同公開契約。 */
function symfonyRequest(
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
    curl_setopt_array(
        $curl,
        [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_POSTFIELDS => $body === null ? '' : (is_array($body) ? json_encode(
                $body,
                JSON_THROW_ON_ERROR
            ) : $body),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HEADER => true,
            CURLOPT_TIMEOUT => 20
        ]
    );
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

/** 同時送 Laravel 與 Symfony 錯密碼，觀察共用設定列及帳號列鎖是否序列化計數。 */
function symfonyParallelBadLogins(array $bases, string $email): array
{
    $multi = curl_multi_init();
    $handles = [];
    // 先將兩個請求都加入 multi handle，才開始執行，避免測試本身串行化。
    foreach ($bases as $base) {
        $curl = curl_init($base . '/api/auth/login');
        curl_setopt_array(
            $curl,
            [
                CURLOPT_POST => true,
                CURLOPT_HTTPHEADER => [
                    'Accept: application/json',
                    'Content-Type: application/json',
                    'Origin: http://localhost:8080'
                ],
                CURLOPT_POSTFIELDS => json_encode(['email' => $email, 'password' => 'Wrong123'], JSON_THROW_ON_ERROR),
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_HEADER => true,
                CURLOPT_TIMEOUT => 20
            ]
        );
        curl_multi_add_handle($multi, $curl);
        $handles[] = $curl;
    }
    do {
        $result = curl_multi_exec($multi, $running);
        assert($result === CURLM_OK);
        if ($running) {
            curl_multi_select($multi, 1.0);
        }
    } while ($running);

    // 保留每個回應的 status 與標頭，方便同一套契約斷言檢查兩種後端。
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

/** 只檢查公開 status、錯誤代碼與安全 log_id，不輸出 token 或帳密。 */
function symfonyCheck(array $result, int $status, ?string $code = null): void
{
    assert($result[0] === $status, 'status expected ' . $status . ', got ' . $result[0]);
    assert($result[1]['success'] === ($code === null));
    if ($code !== null) {
        assert($result[1]['error_code'] === $code);
        $messages = [
            'E0001' => '欄位資料不正確，請確認後再送出',
            'E0002' => '登入已逾期，請重新登入',
            'E0003' => '沒有權限使用此功能',
            'E0004' => '找不到指定資料或 API',
            'E0005' => '系統暫時無法處理，請稍後再試',
            'E0101' => '帳號或密碼錯誤',
            'E0102' => '此帳號已停用，請聯絡系統管理員',
            'E0104' => '後台認證設定異常，請聯絡系統管理員',
            'E0105' => '登入失敗次數過多，請稍後再試',
            'E9999' => '系統暫時無法處理，請稍後再試'
        ];
        assert($result[1]['message'] === $messages[$code]);
        assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
        assert(preg_match('/\A[0-9a-f-]{36}\z/', $result[1]['log_id']) === 1);
    }
}

/** 只在隔離資料庫做狀態 fixture，業務結果仍經公開 HTTP 檢查。 */
function symfonySql(string $sql): string
{
    $pipes = [];
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
    assert(is_resource($process));
    fwrite($pipes[0], $sql);
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 SQL fixture 失敗：' . $error);
    }

    return trim($output);
}

/** 僅控制已指定的隔離 Compose 服務，測試資料庫失聯時認證須安全失敗。 */
function symfonyCompose(array $arguments): void
{
    $pipes = [];
    $process = proc_open(
        array_merge(['docker', 'compose'], $arguments),
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    assert(is_resource($process));
    fclose($pipes[0]);
    stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 Compose 命令失敗：' . $error);
    }
}

// 既有隔離 volume 可重跑；先固定本案例所需的預設鎖定設定。
symfonySql(
    ('UPDATE login_lockout_settings SET enabled=true, ' .
        'window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE ' .
        'id=1;')
);
$suffix = bin2hex(random_bytes(6));
$email = 'symfony-' . $suffix . '@example.test';
$otherEmail = 'laravel-' . $suffix . '@example.test';
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/register',
        ['email' => $email, 'password' => 'Abc1234', 'nickname' => 'Symfony 作者']
    ),
    201
);
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/register',
        ['email' => strtoupper($email), 'password' => 'Abc1234', 'nickname' => '重複']
    ),
    422,
    'E0001'
);
// 非 UTF-8 Query 名稱仍須用 JSON 驗證錯誤回應，不得使錯誤封裝本身失敗。
$invalidQuery = symfonyRequest($symfony, 'POST', '/api/auth/register?%FF=1', '{}');
symfonyCheck($invalidQuery, 422, 'E0001');
assert($invalidQuery[1]['errors']['%FF'] === ['不支援此欄位']);
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/register',
        [
            'email' => 'invalid@example.test',
            'password' => 'Abc1234',
            'nickname' => "暱稱\0"
        ]
    ),
    422,
    'E0001'
);
symfonyCheck(
    symfonyRequest(
        $laravel,
        'POST',
        '/api/auth/register',
        ['email' => $otherEmail, 'password' => 'Abc1234', 'nickname' => 'Laravel 作者']
    ),
    201
);
$login = symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => strtoupper($email), 'password' => 'Abc1234']);
symfonyCheck($login, 200);
$token = $login[1]['data']['access_token'];
$user = $login[1]['data']['user'];
assert($user['email'] === $email && $user['role'] === 'member');
assert($login[1]['data']['token_type'] === 'Bearer');
$laravelLogin = symfonyRequest($laravel, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
symfonyCheck($laravelLogin, 200);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me', null, $laravelLogin[1]['data']['access_token']), 401, 'E0002');
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/login',
        ['email' => $otherEmail, 'password' => 'Abc1234']
    ),
    200
);
$me = symfonyRequest($symfony, 'GET', '/api/me', null, $token);
symfonyCheck($me, 200);
assert($me[1]['data'] === $user);
assert(stripos($me[2], 'Access-Control-Allow-Origin: http://localhost:8080') !== false);
echo "PASS shared Argon2id, independent JWT, account and CORS\n";

// 僅損壞新建隔離帳號的儲存雜湊；共同錯誤碼與 log_id 必須可追查且不可洩漏雜湊。
$corruptEmail = 'corrupt-' . $suffix . '@example.test';
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/register',
        ['email' => $corruptEmail, 'password' => 'Abc1234', 'nickname' => '雜湊測試']
    ),
    201
);
symfonySql("UPDATE accounts SET password_hash='bad-hash' WHERE email='$corruptEmail';");
$corruptLogin = symfonyRequest(
    $symfony,
    'POST',
    '/api/auth/login',
    ['email' => $corruptEmail, 'password' => 'Abc1234']
);
symfonyCheck($corruptLogin, 500, 'E0104');
assert(
    (int) symfonySql(
        ("SELECT count(*) FROM login_failures AS f JOIN accounts AS a ON " .
            "a.id=f.account_id WHERE a.email='$corruptEmail';")
    ) === 0
);
$log = fopen('.local/ticket08-backend/symfony-logs/api-' . gmdate('Y-m-d') . '.log', 'r');
assert($log !== false);
$traceFound = false;
while (($line = fgets($log)) !== false) {
    $entry = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
    if (($entry['log_id'] ?? null) === $corruptLogin[1]['log_id'] && ($entry['event'] ?? null) === 'EX') {
        assert(($entry['error_code'] ?? null) === 'E0104' && ($entry['exception'] ?? null) === 'App\\ApiFailure');
        assert(array_keys($entry) === ['event', 'log_id', 'error_code', 'exception']);
        assert(! str_contains($line, 'bad-hash'));
        $traceFound = true;
        break;
    }
}
fclose($log);
assert($traceFound);
echo "PASS corrupt hash contract and safe log trace\n";

// 兩後端同時打同一會員的第二次錯密碼；一筆仍是 401，門檻當次為 429 且鎖已提交。
$parallelEmail = 'parallel-' . $suffix . '@example.test';
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/register',
        ['email' => $parallelEmail, 'password' => 'Abc1234', 'nickname' => '並行測試']
    ),
    201
);
symfonySql(
    ('UPDATE login_lockout_settings SET enabled=true, ' .
        'window_minutes=5, failure_threshold=2, lock_seconds=300 WHERE ' .
        'id=1;')
);
$parallel = symfonyParallelBadLogins([$laravel, $symfony], $parallelEmail);
$statuses = array_column($parallel, 0);
sort($statuses);
assert($statuses === [401, 429]);
foreach ($parallel as $response) {
    symfonyCheck($response, $response[0], $response[0] === 401 ? 'E0101' : 'E0105');
}
assert(
    (int) symfonySql(
        ("SELECT count(*) FROM login_failures AS f JOIN accounts AS a ON " .
            "a.id=f.account_id WHERE a.email='$parallelEmail' AND " .
            "f.failure_epoch=a.failure_epoch;")
    ) === 2
);
assert(symfonySql("SELECT locked_until > clock_timestamp() FROM accounts WHERE email='$parallelEmail';") === 't');
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/login',
        ['email' => $parallelEmail, 'password' => 'Abc1234']
    ),
    429,
    'E0105'
);
symfonySql('UPDATE login_lockout_settings SET failure_threshold=5 WHERE id=1;');
echo "PASS parallel Laravel/Symfony lockout serialization\n";

// 格式錯誤不能計數；管理範圍須先通過認證與角色門禁。
symfonyCheck(
    symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => $email, 'password' => '123']),
    422,
    'E0001'
);
symfonyCheck(symfonyRequest($symfony, 'POST', '/api/auth/login', '{'), 400, 'E0005');
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me?x=1'), 401, 'E0002');
symfonyCheck(symfonyRequest($symfony, 'POST', '/api/auth/logout', '{'), 401, 'E0002');
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles?page=1&per_page=20', null, 'invalid'), 401, 'E0002');
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles?filters%5Bscope%5D=management&bad=1'), 401, 'E0002');
symfonyCheck(
    symfonyRequest(
        $symfony,
        'GET',
        '/api/articles?filters%5Bscope%5D=management&bad=1',
        null,
        $token
    ),
    403,
    'E0003'
);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles?page=1&page=2&per_page=20'), 422, 'E0001');
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles?page=1'), 422, 'E0001');
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles?page%5B%5D=1&per_page=20'), 422, 'E0001');
$keywordQuery = '/api/articles?page=1&per_page=20&filters%5Bkeyword%5D%5B%5D=x&filters%5Bkeyword%5D=y';
$symfonyKeyword = symfonyRequest($symfony, 'GET', $keywordQuery);
$laravelKeyword = symfonyRequest($laravel, 'GET', $keywordQuery);
symfonyCheck($symfonyKeyword, 422, 'E0001');
symfonyCheck($laravelKeyword, 422, 'E0001');
assert($symfonyKeyword[1]['errors'] === $laravelKeyword[1]['errors']);
assert($symfonyKeyword[1]['errors']['filters.keyword'] === ['此參數不可重複']);
$scopeQuery = '/api/articles?page=1&per_page=20&filters%5Bscope%5D%5B%5D=x&filters%5Bscope%5D=y';
$symfonyScope = symfonyRequest($symfony, 'GET', $scopeQuery);
$laravelScope = symfonyRequest($laravel, 'GET', $scopeQuery);
symfonyCheck($symfonyScope, 422, 'E0001');
symfonyCheck($laravelScope, 422, 'E0001');
assert($symfonyScope[1]['errors'] === $laravelScope[1]['errors']);
assert($symfonyScope[1]['errors']['filters.scope'] === ['此參數不可重複']);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles/bad-id'), 422, 'E0001');
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles/9007199254740991'), 404, 'E0004');

// Laravel 寫入共用資料後，由 Symfony 精確讀取文章及留言。
$article = symfonyRequest(
    $laravel,
    'POST',
    '/api/articles',
    ['title' => '跨後端文章', 'body' => '原文 <b>literal</b>'],
    $laravelLogin[1]['data']['access_token']
);
symfonyCheck($article, 201);
$list = symfonyRequest($symfony, 'GET', '/api/articles?page=1&per_page=20');
symfonyCheck($list, 200);
assert($list[1]['data']['pagination']['total'] >= 1);
$articleId = $list[1]['data']['list'][0]['id'];
assert($list[1]['data']['list'][0]['title'] === '跨後端文章');
$detailQuery = '/api/articles/' . $articleId . '?filters%5Bscope%5D=x';
$symfonyDetailQuery = symfonyRequest($symfony, 'GET', $detailQuery);
$laravelDetailQuery = symfonyRequest($laravel, 'GET', $detailQuery);
symfonyCheck($symfonyDetailQuery, 422, 'E0001');
symfonyCheck($laravelDetailQuery, 422, 'E0001');
assert($symfonyDetailQuery[1]['errors'] === $laravelDetailQuery[1]['errors']);
assert($symfonyDetailQuery[1]['errors']['filters.scope'] === ['不支援此欄位']);
$detail = symfonyRequest($symfony, 'GET', '/api/articles/' . $articleId);
symfonyCheck($detail, 200);
assert($detail[1]['data']['body'] === '原文 <b>literal</b>');
symfonyCheck(
    symfonyRequest(
        $laravel,
        'POST',
        '/api/articles/' . $articleId . '/comments',
        ['body' => '跨後端留言'],
        $laravelLogin[1]['data']['access_token']
    ),
    201
);
$comments = symfonyRequest($symfony, 'GET', '/api/articles/' . $articleId . '/comments?page=1&per_page=20');
symfonyCheck($comments, 200);
assert($comments[1]['data']['list'][0]['body'] === '跨後端留言');
$commentId = $comments[1]['data']['list'][0]['id'];
$commentKeywordQuery = '/api/articles/' . $articleId . (
    '/comments?page=1&per_page=20&filters%5Bkeyword%5D%5B%5D=x&filter' .
    's%5Bkeyword%5D=y');
$symfonyCommentQuery = symfonyRequest($symfony, 'GET', $commentKeywordQuery);
$laravelCommentQuery = symfonyRequest($laravel, 'GET', $commentKeywordQuery);
symfonyCheck($symfonyCommentQuery, 422, 'E0001');
symfonyCheck($laravelCommentQuery, 422, 'E0001');
assert($symfonyCommentQuery[1]['errors'] === $laravelCommentQuery[1]['errors']);
assert($symfonyCommentQuery[1]['errors']['filters.keyword'] === ['此參數不可重複']);
symfonyCheck(
    symfonyRequest(
        $laravel,
        'PUT',
        '/api/articles/' . $articleId . '/visibility',
        ['visibility' => 'hidden', 'version' => 1],
        $laravelLogin[1]['data']['access_token']
    ),
    200
);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles/' . $articleId), 404, 'E0004');
symfonyCheck(
    symfonyRequest(
        $symfony,
        'GET',
        '/api/articles/' . $articleId . '/comments?page=1&per_page=20'
    ),
    404,
    'E0004'
);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/articles/' . $articleId, null, $token), 200);
echo "PASS public reading, pagination, hidden parent and author access\n";

// 預設五次門檻，當次鎖定須提交並帶有 CORS 可讀剩餘秒數。
for ($index = 0; $index < 4; $index++) {
    symfonyCheck(
        symfonyRequest(
            $symfony,
            'POST',
            '/api/auth/login',
            ['email' => $email, 'password' => 'Wrong123']
        ),
        401,
        'E0101'
    );
}
$locked = symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Wrong123']);
symfonyCheck($locked, 429, 'E0105');
assert(
    preg_match('/Retry-After: ([0-9]+)/i', $locked[2], $match) === 1 && (int) $match[1] >= 1 && (int) $match[1] <= 300
);
assert(stripos($locked[2], 'Access-Control-Expose-Headers: Retry-After') !== false);
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/login',
        ['email' => $email, 'password' => 'Abc1234']
    ),
    429,
    'E0105'
);
symfonyCheck(symfonyRequest($symfony, 'POST', '/api/auth/logout', null, $token), 200);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me', null, $token), 401, 'E0002');
echo "PASS committed lockout, Retry-After and session revocation\n";

// 共用設定關閉時解除舊鎖；重新啟用後仍由目前設定與目前世代計數。
symfonySql("UPDATE accounts SET role='admin' WHERE email='$otherEmail';");
$adminLogin = symfonyRequest($laravel, 'POST', '/api/auth/login', ['email' => $otherEmail, 'password' => 'Abc1234']);
symfonyCheck($adminLogin, 200);
$adminToken = $adminLogin[1]['data']['access_token'];
symfonyCheck(
    symfonyRequest(
        $laravel,
        'PUT',
        '/api/settings/login-lockout',
        [
            'enabled' => false,
            'window_minutes' => 5,
            'failure_threshold' => 5,
            'lock_seconds' => 300
        ],
        $adminToken
    ),
    200
);
$afterDisabled = symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
symfonyCheck($afterDisabled, 200);
symfonyCheck(
    symfonyRequest(
        $laravel,
        'PUT',
        '/api/settings/login-lockout',
        [
            'enabled' => true,
            'window_minutes' => 5,
            'failure_threshold' => 1,
            'lock_seconds' => 300
        ],
        $adminToken
    ),
    200
);
symfonyCheck(
    symfonyRequest(
        $symfony,
        'POST',
        '/api/auth/login',
        ['email' => $email, 'password' => 'Wrong123']
    ),
    429,
    'E0105'
);
symfonySql("UPDATE accounts SET locked_until=clock_timestamp()-interval '1 second' WHERE email='$email';");
$afterExpiry = symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
symfonyCheck($afterExpiry, 200);
echo "PASS shared settings, lock expiry and success reset\n";

// 停用優先於撤銷；恢復帳號後，已撤銷的舊登入不可復活。
$currentToken = $afterExpiry[1]['data']['access_token'];
symfonySql(
    ("BEGIN; UPDATE accounts SET status='disabled' WHERE " .
        "email='$email'; UPDATE login_sessions SET " .
        "revoked_at=clock_timestamp() WHERE account_id=(SELECT id FROM " .
        "accounts WHERE email='$email'); COMMIT;")
);
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me', null, $currentToken), 403, 'E0102');
symfonySql("UPDATE accounts SET status='active' WHERE email='$email';");
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me', null, $currentToken), 401, 'E0002');
echo "PASS disabled precedence and revoked-session recovery\n";

// 共用認證資料無法查核時回系統錯誤；恢復資料庫後同一有效憑證仍可使用。
$recovered = symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
symfonyCheck($recovered, 200);
$recoveredToken = $recovered[1]['data']['access_token'];
symfonyCompose(['stop', 'db']);
try {
    symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me', null, $recoveredToken), 500, 'E9999');
} finally {
    symfonyCompose(['up', '-d', '--wait', 'db']);
}
symfonyCheck(symfonyRequest($symfony, 'GET', '/api/me', null, $recoveredToken), 200);
echo "PASS DB failure closes access and recovery preserves valid session\n";

// 父文章恢復公開後，個別下架留言只在管理員的文章留言列表出現。
$laravelRecovered = symfonyRequest($laravel, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
symfonyCheck($laravelRecovered, 200);
$laravelRecoveredToken = $laravelRecovered[1]['data']['access_token'];
symfonyCheck(
    symfonyRequest(
        $laravel,
        'PUT',
        '/api/articles/' . $articleId . '/visibility',
        ['visibility' => 'visible', 'version' => 2],
        $laravelRecoveredToken
    ),
    200
);
symfonyCheck(
    symfonyRequest(
        $laravel,
        'PUT',
        '/api/comments/' . $commentId . '/visibility',
        ['visibility' => 'hidden', 'version' => 1],
        $laravelRecoveredToken
    ),
    200
);
$commentPath = '/api/articles/' . $articleId . '/comments?page=1&per_page=20';
$visitorComments = symfonyRequest($symfony, 'GET', $commentPath);
$memberComments = symfonyRequest($symfony, 'GET', $commentPath, null, $recoveredToken);
symfonyCheck($visitorComments, 200);
symfonyCheck($memberComments, 200);
assert($visitorComments[1]['data']['list'] === [] && $visitorComments[1]['data']['pagination']['total'] === 0);
assert($memberComments[1]['data']['list'] === [] && $memberComments[1]['data']['pagination']['total'] === 0);
$symfonyAdmin = symfonyRequest($symfony, 'POST', '/api/auth/login', ['email' => $otherEmail, 'password' => 'Abc1234']);
symfonyCheck($symfonyAdmin, 200);
$adminComments = symfonyRequest($symfony, 'GET', $commentPath, null, $symfonyAdmin[1]['data']['access_token']);
symfonyCheck($adminComments, 200);
assert($adminComments[1]['data']['pagination']['total'] === 1);
assert(
    $adminComments[1]['data']['list'][0]['id'] === $commentId
        && $adminComments[1]['data']['list'][0]['visibility'] === 'hidden'
);
echo "PASS individual hidden comment visibility by role\n";
