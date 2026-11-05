"""固定 PERF01 隔離量測入口；只用標準庫，所有未知寫入結果保留 receipt。"""

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
import fcntl
from functools import lru_cache
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

from perf01_cases import (ADMIN_EMAIL, BACKENDS, LEVELS, MEMBER_EMAIL, PASSWORD,
                          cases, fixture, verify_response)

VERSION = "1"
HEAD = "4cf8256e2fdd802297c603a17be22dbf485e0dcc"
PROJECT = "perf01_test"
VOLUME = "perf01_test_postgres_data"
NETWORK = "perf01_test_default"
OWNER_LABEL = "org.perf01.owner"
SUBNET = "10.251.135.0/24"
DOCKER_ENDPOINT = "unix:///Users/ksy/.orbstack/run/docker.sock"
IMAGE = "postgres:18.6-alpine3.24@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"
FILES = ("perf01_http.py", "perf01_cases.py", "perf01_test.py", "perf01_compose.yaml")
# ASP.NET Dockerfile 只 COPY csproj、lock 與頂層 *.cs；這四個自動建置輸出不是映像輸入。
DOTNET_OUTPUTS = ("backends/aspnet-core/obj", "backends/aspnet-core/bin",
                  "backends/aspnet-core/checks/obj", "backends/aspnet-core/checks/bin")
METRICS = ("calls", "total_exec_time", "rows", "shared_blks_hit", "shared_blks_read",
           "shared_blks_dirtied", "shared_blks_written", "temp_blks_read", "temp_blks_written")
# Npgsql 10.0.3／PostgreSQL 18.6 啟動 batch 的實際 SQL 與 pg_stat_statements 代表語句 SHA256。
# 依 version、type、composite、enum 順序逐項核對，避免用最後一個日誌 query ID 代表四個查詢。
NPGSQL_STARTUP_QUERIES = (
    ("df1f1d15d442789a5b9c81cdddf44d88d5748499cc48865023ddc1ff1587d0f6", "df1f1d15d442789a5b9c81cdddf44d88d5748499cc48865023ddc1ff1587d0f6"),
    ("677b22aea3c6b1f1ec4ec82abeed45282a4726f752ece2845f5074053cabc2ae", "150a155d17bc53d8887fda6200da29d3420ed14f42ab738e379320bc15f26495"),
    ("a5bb2f511cb83dfff113d69f4510398a4f6f401575fadff3c02cadce5aa7bfcc", "efd9182deb785aa95663c063d1cfb3b329a8046e066f877c74098bd115fa7d45"),
    ("f1ca32b6ace17e361a4881f6921e9ac524857c364f58206a7478d0077daa388b", "4766d2a7ea119accd66685ca4e3c2319ce25f089d9d3b102c63ae900efdab1c7"),
)
# 本次 pgx ping 修正僅承接這個來源的六個已驗證小量單位，其他來源／級別另行處置。
PING_CARRY_SOURCE = "14e48b8c2d6a79c9e18351bfbd5ff1bfef086e2f52ea696b4f50c9c79fac46e4"
PING_CARRY_UNITS = frozenset(("small/1/laravel", "small/1/symfony", "small/1/aspnet-core",
                             "small/1/django", "small/1/flask", "small/1/fastapi"))
# NestJS 時間查詢修正承接此來源的十一組，其中六組仍保留上一代原始量測來源。
CLOCK_CARRY_SOURCE = "d6b4a55244f8ba85a9ffc3be267ebbcfa436978124c83cb21e78f2abd56e0907"
CLOCK_CARRY_UNITS = PING_CARRY_UNITS | frozenset(("small/1/gin", "small/1/echo", "small/1/fiber",
                                                "small/1/express", "small/1/fastify"))
ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / ".local/perf01-fixture"
EVIDENCE = TARGET / ".local/perf01-evidence"


# Failure 僅承載可公開的固定診斷，外部工具原始輸出另存私密檔。
class Failure(RuntimeError):
    pass


# HTTPTransportFailure 保留可公開的傳輸類別，讓唯讀 readiness 精確辨識可重試故障。
class HTTPTransportFailure(Failure):
    # __init__ 只公開例外型別，避免帶出回應、token 或原始例外訊息。
    def __init__(self, stage, error):
        cause = error.reason if isinstance(error, urllib.error.URLError) else error
        self.retryable = isinstance(cause, (ConnectionError, TimeoutError, http.client.IncompleteRead))
        super().__init__(f"HTTP {stage}結果未知（{type(cause).__name__}），禁止自動重送寫入")


# require 在任何外部副作用前拒絕不符條件；不用可被 -O 關閉的 assert 當安全 guard。
def require(condition, message):
    if not condition:
        raise Failure(message)


# digest 綁定實際位元組，避免只用檔名或 tag 宣稱來源相同。
def digest(value):
    return hashlib.sha256(value).hexdigest()


# encoded 提供固定 JSON 位元組，供 receipt 與來源 manifest 雜湊共用。
def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


# private_dir 拒絕 symlink 並固定私密權限，避免原始 bind／憑證落在公開路徑。
def private_dir(path):
    require(not any(parent.is_symlink() for parent in (path, *path.parents)), "私密目錄及父路徑不可為 symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


# save 原子置換自有 JSON／私密輸出，fsync 後才將 pending 寫入視為持久化。
def save(path, value, exclusive=False):
    require(not path.is_symlink() and not path.with_suffix(path.suffix + ".pending").exists(), "私密檔或 pending 路徑異常")
    if exclusive:
        require(not path.exists(), "自有檔已存在，拒絕覆寫")
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(value if isinstance(value, bytes) else encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    # 目錄同步確保 receipt 名稱在程序／主機中斷後仍可供保守恢復。
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# load 只讀私密的普通檔，不接受外部掛入的 receipt 或金鑰。
def load(path):
    require(path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600,
            "缺少私密普通檔或權限不符")
    return json.loads(path.read_bytes())


# tool 捕捉所有輸出；失敗只回固定階段及私密診斷檔名，不把 SQL／密碼印到終端。
def tool(arguments, payload=None, cwd=ROOT, timeout=120):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("COMPOSE_", "DOCKER_", "PG")) and key not in ("DB_OWNER_PASSWORD", "DB_APP_PASSWORD", "APP_KEY", "PERF01_SOURCE")}
    try:
        result = subprocess.run(arguments, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd, env=environment, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        # timeout 不代表寫入未送出，呼叫者的 pending receipt 不會被移除。
        if EVIDENCE.is_dir():
            save(EVIDENCE / ("transport-" + secrets.token_hex(6) + ".json"), {"type": type(error).__name__})
        raise Failure("外部工具傳輸結果未知；保留 pending，請先查核私密證據") from None
    if result.returncode:
        if EVIDENCE.is_dir():
            name = "tool-" + secrets.token_hex(6) + ".log"
            save(EVIDENCE / name, result.stdout + b"\n" + result.stderr)
            raise Failure("外部工具失敗；私密診斷 " + name)
        raise Failure("準備前外部工具失敗；尚未允許 DB 寫入")
    return result.stdout


# docker 將唯讀檢查及寫入固定在核准的本機 OrbStack socket，環境不能改選 daemon。
def docker(*arguments, payload=None, timeout=120):
    return tool(["docker", "--host", DOCKER_ENDPOINT, *arguments], payload, timeout=timeout)


# compose 固定來源目錄、env 與兩個檔案，避免 ambient COMPOSE_* 指向舊環境。
def compose(*arguments, payload=None, timeout=120):
    return docker("compose", "--project-directory", str(TARGET), "--env-file", str(TARGET / ".env"),
                  "--project-name", PROJECT, "--file", str(TARGET / "compose.yaml"),
                  "--file", str(TARGET / "ops/tests/perf01_compose.yaml"), "--profile", "*",
                  *arguments, payload=payload, timeout=timeout)


# json_docker 保留 Docker JSON 的精確結構，拒絕文字 grep 猜測資源身分。
def json_docker(*arguments):
    return json.loads(docker(*arguments))


# preflight 僅讀 daemon metadata 與路由並短暫 bind 新埠；從不停止衝突資源。
def preflight():
    require(not docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT).strip(), "新 project 名稱已被使用")
    require(not docker("volume", "ls", "-q", "--filter", "name=^" + VOLUME + "$").strip(), "新 volume 名稱已被使用")
    networks = docker("network", "ls", "-q").decode().split()
    if networks:
        for network in json_docker("network", "inspect", *networks):
            require(network["Name"] != NETWORK, "新 network 名稱已被使用")
            ipam = network.get("IPAM")
            require(isinstance(ipam, dict), "Docker network IPAM metadata 不完整")
            configs = ipam.get("Config")
            # Docker 內建 host／none 不配置 subnet，合法的 null Config 不代表 bridge 資料也可省略。
            if (network["Name"], network.get("Driver")) in (("host", "host"), ("none", "null")) and configs in (None, []):
                continue
            require(isinstance(configs, list) and configs, "Docker network 缺少可核對的 subnet")
            for config in configs:
                require(isinstance(config, dict) and isinstance(config.get("Subnet"), str) and config["Subnet"], "Docker network subnet metadata 不完整")
                try:
                    subnet = ipaddress.ip_network(config["Subnet"])
                except ValueError:
                    raise Failure("Docker network subnet 格式不符") from None
                if subnet.version == 4:
                    require(not ipaddress.ip_network(SUBNET).overlaps(subnet), "新 subnet 與現有 Docker network 衝突")
    # 本專案主機是 macOS；只辨識 candidate 路由，無法解析時不宣稱安全。
    routes = tool(["netstat", "-rn", "-f", "inet"]).decode()
    route_guard(routes)
    for port in range(18501, 18513):
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
        except OSError:
            raise Failure("專用 loopback 埠已被使用；不停止其他資源") from None


# route_guard 展開 macOS 縮寫網段，連較廣的 10/8 或 10.251/16 路由也拒絕。
def route_guard(routes):
    require("Destination" in routes, "無法辨識 IPv4 路由表")
    for line in routes.splitlines():
        fields = line.split()
        if not fields or not re.match(r"^[0-9]", fields[0]):
            continue
        destination = fields[0]
        try:
            address, separator, prefix = destination.partition("/")
            parts = address.split(".")
            require(1 <= len(parts) <= 4 and all(part.isdecimal() for part in parts), "無法解析 IPv4 路由")
            network = ipaddress.ip_network(".".join(parts + ["0"] * (4 - len(parts))) + "/" + (prefix if separator else str(len(parts) * 8)), strict=False)
        except ValueError:
            raise Failure("無法解析 IPv4 路由；拒絕猜測") from None
        require(not network.overlaps(ipaddress.ip_network(SUBNET)), "新 subnet 與現有 IPv4 路由衝突")


# source_files 驗證全部產品與量測來源，只略過已核對不進映像的固定 .NET 產生目錄。
def source_files():
    found = {}
    for path in TARGET.rglob("*"):
        relative = path.relative_to(TARGET).as_posix()
        if relative == ".env" or relative.startswith(".local/") or relative == ".local":
            continue
        require(not path.is_symlink(), "來源副本出現 symlink")
        if any(relative == directory or relative.startswith(directory + "/") for directory in DOTNET_OUTPUTS):
            continue
        if path.is_file():
            found[relative] = {"sha256": digest(path.read_bytes()), "mode": stat.S_IMODE(path.stat().st_mode)}
    return found


# prepare 由固定 git HEAD 建新副本，再加入本次量測檔；不複製 dirty worktree 或舊私密目錄。
def prepare():
    require(not TARGET.exists() and not TARGET.is_symlink(), "新 fixture 目錄已存在；拒絕覆寫或重新產生 keys")
    require(tool(["git", "rev-parse", "HEAD"]).decode().strip() == HEAD, "來源 HEAD 不符已核准基準")
    preflight()
    private_dir(TARGET)
    private_dir(EVIDENCE)
    save(EVIDENCE / "prepare-receipt.json", {"state": "pending", "head": HEAD, "project": PROJECT}, exclusive=True)
    archive = tool(["git", "archive", "--format=tar", HEAD])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for entry in bundle:
            path = Path(entry.name)
            # 僅取應用追蹤來源，封鎖本機資料、workflow 草稿與可能的私密檔。
            if path.parts[0] in (".git", ".local", ".scratch", "docs", ".agents", ".codex") or any(part.startswith(".env") for part in path.parts) or path.suffix == ".key":
                continue
            require(not path.is_absolute() and ".." not in path.parts and (entry.isfile() or entry.isdir()), "git archive 含不允許的路徑或型態")
            destination = TARGET / path
            if entry.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.extractfile(entry).read())
                os.chmod(destination, entry.mode)
    for filename in FILES:
        source = ROOT / "ops/tests" / filename
        destination = TARGET / "ops/tests" / filename
        destination.write_bytes(source.read_bytes())
        os.chmod(destination, 0o644)
    manifest = source_files()
    identity = digest(encoded(manifest))
    credentials = {"DB_OWNER_PASSWORD": secrets.token_hex(32), "DB_APP_PASSWORD": secrets.token_hex(32),
                   "APP_KEY": "base64:" + base64.b64encode(secrets.token_bytes(32)).decode(), "PERF01_SOURCE": identity}
    private_dir(TARGET / ".local/keys")
    keys = {}
    for backend in BACKENDS:
        path = TARGET / ".local/keys" / (backend + ".key")
        save(path, base64.b64encode(secrets.token_bytes(32)) + b"\n", exclusive=True)
        keys[backend] = digest(path.read_bytes())
    for name in ("backups", "laravel-logs", "symfony-logs"):
        private_dir(TARGET / ".local" / name)
    save(TARGET / ".env", "".join(key + "=" + value + "\n" for key, value in credentials.items()).encode(), exclusive=True)
    save(EVIDENCE / "state.json", {"head": HEAD, "source": identity, "manifest": manifest, "keys": keys,
                                  "env_hash": digest((TARGET / ".env").read_bytes()), "operations": [], "images": {},
                                  "cluster": None, "initialized": False, "level": None, "completed": [], "calibrated": False}, exclusive=True)
    save(EVIDENCE / "prepare-receipt.json", {"state": "complete", "head": HEAD, "source": identity, "project": PROJECT})
    print(json.dumps({"phase": "prepare", "source": identity, "head": HEAD, "status": "complete"}))


