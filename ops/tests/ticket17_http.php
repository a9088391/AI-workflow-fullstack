<?php

declare(strict_types=1);

// 寫入前驗證 Compose 解析結果；測試只接受獨立 volume、密鑰及指定本機埠。
if (
    getenv('COMPOSE_PROJECT_NAME') !== 'ticket17_test'
    || getenv('COMPOSE_FILE') !== 'compose.yaml:ops/tests/ticket17_compose.yaml'
    || getenv('COMPOSE_PROFILES') !== 'django'
) {
    throw new RuntimeException('必須使用 ticket17_test 與 ticket17_compose.yaml 隔離設定');
}
$configuration = json_decode((string) shell_exec('docker compose config --format json'), true, 512, JSON_THROW_ON_ERROR);
if (
    $configuration['name'] !== 'ticket17_test'
    || $configuration['volumes']['postgres_data']['name'] !== 'ticket17_test_postgres_data'
    || ($configuration['services']['django']['ports'][0]['published'] ?? null) !== '8084'
    || ($configuration['services']['laravel']['ports'][0]['published'] ?? null) !== '8081'
    || ! str_contains($configuration['services']['django']['volumes'][0]['source'] ?? '', '/.local/ticket17-backend/')
) {
    throw new RuntimeException('隔離 Compose 解析結果不符，禁止建立 fixture');
}

$django = 'http://localhost:8084';
$laravel = 'http://localhost:8081';

/** 真實 HTTP 回傳狀態、JSON 與原始 Body；字串 Body 保留 JSON 整數原文以驗證解析邊界。 */
function t17Request(
    string $base,
    string $method,
    string $path,
    array|string|null $body = null,
    ?string $token = null
): array {
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer ' . $token;
    }
    $curl = curl_init($base . $path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $headers,
        CURLOPT_POSTFIELDS => $body === null ? '' : (is_string($body) ? $body : json_encode($body, JSON_THROW_ON_ERROR)),
        CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException('HTTP failed: ' . curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
    return [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, $length)];
}

/** 嚴格檢查欄位、狀態與錯誤文案，避免只檢查 HTTP status。 */
function t17Check(array $result, int $status, ?string $code = null, ?array $errors = null): array
{
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API',
        'E0201' => '內容已更新，請確認最新內容後再送出'];
    $response = $result[1];
    if ($result[0] !== $status || $response['success'] !== ($code === null)) {
        throw new RuntimeException('HTTP status/success mismatch: ' . json_encode([$result[0], $response['error_code'] ?? null]));
    }
    if ($code === null) {
        if ($response['message'] !== 'OK' || ! is_array($response['data'])) {
            throw new RuntimeException('Success envelope mismatch');
        }
    } elseif (
        ($response['error_code'] ?? null) !== $code || ($response['message'] ?? null) !== $messages[$code]
        || ($response['errors'] ?? null) !== $errors
        || ! is_string($response['log_id'] ?? null)
        || preg_match('/\A[0-9a-f-]{36}\z/', $response['log_id']) !== 1
    ) {
        throw new RuntimeException('Error envelope mismatch: ' . json_encode($response));
    }
    return $response;
}

/** 唯一 fixture 使用隔離 owner 連線；SQL 不接收使用者輸入。 */
function t17Sql(string $sql): string
{
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At', '-c', $sql],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('Cannot open isolated psql');
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('Isolated SQL failed: ' . $error);
    }
    return trim($output);
}

