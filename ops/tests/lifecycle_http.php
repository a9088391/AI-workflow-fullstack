<?php

declare(strict_types=1);

// 本檔為獨立執行的 HTTP 驗收入口，輔助函式與案例保留同檔以便直接執行。
// phpcs:disable PSR1.Files.SideEffects

require_once __DIR__ . '/http_fixture.php';
// 所有 fixture 操作之前先確認斷言有效，不能在測試檢查被關閉時仍送出寫入。
assertHttpFixtureAssertions();

// 只可對 Ticket 05 專用資料庫執行；此測試會用 SQL fixture 調整留言、文章與帳號狀態。
if ((getenv('COMPOSE_PROJECT_NAME') ?: '') !== 'ticket05_test') {
    throw new RuntimeException('請使用 ticket05_test的隔離 Compose project');
}
$lifecycleBase = getenv('API_BASE') ?: 'http://localhost:8081';
assertHttpFixture(['laravel' => $lifecycleBase]);

/** 經真實 HTTP 傳送原始 JSON，讓認證、解析與公開回應一同受驗證。 */
function lifecycleRequest(string $method, string $path, string $body = '', ?string $token = null): array
{
    global $lifecycleBase;
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer ' . $token;
    }
    $curl = curl_init($lifecycleBase . $path);
    curl_setopt_array(
        $curl,
        [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HEADER => true,
            CURLOPT_TIMEOUT => 15
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
        substr($raw, $length)
    ];
}

