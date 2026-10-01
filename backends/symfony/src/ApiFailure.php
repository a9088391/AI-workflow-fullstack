<?php

namespace App;

use RuntimeException;

/** 保存共同錯誤碼與安全的欄位訊息，不把憑證或資料庫細節帶到回應。 */
class ApiFailure extends RuntimeException
{
    /** 將可預期的業務錯誤交給共同 envelope 轉換。 */
    public function __construct(public string $codeName, public ?array $errors = null, public ?int $retryAfter = null, public ?int $httpStatus = null)
    {
        parent::__construct($codeName);
    }
}
