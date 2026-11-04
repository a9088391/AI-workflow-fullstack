<?php

declare(strict_types=1);

// 本檔為獨立執行的 HTTP 驗收入口，輔助函式與案例保留同檔以便直接執行。
// phpcs:disable PSR1.Files.SideEffects

require_once __DIR__ . '/http_fixture.php';
// 所有 fixture 操作之前先確認斷言有效，不能在測試檢查被關閉時仍送出寫入。
assertHttpFixtureAssertions();

// 兩張票共用同一套作者操作案例；Ticket 14 需同時鎖定專案與 overlay 才能寫入測資。
$project = getenv('COMPOSE_PROJECT_NAME') ?: '';
$ticket14 = $project === 'ticket14_test';
if (! $ticket14 && $project !== 'ticket09_test') {
    throw new RuntimeException('請使用 ticket09_test 或 ticket14_test 隔離 Compose project');
}
if (
    $ticket14 && (getcwd() !== realpath(__DIR__ . '/../..')
    || (getenv('COMPOSE_FILE') ?: '') !== 'compose.yaml:ops/tests/ticket14_compose.yaml')
) {
    throw new RuntimeException('Ticket 14 必須從專案根目錄使用 ticket14_compose.yaml');
}
if ($ticket14 && (ini_get('zend.assertions') !== '1' || ini_get('assert.exception') !== '1')) {
    throw new RuntimeException('Ticket 14 必須啟用 PHP assertion 與例外');
}
$subjectBase = $ticket14 ? 'http://localhost:18083' : 'http://localhost:19082';
$laravelBase = $ticket14 ? 'http://localhost:18081' : 'http://localhost:19081';
$subjectBackend = $ticket14 ? 'aspnet-core' : 'symfony';
assertHttpFixture([$subjectBackend => $subjectBase, 'laravel' => $laravelBase]);

/** 先核對固定埠確實屬於 Ticket 14 Compose，避免請求誤寫另一組本機服務。 */
function ticket09AssertTarget(): void
{
    foreach (
        [
        ['aspnet-core', '8080', '127.0.0.1:18083'],
        ['laravel', '8000', '127.0.0.1:18081']
        ] as [$service, $port, $expected]
    ) {
        $pipes = [];
        $process = proc_open(
            ['docker', 'compose', 'port', $service, $port],
            [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
            $pipes
        );
        if (! is_resource($process)) {
            throw new RuntimeException('無法檢查 Ticket 14 Compose 服務');
        }
        fclose($pipes[0]);
        $actual = trim(stream_get_contents($pipes[1]));
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        if (proc_close($process) !== 0 || $actual !== $expected) {
            throw new RuntimeException('Ticket 14 埠不屬於隔離 Compose：' . $service . ' ' . $error);
        }
    }

    // 必須由同一個 Compose 專案提供 SQL fixture 所連的資料庫。
    $pipes = [];
    $process = proc_open(
        ['docker', 'compose', 'ps', '-q', 'db'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('無法檢查 Ticket 14 資料庫');
    }
    fclose($pipes[0]);
    $container = trim(stream_get_contents($pipes[1]));
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0 || $container === '') {
        throw new RuntimeException('Ticket 14 隔離資料庫尚未啟動');
    }
}

/** 經公開 HTTP 檢查驗證、路由與回應；額外標頭讓 CORS 預檢符合瀏覽器請求。 */
function ticket09Request(
    string $base,
    string $method,
    string $path,
    array|string|null $body = null,
    ?string $token = null,
    array $extraHeaders = []
): array {
    $headers = ['Accept: application/json', 'Origin: http://localhost:8080', ...$extraHeaders];
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
    $responseBody = substr($raw, $length);
    $response = $responseBody === '' ? [] : json_decode($responseBody, true, 512, JSON_THROW_ON_ERROR);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    curl_close($curl);

    return [$status, $response, substr($raw, 0, $length)];
}

/** 精確核對共同錯誤文案與成功資料型態，避免只看 HTTP 狀態。 */
function ticket09Check(array $response, int $status, ?string $code = null, ?array $errors = null): void
{
    assert(
        $response[0] === $status,
        'HTTP expected ' . $status . ', got ' . $response[0] . ' ' . json_encode($response[1], JSON_UNESCAPED_UNICODE)
    );
    assert($response[1]['success'] === ($code === null));
    if ($code === null) {
        assert($response[1]['message'] === 'OK');

        return;
    }
    $messages = [
        'E0001' => '欄位資料不正確，請確認後再送出',
        'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能',
        'E0004' => '找不到指定資料或 API',
        'E0005' => '系統暫時無法處理，請稍後再試',
        'E0201' => '內容已更新，請確認最新內容後再送出'
    ];
    assert($response[1]['error_code'] === $code && $response[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $response[1]) === ($code === 'E0001'));
    assert(preg_match('/\A[0-9a-f-]{36}\z/', $response[1]['log_id']) === 1);
    if ($errors !== null) {
        assert(
            $response[1]['errors'] === $errors,
            'errors mismatch: ' . json_encode($response[1]['errors'], JSON_UNESCAPED_UNICODE)
        );
    }
}

/** 只對隔離資料庫建立管理員測資及取得持久列快照。 */
function ticket09Sql(string $sql): string
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
            '-At',
            '-c',
            $sql
        ],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    assert(is_resource($process));
    fclose($pipes[0]);
    $result = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 SQL 失敗：' . $error);
    }

    return trim($result);
}

