"""驗證 Ticket 37 隔離專案正常停止／重啟時保留資料、設定、金鑰與登入期限。"""

import base64
import json
import os
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
import secrets
import subprocess
from pathlib import Path

from ticket37_http import FASTIFY, call, check, expect, guard, register, sql


# session_state 以 token 的 jti 查真實 session，避免只比較未更新的登入回應。
def session_state(login):
    claims = json.loads(base64.urlsafe_b64decode(login['access_token'].split('.')[1] + '==='))
    jti = claims['jti']
    check(isinstance(jti, str) and re.fullmatch(r'[A-Za-z0-9_-]{21}[AQgw]', jti), '登入 jti 格式不符')
    state = json.loads(sql("SELECT row_to_json(s) FROM (SELECT jti,account_id,backend_id,"
                           "issued_at AT TIME ZONE 'UTC' AS issued_at,expires_at AT TIME ZONE 'UTC' AS expires_at,"
                           "extract(epoch FROM issued_at)::text AS issued_epoch,"
                           "extract(epoch FROM expires_at)::text AS expires_epoch,revoked_at "
                           f"FROM login_sessions WHERE jti='{jti}') s"))
    # 原回應、JWT 與持久期限三方一致；不重新登入，也不捨去持久微秒。
    response_expiry = datetime.strptime(login['expires_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Taipei'))
    check(type(claims['iat']) is int and type(claims['exp']) is int and
          Decimal(state['issued_epoch']) == claims['iat'] and Decimal(state['expires_epoch']) == claims['exp'] and
          response_expiry.timestamp() == claims['exp'] and
          state['account_id'] == login['user']['id'] and state['backend_id'] == 'fastify' and
          state['revoked_at'] is None, '登入回應／JWT／持久 session 不一致')
    return state


# stop_and_restart 在已 guard 的專案驗停止狀態，任何 stop／斷言失敗仍嘗試啟回。
def stop_and_restart(key_paths, keys):
    # 停止入口只作用於 guard 已核對的 project；資料卷及金鑰必須保持原樣。
    try:
        subprocess.check_call(['ops/stop.sh'])
        for service in ('db', 'laravel', 'fastify'):
            running = subprocess.check_output(['docker', 'compose', '--profile', 'fastify', 'ps', '-q', service], text=True).strip()
            check(not running, f'{service} 停止後仍在執行')
        volumes = subprocess.check_output(['docker', 'volume', 'ls', '--filter', 'name=ticket37_test_postgres_data', '--format', '{{.Name}}'], text=True).splitlines()
        check(volumes == ['ticket37_test_postgres_data'], '停止後資料卷遺失')
        check([path.read_bytes() for path in key_paths] == keys, '停止入口變更金鑰')

    finally:
        # stop 本身或停止後斷言失敗也啟回；雙重失敗以 Python 例外鏈保留原因。
        subprocess.check_call(['ops/start.sh', '--backend-only'], env={**os.environ, 'SETUP_IN_PROGRESS': '1'})


# main 先驗證實際隔離服務，再做本票唯一帳號的停止／重啟保留檢查。
def main():
    guard()
    email, login = register('lifecycle-' + secrets.token_hex(5))
    token = login['access_token']
    session = session_state(login)
    account_id = login['user']['id']
    settings = sql('SELECT enabled::text ||\'|\'||window_minutes||\'|\'||failure_threshold||\'|\'||lock_seconds FROM login_lockout_settings WHERE id=1')
    key_paths = [Path('.local/ticket37-backend/laravel.key'), Path('.local/ticket37-backend/fastify.key')]
    keys = [path.read_bytes() for path in key_paths]
    check(expect(call(FASTIFY, 'GET', '/api/me', token=token), 200)['id'] == account_id, '停止前登入無效')

    stop_and_restart(key_paths, keys)
    # 啟回同一隔離目標後才讀持久資料，不以另一個登入取代原 session。
    guard()
    check(sql(f'SELECT count(*) FROM accounts WHERE id={account_id} AND email=\'{email}\'') == '1', '重啟後帳號遺失')
    check(sql('SELECT enabled::text ||\'|\'||window_minutes||\'|\'||failure_threshold||\'|\'||lock_seconds FROM login_lockout_settings WHERE id=1') == settings, '重啟後設定改變')
    check([path.read_bytes() for path in key_paths] == keys, '重啟後金鑰改變')
    me = expect(call(FASTIFY, 'GET', '/api/me', token=token), 200)
    check(me['id'] == account_id and session_state(login) == session, '重啟後登入身分或到期時間改變')
    print('PASS ticket37 stop/restart preserves volume, account, settings, keys and login')


if __name__ == '__main__':
    main()