/** 新帳號由公開 API 建立及登入，管理員 fixture 僅改本隔離資料庫的角色。 */
function t17Member(string $base, string $name, bool $admin = false): array
{
    $email = 't17-' . bin2hex(random_bytes(6)) . '@example.test';
    t17Check(t17Request($base, 'POST', '/api/auth/register', ['email' => $email, 'password' => 'Abc1234', 'nickname' => $name]), 201);
    if ($admin) {
        t17Sql("UPDATE accounts SET role='admin' WHERE email='" . $email . "'");
    }
    $login = t17Check(t17Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']), 200);
    return [$login['data']['user']['id'], $login['data']['access_token'], $email];
}

/** 我的列表以新 fixture 首項找 ID，避免發表 API 私自回傳未約定 ID。 */
function t17Mine(string $base, string $kind, string $token): array
{
    return t17Check(t17Request($base, 'GET', '/api/me/' . $kind . '?page=1&per_page=20', null, $token), 200)['data'];
}

/** 非空成功寫入必須只回 data:{}，建立與編修均遵守此契約。 */
function t17Write(string $base, string $method, string $path, array $body, string $token, int $status = 200): void
{
    $result = t17Request($base, $method, $path, $body, $token);
    t17Check($result, $status);
    if ($result[1]['data'] !== [] || ! (json_decode($result[2], false, 512, JSON_THROW_ON_ERROR)->data instanceof stdClass)) {
        throw new RuntimeException('Write did not return data:{}');
    }
}

/** 比較列原始值，確認失敗及同值操作不更動時間、版本或控制者。 */
function t17Snapshot(string $table, int $id): string
{
    return t17Sql("SELECT row_to_json(t)::text FROM $table t WHERE id=$id");
}

/** 先鎖帳號並觀察兩後端實際等待，再提交狀態轉移以裁決寫入順序。 */
function t17Race(string $lockTable, int $lockId, array $requests, string $beforeCommit = ''): array
{
    if (! in_array($lockTable, ['accounts', 'articles'], true)) {
        throw new RuntimeException('Unsupported isolated race lock');
    }
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-qAt'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('Cannot open isolated race fixture');
    }
    $multi = curl_multi_init();
    $handles = [];
    try {
        fwrite($pipes[0], "BEGIN;\nDO \$\$ BEGIN PERFORM 1 FROM $lockTable WHERE id=$lockId FOR UPDATE; END \$\$;\nSELECT 'locked';\n");
        fflush($pipes[0]);
        if (trim((string) fgets($pipes[1])) !== 'locked') {
            throw new RuntimeException('Race fixture did not lock account');
        }
        foreach ($requests as [$base, $method, $path, $body, $token]) {
            $curl = curl_init($base . $path);
            curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_POSTFIELDS => json_encode($body, JSON_THROW_ON_ERROR),
                CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json', 'Authorization: Bearer ' . $token],
                CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
            curl_multi_add_handle($multi, $curl);
            $handles[] = $curl;
        }
        // 不靠猜測延遲；確認每個請求已在 PostgreSQL 行鎖上等待。
        $deadline = microtime(true) + 10;
        do {
            curl_multi_exec($multi, $running);
            $waiting = (int) t17Sql("SELECT count(*) FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' AND wait_event_type='Lock'");
            if ($waiting < count($requests) && $running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($waiting < count($requests) && $running > 0 && microtime(true) < $deadline);
        if ($waiting < count($requests)) {
            throw new RuntimeException('HTTP did not wait on account row lock');
        }
        fwrite($pipes[0], $beforeCommit . "\nCOMMIT;\n");
        fclose($pipes[0]);
        stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        if (proc_close($process) !== 0) {
            throw new RuntimeException('Race fixture commit failed: ' . $error);
        }
        do {
            curl_multi_exec($multi, $running);
            if ($running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($running > 0);
        $responses = [];
        foreach ($handles as $curl) {
            $raw = curl_multi_getcontent($curl);
            if (! is_string($raw) || curl_errno($curl) !== 0) {
                throw new RuntimeException('Race HTTP failed: ' . curl_error($curl));
            }
            $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
            $responses[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
                json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, $length)];
        }
        return $responses;
    } finally {
        foreach ($handles as $curl) {
            curl_multi_remove_handle($multi, $curl);
            curl_close($curl);
        }
        curl_multi_close($multi);
        if (is_resource($process)) {
            // 例外時先回滾並關閉 owner 連線，不能留下持鎖交易。
            if (isset($pipes[0]) && is_resource($pipes[0])) {
                fwrite($pipes[0], "ROLLBACK;\n");
                fclose($pipes[0]);
            }
            foreach ([1, 2] as $index) {
                if (isset($pipes[$index]) && is_resource($pipes[$index])) {
                    fclose($pipes[$index]);
                }
            }
            proc_close($process);
        }
    }
}

// 每次執行使用新帳號；跨後端互讀不共享 JWT，只共享資料表。
[$authorId, $authorToken] = t17Member($django, 'Django 作者');
[$otherId, $otherToken] = t17Member($django, '其他作者');
[$adminId, $adminToken] = t17Member($django, '管理員', true);
[$laravelId, $laravelToken] = t17Member($laravel, 'Laravel 作者');

// 管理員對自己的內容仍按作者規則操作，不因角色不同失去編修或自行恢復權。
t17Write($django, 'POST', '/api/articles', ['title' => '管理員自有文章', 'body' => '自有內文'], $adminToken, 201);
$adminArticleId = t17Mine($django, 'articles', $adminToken)['list'][0]['id'];
t17Write($django, 'PUT', '/api/articles/' . $adminArticleId, ['title' => '管理員編修', 'body' => '自有內文', 'version' => 1], $adminToken);
t17Write($django, 'POST', '/api/articles/' . $adminArticleId . '/comments', ['body' => '管理員自有留言'], $adminToken, 201);
$adminCommentId = t17Mine($django, 'comments', $adminToken)['list'][0]['id'];
t17Write($django, 'PUT', '/api/comments/' . $adminCommentId, ['body' => '管理員編修留言', 'version' => 1], $adminToken);
t17Write($django, 'PUT', '/api/comments/' . $adminCommentId . '/visibility', ['visibility' => 'hidden', 'version' => 2], $adminToken);
t17Write($django, 'PUT', '/api/comments/' . $adminCommentId . '/visibility', ['visibility' => 'visible', 'version' => 3], $adminToken);

t17Write($django, 'POST', '/api/articles', ['title' => '跨端文章', 'body' => "首行\r\n第二行"], $authorToken, 201);
$mine = t17Mine($django, 'articles', $authorToken);
$articleId = $mine['list'][0]['id'];
if (
    $mine['pagination']['total'] !== 1 || $mine['list'][0]['created_backend'] !== 'django'
    || $mine['list'][0]['body'] !== "首行\r\n第二行" || $mine['list'][0]['version'] !== 1
) {
    throw new RuntimeException('Django article/list mismatch');
}
$article = t17Check(t17Request($laravel, 'GET', '/api/articles/' . $articleId, null, $laravelToken), 200)['data'];
if ($article['created_backend'] !== 'django' || $article['author']['id'] !== $authorId) {
    throw new RuntimeException('Laravel cannot read Django article');
}
t17Write($laravel, 'POST', '/api/articles', ['title' => 'Laravel 來源', 'body' => '反向讀取'], $laravelToken, 201);
$laravelArticleId = t17Mine($laravel, 'articles', $laravelToken)['list'][0]['id'];
if (t17Check(t17Request($django, 'GET', '/api/articles/' . $laravelArticleId), 200)['data']['created_backend'] !== 'laravel') {
    throw new RuntimeException('Django cannot read Laravel article');
}

// 作者編修與版本先後：可讀他人公開內容仍無操作權，過期同值也必須衝突。
t17Check(t17Request($django, 'PUT', '/api/articles/' . $articleId, ['title' => 'X', 'body' => 'X', 'version' => 1], $otherToken), 403, 'E0003');
t17Check(t17Request($django, 'PUT', '/api/articles/' . $articleId, ['title' => 'X', 'body' => 'X', 'version' => 1], $adminToken), 403, 'E0003');
$before = t17Snapshot('articles', $articleId);
t17Write($django, 'PUT', '/api/articles/' . $articleId, ['title' => '跨端文章', 'body' => "首行\r\n第二行", 'version' => 1], $authorToken);
if (t17Snapshot('articles', $articleId) !== $before) {
    throw new RuntimeException('Same-value article update changed row');
}
t17Write($django, 'PUT', '/api/articles/' . $articleId, ['title' => '改版', 'body' => '新內容', 'version' => 1], $authorToken);
t17Check(t17Request($django, 'PUT', '/api/articles/' . $articleId, ['title' => '改版', 'body' => '新內容', 'version' => 1], $authorToken), 409, 'E0201');
if (t17Check(t17Request($laravel, 'GET', '/api/articles/' . $articleId), 200)['data']['version'] !== 2) {
    throw new RuntimeException('Laravel cannot read Django update');
}

// 留言建立、個人列表及獨立詳情，反向來源也須可由另一後端讀取。
t17Write($django, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => 'Django 留言'], $authorToken, 201);
$commentMine = t17Mine($django, 'comments', $authorToken);
$commentId = $commentMine['list'][0]['id'];
if (
    $commentMine['pagination']['total'] !== 1 || $commentMine['list'][0]['article_id'] !== $articleId
    || $commentMine['list'][0]['created_backend'] !== 'django'
) {
    throw new RuntimeException('My comment resource mismatch');
}
if (t17Check(t17Request($laravel, 'GET', '/api/comments/' . $commentId, null, $adminToken), 401, 'E0002')['error_code'] !== 'E0002') {
    throw new RuntimeException('Cross-backend JWT accepted');
}
t17Write($laravel, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => 'Laravel 留言'], $laravelToken, 201);
$laravelCommentId = t17Mine($laravel, 'comments', $laravelToken)['list'][0]['id'];
if (t17Check(t17Request($django, 'GET', '/api/comments/' . $laravelCommentId, null, $adminToken), 200)['data']['created_backend'] !== 'laravel') {
    throw new RuntimeException('Django cannot read Laravel comment');
}
t17Check(t17Request($django, 'GET', '/api/comments/' . $commentId, null, $otherToken), 403, 'E0003');
t17Check(t17Request($django, 'PUT', '/api/comments/' . $commentId, ['body' => 'X', 'version' => 1], $otherToken), 403, 'E0003');
t17Check(t17Request($django, 'PUT', '/api/comments/' . $commentId, ['body' => 'X', 'version' => 1], $adminToken), 403, 'E0003');
$before = t17Snapshot('comments', $commentId);
t17Write($django, 'PUT', '/api/comments/' . $commentId, ['body' => 'Django 留言', 'version' => 1], $authorToken);
if (t17Snapshot('comments', $commentId) !== $before) {
    throw new RuntimeException('Same-value comment update changed row');
}
t17Write($django, 'PUT', '/api/comments/' . $commentId, ['body' => '修改留言', 'version' => 1], $authorToken);
t17Check(t17Request($django, 'PUT', '/api/comments/' . $commentId, ['body' => '修改留言', 'version' => 1], $authorToken), 409, 'E0201');

// 下架父文章後，他人無法讀取父文／留言；作者仍可讀個人留言與獨立詳情。
t17Write($django, 'PUT', '/api/articles/' . $articleId . '/visibility', ['visibility' => 'hidden', 'version' => 2], $authorToken);
t17Check(t17Request($django, 'GET', '/api/articles/' . $articleId, null, $otherToken), 404, 'E0004');
t17Check(t17Request($django, 'GET', '/api/comments/' . $commentId, null, $otherToken), 404, 'E0004');
if (t17Check(t17Request($django, 'GET', '/api/comments/' . $commentId, null, $authorToken), 200)['data']['article_id'] !== $articleId) {
    throw new RuntimeException('Author lost hidden-parent comment');
}
t17Check(t17Request($django, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '不可新增'], $otherToken), 404, 'E0004');
t17Check(t17Request($django, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '不可新增'], $authorToken), 403, 'E0003');
t17Check(t17Request($django, 'PUT', '/api/comments/' . $commentId, ['body' => '不可修改', 'version' => 2], $authorToken), 403, 'E0003');
t17Write($django, 'PUT', '/api/comments/' . $commentId . '/visibility', ['visibility' => 'hidden', 'version' => 2], $authorToken);
if (t17Mine($django, 'comments', $authorToken)['list'][0]['visibility'] !== 'hidden') {
    throw new RuntimeException('My comments omitted individually hidden comment');
}
t17Write($django, 'PUT', '/api/comments/' . $commentId . '/visibility', ['visibility' => 'visible', 'version' => 3], $authorToken);

