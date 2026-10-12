"""首次設定輸入中斷時，只在 ticket37_test 隔離環境驗證停止與重啟。"""

import os
import secrets
import subprocess
from pathlib import Path

from ticket37_http import FASTIFY, LARAVEL, call, check, expect, guard, register, sql


# running 讀取本票 Compose 的實際執行容器，避免把已停止容器誤判為仍提供 API。
def running(service):
    return subprocess.check_output(["docker", "compose", "--profile", "fastify", "ps", "-q", service], text=True).strip()


# main 沿 setup.sh 首次管理員輸入 EOF 失敗路徑，核對服務、資料、金鑰後恢復隔離服務。
def main():
    guard()
    marker = Path(".local/setup-complete-ticket37_test")
    check(not marker.exists(), "隔離專案已有完成標記，不能測首次設定中斷")
    email, login = register("setup-abort-" + secrets.token_hex(5))
    account_id = login["user"]["id"]
    token = login["access_token"]
    key_paths = [Path(f".local/ticket37-backend/{backend}.key") for backend in ("laravel", "fastify")]
    keys = [path.read_bytes() for path in key_paths]
    demo_paths = [Path(".env"), Path(".local/keys/laravel.key"), Path(".local/keys/fastify.key")]
    demo_files = [path.read_bytes() for path in demo_paths]
    volume = "ticket37_test_postgres_data"
    check(subprocess.check_output(["docker", "volume", "ls", "--filter", f"name={volume}", "--format", "{{.Name}}"], text=True).splitlines() == [volume], "隔離資料卷錯誤")

    # setup.sh 必須真正啟動後端並在第一個管理員輸入處收到 EOF；不傳管理員個資或密碼。
    try:
        result = subprocess.run(["ops/setup.sh", "--backend-only"], input="", text=True, capture_output=True, timeout=300)
        check(result.returncode != 0 and "Laravel／Fastify 專用後端已就緒" in result.stdout and
              "首次管理員 Email" in result.stdout, f"首次設定未在輸入處中斷：{result.returncode} {result.stderr[-500:]}")
        check(not marker.exists(), "中斷後錯誤寫入完成標記")
        for service in ("laravel", "fastify"):
            check(not running(service), f"{service} 在首次設定中斷後仍提供服務")
        check([path.read_bytes() for path in key_paths] == keys, "首次設定中斷改變測試金鑰")
        check([path.read_bytes() for path in demo_paths] == demo_files, "首次設定中斷改變展示設定或金鑰")
        check(subprocess.check_output(["docker", "volume", "ls", "--filter", f"name={volume}", "--format", "{{.Name}}"], text=True).splitlines() == [volume], "首次設定中斷移除資料卷")
    finally:
        # 無論斷言結果如何，都只恢復已由 guard 核對的隔離專案至可用邊界。
        subprocess.check_call(["ops/start.sh", "--backend-only"], env={**os.environ, "SETUP_IN_PROGRESS": "1"})
    guard()
    check(sql(f"SELECT count(*) FROM accounts WHERE id={account_id} AND email='{email}'") == "1", "重啟後資料遺失")
    check([path.read_bytes() for path in key_paths] == keys and [path.read_bytes() for path in demo_paths] == demo_files and
          not marker.exists(), "重啟後金鑰、設定或完成標記改變")
    check(expect(call(FASTIFY, "GET", "/api/me", token=token), 200)["id"] == account_id, "重啟後登入未保留")
    check(expect(call(LARAVEL, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)["user"]["id"] == account_id, "Laravel 重啟後未讀同一資料")
    print("PASS ticket37 setup abort stops Laravel/Fastify and preserves volume, account, keys, marker")


if __name__ == "__main__":
    main()
