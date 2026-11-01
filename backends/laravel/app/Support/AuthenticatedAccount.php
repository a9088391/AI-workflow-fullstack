<?php

namespace App\Support;

use App\Exceptions\AuthFailure;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

class AuthenticatedAccount
{
    /** 在呼叫者交易內驗證目前帳號與登入紀錄；公開讀取未帶憑證時才視為訪客。 */
    public static function resolve(Request $request, bool $optional = false): ?array
    {
        // 只有未提供 Header 的公開讀取能略過認證；無效憑證不能降為訪客。
        if ($optional && ! $request->headers->has('Authorization')) {
            return null;
        }
        $authorization = $request->header('Authorization', '');
        if (preg_match('/\ABearer ([^\s]+)\z/Di', $authorization, $match) !== 1) {
            throw new AuthFailure();
        }
        $claims = JwtTokens::verify($match[1]);

        // 鎖定順序必須維持帳號→session，且等鎖後才讀資料庫時間。
        $account = DB::table('accounts')->where('id', $claims->sub)->lockForUpdate()->first();
        $session = DB::table('login_sessions')->where('jti', $claims->jti)->lockForUpdate()->first();
        $now = CarbonImmutable::parse(DB::selectOne('SELECT clock_timestamp() AS now')->now);
        if (
            $now->getTimestamp() < $claims->nbf || $now->getTimestamp() >= $claims->exp
            || $account === null || $session === null
            || (string) $session->account_id !== $claims->sub || $session->backend_id !== 'laravel'
            || CarbonImmutable::parse($session->issued_at)->getTimestamp() !== $claims->iat
            || CarbonImmutable::parse($session->expires_at)->getTimestamp() !== $claims->exp
        ) {
            throw new AuthFailure();
        }
        // 身分與期限確定相符後，停用優先於撤銷；角色一律沿用資料庫目前值。
        if ($account->status === 'disabled') {
            throw new AuthFailure('E0102');
        }
        if ($session->revoked_at !== null) {
            throw new AuthFailure();
        }

        return [$account, $claims->jti];
    }
}