// 管理員接管後作者即使持過期版本或再次下架，都不能恢復控制權。
t17Write($django, 'PUT', '/api/articles/' . $articleId . '/visibility', ['visibility' => 'hidden', 'version' => 3], $adminToken);
$taken = t17Check(t17Request($django, 'GET', '/api/articles/' . $articleId, null, $authorToken), 200)['data'];
if ($taken['version'] !== 4 || $taken['can_restore'] !== false) {
    throw new RuntimeException('Admin takeover did not change version/control');
}
t17Check(t17Request($django, 'PUT', '/api/articles/' . $articleId . '/visibility', ['visibility' => 'visible', 'version' => 4], $authorToken), 403, 'E0003');
$before = t17Snapshot('articles', $articleId);
t17Write($django, 'PUT', '/api/articles/' . $articleId . '/visibility', ['visibility' => 'hidden', 'version' => 4], $authorToken);
if (t17Snapshot('articles', $articleId) !== $before) {
    throw new RuntimeException('Author repeated hide removed admin control');
}
t17Write($django, 'PUT', '/api/articles/' . $articleId . '/visibility', ['visibility' => 'visible', 'version' => 4], $adminToken);
if (t17Check(t17Request($laravel, 'GET', '/api/articles/' . $articleId), 200)['data']['created_backend'] !== 'django') {
    throw new RuntimeException('created_backend changed after lifecycle');
}

