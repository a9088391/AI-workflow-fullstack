<?php

declare(strict_types=1);

/** 真實 HTTP 回傳狀態、JSON 與原始 Body；字串 Body 保留 JSON 整數原文以驗證解析邊界。 */
function t17Request(
    string $base,
    string $method,
    string $path,
    array|string|null $body = null,
    ?string $token = null
): array {
    $headers = ['Accept: application/json', 'Content-Type: application/json'];
    if ($token !== null) {
        $headers[] = 'Authorization: Bearer ' . $token;
    }
    $curl = curl_init($base . $path);
    curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $headers,
        CURLOPT_POSTFIELDS => $body === null ? '' : (is_string($body) ? $body : json_encode(
            $body,
            JSON_THROW_ON_ERROR
        )),
        CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
    $raw = curl_exec($curl);
    if ($raw === false) {
        throw new RuntimeException('HTTP failed: ' . curl_error($curl));
    }
    $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
    return [curl_getinfo($curl, CURLINFO_RESPONSE_CODE), json_decode(
        substr($raw, $length),
        true,
        512,
        JSON_THROW_ON_ERROR
    ), substr($raw, $length)];
}

/** 嚴格檢查欄位、狀態與錯誤文案，避免只檢查 HTTP status。 */
function t17Check(array $result, int $status, ?string $code = null, ?array $errors = null): array
{
    $messages = ['E0001' => '欄位資料不正確，請確認後再送出', 'E0002' => '登入已逾期，請重新登入',
        'E0003' => '沒有權限使用此功能', 'E0004' => '找不到指定資料或 API',
        'E0201' => '內容已更新，請確認最新內容後再送出'];
    $response = $result[1];
    if ($result[0] !== $status || $response['success'] !== ($code === null)) {
        throw new RuntimeException('HTTP status/success mismatch: ' . json_encode([$result[0],
            $response['error_code'] ?? null]));
    }
    if ($code === null) {
        if ($response['message'] !== 'OK' || ! is_array($response['data'])) {
            throw new RuntimeException('Success envelope mismatch');
        }
    } elseif (
        ($response['error_code'] ?? null) !== $code || ($response['message'] ?? null) !== $messages[$code]
        || ($response['errors'] ?? null) !== $errors
        || ! is_string($response['log_id'] ?? null)
        || preg_match('/\A[0-9a-f-]{36}\z/', $response['log_id']) !== 1
    ) {
        throw new RuntimeException('Error envelope mismatch: ' . json_encode($response));
    }
    return $response;
}

/** 唯一 fixture 使用隔離 owner 連線；SQL 不接收使用者輸入。 */
function t17Sql(string $sql): string
{
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v',
            'ON_ERROR_STOP=1', '-At', '-c', $sql],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('Cannot open isolated psql');
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException('Isolated SQL failed: ' . $error);
    }
    return trim($output);
}

/** 新帳號由公開 API 建立及登入，管理員 fixture 僅改本隔離資料庫的角色。 */
function t17Member(string $base, string $name, bool $admin = false): array
{
    $email = 't17-' . bin2hex(random_bytes(6)) . '@example.test';
    t17Check(t17Request($base, 'POST', '/api/auth/register', ['email' => $email, 'password' => 'Abc1234',
        'nickname' => $name]), 201);
    if ($admin) {
        t17Sql("UPDATE accounts SET role='admin' WHERE email='" . $email . "'");
    }
    $login = t17Check(t17Request($base, 'POST', '/api/auth/login', ['email' => $email, 'password' => 'Abc1234']), 200);
    return [$login['data']['user']['id'], $login['data']['access_token'], $email];
}

/** 我的列表以新 fixture 首項找 ID，避免發表 API 私自回傳未約定 ID。 */
function t17Mine(string $base, string $kind, string $token): array
{
    return t17Check(t17Request($base, 'GET', '/api/me/' . $kind . '?page=1&per_page=20', null, $token), 200)['data'];
}

/** 非空成功寫入必須只回 data:{}，建立與編修均遵守此契約。 */
function t17Write(string $base, string $method, string $path, array $body, string $token, int $status = 200): void
{
    $result = t17Request($base, $method, $path, $body, $token);
    t17Check($result, $status);
    if (
        $result[1]['data'] !== [] || ! (json_decode(
            $result[2],
            false,
            512,
            JSON_THROW_ON_ERROR
        )->data instanceof stdClass)
    ) {
        throw new RuntimeException('Write did not return data:{}');
    }
}