# state 核對 receipt、來源與 keys，所有後續命令都由這個固定身分開始。
def state(allow_pending=False, refreshing=False):
    require(not any(parent.is_symlink() for parent in (EVIDENCE, *EVIDENCE.parents)), "fixture 父路徑出現 symlink")
    require(TARGET.is_dir() and not TARGET.is_symlink() and EVIDENCE.is_dir() and not EVIDENCE.is_symlink(), "固定 fixture／證據目錄不存在或型態不符")
    require(load(EVIDENCE / "prepare-receipt.json")["state"] == "complete", "prepare 未完成；拒絕重送")
    value = load(EVIDENCE / "state.json")
    require(value["head"] == HEAD and source_files() == value["manifest"] and digest(encoded(value["manifest"])) == value["source"], "來源 manifest／HEAD 不符")
    if not refreshing:
        for filename in FILES:
            source = ROOT / "ops/tests" / filename
            require(source.is_file() and not source.is_symlink() and digest(source.read_bytes()) == value["manifest"].get("ops/tests/" + filename, {}).get("sha256"), "執行端量測檔尚未與 fixture 來源資格同步，拒絕使用舊 source 身分")
    require(digest((TARGET / ".env").read_bytes()) == value["env_hash"], "專用 env 已變更")
    key_hashes = []
    for backend, expected in value["keys"].items():
        path = TARGET / ".local/keys" / (backend + ".key")
        require(not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600, "專用 key 型態或模式不符")
        key_hashes.append(digest(path.read_bytes()))
        require(key_hashes[-1] == expected, "專用 key 身分不符")
    require(len(set(key_hashes)) == 12, "十二把 key 必須不同")
    if not allow_pending:
        require(not any(item["state"] == "pending" for item in value["operations"]), "存在 pending／unknown receipt；僅允許 stop 限定恢復，禁止重送寫入")
    return value


# refresh 只更新四個量測檔；初始化後須先證明精確自有環境已恢復且全部停止，產品與持久身分不變。
def refresh():
    value = state(refreshing=True)
    if value["initialized"]:
        stopped_refresh_guard(value)
    else:
        require(value["cluster"] is None and value["operations"] == [] and not value["images"]
                and value["level"] is None and value["completed"] == [] and not value["calibrated"], "refresh 僅限尚未初始化、無操作紀錄的新 fixture")
        preflight()
    replacements = {}
    new_manifest = dict(value["manifest"])
    changes = {}
    for filename in FILES:
        source = ROOT / "ops/tests" / filename
        require(source.is_file() and not any(path.is_symlink() for path in (source, *source.parents)), "refresh 來源不是可信普通檔")
        relative = "ops/tests/" + filename
        require(relative in value["manifest"], "原 manifest 缺少固定量測檔，拒絕補造來源")
        replacements[relative] = source.read_bytes()
        metadata = {"sha256": digest(replacements[relative]), "mode": 0o644}
        changes[relative] = {"old": value["manifest"][relative], "new": metadata}
        new_manifest[relative] = metadata
    new_source = digest(encoded(new_manifest))
    require(new_source != value["source"], "四個量測檔未變更，不需要 refresh")
    superseded = {}
    carried = {}
    if value["completed"]:
        if value["source"] in (PING_CARRY_SOURCE, CLOCK_CARRY_SOURCE):
            # 兩次修正只承接各自核准的固定小量單位，逐案核對原始窗口、索引與未變更的量測輸入。
            clock_carry = value["source"] == CLOCK_CARRY_SOURCE
            allowed = CLOCK_CARRY_UNITS if clock_carry else PING_CARRY_UNITS
            require(value["level"] == "small" and len(value["completed"]) == len(allowed)
                    and set(value["completed"]) == set(value["unit_results"]) == allowed, "既有單位不在本次承接範圍")
            for filename in ("perf01_cases.py", "perf01_compose.yaml"):
                change = changes["ops/tests/" + filename]
                require(change["old"] == change["new"], "案例或 Compose 已改變，拒絕承接舊量測")
            for unit in value["completed"]:
                if clock_carry:
                    verified = verify_unit(value, unit, audit_no_clock=True)
                else:
                    verified = verify_unit(value, unit, audit_no_ping=True)
                carried[unit] = {**value["unit_results"][unit], "source": verified.get("source", value["source"])}
        else:
            # 保留先前兩個小量單位的限縮重驗規則；原索引與窗口不會被覆寫。
            allowed = {"small/1/laravel", "small/1/symfony"}
            require(value["level"] == "small" and len(value["completed"]) == 2 and set(value["completed"]) == allowed
                    and set(value["unit_results"]) == allowed, "既有完成單位不在本次小量重驗範圍")
            superseded = {unit: verify_unit(value, unit) for unit in value["completed"]}
    old_env = (TARGET / ".env").read_bytes()
    lines = old_env.splitlines(keepends=True)
    source_lines = [index for index, line in enumerate(lines) if line.startswith(b"PERF01_SOURCE=")]
    require(len(source_lines) == 1 and lines[source_lines[0]].strip() == b"PERF01_SOURCE=" + value["source"].encode(), "原 env source 身分不符")
    lines[source_lines[0]] = b"PERF01_SOURCE=" + new_source.encode() + b"\n"
    new_env = b"".join(lines)
    detail = {"head": HEAD, "old_source": value["source"], "new_source": new_source, "files": changes,
              "old_env_hash": value["env_hash"], "new_env_hash": digest(new_env), "keys": value["keys"],
              "cluster": value["cluster"], "db_id": value.get("db_id"), "db_image": value.get("db_image"),
              "calibration_invalidated": value["calibrated"], "superseded_units": superseded, "carried_units": carried}
    # 先持久化 pending；任何部分同步失敗都保留舊／新身分，不自動重試或重產憑證。
    with mutation(value, "stopped-fixture-refresh" if value["initialized"] else "pre-init-refresh", detail):
        audit = EVIDENCE / ("refresh-" + str(time.time_ns()) + ".json")
        save(audit, {"state": "pending", **detail}, exclusive=True)
        if value["calibrated"]:
            archive_calibration(value["source"], (EVIDENCE / "calibration.json").read_bytes())
        for relative, contents in replacements.items():
            destination = TARGET / relative
            save(destination, contents)
            os.chmod(destination, 0o644)
        save(TARGET / ".env", new_env)
        require(source_files() == new_manifest, "refresh 後來源 readback 不符")
        for backend, expected in value["keys"].items():
            require(digest((TARGET / ".local/keys" / (backend + ".key")).read_bytes()) == expected, "refresh 不得變更 keys")
        # 工具來源不同一律重新校準；舊 PASS 只作歷史證據，不能授予新 run 資格。
        value.update(source=new_source, manifest=new_manifest, env_hash=digest(new_env), calibrated=False)
        if superseded:
            value["completed"] = []
            value["unit_results"] = {}
        save(audit, {"state": "complete", **detail})
    print(json.dumps({"phase": "refresh", "status": "complete", "old_source": detail["old_source"], "source": new_source}))


# stopped_refresh_guard 以既有恢復 receipt 及 Docker metadata 證明更新資格，不啟動或查詢停止中的 DB。
def stopped_refresh_guard(value):
    require(value["cluster"] is not None and value.get("logging") is False and value.get("logging_original") == [], "停止 fixture 的 cluster／logging 恢復身分不完整")
    logging_operations = [operation for operation in value["operations"] if operation["kind"] == "logging"]
    require(logging_operations and logging_operations[-1]["state"] == "complete" and logging_operations[-1]["detail"] == {"enabled": False, "original": value["logging_original"]}
            and logging_operations[-1]["cluster"] == value["cluster"], "缺少同 cluster 的 logging 恢復 readback receipt")
    recoveries = [load(path) for path in EVIDENCE.glob("recovery-*.json")]
    require(recoveries and not list(EVIDENCE.glob("recovery-*.json.pending")) and all(receipt.get("state") == "complete" and receipt.get("cluster") == value["cluster"]
            and receipt.get("action") == "restore-logging-and-stop" for receipt in recoveries), "停止恢復 receipt 未完成或身分不符")
    found = container_guard(value, running=False)
    require(all(not container["State"]["Running"] for container in found.values()), "refresh 前所有自有容器必須停止")
    require(found["db"]["Id"] == value.get("db_id") and found["db"]["Image"] == value.get("db_image"), "refresh 的精確 DB container／image 已改變")


# mutation 在外部操作前持久化 intent；失敗不刪 pending，也不假設失敗代表沒有寫入。
@contextmanager
def mutation(value, kind, detail):
    require(not any(item["state"] == "pending" for item in value["operations"]), "尚有未知操作，拒絕新寫入")
    operation = {"kind": kind, "detail": detail, "state": "pending", "source": value["source"],
                 "cluster": value["cluster"], "time": time.time_ns()}
    value["operations"].append(operation)
    save(EVIDENCE / "state.json", value)
    yield operation
    operation["state"] = "complete"
    save(EVIDENCE / "state.json", value)


# config_guard 核對 merge 後真實設定；舊埠不得追加，db 不可發布且 app healthcheck 必須停用。
def config_guard(value):
    config = json.loads(compose("config", "--format", "json"))
    require(config["name"] == PROJECT, "rendered compose project 不符")
    require(config["volumes"]["postgres_data"]["name"] == VOLUME and not config["volumes"]["postgres_data"].get("external"), "rendered volume 不符")
    network = config["networks"]["default"]
    require(network["name"] == NETWORK and network["ipam"]["config"] == [{"subnet": SUBNET}], "rendered network 不符")
    require(config["services"]["db"]["image"] == IMAGE and not config["services"]["db"].get("ports"), "rendered DB image／ports 不符")
    value["db_command"] = config["services"]["db"]["command"]
    # Compose 只列出帶 config-hash 的容器；沿用官方輸出，避免自行仿造 hash 演算法。
    db_hash = compose("config", "--hash", "db").decode().split()
    require(len(db_hash) == 2 and db_hash[0] == "db" and re.fullmatch(r"[a-f0-9]{64}", db_hash[1]), "rendered DB config hash 不符")
    value["db_config_hash"] = db_hash[1]
    for index, backend in enumerate(BACKENDS):
        service = config["services"][backend]
        ports = service.get("ports", [])
        require(len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1" and str(ports[0]["published"]) == str(18501 + index)
                and ports[0]["target"] == (8080 if backend == "aspnet-core" else 8000), "rendered backend ports 不符")
        require(service["healthcheck"].get("disable") is True and service["image"] == f"perf01_test_{backend}:{value['source']}", "rendered image／healthcheck 不符")
        require(service["build"]["context"] == str(TARGET) and service["build"]["labels"]["org.perf01.source"] == value["source"], "build context／source label 不符")
    save(EVIDENCE / "rendered-config.json", config)
    return config


# claimed_db 核對建立時保存的 nonce、不可變 ID 與 receipt；舊式靜態標籤紀錄不升格成 ownership。
def claimed_db(value):
    db_id = value.get("db_id")
    db_image = value.get("db_image")
    ownership = value.get("ownership", {})
    require(isinstance(db_id, str) and re.fullmatch(r"[a-f0-9]{64}", db_id) and db_image
            and re.fullmatch(r"[a-f0-9]{64}", ownership.get("nonce", ""))
            and re.fullmatch(r"[a-f0-9]{64}", ownership.get("network_id", ""))
            and ownership.get("volume_created") and ownership.get("volume_mountpoint"), "缺少固定 DB 的啟動身分")
    expected = {"project": PROJECT, "volume": VOLUME, "db_id": db_id, "image": db_image, "ownership": ownership}
    starts = [item for item in value["operations"] if item["kind"] == "db-start"]
    require(len(starts) == 1 and starts[0]["detail"] == expected, "缺少精確建立身分的 db-start receipt")
    return db_id


# owned_db 只有完整啟動與 cluster 讀回才准許 resume／stop；未知建立結果不能被補認領。
def owned_db(value):
    db_id = claimed_db(value)
    cluster = value.get("cluster")
    require(isinstance(cluster, dict) and isinstance(cluster.get("system"), str) and re.fullmatch(r"[0-9]+", cluster["system"])
            and cluster.get("database") == "articles" and cluster.get("role") == "articles_owner", "缺少固定 DB 的啟動身分")
    start = next(item for item in value["operations"] if item["kind"] == "db-start")
    require(start["state"] == "complete" and start["cluster"] == cluster, "缺少同容器與 cluster 的完成 db-start receipt")
    return db_id


# volume_claim 核對原生 create 不會更新的 labels，避免同名既有 volume 被誤認為本輪新建。
def volume_claim(nonce):
    volume = json_docker("volume", "inspect", VOLUME)[0]
    labels = volume.get("Labels") or {}
    require(volume["Name"] == VOLUME and volume["Driver"] == "local" and not volume.get("Options")
            and labels.get("com.docker.compose.project") == PROJECT and labels.get("com.docker.compose.volume") == "postgres_data"
            and labels.get(OWNER_LABEL) == nonce and not labels.get("com.docker.compose.config-hash")
            and volume.get("CreatedAt") and volume.get("Mountpoint"), "volume owner／nonce 不符")
    return {"volume_created": volume["CreatedAt"], "volume_mountpoint": volume["Mountpoint"]}


# resource_claim 核對 network ID 與 volume 的建立身分，後續 SQL／恢復沿用相同證明。
def resource_claim(nonce):
    volume = volume_claim(nonce)
    network = json_docker("network", "inspect", NETWORK)[0]
    labels = network.get("Labels") or {}
    require(network["Name"] == NETWORK and network["Driver"] == "bridge"
            and labels.get("com.docker.compose.project") == PROJECT and labels.get("com.docker.compose.network") == "default"
            and labels.get(OWNER_LABEL) == nonce and not labels.get("com.docker.compose.config-hash")
            and re.fullmatch(r"[a-f0-9]{64}", network["Id"])
            and network["IPAM"]["Config"] == [{"Subnet": SUBNET, "Gateway": "10.251.135.1"}], "network owner／nonce／subnet 不符")
    return {"nonce": nonce, "network_id": network["Id"], **volume}, network


