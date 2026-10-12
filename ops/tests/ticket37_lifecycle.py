"""驗證 Ticket 37 隔離專案正常停止／重啟時保留資料、設定、金鑰與登入期限。"""

import os
import secrets
import subprocess
from pathlib import Path

from ticket37_http import FASTIFY, call, check, expect, guard, register, sql


# main 先驗證實際隔離服務，再做本票唯一帳號的停止／重啟保留檢查。
def main():
    guard()
    email, login = register('lifecycle-' + secrets.token_hex(5))
    token = login['access_token']
    expires = login['expires_at']
    account_id = login['user']['id']
    settings = sql('SELECT enabled::text ||\'|\'||window_minutes||\'|\'||failure_threshold||\'|\'||lock_seconds FROM login_lockout_settings WHERE id=1')
    key_paths = [Path('.local/ticket37-backend/laravel.key'), Path('.local/ticket37-backend/fastify.key')]
    keys = [path.read_bytes() for path in key_paths]
    check(expect(call(FASTIFY, 'GET', '/api/me', token=token), 200)['id'] == account_id, '停止前登入無效')

    # 停止入口只作用於 guard 已核對的 project；資料卷及金鑰必須保持原樣。
    subprocess.check_call(['ops/stop.sh'])
    for service in ('db', 'laravel', 'fastify'):
        running = subprocess.check_output(['docker', 'compose', '--profile', 'fastify', 'ps', '-q', service], text=True).strip()
        check(not running, f'{service} 停止後仍在執行')
    volumes = subprocess.check_output(['docker', 'volume', 'ls', '--filter', 'name=ticket37_test_postgres_data', '--format', '{{.Name}}'], text=True).splitlines()
    check(volumes == ['ticket37_test_postgres_data'], '停止後資料卷遺失')
    check([path.read_bytes() for path in key_paths] == keys, '停止入口變更金鑰')

    # 正常啟動後重新做 guard，確保同一資料卷與容器身分，登入不續期。
    subprocess.check_call(['ops/start.sh', '--backend-only'], env={**os.environ, 'SETUP_IN_PROGRESS': '1'})
    guard()
    check(sql(f'SELECT count(*) FROM accounts WHERE id={account_id} AND email=\'{email}\'') == '1', '重啟後帳號遺失')
    check(sql('SELECT enabled::text ||\'|\'||window_minutes||\'|\'||failure_threshold||\'|\'||lock_seconds FROM login_lockout_settings WHERE id=1') == settings, '重啟後設定改變')
    check([path.read_bytes() for path in key_paths] == keys, '重啟後金鑰改變')
    me = expect(call(FASTIFY, 'GET', '/api/me', token=token), 200)
    check(me['id'] == account_id and login['expires_at'] == expires, '重啟後登入身分或到期時間改變')
    print('PASS ticket37 stop/restart preserves volume, account, settings, keys and login')


if __name__ == '__main__':
    main()