/** 每名測試會員取得自身 JWT，管理員由隔離 SQL 固定角色後重新登入。 */
function ticket09Identity(string $base, string $name, bool $admin = false): array
{
    $email = 'ticket09-' . bin2hex(random_bytes(6)) . '@example.test';
    ticket09Check(
        ticket09Request(
            $base,
            'POST',
            '/api/auth/register',
            ['email' => $email, 'password' => 'Abc1234', 'nickname' => $name]
        ),
        201
    );
    if ($admin) {
        ticket09Sql("UPDATE accounts SET role = 'admin' WHERE email = '" . $email . "'");
    }
    $login = ticket09Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
    ticket09Check($login, 200);

    return [$login[1]['data']['user']['id'], $login[1]['data']['access_token'], $email];
}

/** 寫入回應應保持空 object；失敗則逐字符合共同契約。 */
function ticket09Write(
    string $base,
    string $method,
    string $path,
    array|string $body,
    string $token,
    int $status = 200,
    ?string $code = null
): void {
    $result = ticket09Request($base, $method, $path, $body, $token);
    ticket09Check($result, $status, $code);
    if ($code === null) {
        assert($result[1]['data'] === []);
    }
}

/** 持有隔離資料列鎖到 HTTP 確實等待，再提交轉移以驗證鎖後重驗。 */
function ticket09Race(string $lock, string $transition, string $method, string $path, array $body, string $token): array
{
    global $subjectBase;
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
            '-qAt'
        ],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    assert(is_resource($process));
    $curl = curl_init($subjectBase . $path);
    $multi = curl_multi_init();
    try {
        // 確認鎖已取得，才送出待驗證的真實請求。
        fwrite($pipes[0], "BEGIN;\nSELECT 1 $lock;\nSELECT 'locked';\n");
        fflush($pipes[0]);
        $line = '';
        do {
            $line = trim(fgets($pipes[1]) ?: '');
        } while ($line !== 'locked' && $line !== '');
        assert($line === 'locked');
        curl_setopt_array(
            $curl,
            [
                CURLOPT_CUSTOMREQUEST => $method,
                CURLOPT_POSTFIELDS => json_encode($body, JSON_THROW_ON_ERROR),
                CURLOPT_HTTPHEADER => [
                    'Accept: application/json',
                    'Content-Type: application/json',
                    'Authorization: Bearer ' . $token
                ],
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_HEADER => true,
                CURLOPT_TIMEOUT => 20
            ]
        );
        curl_multi_add_handle($multi, $curl);
        $deadline = microtime(true) + 8;
        do {
            curl_multi_exec($multi, $running);
            $waiting = (int) ticket09Sql(
                ("SELECT count(*) FROM pg_stat_activity WHERE datname='articles' " .
                    "AND usename='articles_app' AND wait_event_type='Lock'")
            );
            if ($waiting === 0 && $running > 0) {
                curl_multi_select($multi, 0.02);
            }
        } while ($waiting === 0 && $running > 0 && microtime(true) < $deadline);
        assert($waiting > 0, 'HTTP 未等待隔離資料列鎖');

        // 狀態變化先提交，等待中的請求須使用提交後內容判定結果。
        fwrite($pipes[0], $transition . ";\nCOMMIT;\n");
        fclose($pipes[0]);
        stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        assert(proc_close($process) === 0, 'fixture 提交失敗：' . $error);
        do {
            curl_multi_exec($multi, $running);
            if ($running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($running > 0);
        $raw = curl_multi_getcontent($curl);
        assert(is_string($raw) && curl_errno($curl) === 0, curl_error($curl));
        $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

        return [
            curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
            json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR),
            substr($raw, 0, $length)
        ];
    } finally {
        // 中途失敗必須釋放 fixture 鎖，以免後續測試被本測試阻塞。
        if (is_resource($pipes[0])) {
            fwrite($pipes[0], "ROLLBACK;\n");
            fclose($pipes[0]);
        }
        foreach ([$pipes[1], $pipes[2]] as $pipe) {
            if (is_resource($pipe)) {
                fclose($pipe);
            }
        }
        if (is_resource($process)) {
            proc_close($process);
        }
        curl_multi_remove_handle($multi, $curl);
        curl_multi_close($multi);
        curl_close($curl);
    }
}

