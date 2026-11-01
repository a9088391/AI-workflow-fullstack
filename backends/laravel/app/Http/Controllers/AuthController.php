<?php

namespace App\Http\Controllers;

use App\Exceptions\AuthFailure;
use App\Http\ApiResponse;
use App\Support\AccountRules;
use App\Support\AuthenticatedAccount;
use App\Support\JwtTokens;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use JsonException;
use stdClass;

class AuthController extends Controller
{
    /** 驗證原始帳密後序列化登入判定；業務錯誤以回應返回，使應保存的失敗事件正常提交。 */
    public function login(Request $request): JsonResponse
    {
        $body = $this->input($request, ['email', 'password']);
        if ($body instanceof JsonResponse) {
            return $body;
        }

        // shortcut: 核准的設定單列鎖限制登入吞吐量；高流量時再評估按帳號細分。
        // 交易只回傳鎖定到期時間；提交後才依新時間產生 Retry-After，避免回報已流逝秒數。
        $outcome = DB::transaction(function () use ($body): JsonResponse|CarbonImmutable {
            // 固定設定→帳號鎖序，等候完成後才取資料庫時間判定停用與鎖定。
            $settings = DB::table('login_lockout_settings')->where('id', 1)->lockForUpdate()->first();
            if ($settings === null) {
                throw new AuthFailure('E0104');
            }
            $account = DB::table('accounts')->where('email', $body->email)->lockForUpdate()->first();
            $now = $this->now();
            if ($account === null) {
                return (new AuthFailure('E0101'))->response();
            }
            if ($account->status === 'disabled') {
                return (new AuthFailure('E0102'))->response();
            }

            // 停用優先；暫時鎖定只影響一般會員新登入，不碰既有登入身分。
            if ($account->role === 'member' && $account->locked_until !== null) {
                $until = CarbonImmutable::parse($account->locked_until);
                if ($settings->enabled && $now->lessThan($until)) {
                    return $until;
                }
                $account->failure_epoch++;
                DB::table('accounts')->where('id', $account->id)->update([
                    'failure_epoch' => $account->failure_epoch, 'locked_until' => null
                ]);
            }

            // 儲存格式異常拋安全設定錯誤，不能當成錯密碼累計。
            if (! AccountRules::verifyPassword($body->password, $account->password_hash)) {
                if ($settings->enabled && $account->role === 'member') {
                    DB::table('login_failures')->insert([
                        'account_id' => $account->id,
                        'failure_epoch' => $account->failure_epoch,
                        'occurred_at' => $this->timestamp($now)
                    ]);
                    $count = DB::table('login_failures')
                        ->where('account_id', $account->id)->where('failure_epoch', $account->failure_epoch)
                        ->where('occurred_at', '>', $this->timestamp($now->subMinutes($settings->window_minutes)))
                        ->where('occurred_at', '<=', $this->timestamp($now))->count();
                    if ($count >= $settings->failure_threshold) {
                        $until = $now->addSeconds($settings->lock_seconds);
                        DB::table('accounts')->where('id', $account->id)->update([
                            'locked_until' => $this->timestamp($until)
                        ]);

                        return $until;
                    }
                }

                return (new AuthFailure('E0101'))->response();
            }

            // JWT 使用同一資料庫時間的整數秒；簽章、計數重設與 session 建立失敗都回滾。
            $issued = $now->startOfSecond();
            $expires = $issued->addHours(8);
            $jti = JwtTokens::encode(random_bytes(16));
            $token = JwtTokens::issue((int) $account->id, $jti, $issued->getTimestamp());
            DB::table('accounts')->where('id', $account->id)->update([
                'failure_epoch' => $account->failure_epoch + 1, 'locked_until' => null
            ]);
            DB::table('login_sessions')->insert([
                'jti' => $jti,
                'account_id' => $account->id,
                'backend_id' => 'laravel',
                'issued_at' => $this->timestamp($issued),
                'expires_at' => $this->timestamp($expires)
            ]);

            return $this->success([
                'access_token' => $token,
                'token_type' => 'Bearer',
                'expires_at' => $this->displayTime($expires),
                'user' => $this->account($account)
            ]);
        });

        return $outcome instanceof CarbonImmutable ? $this->locked($outcome) : $outcome;
    }

    /** 查詢目前身分但不續期；與登出共用取得鎖後的認證順序。 */
    public function me(Request $request): JsonResponse
    {
        return $this->protectedRequest($request, false);
    }

    /** 只撤銷本次 jti；撤銷與再次認證同一交易，失敗不宣稱已登出。 */
    public function logout(Request $request): JsonResponse
    {
        return $this->protectedRequest($request, true);
    }

    /** 先認證再驗參數，依帳號→登入紀錄順序鎖定，避免與重設密碼／停用競爭。 */
    private function protectedRequest(Request $request, bool $logout): JsonResponse
    {
        // 共用認證於同一交易中檢查簽章、帳號與 session，維持既有鎖定順序。
        return DB::transaction(function () use ($request, $logout): JsonResponse {
            [$account, $jti] = AuthenticatedAccount::resolve($request);
            $input = $this->input($request, []);
            if ($input instanceof JsonResponse) {
                return $input;
            }
            if ($logout) {
                DB::table('login_sessions')->where('jti', $jti)->update([
                    'revoked_at' => $this->timestamp($this->now())
                ]);
            }

            return $this->success($logout ? [] : $this->account($account));
        });
    }