// 管理員對他人留言接管恢復權；作者再下架不得移除管理員限制。
t17Write($django, 'PUT', '/api/comments/' . $commentId . '/visibility', ['visibility' => 'hidden', 'version' => 4], $authorToken);
t17Write($django, 'PUT', '/api/comments/' . $commentId . '/visibility', ['visibility' => 'hidden', 'version' => 5], $adminToken);
$takenComment = t17Check(t17Request($django, 'GET', '/api/comments/' . $commentId, null, $authorToken), 200)['data'];
if ($takenComment['version'] !== 6 || $takenComment['can_restore'] !== false) {
    throw new RuntimeException('Admin comment takeover mismatch');
}
t17Check(t17Request($django, 'PUT', '/api/comments/' . $commentId . '/visibility', ['visibility' => 'visible', 'version' => 6], $authorToken), 403, 'E0003');
t17Write($django, 'PUT', '/api/comments/' . $commentId . '/visibility', ['visibility' => 'visible', 'version' => 6], $adminToken);

// 同一作者分別由 Django/Laravel 登入，兩請求同版並行只能有一個成功。
$laravelAuthorToken = t17Check(t17Request(
    $laravel,
    'POST',
    '/api/auth/login',
    ['email' => t17Sql("SELECT email FROM accounts WHERE id=$authorId"), 'password' => 'Abc1234']
), 200)['data']['access_token'];

