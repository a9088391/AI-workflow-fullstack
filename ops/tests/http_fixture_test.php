<?php

declare(strict_types=1);

// 本檔為獨立執行的離線驗收入口，fixture 輔助函式與案例保留同檔。
// phpcs:disable PSR1.Files.SideEffects

/** 以子程序執行真實入口並停用 HTTP，確認拒絕發生在任何傳輸或資料操作之前。 */
function fixtureRun(array $arguments, array $environment): array
{
    $process = proc_open(
        [PHP_BINARY, '-d', 'zend.assertions=1', '-d', 'assert.exception=1',
            '-d', 'disable_functions=curl_init', ...$arguments],
        [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes,
        dirname(__DIR__, 2),
        $environment
    );
    if (! is_resource($process)) {
        throw new RuntimeException('無法啟動離線 PHP 檢查');
    }
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);

    return [proc_close($process), $output . $error];
}

// 即使名稱看似隔離，缺少可核對的 Compose 前提仍須在第一個 curl_init 之前拒絕。
[$status, $output] = fixtureRun(['ops/tests/register_http.php'], [
    'COMPOSE_PROJECT_NAME' => 'cq32_test',
    'API_BASE' => 'http://localhost:8081',
    'PATH' => '/nonexistent',
]);
if ($status === 0 || ! str_contains($output, 'HTTP fixture') || str_contains($output, 'curl_init')) {
    throw new RuntimeException('錯誤目標未在 HTTP 傳輸前拒絕：' . $output);
}
echo "PASS register rejects unverified target before HTTP\n";

/** 必要測試條件使用例外，讓本測試本身不受 PHP assertion 設定影響。 */
function fixtureCheck(bool $condition, string $message): void
{
    if (! $condition) {
        throw new RuntimeException($message);
    }
}

/** 產生原生資料卷與有限服務的 Docker 回應，讓離線案例核對隔離而不碰展示資料。 */
function fixtureData(string $work, array $ports, string $project = 'cq32_test'): array
{
    $root = dirname(__DIR__, 2);
    $config = [
        'name' => $project,
        'volumes' => ['postgres_data' => ['name' => $project . '_postgres_data']],
        'networks' => ['default' => ['name' => $project . '_default']],
        'services' => ['db' => ['environment' => ['POSTGRES_DB' => 'articles'],
            'volumes' => [['type' => 'volume', 'source' => 'postgres_data', 'target' => '/var/lib/postgresql']]]],
    ];
    $containers = [];
    foreach (['db' => [0, 0], ...$ports] as $service => [$published, $target]) {
        $container = [
            'Config' => ['Labels' => [
                'com.docker.compose.project' => $project,
                'com.docker.compose.service' => $service,
                'com.docker.compose.project.working_dir' => $root,
                'com.docker.compose.project.config_files' => $root . '/compose.yaml,' . $work . '/override.yaml',
            ], 'Env' => $service === 'db' ? ['POSTGRES_DB=articles'] : ['DB_HOST=db', 'DB_DATABASE=articles']],
            'State' => ['Running' => true, 'Health' => ['Status' => 'healthy']],
            'NetworkSettings' => ['Networks' => [$project . '_default' => []], 'Ports' => []],
            'HostConfig' => ['PortBindings' => []],
        ];
        if ($service === 'db') {
            $container['Mounts'] = [['Type' => 'volume', 'Name' => $project . '_postgres_data',
                'Destination' => '/var/lib/postgresql']];
        } else {
            $key = $work . '/' . $service . '.key';
            if (! is_file($key)) {
                file_put_contents($key, base64_encode(random_bytes(32)) . "\n");
            }
            $destination = '/run/secrets/' . $service . '-jwt.key';
            $config['services'][$service] = [
                'ports' => [['host_ip' => '127.0.0.1', 'published' => (string) $published, 'target' => $target]],
                'environment' => ['DB_HOST' => 'db', 'DB_DATABASE' => 'articles'],
                'volumes' => [['type' => 'bind', 'source' => $key, 'target' => $destination, 'read_only' => true]],
            ];
            // 九個後端在正式 Compose 使用 profile；未選 profile 的查核不可憑空看到這些服務。
            if (! in_array($service, ['laravel', 'symfony', 'aspnet-core'], true)) {
                $config['services'][$service]['profiles'] = [$service];
            }
            $bindings = [['HostIp' => '127.0.0.1', 'HostPort' => (string) $published]];
            $container['HostConfig']['PortBindings'][$target . '/tcp'] = $bindings;
            $container['NetworkSettings']['Ports'][$target . '/tcp'] = $bindings;
            $container['Mounts'] = [['Type' => 'bind', 'Source' => $key,
                'Destination' => $destination, 'RW' => false]];
        }
        $containers[$service] = $container;
    }

    // 名稱之外也提供實際 driver、options 與 Compose 歸屬，才能辨識同名的展示資料別名。
    $volume = ['Name' => $project . '_postgres_data', 'Driver' => 'local', 'Scope' => 'local',
        'Options' => null, 'Labels' => ['com.docker.compose.project' => $project,
            'com.docker.compose.volume' => 'postgres_data']];

    return ['config' => $config, 'containers' => $containers, 'volume_inspect' => [$volume]];
}

