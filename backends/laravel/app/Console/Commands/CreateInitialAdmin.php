<?php

namespace App\Console\Commands;

use App\Support\AccountRules;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use Throwable;

class CreateInitialAdmin extends Command
{
    protected $signature = 'app:create-initial-admin';

    protected $description = 'Read the initial administrator credentials from stdin without command-line arguments';

    /** 從 stdin 讀入首次互動設定的資料，避免密碼出現在 argv 或日誌。 */
    public function handle(): int
    {
        $email = AccountRules::normalizeEmail($this->readInputLine());
        $nickname = $this->readInputLine();
        $password = $this->readInputLine();
        if (
            ! AccountRules::validEmail($email) || ! AccountRules::validNickname($nickname)
            || ! AccountRules::validPassword($password)
        ) {
            $this->error('管理員 Email、暱稱或密碼不符合共同格式');

            return self::FAILURE;
        }

        try {
            // 鎖住首次管理員檢查；重跑只接受同一 admin，不重設密碼或提升會員。
            return DB::transaction(function () use ($email, $nickname, $password): int {
                DB::select('SELECT pg_advisory_xact_lock(1907202602)');
                $existing = DB::table('accounts')->where('email', $email)->first(['role']);
                if ($existing !== null) {
                    if ($existing->role !== 'admin') {
                        $this->error('此 Email 已屬於一般會員；未變更其角色');

                        return self::FAILURE;
                    }
                    $this->info('管理員已存在；密碼與資料保持原狀');

                    return self::SUCCESS;
                }
                if (DB::table('accounts')->where('role', 'admin')->exists()) {
                    $this->error('已有管理員；首次設定不建立其他管理員');

                    return self::FAILURE;
                }
                DB::table('accounts')->insert([
                    'email' => $email,
                    'nickname' => $nickname,
                    'password_hash' => AccountRules::hashPassword($password),
                    'role' => 'admin',
                    'status' => 'active',
                ]);
                $this->info('首次管理員已建立');

                return self::SUCCESS;
            });
        } catch (Throwable $exception) {
            $this->error('管理員建立失敗；請檢查資料庫狀態');

            return self::FAILURE;
        }
    }

    /** 每欄只去掉 stdin 傳輸的一個 LF，保留使用者輸入的 CR 供共同規則判斷。 */
    private function readInputLine(): string
    {
        $line = (string) fgets(STDIN);

        return str_ends_with($line, "\n") ? substr($line, 0, -1) : $line;
    }
}
