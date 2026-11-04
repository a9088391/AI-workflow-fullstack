<?php

declare(strict_types=1);

require_once __DIR__ . '/http_fixture.php';
// 所有 fixture 操作之前先確認斷言有效，不能在測試檢查被關閉時仍送出寫入。
assertHttpFixtureAssertions();

// 只讀取 Docker 身分資訊；錯誤輸出可能含環境設定，不寫入測試記錄。
$t18Docker = static function (array $arguments): string {
    $process = proc_open($arguments, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (! is_resource($process)) {
        throw new RuntimeException('無法檢查 Ticket 18 Docker 身分');
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('Ticket 18 Docker 身分檢查失敗');
    }

    return trim($output);
};

// 寫入前核對解析設定和實際容器，確保請求與 SQL 只到專用資料庫。
$t18Guard = static function () use ($t18Docker): void {
    $root = dirname(__DIR__, 2);
    if (
        getcwd() !== $root || getenv('COMPOSE_PROJECT_NAME') !== 'ticket18_test'
        || getenv('COMPOSE_FILE') !== 'compose.yaml:ops/tests/ticket18_compose.yaml'
        || getenv('COMPOSE_PROFILES') !== 'django'
        || (int) ini_get('zend.assertions') !== 1
        || ! filter_var(ini_get('assert.active'), FILTER_VALIDATE_BOOLEAN)
    ) {
        throw new RuntimeException('Ticket 18 須從 repo root 以專用 Compose 設定及啟用 PHP assertions 執行');
    }
    foreach (['laravel.key', 'django.key'] as $file) {
        if (! is_file($root . '/.local/ticket18-backend/' . $file)) {
            throw new RuntimeException('Ticket 18 專用 JWT 金鑰不存在');
        }
    }
    $configuration = json_decode(
        $t18Docker(['docker', 'compose', 'config', '--format', 'json']),
        true,
        512,
        JSON_THROW_ON_ERROR
    );
    if (
        ($configuration['name'] ?? null) !== 'ticket18_test'
        || ($configuration['volumes']['postgres_data']['name'] ?? null) !== 'ticket18_test_postgres_data'
        || ($configuration['services']['db']['environment']['POSTGRES_DB'] ?? null) !== 'articles'
        || ($configuration['services']['db']['volumes'][0]['source'] ?? null) !== 'postgres_data'
        || ($configuration['services']['db']['volumes'][0]['target'] ?? null) !== '/var/lib/postgresql'
    ) {
        throw new RuntimeException('Ticket 18 Compose 資料庫設定不符');
    }
    $ports = ['laravel' => 19381, 'django' => 19384];
    foreach ($ports as $service => $port) {
        $settings = $configuration['services'][$service];
        $binding = $settings['ports'][0] ?? [];
        if (
            count($settings['ports'] ?? []) !== 1
            || ($binding['host_ip'] ?? null) !== '127.0.0.1'
            || (int) ($binding['published'] ?? 0) !== $port
            || (int) ($binding['target'] ?? 0) !== 8000
            || ($settings['environment']['DB_HOST'] ?? null) !== 'db'
            || ($settings['environment']['DB_DATABASE'] ?? null) !== 'articles'
        ) {
            throw new RuntimeException('Ticket 18 Compose 服務目標不符：' . $service);
        }
        $keyMount = $settings['volumes'][$service === 'laravel' ? 1 : 0] ?? [];
        if (
            ($keyMount['source'] ?? null) !== $root . '/.local/ticket18-backend/' . $service . '.key'
            || ($keyMount['target'] ?? null) !== '/run/secrets/' . $service . '-jwt.key'
            || ($keyMount['read_only'] ?? false) !== true
        ) {
            throw new RuntimeException('Ticket 18 專用金鑰掛載不符：' . $service);
        }
    }
    if (
        ($configuration['services']['laravel']['volumes'][0]['source'] ?? null)
            !== $root . '/.local/ticket18-backend/laravel-logs'
    ) {
        throw new RuntimeException('Ticket 18 Laravel 記錄目錄不符');
    }

    foreach (['db', 'laravel', 'django'] as $service) {
        $id = $t18Docker(['docker', 'compose', 'ps', '-q', $service]);
        if ($id === '' || str_contains($id, "\n")) {
            throw new RuntimeException('Ticket 18 服務未單獨啟動：' . $service);
        }
        $inspection = json_decode($t18Docker(['docker', 'inspect', $id]), true, 512, JSON_THROW_ON_ERROR)[0] ?? [];
        if (
            ($inspection['Config']['Labels']['com.docker.compose.project'] ?? null) !== 'ticket18_test'
            || ($inspection['Config']['Labels']['com.docker.compose.service'] ?? null) !== $service
            || ($inspection['Config']['Labels']['com.docker.compose.project.working_dir'] ?? null) !== $root
            || ($inspection['Config']['Labels']['com.docker.compose.project.config_files'] ?? null)
                !== $root . '/compose.yaml,' . $root . '/ops/tests/ticket18_compose.yaml'
            || ($inspection['State']['Running'] ?? false) !== true
            || ($inspection['State']['Health']['Status'] ?? null) !== 'healthy'
            || ! isset($inspection['NetworkSettings']['Networks']['ticket18_test_default'])
        ) {
            throw new RuntimeException('Ticket 18 實際容器身分不符：' . $service);
        }
        if ($service === 'db') {
            $mount = $inspection['Mounts'][0] ?? [];
            if (
                count($inspection['Mounts'] ?? []) !== 1
                || ($mount['Type'] ?? null) !== 'volume'
                || ($mount['Name'] ?? null) !== 'ticket18_test_postgres_data'
                || ($mount['Destination'] ?? null) !== '/var/lib/postgresql'
            ) {
                throw new RuntimeException('Ticket 18 實際資料庫 volume 不符');
            }
            continue;
        }
        $published = $ports[$service];
        $binding = $inspection['NetworkSettings']['Ports']['8000/tcp'][0] ?? [];
        if (($binding['HostIp'] ?? null) !== '127.0.0.1' || (int) ($binding['HostPort'] ?? 0) !== $published) {
            throw new RuntimeException('Ticket 18 實際服務埠位不符：' . $service);
        }
        $keyMount = [];
        foreach ($inspection['Mounts'] ?? [] as $mount) {
            if (($mount['Destination'] ?? null) === '/run/secrets/' . $service . '-jwt.key') {
                $keyMount = $mount;
            }
        }
        if (
            ($keyMount['Source'] ?? null) !== $root . '/.local/ticket18-backend/' . $service . '.key'
            || ($keyMount['Destination'] ?? null) !== '/run/secrets/' . $service . '-jwt.key'
            || ($keyMount['RW'] ?? true) !== false
        ) {
            throw new RuntimeException('Ticket 18 實際金鑰掛載不符：' . $service);
        }
        if ($service === 'laravel') {
            $logMount = [];
            foreach ($inspection['Mounts'] ?? [] as $mount) {
                if (($mount['Destination'] ?? null) === '/srv/laravel/storage/logs') {
                    $logMount = $mount;
                }
            }
            if (($logMount['Source'] ?? null) !== $root . '/.local/ticket18-backend/laravel-logs') {
                throw new RuntimeException('Ticket 18 實際記錄掛載不符');
            }
        }
    }
};

$t18Guard();
$GLOBALS['t18VerifiedTarget'] = true;
require __DIR__ . '/django_http.php';
require __DIR__ . '/ticket17_http.php';
$GLOBALS['t10Target'] = 'ticket18';
require __DIR__ . '/ticket15_http.php';

// 逐頁驗證完整 ID 順序，避免只檢查首頁或單筆包含而漏掉隱藏內容。
$t18AllIds = static function (string $base, string $path, string $token): array {
    $ids = [];
    $page = 1;
    do {
        $join = str_contains($path, '?') ? '&' : '?';
        $result = t10Request($base, 'GET', $path . $join . 'page=' . $page . '&per_page=100', null, $token);
        t10Check($result, 200);
        $pagination = $result[1]['data']['pagination'];
        assert($pagination['page'] === $page && $pagination['per_page'] === 100);
        assert($pagination['total_pages'] === (int) ceil($pagination['total'] / 100));
        array_push($ids, ...array_column($result[1]['data']['list'], 'id'));
        $page++;
    } while ($page <= $pagination['total_pages']);
    assert(count($ids) === $pagination['total']);

    return $ids;
};

// 新會員使用唯一字面前綴；停用後仍須出現在列表與詳情，管理員永不混入結果。
$marker = 'T18' . bin2hex(random_bytes(5));
[$activeId] = t10Identity($subject, $marker . 'A');
[$disabledId] = t10Identity($laravel, $marker . 'B');
t10Check(t10Request($subject, 'PUT', '/api/members/' . $disabledId . '/status', ['status' => 'disabled'], $admin), 200);
$expectedMembers = [$disabledId, $activeId];
$memberPath = '/api/members?page=1&per_page=100&filters%5Bkeyword%5D=' . rawurlencode(strtolower($marker));
$members = t10Request($subject, 'GET', $memberPath, null, $admin);
$laravelMembers = t10Request($laravel, 'GET', $memberPath, null, $laravelToken);
t10Check($members, 200);
t10Check($laravelMembers, 200);
assert(array_column($members[1]['data']['list'], 'id') === $expectedMembers);
assert($members[1]['data']['pagination'] === ['page' => 1, 'per_page' => 100, 'total' => 2, 'total_pages' => 1]);
assert(array_column($members[1]['data']['list'], 'status') === ['disabled', 'active']);
assert(t10CanonicalJson($members[1]['data']) === t10CanonicalJson($laravelMembers[1]['data']));
$detail = t10Request($subject, 'GET', '/api/members/' . $disabledId, null, $admin);
t10Check($detail, 200);
assert(array_keys($detail[1]['data']) === ['id', 'email', 'nickname', 'role', 'status', 'created_at']);
assert($detail[1]['data']['status'] === 'disabled');
// NUL 是合法字面查詢，但 PostgreSQL text 不可存 NUL，因此必須得到精確空集合。
$nulResults = [];
foreach ([$subject => $admin, $laravel => $laravelToken] as $base => $token) {
    $nul = t10Request($base, 'GET', '/api/members?page=1&per_page=100&filters%5Bkeyword%5D=%00', null, $token);
    $nulResults[$base] = $nul;
}
$nulMismatch = null;
foreach ($nulResults as $base => $nul) {
    if ($nul[0] !== 200) {
        $nulMismatch = 'NUL keyword backend responses: ' . json_encode([
            'django' => [$nulResults[$subject][0], $nulResults[$subject][1]],
            'laravel' => [$nulResults[$laravel][0], $nulResults[$laravel][1]],
        ], JSON_UNESCAPED_UNICODE);
        continue;
    }
    t10Check($nul, 200);
    assert($nul[1]['data'] === ['list' => [], 'pagination' => [
        'page' => 1, 'per_page' => 100, 'total' => 0, 'total_pages' => 0,
    ]]);
}
// 短字查詢以資料庫的完整順序和筆數為基準，避免多次測試殘留資料使首頁包含斷言失真。
$shortExpected = t10Sql("SELECT string_agg(id::text, ',' ORDER BY created_at DESC, id DESC) FROM accounts "
    . "WHERE role='member' AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
    . "'abcdefghijklmnopqrstuvwxyz'), 'a')>0 OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
    . "'abcdefghijklmnopqrstuvwxyz'), 'a')>0)");
$shortIds = $shortExpected === '' ? [] : array_map('intval', explode(',', $shortExpected));
$shortPath = '/api/members?filters%5Bkeyword%5D=A';
assert($t18AllIds($subject, $shortPath, $admin) === $shortIds);
assert($t18AllIds($laravel, $shortPath, $laravelToken) === $shortIds);
echo "PASS exact active/disabled members and short keyword; NUL result recorded\n";

// 管理列表跨所有頁面列出下架文章與父文章下架的留言，並逐筆對照實際儲存順序。
$articleIds = t10Sql("SELECT string_agg(id::text, ',' ORDER BY created_at DESC, id DESC) FROM articles");
$commentIds = t10Sql("SELECT string_agg(id::text, ',' ORDER BY created_at DESC, id DESC) FROM comments");
$expectedArticles = $articleIds === '' ? [] : array_map('intval', explode(',', $articleIds));
$expectedComments = $commentIds === '' ? [] : array_map('intval', explode(',', $commentIds));
assert($t18AllIds($subject, '/api/articles?filters%5Bscope%5D=management', $admin) === $expectedArticles);
assert($t18AllIds($subject, '/api/comments', $admin) === $expectedComments);
assert(in_array($articleId, $expectedArticles, true) && in_array($commentId, $expectedComments, true));
assert(t10Sql("SELECT articles.visibility || '|' || comments.visibility FROM comments JOIN articles "
    . "ON articles.id=comments.article_id WHERE comments.id=$commentId") === 'hidden|visible');
echo "PASS complete management article/comment lists including hidden parent\n";

// 每個設定欄位的缺漏、型態或範圍錯誤都不可讓 Django 改動共用四欄。
$settingsBefore = t10Request($subject, 'GET', $settingsPath, null, $admin);
t10Check($settingsBefore, 200);
$invalidSettings = [
    [['enabled' => 1], ['enabled' => ['欄位型態不正確']]],
    [['window_minutes' => null], ['window_minutes' => ['此欄位不可為 null']]],
    [['failure_threshold' => 0], ['failure_threshold' => ['欄位格式或範圍不正確']]],
    [['lock_seconds' => 86401], ['lock_seconds' => ['欄位格式或範圍不正確']]],
];
foreach ($invalidSettings as [$change, $errors]) {
    $body = array_replace($settingsBefore[1]['data'], $change);
    $fromDjango = t10Request($subject, 'PUT', $settingsPath, $body, $admin);
    $fromLaravel = t10Request($laravel, 'PUT', $settingsPath, $body, $laravelToken);
    t10Check($fromDjango, 422, 'E0001', $errors);
    t10Check($fromLaravel, 422, 'E0001', $errors);
    t10SameError($fromDjango, $fromLaravel);
    assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $settingsBefore[1]['data']);
}
echo "PASS four-field settings validation and unchanged persistence\n";

