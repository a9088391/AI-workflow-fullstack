<?php

namespace App;

/** 實作十二後端共用的帳號文字與 Argon2id 格式，不使用框架不同的預設規則。 */
class Rules
{
    /** 只移除 Email 前後 ASCII 空格並轉小寫，避免改寫其他有效字元。 */
    public static function email(string $value): string
    {
        return strtolower(trim($value, ' '));
    }

    /** 依已核准的簡化 ASCII Email 格式驗證，不做 DNS 查詢。 */
    public static function validEmail(string $value): bool
    {
        return strlen($value) <= 254 && preg_match('/\A(?=.{1,64}@)[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\z/D', $value) === 1;
    }

    /** 密碼保留原文並限制為 4–16 碼 ASCII 英數，格式錯誤不計鎖定失敗。 */
    public static function validPassword(string $value): bool
    {
        return preg_match('/\A[A-Za-z0-9]{4,16}\z/D', $value) === 1;
    }

    /** 暱稱按 Unicode 碼點與固定空白集合驗證，並拒絕 PostgreSQL 無法儲存的 NUL。 */
    public static function validNickname(string $value): bool
    {
        return ! str_contains($value, "\0") && mb_strlen($value, 'UTF-8') >= 1 && mb_strlen($value, 'UTF-8') <= 30
            && preg_match('/[^\x{0009}-\x{000D}\x{0020}\x{0085}\x{00A0}\x{1680}\x{2000}-\x{200A}\x{2028}-\x{2029}\x{202F}\x{205F}\x{3000}]/u', $value) === 1;
    }

    /** 產生與 Laravel 和其他後端互通的固定成本 Argon2id PHC 字串。 */
    public static function hash(string $password): string
    {
        return password_hash($password, PASSWORD_ARGON2ID, ['memory_cost' => 19456, 'time_cost' => 2, 'threads' => 1]);
    }

    /** 先驗儲存的 PHC 格式與成本，毀損時回設定錯誤且不計失敗。 */
    public static function verify(string $password, string $hash): bool
    {
        if (preg_match('/\A\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+\/]{22})\$([A-Za-z0-9+\/]{43})\z/D', $hash, $parts) !== 1) {
            throw new ApiFailure('E0104');
        }
        foreach ([1 => 16, 2 => 32] as $index => $length) {
            $bytes = base64_decode($parts[$index], true);
            if ($bytes === false || strlen($bytes) !== $length || rtrim(base64_encode($bytes), '=') !== $parts[$index]) {
                throw new ApiFailure('E0104');
            }
        }

        return password_verify($password, $hash);
    }
}
