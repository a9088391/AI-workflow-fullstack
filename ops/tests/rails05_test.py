"""Rails05 固定 preset 的離線回歸；重用 CQ34 工具替身，絕不操作服務或真實金鑰。"""

from contextlib import chdir, redirect_stderr
from copy import deepcopy
import io
import importlib.util
import json
import os
import tempfile
from unittest.mock import patch
from urllib.request import Request

import cq18_http as recovery
import cq34_http as driver
from cq34_test import GuardFixture, refused, trace_tests, transport_failure, wire_response
import ticket57_http as shared


# 每次只改一個核對輸入；必須先正向通過，失敗後 SAFE 也必須撤銷。
def guard_case(change):
    with tempfile.TemporaryDirectory(prefix="rails05-guard-") as folder:
        fixture = GuardFixture(folder, "rails05")
        identity = fixture.guard()
        shared.check(fixture.subject.SAFE and identity["project"] == "rails05_test" and
                     set(identity["keys"]) == set(fixture.subject.BACKENDS) and
                     len(identity["keys"]) == 13, "Rails05 正向 guard 未完整核對十三版")
        change(fixture)
        refused(fixture.guard, errors=(AssertionError, ValueError))
        shared.check(not fixture.subject.SAFE, "Rails05 失敗保留寫入權")


# 真 guard 逐項拒絕其他 project／映像／來源／埠／掛載／key；不替換 guard 或 SAFE。
def guard_tests():
    # 舊 CQ34 project 不可借用 Rails05 寫入入口。
    guard_case(lambda f: f.config.update(name="cq34_test"))
    # 展示 project 即使全部 healthy 也不是合成資料目標。
    guard_case(lambda f: f.services["rails"]["Config"]["Labels"].update({"com.docker.compose.project": "ai-workflow-fullstack"}))
    # 固定 source 根目錄標籤不得指到前票。
    guard_case(lambda f: f.services["db"]["Config"]["Labels"].update({"com.docker.compose.project.working_dir": "/other"}))
    # 不接受其他 Compose override 來源。
    guard_case(lambda f: f.services["rails"]["Config"]["Labels"].update({"com.docker.compose.project.config_files": "other.yaml"}))
    # 健康的舊映像仍必須被拒絕。
    guard_case(lambda f: f.services["rails"].update(Image="sha256:old"))
    # 來源副本逐 bytes 核對，不以 HEAD 或 health 代替。
    guard_case(lambda f: (f.root / "backends/rails/Dockerfile").write_text("wrong"))
    # Symfony tracked 建置入口不能因 bin 目錄名而漏驗 bytes。
    guard_case(lambda f: (f.root / "backends/symfony/bin/console").write_bytes(b"changed console\n"))
    # 即使 bytes 相同，來源副本的 mode 不符也必須拒絕。
    guard_case(lambda f: (f.root / "backends/symfony/bin/console").chmod(0o600))
    # 缺少必要建置入口不能仍取得 SAFE。
    guard_case(lambda f: (f.root / "backends/symfony/bin/console").unlink())
    # 指回原來源的 symlink 也不屬於固定實體來源副本。
    guard_case(lambda f: ((f.root / "backends/symfony/bin/console").unlink(),
                          (f.root / "backends/symfony/bin/console").symlink_to(f.repo / "backends/symfony/bin/console")))
    # override 也必須逐 bytes 對應受版控 template。
    guard_case(lambda f: (f.root / ".local/rails05-compose.yaml").write_text("wrong"))
    # Rails 不能額外發布其他主機埠。
    guard_case(lambda f: f.services["rails"]["HostConfig"]["PortBindings"].update({"9000/tcp": []}))
    # key 必須掛到實際固定來源且唯讀。
    guard_case(lambda f: f.services["rails"]["Mounts"][0].update(Source="/other/rails.key"))
    # RW key 不屬於核准 fixture。
    guard_case(lambda f: f.services["rails"]["Mounts"][0].update(RW=True))
    # 跨後端共用 key 會破壞 JWT 隔離，必須拒絕。
    guard_case(lambda f: (f.root / ".local/keys/rails.key").write_bytes((f.root / ".local/keys/laravel.key").read_bytes()))
    # 不可將放寬權限的 key 當作合法。
    guard_case(lambda f: (f.root / ".local/keys/rails.key").chmod(0o644))
    # 多行空白不可經 strip 被誤當 canonical key。
    guard_case(lambda f: (f.root / ".local/keys/rails.key").write_bytes((f.root / ".local/keys/rails.key").read_bytes() + b"\n\n"))
    # Rails 初始化 marker 是既有 key 生命週期的一部分。
    guard_case(lambda f: (f.root / ".local/keys/.rails-initialized").write_bytes(b"wrong\n"))
    # DB 標記不足以取代實際 volume、network 與 cluster 核對。
    guard_case(lambda f: f.volume.update(Driver="remote"))
    # 原生 local volume 不得轉成外部儲存。
    guard_case(lambda f: f.volume.update(Options={"type": "nfs"}))
    # 固定子網不可指到前票。
    guard_case(lambda f: f.network["IPAM"]["Config"][0].update(Subnet="10.251.134.0/24"))
    # cluster name 取自真 SQL seam 的工具回應，不以 env 自稱身分。
    guard_case(lambda f: setattr(f, "cluster_name", "cq34_test"))
    with tempfile.TemporaryDirectory(prefix="rails05-target-") as folder:
        fixture = GuardFixture(folder, "rails05")
        for project in ("cq34_test", "ai-workflow-fullstack"):
            with chdir(fixture.root), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": project, "COMPOSE_FILE": "compose.yaml:.local/rails05-compose.yaml"}):
                # 錯誤環境必須先於任何 subprocess 被拒絕。
                refused(fixture.subject.guard)
        with chdir(fixture.repo), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "rails05_test", "COMPOSE_FILE": "compose.yaml:.local/rails05-compose.yaml"}):
            refused(fixture.subject.guard)