// 原始 JSON 保留超大整數的詞法；兩後端應在寫入前給同一錯誤，且不可改動文章。
$beforeInvalidVersion = t17Snapshot('articles', $articleId);
$invalidVersions = [
    '9007199254740992' => '欄位格式或範圍不正確',
    '9223372036854775808' => '欄位格式或範圍不正確',
    '-9223372036854775809' => '欄位格式或範圍不正確',
    '1.0' => '欄位型態不正確',
    '1e0' => '欄位型態不正確',
    '"9223372036854775808"' => '欄位型態不正確',
    'true' => '欄位型態不正確',
    '5,"version":9223372036854775808' => '欄位格式或範圍不正確',
];
foreach ($invalidVersions as $versionJson => $message) {
    $rawBody = '{"title":"改版","body":"新內容","version":' . $versionJson . '}';
    foreach ([[$django, $authorToken], [$laravel, $laravelAuthorToken]] as [$backend, $token]) {
        t17Check(
            t17Request($backend, 'PUT', '/api/articles/' . $articleId, $rawBody, $token),
            422,
            'E0001',
            ['version' => [$message]]
        );
    }
    if (t17Snapshot('articles', $articleId) !== $beforeInvalidVersion) {
        throw new RuntimeException('Invalid JSON version changed article');
    }
}
// 重複鍵仍取最後值；合法同值版本不應因前面的超大整數而誤判或寫入。
$lastVersionWins = '{"title":"改版","body":"新內容","version":9223372036854775808,"version":5}';
foreach ([[$django, $authorToken], [$laravel, $laravelAuthorToken]] as [$backend, $token]) {
    t17Check(t17Request($backend, 'PUT', '/api/articles/' . $articleId, $lastVersionWins, $token), 200);
}
if (t17Snapshot('articles', $articleId) !== $beforeInvalidVersion) {
    throw new RuntimeException('Duplicate JSON version changed same-value article');
}