if ($ticket14) {
    ticket09AssertTarget();
}

[$authorId, $author, $authorEmail] = ticket09Identity($subjectBase, '作者');
[, $other] = ticket09Identity($subjectBase, '其他人');
[$adminId, $admin] = ticket09Identity($subjectBase, '管理員', true);
[$laravelActorId, $laravelActor] = ticket09Identity($laravelBase, 'Laravel 對照');
$laravelLogin = ticket09Request(
    $laravelBase,
    'POST',
    '/api/auth/login',
    ['email' => $authorEmail, 'password' => 'Abc1234']
);
ticket09Check($laravelLogin, 200);
$laravelAuthor = $laravelLogin[1]['data']['access_token'];

// 兩個後端的巢狀、重複、型態、JSON 錯誤須逐欄同文案，僅 log_id 可不同。
foreach (
    [
    ['POST', '/api/articles', ['title' => 7, 'body' => '   '], 422],
    ['POST', '/api/articles', '{', 400],
    ['GET', '/api/me/articles?page=1&page=2&per_page=20', null, 422],
    ['GET', '/api/me/comments?page[]=1&per_page=20', null, 422],
    ['PUT', '/api/articles/nope', ['title' => '合法', 'body' => '合法', 'version' => 1], 422],
    ] as [$method, $path, $body, $status]
) {
    $subjectError = ticket09Request($subjectBase, $method, $path, $body, $author);
    $laravelError = ticket09Request($laravelBase, $method, $path, $body, $laravelActor);
    assert($subjectError[0] === $status && $laravelError[0] === $status);
    unset($subjectError[1]['log_id'], $laravelError[1]['log_id']);
    assert(
        $subjectError[1] === $laravelError[1],
        '跨後端錯誤不一致：' . json_encode([$subjectError[1], $laravelError[1]], JSON_UNESCAPED_UNICODE)
    );
}

