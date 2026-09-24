<?php

namespace App\Http;

use Illuminate\Http\JsonResponse;

class ApiResponse
{
    /** 將驗證與系統錯誤包成跨後端一致的格式，供前端依 error_code 處理。 */
    public static function error(int $status, string $code, string $message, ?array $errors = null): JsonResponse
    {
        $body = [
            'success' => false,
            'message' => $message,
            'error_code' => $code,
        ];
        if ($errors !== null) {
            $body['errors'] = (object) $errors;
        }
        $body['log_id'] = request()->attributes->get('log_id');

        return response()->json($body, $status);
    }
}
