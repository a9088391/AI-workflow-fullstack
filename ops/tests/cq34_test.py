"""CQ34 離線守衛／恢復反例；只用暫存合成檔與工具 I/O mock，不接服務。"""

import base64
from contextlib import chdir, redirect_stderr, redirect_stdout
from copy import deepcopy
from email.message import Message
from http.client import BadStatusLine, HTTPException, HTTPResponse, IncompleteRead
import importlib.util
import hashlib
import io
import json
import os
import runpy
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import cq34_http as driver
import cq18_http as recovery
import ticket57_http as shared


# 假 HTTP 回應只回固定 health／Origin，未知 URL 在工具 seam 直接失敗。
class Response:
    # 建立不含憑證的固定回應，供真 guard 斷言使用。
    def __init__(self, body=b"", status=200):
        self.body, self.status = body, status
        self.headers = {"Access-Control-Allow-Origin": "http://localhost:8080"}

    # urllib context 不持有資源，與真 response 一樣供 with 使用。
    def __enter__(self):
        return self

    # 不吞例外，讓錯誤的 guard 斷言仍可被反例觀察。
    def __exit__(self, *args):
        return False

    # 保留 health 原始 bytes，避免 mock 跳過回應內容契約。
    def read(self):
        return self.body


# 每個 guard fixture 正常載入真來源的新模組；只 mock Git／Docker／HTTP I/O，不改 shared 常數或 SAFE。
class GuardFixture:
    # 從暫存 repository 的實際檔案位置推導固定 cwd，合成 key 與容器資料完全自有。
    def __init__(self, folder, preset="cq34"):
        # 真來源在暫存目錄正常 import，避免改 shared 的固定常數或繞過 guard。
        self.repo = Path(folder).resolve()
        self.preset, self.project = preset, preset + "_test"
        subnet = "10.251.135" if preset == "rails05" else "10.251.134"
        module_path = self.repo / "ops/tests/ticket57_http.py"
        module_path.parent.mkdir(parents=True)
        module_path.write_bytes(Path(shared.__file__).read_bytes())
        spec = importlib.util.spec_from_file_location("cq34_guard_subject", module_path)
        self.subject = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.subject)
        self.subject.select_preset(preset)
        self.root = self.repo / f".local/{preset}-fixture"
        self.names = [f"ops/tests/{preset}_compose.yaml", "backends/symfony/bin/console"]
        for backend in self.subject.BACKENDS:
            self.names.append(f"backends/{backend}/Dockerfile")
        for name in self.names:
            for root in (self.repo, self.root):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "backends/symfony/bin/console":
                    # Symfony 的 tracked bin/console 是建置入口，正常 fixture 必須保留實際 bytes 與 mode。
                    original = Path(shared.__file__).resolve().parents[2] / name
                    path.write_bytes(original.read_bytes())
                    path.chmod(stat.S_IMODE(original.stat().st_mode))
                else:
                    path.write_text("FROM scratch\n" if name.endswith("Dockerfile") else "fixed cq34 template\n")
        override = self.root / f".local/{preset}-compose.yaml"
        override.parent.mkdir(parents=True)
        override.write_text("fixed cq34 template\n")
        # 每組 key 都是本測試生成的合成值；default 與 CQ34 各自獨立。
        for index, backend in enumerate(self.subject.BACKENDS):
            for kind, offset in (("keys", 40), ("cq34-keys", 1)):
                key = self.root / f".local/{kind}/{backend}.key"
                key.parent.mkdir(parents=True, exist_ok=True)
                key.write_bytes(base64.b64encode(bytes([index + offset]) * 32))
                key.chmod(0o600)
        if preset == "rails05":
            marker = self.root / ".local/keys/.rails-initialized"
            marker.write_bytes(b"rails-jwt-v1\n")
            marker.chmod(0o600)
        # 假 inspect 保留完整身分、實際 port/mount/image，讓 production 斷言逐一執行。
        names = ("db", *self.subject.BACKENDS, *("frontend-" + name for name in ("vue", "nuxt", "react", "next", "angular", "sveltekit")), "nginx")
        self.services = {}
        for name in names:
            self.services[name] = {"Config": {"Labels": {
                "com.docker.compose.project": self.project, "com.docker.compose.service": name,
                "com.docker.compose.project.working_dir": str(self.root),
                "com.docker.compose.project.config_files": f"{self.root}/compose.yaml,{self.root}/.local/{preset}-compose.yaml"}, "Env": []},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "NetworkSettings": {"Networks": {self.project + "_default": {}}}, "Image": "sha256:image-" + name,
                "Mounts": [], "HostConfig": {"PortBindings": {}}}
        self.services["db"]["Config"]["Env"] = [preset.upper() + "_FIXTURE=isolated-" + preset]
        self.services["db"]["Mounts"] = [{"Type": "volume", "Name": self.project + "_postgres_data", "Destination": "/var/lib/postgresql"}]
        for index, name in enumerate(self.subject.BACKENDS):
            item = self.services[name]
            item["Config"]["Env"] = ["DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"]
            item["HostConfig"]["PortBindings"] = {"8080/tcp" if name == "aspnet-core" else "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(8081 + index)}]}
            item["Mounts"] = [{"Type": "bind", "Source": str(self.root / f".local/{'keys' if preset == 'rails05' else 'cq34-keys'}/{name}.key"), "Destination": f"/run/secrets/{name}-jwt.key", "RW": False}]
        self.services["nginx"]["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
        # 宣告設定與實際 network/volume 分開保留，可精確改動其中一項做反例。
        self.config = {"name": self.project, "volumes": {"postgres_data": {"name": self.project + "_postgres_data"}},
                       "services": {"db": {"environment": {preset.upper() + "_FIXTURE": "isolated-" + preset}}},
                       "networks": {"default": {"ipam": {"config": [{"subnet": subnet + ".0/24"}]}}}}
        self.network = {"Name": self.project + "_default", "Labels": {"com.docker.compose.project": self.project}, "IPAM": {"Config": [{"Subnet": subnet + ".0/24", "Gateway": subnet + ".1"}]}}
        self.volume = {"Name": self.project + "_postgres_data", "Labels": {"com.docker.compose.project": self.project, "com.docker.compose.volume": "postgres_data"}, "Driver": "local", "Options": None, "CreatedAt": "2026-11-04T00:00:00Z"}

    # 模擬外部命令但驗證完整固定 argv；未列明的 SQL／Docker 指令不會意外成功。
    def command(self, args, **kwargs):
        args = tuple(args)
        if args[:1] == ("git",):
            if "ls-files" in args:
                return ("\0".join(self.names) + "\0").encode()
            raise AssertionError("unexpected git")
        if args[:2] == ("docker", "inspect"):
            return json.dumps([self.services[args[-1]]])
        if args[:3] == ("docker", "network", "inspect"):
            return json.dumps([self.network])
        if args[:3] == ("docker", "volume", "inspect"):
            return json.dumps([self.volume])
        fixed = ("docker", "compose", "-p", self.project, "-f", "compose.yaml", "-f", f".local/{self.preset}-compose.yaml", "--profile", "*")
        if args[:len(fixed)] != fixed:
            raise AssertionError("unexpected compose argv")
        tail = args[len(fixed):]
        if tail == ("config", "--format", "json"):
            return json.dumps(self.config)
        if tail[:2] == ("ps", "-q"):
            return tail[-1]
        if tail[:2] == ("images", "-q"):
            return "image-" + tail[-1]
        if tail[:3] == ("exec", "-T", "db"):
            if tail[-1] == "SELECT current_database()||':'||current_user":
                return "articles:articles_owner"
            if tail[-1] == "SHOW cluster_name":
                return getattr(self, "cluster_name", self.project)
            if tail[-1] == "SELECT system_identifier FROM pg_control_system()":
                return "123456789"
        raise AssertionError("unexpected fake command")

    # subprocess.run 使用相同嚴格命令表，保留非零與輸出分離的真介面。
    def process(self, args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=self.command(args, **kwargs))

    # 只接受固定 health、前端路徑與 preflight；此處沒有 socket 或 URL 重送。
    def http(self, request, **kwargs):
        if isinstance(request, str):
            if request.endswith("/up"):
                return Response(b'{"status":"ok"}' if ":8082/" in request or ":8083/" in request else b"OK")
            if request.startswith("http://127.0.0.1:8080/"):
                return Response()
        elif request.method == "OPTIONS" and request.full_url.endswith("/api/auth/login"):
            return Response(status=204)
        raise AssertionError("unexpected fake HTTP")

    # 真 guard 完整執行；正負案例都不能以替換 guard／SAFE 取得通過。
    def guard(self):
        with chdir(self.root), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": self.project, "COMPOSE_FILE": f"compose.yaml:.local/{self.preset}-compose.yaml"}), \
                patch.object(self.subject.subprocess, "check_output", self.command), \
                patch.object(self.subject.subprocess, "run", self.process), \
                patch.object(self.subject.CQ34_OPENER, "open", self.http):
            return self.subject.guard()


# 驗證預期拒絕且未意外吞下其他錯誤，供有限反例共用。
def refused(action, errors=(AssertionError,)):
    try:
        action()
    except errors:
        return
    raise AssertionError("expected fail-closed refusal")


# 單一反例只變更一個真 guard 的輸入，先正向通過再確認失敗會重設 SAFE。
def guard_case(change):
    with tempfile.TemporaryDirectory(prefix="cq34-guard-") as folder:
        fixture = GuardFixture(folder)
        fixture.guard()
        shared.check(fixture.subject.SAFE, "positive guard missing")
        change(fixture)
        refused(fixture.guard)
        shared.check(not fixture.subject.SAFE, "failed guard retained write permission")


# 測固定工具輸入的正向與逐邊界反例；不建立真 Compose 或讀取任何使用者 key。
def guard_tests():
    # 每個 callback 緊鄰說明所改的唯一邊界，讓反例的意圖可查。
    # 拒絕宣告 project 冒名。
    guard_case(lambda f: f.config.update(name="wrong"))
    # 拒絕舊網段。
    guard_case(lambda f: f.config["networks"]["default"]["ipam"].update(config=[{"subnet": "10.251.57.0/24"}]))
    # 拒絕實際 volume 冒名。
    guard_case(lambda f: f.volume.update(Name="other_volume"))
    # 實際 driver 非 local，即使 Options 為 null 也不可取得寫入權。
    guard_case(lambda f: f.volume.update(Driver="remote-plugin"))
    # 原生 local volume 也不能藏有外部掛載 Options。
    guard_case(lambda f: f.volume.update(Options={"type": "nfs"}))
    # 宣告必須是原生 local，不能只依實際 volume 同名推定。
    guard_case(lambda f: f.config["volumes"]["postgres_data"].update(driver="remote-plugin"))
    # 宣告 driver_opts 會改變儲存目的地，因此必須拒絕。
    guard_case(lambda f: f.config["volumes"]["postgres_data"].update(driver_opts={"type": "nfs"}))
    # external 會脫離本票生命週期，不屬於已批准的新本機資料卷。
    guard_case(lambda f: f.config["volumes"]["postgres_data"].update(external=True))
    # 拒絕錯 cwd label。
    guard_case(lambda f: f.services["db"]["Config"]["Labels"].update({"com.docker.compose.project.working_dir": "/wrong"}))
    # 拒絕錯 config label。
    guard_case(lambda f: f.services["db"]["Config"]["Labels"].update({"com.docker.compose.project.config_files": "compose.yaml"}))
    # 拒絕非目前映像。
    guard_case(lambda f: f.services["db"].update(Image="sha256:wrong"))
    # 拒絕錯 key 掛載。
    guard_case(lambda f: f.services["laravel"]["Mounts"][0].update(Source="/wrong/key"))
    # 拒絕 source hash 漂移。
    guard_case(lambda f: (f.root / "backends/laravel/Dockerfile").write_text("changed"))
    # Symfony tracked 建置入口不能因 bin 目錄名而漏驗 bytes。
    guard_case(lambda f: (f.root / "backends/symfony/bin/console").write_bytes(b"changed console\n"))
    # 即使 bytes 相同，來源副本的 mode 不符也必須拒絕。
    guard_case(lambda f: (f.root / "backends/symfony/bin/console").chmod(0o600))
    # 缺少必要建置入口不能仍取得 SAFE。
    guard_case(lambda f: (f.root / "backends/symfony/bin/console").unlink())
    # 指回原來源的 symlink 也不屬於固定實體來源副本。
    guard_case(lambda f: ((f.root / "backends/symfony/bin/console").unlink(),
                          (f.root / "backends/symfony/bin/console").symlink_to(f.repo / "backends/symfony/bin/console")))
    # 拒絕 template 漂移。
    guard_case(lambda f: (f.root / ".local/cq34-compose.yaml").write_text("changed"))
    with tempfile.TemporaryDirectory(prefix="cq34-cwd-") as folder:
        fixture = GuardFixture(folder)
        with chdir(fixture.repo), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "cq34_test", "COMPOSE_FILE": "compose.yaml:.local/cq34-compose.yaml"}):
            refused(fixture.subject.guard)
        with chdir(fixture.root), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "wrong", "COMPOSE_FILE": "compose.yaml:.local/cq34-compose.yaml"}):
            refused(fixture.subject.guard)
        with chdir(fixture.root), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "cq34_test", "COMPOSE_FILE": "compose.yaml:wrong.yaml"}):
            refused(fixture.subject.guard)