if ($ticket14) {
    // 原始 JSON 保留重複鍵；兩後端都應採最後一個 title，且各自寫入正確作者與來源。
    $duplicateTitleBody = '{"title":"A","title":"B","body":"ok"}';
    $subjectCreated = ticket09Request($subjectBase, 'POST', '/api/articles', $duplicateTitleBody, $author);
    $laravelCreated = ticket09Request($laravelBase, 'POST', '/api/articles', $duplicateTitleBody, $laravelActor);
    ticket09Check($subjectCreated, 201);
    ticket09Check($laravelCreated, 201);
    unset($subjectCreated[1]['log_id'], $laravelCreated[1]['log_id']);
    assert($subjectCreated[1] === $laravelCreated[1] && $subjectCreated[1]['data'] === []);

    // 建立回應沒有 ID，依契約讀回各自的最新文章，再核對 SQL 與另一後端的單筆資料。
    foreach (
        [
            [$subjectBase, $author, $authorId, $subjectBackend, $laravelBase],
            [$laravelBase, $laravelActor, $laravelActorId, 'laravel', $subjectBase],
        ] as [$base, $token, $expectedAuthorId, $expectedBackend, $otherBase]
    ) {
        $ownArticles = ticket09Request($base, 'GET', '/api/me/articles?page=1&per_page=20', null, $token);
        ticket09Check($ownArticles, 200);
        $createdArticle = $ownArticles[1]['data']['list'][0];
        $duplicateArticleId = $createdArticle['id'];
        assert($createdArticle['title'] === 'B' && $createdArticle['author']['id'] === $expectedAuthorId);
        assert($createdArticle['created_backend'] === $expectedBackend && $createdArticle['version'] === 1);
        $storedArticle = json_decode(
            ticket09Sql(
                'SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $duplicateArticleId
            ),
            true,
            512,
            JSON_THROW_ON_ERROR
        );
        assert(
            $storedArticle['title'] === 'B' && $storedArticle['author_id'] === $expectedAuthorId
                && $storedArticle['created_backend'] === $expectedBackend
        );
        $crossRead = ticket09Request($otherBase, 'GET', '/api/articles/' . $duplicateArticleId);
        ticket09Check($crossRead, 200);
        assert($crossRead[1]['data'] === $createdArticle);
        if ($base === $subjectBase) {
            $duplicateSubjectId = $duplicateArticleId;
        }
    }

    // 重複未知欄位仍只回共同未知欄位錯誤；重複 version 的最後值若型態錯誤也須拒絕。
    foreach (
        [
            [
                'POST',
                '/api/articles',
                '{"title":"有效","body":"ok","unknown":"x","unknown":"y"}',
                ['unknown' => ['不支援此欄位']]
            ],
            [
                'PUT',
                '/api/articles/' . $duplicateSubjectId,
                '{"title":"有效","body":"ok","version":1,"version":"bad"}',
                ['version' => ['欄位型態不正確']]
            ],
        ] as [$method, $path, $body, $errors]
    ) {
        $subjectError = ticket09Request($subjectBase, $method, $path, $body, $author);
        $laravelError = ticket09Request($laravelBase, $method, $path, $body, $laravelAuthor);
        ticket09Check($subjectError, 422, 'E0001', $errors);
        ticket09Check($laravelError, 422, 'E0001', $errors);
        unset($subjectError[1]['log_id'], $laravelError[1]['log_id']);
        assert($subjectError[1] === $laravelError[1]);
    }
}

