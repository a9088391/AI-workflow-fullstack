<?php

declare(strict_types=1);

/** 強制啟用測試斷言及例外，避免腳本寫入資料卻跳過契約檢查。 */
function assertHttpFixtureAssertions(): void
{
    if (
        (int) ini_get('zend.assertions') !== 1
        || ! filter_var(ini_get('assert.active'), FILTER_VALIDATE_BOOLEAN)
        || ! filter_var(ini_get('assert.exception'), FILTER_VALIDATE_BOOLEAN)
    ) {
        throw new RuntimeException('HTTP fixture 必須啟用 PHP assertions 與例外');
    }
}

/** 只讀 Docker 身分 JSON；丟棄可能含環境設定的 stderr，失敗由守衛轉為受控訊息。 */
function httpFixtureDocker(array $arguments): string
{
    $process = @proc_open(
        ['docker', ...$arguments],
        [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['file', '/dev/null', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('Docker 身分查核不可用');
    }
    try {
        $output = stream_get_contents($pipes[1]);
    } finally {
        fclose($pipes[1]);
        $status = proc_close($process);
    }
    if ($output === false || $status !== 0) {
        throw new RuntimeException('Docker 身分查核失敗');
    }

    return trim($output);
}

/** 共用隔離前提不使用可關閉的 assert，讓設定或實際容器不符時必定中止。 */
function httpFixtureRequire(bool $condition): void
{
    if (! $condition) {
        throw new RuntimeException('隔離前提不符');
    }
}

/** 將既有 API 與實際 Compose 資源綁在一起；只允許隔離 volume 與非展示金鑰承受測試寫入。 */
function assertHttpFixture(array $bases, ?string $writableKey = null): void
{
    assertHttpFixtureAssertions();
    $stage = '執行目錄或專案';
    try {
        $root = dirname(__DIR__, 2);
        $project = getenv('COMPOSE_PROJECT_NAME') ?: '';
        httpFixtureRequire(getcwd() === $root && $project !== '' && $project !== 'ai-workflow-fullstack');
        httpFixtureRequire($bases !== [] && ! isset($bases['db']));

        // 保留合法的歷史 override；以實際存在的檔案與容器標籤核對，不依檔名猜測隔離。
        $stage = 'Compose 設定';
        $configFiles = [];
        foreach (explode(PATH_SEPARATOR, getenv('COMPOSE_FILE') ?: 'compose.yaml') as $file) {
            $path = realpath($file);
            httpFixtureRequire($path !== false && is_file($path));
            $configFiles[] = $path;
        }
        // 身分查核明列所有 profile，避免合法後端因 caller 未設定 COMPOSE_PROFILES 而被漏掉。
        $config = json_decode(
            httpFixtureDocker(['compose', '--profile', '*', 'config', '--format', 'json']),
            true,
            512,
            JSON_THROW_ON_ERROR
        );
        $volume = $project . '_postgres_data';
        $network = $project . '_default';
        $configuredVolume = $config['volumes']['postgres_data'] ?? [];
        // 資料卷只接受原生 local 且無 driver options，避免同名 local bind 別名指回展示資料。
        httpFixtureRequire(
            ($config['name'] ?? null) === $project
            && ($config['volumes']['postgres_data']['name'] ?? null) === $volume
            && ! ($config['volumes']['postgres_data']['external'] ?? false)
            && (! array_key_exists('driver', $configuredVolume) || $configuredVolume['driver'] === 'local')
            && (! array_key_exists('driver_opts', $configuredVolume) || $configuredVolume['driver_opts'] === [])
            && ($config['networks']['default']['name'] ?? null) === $network
            && ! ($config['networks']['default']['external'] ?? false)
        );
        $db = $config['services']['db'] ?? [];
        $dbMounts = $db['volumes'] ?? [];
        httpFixtureRequire(
            ($db['environment']['POSTGRES_DB'] ?? null) === 'articles'
            && ($db['ports'] ?? []) === [] && count($dbMounts) === 1
            && ($dbMounts[0]['type'] ?? null) === 'volume'
            && ($dbMounts[0]['source'] ?? null) === 'postgres_data'
            && ($dbMounts[0]['target'] ?? null) === '/var/lib/postgresql'
        );

        // 設定與執行中的服務都必須屬於同專案；僅驗環境變數不能證明 HTTP 與 SQL 連到同一資料庫。
        $keys = [];
        foreach (['db', ...array_keys($bases)] as $service) {
            $stage = '實際容器身分';
            httpFixtureRequire(in_array($service, [
                'db', 'laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi',
                'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs',
            ], true));
            $id = httpFixtureDocker(['compose', '--profile', '*', 'ps', '-q', $service]);
            httpFixtureRequire($id !== '' && ! str_contains($id, "\n"));
            $container = json_decode(httpFixtureDocker(['inspect', $id]), true, 512, JSON_THROW_ON_ERROR)[0] ?? [];
            $labels = $container['Config']['Labels'] ?? [];
            httpFixtureRequire(
                ($labels['com.docker.compose.project'] ?? null) === $project
                && ($labels['com.docker.compose.service'] ?? null) === $service
                && ($labels['com.docker.compose.project.working_dir'] ?? null) === $root
                && ($labels['com.docker.compose.project.config_files'] ?? null) === implode(',', $configFiles)
                && ($container['State']['Running'] ?? false) === true
                && ($container['State']['Health']['Status'] ?? null) === 'healthy'
                && array_keys($container['NetworkSettings']['Networks'] ?? []) === [$network]
            );
            $mounts = array_column($container['Mounts'] ?? [], null, 'Destination');
            if ($service === 'db') {
                $stage = '實際資料卷';
                // 名稱不能證明資料隔離；實際 volume 也須為本專案建立、無 options 的原生 local 卷。
                $volumes = json_decode(
                    httpFixtureDocker(['volume', 'inspect', $volume]),
                    true,
                    512,
                    JSON_THROW_ON_ERROR
                );
                httpFixtureRequire(
                    is_array($volumes) && array_is_list($volumes) && count($volumes) === 1
                    && is_array($volumes[0])
                );
                $actualVolume = $volumes[0];
                httpFixtureRequire(
                    ($actualVolume['Name'] ?? null) === $volume && ($actualVolume['Driver'] ?? null) === 'local'
                    && ($actualVolume['Scope'] ?? null) === 'local' && array_key_exists('Options', $actualVolume)
                    && ($actualVolume['Options'] === null || $actualVolume['Options'] === [])
                    && ($actualVolume['Labels']['com.docker.compose.project'] ?? null) === $project
                    && ($actualVolume['Labels']['com.docker.compose.volume'] ?? null) === 'postgres_data'
                );
                httpFixtureRequire(
                    count($mounts) === 1 && ($mounts['/var/lib/postgresql']['Type'] ?? null) === 'volume'
                    && ($mounts['/var/lib/postgresql']['Name'] ?? null) === $volume
                    && ($container['HostConfig']['PortBindings'] ?? []) === []
                    && in_array('POSTGRES_DB=articles', $container['Config']['Env'] ?? [], true)
                );
                continue;
            }

            // URL 必須精確對應同一服務的唯一 loopback 埠；禁止任意 origin 或展示 API 混入 fixture。
            $stage = 'API 與資料庫映射';
            $settings = $config['services'][$service] ?? [];
            $ports = $settings['ports'] ?? [];
            $port = $ports[0] ?? [];
            $target = (string) ($port['target'] ?? '');
            $binding = [['HostIp' => '127.0.0.1', 'HostPort' => (string) ($port['published'] ?? '')]];
            httpFixtureRequire(
                count($ports) === 1 && ($port['host_ip'] ?? null) === '127.0.0.1'
                && ctype_digit((string) ($port['published'] ?? '')) && ctype_digit($target)
                && $bases[$service] === 'http://localhost:' . $port['published']
                && ($container['HostConfig']['PortBindings'][$target . '/tcp'] ?? null) === $binding
                && ($container['NetworkSettings']['Ports'][$target . '/tcp'] ?? null) === $binding
                && ($settings['environment']['DB_HOST'] ?? null) === 'db'
                && ($settings['environment']['DB_DATABASE'] ?? null) === 'articles'
                && in_array('DB_HOST=db', $container['Config']['Env'] ?? [], true)
                && in_array('DB_DATABASE=articles', $container['Config']['Env'] ?? [], true)
            );

            // 比對唯讀掛載的真正來源，避免合法外觀的 symlink 或測試路徑仍指向展示金鑰。
            $stage = 'JWT 金鑰掛載';
            $destination = '/run/secrets/' . $service . '-jwt.key';
            $configuredMounts = array_column($settings['volumes'] ?? [], null, 'target');
            $keyMount = $configuredMounts[$destination] ?? [];
            $actualKey = $mounts[$destination] ?? [];
            $keyPath = realpath($keyMount['source'] ?? '');
            $demoKeys = realpath($root . '/.local/keys') ?: $root . '/.local/keys';
            httpFixtureRequire(
                ($keyMount['type'] ?? null) === 'bind' && ($keyMount['read_only'] ?? false) === true
                && ($actualKey['Type'] ?? null) === 'bind' && ($actualKey['RW'] ?? true) === false
                && $keyPath !== false && is_file($keyPath) && is_readable($keyPath)
                && realpath($actualKey['Source'] ?? '') === $keyPath
                && ! str_starts_with($keyPath, $demoKeys . DIRECTORY_SEPARATOR)
            );
            $contents = @file_get_contents($keyPath);
            $key = $contents === false ? false : base64_decode(trim($contents), true);
            httpFixtureRequire($key !== false && strlen($key) === 32 && ! in_array($key, $keys, true));
            $keys[$service] = $key;
            if ($service === 'laravel' && $writableKey !== null) {
                $stage = '可寫測試金鑰';
                httpFixtureRequire(realpath($writableKey) === $keyPath && is_writable($keyPath));
            }
        }
        httpFixtureRequire($writableKey === null || isset($keys['laravel']));
    } catch (Throwable) {
        // Docker／JSON／檔案錯誤可能攜帶環境或路徑資訊，只公開目前的隔離檢查階段。
        throw new RuntimeException('HTTP fixture 隔離檢查失敗：' . $stage);
    }
}
