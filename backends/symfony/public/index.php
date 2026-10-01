<?php

use App\ApiController;
use App\ApiFailure;
use App\Database;
use App\Kernel;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpKernel\Exception\HttpExceptionInterface;

require dirname(__DIR__).'/vendor/autoload.php';

$request = Request::createFromGlobals();
if ($request->getPathInfo() === '/up') {
    // 健康檢查必須確認共用資料表可讀，避免資料庫失聯時仍標示後端就緒。
    try {
        (new Database())->connection()->query('SELECT 1 FROM accounts, login_sessions, login_failures, login_lockout_settings, articles, comments LIMIT 0');
        (new JsonResponse(['status' => 'ok']))->send();
    } catch (Throwable) {
        (new JsonResponse(['status' => 'unavailable'], 503))->send();
    }

    return;
}

$logId = sprintf('%s-%s-%s-%s-%s', bin2hex(random_bytes(4)), bin2hex(random_bytes(2)), '4'.substr(bin2hex(random_bytes(2)), 1), dechex((hexdec(bin2hex(random_bytes(1))) & 0x3f) | 0x80).bin2hex(random_bytes(1)), bin2hex(random_bytes(6)));
$path = $request->getPathInfo();
$logFile = dirname(__DIR__).'/var/log/api-'.gmdate('Y-m-d').'.log';
// 僅記方法、路徑、狀態、安全錯誤碼及例外類別，不記帳密、JWT 或雜湊。
@file_put_contents($logFile, json_encode(['event' => 'IN', 'log_id' => $logId, 'method' => $request->getMethod(), 'path' => $path])."\n", FILE_APPEND);
$kernel = new Kernel(getenv('APP_ENV') ?: 'prod', false);
try {
    if ($request->getMethod() === 'OPTIONS') {
        $response = new JsonResponse(null, 204);
        $response->headers->set('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
        $response->headers->set('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept');
    } else {
        $response = $kernel->handle($request, 1, false);
    }
} catch (ApiFailure $failure) {
    // 認證設定或雜湊毀損須能由 log_id 追查；記固定代碼與類別即可，避免寫入敏感值。
    @file_put_contents($logFile, json_encode(['event' => 'EX', 'log_id' => $logId, 'error_code' => $failure->codeName, 'exception' => $failure::class])."\n", FILE_APPEND);
    $response = ApiController::failure($failure, $logId);
} catch (Throwable $exception) {
    @file_put_contents($logFile, json_encode(['event' => 'EX', 'log_id' => $logId, 'exception' => $exception::class])."\n", FILE_APPEND);
    $code = $exception instanceof HttpExceptionInterface && $exception->getStatusCode() === 404 ? 'E0004' : ($exception instanceof HttpExceptionInterface && $exception->getStatusCode() < 500 ? 'E0005' : 'E9999');
    $response = ApiController::failure(new ApiFailure($code, null, null, $code === 'E0005' ? $exception->getStatusCode() : null), $logId);
}
// 登入鎖定業務錯誤是正常提交結果；在送出前補同一請求 log_id。
if ($response->getStatusCode() >= 400 && $response instanceof JsonResponse) {
    $data = json_decode($response->getContent(), true);
    if (is_array($data) && array_key_exists('log_id', $data) && $data['log_id'] === null) {
        $data['log_id'] = $logId;
        $response->setData($data);
    }
}
if ($request->headers->get('Origin') === 'http://localhost:8080') {
    $response->headers->set('Access-Control-Allow-Origin', 'http://localhost:8080');
    $response->headers->set('Vary', 'Origin');
    $response->headers->set('Access-Control-Expose-Headers', 'Retry-After');
}
@file_put_contents($logFile, json_encode(['event' => 'EX', 'log_id' => $logId, 'status' => $response->getStatusCode()])."\n".json_encode(['event' => 'OUT', 'log_id' => $logId, 'status' => $response->getStatusCode()])."\n", FILE_APPEND);
$response->send();
$kernel->terminate($request, $response);