# 真開第二個固定 fd，確認互斥鎖不是只憑檔案存在推定。
def lock_again():
    with driver.run_lock():
        raise AssertionError("concurrent CQ34 lock unexpectedly acquired")


# 假 transport 只在 I/O 邊界拋錯，讓原程式處理未知寫入與持久狀態。
def transport_failure(*args, **kwargs):
    raise TimeoutError("synthetic transport interruption")


# 原 baseline schema 及 CQ34 外層都正常經 save/load；破壞摘要、namespace、pending 或 replay 都須停止。
def recovery_tests():
    shared.select_preset("cq34")
    identity = {"target": "synthetic-cq34"}
    packet = {"nonce": "cq34aabbccdd", "settings": {"enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 30},
              "members": [], "account_times": [], "session_times": [], "restored": False}
    receipt = {"namespace": "cq34", "identity": identity, "times": [], "boundary": None, "uncertain": False,
               "state": "pending", "counts_before": dict.fromkeys(driver.LIMITS, 0), "groups": []}
    with tempfile.TemporaryDirectory(prefix="cq34-recovery-") as folder, chdir(folder):
        driver.RECOVERY.mkdir(mode=0o700, parents=True)
        with driver.run_lock():
            # 同 process 的另一個 fd 也不能並行跨過 pending 掃描。
            refused(lock_again, errors=(BlockingIOError,))
        path = driver.RECOVERY / "recovery-cq34aabbccdd.json"
        recovery.save_packet(packet, path, receipt=receipt)
        shared.check(driver.load_packet(path, identity)[0] == packet, "positive recovery roundtrip missing")
        # 每個 callback 在呼叫點保留反例目的，原始 packet 不被測試意外覆寫。
        # pending 禁止新 run。
        refused(lambda: driver.preflight_packets(identity))
        # 不同目標不能 restore。
        refused(lambda: driver.load_packet(path, {"target": "wrong"}))
        value = json.loads(path.read_text())
        value["baseline"]["settings"]["lock_seconds"] = 31
        path.write_text(json.dumps(value))
        # 不符摘要保留停止。
        refused(lambda: driver.load_packet(path, identity))
        wrong = deepcopy(receipt)
        wrong["namespace"] = "cq33"
        # namespace 不能自授權。
        refused(lambda: recovery.wrapped_packet(packet, wrong))
        packet["restored"], receipt["state"] = True, "restored"
        recovery.save_packet(packet, path, receipt=receipt)
        # 已恢復拒絕重播。
        refused(lambda: driver.preflight_packets(identity, path))
        # transport seam 真拋 timeout，原函式必須把 unknown 持久化；不設定 SAFE 或真的送 HTTP。
        packet["restored"], receipt["state"] = False, "pending"
        shared.CQ34_RECOVERY = packet, path, receipt
        try:
            with patch.object(shared.CQ34_OPENER, "open", transport_failure):
                # 寫入沒有完整回應時，後續 cleanup 完成也不能讓新 run 開始。
                refused(lambda: shared.open_response(Request("http://127.0.0.1:8081/api/articles", method="POST"), timeout=1), errors=(TimeoutError,))
            shared.check(driver.load_packet(path, identity)[1]["uncertain"], "transport unknown 未落盤")
        finally:
            shared.CQ34_RECOVERY = None
        packet["restored"], receipt["state"] = True, "incomplete"
        recovery.save_packet(packet, path, receipt=receipt)
        # 已完成 fixture cleanup 的未知寫入仍阻止新 run，不能誤報回滾成功。
        refused(lambda: driver.preflight_packets(identity))
        unknown = driver.RECOVERY / "pending.tmp"
        unknown.write_text("unknown")
        # 未知檔案禁止新 run。
        refused(lambda: driver.preflight_packets(identity))


# 真 CLI 接上真 suite 的合成案例／恢復失敗，避免只測 helper 而漏掉入口的例外處理。
def reporting_tests():
    import cq34_test as cli_tests
    sentinel = "cq34-cli-sentinel-secret"
    for case_type, cleanup_failure, cycle in ((AssertionError, False, False), (KeyboardInterrupt, False, False),
                                              (None, True, False), (AssertionError, True, False),
                                              (KeyboardInterrupt, True, False), (AssertionError, True, True)):
        packet = {"restored": False}

        # 案例可能正常、一般失敗或中斷；都由原 suite 的 finally 收尾，不送出服務操作。
        def fail_case(*args, **kwargs):
            if case_type is not None:
                raise case_type(sentinel)
            return 1, 1

        # 恢復失敗保留原 context；明示 cause 與 cycle 驗證入口不會遺漏或無限輸出。
        def fail_cleanup(*args, **kwargs):
            if cleanup_failure:
                error = RuntimeError(sentinel)
                if cycle:
                    error.__cause__ = sys.exception()
                    error.__cause__.__context__ = error
                raise error

        # 只替換 suite 的外部狀態／案例 seams；真 try/finally、CLI catch 與 exit 都照常執行。
        def exercise_suite():
            with patch.object(driver, "preflight_packets"), \
                    patch.object(driver, "counts", return_value=dict.fromkeys(driver.LIMITS, 0)), \
                    patch.object(recovery, "capture_baseline", return_value=(packet, Path("synthetic-recovery.json"))), \
                    patch.object(driver, "run_group", fail_case), patch.object(driver, "restore", fail_cleanup), \
                    patch.object(recovery, "readback"), patch.object(recovery, "save_packet"):
                driver.suite(("laravel",), {"synthetic": True})

        # 使用真正 __main__ 入口；--self-test 只提供不接服務的路徑，不取代 catch 或 reporter。
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli_tests, "run", exercise_suite), \
                patch.object(sys, "argv", ["cq34_http.py", "--self-test"]), \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                runpy.run_path(driver.__file__, run_name="__main__")
            except SystemExit as stopped:
                shared.check(stopped.code == 1, "CLI failure 沒有非零退出")
            else:
                raise AssertionError("CLI failure 意外成功")
        text = errors.getvalue()
        expected = int(case_type is not None) + int(cleanup_failure)
        shared.check(text.count("FAIL CQ34 ") == expected, "CLI 例外鏈筆數不符")
        if case_type is not None:
            shared.check(case_type.__name__ in text and "fail_case" in text, "CLI 遺失原案例類型／位置")
        if cleanup_failure:
            shared.check("RuntimeError" in text and "fail_cleanup" in text, "CLI 遺失恢復失敗類型／位置")
        shared.check(sentinel not in text and "PASS" not in output.getvalue() and not packet["restored"] and
                     shared.CQ34_RECOVERY is None, "CLI 洩漏例外內容、假 PASS 或遺留執行 context")


