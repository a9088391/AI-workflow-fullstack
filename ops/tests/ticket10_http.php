<?php

declare(strict_types=1);

// 此測試會建立會員、內容及修改鎖定設定，只允許 Ticket 10 的隔離資料庫。
if ((getenv('COMPOSE_PROJECT_NAME') ?: '') !== 'ticket10_test') {
    throw new RuntimeException('請使用 ticket10_test 隔離 Compose project');
}
$symfony = 'http://localhost:19182';
$laravel = 'http://localhost:19181';

/** 由兩個後端的公開 HTTP 介面送出相同案例，保留狀態、資料與標頭供比對。 */
function t10Request(string $base, string $method, string $path, array|string|null $body = null, ?string $token = null): array
{
    $headers = ['Accept: application/json', 'Origin: http://localhost:8080'];
    if ($body !== null) {
        $headers[] = 'Content-Type: application/json';
    }
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer '.$token;
    }
    $curl = curl_init($base.$path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $headers,
        CURLOPT_POSTFIELDS => is_array($body) ? json_encode($body, JSON_THROW_ON_ERROR) : ($body ?? ''),
        CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 20]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException(curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
    $result = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, 0, $length)];
    curl_close($curl);

    return $result;
}

/** 共同錯誤需逐字一致，且每次錯誤須有可追查的 log_id。 */
function t10Check(array $result, int $status, ?string $code = null, ?array $errors = null): void
{
    assert($result[0] === $status, 'status '.$status.' got '.$result[0].' '.json_encode($result[1], JSON_UNESCAPED_UNICODE));
    assert($result[1]['success'] === ($code === null));
    if ($code === null) {
        assert($result[1]['message'] === 'OK');

        return;
    }
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API', 'E0101' => '帳號或密碼錯誤',
        'E0102' => '此帳號已停用，請聯絡系統管理員', 'E0105' => '登入失敗次數過多，請稍後再試',
        'E9999' => '系統暫時無法處理，請稍後再試'];
    assert($result[1]['error_code'] === $code && $result[1]['message'] === $messages[$code]);
    assert(array_key_exists('errors', $result[1]) === ($code === 'E0001'));
    assert(preg_match('/\A[0-9a-f-]{36}\z/', $result[1]['log_id']) === 1);
    if ($errors !== null) {
        assert($result[1]['errors'] === $errors, json_encode($result[1]['errors'], JSON_UNESCAPED_UNICODE));
    }
}

/** 比對兩後端完整錯誤契約；每次請求獨立產生的 log_id 只驗格式，不比值。 */
function t10SameError(array $symfonyResult, array $laravelResult): void
{
    assert($symfonyResult[0] === $laravelResult[0]);
    assert(array_diff_key($symfonyResult[1], ['log_id' => true]) === array_diff_key($laravelResult[1], ['log_id' => true]));
}

/** fixture 僅寫入隔離 Compose 資料庫；公開結果仍由 HTTP 案例驗證。 */
function t10Sql(string $sql): string
{
    $pipes = [];
    $process = proc_open(['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v', 'ON_ERROR_STOP=1', '-At', '-c', $sql],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    assert(is_resource($process));
    fclose($pipes[0]);
    $out = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('隔離 SQL 失敗：'.$error);
    }

    return trim($out);
}

/** 各後端各自簽發 JWT；帳號資料共用，但 token 仍不可跨後端使用。 */
function t10Identity(string $base, string $nickname, bool $admin = false): array
{
    $email = 't10-'.bin2hex(random_bytes(6)).'@example.test';
    t10Check(t10Request($base, 'POST', '/api/auth/register', ['email' => $email, 'password' => 'Abc1234', 'nickname' => $nickname]), 201);
    if ($admin) {
        t10Sql("UPDATE accounts SET role='admin' WHERE email='$email'");
    }
    $login = t10Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']);
    t10Check($login, 200);

    return [$login[1]['data']['user']['id'], $login[1]['data']['access_token'], $email];
}

/** 同時送兩筆管理寫入，驗證設定或會員列鎖的並行結果。 */
function t10ParallelPut(string $path, array $a, array $b, string $token): void
{
    $multi = curl_multi_init();
    $handles = [];
    foreach ([$a, $b] as $values) {
        $curl = curl_init('http://localhost:19182'.$path);
        curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => 'PUT', CURLOPT_POSTFIELDS => json_encode($values, JSON_THROW_ON_ERROR),
            CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json', 'Authorization: Bearer '.$token],
            CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20]);
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
        assert(curl_errno($curl) === 0, curl_error($curl));
        assert(curl_getinfo($curl, CURLINFO_RESPONSE_CODE) === 200);
        curl_multi_remove_handle($multi, $curl);
        curl_close($curl);
    }
    curl_multi_close($multi);
}