// 原始 JSON 保留超長整數與重複鍵詞法；失敗不改設定，最後有效值同值提交也不產生副作用。
$settingsXmin = t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1');
$settingsPrefix = '{"enabled":' . ($settingsBefore[1]['data']['enabled'] ? 'true' : 'false')
    . ',"window_minutes":';
$settingsSuffix = ',"failure_threshold":' . $settingsBefore[1]['data']['failure_threshold']
    . ',"lock_seconds":' . $settingsBefore[1]['data']['lock_seconds'] . '}';
foreach ([4300, 4301] as $digits) {
    $oversized = str_repeat('9', $digits);
    $invalidBody = $settingsPrefix . $oversized . $settingsSuffix;
    $fromDjango = t10Request($subject, 'PUT', $settingsPath, $invalidBody, $admin);
    $fromLaravel = t10Request($laravel, 'PUT', $settingsPath, $invalidBody, $laravelToken);
    t10Check($fromDjango, 422, 'E0001', ['window_minutes' => ['欄位型態不正確']]);
    t10Check($fromLaravel, 422, 'E0001', ['window_minutes' => ['欄位型態不正確']]);
    t10SameError($fromDjango, $fromLaravel);
    assert(t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1') === $settingsXmin);
    assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $settingsBefore[1]['data']);
    assert(t10Request($laravel, 'GET', $settingsPath, null, $laravelToken)[1]['data'] === $settingsBefore[1]['data']);

    $duplicateBody = $settingsPrefix . $oversized . ',"window_minutes":'
        . $settingsBefore[1]['data']['window_minutes'] . $settingsSuffix;
    t10Check(t10Request($subject, 'PUT', $settingsPath, $duplicateBody, $admin), 200);
    t10Check(t10Request($laravel, 'PUT', $settingsPath, $duplicateBody, $laravelToken), 200);
    assert(t10Sql('SELECT xmin::text FROM login_lockout_settings WHERE id=1') === $settingsXmin);
    assert(t10Request($subject, 'GET', $settingsPath, null, $admin)[1]['data'] === $settingsBefore[1]['data']);
    assert(t10Request($laravel, 'GET', $settingsPath, null, $laravelToken)[1]['data'] === $settingsBefore[1]['data']);
}
echo "PASS 4300/4301-digit settings integers, duplicate last value and persistence\n";

