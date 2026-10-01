<?php

declare(strict_types=1);

// 此測試會改設定、登入失敗事件與帳號狀態，必須使用全新獨立資料庫。
if (! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket07_test')) {
    throw new RuntimeException('請使用 ticket07_test 開頭的隔離 Compose project');
}
$base = getenv('API_BASE') ?: 'http://localhost:18087';
$path = '/api/settings/login-lockout';

/** 經真實 HTTP 傳送原始 JSON，驗證 Laravel 路由、認證、解析和共同回應。 */
function settingsRequest(string $method, string $path, string $body = '', ?string $token = null): array
{
    global $base;
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer '.$token;
    }
    $curl = curl_init($base.$path);
    curl_setopt_array($curl, [
        CURLOPT_CUSTOMREQUEST => $method, CURLOPT_POSTFIELDS => $body, CURLOPT_HTTPHEADER => $headers,
        CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 20,
    ]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
    $result = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
    curl_close($curl);

    return $result;
}

/** 比較跨後端共同碼、文案和欄位錯誤，避免只驗 HTTP 狀態。 */
function settingsCheck(array $result, int $status, ?string $code = null, ?string $field = null, ?string $detail = null): void
{
    assert($result[0] === $status, 'HTTP expected '.$status.', got '.$result[0]);
    assert($result[1]['success'] === ($code === null));
    if ($code === null) {
        assert($result[1]['message'] === 'OK');

        return;
    }
    $messages = [
        'E0001' => '欄位資料不正確，請確認後再送出',
        'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能',
        'E0005' => '系統暫時無法處理，請稍後再試',
        'E0101' => '帳號或密碼錯誤',
        'E0102' => '此帳號已停用，請聯絡系統管理員',
        'E0105' => '登入失敗次數過多，請稍後再試',
        'E9999' => '系統暫時無法處理，請稍後再試',
    ];
    assert($result[1]['error_code'] === $code && $result[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
    assert(is_string($result[1]['log_id']) && strlen($result[1]['log_id']) === 36);
    if ($field !== null) {
        assert($result[1]['errors'][$field] === [$detail]);
    }
}

/** 測試 fixture 只進獨立 Compose 的 owner DB，公開行為仍由 HTTP 斷言。 */
function settingsSql(string $sql): string
{
    $pipes = [];
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At', '-c', $sql],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes
    );
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

/** 每位測試使用者經正式註冊與登入取得 token，避免依賴展示帳號。 */
function settingsMember(string $prefix): array
{
    $email = $prefix.'-'.bin2hex(random_bytes(5)).'@example.test';
    settingsCheck(settingsRequest('POST', '/api/auth/register', json_encode(['email' => $email, 'password' => 'Abc1234', 'nickname' => '設定測試'], JSON_THROW_ON_ERROR)), 201);
    $login = settingsRequest('POST', '/api/auth/login', json_encode(['email' => $email, 'password' => 'Abc1234'], JSON_THROW_ON_ERROR));
    settingsCheck($login, 200);

    return [$email, $login[1]['data']['user']['id'], $login[1]['data']['access_token']];
}

/** 將四欄完整送出，確認成功只回空 object；失敗不會偽裝成局部成功。 */
function settingsPut(array $values, string $token): array
{
    global $path;
    $result = settingsRequest('PUT', $path, json_encode($values, JSON_THROW_ON_ERROR), $token);
    settingsCheck($result, 200);
    assert($result[1]['data'] === []);

    return $result;
}

[$memberEmail, $memberId, $member] = settingsMember('settings-member');
[$adminEmail, $adminId, $admin] = settingsMember('settings-admin');
settingsSql("UPDATE accounts SET role='admin' WHERE id=$adminId");
$defaults = ['enabled' => true, 'window_minutes' => 5, 'failure_threshold' => 5, 'lock_seconds' => 300];
$read = settingsRequest('GET', $path, '', $admin);
settingsCheck($read, 200);
assert($read[1]['data'] === $defaults && array_keys($read[1]['data']) === array_keys($defaults));
settingsCheck(settingsRequest('GET', $path), 401, 'E0002');
settingsCheck(settingsRequest('PUT', $path, '{', $member), 403, 'E0003');
settingsCheck(settingsRequest('GET', $path.'?x=1', '', $member), 403, 'E0003');
settingsCheck(settingsRequest('PUT', $path, '{}'), 401, 'E0002');
settingsCheck(settingsRequest('GET', $path, '', 'bad-token'), 401, 'E0002');

// 先檢查 GET Body，再檢查未知／重複 Query；PUT 只接受完整且型態精確的 JSON object。
settingsCheck(settingsRequest('GET', $path, '{}', $admin), 422, 'E0001', '_body', '此 API 不接受 Request Body');
settingsCheck(settingsRequest('GET', $path.'?x=1', '', $admin), 422, 'E0001', 'x', '不支援此欄位');
settingsCheck(settingsRequest('PUT', $path.'?x=1&x=2', '{}', $admin), 422, 'E0001', 'x', '此參數不可重複');
settingsCheck(settingsRequest('PUT', $path, '', $admin), 422, 'E0001', '_body', 'Request Body 必須為 JSON object');
settingsCheck(settingsRequest('PUT', $path, '[]', $admin), 422, 'E0001', '_body', 'Request Body 必須為 JSON object');
settingsCheck(settingsRequest('PUT', $path, '{', $admin), 400, 'E0005');
foreach (['enabled' => false, 'window_minutes' => 1, 'failure_threshold' => 1, 'lock_seconds' => 1] as $name => $value) {
    $missing = $defaults;
    unset($missing[$name]);
    settingsCheck(settingsRequest('PUT', $path, json_encode($missing), $admin), 422, 'E0001', $name, '此欄位為必填');
    settingsCheck(settingsRequest('PUT', $path, json_encode(array_replace($defaults, [$name => null])), $admin), 422, 'E0001', $name, '此欄位不可為 null');
    settingsCheck(settingsRequest('PUT', $path, json_encode(array_replace($defaults, [$name => is_bool($value) ? 1 : (string) $value])), $admin), 422, 'E0001', $name, '欄位型態不正確');
}
foreach (['window_minutes' => [0, 61], 'failure_threshold' => [0, 101], 'lock_seconds' => [0, 86401]] as $name => $invalid) {
    foreach ($invalid as $value) {
        settingsCheck(settingsRequest('PUT', $path, json_encode(array_replace($defaults, [$name => $value])), $admin), 422, 'E0001', $name, '欄位格式或範圍不正確');
    }
}
settingsCheck(settingsRequest('PUT', $path, json_encode(array_replace($defaults, ['extra' => 1])), $admin), 422, 'E0001', 'extra', '不支援此欄位');
assert(settingsRequest('GET', $path, '', $admin)[1]['data'] === $defaults);
echo "PASS settings read, role gate and exact validation\n";

// 同值與重排 JSON key 都不改帳號世代；數值更新與完整四欄上限仍可讀回。
$epoch = settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId");
settingsPut(array_reverse($defaults, true), $admin);
assert(settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === $epoch);
$upper = ['enabled' => true, 'window_minutes' => 60, 'failure_threshold' => 100, 'lock_seconds' => 86400];
settingsPut($upper, $admin);
assert(settingsRequest('GET', $path, '', $admin)[1]['data'] === $upper);

// 數值調整不能延長既有鎖定；視窗變大時，舊世代事件會重新進入計數。
$short = ['enabled' => true, 'window_minutes' => 1, 'failure_threshold' => 3, 'lock_seconds' => 120];
settingsPut($short, $admin);
settingsSql("INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '2 minutes' FROM accounts WHERE id=$memberId");
settingsSql("INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '30 seconds' FROM accounts WHERE id=$memberId");
$expanded = array_replace($short, ['window_minutes' => 5]);
settingsPut($expanded, $admin);
$wrong = json_encode(['email' => $memberEmail, 'password' => 'Wrong123'], JSON_THROW_ON_ERROR);
$locked = settingsRequest('POST', '/api/auth/login', $wrong);
settingsCheck($locked, 429, 'E0105');
assert(preg_match('/\r?\nRetry-After: [1-9][0-9]*\r?\n/i', $locked[2]) === 1);
$until = settingsSql("SELECT locked_until FROM accounts WHERE id=$memberId");
$numeric = array_replace($expanded, ['lock_seconds' => 600]);
settingsPut($numeric, $admin);
assert(settingsSql("SELECT locked_until FROM accounts WHERE id=$memberId") === $until);

// 真正關閉才清鎖與切換世代；舊事件保留，但重啟後不再計入新的失敗。
$failures = settingsSql("SELECT count(*) FROM login_failures WHERE account_id=$memberId");
$epoch = settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId");
$off = array_replace($numeric, ['enabled' => false]);
// 清帳號若失敗，四欄設定與原鎖定必須一同回滾，不能出現半套關閉。
settingsSql('CREATE FUNCTION settings_test_fail_clear() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION \'isolated clear failure\'; END; $$; CREATE TRIGGER settings_test_fail_clear BEFORE UPDATE ON accounts FOR EACH ROW WHEN (NEW.role = \'member\') EXECUTE FUNCTION settings_test_fail_clear();');
try {
    settingsCheck(settingsRequest('PUT', $path, json_encode($off), $admin), 500, 'E9999');
    assert(settingsRequest('GET', $path, '', $admin)[1]['data'] === $numeric);
    assert(settingsSql("SELECT locked_until FROM accounts WHERE id=$memberId") === $until);
    assert(settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === $epoch);
} finally {
    settingsSql('DROP TRIGGER settings_test_fail_clear ON accounts; DROP FUNCTION settings_test_fail_clear();');
}
settingsPut($off, $admin);
assert(settingsSql("SELECT locked_until IS NULL FROM accounts WHERE id=$memberId") === 't');
assert((int) settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === (int) $epoch + 1);
assert(settingsSql("SELECT count(*) FROM login_failures WHERE account_id=$memberId") === $failures);
settingsPut($off, $admin);
assert((int) settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === (int) $epoch + 1);
settingsCheck(settingsRequest('POST', '/api/auth/login', $wrong), 401, 'E0101');
$on = array_replace($off, ['enabled' => true]);
settingsPut($on, $admin);
settingsCheck(settingsRequest('POST', '/api/auth/login', $wrong), 401, 'E0101');
assert(settingsSql("SELECT locked_until IS NULL FROM accounts WHERE id=$memberId") === 't');

// 已到期鎖定在下次登入才解除並換世代，避免舊視窗事件造成立即重鎖。
settingsPut(array_replace($on, ['failure_threshold' => 2]), $admin);
settingsSql("UPDATE accounts SET locked_until=clock_timestamp()-interval '1 second' WHERE id=$memberId");
$beforeExpiry = settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId");
settingsCheck(settingsRequest('POST', '/api/auth/login', $wrong), 401, 'E0101');
assert((int) settingsSql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === (int) $beforeExpiry + 1);
assert(settingsSql("SELECT locked_until IS NULL FROM accounts WHERE id=$memberId") === 't');
echo "PASS whole update, window expansion, lock expiry and disable transition\n";

// 停用與鎖定互不混用；設定關閉不得把停用會員恢復成可登入。
settingsSql("UPDATE accounts SET status='disabled' WHERE id=$memberId");
settingsPut(array_replace($on, ['enabled' => false]), $admin);
assert(settingsSql("SELECT status FROM accounts WHERE id=$memberId") === 'disabled');
settingsCheck(settingsRequest('POST', '/api/auth/login', $wrong), 403, 'E0102');

// 兩份不同完整值同時送出，最後資料只能等於其中一整份，不能混合各欄。
$a = ['enabled' => false, 'window_minutes' => 7, 'failure_threshold' => 11, 'lock_seconds' => 333];
$b = ['enabled' => true, 'window_minutes' => 9, 'failure_threshold' => 13, 'lock_seconds' => 555];
$multi = curl_multi_init();
$handles = [];
foreach ([$a, $b] as $values) {
    $curl = curl_init($base.$path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => 'PUT', CURLOPT_POSTFIELDS => json_encode($values), CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json', 'Authorization: Bearer '.$admin], CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20]);
    curl_multi_add_handle($multi, $curl);
    $handles[] = $curl;
}
do {
    curl_multi_exec($multi, $running);
    if ($running > 0) {
        curl_multi_select($multi, 1);
    }
} while ($running > 0);
foreach ($handles as $curl) {
    assert(curl_getinfo($curl, CURLINFO_RESPONSE_CODE) === 200);
    curl_multi_remove_handle($multi, $curl);
    curl_close($curl);
}
curl_multi_close($multi);
$final = settingsRequest('GET', $path, '', $admin);
settingsCheck($final, 200);
assert($final[1]['data'] === $a || $final[1]['data'] === $b);
echo "PASS disabled member isolation and concurrent full writes\n";