$defaults = ['enabled' => true, 'window_minutes' => 5, 'failure_threshold' => 5, 'lock_seconds' => 300];
// 可重跑時先將隔離資料庫設定還原；先前測試帳號保留作稽核，不清空資料。
t10Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE id=1');
[$adminId, $admin] = t10Identity($symfony, '管理員', true);
$laravelAdmin = t10Request($laravel, 'POST', '/api/auth/login', ['email' => t10Sql("SELECT email FROM accounts WHERE id=$adminId"), 'password' => 'Abc1234']);
t10Check($laravelAdmin, 200);
$laravelToken = $laravelAdmin[1]['data']['access_token'];
[$memberId, $member, $memberEmail] = t10Identity($symfony, 'A_B%\\É中'.bin2hex(random_bytes(4)));
[$otherId, $other] = t10Identity($laravel, '普通會員é');
$otherSymfony = t10Request($symfony, 'POST', '/api/auth/login', ['email' => t10Sql("SELECT email FROM accounts WHERE id=$otherId"), 'password' => 'Abc1234']);
t10Check($otherSymfony, 200);
$path = '/api/members?page=1&per_page=100';

// 先驗角色再驗 Query；會員搜尋只折疊 ASCII，所有萬用符號均為字面內容。
t10Check(t10Request($symfony, 'GET', $path), 401, 'E0002');
t10Check(t10Request($symfony, 'GET', '/api/members?page=bad&per_page=20', null, $member), 403, 'E0003');
foreach (['a_b', '%', '\\', 'É', '中'] as $keyword) {
    $result = t10Request($symfony, 'GET', $path.'&filters%5Bkeyword%5D='.rawurlencode($keyword), null, $admin);
    t10Check($result, 200);
    assert(in_array($memberId, array_column($result[1]['data']['list'], 'id'), true));
}
$different = t10Request($symfony, 'GET', $path.'&filters%5Bkeyword%5D='.rawurlencode('é'), null, $admin);
t10Check($different, 200);
assert(in_array($otherId, array_column($different[1]['data']['list'], 'id'), true));
$needle = 'T10'.bin2hex(random_bytes(6));
[$literalId] = t10Identity($symfony, $needle.'_X%\\É');
[$decoyId] = t10Identity($symfony, $needle.'AXYé');
[$upperId] = t10Identity($symfony, $needle.'É');
[$lowerId] = t10Identity($symfony, $needle.'é');
// 唯一前綴讓萬用符號與 Unicode 反例可斷言完整集合和精確總數，並對照 Laravel。
foreach ([[$needle.'_', $literalId], [$needle.'_X%', $literalId], [$needle.'_X%\\', $literalId],
    [strtolower($needle).'_x', $literalId], [$needle.'É', $upperId], [$needle.'é', $lowerId],
    [$needle.'AXY', $decoyId]] as [$keyword, $expectedId]) {
    $searchPath = $path.'&filters%5Bkeyword%5D='.rawurlencode($keyword);
    $fromSymfony = t10Request($symfony, 'GET', $searchPath, null, $admin);
    $fromLaravel = t10Request($laravel, 'GET', $searchPath, null, $laravelToken);
    t10Check($fromSymfony, 200);
    t10Check($fromLaravel, 200);
    assert(array_column($fromSymfony[1]['data']['list'], 'id') === [$expectedId], $keyword);
    assert($fromSymfony[1]['data']['pagination']['total'] === 1, $keyword);
    assert($fromSymfony[1]['data'] === $fromLaravel[1]['data'], $keyword);
}
$repeatedSymfony = t10Request($symfony, 'GET', $path.'&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y', null, $admin);
$repeatedLaravel = t10Request($laravel, 'GET', $path.'&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y', null, $laravelToken);
t10Check($repeatedSymfony, 422, 'E0001', ['filters.keyword' => ['此參數不可重複']]);
t10Check($repeatedLaravel, 422, 'E0001', ['filters.keyword' => ['此參數不可重複']]);
t10SameError($repeatedSymfony, $repeatedLaravel);
t10Check(t10Request($symfony, 'GET', $path, '{}', $admin), 422, 'E0001', ['_body' => ['此 API 不接受 Request Body']]);
t10Check(t10Request($symfony, 'GET', '/api/members/'.$adminId, null, $admin), 404, 'E0004');
$detail = t10Request($symfony, 'GET', '/api/members/'.$memberId, null, $admin);
t10Check($detail, 200);
assert(array_keys($detail[1]['data']) === ['id', 'email', 'nickname', 'role', 'status', 'created_at']);
assert($detail[1]['data']['email'] === $memberEmail);
$deepPath = '/api/members?page=9007199254740991&per_page=100&filters%5Bkeyword%5D='.$needle;
$deep = t10Request($symfony, 'GET', $deepPath, null, $admin);
$deepLaravel = t10Request($laravel, 'GET', $deepPath, null, $laravelToken);
t10Check($deep, 200);
t10Check($deepLaravel, 200);
assert($deep[1]['data']['list'] === [] && $deep[1]['data']['pagination']['total'] === 4);
assert($deep[1]['data'] === $deepLaravel[1]['data']);
echo "PASS member authorization, literal search and pagination\n";