// fake Docker 只接受守衛的唯讀命令；任何 exec/run/stop 等副作用命令都會留下失敗 marker。
$work = sys_get_temp_dir() . '/cq32-http-' . bin2hex(random_bytes(6));
fixtureCheck(mkdir($work, 0755), '無法建立本輪離線 fixture');
try {
    file_put_contents($work . '/override.yaml', "services: {}\n");
    file_put_contents($work . '/docker', '#!' . PHP_BINARY . "\n" . <<<'PHP'
<?php
$data = json_decode(file_get_contents(getenv('HTTP_FIXTURE_DATA')), true, 512, JSON_THROW_ON_ERROR);
$args = array_slice($argv, 1);
$allProfiles = array_slice($args, 0, 3) === ['compose', '--profile', '*'];
if ($allProfiles) {
    array_splice($args, 1, 2);
}
// 沿 Compose 的 profile 可見範圍過濾唯讀 config/ps；Docker inspect 不受 Compose profile 影響。
$enabledProfiles = explode(',', getenv('COMPOSE_PROFILES') ?: '');
if ($args[0] === 'compose') {
    foreach ($data['config']['services'] as $service => $settings) {
        if (
            ! $allProfiles && isset($settings['profiles'])
            && ! array_intersect($settings['profiles'], $enabledProfiles)
        ) {
            unset($data['config']['services'][$service], $data['containers'][$service]);
        }
    }
}
if ($args === ['compose', 'config', '--format', 'json']) {
    echo json_encode($data['config'], JSON_THROW_ON_ERROR);
} elseif (array_slice($args, 0, 3) === ['compose', 'ps', '-q'] && count($args) === 4) {
    echo array_key_exists($args[3], $data['containers']) ? $args[3] : '';
} elseif ($args === ['volume', 'inspect', $data['config']['name'] . '_postgres_data']) {
    // 只回應固定預期名稱；另保留原始錯誤 JSON，驗證解析失敗仍走受控拒絕。
    echo $data['volume_inspect_output'] ?? json_encode($data['volume_inspect'], JSON_THROW_ON_ERROR);
} elseif ($args[0] === 'inspect' && count($args) === 2 && isset($data['containers'][$args[1]])) {
    echo json_encode([$data['containers'][$args[1]]], JSON_THROW_ON_ERROR);
} else {
    file_put_contents(getenv('HTTP_FIXTURE_TRACE'), 'FORBIDDEN_EFFECT');
    fwrite(STDERR, 'RAW_SECRET_MARKER');
    exit(29);
}
PHP);
    chmod($work . '/docker', 0755);
    $environment = [
        'PATH' => $work,
        'HTTP_FIXTURE_DATA' => $work . '/data.json',
        'HTTP_FIXTURE_TRACE' => $work . '/effects.txt',
        'COMPOSE_PROJECT_NAME' => 'cq32_test',
        'COMPOSE_FILE' => dirname(__DIR__, 2) . '/compose.yaml:' . $work . '/override.yaml',
    ];
    $call = 'require "ops/tests/http_fixture.php"; '
        . 'assertHttpFixture(json_decode(getenv("HTTP_FIXTURE_BASES"), true, 512, JSON_THROW_ON_ERROR), '
        . 'getenv("HTTP_FIXTURE_KEY") === false ? null : getenv("HTTP_FIXTURE_KEY")); echo "GUARD_OK";';
    $valid = fixtureData($work, ['laravel' => [18081, 8000], 'symfony' => [18082, 8000]]);
    $environment['HTTP_FIXTURE_BASES'] = '{"laravel":"http://localhost:18081","symfony":"http://localhost:18082"}';

    // 正向證明覆蓋單服務、雙服務、不同內部埠，以及歷史 tmp override 與非預設 API 埠。
    foreach (
        [
        ['single', ['laravel' => [18081, 8000]]],
        ['pair', ['laravel' => [18081, 8000], 'symfony' => [18082, 8000]]],
        ['profiled Django without COMPOSE_PROFILES', ['laravel' => [18081, 8000], 'django' => [18084, 8000]]],
        ['all backends', ['laravel' => [18081, 8000], 'symfony' => [18082, 8000],
            'aspnet-core' => [18083, 8080], 'django' => [18084, 8000], 'flask' => [18085, 8000],
            'fastapi' => [18086, 8000], 'gin' => [18087, 8080], 'echo' => [18088, 8080],
            'fiber' => [18089, 8080], 'express' => [18090, 8080], 'fastify' => [18091, 8080],
            'nestjs' => [18092, 8080]]],
        ['historical tmp override and writable key', ['laravel' => [18089, 8000]]],
        ] as [$name, $ports]
    ) {
        $data = fixtureData($work, $ports);
        file_put_contents($work . '/data.json', json_encode($data, JSON_THROW_ON_ERROR));
        $env = $environment;
        $bases = [];
        foreach ($ports as $service => [$published]) {
            $bases[$service] = 'http://localhost:' . $published;
        }
        $env['HTTP_FIXTURE_BASES'] = json_encode($bases, JSON_THROW_ON_ERROR);
        [$toolStatus, $toolOutput] = fixtureRun(
            [$work . '/docker', 'compose', '--profile', '*', 'config', '--format', 'json'],
            $env
        );
        fixtureCheck(
            $toolStatus === 0 && json_decode($toolOutput, true) === $data['config'],
            'fake Docker 前提失敗：' . $toolOutput
        );
        if ($name === 'profiled Django without COMPOSE_PROFILES') {
            [$toolStatus, $toolOutput] = fixtureRun([$work . '/docker', 'compose', 'config', '--format', 'json'], $env);
            $visible = json_decode($toolOutput, true, 512, JSON_THROW_ON_ERROR);
            fixtureCheck(
                $toolStatus === 0 && ! isset($visible['services']['django'])
                && isset($visible['services']['laravel']),
                'fake Docker 未保留預設 profile 可見範圍'
            );
            [$toolStatus, $toolOutput] = fixtureRun([$work . '/docker', 'inspect', 'django'], $env);
            fixtureCheck(
                $toolStatus === 0 && json_decode($toolOutput, true) === [$data['containers']['django']],
                'fake Docker inspect 不得套用 Compose profile 過濾'
            );
        }
        if ($name === 'historical tmp override and writable key') {
            $env['HTTP_FIXTURE_KEY'] = $work . '/laravel.key';
        }
        [$status, $output] = fixtureRun(['-r', $call], $env);
        fixtureCheck($status === 0 && $output === 'GUARD_OK', '正向守衛失敗：' . $name . ' ' . $output);
        echo 'PASS guard ' . $name . "\n";
    }

    // 每項只破壞一個已核准前提，確認實際身分、儲存與憑證錯配都不能通過。
    foreach (
        [
        ['config project', ['config', 'name'], 'other'],
        ['config volume', ['config', 'volumes', 'postgres_data', 'name'], 'demo_postgres_data'],
        ['external volume', ['config', 'volumes', 'postgres_data', 'external'], true],
        ['external network', ['config', 'networks', 'default', 'external'], true],
        ['actual project', ['containers', 'db', 'Config', 'Labels', 'com.docker.compose.project'], 'other'],
        ['actual service', ['containers', 'laravel', 'Config', 'Labels', 'com.docker.compose.service'], 'symfony'],
        ['working directory', ['containers', 'db', 'Config', 'Labels', 'com.docker.compose.project.working_dir'],
            '/other'],
        ['config files', ['containers', 'db', 'Config', 'Labels', 'com.docker.compose.project.config_files'],
            '/other.yaml'],
        ['actual network', ['containers', 'db', 'NetworkSettings', 'Networks'], ['other_default' => []]],
        ['unhealthy', ['containers', 'db', 'State', 'Health', 'Status'], 'unhealthy'],
        ['actual volume', ['containers', 'db', 'Mounts', 0, 'Name'], 'demo_postgres_data'],
        ['public DB port', ['containers', 'db', 'HostConfig', 'PortBindings'], ['5432/tcp' => []]],
        ['configured API port', ['config', 'services', 'laravel', 'ports', 0, 'published'], '8081'],
        ['nonloopback API', ['config', 'services', 'laravel', 'ports', 0, 'host_ip'], '0.0.0.0'],
        ['actual API port', ['containers', 'laravel', 'HostConfig', 'PortBindings', '8000/tcp', 0, 'HostPort'], '8081'],
        ['actual live API port', ['containers', 'laravel', 'NetworkSettings', 'Ports', '8000/tcp', 0, 'HostPort'],
            '8081'],
        ['configured DB mapping', ['config', 'services', 'laravel', 'environment', 'DB_HOST'], 'other'],
        ['actual DB mapping', ['containers', 'laravel', 'Config', 'Env'], ['DB_HOST=other', 'DB_DATABASE=articles']],
        ['writable JWT mount', ['containers', 'laravel', 'Mounts', 0, 'RW'], true],
        ['configured writable JWT mount', ['config', 'services', 'laravel', 'volumes', 0, 'read_only'], false],
        ['missing JWT file', ['config', 'services', 'laravel', 'volumes', 0, 'source'], $work . '/missing.key'],
        ['actual JWT mismatch', ['containers', 'laravel', 'Mounts', 0, 'Source'], $work . '/symfony.key'],
        ] as [$name, $path, $value]
    ) {
        $data = $valid;
        $field = &$data;
        foreach ($path as $part) {
            $field = &$field[$part];
        }
        $field = $value;
        unset($field);
        file_put_contents($work . '/data.json', json_encode($data, JSON_THROW_ON_ERROR));
        [$status, $output] = fixtureRun(['-r', $call], $environment);
        fixtureCheck($status !== 0 && str_contains($output, 'HTTP fixture 隔離檢查失敗')
            && ! str_contains($output, 'RAW_SECRET_MARKER'), '錯配未安全拒絕：' . $name);
        echo 'PASS reject ' . $name . "\n";
    }

    // 原生 volume 可省略 driver 或明列 local；Docker 的空 Options 可能為 null 或空陣列。
    foreach ([['explicit local', 'local', null], ['empty actual options', 'local', []]] as [$name, $driver, $options]) {
        $data = $valid;
        $data['config']['volumes']['postgres_data']['driver'] = $driver;
        $data['config']['volumes']['postgres_data']['driver_opts'] = [];
        $data['volume_inspect'][0]['Options'] = $options;
        file_put_contents($work . '/data.json', json_encode($data, JSON_THROW_ON_ERROR));
        [$status, $output] = fixtureRun(['-r', $call], $environment);
        fixtureCheck($status === 0 && $output === 'GUARD_OK', '合法原生資料卷未通過：' . $name . ' ' . $output);
        echo 'PASS native volume ' . $name . "\n";
    }

    // 每次只破壞一項資料卷前提；守衛後的寫入 marker 必須不存在，終端訊息不得洩露原始診斷。
    $writeCall = $call . 'file_put_contents(getenv("HTTP_FIXTURE_TRACE"), "FIXTURE_WRITE");';
    $volumeFailures = [];
    foreach (
        [
        ['configured bind alias', ['config', 'volumes', 'postgres_data', 'driver_opts'],
            ['type' => 'none', 'o' => 'bind', 'device' => '/demo/postgres'], 'Compose 設定'],
        ['configured custom driver', ['config', 'volumes', 'postgres_data', 'driver'], 'other', 'Compose 設定'],
        ['configured null driver', ['config', 'volumes', 'postgres_data', 'driver'], null, 'Compose 設定'],
        ['configured malformed driver', ['config', 'volumes', 'postgres_data', 'driver'], [], 'Compose 設定'],
        ['configured malformed options', ['config', 'volumes', 'postgres_data', 'driver_opts'], '', 'Compose 設定'],
        ['configured null options', ['config', 'volumes', 'postgres_data', 'driver_opts'], null, 'Compose 設定'],
        ['actual bind alias with plain config', ['volume_inspect', 0, 'Options'],
            ['type' => 'none', 'o' => 'bind', 'device' => '/demo/postgres'], '實際資料卷'],
        ['actual custom driver', ['volume_inspect', 0, 'Driver'], 'other', '實際資料卷'],
        ['actual missing driver', ['volume_inspect', 0, 'Driver'], null, '實際資料卷'],
        ['actual wrong name', ['volume_inspect', 0, 'Name'], 'demo_postgres_data', '實際資料卷'],
        ['actual wrong scope', ['volume_inspect', 0, 'Scope'], 'global', '實際資料卷'],
        ['actual missing scope', ['volume_inspect', 0, 'Scope'], null, '實際資料卷'],
        ['actual wrong project label', ['volume_inspect', 0, 'Labels', 'com.docker.compose.project'],
            'demo', '實際資料卷'],
        ['actual wrong volume label', ['volume_inspect', 0, 'Labels', 'com.docker.compose.volume'],
            'other', '實際資料卷'],
        ['actual missing labels', ['volume_inspect', 0, 'Labels'], null, '實際資料卷'],
        ['actual malformed options', ['volume_inspect', 0, 'Options'], '', '實際資料卷'],
        ['actual false options', ['volume_inspect', 0, 'Options'], false, '實際資料卷'],
        ['actual missing volume', ['volume_inspect'], [], '實際資料卷'],
        ['actual malformed volume', ['volume_inspect'], 'invalid', '實際資料卷'],
        ['actual extra volume', ['volume_inspect', 1], $valid['volume_inspect'][0], '實際資料卷'],
        ['actual malformed JSON', ['volume_inspect_output'], '{RAW_SECRET_MARKER', '實際資料卷'],
        ] as [$name, $path, $value, $stage]
    ) {
        $data = $valid;
        $field = &$data;
        foreach ($path as $part) {
            $field = &$field[$part];
        }
        $field = $value;
        unset($field);
        file_put_contents($work . '/data.json', json_encode($data, JSON_THROW_ON_ERROR));
        [$status, $output] = fixtureRun(['-r', $writeCall], $environment);
        $safe = $status !== 0 && str_contains($output, 'HTTP fixture 隔離檢查失敗：' . $stage)
            && ! str_contains($output, 'GUARD_OK') && ! str_contains($output, 'RAW_SECRET_MARKER')
            && ! is_file($work . '/effects.txt');
        if (! $safe) {
            $volumeFailures[] = $name . ' exit=' . $status . ' output=' . $output;
        }
        // 只移除本案例的 marker，讓 RED 也能逐項證明舊守衛是否已走到寫入終點。
        if (is_file($work . '/effects.txt')) {
            unlink($work . '/effects.txt');
        }
        echo ($safe ? 'PASS' : 'FAIL') . ' reject native volume before writes ' . $name . "\n";
    }
    fixtureCheck($volumeFailures === [], '資料卷未安全拒絕：' . implode("\n", $volumeFailures));

    // 同一內容的兩個檔名也不能冒充獨立後端金鑰；變更後立即復原本輪 fixture。
    file_put_contents($work . '/data.json', json_encode($valid, JSON_THROW_ON_ERROR));
    $symfonyKey = file_get_contents($work . '/symfony.key');
    file_put_contents($work . '/symfony.key', file_get_contents($work . '/laravel.key'));
    [$status, $output] = fixtureRun(['-r', $call], $environment);
    fixtureCheck($status !== 0 && str_contains($output, 'JWT 金鑰掛載'), '共用金鑰未拒絕');
    file_put_contents($work . '/symfony.key', $symfonyKey);
    echo "PASS reject duplicate key contents\n";
    // 金鑰檔存在仍須可解為 32 bytes，不能把格式錯誤誤認為已備妥可用 fixture。
    $laravelKey = file_get_contents($work . '/laravel.key');
    foreach (['broken', base64_encode(str_repeat('x', 31))] as $invalidKey) {
        file_put_contents($work . '/laravel.key', $invalidKey);
        [$status, $output] = fixtureRun(['-r', $call], $environment);
        fixtureCheck($status !== 0 && str_contains($output, 'JWT 金鑰掛載'), '不合法 JWT 金鑰未拒絕');
    }
    file_put_contents($work . '/laravel.key', $laravelKey);
    echo "PASS reject malformed and wrong-length key contents\n";

    // root 可寫入唯讀 mode 的一般檔；以 nobody 子程序驗證真正不可寫的測試金鑰前提。
    chmod($work . '/laravel.key', 0444);
    $readonlyEnvironment = $environment + ['HTTP_FIXTURE_KEY' => $work . '/laravel.key'];
    $readonlyCall = 'if (!function_exists("posix_setuid") || !posix_setuid(65534)) '
        . '{ throw new RuntimeException("離線不可寫金鑰案例缺少降權前提"); } ' . $call;
    [$status, $output] = fixtureRun(['-r', $readonlyCall], $readonlyEnvironment);
    chmod($work . '/laravel.key', 0644);
    fixtureCheck($status !== 0 && str_contains($output, '可寫測試金鑰'), '不可寫金鑰未經實際權限拒絕：' . $output);
    echo "PASS reject unwritable fixture key\n";

    // 以完整、未改寫的守衛副本建立自有展示目錄；有效檔案仍須因位置而拒絕，不能只測缺檔。
    $demoRoot = $work . '/repo';
    foreach (['', '/ops', '/ops/tests', '/.local', '/.local/keys'] as $directory) {
        fixtureCheck(mkdir($demoRoot . $directory, 0755), '無法建立自有展示金鑰反例');
    }
    copy(__DIR__ . '/http_fixture.php', $demoRoot . '/ops/tests/http_fixture.php');
    file_put_contents($demoRoot . '/compose.yaml', "services: {}\n");
    file_put_contents($demoRoot . '/.local/keys/laravel.key', base64_encode(random_bytes(32)) . "\n");
    $demo = $valid;
    $demo['config']['services']['laravel']['volumes'][0]['source'] = $demoRoot . '/.local/keys/laravel.key';
    $demo['containers']['laravel']['Mounts'][0]['Source'] = $demoRoot . '/.local/keys/laravel.key';
    foreach ($demo['containers'] as &$container) {
        $container['Config']['Labels']['com.docker.compose.project.working_dir'] = $demoRoot;
        $container['Config']['Labels']['com.docker.compose.project.config_files'] = $demoRoot
            . '/compose.yaml,' . $work . '/override.yaml';
    }
    unset($container);
    file_put_contents($work . '/data.json', json_encode($demo, JSON_THROW_ON_ERROR));
    [$status, $output] = fixtureRun(
        ['-r', 'chdir($argv[1]); ' . $call, $demoRoot],
        array_replace($environment, ['COMPOSE_FILE' => $demoRoot . '/compose.yaml:' . $work . '/override.yaml'])
    );
    fixtureCheck($status !== 0 && str_contains($output, 'JWT 金鑰掛載'), '展示金鑰未拒絕');
    echo "PASS reject existing valid key in owned demo directory\n";
    file_put_contents($work . '/data.json', json_encode($valid, JSON_THROW_ON_ERROR));
    foreach (
        [
        ['wrong project', ['COMPOSE_PROJECT_NAME' => 'other'], []],
        ['demo project', ['COMPOSE_PROJECT_NAME' => 'ai-workflow-fullstack'], []],
        ['wrong API', ['HTTP_FIXTURE_BASES' => '{"laravel":"http://localhost:8081"}'], []],
        ['wrong writable key', ['HTTP_FIXTURE_KEY' => $work . '/symfony.key'], []],
        ['assertions disabled', [], ['-d', 'zend.assertions=0']],
        ['assert active disabled', [], ['-d', 'assert.active=0']],
        ['assert exceptions disabled', [], ['-d', 'assert.exception=0']],
        ] as [$name, $changes, $arguments]
    ) {
        [$status, $output] = fixtureRun([...$arguments, '-r', $call], array_replace($environment, $changes));
        fixtureCheck($status !== 0 && str_contains($output, 'HTTP fixture'), '前提未拒絕：' . $name);
        echo 'PASS reject ' . $name . "\n";
    }

    // 重播每個真實入口允許的原專案／埠；原守衛可通過時，共用守衛仍須攔下 external network。
    foreach (
        [
        ['register_http.php', 'cq32_test', null, ['laravel' => [8081, 8000]]],
        ['auth_http.php', 'ticket02_offline', null, ['laravel' => [18081, 8000]]],
        ['article_http.php', 'ticket03_offline', null, ['laravel' => [8081, 8000]]],
        ['comment_http.php', 'ticket04_offline', null, ['laravel' => [8081, 8000]]],
        ['lifecycle_http.php', 'ticket05_test', null, ['laravel' => [8081, 8000]]],
        ['admin_http.php', 'ticket06_test_offline', null, ['laravel' => [18086, 8000]]],
        ['lockout_settings_http.php', 'ticket07_test_offline', null, ['laravel' => [18087, 8000]]],
        ['symfony_http.php', 'ticket08_offline', null, ['laravel' => [18081, 8000], 'symfony' => [18082, 8000]]],
        ['aspnet_http.php', 'ticket13_offline', 'aspnet_compose.yaml',
            ['laravel' => [18081, 8000], 'symfony' => [18082, 8000], 'aspnet-core' => [18083, 8080]]],
        ['django_http.php', 'ticket16_offline', 'ticket16_compose.yaml',
            ['laravel' => [18081, 8000], 'django' => [18084, 8000]]],
        ['ticket09_http.php', 'ticket09_test', null, ['laravel' => [19081, 8000], 'symfony' => [19082, 8000]]],
        ['ticket14_http.php', 'ticket14_test', 'ticket14_compose.yaml',
            ['laravel' => [18081, 8000], 'aspnet-core' => [18083, 8080]]],
        ['ticket10_http.php', 'ticket10_test', null, ['laravel' => [19181, 8000], 'symfony' => [19182, 8000]]],
        ['ticket15_http.php', 'ticket15_test', 'ticket15_compose.yaml',
            ['laravel' => [19281, 8000], 'symfony' => [19282, 8000], 'aspnet-core' => [19283, 8080]]],
        ['ticket17_http.php', 'ticket17_test', 'ticket17_compose.yaml',
            ['laravel' => [8081, 8000], 'django' => [8084, 8000]]],
        ] as [$script, $project, $overlay, $ports]
    ) {
        $data = fixtureData($work, $ports, $project);
        $data['config']['networks']['default']['external'] = true;
        $env = array_replace($environment, ['COMPOSE_PROJECT_NAME' => $project,
            'COMPOSE_PROFILES' => 'django', 'AUTH_TEST_KEY' => $work . '/laravel.key']);
        if ($overlay !== null) {
            $env['COMPOSE_FILE'] = 'compose.yaml:ops/tests/' . $overlay;
        }
        if ($script === 'ticket15_http.php') {
            $data['config']['services']['nginx']['ports'] = [
                ['host_ip' => '127.0.0.1', 'published' => '19280', 'target' => 80],
            ];
        }
        if ($script === 'ticket17_http.php') {
            $data['config']['services']['django']['volumes'][0]['source'] = dirname(__DIR__, 2)
                . '/.local/ticket17-backend/django.key';
        }
        file_put_contents($work . '/data.json', json_encode($data, JSON_THROW_ON_ERROR));
        [$status, $output] = fixtureRun(['ops/tests/' . $script], $env);
        fixtureCheck($status !== 0 && str_contains($output, 'HTTP fixture 隔離檢查失敗')
            && ! str_contains($output, 'curl_init'), '入口未在 HTTP 前接上共用守衛：' . $script . ' ' . $output);
        [$status, $output] = fixtureRun(['-d', 'zend.assertions=0', 'ops/tests/' . $script], $env);
        fixtureCheck(
            $status !== 0 && str_contains($output, 'HTTP fixture 必須啟用'),
            '入口未在原守衛或資料操作前檢查斷言：' . $script
        );
        echo 'PASS consumer guard and assertions ' . $script . "\n";
    }
    [$status, $output] = fixtureRun(['-d', 'zend.assertions=0', 'ops/tests/ticket18_http.php'], $environment);
    fixtureCheck($status !== 0 && str_contains($output, 'HTTP fixture 必須啟用'), 'Ticket 18 未先檢查斷言');
    echo "PASS Ticket 18 rejects disabled assertions before its existing strong guard\n";
    fixtureCheck(! is_file($work . '/effects.txt'), '守衛呼叫了副作用命令');
} finally {
    // 只清除此程序獨占建立的目錄樹，不追蹤 symlink；失敗也不留下 fake Docker 或測試金鑰。
    $entries = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($work, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST
    );
    foreach ($entries as $entry) {
        if ($entry->isDir() && ! $entry->isLink()) {
            rmdir($entry->getPathname());
        } else {
            unlink($entry->getPathname());
        }
    }
    rmdir($work);
}

// pg_dump 的輸出本來即不公開；同一原生 descriptor 機制須能處理超過 pipe buffer 的 stderr 並取得 exit。
$process = proc_open(
    [PHP_BINARY, '-r', 'fwrite(STDERR, str_repeat("x", 262144)); exit(42);'],
    [0 => ['file', '/dev/null', 'r'], 1 => ['file', '/dev/null', 'w'], 2 => ['file', '/dev/null', 'w']],
    $pipes
);
fixtureCheck(is_resource($process) && proc_close($process) === 42, '大量備份診斷的丟棄／結束狀態機制失敗');
echo "PASS discarded 262144-byte stderr returns exit 42; SQL integration remains Main-owned\n";

// 真實 SQL 入口在連線前即拒絕缺設定，確認 namespace 整理仍保留受控例外判斷與公開文案。
[$status, $output] = fixtureRun(['ops/sql/apply.php'], []);
fixtureCheck($status === 1 && trim($output) === '必要資料庫設定或 SQL 版本檔缺失', 'SQL 受控初始化失敗契約變動');
echo "PASS SQL controlled missing-configuration error without a DB connection\n";
echo "HTTP fixture offline guard checks passed; no live HTTP/database proof\n";
