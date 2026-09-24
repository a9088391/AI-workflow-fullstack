<?php

namespace App\Exceptions;

use App\Http\ApiResponse;
use Illuminate\Http\JsonResponse;
use RuntimeException;

class AuthFailure extends RuntimeException
{
    /** 只保存安全的共同錯誤碼，避免認證例外攜帶 token、金鑰或密碼。 */
    public function __construct(public readonly string $errorCode = 'E0002')
    {
        parent::__construct($errorCode);
    }

    /** 統一認證錯誤文案；系統／資料庫例外仍交給既有安全 500 管線。 */
    public function response(): JsonResponse
    {
        [$status, $message] = match ($this->errorCode) {
            'E0101' => [401, '帳號或密碼錯誤'],
            'E0102' => [403, '此帳號已停用，請聯絡系統管理員'],
            'E0104' => [500, '後台認證設定異常，請聯絡系統管理員'],
            'E0105' => [429, '登入失敗次數過多，請稍後再試'],
            default => [401, '登入已逾期，請重新登入'],
        };

        return ApiResponse::error($status, $this->errorCode, $message);
    }
}
