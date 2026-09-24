<?php

namespace App\Console\Commands;

use App\Support\AccountRules;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use Throwable;

class ResetAdminPassword extends Command
{
    protected $signature = 'app:reset-admin-password';

    protected $description = 'Reset an existing administrator using email and password from stdin';

    /** 從兩行 stdin 讀既有管理員 Email／新密碼，不讓密碼進 argv；不建立帳號或提升角色。 */
    public function handle(): int
    {
        $emailLine = fgets(STDIN);
        $passwordLine = fgets(STDIN);
        if ($emailLine === false || $passwordLine === false || ! str_ends_with($emailLine, "\n") || ! str_ends_with($passwordLine, "\n")) {
            $this->error('請以兩行 LF 結尾的 stdin 提供 Email 與新密碼');

            return self::FAILURE;
        }
        $email = AccountRules::normalizeEmail(substr($emailLine, 0, -1));
        $password = substr($passwordLine, 0, -1);
        if (! AccountRules::validEmail($email) || ! AccountRules::validPassword($password)) {
            $this->error('管理員 Email 或密碼不符合共同格式');

            return self::FAILURE;
        }

        try {
            // 按帳號→登入紀錄順序鎖定；密碼變更與所有後端的舊登入撤銷必須一起提交。
            $result = DB::transaction(function () use ($email, $password): int {
                $account = DB::table('accounts')->where('email', $email)->lockForUpdate()->first();
                if ($account === null || $account->role !== 'admin') {
                    $this->error('找不到既有管理員；未新增帳號或變更角色');

                    return self::FAILURE;
                }
                DB::table('login_sessions')->where('account_id', $account->id)->orderBy('jti')->lockForUpdate()->get(['jti']);
                $now = DB::selectOne('SELECT clock_timestamp() AS now')->now;
                DB::table('accounts')->where('id', $account->id)->update(['password_hash' => AccountRules::hashPassword($password)]);
                DB::table('login_sessions')->where('account_id', $account->id)->whereNull('revoked_at')->update(['revoked_at' => $now]);

                return self::SUCCESS;
            });
            if ($result === self::SUCCESS) {
                $this->info('管理員密碼已重設，既有登入已撤銷');
            }

            return $result;
        } catch (Throwable) {
            $this->error('管理員密碼重設失敗；未完成變更，請檢查資料庫狀態');

            return self::FAILURE;
        }
    }
}