    /** 從原始 Query／JSON 驗證共同格式，不使用框架已 trim 或覆蓋的輸入。 */
    private function input(Request $request, array $fields): stdClass|JsonResponse
    {
        // 受保護操作由呼叫端先認證；非空 GET Body 須先於 Query 錯誤拒絕，維持共同驗證順序。
        $raw = $request->getContent();
        if ($request->isMethod('GET') && $raw !== '') {
            return $this->invalid(['_body' => ['此 API 不接受 Request Body']]);
        }

        // 這些端點不接受 Query；逐一辨認原始鍵，避免重複參數被框架覆蓋。
        $query = $request->server('QUERY_STRING', '');
        if ($query !== '') {
            $seen = [];
            $errors = [];
            foreach (explode('&', $query) as $pair) {
                $rawName = urldecode(explode('=', $pair, 2)[0]);
                // 原始位元組辨認參數身分；百分號表示只供 JSON 錯誤鍵，不能合併不同名稱。
                $errorName = mb_check_encoding($rawName, 'UTF-8') ? $rawName : rawurlencode($rawName);
                if (isset($seen[$rawName])) {
                    $errors[$errorName] = ['此參數不可重複'];
                } else {
                    // 不同名稱可能共用顯示鍵，後來的未知欄位不可覆蓋已確定的重複錯誤。
                    $errors[$errorName] ??= ['不支援此欄位'];
                }
                $seen[$rawName] = true;
            }

            return $this->invalid($errors);
        }
        // GET 到此只會有空 Body；登出容許無 Body，其餘寫入仍需 JSON object。
        if ($request->isMethod('GET')) {
            return new stdClass();
        }
        if ($raw === '' && $fields === []) {
            return new stdClass();
        }
        if ($raw === '' || ! str_starts_with(strtolower($request->header('Content-Type', '')), 'application/json')) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            return ApiResponse::error(400, 'E0005', '系統暫時無法處理，請稍後再試');
        }
        if (! is_object($body)) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }

        // 各欄只回第一個錯誤，必填/null、型態、格式依序判斷；登入不接受暱稱或角色。
        $errors = [];
        foreach ($fields as $field) {
            if (! property_exists($body, $field)) {
                $errors[$field] = ['此欄位為必填'];
            } elseif ($body->$field === null) {
                $errors[$field] = ['此欄位不可為 null'];
            } elseif (! is_string($body->$field)) {
                $errors[$field] = ['欄位型態不正確'];
            } else {
                if ($field === 'email') {
                    $body->email = AccountRules::normalizeEmail($body->email);
                }
                if (
                    $field === 'email'
                        ? ! AccountRules::validEmail($body->$field)
                        : ! AccountRules::validPassword($body->$field)
                ) {
                    $errors[$field] = ['欄位格式或範圍不正確'];
                }
            }
        }
        foreach ($body as $field => $_) {
            if (! in_array($field, $fields, true)) {
                $errors[$field] = ['不支援此欄位'];
            }
        }

        return $errors === [] ? $body : $this->invalid($errors);
    }

    /** 取得所有必要鎖之後才讀實際時間，避免等待鎖期間使用已過時的交易開始時間。 */
    private function now(): CarbonImmutable
    {
        return CarbonImmutable::parse(DB::selectOne('SELECT clock_timestamp() AS now')->now);
    }

    /** SQL 綁定保留微秒與時區，維持滑動視窗左開右閉的精確邊界。 */
    private function timestamp(CarbonImmutable $time): string
    {
        return $time->format('Y-m-d H:i:s.uP');
    }

    /** 對外業務時間固定顯示台北秒級字串，JWT 仍保留 Unix 整數秒。 */
    private function displayTime(CarbonImmutable $time): string
    {
        return $time->setTimezone('Asia/Taipei')->format('Y-m-d H:i:s');
    }

    /** 只輸出共同 Account 欄位，避免 password_hash 或鎖定內部欄位洩漏。 */
    private function account(stdClass $account): array
    {
        return [
            'id' => (int) $account->id,
            'email' => $account->email,
            'nickname' => $account->nickname,
            'role' => $account->role,
            'status' => $account->status,
            'created_at' => $this->displayTime(CarbonImmutable::parse($account->created_at))
        ];
    }

    /** 交易提交後取資料庫新時間計算剩餘秒數，避免將寫入與等待耗時加回倒數。 */
    private function locked(CarbonImmutable $until): JsonResponse
    {
        $remaining = max(1, (int) ceil($this->now()->diffInSeconds($until)));

        return (new AuthFailure('E0105'))->response()->header('Retry-After', (string) $remaining);
    }

    /** 維持所有驗證錯誤的固定文案與欄位陣列。 */
    private function invalid(array $errors): JsonResponse
    {
        return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', $errors);
    }

    /** 成功資料一律 object，登出空物件也不轉成 JSON 陣列。 */
    private function success(array $data): JsonResponse
    {
        return response()->json(['success' => true, 'message' => 'OK', 'data' => (object) $data]);
    }
}
