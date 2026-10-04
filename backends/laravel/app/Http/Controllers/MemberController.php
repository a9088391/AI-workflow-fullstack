<?php

namespace App\Http\Controllers;

use App\Support\AuthenticatedAccount;
use App\Support\ContentContract;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use stdClass;

class MemberController extends Controller
{
    use ContentContract;

    /** 管理員搜尋一般會員；權限先於輸入檢查，篩選、計數及分頁都在資料庫完成。 */
    public function index(Request $request): JsonResponse
    {
        // 同一交易內先確認當前角色，再讀取列表，避免無權者藉驗證結果探測管理資料。
        return DB::transaction(function () use ($request): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            if ($viewer->role !== 'admin') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $input = $this->query($request, true, 'keyword');
            if ($input instanceof JsonResponse) {
                return $input;
            }
            // PostgreSQL text 無法含 NUL；字面搜尋不可能命中，仍保留合法頁碼的空結果。
            if ($input['keyword'] !== null && str_contains($input['keyword'], "\0")) {
                return $this->success(['list' => [], 'pagination' => [
                    'page' => $input['page'], 'per_page' => $input['per_page'], 'total' => 0, 'total_pages' => 0,
                ]]);
            }

            // translate 只折疊 ASCII；strpos 將 %, _ 和反斜線當一般字元，不受 LIKE 模式影響。
            $accounts = DB::table('accounts')->where('role', 'member');
            if ($input['keyword'] !== null && $input['keyword'] !== '') {
                $folded = strtr($input['keyword'], 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
                $accounts->whereRaw(
                    "(strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), ?) > 0 "
                    . "OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), ?) > 0)",
                    [$folded, $folded]
                );
            }
            $total = $accounts->count();
            $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1
                ? null
                : ($input['page'] - 1) * $input['per_page'];
            $rows = $offset === null || $offset >= $total
                ? []
                : $accounts->orderByDesc('created_at')->orderByDesc('id')
                    ->offset($offset)->limit($input['per_page'])
                    ->get(['id', 'email', 'nickname', 'role', 'status', 'created_at']);
            $list = [];
            foreach ($rows as $account) {
                $list[] = $this->resource($account);
            }

            return $this->success([
                'list' => $list,
                'pagination' => [
                    'page' => $input['page'],
                    'per_page' => $input['per_page'],
                    'total' => $total,
                    'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0),
                ],
            ]);
        });
    }

    /** 管理員只能讀取一般會員詳情；管理員帳號與不存在的 ID 一律隱藏為 404。 */
    public function show(Request $request, string $member_id): JsonResponse
    {
        // 詳情與列表使用相同管理員門禁，格式不合法時不得先查目標存在性。
        return DB::transaction(function () use ($request, $member_id): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            if ($viewer->role !== 'admin') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $id = $this->integer($member_id);
            if ($id === null) {
                return $this->invalid(['member_id' => ['欄位格式或範圍不正確']]);
            }
            $account = DB::table('accounts')
                ->where('id', $id)->where('role', 'member')
                ->first(['id', 'email', 'nickname', 'role', 'status', 'created_at']);
            if ($account === null) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }

            return $this->success($this->resource($account));
        });
    }

    /** 管理員切換一般會員狀態；實際停用與所有舊登入撤銷在同一交易完成。 */
    public function status(Request $request, string $member_id): JsonResponse
    {
        // 先在短交易確認登入與角色，讓非管理員無法藉欄位或目標錯誤探測資料。
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
        $id = $this->integer($member_id);
        if ($id === null) {
            return $this->invalid(['member_id' => ['欄位格式或範圍不正確']]);
        }
        $body = $this->body($request, ['status' => 'status']);
        if ($body instanceof JsonResponse) {
            return $body;
        }

        // 預驗可能已過期；寫入時先按 ID 鎖全部帳號，再重驗管理員與 session，維持帳號→登入紀錄順序。
        return DB::transaction(function () use ($request, $viewer, $id, $body): JsonResponse {
            $accounts = DB::table('accounts')
                ->whereIn('id', [$viewer->id, $id])->orderBy('id')
                ->lockForUpdate()->get()->keyBy('id');
            $current = AuthenticatedAccount::resolve($request)[0];
            if ($current->role !== 'admin') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $target = $accounts->get($id);
            if ($target === null || $target->role !== 'member') {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }
            if ($target->status === $body->status) {
                return $this->success([]);
            }

            // 同值沒有副作用；真正停用才撤銷全部登入，恢復不清除鎖定或舊撤銷紀錄。
            DB::table('accounts')->where('id', $id)->update(['status' => $body->status]);
            if ($body->status === 'disabled') {
                DB::table('login_sessions')
                    ->where('account_id', $id)->whereNull('revoked_at')
                    ->update(['revoked_at' => DB::raw('clock_timestamp()')]);
            }

            return $this->success([]);
        });
    }

    /** 只輸出核准的會員欄位，避免密碼雜湊、登入鎖定與歷史外洩。 */
    private function resource(stdClass $account): array
    {
        return [
            'id' => (int) $account->id,
            'email' => $account->email,
            'nickname' => $account->nickname,
            'role' => $account->role,
            'status' => $account->status,
            'created_at' => $this->displayTime($account->created_at),
        ];
    }
}