/** 比對跨後端共同錯誤碼與精確文案，避免測試只接受 Laravel 原生訊息。 */
function lifecycleCheck(
    array $result,
    int $status,
    ?string $code = null,
    ?string $field = null,
    ?string $detail = null
): void {
    assert($result[0] === $status, 'HTTP status expected ' . $status . ', got ' . $result[0]);
    assert($result[1]['success'] === ($code === null));
    if ($code === null) {
        assert($result[1]['message'] === 'OK');
        if ($status === 201) {
            assert(json_decode($result[2], false, 512, JSON_THROW_ON_ERROR)->data instanceof stdClass);
            assert($result[1]['data'] === []);
        }

        return;
    }
    $messages = [
        'E0001' => '欄位資料不正確，請確認後再送出',
        'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能',
        'E0004' => '找不到指定資料或 API',
        'E0005' => '系統暫時無法處理，請稍後再試',
        'E0201' => '內容已更新，請確認最新內容後再送出',
        'E9999' => '系統暫時無法處理，請稍後再試',
        'E0102' => '此帳號已停用，請聯絡系統管理員'
    ];
    assert($result[1]['error_code'] === $code && $result[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
    assert(is_string($result[1]['log_id']) && strlen($result[1]['log_id']) === 36);
    if ($field !== null) {
        assert($result[1]['errors'][$field] === [$detail]);
    }
}

/** fixture 只使用隔離 Compose DB；正式行為仍逐一由公開 HTTP 斷言。 */
function lifecycleSql(string $sql): string
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

/** 每次測試以新會員取得真實 JWT，排除既有展示帳號與資料影響。 */
function lifecycleMember(string $name, string $prefix): array
{
    $email = $prefix . '-' . bin2hex(random_bytes(6)) . '@example.test';
    lifecycleCheck(
        lifecycleRequest(
            'POST',
            '/api/auth/register',
            json_encode(
                ['email' => $email, 'password' => 'Abc1234', 'nickname' => $name],
                JSON_THROW_ON_ERROR
            )
        ),
        201
    );
    $login = lifecycleRequest(
        'POST',
        '/api/auth/login',
        json_encode(['email' => $email, 'password' => 'Abc1234'], JSON_THROW_ON_ERROR)
    );
    lifecycleCheck($login, 200);

    return [$login[1]['data']['user']['id'], $login[1]['data']['access_token']];
}

/** 成功更新必須是空 object，不能順便回傳未約定的資源或版本。 */
function lifecycleWrite(string $path, array $body, string $token, int $status = 200, ?string $code = null): void
{
    $result = lifecycleRequest('PUT', $path, json_encode($body, JSON_THROW_ON_ERROR), $token);
    lifecycleCheck($result, $status, $code);
    if ($status === 200) {
        assert($result[1]['data'] === [] && json_decode($result[2])->data instanceof stdClass);
    }
}

/** 比對完整資料列，讓拒絕、同值及回滾測試能捕捉未允許的時間／版本／內容副作用。 */
function lifecycleSnapshot(string $table, int $id): string
{
    return lifecycleSql("SELECT row_to_json(t)::text FROM $table t WHERE id=$id");
}

/** 持鎖後觀察真實 HTTP 等鎖，再提交 fixture；不以 sleep 猜測競爭順序。 */
function lifecycleRace(string $lockSql, string $commitSql, array $requests, string|array $waitTable): array
{
    global $lifecycleBase;
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
    $multi = curl_multi_init();
    $handles = [];
    try {
        // fixture 先確定取得目標鎖，才讓 HTTP 進入共同認證與寫入管線。
        fwrite($pipes[0], "BEGIN;\nDO \$\$ BEGIN PERFORM 1 $lockSql; END \$\$;\nSELECT 'locked';\n");
        fflush($pipes[0]);
        assert(trim(fgets($pipes[1]) ?: '') === 'locked', 'fixture 未取得鎖');
        foreach ($requests as $index => [$path, $body, $token]) {
            $curl = curl_init($lifecycleBase . $path);
            curl_setopt_array(
                $curl,
                [
                    CURLOPT_CUSTOMREQUEST => 'PUT',
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
            $handles[] = $curl;
            curl_multi_add_handle($multi, $curl);
            // 反向競爭先讓編修持有文章鎖並等待留言鎖，才送出下架，確立寫入先完成。
            if (is_array($waitTable)) {
                $stageTable = $waitTable[$index];
                $deadline = microtime(true) + 8;
                do {
                    curl_multi_exec($multi, $running);
                    $stageWaiting = (int) lifecycleSql(
                        ("SELECT count(*) FROM pg_stat_activity WHERE datname='articles' " .
                            "AND usename='articles_app' AND wait_event_type='Lock' AND query " .
                            "LIKE '%\"$stageTable\"%for update%'")
                    ) > 0;
                    if (! $stageWaiting && $running > 0) {
                        curl_multi_select($multi, 0.02);
                    }
                } while (! $stageWaiting && $running > 0 && microtime(true) < $deadline);
                assert($stageWaiting, '分階段 HTTP 未等待指定鎖');
            }
        }
        $lockFilter = is_array($waitTable) ? '' : " AND query LIKE '%\"$waitTable\"%for update%'";
        $deadline = microtime(true) + 8;
        do {
            curl_multi_exec($multi, $running);
            $waiting = (int) lifecycleSql(
                ("SELECT count(*) FROM pg_stat_activity WHERE datname='articles' " .
                    "AND usename='articles_app' AND wait_event_type='Lock'$lockFilter")
            );
            if ($waiting < count($requests) && $running > 0) {
                curl_multi_select($multi, 0.02);
            }
        } while ($waiting < count($requests) && $running > 0 && microtime(true) < $deadline);
        assert($waiting >= count($requests), 'HTTP 未實際等待指定列鎖');

        // fixture 的狀態轉移先提交，等待中的 API 必須依提交後狀態判定。
        fwrite($pipes[0], "$commitSql;\nCOMMIT;\n");
        fclose($pipes[0]);
        stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        assert(proc_close($process) === 0, '競爭 fixture 失敗: ' . $error);
        do {
            curl_multi_exec($multi, $running);
            if ($running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($running > 0);
        $results = [];
        foreach ($handles as $curl) {
            $raw = curl_multi_getcontent($curl);
            assert(is_string($raw) && curl_errno($curl) === 0, curl_error($curl));
            $body = substr($raw, curl_getinfo($curl, CURLINFO_HEADER_SIZE));
            $results[] = [
                curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
                json_decode($body, true, 512, JSON_THROW_ON_ERROR),
                $body
            ];
        }

        return $results;
    } finally {
        // 任何失敗都 rollback 並釋放 fixture 資源，避免測試失敗留下鎖。
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
        foreach ($handles as $curl) {
            curl_multi_remove_handle($multi, $curl);
        }
        curl_multi_close($multi);
    }
}

$started = microtime(true);
[$authorId, $author] = lifecycleMember('作者', 'lifecycle-author');
[$parentAuthorId, $parentAuthor] = lifecycleMember('文章作者', 'lifecycle-parent');
[$otherId, $other] = lifecycleMember('其他會員', 'lifecycle-other');
[$adminId, $admin] = lifecycleMember('管理員', 'lifecycle-admin');
lifecycleSql("UPDATE accounts SET role='admin' WHERE id=$adminId");
lifecycleCheck(lifecycleRequest('POST', '/api/articles', '{"title":"文章","body":"原文"}', $parentAuthor), 201);
$articleId = (int) lifecycleSql("SELECT max(id) FROM articles WHERE author_id=$parentAuthorId");
lifecycleCheck(lifecycleRequest('POST', '/api/articles/' . $articleId . '/comments', '{"body":"留言"}', $author), 201);
$commentId = (int) lifecycleSql("SELECT max(id) FROM comments WHERE author_id=$authorId");

// 文章與留言以同一套案例驗證共同寫入契約，SQL 只作隔離 fixture 及副作用觀察。
foreach (
    [
    ['articles', $articleId, $parentAuthorId, $parentAuthor],
    ['comments', $commentId, $authorId, $author]
    ] as [$table, $id, $ownerId, $owner]
) {
    $path = '/api/' . $table . '/' . $id;
    $raw = "  原文😀\r\n第二行\n第三行\r<b>HTML 字面</b>  ";
    $edit = $table === 'articles' ? ['title' => "標題😀\r\n", 'body' => $raw, 'version' => 1]
        : ['body' => $raw, 'version' => 1];
    $original = lifecycleSnapshot($table, $id);

    // 認證先於所有格式與目標查詢；有效登入才會得到欄位或不存在錯誤。
    foreach ([$path, $path . '/visibility'] as $endpoint) {
        lifecycleCheck(lifecycleRequest('PUT', $endpoint . '?unknown=1', '{'), 401, 'E0002');
        lifecycleCheck(lifecycleRequest('PUT', $endpoint, '{', 'broken'), 401, 'E0002');
        lifecycleCheck(lifecycleRequest('PUT', $endpoint, '{', $owner), 400, 'E0005');
        lifecycleCheck(
            lifecycleRequest('PUT', $endpoint, '[]', $owner),
            422,
            'E0001',
            '_body',
            'Request Body 必須為 JSON object'
        );
        lifecycleCheck(
            lifecycleRequest('PUT', $endpoint, '', $owner),
            422,
            'E0001',
            '_body',
            'Request Body 必須為 JSON object'
        );
        lifecycleCheck(lifecycleRequest('PUT', $endpoint . '?x=1', '{}', $owner), 422, 'E0001', 'x', '不支援此欄位');
    }
    foreach (['bad', '0', '9007199254740992'] as $badId) {
        lifecycleCheck(
            lifecycleRequest('PUT', '/api/' . $table . '/' . $badId, json_encode($edit), $owner),
            422,
            'E0001',
            $table === 'articles' ? 'article_id' : 'comment_id',
            '欄位格式或範圍不正確'
        );
    }
    lifecycleWrite('/api/' . $table . '/9007199254740991', $edit, $owner, 404, 'E0004');
    lifecycleWrite($path, $edit, $other, 403, 'E0003');
    lifecycleWrite($path, $edit, $admin, 403, 'E0003');
    foreach (['1', true, [], 1.5, null, 0, -1, 9007199254740992] as $version) {
        $input = [...$edit, 'version' => $version];
        $detail = $version === null ? '此欄位不可為 null' : (is_int($version) ? '欄位格式或範圍不正確' : '欄位型態不正確');
        lifecycleCheck(lifecycleRequest('PUT', $path, json_encode($input), $owner), 422, 'E0001', 'version', $detail);
        lifecycleCheck(
            lifecycleRequest(
                'PUT',
                $path . '/visibility',
                json_encode(['visibility' => 'hidden', 'version' => $version]),
                $owner
            ),
            422,
            'E0001',
            'version',
            $detail
        );
    }
    $floatJson = str_replace('"version":1', '"version":1.0', json_encode($edit));
    lifecycleCheck(lifecycleRequest('PUT', $path, $floatJson, $owner), 422, 'E0001', 'version', '欄位型態不正確');
    if ($table === 'articles') {
        foreach (["x\0y", str_repeat('😀', 201)] as $title) {
            lifecycleCheck(
                lifecycleRequest('PUT', $path, json_encode([...$edit, 'title' => $title]), $owner),
                422,
                'E0001',
                'title',
                '欄位格式或範圍不正確'
            );
        }
    }
    lifecycleCheck(
        lifecycleRequest('PUT', $path, json_encode(array_diff_key($edit, ['version' => 1])), $owner),
        422,
        'E0001',
        'version',
        '此欄位為必填'
    );
    lifecycleCheck(
        lifecycleRequest('PUT', $path, json_encode([...$edit, 'created_backend' => 'gin']), $owner),
        422,
        'E0001',
        'created_backend',
        '不支援此欄位'
    );
    foreach (["x\0y", "\u{3000}\r\n", str_repeat('😀', $table === 'articles' ? 20001 : 2001)] as $body) {
        lifecycleCheck(
            lifecycleRequest('PUT', $path, json_encode([...$edit, 'body' => $body]), $owner),
            422,
            'E0001',
            'body',
            '欄位格式或範圍不正確'
        );
    }
    foreach (['VISIBLE', '', true, null] as $visibility) {
        $detail = $visibility === null ? '此欄位不可為 null' : (is_string($visibility) ? '欄位格式或範圍不正確' : '欄位型態不正確');
        lifecycleCheck(
            lifecycleRequest(
                'PUT',
                $path . '/visibility',
                json_encode(['visibility' => $visibility, 'version' => 1]),
                $owner
            ),
            422,
            'E0001',
            'visibility',
            $detail
        );
    }
    assert(lifecycleSnapshot($table, $id) === $original);

    // 真實編修保留碼點與換行；版本正確的同值不動任何資料，stale 同值仍拒絕。
    lifecycleWrite($path, $edit, $owner);
    $read = lifecycleRequest('GET', $path, '', $owner);
    lifecycleCheck($read, 200);
    assert($read[1]['data']['body'] === $raw && $read[1]['data']['version'] === 2);
    $snapshot = lifecycleSnapshot($table, $id);
    lifecycleWrite($path, [...$edit, 'version' => 2], $owner);
    assert(lifecycleSnapshot($table, $id) === $snapshot);
    lifecycleWrite($path, $edit, $owner, 409, 'E0201');
    assert(lifecycleSnapshot($table, $id) === $snapshot);
    $stored = json_decode($snapshot, true);
    $initial = json_decode($original, true);
    assert(
        $stored['created_at'] === $initial['created_at'] && $stored['created_backend']
            === $initial['created_backend'] && $stored['author_id'] === $initial['author_id']
    );

    // 作者下架仍能編修；非作者先收到404，管理員可讀卻不可改他人內文。
    lifecycleWrite($path . '/visibility', ['visibility' => 'hidden', 'version' => 2], $owner);
    lifecycleWrite($path, [...$edit, 'body' => $raw . '修改', 'version' => 3], $owner);
    lifecycleWrite($path, $edit, $other, 404, 'E0004');
    lifecycleWrite($path . '/visibility', ['visibility' => 'visible', 'version' => 1], $other, 404, 'E0004');
    lifecycleWrite($path, $edit, $admin, 403, 'E0003');
    $snapshot = lifecycleSnapshot($table, $id);
    lifecycleWrite($path . '/visibility', ['visibility' => 'hidden', 'version' => 4], $owner);
    assert(lifecycleSnapshot($table, $id) === $snapshot);

    // 管理員再次下架他人作者下架內容會接管恢復權，作者重複下架不能消除限制。
    lifecycleWrite($path . '/visibility', ['visibility' => 'hidden', 'version' => 4], $admin);
    $resource = lifecycleRequest('GET', $path, '', $owner)[1]['data'];
    assert($resource['version'] === 5 && $resource['can_restore'] === false);
    assert(lifecycleRequest('GET', $path, '', $admin)[1]['data']['can_restore'] === true);
    $snapshot = lifecycleSnapshot($table, $id);
    lifecycleWrite($path . '/visibility', ['visibility' => 'hidden', 'version' => 5], $owner);
    lifecycleWrite($path . '/visibility', ['visibility' => 'hidden', 'version' => 5], $admin);
    assert(lifecycleSnapshot($table, $id) === $snapshot);
    lifecycleWrite($path . '/visibility', ['visibility' => 'visible', 'version' => 1], $owner, 403, 'E0003');
    lifecycleWrite($path . '/visibility', ['visibility' => 'visible', 'version' => 5], $owner, 403, 'E0003');
    lifecycleWrite($path . '/visibility', ['visibility' => 'visible', 'version' => 4], $admin, 409, 'E0201');
    lifecycleWrite($path . '/visibility', ['visibility' => 'visible', 'version' => 5], $admin);
    assert(lifecycleSql("SELECT restore_control IS NULL FROM $table WHERE id=$id") === 't');
    $snapshot = lifecycleSnapshot($table, $id);
    lifecycleWrite($path . '/visibility', ['visibility' => 'visible', 'version' => 6], $owner);
    assert(lifecycleSnapshot($table, $id) === $snapshot);

    // 已核准 version 上限以既有 DB CHECK 擋下實際變更；同值仍成功且錯誤完整回滾。
    lifecycleSql("UPDATE $table SET version=9007199254740991 WHERE id=$id");
    $snapshot = lifecycleSnapshot($table, $id);
    lifecycleWrite($path, [...$edit, 'body' => $raw . '修改', 'version' => 9007199254740991], $owner);
    assert(lifecycleSnapshot($table, $id) === $snapshot);
    lifecycleWrite($path, [...$edit, 'body' => '不可溢位', 'version' => 9007199254740991], $owner, 500, 'E9999');
    lifecycleWrite(
        $path . '/visibility',
        ['visibility' => 'hidden', 'version' => 9007199254740991],
        $owner,
        500,
        'E9999'
    );
    assert(lifecycleSnapshot($table, $id) === $snapshot);
    lifecycleSql("UPDATE $table SET version=1 WHERE id=$id");
    echo "PASS $table validation, precedence, raw edit, no-op, stale, restore control and overflow rollback\n";
}

// 管理員身為作者時仍以 author 控制下架；此規則對文章及留言相同。
lifecycleSql("UPDATE accounts SET role='admin' WHERE id IN ($authorId,$parentAuthorId)");
foreach ([['articles', $articleId, $parentAuthor], ['comments', $commentId, $author]] as [$table, $id, $owner]) {
    lifecycleWrite('/api/' . $table . '/' . $id . '/visibility', ['visibility' => 'hidden', 'version' => 1], $owner);
    assert(lifecycleSql("SELECT restore_control FROM $table WHERE id=$id") === 'author');
    lifecycleWrite('/api/' . $table . '/' . $id . '/visibility', ['visibility' => 'visible', 'version' => 2], $owner);
}
lifecycleSql("UPDATE accounts SET role='member' WHERE id IN ($authorId,$parentAuthorId)");

// 管理員作者仍可用管理員恢復資格恢復另一管理員接管的自己的內容。
lifecycleCheck(lifecycleRequest('POST', '/api/articles', '{"title":"管理員自己的文章","body":"管理員原文"}', $admin), 201);
$adminArticleId = (int) lifecycleSql("SELECT max(id) FROM articles WHERE author_id=$adminId");
lifecycleCheck(
    lifecycleRequest(
        'POST',
        '/api/articles/' . $adminArticleId . '/comments',
        '{"body":"管理員自己的留言"}',
        $admin
    ),
    201
);
$adminCommentId = (int) lifecycleSql("SELECT max(id) FROM comments WHERE author_id=$adminId");
lifecycleSql("UPDATE accounts SET role='admin' WHERE id=$otherId");
foreach ([['articles', $adminArticleId], ['comments', $adminCommentId]] as [$table, $id]) {
    $path = '/api/' . $table . '/' . $id . '/visibility';
    lifecycleWrite($path, ['visibility' => 'hidden', 'version' => 1], $admin);
    lifecycleWrite($path, ['visibility' => 'hidden', 'version' => 2], $other);
    assert(lifecycleSql("SELECT restore_control FROM $table WHERE id=$id") === 'admin');
    lifecycleWrite($path, ['visibility' => 'visible', 'version' => 3], $admin);
    assert(lifecycleSql("SELECT restore_control IS NULL FROM $table WHERE id=$id") === 't');
}
lifecycleSql("UPDATE accounts SET role='member' WHERE id=$otherId");

// 下架父文章不洩漏他人留言；自己留言獨立可讀，但包括管理員作者均不可改內文。
lifecycleWrite(
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'hidden', 'version' => 3],
    $parentAuthor
);
$commentPath = '/api/comments/' . $commentId;
$snapshot = lifecycleSnapshot('comments', $commentId);
lifecycleWrite($commentPath, ['body' => '禁止編修', 'version' => 1], $author, 403, 'E0003');
lifecycleWrite($commentPath, ['body' => '禁止編修', 'version' => 1], $other, 404, 'E0004');
lifecycleWrite($commentPath, ['body' => '禁止編修', 'version' => 1], $parentAuthor, 404, 'E0004');
lifecycleSql("UPDATE accounts SET role='admin' WHERE id=$authorId");
lifecycleWrite($commentPath, ['body' => '禁止編修', 'version' => 1], $author, 403, 'E0003');
lifecycleSql("UPDATE accounts SET role='member' WHERE id=$authorId");
assert(lifecycleSnapshot('comments', $commentId) === $snapshot);
lifecycleWrite($commentPath . '/visibility', ['visibility' => 'hidden', 'version' => 3], $author);
lifecycleCheck(lifecycleRequest('GET', $commentPath, '', $author), 200);
lifecycleWrite($commentPath, ['body' => '自己的下架留言仍不可編修', 'version' => 4], $author, 403, 'E0003');
lifecycleWrite($commentPath . '/visibility', ['visibility' => 'visible', 'version' => 4], $author);
assert(lifecycleRequest('GET', '/api/articles/' . $articleId . '/comments?page=1&per_page=20')[0] === 404);
lifecycleWrite(
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'visible', 'version' => 4],
    $parentAuthor
);
echo "PASS admin own-content rule and hidden parent operation restrictions\n";

// 下架先提交時，等鎖留言修改必須重查父狀態並保持原文／版本；恢復權操作另有資格。
$before = lifecycleSnapshot('comments', $commentId);
$result = lifecycleRace(
    "FROM articles WHERE id=$articleId FOR UPDATE",
    "UPDATE articles SET visibility='hidden',restore_control='author',version=version+1 WHERE id=$articleId",
    [[$commentPath, ['body' => '等鎖不可改', 'version' => 5], $author]],
    'articles'
)[0];
lifecycleCheck($result, 403, 'E0003');
assert(lifecycleSnapshot('comments', $commentId) === $before);
lifecycleWrite(
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'visible', 'version' => 6],
    $parentAuthor
);

// 兩個真實寫入帶同一版本；在帳號鎖競爭後只一個成功，另一個不得覆蓋。
$before = json_decode(lifecycleSnapshot('comments', $commentId), true);
$results = lifecycleRace(
    "FROM accounts WHERE id=$authorId FOR UPDATE",
    'SELECT 1',
    [
        [$commentPath, ['body' => 'writer A', 'version' => 5], $author],
        [$commentPath, ['body' => 'writer B', 'version' => 5], $author]
    ],
    'accounts'
);
$statuses = array_column($results, 0);
sort($statuses);
assert($statuses === [200, 409]);
foreach ($results as $result) {
    lifecycleCheck($result, $result[0], $result[0] === 409 ? 'E0201' : null);
}
$after = json_decode(lifecycleSnapshot('comments', $commentId), true);
assert($after['version'] === $before['version'] + 1 && in_array($after['body'], ['writer A', 'writer B'], true));

// 文章也以真實並行寫入驗證同一版本只能提交一次，而非只測留言的共用 helper。
lifecycleCheck(lifecycleRequest('POST', '/api/articles', '{"title":"競爭文章","body":"原文"}', $other), 201);
$raceArticleId = (int) lifecycleSql("SELECT max(id) FROM articles WHERE author_id=$otherId");
$racePath = '/api/articles/' . $raceArticleId;
$results = lifecycleRace(
    "FROM accounts WHERE id=$otherId FOR UPDATE",
    'SELECT 1',
    [
        [$racePath, ['title' => 'writer A', 'body' => 'A', 'version' => 1], $other],
        [$racePath, ['title' => 'writer B', 'body' => 'B', 'version' => 1], $other]
    ],
    'accounts'
);
$statuses = array_column($results, 0);
sort($statuses);
assert($statuses === [200, 409]);
foreach ($results as $result) {
    lifecycleCheck($result, $result[0], $result[0] === 409 ? 'E0201' : null);
}
assert(lifecycleSql("SELECT version FROM articles WHERE id=$raceArticleId") === '2');

// 編修先取得父鎖時，下架須等待編修完成；完成的內容保留，不倒退或部分更新。
$results = lifecycleRace(
    "FROM comments WHERE id=$commentId FOR UPDATE",
    'SELECT 1',
    [
        [$commentPath, ['body' => '編修先完成', 'version' => 6], $author],
        [
            '/api/articles/' . $articleId . '/visibility',
            ['visibility' => 'hidden', 'version' => 7],
            $parentAuthor
        ]
    ],
    ['comments', 'articles']
);
foreach ($results as $result) {
    lifecycleCheck($result, 200);
}
assert(lifecycleSql("SELECT body || ':' || version FROM comments WHERE id=$commentId") === '編修先完成:7');
assert(lifecycleSql("SELECT visibility || ':' || version FROM articles WHERE id=$articleId") === 'hidden:8');
lifecycleWrite(
    '/api/articles/' . $articleId . '/visibility',
    ['visibility' => 'visible', 'version' => 8],
    $parentAuthor
);

// 停用先提交時優先403/E0102；舊登入被撤銷，等待寫入不改原文或時間。
$before = lifecycleSnapshot('articles', $articleId);
$result = lifecycleRace(
    "FROM accounts WHERE id=$parentAuthorId FOR UPDATE",
    ("UPDATE accounts SET status='disabled' WHERE id=$parentAuthorId; " .
        "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE " .
        "account_id=$parentAuthorId AND revoked_at IS NULL"),
    [
        [
            '/api/articles/' . $articleId,
            ['title' => '停用不可改', 'body' => '停用不可改', 'version' => 9],
            $parentAuthor
        ]
    ],
    'accounts'
)[0];
lifecycleCheck($result, 403, 'E0102');
assert(lifecycleSnapshot('articles', $articleId) === $before);

// 撤銷先提交後，等待帳號／session 的寫入必須401，不能改內容或更新時間。
$before = lifecycleSnapshot('comments', $commentId);
$result = lifecycleRace(
    "FROM accounts WHERE id=$authorId FOR UPDATE",
    "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$authorId AND revoked_at IS NULL",
    [[$commentPath, ['body' => '撤銷後不可改', 'version' => 7], $author]],
    'accounts'
)[0];
lifecycleCheck($result, 401, 'E0002');
assert(lifecycleSnapshot('comments', $commentId) === $before);
echo ('PASS parent hide, two writers and session revocation wait-lock ' .
    'races; ') . round(microtime(true) - $started, 2) . "s\n";
