<?php

namespace App;

use JsonException;
use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排共用登入鎖定設定的讀寫；完整提交與清鎖副作用維持在同一交易。 */
class LoginLockoutSettingsController extends HttpController
{
    /** 管理員讀取共用鎖定設定，只公開四個核准欄位。 */
    public function loginLockoutSettings(Request $request): JsonResponse
    {
        // 先驗目前管理員身分，再回傳共用設定，缺少設定不能偽造預設值。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }

            // 通過角色門禁才驗輸入與讀設定；缺少共用設定時回安全系統錯誤。
            $this->noGetBody($request);
            $this->noQuery($request);
            $settings = $db->query('SELECT * FROM login_lockout_settings WHERE id = 1')->fetch();
            if ($settings === false) {
                throw new ApiFailure('E0104');
            }

            return $this->success($this->settingsValues($settings));
        });
    }

    /** 完整覆寫鎖定設定；只有真正關閉才在同一交易解除會員鎖定並換計數世代。 */
    public function updateLoginLockoutSettings(Request $request): JsonResponse
    {
        // 先短交易驗角色，避免無權者探測輸入錯誤；等鎖後的寫入交易仍須重驗登入。
        $this->database->transaction(function (PDO $db) use ($request): void {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
        });
        $this->noQuery($request);
        $body = $this->settingsBody($request);

        // 設定列先取鎖再重驗目前身分；設定與必要的清鎖副作用必須一起提交。
        return $this->database->transaction(function (PDO $db) use ($request, $body): JsonResponse {
            $settings = $db->query('SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE')->fetch();
            if ($settings === false) {
                throw new ApiFailure('E0104');
            }
            $disabling = (bool) $settings->enabled && ! $body->enabled;
            if ($disabling) {
                // 設定→帳號→session 的共同鎖序與登入一致，避免清鎖與登入計數交錯。
                $db->query('SELECT id FROM accounts ORDER BY id FOR UPDATE')->fetchAll();
            }
            $current = $this->authenticate($request, $db)[0];
            if ($current->role !== 'admin') {
                throw new ApiFailure('E0003');
            }

            // 以鎖後的完整四欄比對，同值不更新也不重做清鎖副作用。
            $values = $this->settingsValues($settings);
            $next = array_replace($values, (array) $body);
            if ($values === $next) {
                return $this->success([]);
            }

            // 四欄一起提交；只修改數值不會重算既有 locked_until。
            $update = $db->prepare(
                'UPDATE login_lockout_settings SET enabled = :enabled, window_minutes = :window, '
                . 'failure_threshold = :threshold, lock_seconds = :seconds WHERE id = 1'
            );
            $update->bindValue(':enabled', $body->enabled, PDO::PARAM_BOOL);
            $update->bindValue(':window', $body->window_minutes, PDO::PARAM_INT);
            $update->bindValue(':threshold', $body->failure_threshold, PDO::PARAM_INT);
            $update->bindValue(':seconds', $body->lock_seconds, PDO::PARAM_INT);
            $update->execute();
            if ($disabling) {
                // 真正關閉才解除一般會員的暫時鎖定並換世代，不改停用狀態或撤銷登入。
                $db->exec(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 WHERE role = 'member'"
                );
            }

            return $this->success([]);
        });
    }

    /** 將 PostgreSQL 回傳的字串整數與布林值轉為共同 JSON 型態。 */
    private function settingsValues(stdClass $settings): array
    {
        return [
            'enabled' => (bool) $settings->enabled,
            'window_minutes' => (int) $settings->window_minutes,
            'failure_threshold' => (int) $settings->failure_threshold,
            'lock_seconds' => (int) $settings->lock_seconds
        ];
    }

    /** 驗證設定四欄的原始 JSON 型態與範圍，維持已核准的逐欄錯誤順序。 */
    private function settingsBody(Request $request): stdClass
    {
        // 先區分必要 JSON object、語法錯誤與根型態，避免框架自動轉型改變共同錯誤。
        $raw = $request->getContent();
        if ($raw === '' || ! $this->hasJsonContentType($request)) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiFailure('E0005');
        }
        if (! is_object($body)) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }

        // 四欄皆必填，即使關閉仍驗數值；每欄固定先必填、null、型態，再驗範圍。
        $limits = ['enabled' => null, 'window_minutes' => 60, 'failure_threshold' => 100, 'lock_seconds' => 86400];
        $errors = [];
        foreach ($limits as $name => $max) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif ($max === null ? ! is_bool($body->$name) : ! is_int($body->$name)) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif ($max !== null && ($body->$name < 1 || $body->$name > $max)) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }

        // 未知欄位最後彙整，與已知欄位錯誤一起回報，防止不完整或額外設定被默收。
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $limits)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }
        $this->rejectErrors($errors);

        return $body;
    }
}
