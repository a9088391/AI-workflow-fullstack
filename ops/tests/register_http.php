<?php

declare(strict_types=1);

/** 真實 HTTP 註冊契約測試；只對隔離 Compose project 寫入隨機帳號。 */

if (! getenv('COMPOSE_PROJECT_NAME') || getenv('COMPOSE_PROJECT_NAME') === 'ai-workflow-fullstack') {
    throw new RuntimeException('請先設定隔離的 COMPOSE_PROJECT_NAME');
}
$base = getenv('API_BASE') ?: 'http://localhost:8081';

/** 保留原始 body 測試 JSON 解析與根型態錯誤。 */
function post(string $base, string $body, string $query = ''): array
{
    $curl = curl_init($base.'/api/auth/register'.$query);
    curl_setopt_array($curl, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => $body,
        CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json'],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 10,
    ]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException('HTTP 呼叫失敗：'.curl_error($curl));
    }
    $result = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode($raw, true, 512, JSON_THROW_ON_ERROR)];
    curl_close($curl);

    return $result;
}

/** 只檢查公開狀態與共同文案，不綁定後端私有實作。 */
function check(array $result, int $status, ?string $code = null, ?string $field = null, ?string $message = null): void
{
    [$actual, $body] = $result;
    if ($actual !== $status) {
        throw new RuntimeException(sprintf('預期 HTTP %d，實際 %d：%s', $status, $actual, json_encode($body, JSON_UNESCAPED_UNICODE)));
    }
    if ($code === null) {
        assert($body === ['success' => true, 'message' => 'OK', 'data' => []]);

        return;
    }
    assert($body['success'] === false && $body['error_code'] === $code);
    assert(preg_match('/\A[0-9a-f]{8}-[0-9a-f-]{27,}\z/i', $body['log_id']) === 1);
    if ($field !== null) {
        assert($body['errors'][$field] === [$message]);
    }
}

/** 用 curl_multi 同時送出相同 Email，確認唯一約束在競爭下只放行一次。 */
function concurrentPosts(string $base, string $body): array
{
    $multi = curl_multi_init();
    $handles = [];
    for ($index = 0; $index < 8; $index++) {
        $curl = curl_init($base.'/api/auth/register');
        curl_setopt_array($curl, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 20,
        ]);
        curl_multi_add_handle($multi, $curl);
        $handles[] = $curl;
    }
    do {
        curl_multi_exec($multi, $running);
        if ($running > 0) {
            curl_multi_select($multi, 1.0);
        }
    } while ($running > 0);

    $results = [];
    foreach ($handles as $curl) {
        $results[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(curl_multi_getcontent($curl), true, 512, JSON_THROW_ON_ERROR)];
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);

    return $results;
}

// 每次生成新 Email，重跑測試不覆寫既有帳號。
$suffix = bin2hex(random_bytes(6));
$valid = ['email' => "  TeSt+$suffix@Example.Test  ", 'password' => 'Abc1234', 'nickname' => '陳以澄'];
check(post($base, json_encode($valid, JSON_THROW_ON_ERROR)), 201);
check(post($base, json_encode($valid, JSON_THROW_ON_ERROR)), 422, 'E0001', 'email', '此 Email 已註冊');

$cases = [
    ['', 422, 'E0001', '_body', 'Request Body 必須為 JSON object'],
    ['[1]', 422, 'E0001', '_body', 'Request Body 必須為 JSON object'],
    ['{', 400, 'E0005', null, null],
    [json_encode($valid + ['role' => 'admin']), 422, 'E0001', 'role', '不支援此欄位'],
    [json_encode(array_replace($valid, ['password' => 'abc'])), 422, 'E0001', 'password', '欄位格式或範圍不正確'],
    [json_encode(array_replace($valid, ['password' => 'abc '])), 422, 'E0001', 'password', '欄位格式或範圍不正確'],
    [json_encode(array_replace($valid, ['nickname' => "\u{3000}\u{0085}"])), 422, 'E0001', 'nickname', '欄位格式或範圍不正確'],
];
foreach ($cases as [$body, $status, $code, $field, $message]) {
    check(post($base, $body), $status, $code, $field, $message);
}
check(post($base, json_encode($valid), '?junk=1'), 422, 'E0001', 'junk', '不支援此欄位');
check(post($base, json_encode($valid), '?junk=1&junk=2'), 422, 'E0001', 'junk', '此參數不可重複');

$race = json_encode(array_replace($valid, ['email' => "race-$suffix@example.test"]));
$results = concurrentPosts($base, $race);
$statuses = array_column($results, 0);
sort($statuses);
assert($statuses === [201, 422, 422, 422, 422, 422, 422, 422]);
foreach ($results as $result) {
    check($result, $result[0], $result[0] === 201 ? null : 'E0001', $result[0] === 201 ? null : 'email', $result[0] === 201 ? null : '此 Email 已註冊');
}

// PostgreSQL text 無法儲存 U+0000；純字元與夾字都必須沿用暱稱格式錯誤。
foreach (["\u{0000}", "阿\u{0000}明"] as $index => $nickname) {
    check(post($base, json_encode(array_replace($valid, ['email' => "nul-$index-$suffix@example.test", 'nickname' => $nickname]), JSON_THROW_ON_ERROR)), 422, 'E0001', 'nickname', '欄位格式或範圍不正確');
}

// 六個可見字元 \u0000 不是 NUL，合法文字仍應成功註冊。
check(post($base, json_encode(array_replace($valid, ['email' => "literal-$suffix@example.test", 'nickname' => '\\u0000']), JSON_THROW_ON_ERROR)), 201);
echo "HTTP registration checks passed\n";
