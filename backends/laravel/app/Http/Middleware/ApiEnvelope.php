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

        $logId = (string) Str::uuid();
        $request->attributes->set('log_id', $logId);
        $logger = Log::build(['driver' => 'daily', 'path' => storage_path('logs/api.log'), 'days' => 14]);
        $logger->info('IN', ['log_id' => $logId, 'method' => $request->method(), 'path' => $request->path()]);

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

        $logger->info('EX', ['log_id' => $logId, 'status' => $response->getStatusCode()]);
        $logger->info('OUT', ['log_id' => $logId, 'status' => $response->getStatusCode()]);

        return $response;
    }
}
