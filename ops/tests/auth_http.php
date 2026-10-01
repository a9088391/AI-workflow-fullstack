<?php

declare(strict_types=1);

// 只對明確隔離的後端執行真實 HTTP，避免污染展示帳號。
if (! str_starts_with(getenv('COMPOSE_PROJECT_NAME') ?: '', 'ticket02_')) {
    throw new RuntimeException('請使用 ticket02_ 開頭的隔離 Compose project');
}
$base = getenv('API_BASE') ?: 'http://localhost:18081';

/** 經公開 HTTP seam 保留原始 Body 與標頭，便於驗證解析和認證順序。 */
function authRequest(string $method, string $path, string $body = '', ?string $token = null): array
{
    global $base;
    $headers = ['Accept: application/json', 'Content-Type: application/json', 'Origin: http://localhost:8080'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer '.$token;
    }
    $curl = curl_init($base.$path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_POSTFIELDS => $body, CURLOPT_HTTPHEADER => $headers, CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 15]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);

    return [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
}

/** 用固定共同文案檢查結果，錯誤時不輸出可能含 token 的完整回應。 */
function authCheck(array $result, int $status, ?string $code = null): void
{
    assert($result[0] === $status, 'HTTP status expected '.$status.', got '.$result[0]);
    assert($result[1]['success'] === ($code === null));
    if ($code !== null) {
        assert($result[1]['error_code'] === $code);
        $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入', 'E0005' => '系統暫時無法處理，請稍後再試', 'E0101' => '帳號或密碼錯誤', 'E0102' => '此帳號已停用，請聯絡系統管理員', 'E0104' => '後台認證設定異常，請聯絡系統管理員', 'E0105' => '登入失敗次數過多，請稍後再試', 'E9999' => '系統暫時無法處理，請稍後再試'];
        assert($result[1]['message'] === $messages[$code]);
        assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
        assert(is_string($result[1]['log_id']) && strlen($result[1]['log_id']) === 36);
    }
}

// 尚不存在的帳號仍是認證失敗，不應落到路由 404。
authCheck(authRequest('POST', '/api/auth/login', '{"email":"unknown@example.test","password":"1234"}'), 401, 'E0101');