# 歷史 audit 只准 source 不同；指定 restore、pending 與其他目標身分仍完整比對且不能改舊檔。
def history_tests():
    if shared.PRESET == "ticket57":
        shared.select_preset("cq34")
    identity = {"project": "cq34_test", "root": str(shared.CQ34_ROOT), "source": "a" * 64,
                "keys": {"laravel": "b" * 64}, "images": {"laravel": "sha256:synthetic"},
                "volume": "cq34_test_postgres_data", "volume_created": "2026-11-04T00:00:00Z", "database": "123456789"}
    current = {**identity, "source": "c" * 64}
    packet = {"nonce": "cq34aabbccdd", "settings": {"enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 30},
              "members": [], "account_times": [], "session_times": [], "restored": True}
    receipt = {"namespace": "cq34", "identity": identity, "times": [], "boundary": None, "uncertain": False,
               "state": "restored", "counts_before": dict.fromkeys(driver.LIMITS, 0), "groups": []}
    with tempfile.TemporaryDirectory(prefix="cq34-history-") as folder, chdir(folder):
        driver.RECOVERY.mkdir(mode=0o700, parents=True)
        path = driver.RECOVERY / "recovery-cq34aabbccdd.json"
        recovery.save_packet(packet, path, receipt=receipt)
        before = path.read_bytes()
        # 歷史掃描不得執行工具或 SQL；完整且同來源、或僅來源不同的已完成記錄皆只讀 audit。
        with patch.object(shared.subprocess, "run", side_effect=AssertionError("history 不可執行工具")), \
                patch.object(shared.subprocess, "check_output", side_effect=AssertionError("history 不可執行工具")):
            shared.check(driver.preflight_packets(identity) is None, "同來源歷史 audit 失敗")
            shared.check(driver.preflight_packets(current) is None and path.read_bytes() == before, "歷史 source audit 未只讀通過")
        # 指定 restore 仍須完整 current identity，已恢復記錄也不可重播。
        refused(lambda: driver.preflight_packets(current, path))
        # 即使身分完全相同，已恢復的 baseline 也不能再次寫回。
        refused(lambda: driver.preflight_packets(identity, path))
        for field in identity.keys() - {"source"}:
            different = {**current[field], "laravel": "wrong"} if isinstance(current[field], dict) else "wrong"
            changed = {**current, field: different}
            # root/project/cluster/volume/keys/images 任一不同都不取得歷史 audit 資格。
            refused(lambda: driver.preflight_packets(changed))
        for state, restored, uncertain in (("pending", False, False), ("restoring", False, False), ("incomplete", True, True)):
            packet["restored"], receipt["state"], receipt["uncertain"] = restored, state, uncertain
            recovery.save_packet(packet, path, receipt=receipt)
            # 非完整歷史不能藉 source 差異被忽略。
            refused(lambda: driver.preflight_packets(current))
            # 指定恢復同樣不能跨過來源不符；僅符合原身分的 pending 才可交原 restore。
            refused(lambda: driver.preflight_packets(current, path))
            if not restored:
                shared.check(driver.preflight_packets(identity, path)[0] == packet, "同身分 pending 恢復路徑退步")
        packet["restored"], receipt["state"], receipt["uncertain"] = True, "restored", False
        for source in ("A" * 64, "g" * 64, "short", None):
            receipt["identity"] = {**identity, "source": source}
            recovery.save_packet(packet, path, receipt=receipt)
            # 唯一可不同的 source 仍須是 64 碼小寫 SHA256，不能跳過欄位格式。
            refused(lambda: driver.preflight_packets(current))
        receipt["identity"] = identity
        # 這是可通過原 metadata validator 的合成 function receipt，但仍表示 fixture 未收束。
        receipt["boundary"] = {"table_oid": "10", "functions": [{"namespace": "public", "owner": "articles_owner", "definition": "synthetic",
            "row": {"oid": "20", "proname": "ticket57_http_boundary_shift", "pronargs": 0, "prokind": "f", "proowner": "30",
                    "proacl": None, "prosrc": "synthetic", "prosecdef": True, "proconfig": None}}],
            "triggers": [], "comments": [], "dependencies": []}
        recovery.save_packet(packet, path, receipt=receipt)
        # 有精確 boundary receipt 也不能把仍在途的 fixture 當成已完成歷史。
        refused(lambda: driver.preflight_packets(current))
        receipt["boundary"] = None
        recovery.save_packet(packet, path, receipt=receipt)
        original = json.loads(path.read_text())
        for section, field, value in (("receipt", "namespace", "cq33"), ("baseline", "extra", True)):
            invalid = deepcopy(original)
            invalid[section][field] = value
            body = {"receipt": invalid["receipt"], "baseline": invalid["baseline"]}
            invalid["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            path.write_text(json.dumps(invalid))
            # 即使摘要有效，原 namespace 與 baseline schema 也不能因 audit 被放寬。
            refused(lambda: driver.preflight_packets(current))
        path.write_text(json.dumps({**original, "sha256": "0" * 64}))
        # 身分與狀態正確仍需摘要完整；不修補或覆寫損壞紀錄。
        refused(lambda: driver.preflight_packets(current))


# 真 suite／capture 使用獨立載入的 shared；SQL 只在 subprocess 邊界回覆合成資料。
class CaptureFixture(GuardFixture):
    # 重用固定 target fixture，保留原 guard、schema、capture 與 restore 的完整實作。
    def __init__(self, folder):
        super().__init__(folder)
        with patch.dict(sys.modules, {"ticket57_http": self.subject}):
            spec = importlib.util.spec_from_file_location("cq34_capture_recovery", recovery.__file__)
            self.recovery = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.recovery)
            with patch.dict(sys.modules, {"cq18_http": self.recovery}):
                spec = importlib.util.spec_from_file_location("cq34_capture_driver", driver.__file__)
                self.driver = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self.driver)
        self.statements = []
        self.settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 47}

    # 只接受 capture／空批次 restore 的既有固定 SQL；所有命令仍經真 compose 目標檢查。
    def command(self, args, **kwargs):
        fixed = (*self.subject.compose_argv("exec", "-T", "db", "psql"), "-X", "-q", "-t", "-A",
                 "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles")
        args = tuple(args)
        if args == fixed or args[:len(fixed) + 1] == (*fixed, "-c"):
            statement = kwargs["input"] if args == fixed else args[-1]
            if statement in ("SELECT current_database()||':'||current_user", "SELECT system_identifier FROM pg_control_system()"):
                return super().command(args, **kwargs)
            self.statements.append(statement)
            if statement == self.subject.boundary_catalog_sql():
                return json.dumps({"table_oid": "10", "functions": [], "triggers": [], "comments": [], "dependencies": []})
            if statement.startswith("SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "):
                return "t"
            if statement.startswith("SELECT count(*) FROM "):
                return "0"
            if statement.startswith("SELECT json_build_object('accounts',"):
                return json.dumps(dict.fromkeys(self.driver.LIMITS, 0))
            if statement.startswith("SELECT json_build_object('enabled',"):
                return json.dumps(self.settings)
            if statement.startswith("SELECT coalesce(json_agg(m ORDER BY id),'[]'::json)::text FROM "):
                return "[]"
            # 空批次仍執行真 finally；只准恢復剛讀到的設定，不能採用舊 baseline 的 30 秒。
            if statement.startswith("BEGIN; SET LOCAL lock_timeout='5s';"):
                shared.check("lock_seconds=47 WHERE id=1" in statement and "cq34aabbccdd" not in statement,
                             "capture 重播了歷史 baseline")
                return ""
            raise AssertionError("unexpected capture SQL")
        return super().command(args, **kwargs)