// 管理員建立自己的文章／留言時走作者恢復權，不能誤套管理員接管他人內容。
ticket09Write($subjectBase, 'POST', '/api/articles', ['title' => '管理員自己的文章', 'body' => '自己的內容'], $admin, 201);
$adminArticles = ticket09Request($subjectBase, 'GET', '/api/me/articles?page=1&per_page=20', null, $admin);
ticket09Check($adminArticles, 200);
$adminArticle = $adminArticles[1]['data']['list'][0];
$adminArticleId = $adminArticle['id'];
assert($adminArticle['author']['id'] === $adminId && $adminArticle['version'] === 1 && ! $adminArticle['can_restore']);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $adminArticleId . '/visibility',
    ['visibility' => 'hidden', 'version' => 1],
    $admin
);
$adminHidden = ticket09Request($subjectBase, 'GET', '/api/articles/' . $adminArticleId, null, $admin);
ticket09Check($adminHidden, 200);
assert(
    $adminHidden[1]['data']['visibility'] === 'hidden' && $adminHidden[1]['data']['version'] === 2
        && $adminHidden[1]['data']['can_restore']
);
if ($ticket14) {
    // 作者可編修自己的下架文章，編修不得讓文章公開或轉移恢復權。
    ticket09Write(
        $subjectBase,
        'PUT',
        '/api/articles/' . $adminArticleId,
        ['title' => '下架後編修', 'body' => '自己的內容', 'version' => 2],
        $admin
    );
    $adminHidden = ticket09Request($subjectBase, 'GET', '/api/articles/' . $adminArticleId, null, $admin);
    ticket09Check($adminHidden, 200);
    assert(
        $adminHidden[1]['data']['visibility'] === 'hidden' && $adminHidden[1]['data']['version']
            === 3 && $adminHidden[1]['data']['can_restore']
    );
}
$adminArticleRow = json_decode(
    ticket09Sql('SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $adminArticleId),
    true,
    512,
    JSON_THROW_ON_ERROR
);
assert($adminArticleRow['restore_control'] === 'author');
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $adminArticleId . '/visibility',
    ['visibility' => 'visible', 'version' => $ticket14 ? 3 : 2],
    $admin
);
$adminRestored = ticket09Request($subjectBase, 'GET', '/api/articles/' . $adminArticleId, null, $admin);
ticket09Check($adminRestored, 200);
assert(
    $adminRestored[1]['data']['visibility'] === 'visible' && $adminRestored[1]['data']['version']
        === ($ticket14 ? 4 : 3) && ! $adminRestored[1]['data']['can_restore']
);
$adminArticleRow = json_decode(
    ticket09Sql('SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $adminArticleId),
    true,
    512,
    JSON_THROW_ON_ERROR
);
assert($adminArticleRow['restore_control'] === null);
ticket09Write(
    $subjectBase,
    'POST',
    '/api/articles/' . $adminArticleId . '/comments',
    ['body' => '管理員自己的留言'],
    $admin,
    201
);
$adminComments = ticket09Request($subjectBase, 'GET', '/api/me/comments?page=1&per_page=20', null, $admin);
ticket09Check($adminComments, 200);
$adminComment = $adminComments[1]['data']['list'][0];
$adminCommentId = $adminComment['id'];
assert($adminComment['author']['id'] === $adminId && $adminComment['version'] === 1 && ! $adminComment['can_restore']);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $adminCommentId . '/visibility',
    ['visibility' => 'hidden', 'version' => 1],
    $admin
);
$adminCommentHidden = ticket09Request($subjectBase, 'GET', '/api/comments/' . $adminCommentId, null, $admin);
ticket09Check($adminCommentHidden, 200);
assert(
    $adminCommentHidden[1]['data']['visibility'] === 'hidden'
        && $adminCommentHidden[1]['data']['version'] === 2 && $adminCommentHidden[1]['data']['can_restore']
);
$adminCommentRow = json_decode(
    ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $adminCommentId),
    true,
    512,
    JSON_THROW_ON_ERROR
);
assert($adminCommentRow['restore_control'] === 'author');
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $adminCommentId . '/visibility',
    ['visibility' => 'visible', 'version' => 2],
    $admin
);
$adminCommentRestored = ticket09Request($subjectBase, 'GET', '/api/comments/' . $adminCommentId, null, $admin);
ticket09Check($adminCommentRestored, 200);
assert(
    $adminCommentRestored[1]['data']['visibility'] === 'visible'
        && $adminCommentRestored[1]['data']['version'] === 3 && ! $adminCommentRestored[1]['data']['can_restore']
);
$adminCommentRow = json_decode(
    ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $adminCommentId),
    true,
    512,
    JSON_THROW_ON_ERROR
);
assert($adminCommentRow['restore_control'] === null);

