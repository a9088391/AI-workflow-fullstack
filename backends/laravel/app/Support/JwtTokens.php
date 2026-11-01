<?php

namespace App\Support;

use App\Exceptions\AuthFailure;
use JsonException;
use stdClass;

class JwtTokens
{
    private const ISSUER = 'ai-workflow-fullstack:laravel';

    private const AUDIENCE = 'ai-workflow-fullstack-api:laravel';

    /** 將標準 bytes 編碼為無 padding base64url，供 JWT 區段和 jti 共用。 */
    public static function encode(string $bytes): string
    {
        return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
    }

    /** 拒絕 padding、非 URL 字元及非正規尾端 bits，避免同一 jti 有不同字串表示。 */
    private static function decode(string $text): string
    {
        $bytes = base64_decode(strtr($text, '-_', '+/'), true);
        if ($text === '' || $bytes === false || self::encode($bytes) !== $text) {
            throw new AuthFailure();
        }

        return $bytes;
    }

    /** 只讀首次設定產生的持久化金鑰；缺漏或損壞時不降級、不重新產生。 */
    private static function key(): string
    {
        $raw = @file_get_contents('/run/secrets/laravel-jwt.key');
        $encoded = $raw === false ? '' : rtrim($raw, "\n");
        $key = base64_decode($encoded, true);
        if ($key === false || strlen($key) !== 32 || base64_encode($key) !== $encoded) {
            throw new AuthFailure('E0104');
        }

        return $key;
    }

    /** 用原生 HMAC-SHA256 簽署固定 header/claims；密碼學運算交給 PHP，期限由登入交易給定。 */
    public static function issue(int $accountId, string $jti, int $issuedAt): string
    {
        $header = self::encode('{"alg":"HS256","typ":"JWT"}');
        $claims = self::encode(json_encode([
            'iss' => self::ISSUER,
            'aud' => self::AUDIENCE,
            'sub' => (string) $accountId,
            'jti' => $jti,
            'iat' => $issuedAt,
            'nbf' => $issuedAt,
            'exp' => $issuedAt + 28800
        ], JSON_THROW_ON_ERROR));
        $signed = $header . '.' . $claims;

        return $signed . '.' . self::encode(hash_hmac('sha256', $signed, self::key(), true));
    }

    /** 先驗簽與固定結構；時間及資料庫 session 對應由取得鎖後的單一資料庫時間判定。 */
    public static function verify(string $token): stdClass
    {
        // 只用自身持久金鑰驗簽，不依不受信任的 Header 選擇演算法或金鑰。
        $key = self::key();
        $parts = explode('.', $token);
        if (
            count($parts) !== 3
            || ! hash_equals(hash_hmac('sha256', $parts[0] . '.' . $parts[1], $key, true), self::decode($parts[2]))
        ) {
            throw new AuthFailure();
        }
        // 通過驗簽才解析固定 Header 與 claims；解析失敗不回傳 token 內容。
        try {
            $header = json_decode(self::decode($parts[0]), false, 32, JSON_THROW_ON_ERROR);
            $claims = json_decode(self::decode($parts[1]), false, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new AuthFailure();
        }
        if (
            ! is_object($header) || count(get_object_vars($header)) !== 2
            || ($header->alg ?? null) !== 'HS256' || ($header->typ ?? null) !== 'JWT' || ! is_object($claims)
        ) {
            throw new AuthFailure();
        }
        // 型態檢查先於算術和 DB 查詢；角色永遠不從 token 取得。
        if (
            ($claims->iss ?? null) !== self::ISSUER || ($claims->aud ?? null) !== self::AUDIENCE
            || ! is_string($claims->sub ?? null) || preg_match('/\A[1-9][0-9]{0,15}\z/D', $claims->sub) !== 1
            || (int) $claims->sub > 9007199254740991
            || ! is_string($claims->jti ?? null) || strlen(self::decode($claims->jti)) !== 16
            || ! is_int($claims->iat ?? null) || $claims->iat < 0 || $claims->iat > PHP_INT_MAX - 28800
            || ! is_int($claims->nbf ?? null) || $claims->nbf !== $claims->iat
            || ! is_int($claims->exp ?? null) || $claims->exp !== $claims->iat + 28800
        ) {
            throw new AuthFailure();
        }

        return $claims;
    }
}