# 從真 suite 穿越兩次 preflight 與 capture，確認 source 修正後仍可安全建立新紀錄。
def capture_tests():
    with tempfile.TemporaryDirectory(prefix="cq34-capture-") as folder:
        fixture = CaptureFixture(folder)
        identity = fixture.guard()
        subject, baseline = fixture.driver, fixture.recovery
        packet = {"nonce": "cq34aabbccdd", "settings": {**fixture.settings, "lock_seconds": 30},
                  "members": [], "account_times": [], "session_times": [], "restored": True}
        receipt = {"namespace": "cq34", "identity": {**identity, "source": "a" * 64}, "times": [], "boundary": None,
                   "uncertain": False, "state": "restored", "counts_before": dict.fromkeys(subject.LIMITS, 0), "groups": []}
        with chdir(fixture.root), patch.object(fixture.subject.subprocess, "run", fixture.process), redirect_stdout(io.StringIO()):
            subject.RECOVERY.mkdir(mode=0o700)
            old = subject.RECOVERY / "recovery-cq34aabbccdd.json"
            baseline.save_packet(packet, old, receipt=receipt)
            before = old.read_bytes()
            real_fsync = os.fsync
            synced_modes = []

            # 保留真 fsync，記錄最早同步的是新 baseline 檔案及父目錄，不能用後續 save 掩蓋缺漏。
            def observe_sync(descriptor):
                synced_modes.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
                return real_fsync(descriptor)

            # 修正來源後可 fresh run；相同來源再跑也只能新增，不能重寫任一歷史 baseline。
            for _ in range(2):
                frozen = {path: path.read_bytes() for path in subject.RECOVERY.iterdir()}
                synced_modes.clear()
                with subject.run_lock(), patch.object(os, "open", wraps=os.open) as opened, \
                        patch.object(os, "fsync", observe_sync):
                    subject.suite((), identity)
                fresh = [path for path in subject.RECOVERY.iterdir() if path not in frozen]
                shared.check(len(fresh) == 1 and all(path.read_bytes() == content for path, content in frozen.items()),
                             "新 baseline 遺失或歷史被覆寫")
                new_packet, new_receipt = subject.load_packet(fresh[0], identity)
                shared.check(new_packet["settings"] == fixture.settings and new_packet["restored"] and
                             new_receipt["state"] == "restored", "新 baseline 未取當前資料或未完成原 finally")
                shared.check(any(call.args == (fresh[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                                 for call in opened.call_args_list) and synced_modes[:2] == [stat.S_IFREG, stat.S_IFDIR] and
                             stat.S_IMODE(fresh[0].stat().st_mode) == 0o600, "新 baseline 缺 exclusive／private／fsync")
            subject.preflight_packets(identity)
            shared.check(old.read_bytes() == before, "完成後 audit 改寫歷史")

            pending = {**receipt, "identity": identity, "state": "pending"}

            # 直接進 capture 再驗一次政策，防止僅 suite 首次 preflight 通過卻留下邊界漏洞。
            def capture():
                return baseline.capture_baseline("cq3401234567", ticket="cq34", receipt=pending,
                                                 preflight=subject.preflight_packets)

            # 每個拒絕都須在 SQL／新檔之前發生；也確認失敗未偷偷修補歷史檔案。
            def reject(action):
                paths = set(subject.RECOVERY.iterdir())
                content, calls = old.read_bytes(), len(fixture.statements)
                refused(action, errors=(AssertionError, KeyError, TypeError, ValueError))
                shared.check(set(subject.RECOVERY.iterdir()) == paths and old.read_bytes() == content and
                             len(fixture.statements) == calls, "失敗 preflight 執行 SQL／建檔／改寫歷史")

            # 即使有完整 receipt，也不能漏傳 CQ34 政策；其他票不能誤用其來源例外。
            reject(lambda: baseline.capture_baseline("cq3401234567", ticket="cq34", receipt=pending))
            # 非 CQ34 callback 必須在原票 SQL 前拒絕。
            reject(lambda: baseline.capture_baseline("cq180123456789ab", preflight=subject.preflight_packets))
            # 指定 restore 保持完整 current identity，完成紀錄不可藉 audit 例外重播。
            reject(lambda: subject.preflight_packets(identity, old))

            # 先改一個身分／狀態／schema 邊界並重算正確摘要，避免反例只撞到 digest 檢查。
            invalids = []
            for field in identity.keys() - {"source"}:
                invalids.append(("identity", field, "wrong"))
            invalids.extend(("identity", "source", source) for source in ("A" * 64, "g" * 64, "short", None))
            invalids.extend((section, field, value) for section, field, value in (
                ("baseline", "restored", False), ("baseline", "restored", 1), ("baseline", "extra", True),
                ("receipt", "state", "pending"), ("receipt", "state", "restoring"), ("receipt", "state", "incomplete"),
                ("receipt", "uncertain", True), ("receipt", "boundary", {}), ("receipt", "namespace", "cq33")))
            for section, field, value in invalids:
                body = {"baseline": deepcopy(packet), "receipt": deepcopy(receipt)}
                target = body["receipt"]["identity"] if section == "identity" else body[section]
                target[field] = value
                digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                old.write_text(json.dumps({**body, "sha256": digest}))
                reject(capture)
                # 真 suite 也必須在 counts 或 baseline SQL 之前拒絕同一組反例。
                reject(lambda: subject.suite((), identity))

            # 損壞 JSON、外層欄位與 digest 都不能被完成狀態掩蓋。
            for content in ("{", "{}", json.dumps({**json.loads(before), "sha256": "0" * 64})):
                old.write_text(content)
                reject(capture)
            old.write_bytes(before)
            # 檔案與目錄各自保持 private；拒絕 unknown 檔案及 symlink 而不刪除它們。
            for path, mode in ((old, 0o644), (subject.RECOVERY, 0o755)):
                original_mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode)
                reject(capture)
                path.chmod(original_mode)
            unknown = subject.RECOVERY / "pending.tmp"
            unknown.write_text("unknown")
            reject(capture)
            unknown.unlink()
            outside = Path("history.json")
            outside.write_bytes(before)
            outside.chmod(0o600)
            old.unlink()
            old.symlink_to("../../history.json")
            reject(capture)
            old.unlink()
            old.write_bytes(before)
            old.chmod(0o600)
            subject.preflight_packets(identity)
            shared.check(old.read_bytes() == before, "反例完成後歷史 bytes 不符")


# 只在 body read 中斷；header parser 仍讀真正 BytesIO，保留送出後中斷的因果邊界。
class InterruptedBody(io.BytesIO):
    # HTTPResponse.read 會走這個 I/O seam，不替換 request／例外處理或未知旗標。
    def read(self, *args):
        raise KeyboardInterrupt("cq34-transport-sentinel")


# 只提供 HTTPResponse 所需的 makefile，讓 stdlib 真正解析 status、headers 及 Content-Length。
class WireSocket:
    # 完全合成的 wire bytes 不使用 socket，也不讀取任何帳號或 key。
    def __init__(self, wire, interrupt=False):
        self.stream = InterruptedBody(wire) if interrupt else io.BytesIO(wire)

    # 真 parser 使用相同 stream 讀 header/body，避免自製例外冒充協定截斷。
    def makefile(self, mode):
        return self.stream


# 只在 opener I/O 邊界提供真協定回應；完整 HTTPError 與未知 transport 必須分開處理。
def wire_response(request, mode):
    if mode == "interrupt-open":
        raise KeyboardInterrupt("cq34-transport-sentinel")
    if mode == "complete-error":
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        body = {"success": False, "message": shared.MESSAGES["E0001"], "error_code": "E0001",
                "log_id": "00000000-0000-4000-8000-000000000001", "errors": {"email": ["此欄位為必填"]}}
        raise HTTPError(request.full_url, 422, "synthetic complete error", headers, io.BytesIO(json.dumps(body).encode()))
    body = b'{"probe":"cq34-transport-sentinel"}'
    if mode == "bad-status":
        wire = b"cq34-transport-sentinel\r\n\r\n"
    else:
        length = len(body) + (1 if mode == "short-body" else 0)
        wire = (b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                b"Access-Control-Allow-Origin: http://localhost:8080\r\nContent-Length: " + str(length).encode() + b"\r\n\r\n" + body)
    response = HTTPResponse(WireSocket(wire, interrupt=mode == "interrupt-read"))
    response.begin()
    return response


# 真 guard／suite／capture／finally 走到底；未知寫入必須落盤 incomplete，不能被 cleanup 清回成功。
def transport_tests():
    failures = []
    cases = (("short-body", IncompleteRead), ("bad-status", BadStatusLine),
             ("interrupt-open", KeyboardInterrupt), ("interrupt-read", KeyboardInterrupt))
    for mode, error_type in cases:
        for writing in (True, False):
            with tempfile.TemporaryDirectory(prefix="cq34-transport-") as folder:
                fixture = CaptureFixture(folder)
                identity = fixture.guard()
                subject, baseline, target = fixture.driver, fixture.recovery, fixture.subject
                observed = {"checked": False, "blocked": False, "safe_clean": True}
                calls = []

                # 只 mock opener，不替換 guard/SAFE/HTTP caller；安全 GET 與寫入各走相同真 parser。
                def opened(request, **kwargs):
                    calls.append(request.get_method())
                    return wire_response(request, mode if writing or request.get_method() == "GET" else "complete-error")

                # cleanup 的第一個真 SQL I/O 邊界仍有 active context，可檢查下一筆寫入是否在送出前拒絕。
                def process(args, **kwargs):
                    active = target.CQ34_RECOVERY
                    if active is not None and active[2]["state"] == "restoring" and not observed["checked"]:
                        observed["checked"] = True
                        if writing:
                            try:
                                target.request("laravel", "POST", "/api/articles", {"title": "synthetic", "body": "synthetic"})
                            except AssertionError as error:
                                observed["blocked"] = str(error) == "CQ34 前筆寫入結果未知，停止新寫入"
                            except (HTTPException, KeyboardInterrupt):
                                # 修正前會再次進 opener；只記反例，不讓探測失敗掩蓋原 finally 的落盤結果。
                                pass
                        else:
                            try:
                                target.request("laravel", "GET", "/api/me")
                            except error_type:
                                pass
                            else:
                                raise AssertionError("安全 GET 未到達指定 transport 邊界")
                            observed["safe_clean"] = not active[2]["uncertain"]
                    return fixture.process(args, **kwargs)

                # 所有工具仍受 CaptureFixture 的固定命令白名單限制；save_packet 正常寫入／fsync 暫存檔。
                output = io.StringIO()
                with chdir(fixture.root), patch.dict(sys.modules, {"cq18_http": baseline}), \
                        patch.object(target.subprocess, "run", process), patch.object(target.CQ34_OPENER, "open", opened), \
                        redirect_stdout(output):
                    expected = error_type if writing else AssertionError
                    try:
                        with subject.run_lock():
                            subject.suite(("laravel",), identity)
                    except expected as error:
                        if not writing:
                            shared.check(str(error) == "HTTP 422，預期 201/None", "完整 HTTPError 未正常轉成可斷言回應")
                    else:
                        raise AssertionError("transport suite 意外成功")
                    paths = list(subject.RECOVERY.glob("recovery-*.json"))
                    shared.check(len(paths) == 1, "transport probe 沒有唯一 baseline")
                    packet, receipt = subject.load_packet(paths[0], identity)
                    state = "incomplete" if writing else "restored"
                    if not (packet["restored"] and receipt["state"] == state and receipt["uncertain"] is writing):
                        failures.append(mode + ("/write" if writing else "/GET") + ": cleanup 狀態不符")
                    if writing:
                        try:
                            subject.preflight_packets(identity)
                        except AssertionError as error:
                            shared.check(str(error) == "CQ34 上輪 pending／未知結果；先由 Main 核對", "preflight 因其他錯誤遮蔽未知狀態")
                        else:
                            failures.append(mode + ": 下一輪未拒絕")
                        if not observed["blocked"] or calls.count("POST") != 1:
                            failures.append(mode + ": 未在下一筆送出前拒絕")
                        if "INCOMPLETE CQ34" not in output.getvalue():
                            failures.append(mode + ": 未回報 incomplete")
                    else:
                        subject.preflight_packets(identity)
                        shared.check(observed["safe_clean"] and calls == ["POST", "GET"], "安全 GET 或完整 HTTPError 汙染未知狀態")
                    shared.check(observed["checked"] and target.CQ34_RECOVERY is None and
                                 "PASS CQ34 1 後端" not in output.getvalue() and
                                 "cq34-transport-sentinel" not in output.getvalue(), "transport probe 遺失 finally／洩漏內容／假 PASS")
    shared.check(not failures, "；".join(failures))


# 只替換 log reader I/O，驗證真 trace_errors 保留共同追查規則及各版現有格式。
def trace_tests():
    identifier = "f260a2e1-0824-44ce-8e9c-b95dbe477842"
    observed = [(identifier, "GET", "/api/me", 422)]
    # Rails 合法安全格式不含 URL；固定 UUID 與欄位形狀來自實際保留 trace。
    lines = [f"I, [2026-11-22T16:46:37 #1] INFO -- : IN log_id={identifier} method=GET",
             f"W, [2026-11-22T16:46:37 #1] WARN -- : EX log_id={identifier} status=422",
             f"I, [2026-11-22T16:46:37 #1] INFO -- : OUT log_id={identifier} status=422"]
    with patch.object(shared, "api_log_lines", return_value=lines):
        shared.trace_errors("rails", observed)
        # 同輪兩筆錯誤不能共用識別碼，即使各自都能找到完整 trace。
        refused(lambda: shared.trace_errors("rails", observed * 2))

    # 每個反例只變一個事件／欄位，不能因不記 URL 就放寬同次請求的對應。
    invalid = [
        lines[1:], lines[:1] + lines[2:], lines[:2],
        [lines[0], *lines], [*lines, lines[2]],
        [lines[1], lines[0], lines[2]],
        [lines[0], f"NOTE log_id={identifier}", f"NOTE log_id={identifier}", *lines[1:]],
        [lines[0].replace(identifier, "00000000-0000-4000-8000-000000000000"), *lines[1:]],
        [lines[0].replace(f"log_id={identifier}", f"log_id=prefix-{identifier}"), *lines[1:]],
        [lines[0].replace("method=GET", "method=POST"), *lines[1:]],
        [lines[0], lines[1].replace("status=422", "status=401"), lines[2]],
        [*lines[:2], lines[2].replace("status=422", "status=401")],
        [lines[0] + " path=/api/me", *lines[1:]],
        [lines[0].replace("method=GET", "method=GETTING"), *lines[1:]],
        [*lines[:2], lines[2].replace("status=422", "status=4220")],
    ]
    for changed in invalid:
        with patch.object(shared, "api_log_lines", return_value=changed):
            # 只讀合成 log；缺 OUT 仍走原有限等待，不修改時間或追查函式。
            refused(lambda: shared.trace_errors("rails", observed))

    # 原十二版仍須 path、method、status 與事件對應，Rails 格式不能成為通用豁免。
    for backend in shared.BACKENDS:
        if backend == "rails":
            continue
        legacy = [f"IN log_id={identifier} GET /api/me", lines[1], lines[2]]
        with patch.object(shared, "api_log_lines", return_value=legacy):
            shared.trace_errors(backend, observed)
        with patch.object(shared, "api_log_lines", return_value=lines):
            # 即使其他欄位齊全，原十二版缺 path 仍必須拒絕。
            refused(lambda: shared.trace_errors(backend, observed))


# 所有自測都必須在不執行服務的條件下完成，測試錯誤不轉成成功訊息。
def run():
    guard_tests()
    trace_tests()
    # argparse 錯誤是預期非零；捕捉輸出避免 help 噪音掩蓋真自測結果。
    with patch("sys.stderr", new=io.StringIO()):
        for argv in ([], ["--unknown"], ["--backend", "wrong"], ["--all", "--guard"], ["--restore", ".local/cq33-recovery/recovery-cq34aabbccdd.json"]):
            # callback 保留本次 argv，驗證未知 CLI 在任何 guard 前即拒絕。
            refused(lambda: driver.arguments(argv), errors=(SystemExit,))
    recovery_tests()
    reporting_tests()
    history_tests()
    capture_tests()
    transport_tests()
    print("PASS CQ34 離線 fail-closed：真 guard 正反例、有限 CLI、private recovery 摘要／pending／namespace／replay", flush=True)


if __name__ == "__main__":
    run()