# container_guard 在第一次 SQL 前核實實際容器、映像、唯一 network、volume、port 與 keys。
def container_guard(value, backend=None, running=True, *, new_db=False):
    expected_id = claimed_db(value) if new_db else owned_db(value)
    ownership, network = resource_claim(value["ownership"]["nonce"])
    require(ownership == value["ownership"], "network／volume 建立身分已改變")
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT).decode().split()
    require(ids, "固定 project 沒有容器")
    containers = json_docker("inspect", *ids)
    found = {}
    active = []
    for container in containers:
        labels = container["Config"]["Labels"]
        service = labels.get("com.docker.compose.service")
        require(labels.get("com.docker.compose.project") == PROJECT and service in ("db", "sql", *BACKENDS), "容器 project／service 不符")
        require(labels.get("com.docker.compose.project.working_dir") == str(TARGET), "容器來源目錄不符")
        networks = container["NetworkSettings"]["Networks"]
        # create 尚未配置 endpoint，Moby 可保留 ID key；只接受綁住精確 ID 的未啟動 DB。
        if service == "db" and container["State"].get("Status") == "created" and not container["State"]["Running"]:
            require(container["HostConfig"].get("NetworkMode") == ownership["network_id"]
                    and len(networks) == 1 and set(networks) <= {NETWORK, ownership["network_id"]}
                    and next(iter(networks.values())).get("NetworkID", "") in ("", ownership["network_id"]), "新 DB network ID 不符")
        else:
            require(set(networks) == {NETWORK}, "容器附加未知 network")
            require(networks[NETWORK]["NetworkID"] == ownership["network_id"], "容器 network ID 不符")
        if container["State"]["Running"]:
            active.append(service)
        require(service not in found, "同服務出現多個容器")
        found[service] = container
    require("db" in found and (not running or found["db"]["State"]["Running"]), "固定 DB 未執行")
    require(set(active) <= {"db", backend} - {None}, "出現非當輪執行中的服務")
    db = found["db"]
    require(db["Id"] == expected_id and db["Config"]["Labels"].get(OWNER_LABEL) == ownership["nonce"], "實際 DB container ID／nonce 不符")
    require(db["Config"]["Labels"].get("com.docker.compose.config-hash") == value["db_config_hash"]
            and db["Config"]["Labels"].get("com.docker.compose.oneoff") == "False", "實際 DB Compose discovery labels 不符")
    require(db["Config"]["Image"] == IMAGE and not db["HostConfig"].get("PortBindings"), "實際 DB image／ports 不符")
    require(db["Config"]["Cmd"] == value["db_command"], "實際 DB instrumentation command 不符")
    require(db["Config"]["Healthcheck"]["Test"] == ["CMD-SHELL", "pg_isready -U articles_owner -d articles"], "實際 DB healthcheck 不符")
    require(db["Image"] == json_docker("image", "inspect", IMAGE)[0]["Id"], "實際 DB immutable image 與鎖定 digest 不符")
    mounts = db["Mounts"]
    require(len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["Name"] == VOLUME
            and mounts[0]["Destination"] == "/var/lib/postgresql" and mounts[0]["Source"] == ownership["volume_mountpoint"], "實際 DB volume 不符")
    require(set(network.get("Containers", {})) <= {entry["Id"] for entry in containers}, "network 有未知 client 容器")
    require(db["Image"] == value["db_image"], "DB immutable image ID 已改變")
    if backend:
        require(backend in found and found[backend]["State"]["Running"], "當輪 backend 未執行")
        app = found[backend]
        index = BACKENDS.index(backend)
        binding = {str(8080 if backend == "aspnet-core" else 8000) + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(18501 + index)}]}
        require(app["HostConfig"]["PortBindings"] == binding and app["Config"]["Healthcheck"]["Test"] == ["NONE"], "實際 backend ports／healthcheck 不符")
        require(app["Image"] == value["images"].get(backend) and app["Config"]["Labels"].get("org.perf01.source") == value["source"], "實際 backend image／source 不符")
        key = [mount for mount in app["Mounts"] if mount["Destination"].endswith("-jwt.key")]
        require(len(key) == 1 and key[0]["Source"] == str(TARGET / ".local/keys" / (backend + ".key")) and key[0]["RW"] is False, "實際 backend key mount 不符")
        env = dict(item.split("=", 1) for item in app["Config"]["Env"] if "=" in item)
        require(env.get("DB_HOST") == "db" and env.get("DB_DATABASE") == "articles" and env.get("DB_USERNAME") == "articles_app", "實際 app DB 來源不符")
    return found


# sql 只對已驗證新容器的本機 socket 執行；app 語境以真正 LOGIN role 而非 owner 模擬。
def sql(value, query, app=False):
    db_id = value.get("db_id")
    require(db_id and re.fullmatch(r"[a-f0-9]{64}", db_id), "缺少精確 DB container ID")
    arguments = ["exec", "-i", "--env", "PGAPPNAME=perf01-calibration" if app else "PGAPPNAME=perf01-owner"]
    if app:
        arguments.extend(("--env", "PGOPTIONS=-c timezone=Asia/Taipei"))
    arguments.extend(("--user", "postgres", db_id, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "articles_app" if app else "articles_owner", "-d", "articles"))
    return docker(*arguments, payload=query.encode(), timeout=90).decode().strip()


# query_json 讓控制查詢每次僅回一個 JSON 值，避免以分隔符解析不可信欄位。
def query_json(value, query, app=False):
    return json.loads(sql(value, query, app))


# identity 讀回 system_identifier；只有完成全新啟動且已持久化精確容器身分時才能建立初始綁定。
def identity(value, *, new_db=False):
    if new_db:
        # 首次 SQL 也核對落盤的建立證明，不能僅憑 caller 傳入的 ID 採納 cluster。
        require(load(EVIDENCE / "state.json") == value, "首次 SQL 前建立身分尚未持久化")
        container_guard(value, new_db=True)
    else:
        owned_db(value)
    actual = query_json(value, "SELECT json_build_object('system',system_identifier::text,'database',current_database(),'role',current_user) FROM pg_control_system()")
    require(isinstance(actual.get("system"), str) and re.fullmatch(r"[0-9]+", actual["system"])
            and actual["database"] == "articles" and actual["role"] == "articles_owner", "SQL cluster／DB／role 不符")
    if new_db:
        require(value["cluster"] is None, "已存在 cluster，拒絕建立新身分")
        value["cluster"] = actual
    else:
        require(actual == value["cluster"], "cluster system_identifier 已改變")


# schema_snapshot 保存業務表、約束、索引與 grants；量測 extension 不冒稱原業務 migration。
def schema_snapshot(value):
    return query_json(value, """SELECT json_build_object(
      'migrations',(SELECT json_agg(x ORDER BY version) FROM (SELECT version,checksum FROM schema_migrations) x),
      'columns',(SELECT json_agg(x ORDER BY table_name,ordinal_position) FROM (SELECT table_name,column_name,data_type,is_nullable,column_default,ordinal_position FROM information_schema.columns WHERE table_schema='public' AND table_name IN ('accounts','articles','comments','login_sessions','login_failures','login_lockout_settings','schema_migrations')) x),
      'constraints',(SELECT json_agg(x ORDER BY conname) FROM (SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE connamespace='public'::regnamespace) x),
      'indexes',(SELECT json_agg(x ORDER BY indexname) FROM (SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public') x),
      'grants',(SELECT json_agg(x ORDER BY table_name,grantee,privilege_type) FROM (SELECT table_name,grantee,privilege_type FROM information_schema.role_table_grants WHERE table_schema='public' AND table_name NOT LIKE 'pg_stat%') x))""")


# create_database 只建立停止中的自有資源；原生 create 不移除、重新標記或啟動同名競爭者。
def create_database(value, config, operation):
    nonce = secrets.token_hex(32)
    operation["detail"]["nonce"] = nonce
    save(EVIDENCE / "state.json", value)
    labels = ["--label", "com.docker.compose.project=" + PROJECT, "--label", OWNER_LABEL + "=" + nonce]
    # volume create 對既有名稱可能成功回傳，因此立即驗不可變 labels；任何未知結果都保留 pending。
    docker("volume", "create", "--driver", "local", *labels, "--label", "com.docker.compose.volume=postgres_data", VOLUME)
    volume = volume_claim(nonce)
    network_id = docker("network", "create", "--driver", "bridge", "--subnet", SUBNET, "--gateway", "10.251.135.1",
                        *labels, "--label", "com.docker.compose.network=default", NETWORK).decode().strip()
    ownership, network = resource_claim(nonce)
    require(ownership == {"nonce": nonce, "network_id": network_id, **volume} and not network.get("Containers"), "新 network／volume 建立身分不符")
    # 僅把 DB 所需四個值寫入私密 env-file，密碼不放 argv；volume-nocopy 不把映像內容寫進未知 volume。
    environment = config["services"]["db"]["environment"]
    require(set(environment) == {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "PERF01_FIXTURE"}
            and environment["POSTGRES_DB"] == "articles" and environment["POSTGRES_USER"] == "articles_owner"
            and environment["PERF01_FIXTURE"] == "isolated-perf01"
            and re.fullmatch(r"[a-f0-9]{64}", environment["POSTGRES_PASSWORD"]), "新 DB environment 不符")
    env_path = EVIDENCE / "db-create.env"
    save(env_path, "".join(key + "=" + item + "\n" for key, item in environment.items()).encode(), exclusive=True)
    db_id = docker("create", "--pull", "never", "--name", PROJECT + "-db-1", *labels,
                   "--label", "com.docker.compose.service=db", "--label", "com.docker.compose.container-number=1",
                   "--label", "com.docker.compose.oneoff=False", "--label", "com.docker.compose.config-hash=" + value["db_config_hash"],
                   "--label", "com.docker.compose.project.working_dir=" + str(TARGET),
                   "--network", network_id, "--network-alias", "db", "--env-file", str(env_path),
                   "--mount", "type=volume,src=" + VOLUME + ",dst=/var/lib/postgresql,volume-nocopy",
                   "--health-cmd", "pg_isready -U articles_owner -d articles", "--health-interval", "2s",
                   "--health-timeout", "3s", "--health-retries", "20", IMAGE, *value["db_command"]).decode().strip()
    value.update(db_id=db_id, ownership=ownership)
    operation["detail"] = {"project": PROJECT, "volume": VOLUME, "db_id": db_id, "image": value["db_image"], "ownership": ownership}
    found = container_guard(value, running=False, new_db=True)
    require(set(found) == {"db"} and found["db"]["State"].get("Status") == "created"
            and not found["db"]["State"]["Running"], "新 DB 必須尚未啟動")
    # 身分落盤後才允許 exact-ID start；建立中斷或讀回失敗不會獲得 cleanup 權限。
    save(EVIDENCE / "state.json", value)


# start_database 對已確認的精確 ID 啟動與等待，首次建立及 resume 共用原生 healthcheck。
def start_database(value):
    docker("start", value["db_id"], timeout=180)
    deadline = time.monotonic() + 180
    while True:
        observed = json_docker("inspect", value["db_id"])[0]
        require(observed["Id"] == value["db_id"] and observed["State"]["Running"], "自有 DB 未啟動")
        health = observed["State"]["Health"]["Status"]
        # entrypoint 的暫時 server 只有 Unix socket；TCP 就緒才可進入集中 SQL，避免初始化尚未結束。
        if health == "healthy" and docker("exec", value["db_id"], "sh", "-c",
                                         "if pg_isready -h 127.0.0.1 -U articles_owner -d articles >/dev/null; then printf ready; fi",
                                         timeout=5) == b"ready":
            return
        require(health in ("starting", "healthy") and time.monotonic() < deadline, "自有 DB healthcheck 未就緒")
        time.sleep(0.2)


# initialize 先建立停止中的 DB 並保存所有權才啟動；未知操作不會被自動重送。
def initialize(owned=None):
    value = state()
    config = config_guard(value)
    if value["initialized"]:
        found = container_guard(value, running=False)
        if not found["db"]["State"]["Running"]:
            with mutation(value, "db-resume", {"volume": VOLUME, "cluster": value["cluster"]}):
                # 只啟動 receipt 內已存在的容器；等待鎖定的原生 healthcheck，避免 Compose 重建替代容器。
                start_database(value)
                container_guard(value)
                # SQL 只讀核對原 cluster、schema 及 restored logging 後，才將 resume receipt 標為完成。
                identity(value)
                if owned:
                    owned()
                require(schema_snapshot(value) == value["schema"], "重啟後業務 schema 已漂移")
                require(query_json(value, "SELECT coalesce(to_json(rolconfig),'[]'::json) FROM pg_roles WHERE rolname='articles_app'") == value["logging_original"], "重啟後 app logging 未恢復")
        else:
            identity(value)
            if owned:
                owned()
            require(schema_snapshot(value) == value["schema"], "業務 schema 已漂移")
        print(json.dumps({"phase": "init", "status": "already-complete", "source": value["source"]}))
        return
    # 寫入新 cluster 前保留 receipt；新 DB 不允許重新命名覆蓋既有 volume。
    preflight()
    require(value["cluster"] is None and not any(item["kind"] == "db-start" for item in value["operations"]), "已有 DB 啟動紀錄；保留原狀，禁止再次建立")
    # --pull never 的快取前提先唯讀確認；缺映像不得留下 pending 或占用 volume／network 名稱。
    value["db_image"] = json_docker("image", "inspect", IMAGE)[0]["Id"]
    with mutation(value, "db-start", {"project": PROJECT, "volume": VOLUME}) as operation:
        create_database(value, config, operation)
        start_database(value)
        identity(value, new_db=True)
        operation["cluster"] = value["cluster"]
    if owned:
        owned()
    with mutation(value, "central-sql", {"head": HEAD}):
        compose("build", "sql", timeout=1800)
        image = json_docker("image", "inspect", f"perf01_test_laravel:{value['source']}")[0]
        require(image["Config"]["Labels"].get("org.perf01.source") == value["source"], "SQL image source 不符")
        compose("run", "--rm", "--no-deps", "sql", timeout=180)
        migrations = query_json(value, "SELECT json_object_agg(version,trim(checksum)) FROM schema_migrations")
        expected = {path.stem: digest(path.read_bytes()) for path in (TARGET / "database/sql").glob("[0-9][0-9][0-9][0-9]_*.sql")}
        require(migrations == expected, "集中 SQL checksum 不符")
        value["schema"] = schema_snapshot(value)
    # 原生 extension 只建立在本輪資料庫；不調整 production migration 或索引。
    with mutation(value, "instrumentation", {"extension": "pg_stat_statements"}):
        require(sql(value, "SELECT default_version FROM pg_available_extensions WHERE name='pg_stat_statements'"), "鎖定映像無原生 pg_stat_statements；停止，不安裝替代")
        sql(value, "CREATE EXTENSION pg_stat_statements")
        value["logging_original"] = query_json(value, "SELECT coalesce(to_json(rolconfig),'[]'::json) FROM pg_roles WHERE rolname='articles_app'")
        require(value["logging_original"] == [], "新 app role 有未知設定，拒絕覆寫")
    value["initialized"] = True
    save(EVIDENCE / "state.json", value)
    print(json.dumps({"phase": "init", "status": "complete", "source": value["source"], "cluster": value["cluster"]["system"]}))