// 使用 Django 會員列表實際 count/SELECT 形狀記錄計畫；不新增索引或改變搜尋語意。
$memberPlanCondition = "role='member' AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
    . "'abcdefghijklmnopqrstuvwxyz'), 'a')>0 OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
    . "'abcdefghijklmnopqrstuvwxyz'), 'a')>0)";
foreach (
    ['count' => "SELECT count(*) FROM accounts WHERE $memberPlanCondition",
        'page' => "SELECT id,email,nickname,role,status,created_at FROM accounts WHERE $memberPlanCondition "
            . 'ORDER BY created_at DESC,id DESC LIMIT 100 OFFSET 0'] as $label => $sql
) {
    $plan = json_decode(t10Sql('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' . $sql), true, 512, JSON_THROW_ON_ERROR);
    assert(isset($plan[0]['Plan']['Node Type'], $plan[0]['Execution Time']));
    echo 'member ' . $label . ' plan: ' . json_encode([
        'node' => $plan[0]['Plan']['Node Type'],
        'rows' => $plan[0]['Plan']['Actual Rows'],
        'execution_ms' => $plan[0]['Execution Time'],
        'hit_blocks' => $plan[0]['Plan']['Shared Hit Blocks'],
    ]) . "\n";
}

if ($nulMismatch !== null) {
    throw new RuntimeException($nulMismatch);
}

echo "Ticket 18 Django HTTP/SQL parity: PASS\n";
