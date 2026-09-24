<?php

namespace App\Support;

class AccountRules
{
    /** 僅裁切 Email 前後 ASCII 空格並轉小寫，保留其他空白以符合共同帳號契約。 */
    public static function normalizeEmail(string $email): string
    {
        return strtolower(trim($email, ' '));
    }

    /** 採跨語言共用的簡化 ASCII Email 規格，不套用各框架不同的 email 驗證器。 */
    public static function validEmail(string $email): bool
    {
        return strlen($email) <= 254 && preg_match('/\A(?=.{1,64}@)[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\z/D', $email) === 1;
    }

    /** 密碼只接受原始 4–16 碼 ASCII 英數，避免各後端 trim 或隱性轉型。 */
    public static function validPassword(string $password): bool
    {
        return preg_match('/\A[A-Za-z0-9]{4,16}\z/D', $password) === 1;
    }

    /** 暱稱依 Unicode 碼點計長，不可只含指定空白，也要拒絕 PostgreSQL text 無法儲存的 U+0000。 */
    public static function validNickname(string $nickname): bool
    {
        return ! str_contains($nickname, "\0")
            && mb_strlen($nickname, 'UTF-8') >= 1
            && mb_strlen($nickname, 'UTF-8') <= 30
            && preg_match('/[^\x{0009}-\x{000D}\x{0020}\x{0085}\x{00A0}\x{1680}\x{2000}-\x{200A}\x{2028}-\x{2029}\x{202F}\x{205F}\x{3000}]/u', $nickname) === 1;
    }

    /** 以 PHP 原生 Argon2id 的固定成本產生可供十二後端驗證的 PHC 字串。 */
    public static function hashPassword(string $password): string
    {
        return password_hash($password, PASSWORD_ARGON2ID, ['memory_cost' => 19456, 'time_cost' => 2, 'threads' => 1]);
    }
}