# literal 只建立 PREPARE 的 EXECUTE 參數或受控 fixture literal，從不改寫 emitted SQL 本文。
def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    require(isinstance(value, str) and "\0" not in value, "不允許的 SQL literal 型態")
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


# NoRedirect 讓固定 loopback 請求拒絕 redirect，不將 Authorization 送到其他來源。
class NoRedirect(urllib.request.HTTPRedirectHandler):
    # redirect_request 拒絕所有 redirect，交由 HTTPError 保留原狀態供契約判斷。
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


# request 只送一次 HTTP，proxy 停用；傳輸未知時不重送 POST 或登入。
def request(backend, method, path, token=None, body=None, *, timeout=30):
    require(backend in BACKENDS and (path.startswith("/api/") or path == "/up"), "HTTP 目標不在固定清單")
    require(not any(character in path for character in ("\r", "\n", "\0")), "HTTP path 含原始控制字元")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None if body is None else encoded(body)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    operation = urllib.request.Request("http://127.0.0.1:" + str(18501 + BACKENDS.index(backend)) + path,
                                       data=payload, headers=headers, method=method)
    try:
        response = opener.open(operation, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
        raise HTTPTransportFailure("傳輸", error) from None
    try:
        with response:
            raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, "HTTP 回應超過量測上限")
            return response.code, raw.decode() if path == "/up" else json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise HTTPTransportFailure("回應讀取／JSON", error) from None


# wait_backend 先證明容器內實際 LISTEN，再查 host loopback，避免把轉發層開埠當成 app 就緒。
def wait_backend(backend, app_id, readiness):
    require(re.fullmatch(r"[a-f0-9]{64}", app_id), "readiness 缺少精確 backend container ID")
    port = 8080 if backend == "aspnet-core" else 8000
    # 十二個鎖定映像皆用 Alpine／Debian shell，服務以 IPv4 或 IPv6 wildcard 綁定容器埠。
    addresses = {"0" * width + f":{port:04X}" for width in (8, 32)}
    deadline = time.monotonic() + 60
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "backend 容器 LISTEN／loopback readiness 逾時")
        output = docker("exec", app_id, "sh", "-ec", "cat /proc/net/tcp; if [ -r /proc/net/tcp6 ]; then cat /proc/net/tcp6; fi", timeout=min(3, remaining))
        # Linux procfs 狀態 0A 是 LISTEN。只讀 socket 表，冷啟時不先送 /up 或任何業務 GET。
        if any(len(fields) >= 4 and fields[1] in addresses and fields[3] == "0A" for fields in (line.split() for line in output.decode("ascii").splitlines())):
            try:
                with socket.create_connection(("127.0.0.1", 18501 + BACKENDS.index(backend)), timeout=min(1, remaining)):
                    break
            except (ConnectionError, TimeoutError):
                pass
        time.sleep(0.2)
    if not readiness:
        return
    # 暖機只重試唯讀 /up 的特定短暫傳輸故障。已收到的狀態或本文錯誤直接回報。
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "人工 /up readiness 逾時")
        try:
            status_code, body = request(backend, "GET", "/up", timeout=min(3, remaining))
        except HTTPTransportFailure as error:
            if not error.retryable or attempt == 2 or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
            continue
        require(status_code == 200, f"人工 /up readiness 回 HTTP {status_code}")
        if backend in ("symfony", "aspnet-core"):
            try:
                require(json.loads(body) == {"status": "ok"}, "人工 /up JSON 本文不符")
            except ValueError:
                raise Failure("人工 /up JSON 本文不符") from None
        else:
            require(body == "OK", "人工 /up 純文字本文不符")
        return


# start_backend 只啟動指定一個後端，記住 immutable image 並以實際 inspect 核對。
def start_backend(value, backend, readiness=True):
    container_guard(value)
    with mutation(value, "backend-start", {"backend": backend}) as operation:
        compose("build", backend, timeout=1800)
        image = json_docker("image", "inspect", f"perf01_test_{backend}:{value['source']}")[0]
        require(image["Config"]["Labels"].get("org.perf01.source") == value["source"], "built image source 不符")
        value["images"][backend] = image["Id"]
        operation["detail"]["image"] = image["Id"]
        # up 送出前先落盤 immutable image，傳輸中斷後才能查證真正建立的是哪個容器。
        save(EVIDENCE / "state.json", value)
        compose("up", "-d", "--no-deps", backend, timeout=180)
        found = container_guard(value, backend)
    # 已驗證啟動才結束 receipt，後續唯讀 readiness 失敗不會把已知啟動改成未知寫入。
    wait_backend(backend, found[backend]["Id"], readiness)


# stop_backend 僅停止已核對的本輪服務，等待其 app sessions 結束後才切換後端。
def stop_backend(value, backend):
    found = container_guard(value, backend)
    app_id = found[backend]["Id"]
    docker("stop", "--time", "15", app_id, timeout=60)
    require(not json_docker("inspect", app_id)[0]["State"]["Running"], "backend 未停止")
    deadline = time.monotonic() + 15
    while query_json(value, "SELECT count(*) FROM pg_stat_activity WHERE usename='articles_app'"):
        require(time.monotonic() < deadline, "停止後 app sessions 未結束；不做泛用終止")
        time.sleep(0.1)
    container_guard(value)
    return app_id


# logging_settings 僅改新 app role；原設定先存 receipt，登入與密碼 setup 前必須關閉。
def logging_settings(value, enabled):
    ids = docker("ps", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT).decode().split()
    active = [item["Config"]["Labels"]["com.docker.compose.service"] for item in json_docker("inspect", *ids)
              if item["Config"]["Labels"]["com.docker.compose.service"] in BACKENDS] if ids else []
    require(len(active) <= 1 and (not enabled or not active), "啟用詳細 logging 前必須停止所有 app 連線")
    container_guard(value, active[0] if active else None)
    identity(value)
    with mutation(value, "logging", {"enabled": enabled, "original": value["logging_original"]}):
        settings = {"log_min_duration_statement": "0" if enabled else "-1", "log_parameter_max_length": "-1" if enabled else "0"}
        if enabled:
            query = ";".join("ALTER ROLE articles_app SET " + key + "=" + setting for key, setting in settings.items())
        else:
            query = ";".join("ALTER ROLE articles_app RESET " + key for key in settings)
        sql(value, query)
        actual = query_json(value, "SELECT coalesce(to_json(rolconfig),'[]'::json) FROM pg_roles WHERE rolname='articles_app'")
        expected = sorted(key + "=" + setting for key, setting in settings.items()) if enabled else value["logging_original"]
        require(sorted(actual) == sorted(expected), "app logging 設定 readback 不符")
        value["logging"] = enabled


# seed 透過既有首次 admin 指令及 register 建 actor，其餘固定資料在單一受控交易增量種入。
def seed(value, level):
    require(value["level"] in (None, "small") and (level == "small" or value["level"] == "small"), "fixture 級別轉換不符；禁止刪資料退回小量")
    data = fixture(level)
    if value["level"] is None:
        start_backend(value, "laravel")
        try:
            with mutation(value, "initial-admin", {"email": ADMIN_EMAIL}):
                compose("exec", "-T", "laravel", "php", "artisan", "app:create-initial-admin",
                        payload=(ADMIN_EMAIL + "\na aa nickonly 陳 AsCiI Ä ä %_\\\n" + PASSWORD + "\n").encode())
                require(query_json(value, "SELECT json_build_object('id',id,'role',role) FROM accounts WHERE email=" + literal(ADMIN_EMAIL)) == {"id": 1, "role": "admin"}, "admin 建立 readback 不符")
            with mutation(value, "register-actor", {"email": MEMBER_EMAIL}):
                status_code, body = request("laravel", "POST", "/api/auth/register", body={"email": MEMBER_EMAIL, "nickname": data["accounts"][1]["nickname"], "password": PASSWORD})
                require(status_code == 201 and body.get("success") is True and sql(value, "SELECT id FROM accounts WHERE email=" + literal(MEMBER_EMAIL)) == "2", "member 註冊或 ID 不符")
        finally:
            # 初始化異常也只停自己的 backend；pending receipt 仍保留供人工查證。
            stop_backend(value, "laravel")
    previous = fixture(value["level"]) if value["level"] else {"accounts": data["accounts"][:2], "articles": [], "comments": []}
    statements = ["BEGIN", "SET LOCAL TIME ZONE 'Asia/Taipei'"]
    if value["level"] is None:
        for row in data["accounts"][:2]:
            statements.append("UPDATE accounts SET created_at=" + literal(row["created_at"]) + " WHERE id=" + str(row["id"]))
    # 尾段批次 VALUES 避免逐筆 DB 往返；新帳號只借本輪 actor hash，不輸出 hash。
    for table in ("accounts", "articles", "comments"):
        added = data[table][len(previous[table]):]
        if not added:
            continue
        keys = list(added[0])
        if table == "accounts":
            columns = keys + ["password_hash"]
            values = ["(" + ",".join(literal(row[key]) for key in keys) + ",(SELECT password_hash FROM accounts WHERE id=2))" for row in added]
        else:
            columns = keys
            values = ["(" + ",".join(literal(row[key]) for key in keys) + ")" for row in added]
        statements.append("INSERT INTO " + table + "(" + ",".join(columns) + ") OVERRIDING SYSTEM VALUE VALUES " + ",".join(values))
        statements.append("SELECT setval(pg_get_serial_sequence('" + table + "','id')," + str(len(data[table])) + ",true)")
    statements.extend(("COMMIT", "ANALYZE accounts,articles,comments"))
    container_guard(value)
    identity(value)
    with mutation(value, "seed", {"from": value["level"], "to": level, "oracle_hash": digest(encoded(data))}):
        sql(value, ";\n".join(statements))
        verify_fixture(value, data)
        value["level"] = level
        save(EVIDENCE / (level + "-fixture.json"), data)
        statistics = query_json(value, "SELECT json_agg(x ORDER BY relname) FROM (SELECT relname,n_live_tup,last_analyze FROM pg_stat_user_tables WHERE relname IN ('accounts','articles','comments')) x")
        value["statistics"] = {"level": level, "analyzed_at": time.time_ns(), "tables": statistics}
        save(EVIDENCE / (level + "-statistics.json"), value["statistics"])


# verify_fixture 直接讀回固定資料與 oracle 比完整欄位；不以受測列表反推預期 total。
def verify_fixture(value, data):
    for table, expected in data.items():
        columns = list(expected[0])
        expressions = []
        for column in columns:
            expressions.append("to_char(" + column + " AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD HH24:MI:SS') AS " + column if column.endswith("_at") else column)
        actual = query_json(value, "SELECT json_agg(x ORDER BY id) FROM (SELECT " + ",".join(expressions) + " FROM " + table + ") x")
        require(actual == expected, "fixture readback 與固定 oracle 不符：" + table)


# snapshot 取得完整原生 app counter 及 reset／eviction 證據；owner 控制查詢不混入。
def snapshot(value):
    return query_json(value, """SELECT json_build_object(
      'reset',(SELECT stats_reset::text FROM pg_stat_statements_info),
      'dealloc',(SELECT dealloc FROM pg_stat_statements_info),
      'entries',coalesce((SELECT json_agg(x ORDER BY queryid) FROM (
        SELECT dbid,userid,queryid::text,toplevel,query,calls,total_exec_time,rows,
        shared_blks_hit,shared_blks_read,shared_blks_dirtied,shared_blks_written,temp_blks_read,temp_blks_written
        FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname='articles')
        AND userid=(SELECT oid FROM pg_roles WHERE rolname='articles_app')) x),'[]'::json))""")


# counter_key 保留 db/role/top-level 維度，不能只按 normalized SQL 文字拼接。
def counter_key(entry):
    return entry["dbid"], entry["userid"], entry["queryid"], entry["toplevel"]


# delta 拒絕 reset、eviction、消失或下降的 counter，避免把截短窗口當真實少查詢。
def delta(before, after):
    require(before["reset"] == after["reset"] and before["dealloc"] == after["dealloc"], "counter reset／entry eviction，量測無效")
    old = {counter_key(entry): entry for entry in before["entries"]}
    new = {counter_key(entry): entry for entry in after["entries"]}
    require(old.keys() <= new.keys(), "counter entry 遺失，量測無效")
    result = []
    for key, entry in new.items():
        differences = {name: entry[name] - old.get(key, {}).get(name, 0) for name in METRICS}
        require(all(number >= 0 for number in differences.values()), "counter 下降，量測無效")
        if differences["calls"]:
            require(entry["toplevel"] is True, "出現非 top-level counter")
            result.append({**entry, **differences, "before_metrics": {name: old.get(key, {}).get(name, 0) for name in METRICS},
                           "after_metrics": {name: entry[name] for name in METRICS}})
    return result


# cursor 保存原生日誌路徑、完整 prefix 與大小；固定不輪替的檔案被截斷會被拒絕。
def cursor(value):
    return query_json(value, """SELECT json_build_object('path',p,'size',(pg_stat_file(p)).size,
       'prefix',encode(pg_read_binary_file(p,0,least((pg_stat_file(p)).size,128)),'base64'))
       FROM (SELECT pg_current_logfile('jsonlog') AS p) x""")


# log_slice 從原游標讀增量，完整 JSON 及每個 session line number 都須連續。
def log_slice(value, before, after):
    require(before["path"] == after["path"] and after["size"] >= before["size"], "日誌輪替／截斷，量測無效")
    if before["size"] >= 128:
        require(before["prefix"] == after["prefix"], "日誌 prefix 改變，量測無效")
    length = after["size"] - before["size"]
    require(length <= 16 * 1024 * 1024, "單次日誌窗口超過上限")
    raw = base64.b64decode(sql(value, "SELECT encode(pg_read_binary_file(" + literal(before["path"]) + "," + str(before["size"]) + "," + str(length) + "),'base64')")) if length else b""
    require(not raw or raw.endswith(b"\n"), "原生日誌尚未完成一筆 JSON")
    try:
        records = [json.loads(line) for line in raw.splitlines()]
    except ValueError:
        raise Failure("原生 JSON 日誌不完整") from None
    return raw, records


# binds 嚴格解碼 PostgreSQL Parameters 字串，保留反斜線、Unicode、NULL 與單引號。
def binds(detail):
    if not detail:
        return []
    require(detail.startswith("parameters: ") or detail.startswith("Parameters: "), "未知 bind detail 格式")
    text = detail[len("parameters: "):]
    result = []
    position = 0
    while position < len(text):
        match = re.match(r"\$(\d+) = ", text[position:])
        require(match and int(match[1]) == len(result) + 1, "bind 編號不連續")
        position += match.end()
        if text.startswith("NULL", position):
            result.append(None)
            position += 4
        else:
            require(text[position:position + 1] == "'", "bind 值被截斷或型態未知")
            position += 1
            value = ""
            while True:
                require(position < len(text), "bind 字串未結束")
                if text[position] == "'":
                    if text[position:position + 2] == "''":
                        value += "'"
                        position += 2
                    else:
                        position += 1
                        break
                else:
                    value += text[position]
                    position += 1
            result.append(value)
        if position < len(text):
            require(text[position:position + 2] == ", ", "bind 尾段不符")
            position += 2
    return result


