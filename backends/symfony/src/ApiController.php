<?php

namespace App;

use JsonException;
use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排會員管理與鎖定設定；帳號、文章及留言由各自控制器負責。 */
class ApiController extends HttpController
{
    /** 管理員以資料庫完成會員字面搜尋、精確計數與穩定分頁，避免載入全部帳號。 */
    public function members(Request $request): JsonResponse
    {
        // 角色門禁先於 Query；先精確計數，有該頁資料才查列表，查詢次數不隨結果筆數增加。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $input = $this->listQuery($request, false, true);
            $keyword = $input['keyword'];
            $filter = " WHERE accounts.role = 'member'";
            $parameters = [];
            if ($keyword !== null && $keyword !== '') {
                // translate 只折疊 ASCII；strpos 將 %, _ 與反斜線視為字面內容。
                $filter .= " AND (strpos(translate(accounts.nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    . "'abcdefghijklmnopqrstuvwxyz'), :nickname) > 0 OR strpos(translate(accounts.email, "
                    . "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), :email) > 0)";
                $folded = strtr($keyword, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
                $parameters = ['nickname' => $folded, 'email' => $folded];
            }
            $count = $db->prepare('SELECT count(*) FROM accounts' . $filter);
            $count->execute($parameters);
            $total = (int) $count->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT accounts.id, accounts.email, accounts.nickname, accounts.role, '
                . 'accounts.status, accounts.created_at FROM accounts'
                . $filter . ' ORDER BY accounts.created_at DESC, accounts.id DESC',
                $input,
                $total,
                $parameters,
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->account($row);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 管理員只能讀一般會員；管理員 ID 與不存在的 ID 都以 404 隱藏。 */
    public function member(Request $request, string $member_id): JsonResponse
    {
        // 保留認證鎖直到回應形成；先驗角色，再處理參數及只含一般會員的目標。
        return $this->database->transaction(function (PDO $db) use ($request, $member_id): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $this->noGetBody($request);
            $this->noQuery($request);
            $id = $this->id($member_id, 'member_id');
            $query = $db->prepare(
                "SELECT id, email, nickname, role, status, created_at FROM accounts WHERE id = :id AND role = 'member'"
            );
            $query->execute(['id' => $id]);
            $member = $query->fetch();
            if ($member === false) {
                throw new ApiFailure('E0004');
            }

            return $this->success($this->account($member));
        });
    }

    /** 管理員停用一般會員時同步撤銷舊登入；同值與恢復不重做副作用。 */
    public function memberStatus(Request $request, string $member_id): JsonResponse
    {
        // 先短交易驗角色，讓無權者無法藉 Path、Body 或目標資料探測會員。
        $viewer = $this->database->transaction(function (PDO $db) use ($request): stdClass {
            $account = $this->authenticate($request, $db)[0];
            if ($account->role !== 'admin') {
                throw new ApiFailure('E0003');
            }

            return $account;
        });
        $this->noQuery($request);
        $id = $this->id($member_id, 'member_id');
        $body = $this->contentBody($request, ['status' => 'status']);

        // 帳號按 ID 鎖定，再重驗管理員 session；停用與撤銷不可分開提交。
        return $this->database->transaction(function (PDO $db) use ($request, $viewer, $id, $body): JsonResponse {
            $lock = $db->prepare(
                'SELECT id, role, status FROM accounts WHERE id IN (:viewer, :target) ORDER BY id FOR UPDATE'
            );
            $lock->execute(['viewer' => $viewer->id, 'target' => $id]);
            $accounts = [];
            foreach ($lock->fetchAll() as $account) {
                $accounts[(int) $account->id] = $account;
            }
            $current = $this->authenticate($request, $db)[0];
            if ($current->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $target = $accounts[$id] ?? null;
            if ($target === null || $target->role !== 'member') {
                throw new ApiFailure('E0004');
            }
            if ($target->status === $body->status) {
                return $this->success([]);
            }
            $update = $db->prepare('UPDATE accounts SET status = :status WHERE id = :id');
            $update->execute(['status' => $body->status, 'id' => $id]);
            if ($body->status === 'disabled') {
                $revoke = $db->prepare(
                    'UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE account_id = :id AND '
                    . 'revoked_at IS NULL'
                );
                $revoke->execute(['id' => $id]);
            }

            return $this->success([]);
        });
    }

    /** 管理員讀取共用鎖定設定，只公開四個核准欄位。 */
    public function loginLockoutSettings(Request $request): JsonResponse
    {
        // 先驗目前管理員身分，再回傳共用設定，缺少設定不能偽造預設值。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
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
        // 先驗角色及輸入；稍後設定列鎖可能等待，故寫入交易內仍須重新驗證登入。
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
            $values = $this->settingsValues($settings);
            $next = array_replace($values, (array) $body);
            if ($values === $next) {
                return $this->success([]);
            }
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
        return ['enabled' => (bool) $settings->enabled, 'window_minutes' => (int) $settings->window_minutes,
            'failure_threshold' => (int) $settings->failure_threshold, 'lock_seconds' => (int) $settings->lock_seconds];
    }

    /** 驗證設定四欄的原始 JSON 型態與範圍，維持已核准的逐欄錯誤順序。 */
    private function settingsBody(Request $request): stdClass
    {
        $raw = $request->getContent();
        if (
            $raw === ''
            || ! str_starts_with(strtolower($request->headers->get('Content-Type', '')), 'application/json')
        ) {
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
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $limits)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }
        $this->rejectErrors($errors);

        return $body;
    }
}
