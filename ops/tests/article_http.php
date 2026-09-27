<?php

declare(strict_types=1);

// 只可對 Ticket 03 專用資料庫執行；此測試會用 SQL fixture 調整文章與帳號狀態。
if (! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket03_')) {
    throw new RuntimeException('請使用 ticket03_ 開頭的隔離 Compose project');
}
$articleBase = getenv('API_BASE') ?: 'http://localhost:8081';

/** 經真實 HTTP 傳送原始 JSON，讓認證、解析與公開回應一同受驗證。 */
function articleRequest(string $method, string $path, string $body = '', ?string $token = null): array
{
    global $articleBase;
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer '.$token;
    }
    $curl = curl_init($articleBase.$path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_POSTFIELDS => $body, CURLOPT_HTTPHEADER => $headers, CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 15]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

    return [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR)];
}

/** 比對跨後端共同錯誤碼與精確文案，避免測試只接受 Laravel 原生訊息。 */
function articleCheck(array $result, int $status, ?string $code = null, ?string $field = null, ?string $detail = null): void
{
    assert($result[0] === $status, 'HTTP status expected '.$status.', got '.$result[0]);
    assert($result[1]['success'] === ($code === null));
    if ($code === null) {
        assert($result[1]['message'] === 'OK');

        return;
    }
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入', 'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API', 'E0005' => '系統暫時無法處理，請稍後再試', 'E0102' => '此帳號已停用，請聯絡系統管理員'];
    assert($result[1]['error_code'] === $code && $result[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
    assert(is_string($result[1]['log_id']) && strlen($result[1]['log_id']) === 36);
    if ($field !== null) {
        assert($result[1]['errors'][$field] === [$detail]);
    }
}

/** fixture 只使用隔離 Compose DB；正式行為仍逐一由公開 HTTP 斷言。 */
function articleSql(string $sql): string
{
    $pipes = [];
    $process = proc_open(['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At', '-c', $sql], [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    assert(is_resource($process));
    fclose($pipes[0]);
    $out = stream_get_contents($pipes[1]);
    $err = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 fixture SQL 失敗: '.$err);
    }

    return trim($out);
}

/** 每次測試以新會員取得真實 JWT，排除既有展示帳號與資料影響。 */
function articleMember(string $name, string $prefix): array
{
    $email = $prefix.'-'.bin2hex(random_bytes(6)).'@example.test';
    articleCheck(articleRequest('POST', '/api/auth/register', json_encode(['email' => $email, 'password' => 'Abc1234', 'nickname' => $name], JSON_THROW_ON_ERROR)), 201);
    $login = articleRequest('POST', '/api/auth/login', json_encode(['email' => $email, 'password' => 'Abc1234'], JSON_THROW_ON_ERROR));
    articleCheck($login, 200);

    return [$login[1]['data']['user']['id'], $login[1]['data']['access_token']];
}

$started = microtime(true);
[$authorId, $author] = articleMember('作者', 'article-author');
[$otherId, $other] = articleMember('讀者', 'article-reader');
[$adminId, $admin] = articleMember('管理者', 'article-admin');
articleSql("UPDATE accounts SET role='admin' WHERE id=$adminId");
$title = "  測試😀\r\n標題  ";
$body = "第一行\r\n第二行\n第三行\r保留 <b>文字</b>";
$first = json_encode(['title' => $title, 'body' => $body], JSON_THROW_ON_ERROR);
articleCheck(articleRequest('POST', '/api/articles', $first), 401, 'E0002');
articleCheck(articleRequest('POST', '/api/articles', $first, $author), 201);
$mine = articleRequest('GET', '/api/me/articles?page=1&per_page=20', '', $author);
articleCheck($mine, 200);
assert($mine[1]['data']['pagination']['total'] === 1 && $mine[1]['data']['pagination']['total_pages'] === 1);
$item = $mine[1]['data']['list'][0];
$id = $item['id'];
assert($item['title'] === $title && $item['body'] === $body && $item['version'] === 1);
assert($item['created_backend'] === 'laravel' && $item['created_at'] === $item['updated_at']);
assert($item['visibility'] === 'visible' && $item['can_restore'] === false);
assert($item['author'] === ['id' => $authorId, 'nickname' => '作者']);
assert(array_keys($item) === ['id', 'title', 'body', 'author', 'version', 'created_backend', 'created_at', 'updated_at', 'visibility', 'can_restore']);
assert(preg_match('/\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\z/', $item['created_at']) === 1);
articleCheck(articleRequest('GET', '/api/articles/'.$id), 200);
assert(articleRequest('GET', '/api/articles/'.$id)[1]['data'] === $item);
echo "PASS creation, raw text, privacy, public detail and resource shape\n";

// 以相同建立時間測試 ID 降冪，避免測試只偶然通過時間排序。
articleCheck(articleRequest('POST', '/api/articles', json_encode(['title' => '另一篇', 'body' => '內容']), $author), 201);
$secondId = (int) articleSql("SELECT max(id) FROM articles WHERE author_id=$authorId");
articleSql("UPDATE articles SET created_at=(SELECT created_at FROM articles WHERE id=$id), updated_at=(SELECT created_at FROM articles WHERE id=$id) WHERE id=$secondId");
$public = articleRequest('GET', '/api/articles?page=1&per_page=1');
articleCheck($public, 200);
assert($public[1]['data']['pagination']['total'] >= 2);
assert($public[1]['data']['list'][0]['id'] === $secondId);
$pageTwo = articleRequest('GET', '/api/articles?page=2&per_page=1');
articleCheck($pageTwo, 200);
assert($pageTwo[1]['data']['list'][0]['id'] === $id);
$far = articleRequest('GET', '/api/me/articles?page=9007199254740991&per_page=100', '', $author);
articleCheck($far, 200);
assert($far[1]['data'] === ['list' => [], 'pagination' => ['page' => 9007199254740991, 'per_page' => 100, 'total' => 2, 'total_pages' => 1]]);
echo "PASS ordering, total, page boundaries and huge safe page\n";

// 下架 fixture 用於本票讀取權限，正式下架 API 屬後續工作。
articleSql("UPDATE articles SET visibility='hidden',restore_control='author' WHERE id=$id");
articleCheck(articleRequest('GET', '/api/articles/'.$id), 404, 'E0004');
articleCheck(articleRequest('GET', '/api/articles/'.$id, '', $other), 404, 'E0004');
articleCheck(articleRequest('GET', '/api/articles/9007199254740991', '', $other), 404, 'E0004');
$ownHidden = articleRequest('GET', '/api/articles/'.$id, '', $author);
articleCheck($ownHidden, 200);
assert($ownHidden[1]['data']['can_restore'] === true);
$adminHidden = articleRequest('GET', '/api/articles/'.$id, '', $admin);
articleCheck($adminHidden, 200);
assert($adminHidden[1]['data']['can_restore'] === true);
$public = articleRequest('GET', '/api/articles?page=1&per_page=20');
articleCheck($public, 200);
assert(! in_array($id, array_column($public[1]['data']['list'], 'id'), true));
$managed = articleRequest('GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', '', $admin);
articleCheck($managed, 200);
assert(in_array($id, array_column($managed[1]['data']['list'], 'id'), true));
articleCheck(articleRequest('GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', '', $other), 403, 'E0003');
articleCheck(articleRequest('GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'), 401, 'E0002');
articleCheck(articleRequest('GET', '/api/articles?page=bad&per_page=20&filters%5Bscope%5D=management', '{}', $other), 403, 'E0003');
articleCheck(articleRequest('GET', '/api/articles?page=bad&per_page=20&filters%5Bscope%5D=management'), 401, 'E0002');
$own = articleRequest('GET', '/api/me/articles?page=1&per_page=20', '', $author);
articleCheck($own, 200);
assert($own[1]['data']['pagination']['total'] === 2 && in_array($id, array_column($own[1]['data']['list'], 'id'), true));
articleSql("UPDATE articles SET restore_control='admin' WHERE id=$id");
assert(articleRequest('GET', '/api/articles/'.$id, '', $author)[1]['data']['can_restore'] === false);
assert(articleRequest('GET', '/api/articles/'.$id, '', $admin)[1]['data']['can_restore'] === true);
echo "PASS hidden reads, management scope and restore control\n";

// 驗證欄位先後、唯讀欄位、Query 重複與未授權時不可寫入。
$before = (int) articleSql("SELECT count(*) FROM articles WHERE author_id=$authorId");
// PostgreSQL text 無法儲存 U+0000；標題或內文含該字元都須回欄位錯誤且不新增文章。
foreach (['title', 'body'] as $field) {
    $invalid = json_encode(['title' => '正常標題', 'body' => '正常內文', $field => "前\0後"], JSON_THROW_ON_ERROR);
    articleCheck(articleRequest('POST', '/api/articles', $invalid, $author), 422, 'E0001', $field, '欄位格式或範圍不正確');
    assert((int) articleSql("SELECT count(*) FROM articles WHERE author_id=$authorId") === $before);
}
foreach ([['{}', 'title', '此欄位為必填'], ['{"title":null,"body":"x"}', 'title', '此欄位不可為 null'], ['{"title":9,"body":"x"}', 'title', '欄位型態不正確'], [json_encode(['title' => "\u{00A0}\u{3000}", 'body' => 'x']), 'title', '欄位格式或範圍不正確'], [json_encode(['title' => str_repeat('😀', 201), 'body' => 'x']), 'title', '欄位格式或範圍不正確'], [json_encode(['title' => 'x', 'body' => str_repeat('字', 20001)]), 'body', '欄位格式或範圍不正確'], ['{"title":"x","body":"y","created_backend":"django"}', 'created_backend', '不支援此欄位'], ['[]', '_body', 'Request Body 必須為 JSON object']] as [$invalid,$field,$message]) {
    articleCheck(articleRequest('POST', '/api/articles', $invalid, $author), 422, 'E0001', $field, $message);
}
articleCheck(articleRequest('POST', '/api/articles', '{', $author), 400, 'E0005');
articleCheck(articleRequest('POST', '/api/articles', '{}?ignored', $other), 400, 'E0005');
assert((int) articleSql("SELECT count(*) FROM articles WHERE author_id=$authorId") === $before);
// 無效 UTF-8 的未知 Query 名稱仍須得到可序列化的欄位錯誤，不可落入系統 500。
articleCheck(articleRequest('GET', '/api/articles?page=1&per_page=20&%FF=1'), 422, 'E0001', '%FF', '不支援此欄位');
foreach ([['/api/articles?page=1&per_page=20&page=2', 'page', '此參數不可重複'], ['/api/articles?page%5B%5D=1&per_page=20', 'page', '欄位型態不正確'], ['/api/articles?page=1&per_page=20&filters%5Bscope%5D%5B%5D=management', 'filters.scope', '欄位型態不正確'], ['/api/articles?page=1&per_page=20&x=1', 'x', '不支援此欄位'], ['/api/articles?page=-1&per_page=20', 'page', '欄位格式或範圍不正確'], ['/api/articles?page=1&per_page=101', 'per_page', '欄位格式或範圍不正確'], ['/api/articles?page=1&per_page=20&filters%5Bscope%5D=other', 'filters.scope', '欄位格式或範圍不正確'], ['/api/articles?page=1&per_page=20&filters%5Bscope%5D=management&filters%5Bscope%5D=management', 'filters.scope', '此參數不可重複']] as [$path,$field,$message]) {
    articleCheck(articleRequest('GET', $path, '', $admin), 422, 'E0001', $field, $message);
}
articleCheck(articleRequest('GET', '/api/articles?page=1&per_page=20', '{}'), 422, 'E0001', '_body', '此 API 不接受 Request Body');
articleCheck(articleRequest('GET', '/api/articles/nope', '', $other), 422, 'E0001', 'article_id', '欄位格式或範圍不正確');
articleCheck(articleRequest('GET', '/api/me/articles?page=1&per_page=20&filters%5Bscope%5D=management', '', $author), 422, 'E0001', 'filters.scope', '不支援此欄位');
articleCheck(articleRequest('GET', '/api/articles?page=1&per_page=20', '', 'bad-token'), 401, 'E0002');
articleCheck(articleRequest('GET', '/api/me/articles?page=1&per_page=20'), 401, 'E0002');
articleSql("UPDATE accounts SET status='disabled' WHERE id=$otherId");
articleCheck(articleRequest('GET', '/api/articles?page=1&per_page=20', '', $other), 403, 'E0102');
echo "PASS validation, authentication precedence and no invalid writes\n";
echo 'HTTP article checks passed in '.round(microtime(true) - $started, 2)."s\n";
