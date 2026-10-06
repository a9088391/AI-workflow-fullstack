<?php

namespace App\Http\Controllers;

use App\Exceptions\AuthFailure;
use App\Support\AuthenticatedAccount;
use App\Support\ContentContract;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use JsonException;
use stdClass;

class LoginLockoutSettingsController extends Controller
{
    use ContentContract;

    /** 管理員讀取共用鎖定設定；先驗目前登入與角色，避免參數錯誤洩漏管理端點資訊。 */
    public function show(Request $request): JsonResponse
    {
        // 讀取期間持有帳號與 session 鎖，讓驗權與設定回應基於同一有效登入。
        return DB::transaction(function () use ($request): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            if ($viewer->role !== 'admin') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $settings = DB::table('login_lockout_settings')->where('id', 1)->first();
            if ($settings === null) {
                throw new AuthFailure('E0104');
            }

            return $this->success($this->values($settings));
        });
    }

    /** 管理員完整覆寫四欄；先短暫驗權，再依設定→帳號→session 的鎖序重驗及寫入。 */
    public function update(Request $request): JsonResponse
    {
        // 短交易先驗身分與角色，再檢查輸入；寫入交易稍後仍須重新驗權。
        $viewer = DB::transaction(function () use ($request): stdClass|JsonResponse {
            $account = AuthenticatedAccount::resolve($request)[0];

            return $account->role === 'admin' ? $account : $this->error(403, 'E0003', '沒有權限使用此功能');
        });
        if ($viewer instanceof JsonResponse) {
            return $viewer;
        }
        $query = $this->query($request, false, false);
        if ($query instanceof JsonResponse) {
            return $query;
        }
        $body = $this->settingsBody($request);
        if ($body instanceof JsonResponse) {
            return $body;
        }

        // 先鎖設定，關閉時依 ID 鎖帳號，再重驗 session；設定與清鎖一同提交。
        return DB::transaction(function () use ($request, $body): JsonResponse {
            $settings = DB::table('login_lockout_settings')->where('id', 1)->lockForUpdate()->first();
            if ($settings === null) {
                throw new AuthFailure('E0104');
            }
            $disabling = (bool) $settings->enabled && ! $body->enabled;
            // 關閉時一次鎖住所有帳號並依 ID 排序，再重驗管理員 session；避免與登入、會員停用交錯。
            if ($disabling) {
                DB::table('accounts')->orderBy('id')->lockForUpdate()->get(['id']);
            }
            $current = AuthenticatedAccount::resolve($request)[0];
            if ($current->role !== 'admin') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $values = $this->values($settings);
            $next = array_replace($values, (array) $body);
            if ($values === $next) {
                return $this->success([]);
            }

            // 四欄同筆更新採最後提交者的完整值；只有 true→false 才清鎖並切換失敗計數世代。
            DB::table('login_lockout_settings')->where('id', 1)->update($next);
            if ($disabling) {
                DB::table('accounts')->where('role', 'member')->update([
                    'locked_until' => null,
                    'failure_epoch' => DB::raw('failure_epoch + 1'),
                ]);
            }

            return $this->success([]);
        });
    }

    /** 將資料庫型態固定成公開 JSON 型態，避免 PDO 驅動的字串數字影響契約。 */
    private function values(stdClass $settings): array
    {
        return [
            'enabled' => (bool) $settings->enabled,
            'window_minutes' => (int) $settings->window_minutes,
            'failure_threshold' => (int) $settings->failure_threshold,
            'lock_seconds' => (int) $settings->lock_seconds,
        ];
    }

    /** 保留原始 JSON 型態並逐欄驗必填、null、型態、範圍及未知欄位。 */
    private function settingsBody(Request $request): stdClass|JsonResponse
    {
        $raw = $request->getContent();
        if ($raw === '' || ! str_starts_with(strtolower($request->header('Content-Type', '')), 'application/json')) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            return $this->error(400, 'E0005', '系統暫時無法處理，請稍後再試');
        }
        if (! is_object($body)) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }

        // 整數不可由字串或 boolean 轉型；每欄只輸出第一項精確錯誤文案。
        $limits = ['enabled' => null, 'window_minutes' => 60, 'failure_threshold' => 100, 'lock_seconds' => 86400];
        $errors = [];
        foreach ($limits as $name => $max) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif ($max === null && ! is_bool($body->$name)) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif ($max !== null && ! is_int($body->$name)) {
                // 原生解碼會把超大 JSON 整數轉成 float；重解碼只將該整數判為範圍錯誤，小數與指數仍屬型態錯誤。
                $bigIntegerBody = is_float($body->$name)
                    ? json_decode($raw, false, 512, JSON_THROW_ON_ERROR | JSON_BIGINT_AS_STRING)
                    : null;
                $errors[$name] = [is_string($bigIntegerBody->$name ?? null)
                    ? '欄位格式或範圍不正確' : '欄位型態不正確'];
            } elseif ($max !== null && ($body->$name < 1 || $max < $body->$name)) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $limits)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }

        return $errors === [] ? $body : $this->invalid($errors);
    }
}