// 既有作者操作建立內容，管理列表需含隱藏內容且只回核准欄位。
t10Check(t10Request($symfony, 'POST', '/api/articles', ['title' => '管理文章', 'body' => '內容'], $member), 201);
$articleId = (int) t10Sql("SELECT max(id) FROM articles WHERE author_id=$memberId");
t10Check(t10Request($symfony, 'POST', '/api/articles/'.$articleId.'/comments', ['body' => '留言'], $member), 201);
$commentId = (int) t10Sql("SELECT max(id) FROM comments WHERE article_id=$articleId");
t10Check(t10Request($symfony, 'PUT', '/api/articles/'.$articleId.'/visibility', ['visibility' => 'hidden', 'version' => 1], $admin), 200);
$articles = t10Request($symfony, 'GET', '/api/articles?page=1&per_page=100&filters%5Bscope%5D=management', null, $admin);
t10Check($articles, 200);
assert(in_array($articleId, array_column($articles[1]['data']['list'], 'id'), true));
$comments = t10Request($symfony, 'GET', '/api/comments?page=1&per_page=100', null, $admin);
t10Check($comments, 200);
assert(in_array($commentId, array_column($comments[1]['data']['list'], 'id'), true));
t10Check(t10Request($symfony, 'GET', '/api/comments?page=1&per_page=20', null, $member), 403, 'E0003');
echo "PASS management articles and comments\n";

