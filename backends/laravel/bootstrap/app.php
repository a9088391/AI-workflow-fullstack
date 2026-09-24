<?php

use App\Http\ApiResponse;
use App\Http\Middleware\ApiEnvelope;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;
use Illuminate\Http\Request;
use Symfony\Component\HttpKernel\Exception\HttpExceptionInterface;

// 只註冊本票的 API 路由；其他操作留待各票加入。
return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        api: __DIR__.'/../routes/api.php',
        web: __DIR__.'/../routes/web.php',
    )
    // API 最外層加追蹤記錄，讓每次錯誤都能用 log_id 查回 IN／EX／OUT。
    ->withMiddleware(function (Middleware $middleware): void {
        // API 最外層統一追蹤、錯誤格式與日誌，避免框架預設回應洩漏細節。
        $middleware->prepend(ApiEnvelope::class);
    })
    // Laravel 可能在路由管線內先攔住例外，因此在官方 render seam 統一錯誤外觀。
    ->withExceptions(function (Exceptions $exceptions): void {
        $exceptions->report(
            // API 例外的原始訊息可能含 SQL 綁定值；只保留類別與 log_id。
            function (Throwable $exception): ?bool {
                if (! request()->is('api', 'api/*')) {
                    return null;
                }
                logger()->error('API exception', ['log_id' => request()->attributes->get('log_id'), 'exception' => $exception::class]);

                return false;
            },
        );
        $exceptions->shouldRenderJsonWhen(
            // API 無論 Accept 為何都用共同 JSON envelope。
            fn (Request $request) => $request->is('api', 'api/*') || $request->expectsJson(),
        );
        $exceptions->render(
            // 未知 API 路徑為共同 404；其他例外只給安全訊息。
            function (Throwable $exception, Request $request) {
                if (! $request->is('api', 'api/*')) {
                    return null;
                }

                if ($exception instanceof HttpExceptionInterface) {
                    return $exception->getStatusCode() === 404
                        ? ApiResponse::error(404, 'E0004', '找不到指定資料或 API')
                        : ApiResponse::error($exception->getStatusCode(), 'E0005', '系統暫時無法處理，請稍後再試');
                }

                return ApiResponse::error(500, 'E9999', '系統暫時無法處理，請稍後再試');
            },
        );
    })->create();
