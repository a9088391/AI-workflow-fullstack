<?php

declare(strict_types=1);

// 共用 Ticket 10 的管理與交易案例；主腳本先驗隔離 Compose，完成後再補 Ticket 15 邊界。
if (! defined('T10_TARGET') && ($GLOBALS['t10Target'] ?? null) !== 'ticket18') {
    define('T10_TARGET', 'ticket15');
}
require __DIR__ . '/ticket10_http.php';

// JSON 整數須先分型態再判範圍；兩端錯誤全文一致且失敗不能改共用設定。
$settingsPath = '/api/settings/login-lockout';
$before = t10Request($subject, 'GET', $settingsPath, null, $admin);
t10Check($before, 200);
foreach (
    [
    ['2147483648', '欄位格式或範圍不正確'],
    ['9223372036854775808', '欄位型態不正確'],
    ['1.0', '欄位型態不正確'],
    ] as [$number, $message]
) {
    $body = '{"enabled":true,"window_minutes":' . $number . ',"failure_threshold":5,"lock_seconds":300}';
    $fromSubject = t10Request($subject, 'PUT', $settingsPath, $body, $admin);
    $fromLaravel = t10Request($laravel, 'PUT', $settingsPath, $body, $laravelToken);
    t10Check($fromSubject, 422, 'E0001', ['window_minutes' => [$message]]);
    t10Check($fromLaravel, 422, 'E0001', ['window_minutes' => [$message]]);
    t10SameError($fromSubject, $fromLaravel);
    assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $before[1]['data']);
}

// 同名 JSON 欄位以最後一筆為準；先以錯值覆蓋前值，確認兩端都拒絕且未寫入。
$current = $before[1]['data'];
$settingsBody = '{"enabled":true,"window_minutes":' . $current['window_minutes']
    . ',"window_minutes":2147483648,"failure_threshold":' . $current['failure_threshold']
    . ',"lock_seconds":' . $current['lock_seconds'] . '}';
$fromSubject = t10Request($subject, 'PUT', $settingsPath, $settingsBody, $admin);
$fromLaravel = t10Request($laravel, 'PUT', $settingsPath, $settingsBody, $laravelToken);
t10Check($fromSubject, 422, 'E0001', ['window_minutes' => ['欄位格式或範圍不正確']]);
t10Check($fromLaravel, 422, 'E0001', ['window_minutes' => ['欄位格式或範圍不正確']]);
t10SameError($fromSubject, $fromLaravel);
assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $current);

// 先錯後對也須採最後一筆；同值更新不改目前設定或觸發關閉副作用。
$settingsBody = '{"enabled":true,"window_minutes":2147483648,"enabled":'
    . ($current['enabled'] ? 'true' : 'false') . ',"window_minutes":' . $current['window_minutes']
    . ',"failure_threshold":' . $current['failure_threshold']
    . ',"lock_seconds":' . $current['lock_seconds'] . '}';
t10Check(t10Request($subject, 'PUT', $settingsPath, $settingsBody, $admin), 200);
t10Check(t10Request($laravel, 'PUT', $settingsPath, $settingsBody, $laravelToken), 200);
assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $current);
echo "PASS integer validation and duplicate settings keys\n";