$race = t17Race('accounts', $authorId, [
    [$django, 'PUT', '/api/articles/' . $articleId, ['title' => '競爭 Django', 'body' => '甲', 'version' => 5], $authorToken],
    [$laravel, 'PUT', '/api/articles/' . $articleId, ['title' => '競爭 Laravel', 'body' => '乙', 'version' => 5], $laravelAuthorToken],
]);
// 將兩後端回應投影成狀態碼後排序，只接受一個成功及一個版本衝突。
$statuses = array_map(static fn (array $result): int => $result[0], $race);
sort($statuses);
if ($statuses !== [200, 409]) {
    throw new RuntimeException('Cross-backend version race mismatch: ' . json_encode($statuses));
}
foreach ($race as $result) {
    t17Check($result, $result[0], $result[0] === 409 ? 'E0201' : null);
}
if (t17Check(t17Request($django, 'GET', '/api/articles/' . $articleId), 200)['data']['version'] !== 6) {
    throw new RuntimeException('Race version did not advance once');
}

// 個人分頁斷言完整 ID 順序、精確 total、超頁及隱藏內容，不靠局部包含判斷。
t17Write($django, 'POST', '/api/articles', ['title' => '第二篇', 'body' => '分頁內容'], $authorToken, 201);
$firstPage = t17Check(t17Request($django, 'GET', '/api/me/articles?page=1&per_page=1', null, $authorToken), 200)['data'];
$secondArticleId = $firstPage['list'][0]['id'];
$secondPage = t17Check(t17Request($django, 'GET', '/api/me/articles?page=2&per_page=1', null, $authorToken), 200)['data'];
$thirdPage = t17Check(t17Request($django, 'GET', '/api/me/articles?page=3&per_page=1', null, $authorToken), 200)['data'];
if (
    $firstPage['pagination'] !== ['page' => 1, 'per_page' => 1, 'total' => 2, 'total_pages' => 2]
    || array_column($firstPage['list'], 'id') !== [$secondArticleId]
    || array_column($secondPage['list'], 'id') !== [$articleId]
    || $thirdPage !== ['list' => [], 'pagination' => ['page' => 3, 'per_page' => 1, 'total' => 2, 'total_pages' => 2]]
) {
    throw new RuntimeException('Article exact pagination mismatch');
}
if (t17Mine($django, 'articles', $otherToken)['pagination']['total'] !== 0) {
    throw new RuntimeException('Other account can list author articles');
}
t17Write($django, 'PUT', '/api/articles/' . $secondArticleId . '/visibility', ['visibility' => 'hidden', 'version' => 1], $authorToken);
$hiddenFirst = t17Check(t17Request($django, 'GET', '/api/me/articles?page=1&per_page=1', null, $authorToken), 200)['data'];
if (
    $hiddenFirst['list'][0]['id'] !== $secondArticleId || $hiddenFirst['list'][0]['visibility'] !== 'hidden'
    || $hiddenFirst['pagination']['total'] !== 2
) {
    throw new RuntimeException('Hidden own article missing from count/order');
}
t17Check(t17Request(
    $django,
    'PUT',
    '/api/articles/' . $secondArticleId,
    ['title' => '錯誤', 'body' => '不可改', 'version' => 1],
    $otherToken
), 404, 'E0004');
t17Check(t17Request(
    $django,
    'PUT',
    '/api/articles/' . $secondArticleId,
    ['title' => '第二篇', 'body' => '分頁內容', 'version' => 1],
    $authorToken
), 409, 'E0201');