# CLI 在 guard 前選定 namespace，禁止默認 CQ34 使用 Rails 或跨 namespace 還原。
def cli_tests():
    shared.check(driver.arguments(["--preset", "rails05", "--backend", "rails"]).backend == "rails", "Rails05 CLI 不可達")
    shared.check(driver.arguments(["--all"]).preset == "cq34", "舊 CQ34 預設改變")
    with redirect_stderr(io.StringIO()):
        for argv in (["--backend", "rails"], ["--preset", "display", "--all"],
                     ["--preset", "rails05", "--restore", ".local/cq34-recovery/recovery-cq34aabbccdd.json"],
                     ["--restore", ".local/rails05-recovery/recovery-r05aabbccdd.json"]):
            # 綁定當輪 argv，確認 parser 本身拒絕而非工具或 fixture 失敗。
            refused(lambda: driver.arguments(argv), errors=(SystemExit,))


# 真 save/load/preflight 測跨票與跨來源 restore、pending、未知 transport 及 replay，僅寫暫存檔。
def recovery_tests():
    driver.select_preset("rails05")
    identity = {"project": "rails05_test", "source": "a" * 64, "database": "synthetic-cluster"}
    packet = {"nonce": "r05aabbccdd", "settings": {"enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 30},
              "members": [], "account_times": [], "session_times": [], "restored": False}
    receipt = {"namespace": "rails05", "identity": identity, "times": [], "boundary": None, "uncertain": False,
               "state": "pending", "counts_before": dict.fromkeys(driver.LIMITS, 0), "groups": []}
    with tempfile.TemporaryDirectory(prefix="rails05-recovery-") as folder, chdir(folder):
        driver.RECOVERY.mkdir(mode=0o700, parents=True)
        path = driver.RECOVERY / "recovery-r05aabbccdd.json"
        recovery.save_packet(packet, path, receipt=receipt)
        shared.check(driver.load_packet(path, identity)[0] == packet, "Rails05 recovery roundtrip 失敗")
        # 未完成 baseline 禁止下一 run；恢復則必須原 target 與 source 完全相同。
        refused(lambda: driver.preflight_packets(identity))
        for field in identity:
            wrong = {**identity, field: "wrong"}
            # 摘要正確也不能換 target、source 或 cluster。
            refused(lambda: driver.load_packet(path, wrong))
        wrong = deepcopy(receipt)
        wrong["namespace"] = "cq34"
        # 同一 schema 不授予跨 namespace 恢復權。
        refused(lambda: recovery.wrapped_packet(packet, wrong))
        wrong_packet = {**packet, "nonce": "cq34aabbccdd"}
        # Rails05 baseline 不接受舊 CQ34 nonce。
        refused(lambda: recovery.validate_packet(wrong_packet, ticket="rails05"))
        shared.CQ34_RECOVERY = packet, path, receipt
        try:
            with patch.object(shared.CQ34_OPENER, "open", transport_failure):
                # 真傳輸失敗持久化 unknown，不能因 cleanup 完成變成 PASS。
                refused(lambda: shared.open_response(Request("http://127.0.0.1:8093/api/articles", method="POST"), timeout=1), errors=(TimeoutError,))
            shared.check(driver.load_packet(path, identity)[1]["uncertain"], "Rails05 unknown 未落盤")
        finally:
            shared.CQ34_RECOVERY = None
        packet["restored"], receipt["state"] = True, "incomplete"
        recovery.save_packet(packet, path, receipt=receipt)
        refused(lambda: driver.preflight_packets(identity))
        receipt["state"], receipt["uncertain"] = "restored", False
        recovery.save_packet(packet, path, receipt=receipt)
        shared.check(driver.preflight_packets(identity) is None, "完成記錄不可只讀稽核")
        refused(lambda: driver.preflight_packets(identity, path))
        # 未知暫存檔不能被自動略過或移除。
        (driver.RECOVERY / "pending.tmp").write_text("unknown")
        refused(lambda: driver.preflight_packets(identity))


# 原始 bytes client 同樣經真 guard；截斷 POST 標記 unknown，之後不可再送出第二筆寫入。
def raw_transport_test():
    with tempfile.TemporaryDirectory(prefix="rails05-raw-") as folder:
        fixture = GuardFixture(folder, "rails05")
        identity = fixture.guard()
        target = fixture.subject
        with patch.dict("sys.modules", {"ticket57_http": target}):
            spec = importlib.util.spec_from_file_location("rails05_raw_recovery", recovery.__file__)
            baseline = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(baseline)
        packet = {"nonce": "r05aabbccdd"}
        receipt = {"namespace": "rails05", "identity": identity, "uncertain": False}
        path = fixture.root / ".local/raw-transport.json"
        target.CQ34_RECOVERY = packet, path, receipt
        calls = []

        # 工具 seam 只回應指定 Rails POST，保留真 HTTPResponse 的短 body 失敗。
        def opened(request, **kwargs):
            shared.check(request.full_url == "http://127.0.0.1:8093/api/articles", "raw client 送往錯後端")
            calls.append(request.get_method())
            return wire_response(request, "short-body")

        with chdir(fixture.root), patch.dict("sys.modules", {"cq18_http": baseline}), \
                patch.object(target.CQ34_OPENER, "open", opened):
            # 短 body 是真 parser 拋出；不修改 guard、SAFE 或呼叫者函式。
            refused(lambda: baseline.raw_request("POST", "/api/articles", b"{}", backend="rails"), errors=(baseline.HTTPException,))
            shared.check(json.loads(path.read_text())["receipt"]["uncertain"], "raw 未持久化 unknown")
            refused(lambda: baseline.raw_request("POST", "/api/articles", b"{}", backend="rails"))
            shared.check(calls == ["POST"], "raw unknown 後仍重送寫入")
        target.CQ34_RECOVERY = None


# 保留原十組及 readback，另核對 helper 最大前綴以免案例在首寫時才因暱稱失敗。
def run():
    guard_tests()
    trace_tests()
    cli_tests()
    recovery_tests()
    raw_transport_test()
    shared.check(len(driver.GROUPS) == 10 and len(driver.GROUP_NAMES) == 11 and len(shared.BACKENDS) == 13,
                 "Rails05 必要共同組數或後端缺漏")
    shared.check(len("Aa%_\\Ä陳" + "r05aabbccdd" + "aspnet-core") <= 30, "Rails05 搜尋暱稱超過契約")
    for index, backend in enumerate(shared.BACKENDS[:-1]):
        shared.check(shared.peer_backend(backend) == shared.BACKENDS[(index + 1) % 12], "原十二版交易配對改變")
    shared.check(shared.peer_backend("rails") == "laravel", "Rails 交易 peer 不符")
    print("PASS RAILS05 離線固定 guard／CLI／namespace／pending／unknown／replay；未執行 API", flush=True)


if __name__ == "__main__":
    run()