// 無效 UTF-8 不可被替換；正確編碼的 %FF 則是字面搜尋，兩端回相同唯一會員。
$keyword = 'T15' . bin2hex(random_bytes(6)) . '%FF';
[$keywordId] = t10Identity($subject, $keyword);
$searchBase = '/api/members?page=1&per_page=100&filters%5Bkeyword%5D=';
$invalidPath = $searchBase . '%FF';
$fromSubject = t10Request($subject, 'GET', $invalidPath, null, $admin);
$fromLaravel = t10Request($laravel, 'GET', $invalidPath, null, $laravelToken);
t10Check($fromSubject, 422, 'E0001', ['filters.keyword' => ['欄位格式或範圍不正確']]);
t10Check($fromLaravel, 422, 'E0001', ['filters.keyword' => ['欄位格式或範圍不正確']]);
t10SameError($fromSubject, $fromLaravel);
// 單獨的 %25FF 解碼為字面 %FF；再用唯一前綴斷言精確集合與總數。
$literalPath = $searchBase . '%25FF';
$literalSubject = t10Request($subject, 'GET', $literalPath, null, $admin);
$literalLaravel = t10Request($laravel, 'GET', $literalPath, null, $laravelToken);
t10Check($literalSubject, 200);
t10Check($literalLaravel, 200);
assert(in_array($keywordId, array_column($literalSubject[1]['data']['list'], 'id'), true));
assert(t10CanonicalJson($literalSubject[1]['data']) === t10CanonicalJson($literalLaravel[1]['data']));
$encodedPath = $searchBase . rawurlencode($keyword);
$fromSubject = t10Request($subject, 'GET', $encodedPath, null, $admin);
$fromLaravel = t10Request($laravel, 'GET', $encodedPath, null, $laravelToken);
t10Check($fromSubject, 200);
t10Check($fromLaravel, 200);
assert(array_column($fromSubject[1]['data']['list'], 'id') === [$keywordId]);
assert($fromSubject[1]['data']['pagination']['total'] === 1);
assert(t10CanonicalJson($fromSubject[1]['data']) === t10CanonicalJson($fromLaravel[1]['data']));
echo "PASS UTF-8 keyword and literal percent search\n";

// 兩端以各自管理員及新會員重播相同重複鍵案例，並從共用資料確認最後值與撤銷。
$statusErrors = [];
foreach ([[$subject, $admin, $laravel], [$laravel, $laravelToken, $subject]] as [$base, $token, $otherBase]) {
    [$duplicateId, $duplicateToken, $duplicateEmail] = t10Identity($base, '重複鍵會員');
    $otherLogin = t10Request($otherBase, 'POST', '/api/auth/login', ['email' => $duplicateEmail, 'password' => 'Abc1234']);
    t10Check($otherLogin, 200);
    $statusPath = '/api/members/' . $duplicateId . '/status';
    $statusError = t10Request($base, 'PUT', $statusPath, '{"status":"active","status":"retired"}', $token);
    t10Check($statusError, 422, 'E0001', ['status' => ['欄位格式或範圍不正確']]);
    $statusErrors[] = $statusError;
    assert(t10Sql("SELECT status FROM accounts WHERE id=$duplicateId") === 'active');
    t10Check(t10Request($base, 'PUT', $statusPath, '{"status":"retired","status":"active"}', $token), 200);
    assert(t10Sql("SELECT status FROM accounts WHERE id=$duplicateId") === 'active');
    t10Check(t10Request($base, 'PUT', $statusPath, '{"status":"active","status":"disabled"}', $token), 200);
    assert(t10Sql("SELECT status FROM accounts WHERE id=$duplicateId") === 'disabled');
    t10Check(t10Request($base, 'GET', '/api/me', null, $duplicateToken), 403, 'E0102');
    t10Check(t10Request($otherBase, 'GET', '/api/me', null, $otherLogin[1]['data']['access_token']), 403, 'E0102');
}
t10SameError($statusErrors[0], $statusErrors[1]);
echo "PASS duplicate status keys and shared revocation\n";

