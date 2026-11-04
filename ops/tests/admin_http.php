<?php

declare(strict_types=1);

// 本檔為獨立執行的 HTTP 驗收入口，輔助函式與案例保留同檔以便直接執行。
// phpcs:disable PSR1.Files.SideEffects

require_once __DIR__ . '/http_fixture.php';
// 所有 fixture 操作之前先確認斷言有效，不能在測試檢查被關閉時仍送出寫入。
assertHttpFixtureAssertions();

// 測試會新增帳號與內容並改動 fixture，僅允許 Ticket 06 獨立資料庫。
if (! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket06_test')) {
    throw new RuntimeException('請使用 ticket06_test 開頭的隔離 Compose project');
}
$adminBase = getenv('API_BASE') ?: 'http://localhost:18086';
assertHttpFixture(['laravel' => $adminBase]);

/** 透過真實 HTTP 驗證路由、認證及共同 envelope，而非直接呼叫 Controller。 */
function adminRequest(string $method, string $path, string $body = '', ?string $token = null): array
{
    global $adminBase;
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer ' . $token;
    }
    $curl = curl_init($adminBase . $path);
    curl_setopt_array($curl, [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_POSTFIELDS => $body,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HEADER => true,
        CURLOPT_TIMEOUT => 15,
    ]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

    return [
        curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
        json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR)
    ];
}

/** 固定檢查共同錯誤碼與文案，避免只碰巧取得相同 HTTP 狀態。 */
function adminCheck(
    array $result,
    int $status,
    ?string $code = null,
    ?string $field = null,
    ?string $detail = null
): void {
    assert($result[0] === $status, 'HTTP expected ' . $status . ', got ' . $result[0]);
    assert($result[1]['success'] === ($code === null));
    $messages = [
        'E0001' => '欄位資料不正確，請確認後再送出',
        'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能',
        'E0004' => '找不到指定資料或 API',
        'E0102' => '此帳號已停用，請聯絡系統管理員',
    ];
    if ($code === null) {
        assert($result[1]['message'] === 'OK');

        return;
    }
    assert($result[1]['error_code'] === $code && $result[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
    assert(is_string($result[1]['log_id']) && strlen($result[1]['log_id']) === 36);
    if ($field !== null) {
        assert($result[1]['errors'][$field] === [$detail]);
    }
}

/** fixture SQL 僅連向隔離 Compose 的資料庫，測試角色與隱藏內容條件。 */
function adminSql(string $sql): string
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
    $out = stream_get_contents($pipes[1]);
    $err = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 fixture SQL 失敗: ' . $err);
    }

    return trim($out);
}

/** 每位測試使用者透過正式註冊與登入取得 JWT，避免依賴展示帳號。 */
function adminMember(string $nickname, string $prefix): array
{
    $email = $prefix . '-' . bin2hex(random_bytes(5)) . '@example.test';
    adminCheck(adminRequest('POST', '/api/auth/register', json_encode([
        'email' => $email,
        'password' => 'Abc1234',
        'nickname' => $nickname,
    ], JSON_THROW_ON_ERROR)), 201);
    $login = adminRequest('POST', '/api/auth/login', json_encode([
        'email' => $email,
        'password' => 'Abc1234',
    ], JSON_THROW_ON_ERROR));
    adminCheck($login, 200);

    return [(int) $login[1]['data']['user']['id'], $login[1]['data']['access_token']];
}