/** 比較列原始值，確認失敗及同值操作不更動時間、版本或控制者。 */
function t17Snapshot(string $table, int $id): string
{
    return t17Sql("SELECT row_to_json(t)::text FROM $table t WHERE id=$id");
}

/** 先鎖指定帳號或文章並觀察兩後端實際等待，再提交狀態轉移以裁決寫入順序。 */
function t17Race(string $lockTable, int $lockId, array $requests, string $beforeCommit = ''): array
{
    if (! in_array($lockTable, ['accounts', 'articles'], true)) {
        throw new RuntimeException('Unsupported isolated race lock');
    }
    $process = proc_open(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'articles_owner', '-d', 'articles', '-v',
            'ON_ERROR_STOP=1', '-qAt'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (! is_resource($process)) {
        throw new RuntimeException('Cannot open isolated race fixture');
    }
    $multi = curl_multi_init();
    $handles = [];
    try {
        fwrite(
            $pipes[0],
            "BEGIN;\nDO \$\$ BEGIN PERFORM 1 FROM $lockTable WHERE id=$lockId FOR UPDATE; "
            . "END \$\$;\nSELECT 'locked';\n"
        );
        fflush($pipes[0]);
        if (trim((string) fgets($pipes[1])) !== 'locked') {
            throw new RuntimeException('Race fixture did not lock account');
        }
        foreach ($requests as [$base, $method, $path, $body, $token]) {
            $curl = curl_init($base . $path);
            curl_setopt_array($curl, [CURLOPT_CUSTOMREQUEST => $method,
                CURLOPT_POSTFIELDS => json_encode($body, JSON_THROW_ON_ERROR),
                CURLOPT_HTTPHEADER => ['Accept: application/json', 'Content-Type: application/json',
                    'Authorization: Bearer ' . $token],
                CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true, CURLOPT_TIMEOUT => 30]);
            curl_multi_add_handle($multi, $curl);
            $handles[] = $curl;
        }
        // 不靠猜測延遲；確認每個請求已在 PostgreSQL 行鎖上等待。
        $deadline = microtime(true) + 10;
        do {
            curl_multi_exec($multi, $running);
            $waiting = (int) t17Sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname='articles' "
                . "AND usename='articles_app' AND wait_event_type='Lock'"
            );
            if ($waiting < count($requests) && $running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($waiting < count($requests) && $running > 0 && microtime(true) < $deadline);
        if ($waiting < count($requests)) {
            throw new RuntimeException('HTTP did not wait on account row lock');
        }
        fwrite($pipes[0], $beforeCommit . "\nCOMMIT;\n");
        fclose($pipes[0]);
        stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        if (proc_close($process) !== 0) {
            throw new RuntimeException('Race fixture commit failed: ' . $error);
        }
        do {
            curl_multi_exec($multi, $running);
            if ($running > 0) {
                curl_multi_select($multi, 0.05);
            }
        } while ($running > 0);
        $responses = [];
        foreach ($handles as $curl) {
            $raw = curl_multi_getcontent($curl);
            if (! is_string($raw) || curl_errno($curl) !== 0) {
                throw new RuntimeException('Race HTTP failed: ' . curl_error($curl));
            }
            $length = curl_getinfo($curl, CURLINFO_HEADER_SIZE);
            $responses[] = [curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
                json_decode(substr($raw, $length), true, 512, JSON_THROW_ON_ERROR), substr($raw, $length)];
        }
        return $responses;
    } finally {
        foreach ($handles as $curl) {
            curl_multi_remove_handle($multi, $curl);
            curl_close($curl);
        }
        curl_multi_close($multi);
        if (is_resource($process)) {
            // 例外時先回滾並關閉 owner 連線，不能留下持鎖交易。
            if (isset($pipes[0]) && is_resource($pipes[0])) {
                fwrite($pipes[0], "ROLLBACK;\n");
                fclose($pipes[0]);
            }
            foreach ([1, 2] as $index) {
                if (isset($pipes[$index]) && is_resource($pipes[$index])) {
                    fclose($pipes[$index]);
                }
            }
            proc_close($process);
        }
    }
}