// 兩端同時更新四欄；無論提交順序，最後資料只能是其中一份完整值，實際關閉須清鎖與遞增世代。
$settingsA = ['enabled' => false, 'window_minutes' => 6, 'failure_threshold' => 4, 'lock_seconds' => 121];
$settingsB = ['enabled' => true, 'window_minutes' => 8, 'failure_threshold' => 7, 'lock_seconds' => 222];
t10Check(t10Request($subject, 'PUT', $settingsPath, $defaults, $admin), 200);
[$settingsRaceId] = t10Identity($subject, '跨後端設定競爭');
t10Sql("UPDATE accounts SET locked_until=clock_timestamp()+interval '1 hour' WHERE id=$settingsRaceId");
$settingsEpoch = (int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$settingsRaceId");
$results = t10ParallelRequests([
    [$subject, 'PUT', $settingsPath, $settingsA, $admin],
    [$laravel, 'PUT', $settingsPath, $settingsB, $laravelToken],
]);
foreach ($results as $result) {
    t10Check($result, 200);
}
$settingsResult = t10Request($subject, 'GET', $settingsPath, null, $admin);
$laravelSettingsResult = t10Request($laravel, 'GET', $settingsPath, null, $laravelToken);
t10Check($settingsResult, 200);
t10Check($laravelSettingsResult, 200);
assert($settingsResult[1]['data'] === $settingsA || $settingsResult[1]['data'] === $settingsB);
assert($settingsResult[1]['data'] === $laravelSettingsResult[1]['data']);
$storedSettings = t10Sql("SELECT enabled::text || '|' || window_minutes || '|' || failure_threshold || '|' || lock_seconds FROM login_lockout_settings WHERE id=1");
$chosen = $settingsResult[1]['data'];
assert($storedSettings === ($chosen['enabled'] ? 'true' : 'false') . '|' . $chosen['window_minutes'] . '|' . $chosen['failure_threshold'] . '|' . $chosen['lock_seconds']);
assert(t10Sql("SELECT locked_until IS NULL FROM accounts WHERE id=$settingsRaceId") === 't');
assert((int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$settingsRaceId") === $settingsEpoch + 1);
echo "PASS cross-backend complete settings and disable side effect\n";

// 同一會員的 active／disabled 由不同後端競爭；任何完成的停用都撤銷兩種既有 JWT。
[$statusRaceId, $statusRaceToken, $statusRaceEmail] = t10Identity($subject, '跨後端狀態競爭');
$statusRaceLogin = t10Request($laravel, 'POST', '/api/auth/login', ['email' => $statusRaceEmail, 'password' => 'Abc1234']);
t10Check($statusRaceLogin, 200);
$statusRacePath = '/api/members/' . $statusRaceId . '/status';
$results = t10ParallelRequests([
    [$subject, 'PUT', $statusRacePath, ['status' => 'active'], $admin],
    [$laravel, 'PUT', $statusRacePath, ['status' => 'disabled'], $laravelToken],
]);
foreach ($results as $result) {
    t10Check($result, 200);
}
$finalStatus = t10Sql("SELECT status FROM accounts WHERE id=$statusRaceId");
assert(in_array($finalStatus, ['active', 'disabled'], true));
assert(t10Sql("SELECT count(*) FROM login_sessions WHERE account_id=$statusRaceId AND revoked_at IS NULL") === '0');
$revokedCode = $finalStatus === 'active' ? 'E0002' : 'E0102';
$revokedStatus = $finalStatus === 'active' ? 401 : 403;
t10Check(t10Request($subject, 'GET', '/api/me', null, $statusRaceToken), $revokedStatus, $revokedCode);
t10Check(t10Request($laravel, 'GET', '/api/me', null, $statusRaceLogin[1]['data']['access_token']), $revokedStatus, $revokedCode);

// 停用與另一後端新登入相撞時，登入可先成功或看到停用；提交後不得留下可用 session。
foreach ([[$subject, $admin, $laravel], [$laravel, $laravelToken, $subject]] as [$disableBase, $disableToken, $loginBase]) {
    [$disableId, $oldToken, $disableEmail] = t10Identity($loginBase, '停用登入競爭');
    $disablePath = '/api/members/' . $disableId . '/status';
    [$disableResult, $loginResult] = t10ParallelRequests([
        [$disableBase, 'PUT', $disablePath, ['status' => 'disabled'], $disableToken],
        [$loginBase, 'POST', '/api/auth/login', ['email' => $disableEmail, 'password' => 'Abc1234'], null],
    ]);
    t10Check($disableResult, 200);
    if ($loginResult[0] === 200) {
        t10Check($loginResult, 200);
        t10Check(t10Request($loginBase, 'GET', '/api/me', null, $loginResult[1]['data']['access_token']), 403, 'E0102');
    } else {
        t10Check($loginResult, 403, 'E0102');
    }
    assert(t10Sql("SELECT status FROM accounts WHERE id=$disableId") === 'disabled');
    assert(t10Sql("SELECT count(*) FROM login_sessions WHERE account_id=$disableId AND revoked_at IS NULL") === '0');
    t10Check(t10Request($loginBase, 'GET', '/api/me', null, $oldToken), 403, 'E0102');
}
echo "PASS cross-backend status and login revocation races\n";

// 左界事件與兩端同時輸入錯密碼：恰在左界只計兩筆新事件，+1μs 則第三筆當次鎖定。
$boundarySettings = ['enabled' => true, 'window_minutes' => 5, 'failure_threshold' => 3, 'lock_seconds' => 300];
t10Check(t10Request($subject, 'PUT', $settingsPath, $boundarySettings, $admin), 200);
assert(t10Request($laravel, 'GET', $settingsPath, null, $laravelToken)[1]['data'] === $boundarySettings);
foreach ([0 => [401, 401], 1 => [401, 429]] as $microseconds => $expectedStatuses) {
    [$boundaryRaceId, , $boundaryRaceEmail] = t10Identity($subject, '跨後端視窗邊界');
    $boundaryEpoch = (int) t10Sql("SELECT failure_epoch FROM accounts WHERE id=$boundaryRaceId");
    $seedId = (int) t10Sql("WITH seed AS (INSERT INTO login_failures(account_id,failure_epoch,occurred_at) VALUES ($boundaryRaceId,$boundaryEpoch,clock_timestamp()-interval '10 minutes') RETURNING id) SELECT id FROM seed");
    // 只移動本案例的種子事件，對齊每次登入自己的 DB 時間；兩筆新失敗仍保持原時間。
    t10Sql("CREATE FUNCTION t15_boundary() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS \$\$
        BEGIN
            IF NEW.account_id=$boundaryRaceId THEN
                UPDATE public.login_failures SET occurred_at=NEW.occurred_at-interval '5 minutes'+interval '$microseconds microseconds' WHERE id=$seedId;
            END IF;
            RETURN NEW;
        END; \$\$;
        CREATE TRIGGER t15_boundary BEFORE INSERT ON login_failures FOR EACH ROW EXECUTE FUNCTION t15_boundary();");
    try {
        $results = t10ParallelRequests([
            [$subject, 'POST', '/api/auth/login', ['email' => $boundaryRaceEmail, 'password' => 'Wrong123'], null],
            [$laravel, 'POST', '/api/auth/login', ['email' => $boundaryRaceEmail, 'password' => 'Wrong123'], null],
        ]);
    } finally {
        t10Sql('DROP TRIGGER t15_boundary ON login_failures; DROP FUNCTION t15_boundary();');
    }
    $statuses = array_column($results, 0);
    sort($statuses);
    assert($statuses === $expectedStatuses);
    foreach ($results as $result) {
        t10Check($result, $result[0], $result[0] === 401 ? 'E0101' : 'E0105');
        if ($result[0] === 429) {
            assert(preg_match('/Retry-After: [1-9][0-9]*/i', $result[2]) === 1);
        }
    }
    if ($microseconds === 0) {
        t10SameError($results[0], $results[1]);
    }
    assert(t10Sql("SELECT count(*) FROM login_failures WHERE account_id=$boundaryRaceId AND failure_epoch=$boundaryEpoch") === '3');
    $inWindow = t10Sql("SELECT count(*) FROM login_failures WHERE account_id=$boundaryRaceId AND failure_epoch=$boundaryEpoch AND occurred_at > (SELECT max(occurred_at) FROM login_failures WHERE account_id=$boundaryRaceId AND failure_epoch=$boundaryEpoch)-interval '5 minutes'");
    assert($inWindow === ($microseconds === 0 ? '2' : '3'));
    assert(t10Sql("SELECT locked_until IS NOT NULL FROM accounts WHERE id=$boundaryRaceId") === ($microseconds === 0 ? 'f' : 't'));
}
t10Check(t10Request($subject, 'PUT', $settingsPath, $defaults, $admin), 200);
echo "PASS cross-backend concurrent lockout and microsecond boundary\n";
echo $ticket18 ? "Ticket 18 Django management HTTP: PASS\n" : "Ticket 15 ASP.NET Core management HTTP: PASS\n";
