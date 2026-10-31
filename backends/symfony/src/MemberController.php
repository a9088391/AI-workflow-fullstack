<?php

namespace App;

use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排一般會員的管理員查詢與狀態更新；共用認證及回應沿用 HTTP 基底。 */
class MemberController extends HttpController
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

            // 只搜尋一般會員，列表與 count 共用條件，讓總數與分頁範圍一致。
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

            // 先取精確總數再查本頁，頁碼超界時由共用分頁邏輯直接回空集合。
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

            // 對外只回共同 Account 欄位，避免洩漏登入及鎖定資料。
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

            // 通過角色門禁才驗輸入；查詢排除管理員，與不存在的 ID 回應相同。
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

            // 重驗目前權限後才辨識目標；同值更新不得重複撤銷登入。
            $target = $accounts[$id] ?? null;
            if ($target === null || $target->role !== 'member') {
                throw new ApiFailure('E0004');
            }
            if ($target->status === $body->status) {
                return $this->success([]);
            }

            // 僅實際停用才撤銷所有未撤銷登入；恢復帳號不恢復舊登入或清除暫時鎖定。
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
}