// 受測後端建立後由 Laravel 讀；來源與作者不可由請求冒填。
ticket09Write($subjectBase, 'POST', '/api/articles', ['title' => '跨後端文章', 'body' => "原文\r\n保留"], $author, 201);
$mine = ticket09Request($subjectBase, 'GET', '/api/me/articles?page=1&per_page=20', null, $author);
ticket09Check($mine, 200);
$article = $mine[1]['data']['list'][0];
assert(
    $article['author']['id'] === $authorId && $article['created_backend'] === $subjectBackend
        && $article['version'] === 1
);
$articleId = $article['id'];
$laravelRead = ticket09Request($laravelBase, 'GET', '/api/articles/' . $articleId);
ticket09Check($laravelRead, 200);
assert($laravelRead[1]['data'] === $article);
ticket09Write(
    $laravelBase,
    'POST',
    '/api/articles',
    ['title' => 'Laravel 建立供受測後端讀取', 'body' => '共同資料'],
    $laravelActor,
    201
);
$laravelMine = ticket09Request($laravelBase, 'GET', '/api/me/articles?page=1&per_page=20', null, $laravelActor);
ticket09Check($laravelMine, 200);
$laravelArticle = $laravelMine[1]['data']['list'][0];
assert($laravelArticle['created_backend'] === 'laravel');
$subjectRead = ticket09Request($subjectBase, 'GET', '/api/articles/' . $laravelArticle['id']);
ticket09Check($subjectRead, 200);
assert($subjectRead[1]['data'] === $laravelArticle);
ticket09Check(
    ticket09Request(
        $subjectBase,
        'POST',
        '/api/articles',
        ['title' => '合法', 'body' => '內文', 'created_backend' => 'laravel'],
        $author
    ),
    422,
    'E0001',
    ['created_backend' => ['不支援此欄位']]
);

// 可讀但非作者先拒絕操作，版本過期與同值寫入不改資料列。
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '改寫', 'body' => '內文', 'version' => 1],
    $other,
    403,
    'E0003'
);
if ($ticket14) {
    // 可讀目標仍先檢查操作權，非作者的過期版本不得洩漏目前版本。
    ticket09Write(
        $subjectBase,
        'PUT',
        '/api/articles/' . $articleId,
        ['title' => '改寫', 'body' => '內文', 'version' => 999],
        $other,
        403,
        'E0003'
    );
}
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '跨後端文章', 'body' => "原文\r\n保留", 'version' => 1],
    $author
);
$before = ticket09Sql('SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $articleId);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '跨後端文章', 'body' => "原文\r\n保留", 'version' => 1],
    $author
);
assert(ticket09Sql('SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $articleId) === $before);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '新版', 'body' => '內文', 'version' => 1],
    $author
);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '新版', 'body' => '內文', 'version' => 1],
    $author,
    409,
    'E0201'
);
if ($ticket14) {
    // 管理員可接管下架，但不得藉角色改寫另一人的內文或建立來源。
    ticket09Write(
        $subjectBase,
        'PUT',
        '/api/articles/' . $articleId,
        ['title' => '管理員改寫', 'body' => '不允許', 'version' => 2],
        $admin,
        403,
        'E0003'
    );
    assert(ticket09Sql('SELECT created_backend FROM articles WHERE id = ' . $articleId) === $subjectBackend);
}