# nestjs_auth_clock 只識別鎖後用於驗證 JWT 期限的固定 SQL，保留其 SELECT 呼叫計數。
def nestjs_auth_clock(query):
    return " ".join(query.lower().split()) == "select floor(extract(epoch from clock_timestamp()))::bigint as now"


# category 計入全部 SQL 並保留 unknown，不以 framework 特定 fragment 刪除初始化／清理。
def category(query):
    normalized = re.sub(r'\s+', ' ', query.replace('"', '')).strip().lower()
    if normalized.startswith(("begin", "commit", "rollback", "savepoint", "release")):
        return "transaction"
    if normalized.startswith(("set ", "reset ", "discard ")):
        return "connection"
    if normalized.startswith("select") and (re.search(r"\bfrom\s+(?:public\.)?(?:articles|comments)\b", normalized) or re.search(r"\bfrom\s+(?:public\.)?accounts\b", normalized) and ("order by" in normalized or re.search(r"\bcount\s*\(", normalized))):
        return "count" if re.search(r"\bcount\s*\(", normalized) else "list" if "order by" in normalized else "parent"
    if "login_failures" in normalized or "login_lockout_settings" in normalized:
        return "other"
    projection = normalized.partition(" from ")[0]
    if "login_sessions" in normalized or re.fullmatch(r"select clock_timestamp\(\)(?: as \w+)?;?", normalized) or nestjs_auth_clock(query):
        return "auth"
    if re.search(r"\bfrom accounts\b", normalized) and ("for update" in normalized or all(re.search(r"\b" + field + r"\b", projection) for field in ("id", "email", "nickname", "role", "status", "created_at"))):
        return "auth"
    if normalized.startswith(("select ", "with ", "show ")):
        return "data-fetch"
    return "other"


# split_statements 辨識 SQL 字串／識別字／註解／dollar quote 內分號，避免把日誌行數當 SQL 次數。
def split_statements(query):
    result = []
    start = position = 0
    quote = None
    comment_depth = 0
    while position < len(query):
        if comment_depth:
            if query.startswith("/*", position):
                comment_depth += 1
                position += 2
            elif query.startswith("*/", position):
                comment_depth -= 1
                position += 2
            else:
                position += 1
        elif quote:
            if query.startswith(quote, position):
                if quote in ("'", '"') and query.startswith(quote * 2, position):
                    position += 2
                else:
                    position += len(quote)
                    quote = None
            elif query[position] == "\\" and quote == "'":
                # 未知 standard_conforming_strings 語境下寧可保守拒絕，不錯拆 SQL。
                raise Failure("複合 SQL 含反斜線字串，需先校準其字串語境")
            else:
                position += 1
        elif query.startswith("--", position):
            newline = query.find("\n", position)
            position = len(query) if newline == -1 else newline + 1
        elif query.startswith("/*", position):
            comment_depth = 1
            position += 2
        elif query[position] in ("'", '"'):
            quote = query[position]
            position += 1
        elif query[position] == "$" and (match := re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", query[position:])):
            quote = match[0]
            position += len(quote)
        elif query[position] == ";":
            if query[start:position].strip():
                result.append(query[start:position].strip())
            position += 1
            start = position
        else:
            position += 1
    require(not quote and not comment_depth, "SQL 日誌字串／註解未完成")
    if query[start:].strip():
        result.append(query[start:].strip())
    return result


# pgx_empty_ping 只識別 pgx 5.7.6 的精確 simple-query 空註解；附帶 SQL、bind 或非零 ID 都不是 ping。
def pgx_empty_ping(record):
    query_id = record.get("query_id")
    return (type(query_id) is int and query_id == 0 and record.get("detail") in (None, "")
            and re.fullmatch(r"duration: [0-9.]+ ms\s+statement: -- ping", record.get("message", "")) is not None)


# events 以 duration 完成事件建立 SQL 清單；Parse/Bind 與空 ping 保留為協定證據。
def events(records, expected_host=None, calibration=False):
    completed, protocol, failures = [], [], []
    previous = {}
    calibration_sessions = {}
    if calibration:
        # 原生 authentication／authorization 尚未填 application_name；必須由同 PID/session 的後續 ready／duration 佐證。
        for record in records:
            if record.get("user") == "articles_app" and record.get("dbname") == "articles":
                key = (record.get("session_id"), record.get("pid"))
                name = record.get("application_name")
                if name:
                    require(name == "perf01-calibration", "校準窗口出現非校準 client")
                    if record.get("message", "").startswith(("connection ready:", "duration:")):
                        calibration_sessions[key] = max(calibration_sessions.get(key, 0), record.get("line_num", 0))
    for record in records:
        if record.get("user") != "articles_app" or record.get("dbname") != "articles":
            continue
        session = record.get("session_id")
        pid = record.get("pid")
        key = (session, pid)
        line = record.get("line_num")
        require(session and isinstance(pid, int) and pid > 0 and isinstance(line, int), "日誌缺 PID／session／line number")
        if key in previous:
            require(line == previous[key] + 1, "session 日誌有游標缺口")
        previous[key] = line
        if expected_host is not None:
            host = record.get("remote_host", "")
            require(host == expected_host, "量測窗口出現未知 app client")
        if calibration:
            name = record.get("application_name")
            if not name:
                require(record.get("message", "").startswith(("connection authenticated:", "connection authorized:"))
                        and calibration_sessions.get(key, 0) > line, "校準窗口出現孤立或未驗證的 app 啟動事件")
            else:
                require(name == "perf01-calibration", "校準窗口出現非校準 client")
        if record.get("error_severity") in ("ERROR", "FATAL", "PANIC"):
            failures.append(record)
            continue
        if pgx_empty_ping(record):
            protocol.append({**record, "protocol_kind": "pgx-empty-ping"})
            continue
        message = record.get("message", "")
        match = re.fullmatch(r"duration: ([0-9.]+) ms\s+(statement|execute [^:]*|parse [^:]*|bind [^:]*): (.*)", message, re.DOTALL)
        if not match:
            # connection/disconnection 是已知原生日誌，其他 app 訊息不可悄悄略過。
            require(message.startswith(("connection ", "disconnection:", "connection authorized:", "connection authenticated:")), "未知 app 日誌事件，量測無效")
            continue
        if match[2].startswith(("parse ", "bind ")):
            protocol.append(record)
            continue
        query = match[3]
        statements = split_statements(query) if ";" in query else [query]
        require(len(statements) <= 1 or not record.get("detail"), "複合 SQL 不能借用單一 bind detail")
        for ordinal, statement in enumerate(statements):
            completed.append({"query": statement, "binds": binds(record.get("detail")), "category": category(statement),
                              "duration_ms": float(match[1]) if len(statements) == 1 else None,
                              "batch_duration_ms": float(match[1]), "batch_sql": query, "batch_ordinal": ordinal,
                              "protocol": match[2],
                              "query_id": str(int(record.get("query_id", 0)) % (1 << 64)),
                              "session_id": session, "pid": pid, "line_num": line,
                              "timestamp": record.get("timestamp")})
    return completed, protocol, failures


# attribute_npgsql_startup 只歸屬 ASP.NET 的精確驅動啟動 batch，保留原日誌 ID 與所有原生 calls。
def attribute_npgsql_startup(counters, completed):
    heads = [event for event in completed if event["protocol"] == "statement" and event["duration_ms"] is None
             and event["batch_ordinal"] == 0 and digest(event["query"].strip().encode()) == NPGSQL_STARTUP_QUERIES[0][0]]
    if not heads:
        return
    require(len(heads) == 1, "Npgsql 啟動 batch 非唯一")
    head = heads[0]
    identity = (head["pid"], head["session_id"], head["line_num"])
    batch = [event for event in completed if (event["pid"], event["session_id"], event["line_num"]) == identity]
    require(len(batch) == len(NPGSQL_STARTUP_QUERIES), "Npgsql 啟動 batch 語句數不符")
    matched = []
    for ordinal, (event, (raw_hash, counter_hash)) in enumerate(zip(batch, NPGSQL_STARTUP_QUERIES)):
        require(event["batch_ordinal"] == ordinal and event["batch_sql"] == head["batch_sql"] and event["protocol"] == "statement"
                and event["query_id"] == head["query_id"] and not event["binds"]
                and digest(event["query"].strip().encode()) == raw_hash, "Npgsql 啟動 batch 原始 SQL／順序不符")
        entries = [entry for entry in counters if digest(entry["query"].strip().encode()) == counter_hash]
        require(len(entries) == 1 and entries[0]["calls"] == 1, "Npgsql 啟動 SQL 無唯一原生 counter")
        matched.append(str(int(entries[0]["queryid"]) % (1 << 64)))
    # 其餘 SQL 不能借走 metadata counter。單筆 batch duration 的 query ID 必須等於最後一項。
    unrelated = {event["query_id"] for event in completed if (event["pid"], event["session_id"], event["line_num"]) != identity}
    require(len(set(matched)) == len(batch) and not (set(matched) & unrelated) and matched[-1] == head["query_id"], "Npgsql 啟動 batch 最後 query ID／原生 calls 不符")
    for event, query_id in zip(batch, matched):
        event.update(logged_query_id=event["query_id"], query_id=query_id, category="driver-metadata")


# reconcile 要求每個成功 SQL 都有 counter 對應，queryid=0 utility 也保留明確文字配對。
def reconcile(counters, completed, backend=None):
    require(sum(entry["calls"] for entry in counters) == len(completed), "counter／完成 SQL 次數不同，量測無效")
    if backend == "aspnet-core":
        attribute_npgsql_startup(counters, completed)
    counter_ids = Counter()
    log_ids = Counter()
    zero = []
    for event in completed:
        if event["query_id"] == "0":
            zero.append(event)
        else:
            log_ids[event["query_id"]] += 1
    for entry in counters:
        counter_ids[str(int(entry["queryid"]) % (1 << 64))] += entry["calls"]
    for query_id, count in log_ids.items():
        require(counter_ids[query_id] >= count, "duration query ID 無對應原生 counter")
        counter_ids[query_id] -= count
    # 原生 utility 的 JSON query_id 可能為零；只接受一對一且文字相同的剩餘語句。
    for event in zero:
        matching = [entry for entry in counters if re.sub(r"\s+", " ", entry["query"]).strip().rstrip(";") == re.sub(r"\s+", " ", event["query"]).strip().rstrip(";") and counter_ids[str(int(entry["queryid"]) % (1 << 64))] > 0]
        require(len(matching) == 1, "零 query ID utility 無法唯一對應，量測無效")
        counter_ids[str(int(matching[0]["queryid"]) % (1 << 64))] -= 1
    require(not any(counter_ids.values()), "仍有無法歸屬 counter，量測無效")


# app_quiet 拒絕未完成 SQL、未知 client 或閒置交易，窗口不以睡固定秒數截短。
def app_quiet(value, expected_host=None, calibration=False):
    # host(inet) 保留單一 IP，不附 /32。只接受目前 backend 的精確位址。
    sessions = query_json(value, "SELECT coalesce(json_agg(x),'[]'::json) FROM (SELECT pid,host(client_addr) AS client_addr,application_name,state FROM pg_stat_activity WHERE usename='articles_app') x")
    for session in sessions:
        if expected_host is not None:
            require(session["client_addr"] == expected_host, "存在未知 app session")
        if calibration:
            require(session["application_name"] == "perf01-calibration", "校準遇到其他 app session")
    return all(session["state"] == "idle" for session in sessions)


# capture 在原生 counter、完整日誌與 quiet 三方收斂後才關閉窗口，逾時保留未完成證據。
def capture(value, action, host=None, calibration=False, *, backend=None):
    before = after = start = end = None
    raw, records = b"", []
    try:
        start = cursor(value)
        before = snapshot(value)
        require(app_quiet(value, host, calibration), "量測前 app 非 quiet")
        time.sleep(0.1)
        settled = snapshot(value)
        end = cursor(value)
        raw, records = log_slice(value, start, end)
        # owner 的觀測連線也有日誌；只以 app 事件判斷 quiet，原始 owner 日誌仍完整保留。
        events(records, host, calibration)
        require(not delta(before, settled) and not any(record.get("user") == "articles_app" and record.get("dbname") == "articles" for record in records)
                and app_quiet(value, host, calibration), "量測前有背景 app SQL／日誌活動")
        output = action()
        deadline = time.monotonic() + 12
        stable = None
        last_error = "SQL／日誌尚未收斂"
        while time.monotonic() < deadline:
            after = snapshot(value)
            end = cursor(value)
            raw, records = log_slice(value, start, end)
            completed, protocol, failures = events(records, host, calibration)
            counters = delta(before, after)
            try:
                reconcile(counters, completed, backend)
                # 收斂依 app counter 與所有 app 事件；控制連線新增 owner 日誌不會製造永遠變動的游標。
                app_records = [record for record in records if record.get("user") == "articles_app" and record.get("dbname") == "articles"]
                signature = digest(encoded((after, app_records)))
                if app_quiet(value, host, calibration) and signature == stable:
                    return output, {"before": before, "after": after, "cursor_before": start, "cursor_after": end,
                                    "counters": counters, "sql": completed, "protocol": protocol, "failures": failures,
                                    "categories": dict(Counter(event["category"] for event in completed)),
                                    "all_sql_calls": sum(entry["calls"] for entry in counters), "raw": raw.decode()}
                stable = signature
            except Failure as error:
                last_error = str(error)
                stable = None
            time.sleep(0.15)
        raise Failure("量測窗口未完成：" + last_error)
    except (Failure, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt) as error:
        # parser／counter／quiet 的立即拒絕也必須留原始窗口；補採證失敗不得蓋掉首個錯誤。
        preservation_error = None
        try:
            if after is None:
                after = snapshot(value)
            if start is not None:
                end = cursor(value)
                raw, records = log_slice(value, start, end)
        except (Failure, OSError, ValueError, KeyError, TypeError) as secondary:
            preservation_error = type(secondary).__name__
        try:
            save(EVIDENCE / ("invalid-window-" + secrets.token_hex(6) + ".json"), {
                "before": before, "after": after, "cursor_before": start, "cursor_after": end,
                "raw": raw.decode(errors="replace"), "records": records,
                "reason": str(error) if isinstance(error, Failure) else type(error).__name__,
                "preservation_error": preservation_error})
        except (Failure, OSError):
            print("PERF01 私密失敗窗口無法寫入；首個錯誤仍保留，限定恢復另行回報", file=sys.stderr)
        raise


# authenticate 每次合法登入都先存 receipt；JSON／transport 失敗會保留未知結果且不重送。
def authenticate(value, backend, actor, token_file=None):
    email = ADMIN_EMAIL if actor == "admin" else MEMBER_EMAIL
    with mutation(value, "login", {"backend": backend, "actor": actor}):
        status_code, body = request(backend, "POST", "/api/auth/login", body={"email": email, "password": PASSWORD})
        require(status_code == 200 and body.get("success") is True, "合法 actor 登入失敗")
        token = body.get("data", {}).get("access_token")
        require(isinstance(token, str) and token.count(".") == 2, "合法登入未回 access_token")
        if token_file is not None:
            save(token_file, {"backend": backend, "actor": actor, "cluster": value["cluster"],
                              "key_hash": value["keys"][backend], "token": token}, exclusive=True)
    return token


# actor_token 重用已成功保存並以 /me 讀回的憑證；未知登入 receipt 由 state 阻止，不盲目重送 POST。
def actor_token(value, backend, actor):
    paths = sorted(EVIDENCE.glob(f"token-{backend}-{actor}-*.json"), reverse=True)
    for path in paths:
        record = load(path)
        require(record["backend"] == backend and record["actor"] == actor and record["cluster"] == value["cluster"]
                and record["key_hash"] == value["keys"][backend], "保存 token 的 fixture／key 身分不符")
        token = record["token"]
        try:
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        except (ValueError, IndexError):
            raise Failure("保存 token 格式不符") from None
        require(type(claims.get("exp")) is int, "保存 token 缺少原期限")
        if claims["exp"] <= time.time():
            continue
        status_code, body = request(backend, "GET", "/api/me", token)
        require(status_code == 200 and body.get("data", {}).get("id") == (1 if actor == "admin" else 2)
                and body["data"].get("role") == actor and body["data"].get("status") == "active", "保存 token 的 /me 讀回不符，不重新登入")
        return token
    path = EVIDENCE / f"token-{backend}-{actor}-{time.time_ns()}.json"
    return authenticate(value, backend, actor, path)


# startup_plan_settings 使用鎖定後端的連線設定。Express 沿用 DB 的 UTC，另由 Pool 設定逾時。
def startup_plan_settings(backend):
    if backend == "express":
        return ["SET TIME ZONE 'UTC'", "SET statement_timeout = 10000"]
    return ["SET TIME ZONE 'Asia/Taipei'"]


# effective_plan_settings 只允許已核對的 startup 設定與實測 SET；未知設定會改變語境，必須拒絕。
def effective_plan_settings(backend, settings):
    startup = startup_plan_settings(backend)
    require(settings[:len(startup)] == startup, "缺少已驗證的 startup 設定")
    effective = {"TimeZone": "Asia/Taipei", "search_path": '"$user", public', "plan_cache_mode": "auto"}
    if backend == "express":
        effective.update(TimeZone="UTC", statement_timeout="10000")
    for setting in settings[len(startup):]:
        if backend != "express" and re.fullmatch(r"(?i:set time zone) 'Asia/Taipei'", setting):
            continue
        require(re.fullmatch(r'(?i:set search_path to) "public"', setting), "不支援的 startup SET，計畫未完成")
        effective["search_path"] = "public"
    return effective


# session_settings 從冷啟開始保留該 session 的 SET／DISCARD，計畫重播同一設定順序。
def session_settings(backend, contexts, completed):
    for event in completed:
        # 啟動設定有些透過協定送出，不一定產生 SQL 日誌；其餘依原生完成事件的順序重播。
        context = contexts.setdefault(event["session_id"], startup_plan_settings(backend))
        query = event["query"].strip().rstrip(";")
        lower = query.lower()
        if event["category"] == "connection":
            require(not event["binds"] and not query.startswith("--") and ";" not in query, "無法重播參數化或複合 session 設定")
            if lower == "discard all" or lower == "reset all":
                context[:] = startup_plan_settings(backend)
            else:
                context.append(query)
                effective_plan_settings(backend, context)
        event["settings"] = list(context)


# evidence_file 限定證據參照在自有目錄內，拒絕絕對路徑、上跳與 symlink。
def evidence_file(relative):
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, "證據參照越出固定目錄")
    result = EVIDENCE / path
    require(not any(parent.is_symlink() for parent in (result, *result.parents)), "證據參照含 symlink")
    return result


