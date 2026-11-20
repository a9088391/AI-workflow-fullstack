<?php

namespace App;

use JsonException;
use stdClass;

/** 每個 Symfony 登入使用獨立固定金鑰與 issuer/audience，不能接受其他後端 JWT。 */
class Jwt
{
    private const ISSUER = 'ai-workflow-fullstack:symfony';
    private const AUDIENCE = 'ai-workflow-fullstack-api:symfony';

    /** 將 bytes 編成無 padding base64url，JWT 區段與隨機 jti 均使用同規則。 */
    public static function encode(string $bytes): string
    {
        return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
    }

    /** 拒絕非正規編碼，避免同一簽章或 jti 出現不同字串表示。 */
    private static function decode(string $text): string
    {
        $bytes = base64_decode(strtr($text, '-_', '+/'), true);
        if ($text === '' || $bytes === false || self::encode($bytes) !== $text) {
            throw new ApiFailure('E0002');
        }

        return $bytes;
    }

    /** 只讀首次設定保存的 Symfony 專屬 32-byte 金鑰；缺損絕不自動重生。 */
    private static function key(): string
    {
        $raw = @file_get_contents('/run/secrets/symfony-jwt.key');
        $encoded = $raw === false ? '' : rtrim($raw, "\n");
        $key = base64_decode($encoded, true);
        if ($key === false || strlen($key) !== 32 || base64_encode($key) !== $encoded) {
            throw new ApiFailure('E0104');
        }

        return $key;
    }

    /** 使用固定 HS256 與資料庫簽發秒數建立八小時 JWT，不將角色寫入 token。 */
    public static function issue(int $id, string $jti, int $issued): string
    {
        $header = self::encode('{"alg":"HS256","typ":"JWT"}');
        $claims = self::encode(json_encode([
            'iss' => self::ISSUER,
            'aud' => self::AUDIENCE,
            'sub' => (string) $id,
            'jti' => $jti,
            'iat' => $issued,
            'nbf' => $issued,
            'exp' => $issued + 28800
        ], JSON_THROW_ON_ERROR));
        $signed = $header . '.' . $claims;

        return $signed . '.' . self::encode(hash_hmac('sha256', $signed, self::key(), true));
    }

    /** 驗簽並嚴格核對固定 header、claims 及時間關係；當前狀態由資料庫另行查核。 */
    public static function verify(string $token): stdClass
    {
        $key = self::key();
        $parts = explode('.', $token);
        if (
            count($parts) !== 3
            || ! hash_equals(hash_hmac('sha256', $parts[0] . '.' . $parts[1], $key, true), self::decode($parts[2]))
        ) {
            throw new ApiFailure('E0002');
        }
        try {
            $header = json_decode(self::decode($parts[0]), false, 32, JSON_THROW_ON_ERROR);
            $claims = json_decode(self::decode($parts[1]), false, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiFailure('E0002');
        }
        if (
            ! is_object($header) || count(get_object_vars($header)) !== 2
            || ($header->alg ?? null) !== 'HS256' || ($header->typ ?? null) !== 'JWT' || ! is_object($claims)
        ) {
            throw new ApiFailure('E0002');
        }
        // 型態檢查先於算術與查庫；額外 claims 不授予額外權限。
        if (
            ($claims->iss ?? null) !== self::ISSUER || ($claims->aud ?? null) !== self::AUDIENCE
            || ! is_string($claims->sub ?? null) || preg_match('/\A[1-9][0-9]{0,15}\z/D', $claims->sub) !== 1
            || (int) $claims->sub > 9007199254740991
            || ! is_string($claims->jti ?? null) || strlen(self::decode($claims->jti)) !== 16
            || ! is_int($claims->iat ?? null) || $claims->iat < 0 || $claims->iat > PHP_INT_MAX - 28800
            || ! is_int($claims->nbf ?? null) || $claims->nbf !== $claims->iat
            || ! is_int($claims->exp ?? null) || $claims->exp !== $claims->iat + 28800
        ) {
            throw new ApiFailure('E0002');
        }

        return $claims;
    }
}