// 父文章公開才能留言；作者下架後他人不得由單則詳情推知隱藏內容。
ticket09Write($subjectBase, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '我的留言'], $author, 201);
ticket09Write($subjectBase, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '其他人的留言'], $other, 201);
if ($ticket14) {
    // 建立來源只能由實際後端填入，不能讓呼叫者冒填。
    ticket09Check(
        ticket09Request(
            $subjectBase,
            'POST',
            '/api/articles/' . $articleId . '/comments',
            ['body' => '冒填來源', 'created_backend' => 'laravel'],
            $author
        ),
        422,
        'E0001',
        ['created_backend' => ['不支援此欄位']]
    );
}
$otherComments = ticket09Request($subjectBase, 'GET', '/api/me/comments?page=1&per_page=20', null, $other);
ticket09Check($otherComments, 200);
$otherCommentId = $otherComments[1]['data']['list'][0]['id'];
$comments = ticket09Request($subjectBase, 'GET', '/api/me/comments?page=1&per_page=20', null, $author);
ticket09Check($comments, 200);
$comment = $comments[1]['data']['list'][0];
assert($comment['article_id'] === $articleId && $comment['created_backend'] === $subjectBackend);
$commentId = $comment['id'];
if ($ticket14) {
    // 管理員不能編修他人的留言內文，權限檢查也必須早於版本判斷。
    ticket09Write(
        $subjectBase,
        'PUT',
        '/api/comments/' . $commentId,
        ['body' => '管理員改寫', 'version' => 1],
        $admin,
        403,
        'E0003'
    );
}
// 同一位共用資料作者在兩後端各自登入，才能有效驗證雙向單則留言讀取與來源不變。
$subjectCommentRead = ticket09Request($subjectBase, 'GET', '/api/comments/' . $commentId, null, $author);
$laravelCommentRead = ticket09Request($laravelBase, 'GET', '/api/comments/' . $commentId, null, $laravelAuthor);
ticket09Check($subjectCommentRead, 200);
ticket09Check($laravelCommentRead, 200);
assert(
    $subjectCommentRead[1]['data'] === $laravelCommentRead[1]['data']
        && $laravelCommentRead[1]['data']['created_backend'] === $subjectBackend
);
ticket09Write(
    $laravelBase,
    'POST',
    '/api/articles/' . $articleId . '/comments',
    ['body' => 'Laravel 建立的留言'],
    $laravelAuthor,
    201
);
$laravelComments = ticket09Request($laravelBase, 'GET', '/api/me/comments?page=1&per_page=20', null, $laravelAuthor);
ticket09Check($laravelComments, 200);
$laravelComment = $laravelComments[1]['data']['list'][0];
$subjectLaravelComment = ticket09Request($subjectBase, 'GET', '/api/comments/' . $laravelComment['id'], null, $author);
ticket09Check($subjectLaravelComment, 200);
assert($subjectLaravelComment[1]['data'] === $laravelComment && $laravelComment['created_backend'] === 'laravel');
ticket09Check(ticket09Request($laravelBase, 'GET', '/api/comments/' . $commentId, null, $author), 401, 'E0002');
ticket09Check(ticket09Request($subjectBase, 'GET', '/api/comments/' . $commentId, null, $other), 403, 'E0003');
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'hidden', 'version' => 2],
    $author
);
ticket09Check(ticket09Request($subjectBase, 'GET', '/api/articles/' . $articleId, null, $other), 404, 'E0004');
ticket09Check(ticket09Request($subjectBase, 'GET', '/api/comments/' . $commentId, null, $other), 404, 'E0004');
if ($ticket14) {
    // 他人不可讀的下架文章，寫入也須維持 404，不能先暴露操作權或版本結果。
    ticket09Write(
        $subjectBase,
        'PUT',
        '/api/articles/' . $articleId,
        ['title' => '不允許', 'body' => '不允許', 'version' => 999],
        $other,
        404,
        'E0004'
    );
}
ticket09Check(ticket09Request($subjectBase, 'GET', '/api/comments/' . $otherCommentId, null, $other), 200);
ticket09Check(
    ticket09Request(
        $subjectBase,
        'GET',
        '/api/articles/' . $articleId . '/comments?page=1&per_page=20',
        null,
        $other
    ),
    404,
    'E0004'
);
ticket09Check(ticket09Request($subjectBase, 'GET', '/api/comments/' . $commentId, null, $author), 200);
ticket09Write(
    $subjectBase,
    'POST',
    '/api/articles/' . $articleId . '/comments',
    ['body' => '不允許'],
    $author,
    403,
    'E0003'
);
ticket09Write(
    $subjectBase,
    'POST',
    '/api/articles/' . $articleId . '/comments',
    ['body' => '不允許'],
    $other,
    404,
    'E0004'
);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId,
    ['body' => '不允許', 'version' => 1],
    $author,
    403,
    'E0003'
);