/** 以 argv 陣列啟動 Compose，測試 fixture／維運輸入只經 stdin，不透過 shell 插值。 */
function authCompose(array $arguments, string $input = '', bool $expectSuccess = true): array
{
    $pipes = [];
    $process = proc_open(array_merge(['docker', 'compose'], $arguments), [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    assert(is_resource($process));
    fwrite($pipes[0], $input);
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $status = proc_close($process);
    if ($expectSuccess && $status !== 0) {
        throw new RuntimeException('隔離 fixture 命令失敗，exit='.$status);
    }

    return [$status, $output, $error];
}

/** 寫入隔離金鑰後確認容器已讀到完整內容，避免把 bind mount 同步延遲誤判為 API 恢復失敗。 */
function authSetTestKey(string $path, string $contents): void
{
    if (file_put_contents($path, $contents) !== strlen($contents)) {
        throw new RuntimeException('隔離測試金鑰寫入失敗');
    }

    // 金鑰由唯讀 bind mount 供應；只比對雜湊，不將原文送到容器命令或測試輸出。
    $expected = hash('sha256', $contents);
    for ($attempt = 0; $attempt < 20; $attempt++) {
        $actual = trim(authCompose(['exec', '-T', 'laravel', 'php', '-r', 'echo hash_file("sha256", "/run/secrets/laravel-jwt.key");'])[1]);
        if (hash_equals($expected, $actual)) {
            return;
        }
        usleep(50_000);
    }

    throw new RuntimeException('隔離容器未讀到已寫入的測試金鑰');
}

/** 僅供缺少管理端點與時間旅行的 fixture；行為斷言仍由公開 HTTP 驗證。 */
function authSql(string $sql): string
{
    return trim(authCompose(['exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At'], $sql)[1]);
}

/** fixture 值走 PostgreSQL 字面跳脫，避免測試暱稱或 PHC 破壞 SQL。 */
function authLiteral(string $value): string
{
    return "'".str_replace("'", "''", $value)."'";
}

/** 送出登入請求，舊 token 可指定以驗證登入不沿用它。 */
function authLogin(string $email, string $password = 'Abc1234', ?string $token = null): array
{
    return authRequest('POST', '/api/auth/login', json_encode(['email' => $email, 'password' => $password], JSON_THROW_ON_ERROR), $token);
}

/** 以獨立的測試 signer 製作不合法 claims；不呼叫應用 JWT helper。 */
function authToken(array $claims, array $header = ['alg' => 'HS256', 'typ' => 'JWT']): string
{
    $key = base64_decode(trim(file_get_contents(getenv('AUTH_TEST_KEY'))), true);
    $first = rtrim(strtr(base64_encode(json_encode($header)), '+/', '-_'), '=');
    $second = rtrim(strtr(base64_encode(json_encode($claims)), '+/', '-_'), '=');

    return $first.'.'.$second.'.'.rtrim(strtr(base64_encode(hash_hmac('sha256', $first.'.'.$second, $key, true)), '+/', '-_'), '=');
}

/** 同時送出八次錯密碼，驗證資料庫鎖裁決不漏計且鎖定不延長。 */
function authConcurrent(string $email): array
{
    global $base;
    $multi = curl_multi_init();
    $handles = [];
    for ($index = 0; $index < 8; $index++) {
        $curl = curl_init($base.'/api/auth/login');
        curl_setopt_array($curl, [CURLOPT_POST => true, CURLOPT_POSTFIELDS => json_encode(['email' => $email, 'password' => 'wrong123']), CURLOPT_HTTPHEADER => ['Content-Type: application/json'], CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20]);
        curl_multi_add_handle($multi, $curl);
        $handles[] = $curl;
    }
    do {
        curl_multi_exec($multi, $running);
        if ($running > 0) {
            curl_multi_select($multi, 1);
        }
    } while ($running > 0);
    $statuses = [];
    foreach ($handles as $curl) {
        $statuses[] = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);
    sort($statuses);

    return $statuses;
}

$started = microtime(true);
$suffix = bin2hex(random_bytes(6));
$email = 'auth-'.$suffix.'@example.test';
authCheck(authRequest('POST', '/api/auth/register', json_encode(['email' => $email, 'password' => 'Abc1234', 'nickname' => '登入測試'])), 201);
$login = authLogin('  '.strtoupper($email).'  ', 'Abc1234', 'invalidOldToken');
authCheck($login, 200);
$token = $login[1]['data']['access_token'];
$user = $login[1]['data']['user'];
$id = $user['id'];
assert(array_keys($user) === ['id', 'email', 'nickname', 'role', 'status', 'created_at']);
assert(is_int($id) && $user['email'] === $email && $user['role'] === 'member' && $user['status'] === 'active');
assert($login[1]['data']['token_type'] === 'Bearer');
$parts = explode('.', $token);
$claims = json_decode(base64_decode(strtr($parts[1], '-_', '+/')), true, 512, JSON_THROW_ON_ERROR);
assert($claims['exp'] - $claims['iat'] === 28800 && $claims['iat'] === $claims['nbf']);
assert($claims['sub'] === (string) $id && strlen(base64_decode(strtr($claims['jti'], '-_', '+/'))) === 16);
$expiry = (new DateTimeImmutable('@'.$claims['exp']))->setTimezone(new DateTimeZone('Asia/Taipei'))->format('Y-m-d H:i:s');
assert($login[1]['data']['expires_at'] === $expiry);
$me = authRequest('GET', '/api/me', '', $token);
authCheck($me, 200);
assert($me[1]['data'] === $user);
assert(stripos($me[2], 'Access-Control-Allow-Origin: http://localhost:8080') !== false);
$second = authLogin($email);
authCheck($second, 200);
$other = $second[1]['data']['access_token'];
assert($token !== $other);
// 身分使用資料庫目前角色；JWT 本身不保存或授予角色。
authSql("UPDATE accounts SET role='admin' WHERE id=$id;");
assert(authRequest('GET', '/api/me', '', $token)[1]['data']['role'] === 'admin');
authSql("UPDATE accounts SET role='member' WHERE id=$id;");
echo "PASS login, account shape, independent sessions, fixed expiry, CORS\n";

// 欄位、Query、JSON 與認證優先序都比較公開共同文案。
authCheck(authRequest('GET', '/api/me?x=1', '{'), 401, 'E0002');
authCheck(authRequest('POST', '/api/auth/logout', '{'), 401, 'E0002');
foreach ([['', 422, 'E0001', '_body', 'Request Body 必須為 JSON object'], ['[]', 422, 'E0001', '_body', 'Request Body 必須為 JSON object'], ['{', 400, 'E0005', null, null], ['{}', 422, 'E0001', 'email', '此欄位為必填'], ['{"email":null,"password":"1234"}', 422, 'E0001', 'email', '此欄位不可為 null'], ['{"email":4,"password":"1234"}', 422, 'E0001', 'email', '欄位型態不正確'], [json_encode(['email' => $email, 'password' => '123', 'role' => 'admin']), 422, 'E0001', 'password', '欄位格式或範圍不正確']] as [$body,$status,$code,$field,$message]) {
    $result = authRequest('POST', '/api/auth/login', $body);
    authCheck($result, $status, $code);
    if ($field !== null) {
        assert($result[1]['errors'][$field] === [$message]);
    }
}
foreach (['/api/auth/login', '/api/auth/logout', '/api/me'] as $path) {
    $result = authRequest($path === '/api/me' ? 'GET' : 'POST', $path.'?x=1&x=2', '{}', $token);
    authCheck($result, 422, 'E0001');
    assert($result[1]['errors']['x'] === ['此參數不可重複']);
}
$result = authRequest('GET', '/api/me', '{', $token);
authCheck($result, 422, 'E0001');
assert($result[1]['errors']['_body'] === ['此 API 不接受 Request Body']);
authCheck(authRequest('POST', '/api/auth/logout', '{', $token), 400, 'E0005');
authCheck(authRequest('POST', '/api/auth/logout', '[]', $token), 422, 'E0001');
$result = authRequest('POST', '/api/auth/logout', '{"extra":1}', $token);
authCheck($result, 422, 'E0001');
assert($result[1]['errors']['extra'] === ['不支援此欄位']);
echo "PASS validation and protected authentication order\n";

// 每個非法 claim 用正確簽章，確保失敗不是只因簽章錯誤。
foreach (['none', 'RS256', 'HS512'] as $algorithm) {
    authCheck(authRequest('GET', '/api/me', '', authToken($claims, ['alg' => $algorithm, 'typ' => 'JWT'])), 401, 'E0002');
}
foreach (['iss' => 'ai-workflow-fullstack:django', 'aud' => ['ai-workflow-fullstack-api:laravel'], 'sub' => '0'.$id, 'jti' => $claims['jti'].'=', 'iat' => (string) $claims['iat'], 'nbf' => $claims['nbf'] + 1, 'exp' => $claims['exp'] + 1] as $field => $value) {
    authCheck(authRequest('GET', '/api/me', '', authToken(array_replace($claims, [$field => $value]))), 401, 'E0002');
}
foreach (array_keys($claims) as $field) {
    $missing = $claims;
    unset($missing[$field]);
    authCheck(authRequest('GET', '/api/me', '', authToken($missing)), 401, 'E0002');
}
$future = time() + 60;
$expired = time() - 28800;
foreach ([$future, $expired] as $issued) {
    authCheck(authRequest('GET', '/api/me', '', authToken(array_replace($claims, ['iat' => $issued, 'nbf' => $issued, 'exp' => $issued + 28800]))), 401, 'E0002');
}
$badSignature = $parts[0].'.'.$parts[1].'.'.str_repeat('A', 43);
authCheck(authRequest('GET', '/api/me', '', $badSignature), 401, 'E0002');
authCheck(authRequest('GET', '/api/me', '', authToken(array_replace($claims, ['jti' => rtrim(strtr(base64_encode(random_bytes(16)), '+/', '-_'), '=')]))), 401, 'E0002');
$jti = authLiteral($claims['jti']);
authSql("UPDATE login_sessions SET backend_id='django' WHERE jti=$jti;");
authCheck(authRequest('GET', '/api/me', '', $token), 401, 'E0002');
authSql("UPDATE login_sessions SET backend_id='laravel', issued_at=issued_at+interval '1 second', expires_at=expires_at+interval '1 second' WHERE jti=$jti;");
authCheck(authRequest('GET', '/api/me', '', $token), 401, 'E0002');
authSql("UPDATE login_sessions SET issued_at=issued_at-interval '1 second', expires_at=expires_at-interval '1 second' WHERE jti=$jti;");

$mismatchEmail = 'mismatch-'.$suffix.'@example.test';
authCheck(authRequest('POST', '/api/auth/register', json_encode(['email' => $mismatchEmail, 'password' => 'Abc1234', 'nickname' => '另一位'])), 201);
$mismatchId = authLogin($mismatchEmail)[1]['data']['user']['id'];
authSql("UPDATE login_sessions SET account_id=$mismatchId WHERE jti=$jti;");
authCheck(authRequest('GET', '/api/me', '', $token), 401, 'E0002');
authSql("UPDATE login_sessions SET account_id=$id WHERE jti=$jti;");
echo "PASS JWT algorithm, claims, signature, expiration and stored identity\n";

// 合法錯密碼才累計；格式錯誤與不存在帳號不應污染目前會員的計數。
authCheck(authLogin($email, '123'), 422, 'E0001');
for ($i = 0; $i < 4; $i++) {
    authCheck(authLogin($email, 'wrong123'), 401, 'E0101');
}
$locked = authLogin($email, 'wrong123');
authCheck($locked, 429, 'E0105');
assert(preg_match('/Retry-After: 300\r?\n/i', $locked[2]) === 1);
assert(stripos($locked[2], 'Access-Control-Expose-Headers: Retry-After') !== false);
authCheck(authRequest('GET', '/api/me', '', $token), 200);
$lockedUntil = authSql("SELECT locked_until FROM accounts WHERE id=$id;");
authCheck(authLogin($email), 429, 'E0105');
assert(authSql("SELECT locked_until FROM accounts WHERE id=$id;") === $lockedUntil);
authSql("UPDATE accounts SET locked_until=clock_timestamp()+interval '1.2 seconds' WHERE id=$id;");
$remaining = authLogin($email);
authCheck($remaining, 429, 'E0105');
assert(preg_match('/Retry-After: [12]\r?\n/i', $remaining[2]) === 1);
authSql("UPDATE accounts SET locked_until=clock_timestamp()-interval '1 second' WHERE id=$id;");
authCheck(authLogin($email, 'wrong123'), 401, 'E0101');
authCheck(authLogin($email), 200);
assert(authConcurrent($email) === [401, 401, 401, 401, 429, 429, 429, 429]);
echo "PASS threshold, retained sessions, remaining seconds, expiry/success reset, concurrent count\n";

// fixture 建立視窗外／內／未來事件，使用實際 DB 時間並保留微秒，生產時鐘不被替換。
authSql("UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE id=$id;
INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '301 seconds' FROM accounts WHERE id=$id;
INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()+interval '60 seconds' FROM accounts WHERE id=$id;
UPDATE login_lockout_settings SET failure_threshold=2 WHERE id=1;");
authCheck(authLogin($email, 'wrong123'), 401, 'E0101');
authCheck(authLogin($email, 'wrong123'), 429, 'E0105');
authSql("UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE id=$id;
INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '299 seconds' FROM accounts WHERE id=$id;");
authCheck(authLogin($email, 'wrong123'), 429, 'E0105');
authSql("UPDATE login_lockout_settings SET enabled=false,failure_threshold=5 WHERE id=1; UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE id=$id;");
for ($i = 0; $i < 6; $i++) {
    authCheck(authLogin($email, 'wrong123'), 401, 'E0101');
}
authSql('UPDATE login_lockout_settings SET enabled=true WHERE id=1;');
authCheck(authLogin($email, 'wrong123'), 401, 'E0101');
authCheck(authLogin($email), 200);
echo "PASS sliding window and disabled counting fixtures\n";

// 隔離 DB trigger 只對 fixture 對齊事件時間，不替換應用或資料庫時鐘。
// 對齊值取本次真實登入事件 NEW.occurred_at，可驗證左邊界相等排除、加 1μs 納入。
foreach (['0 microseconds' => 401, '1 microsecond' => 429] as $offset => $status) {
    authSql("UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE id=$id;
    UPDATE login_lockout_settings SET failure_threshold=2 WHERE id=1;
    INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '10 minutes' FROM accounts WHERE id=$id;
    CREATE FUNCTION auth_test_boundary() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS \$\$
    BEGIN
      IF NEW.account_id=$id THEN
        UPDATE public.login_failures SET occurred_at=NEW.occurred_at-interval '5 minutes'+interval '$offset' WHERE account_id=NEW.account_id AND failure_epoch=NEW.failure_epoch;
      END IF;
      RETURN NEW;
    END; \$\$;
    CREATE TRIGGER auth_test_boundary BEFORE INSERT ON login_failures FOR EACH ROW EXECUTE FUNCTION auth_test_boundary();");
    try {
        authCheck(authLogin($email, 'wrong123'), $status, $status === 401 ? 'E0101' : 'E0105');
    } finally {
        authSql('DROP TRIGGER auth_test_boundary ON login_failures; DROP FUNCTION auth_test_boundary();');
    }
}
authSql("UPDATE login_lockout_settings SET failure_threshold=5 WHERE id=1; UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE id=$id;");
echo "PASS exact sliding left boundary and +1 microsecond\n";

// 簽章與 session 身分有效才顯示停用；撤銷 token 的停用提示優先，恢復後仍不得復活。
authSql("UPDATE accounts SET status='disabled',locked_until=clock_timestamp()+interval '300 seconds' WHERE id=$id; UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$id;");
authCheck(authLogin($email), 403, 'E0102');
authCheck(authRequest('GET', '/api/me', '', $token), 403, 'E0102');
authCheck(authRequest('GET', '/api/me', '', $badSignature), 401, 'E0002');
authSql("UPDATE accounts SET status='active' WHERE id=$id;");
authCheck(authRequest('GET', '/api/me', '', $token), 401, 'E0002');
authCheck(authLogin($email), 429, 'E0105');
authSql("UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE id=$id;");
$third = authLogin($email)[1]['data']['access_token'];
$fourth = authLogin($email)[1]['data']['access_token'];
authCheck(authRequest('POST', '/api/auth/logout', '{}', $third), 200);
authCheck(authRequest('GET', '/api/me', '', $third), 401, 'E0002');
authCheck(authRequest('POST', '/api/auth/logout', '', $third), 401, 'E0002');
authCheck(authRequest('GET', '/api/me', '', $fourth), 200);
echo "PASS disabled precedence, restored account, logout scope and replay\n";

// 各種不支援 PHC 都是設定異常；只保存隔離 fixture 原雜湊於記憶體以便復原。
$hash = authSql("SELECT password_hash FROM accounts WHERE id=$id;");
foreach (['broken', str_replace('argon2id', 'argon2i', $hash), str_replace('m=19456', 'm=65536', $hash), substr($hash, 0, -1)] as $badHash) {
    authSql('UPDATE accounts SET password_hash='.authLiteral($badHash)." WHERE id=$id;");
    authCheck(authLogin($email, 'wrong123'), 500, 'E0104');
}
authSql('UPDATE accounts SET password_hash='.authLiteral($hash)." WHERE id=$id;");
for ($i = 0; $i < 4; $i++) {
    authCheck(authLogin($email, 'wrong123'), 401, 'E0101');
}
authCheck(authLogin($email), 200);
echo "PASS malformed hashes fail as E0104 without counting\n";

// 本機 command 從 LF framing stdin 讀入，既有會員不得被提升成管理員。
$admin = authSql("SELECT email FROM accounts WHERE role='admin' ORDER BY id LIMIT 1;");
if ($admin === '') {
    $admin = 'admin-'.$suffix.'@example.test';
    authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:create-initial-admin'], "$admin\n管理員\nAdmin123\n");
} else {
    authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:reset-admin-password'], "$admin\nAdmin123\n");
}
for ($i = 0; $i < 6; $i++) {
    authCheck(authLogin($admin, 'wrong123'), 401, 'E0101');
}
$adminOne = authLogin($admin, 'Admin123')[1]['data']['access_token'];
$adminTwo = authLogin($admin, 'Admin123')[1]['data']['access_token'];
$adminId = authLogin($admin, 'Admin123')[1]['data']['user']['id'];
// 模擬另後端 session，重設仍須撤銷該管理員全部舊登入。
$foreignJti = rtrim(strtr(base64_encode(random_bytes(16)), '+/', '-_'), '=');
authSql('INSERT INTO login_sessions(jti,account_id,backend_id,issued_at,expires_at) VALUES ('.authLiteral($foreignJti).",$adminId,'django',date_trunc('second',clock_timestamp()),date_trunc('second',clock_timestamp())+interval '8 hours');");
assert(authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:reset-admin-password'], "$email\nChanged123\n", false)[0] !== 0);
assert(authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:reset-admin-password'], "$admin\nChanged123\r\n", false)[0] !== 0);
assert(authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:reset-admin-password'], "$admin\nChanged123", false)[0] !== 0);

// 撤銷步驟故障必須整筆回滾，包括已更新的密碼；stdout 不得先印出成功。
authSql("CREATE FUNCTION auth_test_reset_failure() RETURNS trigger LANGUAGE plpgsql AS \$\$ BEGIN IF NEW.account_id=$adminId THEN RAISE EXCEPTION 'isolated reset failure'; END IF; RETURN NEW; END; \$\$;
CREATE TRIGGER auth_test_reset_failure BEFORE UPDATE ON login_sessions FOR EACH ROW EXECUTE FUNCTION auth_test_reset_failure();");
try {
    $failedReset = authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:reset-admin-password'], "$admin\nRejected123\n", false);
    assert($failedReset[0] !== 0 && ! str_contains($failedReset[1], '管理員密碼已重設'));
    authCheck(authLogin($admin, 'Admin123'), 200);
    authCheck(authLogin($admin, 'Rejected123'), 401, 'E0101');
    authCheck(authRequest('GET', '/api/me', '', $adminOne), 200);
} finally {
    authSql('DROP TRIGGER auth_test_reset_failure ON login_sessions; DROP FUNCTION auth_test_reset_failure();');
}
authCompose(['exec', '-T', 'laravel', 'php', 'artisan', 'app:reset-admin-password'], "$admin\nChanged123\n");
authCheck(authRequest('GET', '/api/me', '', $adminOne), 401, 'E0002');
authCheck(authRequest('GET', '/api/me', '', $adminTwo), 401, 'E0002');
assert(authSql('SELECT revoked_at IS NOT NULL FROM login_sessions WHERE jti='.authLiteral($foreignJti).';') === 't');
authCheck(authLogin($admin, 'Admin123'), 401, 'E0101');
authCheck(authLogin($admin, 'Changed123'), 200);
authCheck(authLogin($email), 200);
echo "PASS administrator eligibility, safe reset and all-backend revocation\n";

// 實際資料庫停用與僅屬測試的金鑰毀損，確認 fail closed 不被包成 401 或登出成功。
try {
    authCompose(['stop', 'db']);
    authCheck(authRequest('GET', '/api/me', '', $fourth), 500, 'E9999');
    authCheck(authRequest('POST', '/api/auth/logout', '', $fourth), 500, 'E9999');
} finally {
    authCompose(['up', '-d', '--wait', 'db']);
}
authCheck(authRequest('GET', '/api/me', '', $fourth), 200);
$keyPath = getenv('AUTH_TEST_KEY');
assert(str_contains($keyPath, '.local/ticket02-backend/'));
$key = file_get_contents($keyPath);
try {
    authSetTestKey($keyPath, "broken\n");
    authCheck(authRequest('GET', '/api/me', '', $fourth), 500, 'E0104');
    authCheck(authLogin($email), 500, 'E0104');
} finally {
    authSetTestKey($keyPath, $key);
}
authCheck(authRequest('GET', '/api/me', '', $fourth), 200);
echo "PASS database outage, failed logout, invalid key and recovery\n";
echo 'HTTP authentication checks passed in '.round(microtime(true) - $started, 2)."s\n";