[$firstId, $first] = adminMember('A_B%\\É中', 'admin-first');
[$secondId, $second] = adminMember('普通會員é', 'admin-second');
[$adminId, $admin] = adminMember('管理員', 'admin-owner');
adminSql("UPDATE accounts SET role='admin' WHERE id=$adminId");
adminSql("UPDATE accounts SET created_at='2026-09-23 09:00:00+08' WHERE id IN ($firstId,$secondId,$adminId)");
$members = '/api/members?page=1&per_page=20';
$all = adminRequest('GET', $members, '', $admin);
adminCheck($all, 200);
assert(array_column($all[1]['data']['list'], 'id') === [$secondId, $firstId]);
assert($all[1]['data']['pagination'] === ['page' => 1, 'per_page' => 20, 'total' => 2, 'total_pages' => 1]);
assert(array_keys($all[1]['data']['list'][0]) === [
    'id', 'email', 'nickname', 'role', 'status', 'created_at',
]);
assert($all[1]['data']['list'][0]['role'] === 'member' && $all[1]['data']['list'][0]['status'] === 'active');
assert($all[1]['data']['list'][0]['created_at'] === '2026-09-23 09:00:00');
assert(adminRequest('GET', '/api/members/' . $firstId, '', $admin)[1]['data'] === $all[1]['data']['list'][1]);
adminCheck(adminRequest('GET', '/api/members/' . $adminId, '', $admin), 404, 'E0004');
adminCheck(adminRequest('GET', '/api/members/9007199254740991', '', $admin), 404, 'E0004');
adminSql("UPDATE accounts SET status='disabled' WHERE id=$secondId");
assert(adminRequest('GET', '/api/members/' . $secondId, '', $admin)[1]['data']['status'] === 'disabled');
adminCheck(adminRequest('GET', $members, '', $second), 403, 'E0102');
echo "PASS member scope, field privacy, stable order and disabled visibility\n";

// 搜尋保留非 ASCII 碼點與字面符號，僅 ASCII 大小寫摺疊；空關鍵字不篩選。
adminMember('A1B', 'admin-wildcard');
foreach (['a_b', '%', '\\', 'É', '中'] as $keyword) {
    $found = adminRequest('GET', $members . '&filters%5Bkeyword%5D=' . rawurlencode($keyword), '', $admin);
    adminCheck($found, 200);
    assert(array_column($found[1]['data']['list'], 'id') === [$firstId], $keyword);
    assert($found[1]['data']['pagination']['total'] === 1);
}
$nonAscii = adminRequest('GET', $members . '&filters%5Bkeyword%5D=' . rawurlencode('é'), '', $admin);
adminCheck($nonAscii, 200);
assert(array_column($nonAscii[1]['data']['list'], 'id') === [$secondId]);
foreach (['A%5Fb', '%20A_B%20', '%20%20'] as $keyword) {
    $found = adminRequest('GET', $members . '&filters%5Bkeyword%5D=' . $keyword, '', $admin);
    adminCheck($found, 200);
    assert($found[1]['data']['pagination']['total'] === ($keyword === '%20%20' ? 3 : 1));
}
$none = adminRequest('GET', $members . '&filters%5Bkeyword%5D=' . rawurlencode('Ｅ'), '', $admin);
adminCheck($none, 200);
assert($none[1]['data']['pagination']['total'] === 0);
assert(adminRequest('GET', '/api/members?page=3&per_page=1', '', $admin)[1]['data']['list'][0]['id'] === $firstId);
assert(adminRequest('GET', '/api/members?page=9007199254740991&per_page=100', '', $admin)[1]['data']['list'] === []);
echo "PASS literal search, ASCII folding and pagination\n";