// 管理員接管他人自行下架時即使同為 hidden 也增加版本並獨占恢復權。
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'hidden', 'version' => 3],
    $admin
);
if ($ticket14) {
    assert(ticket09Sql('SELECT restore_control FROM articles WHERE id = ' . $articleId) === 'admin');
}
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'visible', 'version' => 4],
    $author,
    403,
    'E0003'
);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'visible', 'version' => 4],
    $admin
);
if ($ticket14) {
    // 正確版本的留言同值編修不應增加版本或更新時間；過期同值則必須 409。
    $commentBefore = ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $commentId);
    ticket09Write($subjectBase, 'PUT', '/api/comments/' . $commentId, ['body' => '我的留言', 'version' => 1], $author);
    assert(ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $commentId) === $commentBefore);
}
ticket09Write($subjectBase, 'PUT', '/api/comments/' . $commentId, ['body' => '新版留言', 'version' => 1], $author);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId,
    ['body' => '新版留言', 'version' => 1],
    $author,
    409,
    'E0201'
);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId . '/visibility',
    ['visibility' => 'hidden', 'version' => 2],
    $author
);
$commentBefore = ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $commentId);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId . '/visibility',
    ['visibility' => 'hidden', 'version' => 3],
    $author
);
assert(ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $commentId) === $commentBefore);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId . '/visibility',
    ['visibility' => 'hidden', 'version' => 3],
    $admin
);
if ($ticket14) {
    assert(ticket09Sql('SELECT restore_control FROM comments WHERE id = ' . $commentId) === 'admin');
    assert(ticket09Sql('SELECT created_backend FROM comments WHERE id = ' . $commentId) === $subjectBackend);
}
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId . '/visibility',
    ['visibility' => 'visible', 'version' => 4],
    $author,
    403,
    'E0003'
);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/comments/' . $commentId . '/visibility',
    ['visibility' => 'visible', 'version' => 4],
    $admin
);

// 留言更新等待父文章鎖時若文章先下架，必須按新狀態拒絕且不改留言。
$commentBefore = ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $commentId);
$race = ticket09Race(
    'FROM articles WHERE id = ' . $articleId . ' FOR UPDATE',
    ("UPDATE articles SET visibility = 'hidden', restore_control = " .
        "'author', version = version + 1, updated_at = clock_timestamp() " .
        "WHERE id = ") . $articleId,
    'PUT',
    '/api/comments/' . $commentId,
    ['body' => '競爭寫入', 'version' => 5],
    $author
);
ticket09Check($race, 403, 'E0003');
assert(ticket09Sql('SELECT row_to_json(t)::text FROM comments t WHERE id = ' . $commentId) === $commentBefore);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'visible', 'version' => 6],
    $author
);

// 預檢允許 Vue 的 PUT；撤銷 session 先提交時，等待中的受保護寫入必須拒絕。
$preflight = ticket09Request(
    $subjectBase,
    'OPTIONS',
    '/api/articles/' . $articleId,
    null,
    null,
    $ticket14 ? ['Access-Control-Request-Method: PUT',
        'Access-Control-Request-Headers: authorization,content-type'] : []
);
if ($ticket14) {
    // 真正的瀏覽器預檢需允許 PUT 與授權／JSON 標頭，不能以一般 OPTIONS 的結果代替。
    assert($preflight[0] === 204);
    assert(stripos($preflight[2], 'Access-Control-Allow-Origin: http://localhost:8080') !== false);
    assert(preg_match('/^Access-Control-Allow-Methods:[^\r\n]*\bPUT\b/im', $preflight[2]) === 1);
    assert(preg_match('/^Access-Control-Allow-Headers:[^\r\n]*\bauthorization\b/im', $preflight[2]) === 1);
    assert(preg_match('/^Access-Control-Allow-Headers:[^\r\n]*\bcontent-type\b/im', $preflight[2]) === 1);
} else {
    assert($preflight[0] === 204 && str_contains($preflight[2], 'GET, POST, PUT, OPTIONS'));
}
$articleBefore = ticket09Sql('SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $articleId);
$race = ticket09Race(
    'FROM login_sessions WHERE account_id = ' . $authorId . " AND backend_id = '" . $subjectBackend
        . "' AND revoked_at IS NULL FOR UPDATE",
    ("UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE " .
        "account_id = ") . $authorId . " AND backend_id = '" . $subjectBackend . "' AND revoked_at IS NULL",
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '不能寫', 'body' => '不能寫', 'version' => 7],
    $author
);
ticket09Check($race, 401, 'E0002');
assert(ticket09Sql('SELECT row_to_json(t)::text FROM articles t WHERE id = ' . $articleId) === $articleBefore);
ticket09Write(
    $subjectBase,
    'PUT',
    '/api/articles/' . $articleId,
    ['title' => '不能寫', 'body' => '不能寫', 'version' => 7],
    $author,
    401,
    'E0002'
);

echo $ticket14 ? "Ticket 14 ASP.NET author HTTP: PASS\n" : "Ticket 09 Symfony author HTTP: PASS\n";
