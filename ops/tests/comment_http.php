<?php

declare(strict_types=1);

// 只可對 Ticket 04 專用資料庫執行；此測試會用 SQL fixture 調整留言、文章與帳號狀態。
if (! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket04_')) {
    throw new RuntimeException('請使用 ticket04_ 開頭的隔離 Compose project');
}
$commentBase = getenv('API_BASE') ?: 'http://localhost:8081';

/** 經真實 HTTP 傳送原始 JSON，讓認證、解析與公開回應一同受驗證。 */
function commentRequest(string $method, string $path, string $body = '', ?string $token = null): array
{
    global $commentBase;
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer '.$token;
    }
    $curl = curl_init($commentBase.$path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_POSTFIELDS => $body, CURLOPT_HTTPHEADER => $headers, CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 15]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

    return [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, $length)];
}

/** 比對跨後端共同錯誤碼與精確文案，避免測試只接受 Laravel 原生訊息。 */
function commentCheck(array $result, int $status, ?string $code = null, ?string $field = null, ?string $detail = null): void
{
    assert($result[0] === $status, 'HTTP status expected '.$status.', got '.$result[0]);
    assert($result[1]['success'] === ($code === null));
    if ($code === null) {
        assert($result[1]['message'] === 'OK');
        if ($status === 201) {
            assert(json_decode($result[2], false, 512, JSON_THROW_ON_ERROR)->data instanceof stdClass);
            assert($result[1]['data'] === []);
        }

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
function commentSql(string $sql): string
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
function commentMember(string $name, string $prefix): array
{
    $email = $prefix.'-'.bin2hex(random_bytes(6)).'@example.test';
    commentCheck(commentRequest('POST', '/api/auth/register', json_encode(['email' => $email, 'password' => 'Abc1234', 'nickname' => $name], JSON_THROW_ON_ERROR)), 201);
    $login = commentRequest('POST', '/api/auth/login', json_encode(['email' => $email, 'password' => 'Abc1234'], JSON_THROW_ON_ERROR));
    commentCheck($login, 200);

    return [$login[1]['data']['user']['id'], $login[1]['data']['access_token']];
}

/** 隔離 SQL 持有文章鎖並觀察 HTTP 等鎖，驗證下架先提交時留言不可寫入。 */
function commentParentLock(int $articleId, string $token): void
{
    global $commentBase;
    // psql 保持交易開啟，quiet 模式只輸出鎖定後 ID，作為確實取得文章鎖的訊號。
    $pipes = [];
    $process = proc_open(['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-qAt'], [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    assert(is_resource($process));
    $curl = curl_init($commentBase.'/api/articles/'.$articleId.'/comments');
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => 'POST', CURLOPT_POSTFIELDS => '{"body":"等鎖後不可新增"}', CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json', 'Authorization: Bearer '.$token], CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 15]);
    $multi = curl_multi_init();
    try {
        fwrite($pipes[0], "BEGIN;\nSELECT id FROM articles WHERE id=$articleId FOR UPDATE;\n");
        fflush($pipes[0]);
        assert(trim(fgets($pipes[1]) ?: '') === (string) $articleId, 'fixture 未取得父文章鎖');

        // 以非阻塞 HTTP 實際送出，必須觀察到 API 的文章查詢等鎖，不以固定 sleep 猜時序。
        curl_multi_add_handle($multi, $curl);
        $waiting = false;
        $deadline = microtime(true) + 5;
        do {
            curl_multi_exec($multi, $running);
            $waiting = (int) commentSql("SELECT count(*) FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' AND wait_event_type='Lock' AND query LIKE '%\"articles\"%for update%'") > 0;
            if (! $waiting && $running > 0) {
                curl_multi_select($multi, 0.02);
            }
        } while (! $waiting && $running > 0 && microtime(true) < $deadline);
        assert($waiting, 'API 必須在父文章鎖上等待後才判定公開狀態');

        // 等待中的新增只能在下架提交後重查；留下架 fixture 供呼叫者確認未新增。
        fwrite($pipes[0], "UPDATE articles SET visibility='hidden',restore_control='author' WHERE id=$articleId;\nCOMMIT;\n");
        fclose($pipes[0]);
        stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        assert(proc_close($process) === 0, '鎖競爭 fixture 失敗: '.$error);
        do {
            curl_multi_exec($multi, $running);
            if ($running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($running > 0);
        $raw = curl_multi_getcontent($curl);
        assert(is_string($raw) && curl_errno($curl) === 0, curl_error($curl));
        $body = substr($raw, curl_getinfo($curl, CURLINFO_HEADER_SIZE));
        commentCheck([curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode($body, true, 512, JSON_THROW_ON_ERROR), $body], 404, 'E0004');
    } finally {
        // 異常時以 rollback 釋放 fixture 鎖，避免測試失敗後卡住隔離資料庫。
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
    }
}

$started = microtime(true);
[$articleAuthorId, $articleAuthor] = commentMember('文章作者', 'comment-parent');
[$authorId, $author] = commentMember('留言作者', 'comment-author');
[$otherId, $other] = commentMember('其他讀者', 'comment-reader');
[$adminId, $admin] = commentMember('管理者', 'comment-admin');
commentSql("UPDATE accounts SET role='admin' WHERE id=$adminId");
commentCheck(commentRequest('POST', '/api/articles', '{"title":"父文章機密標題","body":"父文章機密內文"}', $articleAuthor), 201);
$articleId = (int) commentSql("SELECT max(id) FROM articles WHERE author_id=$articleAuthorId");
$path = '/api/articles/'.$articleId.'/comments';
$body = "  留言😀\r\n第二行\n第三行\r保留 <b>文字</b>  ";
// 正常建立走真實 HTTP；從自己的列表讀取新留言，不依賴寫入回傳未約定的 ID。
commentCheck(commentRequest('POST', $path, json_encode(['body' => $body], JSON_THROW_ON_ERROR), $author), 201);
$mine = commentRequest('GET', '/api/me/comments?page=1&per_page=20', '', $author);
commentCheck($mine, 200);
assert($mine[1]['data']['pagination'] === ['page' => 1, 'per_page' => 20, 'total' => 1, 'total_pages' => 1]);
$item = $mine[1]['data']['list'][0];
$id = $item['id'];
assert(array_keys($item) === ['id', 'body', 'author', 'article_id', 'version', 'created_backend', 'created_at', 'updated_at', 'visibility', 'can_restore']);
assert($item['body'] === $body && $item['article_id'] === $articleId && $item['version'] === 1);
assert($item['created_backend'] === 'laravel' && $item['created_at'] === $item['updated_at']);
assert($item['visibility'] === 'visible' && $item['can_restore'] === false);
assert($item['author'] === ['id' => $authorId, 'nickname' => '留言作者']);
assert(preg_match('/\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\z/', $item['created_at']) === 1);
assert(commentRequest('GET', $path.'?page=1&per_page=20')[1]['data']['list'] === [$item]);
assert(commentRequest('GET', '/api/comments/'.$id, '', $author)[1]['data'] === $item);
commentCheck(commentRequest('GET', '/api/comments/'.$id), 401, 'E0002');
commentCheck(commentRequest('GET', '/api/comments/'.$id, '', $other), 403, 'E0003');
commentCheck(commentRequest('GET', '/api/comments/'.$id, '', $articleAuthor), 403, 'E0003');
commentCheck(commentRequest('GET', '/api/comments/'.$id, '', $admin), 200);
echo "PASS public, own and admin reads, raw text and resource privacy\n";

// 相同時間依 ID 降冪，總數按各端點可見範圍計算；巨大合法頁碼不溢位。
$secondId = (int) commentSql("INSERT INTO comments(article_id,author_id,body,visibility,restore_control,version,created_backend,created_at,updated_at) SELECT article_id,author_id,'第二則','visible',NULL,1,'gin',created_at,created_at FROM comments WHERE id=$id RETURNING id");
$firstPage = commentRequest('GET', $path.'?page=1&per_page=1');
commentCheck($firstPage, 200);
assert($firstPage[1]['data']['list'][0]['id'] === $secondId);
assert($firstPage[1]['data']['pagination'] === ['page' => 1, 'per_page' => 1, 'total' => 2, 'total_pages' => 2]);
assert(commentRequest('GET', $path.'?page=2&per_page=1')[1]['data']['list'][0]['id'] === $id);
$far = commentRequest('GET', '/api/me/comments?page=9007199254740991&per_page=100', '', $author);
commentCheck($far, 200);
assert($far[1]['data'] === ['list' => [], 'pagination' => ['page' => 9007199254740991, 'per_page' => 100, 'total' => 2, 'total_pages' => 1]]);
$empty = commentRequest('GET', '/api/me/comments?page=1&per_page=20', '', $other);
commentCheck($empty, 200);
assert($empty[1]['data'] === ['list' => [], 'pagination' => ['page' => 1, 'per_page' => 20, 'total' => 0, 'total_pages' => 0]]);
echo "PASS deterministic order, exact counts and safe pagination\n";

// 父文章與留言各自的下架狀態不可互相授權，個人讀取例外不得洩漏父文章欄位。
commentSql("UPDATE comments SET visibility='hidden',restore_control='author' WHERE id=$id");
$public = commentRequest('GET', $path.'?page=1&per_page=20');
commentCheck($public, 200);
assert(array_column($public[1]['data']['list'], 'id') === [$secondId]);
assert($public[1]['data']['pagination']['total'] === 1);
assert(count(commentRequest('GET', $path.'?page=1&per_page=20', '', $admin)[1]['data']['list']) === 2);
commentCheck(commentRequest('GET', '/api/comments/'.$id, '', $other), 404, 'E0004');
assert(commentRequest('GET', '/api/comments/'.$id, '', $author)[1]['data']['can_restore'] === true);
commentSql("UPDATE comments SET restore_control='admin' WHERE id=$id");
assert(commentRequest('GET', '/api/comments/'.$id, '', $author)[1]['data']['can_restore'] === false);
assert(commentRequest('GET', '/api/comments/'.$id, '', $admin)[1]['data']['can_restore'] === true);
commentSql("UPDATE articles SET visibility='hidden',restore_control='author' WHERE id=$articleId");
commentCheck(commentRequest('GET', $path.'?page=1&per_page=20'), 404, 'E0004');
commentCheck(commentRequest('GET', $path.'?page=1&per_page=20', '', $author), 404, 'E0004');
commentCheck(commentRequest('GET', $path.'?page=1&per_page=20', '', $other), 404, 'E0004');
$preview = commentRequest('GET', $path.'?page=1&per_page=20', '', $articleAuthor);
commentCheck($preview, 200);
assert(array_column($preview[1]['data']['list'], 'id') === [$secondId]);
assert($preview[1]['data']['pagination']['total'] === 1);
assert(count(commentRequest('GET', $path.'?page=1&per_page=20', '', $admin)[1]['data']['list']) === 2);
commentCheck(commentRequest('GET', '/api/comments/'.$secondId, '', $other), 404, 'E0004');
commentCheck(commentRequest('GET', '/api/comments/'.$secondId, '', $articleAuthor), 404, 'E0004');
commentCheck(commentRequest('GET', '/api/comments/'.$secondId, '', $author), 200);
$mine = commentRequest('GET', '/api/me/comments?page=1&per_page=20', '', $author);
commentCheck($mine, 200);
assert($mine[1]['data']['pagination']['total'] === 2);
assert(! str_contains(json_encode($mine), '父文章機密'));
commentCheck(commentRequest('GET', '/api/comments/9007199254740991', '', $author), 404, 'E0004');
commentCheck(commentRequest('GET', '/api/articles/9007199254740991/comments?page=1&per_page=20'), 404, 'E0004');
echo "PASS hidden parent, hidden comments, restore control and 404/403 precedence\n";

// 原始 Query 驗證拒絕未知、重複及陣列參數；格式先於隱藏目標查詢。
foreach ([['page=1&per_page=20&page=2', 'page', '此參數不可重複'], ['page%5B%5D=1&per_page=20', 'page', '欄位型態不正確'], ['page=1&per_page=20&filters%5Bscope%5D=management', 'filters.scope', '不支援此欄位'], ['page=1&per_page=20&%FF=1', '%FF', '不支援此欄位'], ['page=-1&per_page=20', 'page', '欄位格式或範圍不正確'], ['page=1&per_page=101', 'per_page', '欄位格式或範圍不正確'], ['per_page=20', 'page', '此欄位為必填'], ['page=1&per_page=1e2', 'per_page', '欄位格式或範圍不正確'], ['page=9007199254740992&per_page=20', 'page', '欄位格式或範圍不正確']] as [$query, $field, $detail]) {
    commentCheck(commentRequest('GET', $path.'?'.$query), 422, 'E0001', $field, $detail);
    commentCheck(commentRequest('GET', '/api/me/comments?'.$query, '', $author), 422, 'E0001', $field, $detail);
}
commentCheck(commentRequest('GET', $path.'?page=1&per_page=20', '{}'), 422, 'E0001', '_body', '此 API 不接受 Request Body');
commentCheck(commentRequest('GET', '/api/comments/'.$id.'?x=1', '', $author), 422, 'E0001', 'x', '不支援此欄位');
commentCheck(commentRequest('GET', '/api/comments/'.$id, '{}', $author), 422, 'E0001', '_body', '此 API 不接受 Request Body');
commentCheck(commentRequest('GET', '/api/comments/nope', '', $author), 422, 'E0001', 'comment_id', '欄位格式或範圍不正確');
commentCheck(commentRequest('GET', '/api/articles/nope/comments?page=1&per_page=20'), 422, 'E0001', 'article_id', '欄位格式或範圍不正確');
commentCheck(commentRequest('GET', $path.'?page=bad&per_page=20', '{}', 'bad-token'), 401, 'E0002');
commentCheck(commentRequest('GET', '/api/me/comments?page=bad&per_page=20'), 401, 'E0002');
commentCheck(commentRequest('GET', '/api/comments/nope'), 401, 'E0002');
// 下架文章對無預覽權者隱藏存在，有預覽權的作者及管理員仍不能新增留言。
$before = (int) commentSql("SELECT count(*) FROM comments WHERE article_id=$articleId");
foreach ([[$author, 404], [$other, 404], [$articleAuthor, 403], [$admin, 403]] as [$token, $status]) {
    commentCheck(commentRequest('POST', $path, '{"body":"不可寫入"}', $token), $status, $status === 404 ? 'E0004' : 'E0003');
}
commentCheck(commentRequest('POST', $path, '{}', $author), 422, 'E0001', 'body', '此欄位為必填');
commentCheck(commentRequest('POST', $path.'?x=1', '{', 'bad-token'), 401, 'E0002');
commentCheck(commentRequest('POST', '/api/articles/nope/comments', '{"body":"文字"}', $author), 422, 'E0001', 'article_id', '欄位格式或範圍不正確');
commentCheck(commentRequest('POST', '/api/articles/9007199254740991/comments', '{"body":"文字"}', $author), 404, 'E0004');
assert((int) commentSql("SELECT count(*) FROM comments WHERE article_id=$articleId") === $before);
commentSql("UPDATE articles SET visibility='visible',restore_control=NULL WHERE id=$articleId");

// Unicode 碼點含 emoji，固定空白集合以外的字元仍可輸入；字面六字元不是 U+0000。
foreach (['😀', str_repeat('😀', 2000), "\u{200B}", '\u0000'] as $validBody) {
    commentCheck(commentRequest('POST', $path, json_encode(['body' => $validBody], JSON_THROW_ON_ERROR), $author), 201);
    assert(commentRequest('GET', '/api/me/comments?page=1&per_page=1', '', $author)[1]['data']['list'][0]['body'] === $validBody);
}
commentCheck(commentRequest('POST', $path, '{"body":"管理員亦可留言"}', $admin), 201);
$adminItem = commentRequest('GET', '/api/me/comments?page=1&per_page=1', '', $admin)[1]['data']['list'][0];
assert($adminItem['author']['id'] === $adminId && $adminItem['created_backend'] === 'laravel');
$before = (int) commentSql("SELECT count(*) FROM comments WHERE article_id=$articleId");
// 每欄依必填、null、型態、格式先後判定，來源與作者等唯讀欄位一律拒絕。
$whitespace = "\u{0009}\u{000A}\u{000B}\u{000C}\u{000D} \u{0085}\u{00A0}\u{1680}\u{2000}\u{2001}\u{2002}\u{2003}\u{2004}\u{2005}\u{2006}\u{2007}\u{2008}\u{2009}\u{200A}\u{2028}\u{2029}\u{202F}\u{205F}\u{3000}";
foreach ([['{}', 'body', '此欄位為必填'], ['{"body":null}', 'body', '此欄位不可為 null'], ['{"body":1}', 'body', '欄位型態不正確'], ['{"body":false}', 'body', '欄位型態不正確'], ['{"body":[]}', 'body', '欄位型態不正確'], ['{"body":""}', 'body', '欄位格式或範圍不正確'], [json_encode(['body' => $whitespace]), 'body', '欄位格式或範圍不正確'], [json_encode(['body' => str_repeat('😀', 2001)]), 'body', '欄位格式或範圍不正確'], [json_encode(['body' => "前\0後"]), 'body', '欄位格式或範圍不正確'], ['[]', '_body', 'Request Body 必須為 JSON object'], ['null', '_body', 'Request Body 必須為 JSON object'], ['', '_body', 'Request Body 必須為 JSON object']] as [$invalid, $field, $detail]) {
    commentCheck(commentRequest('POST', $path, $invalid, $author), 422, 'E0001', $field, $detail);
    assert((int) commentSql("SELECT count(*) FROM comments WHERE article_id=$articleId") === $before);
}
foreach (['author_id', 'article_id', 'created_backend', 'visibility', 'restore_control', 'version', 'title', 'created_at', 'unknown'] as $field) {
    commentCheck(commentRequest('POST', $path, json_encode(['body' => '正常留言', $field => '偽造']), $author), 422, 'E0001', $field, '不支援此欄位');
}
commentCheck(commentRequest('POST', $path, '{', $author), 400, 'E0005');
commentCheck(commentRequest('POST', $path.'?x=1', '{"body":"文字"}', $author), 422, 'E0001', 'x', '不支援此欄位');
commentCheck(commentRequest('POST', $path.'?x=1&x=2', '{"body":"文字"}', $author), 422, 'E0001', 'x', '此參數不可重複');
commentCheck(commentRequest('POST', $path, '{"body":"文字"}'), 401, 'E0002');
assert((int) commentSql("SELECT count(*) FROM comments WHERE article_id=$articleId") === $before);
echo "PASS creation, Unicode boundaries, NUL rejection, server fields and hidden writes\n";

// 下架交易先持有父文章鎖；確認 HTTP 正在等鎖後再提交下架，不能依舊狀態新增。
commentParentLock($articleId, $author);
assert((int) commentSql("SELECT count(*) FROM comments WHERE article_id=$articleId") === $before);
commentSql("UPDATE articles SET visibility='visible',restore_control=NULL WHERE id=$articleId");
echo "PASS parent lock concurrency: committed hide prevents waiting comment insert\n";
commentSql("UPDATE accounts SET status='disabled' WHERE id=$otherId");
commentCheck(commentRequest('GET', $path.'?page=bad&per_page=20', '', $other), 403, 'E0102');
commentCheck(commentRequest('GET', '/api/me/comments?page=1&per_page=20', '', $other), 403, 'E0102');
commentCheck(commentRequest('POST', $path, '{', $other), 403, 'E0102');
echo "PASS query validation and authentication precedence\n";
echo 'HTTP comment checks passed in '.round(microtime(true) - $started, 2)."s\n";