# infer_parameter_types 用同一 DB 原生 parser 推導 placeholder 型別；同一 SQL／設定不為每組 bind 重複 PREPARE。
@lru_cache(maxsize=512)
def infer_parameter_types(db_id, cluster_system, query, setup):
    commands = setup + "PREPARE perf01_infer AS " + query + ";\nSELECT to_json(parameter_types) FROM pg_prepared_statements WHERE name='perf01_infer';"
    return query_json({"db_id": db_id}, commands, app=True)


# typed_plan_context 從原生推導加上已鎖定 driver 的參數規則重建 Parse 語境；文字值即使全是數字也維持 text。
def typed_plan_context(backend, event, inferred):
    query = event["query"].strip().rstrip(";")
    arguments = event["binds"]
    require(isinstance(inferred, list) and len(inferred) == len(arguments)
            and all(kind in ("text", "bigint") for kind in inferred), "原生推導型別超出已驗證語境")
    require(re.fullmatch(r"[1-9][0-9]*", event["query_id"]) and int(event["query_id"]) < 1 << 64, "business SQL 缺少原生 query ID")
    types = list(inferred)
    if backend in ("django", "flask", "fastapi"):
        # psycopg 3.3.6 的 Python int 依值選有號 int2、int4、int8。
        for index, (kind, argument) in enumerate(zip(inferred, arguments)):
            if kind == "bigint":
                require(isinstance(argument, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", argument), "psycopg 整數 bind 格式不符")
                number = int(argument)
                require(-(1 << 63) <= number < 1 << 63, "psycopg 整數 bind 超出已驗證 int8")
                types[index] = "smallint" if -(1 << 15) <= number < 1 << 15 else "integer" if -(1 << 31) <= number < 1 << 31 else "bigint"
    elif backend == "aspnet-core":
        # Npgsql 的 perPage 為 int，offset 與 ID 為 long；只替換實際 LIMIT placeholder。
        limit = re.search(r"\bLIMIT\s+\$(\d+)\b", query, re.IGNORECASE)
        if limit:
            index = int(limit[1]) - 1
            require(0 <= index < len(types) and types[index] == "bigint", "Npgsql LIMIT 型別超出已驗證語境")
            types[index] = "integer"
    return {"inferred_types": inferred, "parameter_types": types, "query_id": event["query_id"],
            "effective_settings": effective_plan_settings(backend, event["settings"])}


# plan_key 綁定完整 SQL、實際 bind、有效型別與設定；原有未核對型別的計畫會保留在舊名稱。
def plan_key(value, event, typing):
    return digest(encoded({"query": event["query"].strip().rstrip(";"), "binds": event["binds"],
                           "settings": event["settings"], "typing": typing, "statistics": value["statistics"]}))


# verify_plan 檢查已存計畫的內容、唯讀 app 語境及原始 HTTP 窗口，才能跨後端／輪次重用。
def verify_plan(value, record, key, event, backend, case):
    query, arguments, settings = event["query"].strip().rstrip(";"), event["binds"], event["settings"]
    require(isinstance(record.get("typing"), dict), "既有 plan 未核對原生型別與 query ID")
    typing = typed_plan_context(backend, event, record["typing"]["inferred_types"])
    payload = {name: content for name, content in record.items() if name != "proof_sha256"}
    require(record.get("proof_sha256") == digest(encoded(payload)) and record.get("hash") == key == plan_key(value, event, typing)
            and record.get("sql") == query and record.get("binds") == arguments and record.get("settings") == settings
            and record["typing"] == typing
            and record.get("statistics") == value["statistics"] and record.get("cluster") == value["cluster"], "既有 plan 內容／參數／統計身分不符")
    context = record["context"]
    require(context["role"] == context["session_role"] == "articles_app" and context["database"] == "articles"
            and context["settings"].get("default_transaction_read_only") == "on"
            and context["settings"].get("transaction_read_only") == "on", "既有 plan 未證明唯讀 app role 語境")
    require(context["types"] == typing["parameter_types"] and all(context["settings"].get(name) == setting
            for name, setting in typing["effective_settings"].items()), "plan 實際型別／時區／search_path／planner 設定不符")
    explain = record["explain"]
    require(len(explain) == 1 and "Planning Time" in explain[0] and "Execution Time" in explain[0], "缺少原生 EXPLAIN ANALYZE")
    query_id = explain[0].get("Query Identifier")
    require(type(query_id) is int and str(query_id % (1 << 64)) == typing["query_id"], "計畫原生 Query Identifier 與實測 SQL 不符")
    if event["category"] == "list":
        require(re.search(r"\bLIMIT\b", query, re.IGNORECASE) and explain[0]["Plan"]["Actual Rows"] <= case["per_page"], "列表未在 DB 完成有界分頁")

    # 保留第一個窗口的來源與 query ID；後續窗口也必須具有相同的 driver 型別語境。
    origin = record["origin"]
    sources = {value["source"]}
    for operation in value["operations"]:
        if operation["kind"] in ("pre-init-refresh", "stopped-fixture-refresh") and operation["state"] == "complete":
            sources.update((operation["detail"]["old_source"], operation["detail"]["new_source"]))
    require(origin["source"] in sources and origin["backend"] in BACKENDS and origin["round"] in (1, 2)
            and origin["level"] == value["level"], "既有 plan origin 不在已核定來源／資料級別")
    path = evidence_file(origin["window"])
    require(digest(path.read_bytes()) == origin["window_sha256"], "既有 plan 原始窗口 hash 不符")
    window = load(path)
    require(all(window[name] == origin[name] for name in ("source", "backend", "round", "level"))
            and window["case"]["id"] == origin["case"] and window["oracle_verified"] is True and window["http"]["status"] == 200,
            "既有 plan 原始 HTTP／oracle 證據不符")
    require(any(item["query"].strip().rstrip(";") == query and item["binds"] == arguments and item["settings"] == settings
                and item["category"] == event["category"] and item["query_id"] == typing["query_id"]
                and typed_plan_context(origin["backend"], item, typing["inferred_types"]) == typing for item in window["sql"]),
            "既有 plan 找不到相同 emitted SQL／bind／型別")


# plan 僅重播實際 business SELECT；既有相同 SQL／bind／統計必須通過完整 provenance 驗證才重用。
def plan(value, event, case, origin):
    query = event["query"].strip().rstrip(";")
    arguments = event["binds"]
    require(query.upper().startswith("SELECT ") and ";" not in query and not re.search(r"\b(?:FOR\s+(?:UPDATE|SHARE)|INTO|pg_sleep|pg_terminate_backend)\b", query, re.IGNORECASE), "只允許單一唯讀 business SELECT 計畫")
    placeholders = {int(number) for number in re.findall(r"\$(\d+)", query)}
    require(placeholders == set(range(1, len(arguments) + 1)), "實際 SQL／bind 數目不符，拒絕近似計畫")
    # 只重播已知的 SET，原生推導及明確型別計畫均在唯讀 app 語境。
    effective_plan_settings(origin["backend"], event["settings"])
    prefix = ";\n".join(event["settings"]) + ";\nSET default_transaction_read_only=on;\n"
    inferred = infer_parameter_types(value["db_id"], value["cluster"]["system"], query, prefix) if arguments else []
    typing = typed_plan_context(origin["backend"], event, inferred)
    identity_key = plan_key(value, event, typing)
    path = EVIDENCE / ("plan-" + identity_key + ".json")
    if path.exists():
        record = load(path)
        verify_plan(value, record, identity_key, event, origin["backend"], case)
        return {"hash": identity_key, "origin": record["origin"], "reused": True}
    # 明確 PREPARE 型別後讀回 native role／GUC／型別，EXPLAIN 同時回底層 query ID。
    types = "(" + ",".join(typing["parameter_types"]) + ")" if arguments else ""
    prepared = "PREPARE perf01_plan" + types + " AS " + query + ";\n"
    values = "(" + ",".join(literal(argument) for argument in arguments) + ")" if arguments else ""
    commands = prefix + prepared + "SELECT json_build_object('role',current_user,'session_role',session_user,'database',current_database(),'settings',(SELECT json_object_agg(name,setting) FROM pg_settings WHERE context IN ('user','superuser')),'types',(SELECT to_json(parameter_types) FROM pg_prepared_statements WHERE name='perf01_plan'));\nEXPLAIN (ANALYZE,BUFFERS,VERBOSE,SETTINGS,FORMAT JSON) EXECUTE perf01_plan" + values + ";\nDEALLOCATE perf01_plan;"
    result = sql(value, commands, app=True)
    decoder = json.JSONDecoder()
    context, position = decoder.raw_decode(result)
    explain = json.loads(result[position:].strip())
    record = {"hash": identity_key, "origin": origin, "statistics": value["statistics"], "cluster": value["cluster"], "sql": query,
              "binds": arguments, "settings": event["settings"], "typing": typing, "context": context, "explain": explain}
    record["proof_sha256"] = digest(encoded(record))
    verify_plan(value, record, identity_key, event, origin["backend"], case)
    save(path, record, exclusive=True)
    return {"hash": identity_key, "origin": origin, "reused": False}


# calibration_action 用新 DB app role 的 native psql extended protocol 驗證真實執行次數。
def calibration_action(value, count):
    sql(value, "\n".join("SELECT " + str(number + 1) + ";" for number in range(count)), app=True)


# archive_calibration 依來源保存不可覆寫原件；已有同名副本必須逐位元相同，不能靜默取代。
def archive_calibration(source, contents):
    require(re.fullmatch(r"[0-9a-f]{64}", source), "校準來源 hash 格式不符")
    path = EVIDENCE / ("calibration-" + source + ".json")
    if path.exists():
        load(path)
        require(path.read_bytes() == contents, "既有來源校準副本內容不符；拒絕覆寫")
    else:
        save(path, contents, exclusive=True)


# calibrate 驗已知一次與 1+N 差量、extended bind、SET/DISCARD 及污染拒絕，失敗不擴跑矩陣。
def calibrate(owned=None):
    value = state()
    require(value["initialized"], "必須先 init")
    config_guard(value)
    container_guard(value)
    identity(value)
    if owned:
        owned()
    if value["calibrated"]:
        print(json.dumps({"phase": "calibrate", "status": "already-complete"}))
        return
    logging_settings(value, True)
    results = []
    try:
        for count in (1, 2, 21):
            # callback 只送本輪固定校準 SQL，capture 本身仍走正式原生日誌／counter caller chain。
            def action():
                return calibration_action(value, count)
            _, evidence = capture(value, action, calibration=True)
            require(evidence["all_sql_calls"] == count, "原生 counter 對 1／1+N 不敏感")
            results.append({"kind": "sensitivity", "expected_calls": count, **evidence})
        # psql \bind 先宣告參數再 \g，不能用 Python 近似日誌冒稱 extended 已驗證。
        def extended():
            return sql(value, "SELECT $1::text\n\\bind 'a%_\\\\陳\\'quote'\n\\g\n", app=True)
        _, evidence = capture(value, extended, calibration=True)
        require(evidence["all_sql_calls"] == 1 and evidence["sql"][0]["binds"] == ["a%_\\陳'quote"], "extended protocol／bind 解碼校準不符")
        results.append({"kind": "extended", **evidence})
        # utility callback 讓兩個原生連線管理語句經同一 capture 路徑計數。
        def utility():
            return sql(value, "SET TIME ZONE 'Asia/Taipei';\nDISCARD ALL;", app=True)
        _, evidence = capture(value, utility, calibration=True)
        require(evidence["all_sql_calls"] == 2 and evidence["categories"] == {"connection": 2}, "SET／DISCARD 被漏算")
        results.append({"kind": "utility", **evidence})
        # 同一 simple-query message 含兩個同形 SELECT，驗證不以一筆日誌誤算成一次 SQL。
        def multiple():
            return docker("exec", "--env", "PGAPPNAME=perf01-calibration", "--user", "postgres", value["db_id"],
                          "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "articles_app", "-d", "articles", "-c", "SELECT 11001; SELECT 22002")
        _, evidence = capture(value, multiple, calibration=True)
        require(evidence["all_sql_calls"] == 2 and len(evidence["sql"]) == 2, "複合 simple-query SQL 次數未辨識")
        results.append({"kind": "multiple", **evidence})
        # psql -c 以原生 simple-query 送出同一空註解。須同時看到日誌證據及零 SQL counter。
        def ping():
            return docker("exec", "--env", "PGAPPNAME=perf01-calibration", "--user", "postgres", value["db_id"],
                          "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "articles_app", "-d", "articles", "-c", "-- ping")
        _, evidence = capture(value, ping, calibration=True)
        require(evidence["all_sql_calls"] == 0 and not evidence["sql"] and not evidence["failures"]
                and len(evidence["protocol"]) == 1 and evidence["protocol"][0].get("protocol_kind") == "pgx-empty-ping", "原生空 ping 協定校準不符")
        results.append({"kind": "pgx-empty-ping", **evidence})
        # 污染探針故意走錯 application_name 並延遲，正式 capture 必須拒絕。
        def pollution():
            return sql(value, "SET application_name='perf01-pollution';\nSELECT pg_sleep(0.2);\nSELECT 12345;", app=True)
        rejected = False
        try:
            capture(value, pollution, calibration=True)
        except Failure as error:
            rejected = str(error) == "校準窗口出現非校準 client"
        require(rejected, "受控污染窗口未被拒絕")
        results.append({"kind": "pollution", "rejected": True, "delay_seconds": 0.2})
        contents = encoded(results)
        archive_calibration(value["source"], contents)
        save(EVIDENCE / "calibration.json", contents)
        value["calibrated"] = True
        save(EVIDENCE / "state.json", value)
    finally:
        logging_settings(value, False)
    print(json.dumps({"phase": "calibrate", "status": "complete", "cases": len(results)}))


# fetch_counts 以原生 query ID 計所有資料讀取，包含認證與任何作者查詢；只明示排除交易與連線管理。
def fetch_counts(evidence):
    calls = Counter()
    excluded = Counter()
    for entry in evidence["counters"]:
        query = entry["query"].strip()
        kind = category(query)
        if kind in ("transaction", "connection"):
            excluded[kind] += entry["calls"]
        else:
            require(query.lower().startswith("select "), "GET 出現無法解釋的非 SELECT SQL")
            calls[str(int(entry["queryid"]) % (1 << 64))] += entry["calls"]
    return {"queries": dict(calls), "excluded": dict(excluded), "all_sql_calls": evidence["all_sql_calls"]}


# compare_fetch_pair 不依 auth/business 分類省略查詢，逐 query shape 驗證 1→20 沒有新增資料讀取。
def compare_fetch_pair(pair):
    require(set(pair) == {1, 20} and pair[1]["queries"] == pair[20]["queries"], "資料讀取 SQL 次數隨 per_page 1→20 成長或 query shape 不一致")
    return {"queries": pair[1]["queries"], "excluded_1": pair[1]["excluded"], "excluded_20": pair[20]["excluded"],
            "all_sql_calls_1": pair[1]["all_sql_calls"], "all_sql_calls_20": pair[20]["all_sql_calls"]}


# snapshot_summary 保存原生 reset/eviction 與完整快照 hash；逐筆 changed counter 已另存，不重複大量歷史 SQL。
def snapshot_summary(value):
    return {"reset": value["reset"], "dealloc": value["dealloc"], "entry_count": len(value["entries"]),
            "all_calls": sum(entry["calls"] for entry in value["entries"]), "sha256": digest(encoded(value))}


# verify_window_sql 限制單一 GET 的 count、list 和父文章查詢；原生列數驗證不把 auth 當成無界豁免。
def verify_window_sql(case, evidence):
    emitted = evidence["sql"]
    require(not evidence["failures"], "真實 GET 出現失敗 SQL")
    require(all(event["category"] not in ("other", "data-fetch") for event in emitted), "GET 出現尚未解釋的額外 SQL；拒絕冒稱 auth")
    counts = Counter(event["category"] for event in emitted)
    business = [event for event in emitted if event["category"] in ("count", "list")]
    if case["status"] == 200:
        require(counts["count"] == 1, "成功列表必須恰有一次精確 DB count")
        require(counts["list"] <= 1 and (not case["expected"]["list"] or counts["list"] == 1), "成功列表 DB list 次數不符")
    else:
        require(not business, "拒絕案例執行 business count／list")

    # 只有文章留言路由可讀取同 ID 的窄欄位父文章；欄位、bind 與原生 rows 都需符合。
    counters = {str(int(entry["queryid"]) % (1 << 64)): entry for entry in evidence["counters"]}
    parent_path = re.fullmatch(r"/api/articles/([1-9][0-9]*)/comments", case["path"].split("?", 1)[0])
    require(counts["parent"] <= 1, "GET 出現重複父文章查詢")
    for event in emitted:
        kind = event["category"]
        if kind not in ("parent", "auth"):
            continue
        counter = counters.get(event["query_id"])
        require(counter is not None, "父文章／auth 缺少原生列數證據")
        if kind == "parent":
            query = re.sub(r'"(id|author_id|visibility|articles)"', r'\1', event["query"].strip().rstrip(";"))
            require(parent_path and event["binds"] == [parent_path[1]]
                    and re.fullmatch(r"select\s+(?:id\s*,\s*)?author_id\s*,\s*visibility\s+from\s+articles\s+where\s+id\s*=\s*\$1(?:\s+limit\s+1)?", query, re.IGNORECASE)
                    and counter["rows"] <= 1, "非有界的父文章查詢或路由／bind 不符")
        else:
            require(counter["rows"] <= counter["calls"], "auth 查詢原生列數超出呼叫數")
    return business


# measure_case 讓冷／暖機及正式案例共用完整 oracle、原生 SQL 與計畫證據，避免前置請求漏驗。
def measure_case(value, backend, round_number, case, token, contexts, directory, label, host):
    container_guard(value, backend)
    # actual_get 每個窗口只送一個實際 GET；HTTP 失敗交 capture 保留原始窗口。
    def actual_get():
        return request(backend, "GET", case["path"], token)
    output, evidence = capture(value, actual_get, host, backend=backend)
    try:
        verify_response(case, *output)
        business = verify_window_sql(case, evidence)
        session_settings(backend, contexts, evidence["sql"])
    except (Failure, AssertionError) as error:
        save(directory / (label + "-failed-window.json"), {"case": case["id"], "http": output, "reason": str(error), **evidence}, exclusive=True)
        raise
    # fixture 本身只存一次；窗口保留完整 expected IDs、分頁與完整列 hash，不重複長內文。
    descriptor = {name: content for name, content in case.items() if name != "expected"}
    if case["status"] == 200:
        descriptor.update(expected_page_ids=[row["id"] for row in case["expected"]["list"]],
                          expected_pagination=case["expected"]["pagination"], expected_rows_sha256=digest(encoded(case["expected"]["list"])))
    evidence["before"] = snapshot_summary(evidence["before"])
    evidence["after"] = snapshot_summary(evidence["after"])
    record = {"backend": backend, "round": round_number, "level": value["level"], "source": value["source"],
              "case": descriptor, "oracle_verified": True, "http": {"status": output[0], "response_hash": digest(encoded(output[1]))}, **evidence}
    path = directory / (label + "-window.json")
    save(path, record, exclusive=True)
    origin = {"source": value["source"], "backend": backend, "round": round_number, "level": value["level"],
              "case": case["id"], "window": path.relative_to(EVIDENCE).as_posix(), "window_sha256": digest(path.read_bytes())}
    plans = [plan(value, event, case, origin) for event in business]
    result = {"case": case["id"], "window": origin["window"], "window_sha256": origin["window_sha256"],
              "plans": plans, "fetch": fetch_counts(evidence), "categories": evidence["categories"],
              "list_skipped": not any(event["category"] == "list" for event in business), "phase": label}
    save(directory / (label + "-result.json"), result, exclusive=True)
    return result


# run_backend 對同一 fixture 逐案量測，1/20 比較只用同 actor/filter 的第一頁暖機結果。
def run_backend(value, backend, round_number):
    require(not value.get("logging"), "登入前 app 詳細日誌仍啟用")
    data = fixture(value["level"])
    verify_fixture(value, data)
    unit = f"{value['level']}/{round_number}/{backend}"
    directory = EVIDENCE / f"attempt-{value['level']}-{round_number}-{backend}-{time.time_ns()}-{secrets.token_hex(3)}"
    private_dir(directory)
    attempt = {"unit": unit, "source": value["source"], "path": directory.name, "status": "running"}
    for previous in value.setdefault("attempts", []):
        if previous["unit"] == unit and previous["status"] == "running":
            previous["status"] = "interrupted-readonly"
    value["attempts"].append(attempt)
    save(EVIDENCE / "state.json", value)
    save(directory / "attempt.json", attempt, exclusive=True)
    print(json.dumps({"phase": "run", "status": "starting", "unit": unit, "attempt": directory.name}), flush=True)
    started = False
    results = []
    contexts = {}
    comparisons = {}
    primary = None
    try:
        start_backend(value, backend)
        started = True
        tokens = {actor: actor_token(value, backend, actor) for actor in ("member", "admin")}
        stop_backend(value, backend)
        started = False
        logging_settings(value, True)
        start_backend(value, backend, readiness=False)
        started = True
        found = container_guard(value, backend)
        host = found[backend]["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
        all_cases = list(cases(data))
        first = {(case["kind"], case["actor"]): case for case in reversed(all_cases) if case["status"] == 200
                 and case["page"] == 1 and case["per_page"] == 20 and "/keyword-0/" in case["id"]}
        # 冷請求與三個暖機也走同一完整 oracle／plan 入口，並與正式 1/20 對照分開保存。
        cold = measure_case(value, backend, round_number, first[("public-articles", "guest")], None, contexts, directory, "cold", host)
        warm = []
        for index, branch in enumerate((("public-articles", "guest"), ("own-articles", "member"), ("members", "admin"))):
            case = first[branch]
            warm.append(measure_case(value, backend, round_number, case, tokens.get(case["actor"]), contexts, directory, f"warm-{index}", host))
        for index, case in enumerate(all_cases):
            result = measure_case(value, backend, round_number, case, tokens.get(case["actor"]), contexts, directory, f"case-{index:04d}", host)
            results.append(result)
            if case["status"] == 200 and case["page"] == 1:
                key = case["id"].rsplit("/", 2)[0]
                comparisons.setdefault(key, {})[case["per_page"]] = result["fetch"]
            if index % 25 == 0:
                print(json.dumps({"phase": "run", "backend": backend, "level": value["level"], "round": round_number, "cases_done": index + 1, "cases_total": len(all_cases)}), flush=True)
        compared = {key: compare_fetch_pair(pair) for key, pair in comparisons.items()}
        index_path = directory / "index.json"
        save(index_path, {"unit": unit, "source": value["source"], "fixture_sha256": digest(encoded(data)),
                          "case_ids": [case["id"] for case in all_cases], "cold": cold, "warm": warm,
                          "results": results, "comparisons": compared}, exclusive=True)
    except BaseException as error:
        primary = error
        raise
    finally:
        # 先關詳細 logging 再停現存連線；中斷時只恢復固定新環境並保留全部結果。
        cleanup = []
        if value.get("logging"):
            try:
                logging_settings(value, False)
            except (Failure, OSError, ValueError, KeyError, TypeError) as error:
                cleanup.append(type(error).__name__)
        if started:
            try:
                stop_backend(value, backend)
            except (Failure, OSError, ValueError, KeyError, TypeError) as error:
                cleanup.append(type(error).__name__)
        try:
            require(schema_snapshot(value) == value["schema"], "量測後業務 schema／grants／索引改變")
        except (Failure, OSError, ValueError, KeyError, TypeError) as error:
            cleanup.append(type(error).__name__)
        attempt["status"] = "incomplete" if primary is not None or cleanup else "complete"
        try:
            save(directory / "attempt.json", {**attempt, "cleanup_errors": cleanup})
            save(EVIDENCE / "state.json", value)
        except (Failure, OSError):
            cleanup.append("evidence-save")
        if cleanup:
            if primary is None:
                raise Failure("本輪限定恢復未完成；保留 attempt 與 receipt")
            print("PERF01 本輪限定恢復另有失敗；首個錯誤及 attempt 證據均保留", file=sys.stderr)
    value.setdefault("unit_results", {})[unit] = {"index": index_path.relative_to(EVIDENCE).as_posix(), "sha256": digest(index_path.read_bytes())}
    value["completed"].append(unit)
    save(EVIDENCE / "state.json", value)
    print(json.dumps({"phase": "run", "status": "unit-complete", "unit": unit, "completed_units": len(value["completed"])}), flush=True)


# verify_carried_unit 只接受兩次已核准的承接，並驗證較早六組的原始索引確實經過第一份 receipt。
def verify_carried_unit(value, unit, reference, measured_source):
    refreshes = [item for item in value["operations"] if item["kind"] in ("pre-init-refresh", "stopped-fixture-refresh") and item["state"] == "complete"]
    require(refreshes, "既有單位來源缺少當前承接資格")
    latest = refreshes[-1]
    previous_source = latest.get("source")
    expected_source = PING_CARRY_SOURCE if unit in PING_CARRY_UNITS else CLOCK_CARRY_SOURCE
    allowed = {PING_CARRY_SOURCE: PING_CARRY_UNITS, CLOCK_CARRY_SOURCE: CLOCK_CARRY_UNITS}.get(previous_source, ())
    require(unit in allowed and measured_source == expected_source, "既有單位來源缺少當前承接資格")
    transitions = [(latest, previous_source, value["source"])]
    if previous_source == CLOCK_CARRY_SOURCE and unit in PING_CARRY_UNITS:
        require(len(refreshes) >= 2, "較早量測缺少第一次承接資格")
        transitions.append((refreshes[-2], PING_CARRY_SOURCE, CLOCK_CARRY_SOURCE))
    for receipt, old_source, new_source in transitions:
        detail = receipt.get("detail", {})
        require(receipt.get("kind") == "stopped-fixture-refresh" and receipt.get("source") == old_source
                and receipt.get("cluster") == value["cluster"] and detail.get("old_source") == old_source
                and detail.get("new_source") == new_source
                and detail.get("carried_units", {}).get(unit) == {**reference, "source": measured_source}, "既有單位來源缺少當前承接資格")
        # 每一代都核對目前 manifest 的案例與 Compose，不能用不同輸入借用舊結果。
        for filename in ("perf01_cases.py", "perf01_compose.yaml"):
            name = "ops/tests/" + filename
            metadata = value["manifest"][name]
            require(detail["files"].get(name) == {"old": metadata, "new": metadata}, "承接後案例或 Compose 身分不符")
    return previous_source


# verify_unit 核對既有 index／窗口／plan。舊結果保留實測來源，且再次掃描適用修正的原始 SQL。
def verify_unit(value, unit, *, audit_no_ping=False, audit_no_clock=False):
    reference = value.get("unit_results", {}).get(unit)
    require(reference, "完成單位缺少結果 index")
    path = evidence_file(reference["index"])
    require(digest(path.read_bytes()) == reference["sha256"], "完成單位 index hash 不符")
    index = load(path)
    measured_source = index["source"]
    carried = measured_source != value["source"]
    if carried:
        previous_source = verify_carried_unit(value, unit, reference, measured_source)
        audit_no_ping = audit_no_ping or measured_source == PING_CARRY_SOURCE
        audit_no_clock = audit_no_clock or previous_source == CLOCK_CARRY_SOURCE
    level, round_text, backend = unit.split("/")
    data = fixture(level)
    matrix = {case["id"]: case for case in cases(data)}
    require(index["unit"] == unit and index["fixture_sha256"] == digest(encoded(data))
            and index["case_ids"] == list(matrix) and len(index["results"]) == len(matrix)
            and [result["case"] for result in index["results"]] == list(matrix) and len(index["warm"]) == 3, "完成單位來源／fixture／案例覆蓋不符")
    scoped = {**value, "level": level, "statistics": load(EVIDENCE / (level + "-statistics.json"))}
    pairs = {}
    for result in [index["cold"], *index["warm"], *index["results"]]:
        window_path = evidence_file(result["window"])
        require(digest(window_path.read_bytes()) == result["window_sha256"], "完成窗口 hash 不符")
        window = load(window_path)
        require(window["oracle_verified"] is True and window["case"]["id"] == result["case"] and window["source"] == measured_source
                and window["backend"] == backend and window["round"] == int(round_text) and window["level"] == level, "完成窗口來源／actor 案例不符")
        if audit_no_ping or audit_no_clock:
            records = [json.loads(line) for line in window["raw"].splitlines()]
            if audit_no_ping:
                require(not any(pgx_empty_ping(record) for record in records), "舊窗口含本次修正的空 ping，拒絕承接")
            if audit_no_clock:
                emitted, _, _ = events(records)
                require(not any(nestjs_auth_clock(event["query"]) for event in emitted), "舊窗口含本次修正的 NestJS 時間查詢，拒絕承接")
        require(fetch_counts(window) == result["fetch"], "完成窗口的 native fetch calls 不符")
        case = matrix[result["case"]]
        business = verify_window_sql(case, window)
        require(len(business) == len(result["plans"]), "完成窗口缺少 emitted business SQL 計畫")
        for event, reference_plan in zip(business, result["plans"]):
            key = reference_plan["hash"]
            require(re.fullmatch(r"[0-9a-f]{64}", key), "完成窗口 plan 參數 hash 格式不符")
            record = load(evidence_file("plan-" + key + ".json"))
            require(reference_plan["origin"] == record["origin"], "完成窗口 plan origin 不符")
            verify_plan(scoped, record, key, event, backend, case)
        if result["phase"].startswith("case-") and case["status"] == 200 and case["page"] == 1:
            pairs.setdefault(case["id"].rsplit("/", 2)[0], {})[case["per_page"]] = result["fetch"]
    require(index["comparisons"] == {key: compare_fetch_pair(pair) for key, pair in pairs.items()}, "完成單位的 1/20 比較不符")
    if carried:
        return {**reference, "source": measured_source, "carried": True}
    return reference


# run_matrix 固定兩級各兩輪十二後端；同 volume 重跑略過已完整結束的單位，不重送未知寫入。
def run_matrix(owned=None):
    value = state()
    require(value["initialized"] and value["calibrated"], "必須先 init 及 calibrate")
    config_guard(value)
    container_guard(value)
    identity(value)
    if owned:
        owned()
    require(schema_snapshot(value) == value["schema"], "業務 schema 漂移")
    require(len(value["completed"]) == len(set(value["completed"])), "完成單位重複")
    for unit in value["completed"]:
        verify_unit(value, unit)
    for level in LEVELS:
        if level == "small" and value["level"] == "medium":
            require(all(f"small/{number}/{backend}" in value["completed"] for number in (1, 2) for backend in BACKENDS), "中量之前小量完整結果缺漏")
            continue
        if value["level"] != level:
            seed(value, level)
        for round_number in (1, 2):
            for backend in BACKENDS:
                if f"{level}/{round_number}/{backend}" not in value["completed"]:
                    run_backend(value, backend, round_number)
    require(len(value["completed"]) == 48, "完整矩陣必須有兩級×兩輪×十二後端")
    save(EVIDENCE / "matrix-index.json", {"source": value["source"], "cluster": value["cluster"],
                                         "units": {unit: verify_unit(value, unit) for unit in value["completed"]}})
    print(json.dumps({"phase": "run", "status": "complete", "units": len(value["completed"])}))


# stop 僅恢復已識別新環境的 logging 並停自有容器；pending 業務寫入一律保留未知。
def stop():
    value = state(allow_pending=True)
    owned_db(value)
    config_guard(value)
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT).decode().split()
    require(ids, "沒有已識別的新容器可停止；保留 receipt")
    running = [entry["Config"]["Labels"]["com.docker.compose.service"] for entry in json_docker("inspect", *ids) if entry["State"]["Running"] and entry["Config"]["Labels"]["com.docker.compose.service"] in BACKENDS]
    require(len(running) <= 1, "同時有多個 backend，拒絕猜測恢復")
    found = container_guard(value, running[0] if running else None, running=False)
    stopped_app = None
    require(found["db"]["State"]["Running"] or not running, "DB 已停止且 backend 仍在執行，無法完成 app session 讀回")
    if found["db"]["State"]["Running"]:
        identity(value)
        # 限定恢復不重播 pending 原操作；RESET 的實際結果獨立 readback 後記錄。
        recovery_path = EVIDENCE / ("recovery-" + str(time.time_ns()) + ".json")
        recovery = {"state": "pending", "cluster": value["cluster"], "db_id": value["db_id"], "action": "restore-logging-and-stop"}
        save(recovery_path, recovery, exclusive=True)
        if value["initialized"]:
            sql(value, "ALTER ROLE articles_app RESET log_min_duration_statement; ALTER ROLE articles_app RESET log_parameter_max_length")
            actual = query_json(value, "SELECT coalesce(to_json(rolconfig),'[]'::json) FROM pg_roles WHERE rolname='articles_app'")
            require(actual == value["logging_original"], "恢復 logging readback 不符")
            value["logging"] = False
            recovery["logging_readback"] = actual
        if running:
            stopped_app = stop_backend(value, running[0])
        if value["initialized"]:
            require(schema_snapshot(value) == value["schema"], "停止前業務 schema 已改變")
        # logging 有同 cluster 的實際 readback 才解除 pending，其他寫入等各自證據。
        for operation in value["operations"]:
            if (value["initialized"] and operation["state"] == "pending" and operation["kind"] == "logging"
                    and operation["cluster"] == value["cluster"] and operation["source"] == value["source"]
                    and operation["detail"]["original"] == value["logging_original"]):
                operation["state"] = "recovered"
        save(EVIDENCE / "state.json", value)
        docker("stop", "--time", "15", value["db_id"], timeout=90)
        require(not json_docker("inspect", value["db_id"])[0]["State"]["Running"], "固定 DB 未停止")
    require(not docker("ps", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT).strip(), "固定 project 未全部停止")
    if found["db"]["State"]["Running"]:
        # 只有確認同一 immutable image 已精確停止並排空 session，才能解除該次未知啟動。
        recovered = []
        for operation in value["operations"]:
            if (stopped_app and operation["state"] == "pending" and operation["kind"] == "backend-start"
                    and operation["source"] == value["source"] and operation["cluster"] == value["cluster"]
                    and operation["detail"] == {"backend": running[0], "image": value["images"][running[0]]}):
                operation["state"] = "recovered"
                recovered.append({"receipt_time": operation["time"], "container_id": stopped_app, **operation["detail"]})
        save(recovery_path, {**recovery, "state": "complete", "backend_starts": recovered})
        if recovered:
            save(EVIDENCE / "state.json", value)
    else:
        # 已停止時無法讀回 DB，只接受最新同容器的完成復原與既有 logging readback。
        latest = max(EVIDENCE.glob("recovery-*.json"), default=None)
        require(value.get("logging") is False and value.get("logging_original") == [] and latest
                and not any(EVIDENCE.glob("recovery-*.json.pending"))
                and not any(item["kind"] == "logging" and item["state"] == "pending" for item in value["operations"]),
                "DB 已停止但 logging 恢復未確認；保留 receipt")
        receipt = load(latest)
        require(receipt.get("state") == "complete" and receipt.get("cluster") == value["cluster"] and receipt.get("db_id") == value["db_id"]
                and receipt.get("action") == "restore-logging-and-stop" and receipt.get("logging_readback") == value["logging_original"],
                "DB 已停止但缺少同 cluster 的 logging 恢復 readback")
    print(json.dumps({"phase": "stop", "status": "stopped", "volume_preserved": VOLUME,
                      "pending": sum(item["state"] == "pending" for item in value["operations"])}))


# lifecycle_lock 用原生非阻塞 flock 序列化入口，避免兩程序同時通過 pending 檢查。
@contextmanager
def lifecycle_lock():
    require(not any(parent.is_symlink() for parent in (ROOT / ".local", *ROOT.parents)), "lock 父路徑不可為 symlink")
    # 不改既有 .local 目錄權限或內容；只建立本輪專用 lock 檔。
    (ROOT / ".local").mkdir(mode=0o700, exist_ok=True)
    path = ROOT / ".local/perf01.lock"
    require(not path.is_symlink(), "lifecycle lock 不可為 symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("另一個 PERF01 lifecycle 正在執行") from None
        yield
    finally:
        os.close(descriptor)


# main 先解析有限命令；持鎖後確認 DB 歸屬，原命令失敗時仍由同一個持鎖者恢復。
def main(arguments=None):
    parser = argparse.ArgumentParser(description="固定 PERF01 新環境量測；不接受自訂 DB／project／URL")
    parser.add_argument("--version", action="version", version="PERF01 " + VERSION)
    parser.add_argument("phase", choices=("prepare", "refresh", "init", "calibrate", "run", "stop"))
    phase = parser.parse_args(arguments).phase
    cleanup_allowed = False

    # owned 只由通過精確容器與 cluster 讀回的執行路徑呼叫，阻止 preflight 失敗走停機。
    def owned():
        nonlocal cleanup_allowed
        cleanup_allowed = True

    errors = (Failure, AssertionError, KeyboardInterrupt, OSError, ValueError, KeyError, TypeError)
    try:
        with lifecycle_lock():
            try:
                operation = {"prepare": prepare, "refresh": refresh, "init": initialize, "calibrate": calibrate, "run": run_matrix, "stop": stop}[phase]
                if phase in ("init", "calibrate", "run"):
                    operation(owned)
                else:
                    operation()
            except errors as error:
                status = report_failure(phase, error)
                if cleanup_allowed:
                    failure_cleanup()
                return status
            return 0
    except errors as error:
        # 取鎖或釋鎖失敗時只回報，禁止再取得鎖並變更他人正在使用的 fixture。
        return report_failure(phase, error)


# report_failure 保留固定診斷與取消狀態；外部例外訊息可能含 credential，只留在私密證據。
def report_failure(phase, error):
    if isinstance(error, (Failure, AssertionError)):
        reason = str(error)
    elif isinstance(error, KeyboardInterrupt):
        reason = "操作中斷；已送出寫入的結果仍依 pending receipt 查證，禁止盲目重送"
    else:
        reason = "工具／證據格式異常；保留 receipt，請查核私密證據"
    print(json.dumps({"phase": phase, "status": "incomplete", "reason": reason}, ensure_ascii=False), file=sys.stderr)
    return 130 if isinstance(error, KeyboardInterrupt) else 1


# failure_cleanup 在原鎖內執行受限 stop；第二個失敗不掩蓋原錯誤與未知寫入結果。
def failure_cleanup():
    try:
        stop()
    except (Failure, KeyboardInterrupt, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"phase": "recovery", "status": "incomplete", "reason": "限定恢復未完成；receipt 保留，禁止盲目重送"}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
