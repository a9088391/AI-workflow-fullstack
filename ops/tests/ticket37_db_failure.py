"""僅在 ticket37_test 隔離庫檢查驗證資料不可用時的安全回應與恢復。"""

import subprocess
import time
import secrets
from urllib.request import urlopen

from ticket37_http import FASTIFY, call, check, expect, guard, register


# compose 只作用於已由 guard 核對的 ticket37_test 專案。
def compose(*args):
    return subprocess.check_output(["docker", "compose", *args], text=True)


# main 停止專用 DB 後重試受保護讀取，最後一律恢復資料庫並驗證原 session。
def main():
    guard()
    _, data = register("dbfail" + secrets.token_hex(5))
    token = data["access_token"]
    try:
        compose("stop", "db")
        expect(call(FASTIFY, "GET", "/api/me", token=token), 500, "E9999")
    finally:
        compose("up", "-d", "--wait", "db")
    for _ in range(40):
        try:
            with urlopen(FASTIFY + "/up", timeout=2) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        time.sleep(0.25)
    else:
        raise AssertionError("資料庫恢復後 Fastify 未就緒")
    check(expect(call(FASTIFY, "GET", "/api/me", token=token), 200)["id"] == data["user"]["id"], "原登入恢復後應有效")
    print("PASS ticket37 DB failure/recovery")


if __name__ == "__main__":
    main()