// 管理端點先驗角色，之後才驗格式與目標；未知、重複及巢狀 Query 皆不能被框架吞掉。
adminCheck(adminRequest('GET', '/api/members/nope?x=1', '', $first), 403, 'E0003');
adminCheck(adminRequest('GET', '/api/comments?page=bad&per_page=20', '', $first), 403, 'E0003');
adminCheck(adminRequest('GET', $members), 401, 'E0002');
adminCheck(adminRequest('GET', '/api/members/nope', '', $admin), 422, 'E0001', 'member_id', '欄位格式或範圍不正確');
foreach (
    [
    [$members . '&page=2', 'page', '此參數不可重複'],
    [$members . '&x=1', 'x', '不支援此欄位'],
    [$members . '&filters%5Bkeyword%5D%5B%5D=x', 'filters.keyword', '欄位型態不正確'],
    [$members . '&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y', 'filters.keyword', '此參數不可重複'],
    [$members . '&filters%5Bkeyword%5D=' . str_repeat('x', 255), 'filters.keyword', '欄位格式或範圍不正確'],
    ] as [$path, $field, $detail]
) {
    adminCheck(adminRequest('GET', $path, '', $admin), 422, 'E0001', $field, $detail);
}
adminCheck(adminRequest('GET', $members, '{}', $admin), 422, 'E0001', '_body', '此 API 不接受 Request Body');
adminCheck(adminRequest('GET', '/api/members/' . $firstId . '?x=1', '', $admin), 422, 'E0001', 'x', '不支援此欄位');
echo "PASS authorization and malformed request order\n";

adminCheck(adminRequest('POST', '/api/articles', '{"title":"管理文章","body":"父文章"}', $first), 201);
$articleId = (int) adminSql("SELECT max(id) FROM articles WHERE author_id=$firstId");
adminCheck(adminRequest('POST', '/api/articles/' . $articleId . '/comments', '{"body":"公開留言"}', $first), 201);
$publicId = (int) adminSql("SELECT max(id) FROM comments WHERE article_id=$articleId");
$hiddenId = (int) adminSql(
    ("INSERT INTO comments(article_id,author_id,body,visibility,restor" .
        "e_control,version,created_backend,created_at,updated_at) VALUES " .
        "($articleId,$secondId,'隱藏留言','hidden','admin',1,'gin',now(),now(" .
        ")) RETURNING id")
);
adminSql("UPDATE articles SET visibility='hidden',restore_control='admin' WHERE id=$articleId");
$comments = adminRequest('GET', '/api/comments?page=1&per_page=20', '', $admin);
adminCheck($comments, 200);
assert($comments[1]['data']['pagination']['total'] === 2);
assert(array_column($comments[1]['data']['list'], 'id') === [$hiddenId, $publicId]);
assert(array_keys($comments[1]['data']['list'][0]) === [
    'id', 'body', 'author', 'article_id', 'version', 'created_backend',
    'created_at', 'updated_at', 'visibility', 'can_restore',
]);
assert($comments[1]['data']['list'][0]['author'] === ['id' => $secondId, 'nickname' => '普通會員é']);
assert(
    $comments[1]['data']['list'][0]['visibility'] === 'hidden'
        && $comments[1]['data']['list'][0]['can_restore'] === true
);
adminCheck(adminRequest('GET', '/api/comments?page=bad&per_page=20', '', $admin), 422, 'E0001', 'page', '欄位格式或範圍不正確');
adminCheck(
    adminRequest('GET', '/api/comments?page=1&per_page=20&filters%5Bkeyword%5D=x', '', $admin),
    422,
    'E0001',
    'filters.keyword',
    '不支援此欄位'
);
adminCheck(
    adminRequest('GET', '/api/comments?page=1&per_page=20', '{}', $admin),
    422,
    'E0001',
    '_body',
    '此 API 不接受 Request Body'
);
echo "PASS admin comments include hidden content and preserve field contract\n";