// 設定完整覆寫、關閉清鎖及同值無副作用，並由 Laravel 讀同一份設定。
$settingsPath = '/api/settings/login-lockout';
t10Check(t10Request($symfony, 'GET', $settingsPath, null, $member), 403, 'E0003');
$initial = t10Request($symfony, 'GET', $settingsPath, null, $admin);
t10Check($initial, 200);
assert($initial[1]['data'] === $defaults);
$sameVersion = t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1');
t10Check(t10Request($symfony, 'PUT', $settingsPath, array_reverse($defaults, true), $admin), 200);
assert(t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1') === $sameVersion);
$missingSymfony = t10Request($symfony, 'PUT', $settingsPath, ['enabled' => true], $admin);
$missingLaravel = t10Request($laravel, 'PUT', $settingsPath, ['enabled' => true], $laravelToken);
t10Check($missingSymfony, 422, 'E0001');
t10Check($missingLaravel, 422, 'E0001');
t10SameError($missingSymfony, $missingLaravel);
$next = ['enabled' => true, 'window_minutes' => 7, 'failure_threshold' => 2, 'lock_seconds' => 120];
t10Check(t10Request($symfony, 'PUT', $settingsPath, $next, $admin), 200);
assert(t10Request($symfony, 'GET', $settingsPath, null, $admin)[1]['data'] === $next);
assert(t10Request($laravel, 'GET', $settingsPath, null, $laravelToken)[1]['data'] === $next);
// 同一份既有資料以兩種 JWT 各自讀取，逐欄比較管理列表及詳情。
foreach ([$path, '/api/members/'.$memberId, '/api/comments?page=1&per_page=100', '/api/articles?page=1&per_page=100&filters%5Bscope%5D=management'] as $readPath) {
    $fromSymfony = t10Request($symfony, 'GET', $readPath, null, $admin);
    $fromLaravel = t10Request($laravel, 'GET', $readPath, null, $laravelToken);
    t10Check($fromSymfony, 200);
    t10Check($fromLaravel, 200);
    assert($fromSymfony[1]['data'] === $fromLaravel[1]['data'], '管理資料跨後端不一致：'.$readPath);
}
$wrong = ['email' => $memberEmail, 'password' => 'Wrong123'];
t10Check(t10Request($laravel, 'POST', '/api/auth/login', $wrong), 401, 'E0101');
$locked = t10Request($symfony, 'POST', '/api/auth/login', $wrong);
t10Check($locked, 429, 'E0105');
assert(preg_match('/Retry-After: [1-9][0-9]*/i', $locked[2]) === 1);
$until = t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId");
t10Check(t10Request($symfony, 'PUT', $settingsPath, array_replace($next, ['lock_seconds' => 300]), $admin), 200);
assert(t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId") === $until);
$epoch = (int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$memberId");
$off = array_replace($next, ['enabled' => false]);
t10Check(t10Request($symfony, 'PUT', $settingsPath, $off, $admin), 200);
assert(t10Sql("SELECT locked_until IS NULL FROM accounts WHERE id=$memberId") === 't');
assert((int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === $epoch + 1);
assert(t10Request($symfony, 'GET', $settingsPath, null, $admin)[1]['data'] === $off);
t10Check(t10Request($symfony, 'PUT', $settingsPath, $off, $admin), 200);
assert((int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$memberId") === $epoch + 1);
// 清帳號失敗時，四欄設定與鎖定資料都要回滾，不得留下半套更新。
t10Check(t10Request($symfony, 'PUT', $settingsPath, $next, $admin), 200);
t10Sql("UPDATE accounts SET locked_until=clock_timestamp()+interval '1 hour' WHERE id=$memberId");
$beforeRollback = t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId");
t10Sql("CREATE FUNCTION t10_fail_clear() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated clear failure'; END; $$; CREATE TRIGGER t10_fail_clear BEFORE UPDATE ON accounts FOR EACH ROW WHEN (NEW.role = 'member') EXECUTE FUNCTION t10_fail_clear();");
try {
    t10Check(t10Request($symfony, 'PUT', $settingsPath, $off, $admin), 500, 'E9999');
    assert(t10Request($symfony, 'GET', $settingsPath, null, $admin)[1]['data'] === $next);
    assert(t10Sql("SELECT locked_until FROM accounts WHERE id=$memberId") === $beforeRollback);
} finally {
    t10Sql('DROP TRIGGER t10_fail_clear ON accounts; DROP FUNCTION t10_fail_clear();');
}
t10Check(t10Request($symfony, 'PUT', $settingsPath, $off, $admin), 200);
$a = ['enabled' => false, 'window_minutes' => 7, 'failure_threshold' => 11, 'lock_seconds' => 333];
$b = ['enabled' => true, 'window_minutes' => 9, 'failure_threshold' => 13, 'lock_seconds' => 555];
t10ParallelPut($settingsPath, $a, $b, $admin);
$finalSettings = t10Request($symfony, 'GET', $settingsPath, null, $admin);
t10Check($finalSettings, 200);
assert($finalSettings[1]['data'] === $a || $finalSettings[1]['data'] === $b);
echo "PASS shared settings, cross-backend lockout and disable transition\n";

// 停用應撤銷 Symfony 與 Laravel 的所有舊登入；恢復不可讓舊 JWT 復活。
$memberLaravel = t10Request($laravel, 'POST', '/api/auth/login', ['email' => $memberEmail, 'password' => 'Abc1234']);
t10Check($memberLaravel, 200);
$statusPath = '/api/members/'.$memberId.'/status';
t10Check(t10Request($symfony, 'PUT', $statusPath, ['status' => 'disabled'], $member), 403, 'E0003');
$invalidStatusSymfony = t10Request($symfony, 'PUT', $statusPath, ['status' => 'retired'], $admin);
$invalidStatusLaravel = t10Request($laravel, 'PUT', $statusPath, ['status' => 'retired'], $laravelToken);
t10Check($invalidStatusSymfony, 422, 'E0001', ['status' => ['欄位格式或範圍不正確']]);
t10Check($invalidStatusLaravel, 422, 'E0001', ['status' => ['欄位格式或範圍不正確']]);
t10SameError($invalidStatusSymfony, $invalidStatusLaravel);
$adminTargetSymfony = t10Request($symfony, 'PUT', '/api/members/'.$adminId.'/status', ['status' => 'disabled'], $admin);
$adminTargetLaravel = t10Request($laravel, 'PUT', '/api/members/'.$adminId.'/status', ['status' => 'disabled'], $laravelToken);
t10Check($adminTargetSymfony, 404, 'E0004');
t10Check($adminTargetLaravel, 404, 'E0004');
t10SameError($adminTargetSymfony, $adminTargetLaravel);
t10Check(t10Request($symfony, 'PUT', $statusPath, ['status' => 'disabled'], $admin), 200);
t10Check(t10Request($symfony, 'GET', '/api/me', null, $member), 403, 'E0102');
t10Check(t10Request($laravel, 'GET', '/api/me', null, $memberLaravel[1]['data']['access_token']), 403, 'E0102');
t10Check(t10Request($symfony, 'PUT', $statusPath, ['status' => 'active'], $admin), 200);
t10Check(t10Request($symfony, 'GET', '/api/me', null, $member), 401, 'E0002');
t10Check(t10Request($laravel, 'GET', '/api/me', null, $memberLaravel[1]['data']['access_token']), 401, 'E0002');
assert(t10Sql("SELECT visibility FROM articles WHERE id=$articleId") === 'hidden');
// 兩筆狀態寫入可依提交順序決定最終值，但任何實際停用都必須撤銷舊登入。
[$raceId, $raceToken] = t10Identity($symfony, '並行會員');
t10ParallelPut('/api/members/'.$raceId.'/status', ['status' => 'active'], ['status' => 'disabled'], $admin);
assert(in_array(t10Sql("SELECT status FROM accounts WHERE id=$raceId"), ['active', 'disabled'], true));
assert(t10Sql("SELECT count(*) FROM login_sessions WHERE account_id=$raceId AND revoked_at IS NULL") === '0');
if (t10Sql("SELECT status FROM accounts WHERE id=$raceId") === 'active') {
    t10Check(t10Request($symfony, 'GET', '/api/me', null, $raceToken), 401, 'E0002');
} else {
    t10Check(t10Request($symfony, 'GET', '/api/me', null, $raceToken), 403, 'E0102');
}
echo "PASS status, cross-backend revocation and content preservation\n";

// 同一會員逐次重置世代，將歷史事件對齊到新事件的視窗左界與 +1μs，重播兩後端。
t10Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=2, lock_seconds=300 WHERE id=1');
[$boundaryId, , $boundaryEmail] = t10Identity($symfony, '邊界會員');
foreach (['0 microseconds' => 401, '1 microsecond' => 429] as $offset => $status) {
    $responses = [];
    $lockStates = [];
    foreach ([$symfony, $laravel] as $base) {
        t10Sql("UPDATE accounts SET locked_until=NULL, failure_epoch=failure_epoch+1 WHERE id=$boundaryId;
            INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '10 minutes' FROM accounts WHERE id=$boundaryId;
            CREATE FUNCTION t10_boundary() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS \$\$
            BEGIN
                IF NEW.account_id=$boundaryId THEN
                    UPDATE public.login_failures SET occurred_at=NEW.occurred_at-interval '5 minutes'+interval '$offset' WHERE account_id=NEW.account_id AND failure_epoch=NEW.failure_epoch;
                END IF;
                RETURN NEW;
            END; \$\$;
            CREATE TRIGGER t10_boundary BEFORE INSERT ON login_failures FOR EACH ROW EXECUTE FUNCTION t10_boundary();");
        try {
            $response = t10Request($base, 'POST', '/api/auth/login', ['email' => $boundaryEmail, 'password' => 'Wrong123']);
            t10Check($response, $status, $status === 401 ? 'E0101' : 'E0105');
            $responses[] = $response;
            $lockStates[] = t10Sql("SELECT locked_until IS NOT NULL FROM accounts WHERE id=$boundaryId");
        } finally {
            t10Sql('DROP TRIGGER t10_boundary ON login_failures; DROP FUNCTION t10_boundary();');
        }
    }
    t10SameError($responses[0], $responses[1]);
    assert($lockStates === ($status === 401 ? ['f', 'f'] : ['t', 't']));
}
t10Sql('UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=5, lock_seconds=300 WHERE id=1');
echo "PASS cross-backend error and microsecond boundary parity\n";
echo "Ticket 10 Symfony management HTTP: PASS\n";