t17Write($django, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '第二則留言'], $authorToken, 201);
$commentFirst = t17Check(t17Request($django, 'GET', '/api/me/comments?page=1&per_page=1', null, $authorToken), 200)['data'];
$secondCommentId = $commentFirst['list'][0]['id'];
$commentSecond = t17Check(t17Request($django, 'GET', '/api/me/comments?page=2&per_page=1', null, $authorToken), 200)['data'];
$commentThird = t17Check(t17Request($django, 'GET', '/api/me/comments?page=3&per_page=1', null, $authorToken), 200)['data'];
if (
    $commentFirst['pagination'] !== ['page' => 1, 'per_page' => 1, 'total' => 2, 'total_pages' => 2]
    || array_column($commentFirst['list'], 'id') !== [$secondCommentId]
    || array_column($commentSecond['list'], 'id') !== [$commentId]
    || $commentThird['list'] !== [] || $commentThird['pagination']['total'] !== 2
) {
    throw new RuntimeException('Comment exact pagination mismatch');
}

// 父文章下架先取得文章鎖時，兩後端新增與 Django 內文修改都必須等鎖後依新狀態拒絕。
$commentCountBeforeHide = t17Sql("SELECT count(*) FROM comments WHERE article_id=$articleId");
$parentRace = t17Race('articles', $articleId, [
    [$django, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '不可晚於下架'], $otherToken],
    [$laravel, 'POST', '/api/articles/' . $articleId . '/comments', ['body' => '跨後端不可晚於下架'], $laravelToken],
    [$django, 'PUT', '/api/comments/' . $secondCommentId, ['body' => '不可晚於下架', 'version' => 1], $authorToken],
], "UPDATE articles SET visibility='hidden', restore_control='author', version=version+1, updated_at=clock_timestamp() WHERE id=$articleId;");
t17Check($parentRace[0], 404, 'E0004');
t17Check($parentRace[1], 404, 'E0004');
t17Check($parentRace[2], 403, 'E0003');
if (
    t17Sql("SELECT count(*) FROM comments WHERE article_id=$articleId") !== $commentCountBeforeHide
    || t17Check(t17Request($django, 'GET', '/api/comments/' . $secondCommentId, null, $authorToken), 200)['data']['body'] !== '第二則留言'
) {
    throw new RuntimeException('Hidden parent race changed comments');
}

// 直接驗證 JSON 型態、未知欄位與原始 Query 鍵，不讓框架預設靜默接受。
t17Check(t17Request(
    $django,
    'PUT',
    '/api/comments/' . $commentId,
    ['body' => '合法', 'version' => true],
    $authorToken
), 422, 'E0001', ['version' => ['欄位型態不正確']]);
t17Check(
    t17Request($django, 'POST', '/api/articles', ['title' => ' ', 'body' => '合法'], $authorToken),
    422,
    'E0001',
    ['title' => ['欄位格式或範圍不正確']]
);
t17Check(
    t17Request($django, 'GET', '/api/me/articles?page=1&per_page=1&other=1', null, $authorToken),
    422,
    'E0001',
    ['other' => ['不支援此欄位']]
);
t17Check(
    t17Request($django, 'GET', '/api/comments/not-an-id', null, $authorToken),
    422,
    'E0001',
    ['comment_id' => ['欄位格式或範圍不正確']]
);

// 撤銷在受保護寫入等待帳號列鎖時先提交，請求重驗後須回 401 且無文章副作用。
[$revokedId, $revokedToken] = t17Member($django, '撤銷競爭');
$revokedRace = t17Race(
    'accounts',
    $revokedId,
    [[$django, 'POST', '/api/articles', ['title' => '不可建立', 'body' => '撤銷後寫入'], $revokedToken]],
    "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$revokedId AND backend_id='django' AND revoked_at IS NULL;"
);
t17Check($revokedRace[0], 401, 'E0002');
if (t17Sql("SELECT count(*) FROM articles WHERE author_id=$revokedId") !== '0') {
    throw new RuntimeException('Revoked write committed an article');
}

echo "ticket17 HTTP cross-backend and lifecycle checks passed\n";