// 狀態端點先驗登入與角色、再驗輸入及目標；管理員 ID 隱藏為不存在。
$statusPath = '/api/members/' . $firstId . '/status';
$disabled = '{"status":"disabled"}';
$active = '{"status":"active"}';
adminCheck(adminRequest('PUT', '/api/members/nope/status', '{"status":false}', $first), 403, 'E0003');
adminCheck(adminRequest('PUT', $statusPath, $disabled), 401, 'E0002');
adminCheck(adminRequest('PUT', '/api/members/nope/status', $disabled, $admin), 422, 'E0001', 'member_id', '欄位格式或範圍不正確');
adminCheck(adminRequest('PUT', $statusPath . '?x=1', $disabled, $admin), 422, 'E0001', 'x', '不支援此欄位');
adminCheck(adminRequest('PUT', $statusPath, '{}', $admin), 422, 'E0001', 'status', '此欄位為必填');
adminCheck(adminRequest('PUT', $statusPath, '{"status":null}', $admin), 422, 'E0001', 'status', '此欄位不可為 null');
adminCheck(adminRequest('PUT', $statusPath, '{"status":1}', $admin), 422, 'E0001', 'status', '欄位型態不正確');
adminCheck(adminRequest('PUT', $statusPath, '{"status":"paused"}', $admin), 422, 'E0001', 'status', '欄位格式或範圍不正確');
adminCheck(adminRequest('PUT', $statusPath, '{"status":"active","extra":1}', $admin), 422, 'E0001', 'extra', '不支援此欄位');
adminCheck(adminRequest('PUT', '/api/members/' . $adminId . '/status', $disabled, $admin), 404, 'E0004');
adminCheck(adminRequest('PUT', '/api/members/9007199254740991/status', $disabled, $admin), 404, 'E0004');

// 同值不撤銷 session；真正停用同交易撤銷所有舊登入，但保留內容與未到期鎖定。
$firstEmail = adminSql("SELECT email FROM accounts WHERE id=$firstId");
$otherLogin = adminRequest(
    'POST',
    '/api/auth/login',
    json_encode(['email' => $firstEmail, 'password' => 'Abc1234'], JSON_THROW_ON_ERROR)
);
adminCheck($otherLogin, 200);
$otherToken = $otherLogin[1]['data']['access_token'];
$before = adminSql("SELECT count(*) FROM login_sessions WHERE account_id=$firstId AND revoked_at IS NOT NULL");
$same = adminRequest('PUT', $statusPath, $active, $admin);
adminCheck($same, 200);
assert($same[1]['data'] === []);
assert(
    adminSql(
        "SELECT count(*) FROM login_sessions WHERE account_id=$firstId AND revoked_at IS NOT NULL"
    ) === $before
);
adminSql("UPDATE accounts SET locked_until=now()+interval '1 hour' WHERE id=$firstId");
$until = adminSql("SELECT locked_until FROM accounts WHERE id=$firstId");
$contentBefore = adminSql(
    ("SELECT a.visibility||'|'||c.visibility FROM articles a JOIN " .
        "comments c ON c.article_id=a.id WHERE a.id=$articleId AND " .
        "c.id=$publicId")
);
adminCheck(adminRequest('PUT', $statusPath, $disabled, $admin), 200);
assert(adminSql("SELECT status FROM accounts WHERE id=$firstId") === 'disabled');
assert(adminSql("SELECT count(*) FROM login_sessions WHERE account_id=$firstId AND revoked_at IS NULL") === '0');
assert(adminSql("SELECT locked_until FROM accounts WHERE id=$firstId") === $until);
assert(
    adminSql(
        ("SELECT a.visibility||'|'||c.visibility FROM articles a JOIN " .
            "comments c ON c.article_id=a.id WHERE a.id=$articleId AND " .
            "c.id=$publicId")
    ) === $contentBefore
);
adminCheck(adminRequest('GET', '/api/me', '', $first), 403, 'E0102');
adminCheck(adminRequest('GET', '/api/me', '', $otherToken), 403, 'E0102');
adminCheck(adminRequest('PUT', $statusPath, $disabled, $admin), 200);
adminCheck(adminRequest('PUT', $statusPath, $active, $admin), 200);
assert(adminSql("SELECT locked_until FROM accounts WHERE id=$firstId") === $until);
adminCheck(adminRequest('GET', '/api/me', '', $first), 401, 'E0002');
adminCheck(adminRequest('GET', '/api/me', '', $otherToken), 401, 'E0002');
echo "PASS status authorization, validation, atomic revoke, idempotence and lock/content preservation\n";
