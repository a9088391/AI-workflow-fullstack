<?php

namespace App\Http\Middleware;

use App\Http\ApiResponse;
use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpKernel\Exception\HttpExceptionInterface;
use Throwable;

class ApiEnvelope
{
    /** 給每次 API 呼叫單一追蹤碼，並只記方法、路徑及結果以免記下帳密。 */
    public function handle(Request $request, Closure $next): Response
    {
        if (! $request->is('api', 'api/*')) {
            return $next($request);
        }

        // 先建立追蹤碼供控制器與例外管線共用；不記錄 Query、Body 或 Authorization。
        $logId = (string) Str::uuid();
        $request->attributes->set('log_id', $logId);
        $logger = Log::build(['driver' => 'daily', 'path' => storage_path('logs/api.log'), 'days' => 14]);
        $logger->info('IN', ['log_id' => $logId, 'method' => $request->method(), 'path' => $request->path()]);

        // 捕捉仍穿出框架例外管線的錯誤，對外只保留安全的共同 envelope。
        try {
            $response = $next($request);
        } catch (Throwable $exception) {
            // SQL/框架例外內容可能夾帶輸入或密碼雜湊，只記類別供追查。
            $logger->error('EX', ['log_id' => $logId, 'exception' => $exception::class]);
            if ($exception instanceof HttpExceptionInterface) {
                $response = $exception->getStatusCode() === 404
                    ? ApiResponse::error(404, 'E0004', '找不到指定資料或 API')
                    : ApiResponse::error($exception->getStatusCode(), 'E0005', '系統暫時無法處理，請稍後再試');
            } else {
                $response = ApiResponse::error(500, 'E9999', '系統暫時無法處理，請稍後再試');
            }
        }

        // 框架可能已把例外轉為回應；所有結果仍留下同一追蹤碼的 EX／OUT 狀態。
        $logger->info('EX', ['log_id' => $logId, 'status' => $response->getStatusCode()]);
        $logger->info('OUT', ['log_id' => $logId, 'status' => $response->getStatusCode()]);

        return $response;
    }
}
