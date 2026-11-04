"""CQ32 離線回歸：真函式配受控依賴，只證分支／清理，不代表真 DB 或歷史 HTTP suite。"""

import base64
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import re
import time
import sqlite3
import sys
import hashlib
import copy
import subprocess
from email.message import Message
from unittest.mock import Mock, patch

import cq32_http as driver
import ticket33_http as prepare
import ticket37_lifecycle as lifecycle
import ticket28_http as echo
import ticket30_http as administration
import ticket21_http as flask_settings
import ticket24_http as fast_settings
import ticket22_http as reading
import ticket32_http as fiber
import ticket57_http as ddl57
import ticket39_http as ddl39
import cq24_http as cq24
import cq09_http as cq09
import cq10_http as cq10
import cq11_http as cq11
import cq18_http as cq18
import ticket40_http as transport


# entered 只暫時切到自有目錄；離開時一定回到原 cwd，避免下一案誤用 fixture。
@contextlib.contextmanager
def entered(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


# login_fixture 產生完全合成的 claims/持久資料，不讀 repository .env 或金鑰。
def login_fixture(backend="fastify"):
    claims = {"jti": base64.urlsafe_b64encode(b"\0" * 16).decode().rstrip("="), "iat": 0, "exp": 28800}
    token = "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"
    login = {"access_token": token, "expires_at": "1970-01-01 16:00:00", "user": {"id": 7}}
    state = {"jti": claims["jti"], "account_id": 7, "backend_id": backend,
             "issued_at": "1970-01-01T00:00:00", "expires_at": "1970-01-01T08:00:00",
             "issued_epoch": "0.000000", "expires_epoch": "28800.000000", "revoked_at": None}
    return login, state


# Response 提供假工具/API 的 context-manager 介面，絕不開 socket。
class Response:
    # 保留狀態與 bytes，讓被測函式仍執行自己的讀取／狀態斷言。
    def __init__(self, body=b"OK", status=200):
        self.body, self.status = body, status

    # 假回應也經由與真 urllib 相同的 with 邊界進入。
    def __enter__(self):
        return self

    # 不吞例外，保留被測程式的原始失敗。
    def __exit__(self, *args):
        return False

    # 回傳預置 bytes，沒有任何外部 I/O。
    def read(self):
        return self.body


# GuardFixture 只建立暫存副本與假 inspect 結果；實際 driver.guard 原樣執行。
class GuardFixture:
    # 建立最小來源、獨立合成 keys 與實際固定 labels，供正反案例改動單一邊界。
    def __init__(self, folder):
        self.repo = Path(folder)
        self.root = self.repo / ".local/cq32-fixture"
        self.services = {}
        self.source_names = ["ops/nginx/default.conf"]
        for path, content in ((self.repo / "ops/nginx/default.conf", "user working nginx"),
                              (self.root / "ops/nginx/default.conf", "candidate nginx")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for index, backend in enumerate(driver.BACKENDS):
            relative = f"backends/{backend}/Dockerfile"
            self.source_names.append(relative)
            for root in (self.repo, self.root):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("FROM scratch\n")
                key = root / f".local/keys/{backend}.key"
                key.parent.mkdir(parents=True, exist_ok=True)
                key.write_bytes(base64.b64encode(bytes([index + 40]) * 32))
            key = self.root / f".local/cq32-keys/{backend}.key"
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_bytes(base64.b64encode(bytes([index + 1]) * 32))
        names = ["db", "nginx", *driver.BACKENDS, *("frontend-" + name for name in driver.FRONTENDS)]
        for name in names:
            labels = {"com.docker.compose.project": driver.PROJECT, "com.docker.compose.service": name,
                      "com.docker.compose.project.working_dir": str(self.root),
                      "com.docker.compose.project.config_files": ",".join(str(self.root / file) for file in driver.FILES)}
            self.services[name] = {"Config": {"Labels": labels, "Env": []}, "State": {"Running": True, "Health": {"Status": "healthy"}},
                                   "NetworkSettings": {"Networks": {driver.PROJECT + "_default": {}}},
                                   "Image": "sha256:image-" + name, "Mounts": [], "HostConfig": {"PortBindings": {}}}
        database = self.services["db"]
        database["Config"]["Env"] = ["CQ32_FIXTURE=isolated-cq32"]
        database["Mounts"] = [{"Type": "volume", "Name": driver.PROJECT + "_postgres_data", "Destination": "/var/lib/postgresql"}]
        for index, name in enumerate(driver.BACKENDS):
            item = self.services[name]
            item["Config"]["Env"] = ["DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"]
            internal = "8080/tcp" if name == "aspnet-core" else "8000/tcp"
            item["HostConfig"]["PortBindings"] = {internal: [{"HostIp": "127.0.0.1", "HostPort": str(8081 + index)}]}
            item["Mounts"] = [{"Type": "bind", "Source": str(self.root / f".local/cq32-keys/{name}.key"),
                               "Destination": f"/run/secrets/{name}-jwt.key", "RW": False}]
        self.services["nginx"]["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
        self.config = {"name": driver.PROJECT, "volumes": {"postgres_data": {"name": driver.PROJECT + "_postgres_data"}},
                       "services": {"db": {"environment": {"CQ32_FIXTURE": "isolated-cq32"}}},
                       "networks": {"default": {"ipam": {"config": [{"subnet": "10.251.132.0/24"}]}}}}
        for path in (self.root / ".local/cq32-compose.yaml", self.repo / "ops/tests/cq32_compose.yaml"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic fixed template")

    # 假 Docker 僅接受被測固定命令；未知分支直接失敗，防止 mock 意外吞掉新操作。
    def command(self, *args):
        if args[:2] == ("docker", "inspect"):
            return json.dumps([self.services[args[-1]]])
        if args[:3] == ("docker", "network", "inspect"):
            return json.dumps([{"Name": driver.PROJECT + "_default", "Labels": {"com.docker.compose.project": driver.PROJECT}, "IPAM": {"Config": [{"Subnet": "10.251.132.0/24", "Gateway": "10.251.132.1"}]}}])
        if args[:3] == ("docker", "volume", "inspect"):
            return json.dumps([{"Name": driver.PROJECT + "_postgres_data", "Labels": {"com.docker.compose.project": driver.PROJECT, "com.docker.compose.volume": "postgres_data"}, "Options": None}])
        fixed = ("docker", "compose", "-p", driver.PROJECT, "-f", "compose.yaml", "-f", ".local/cq32-compose.yaml", "--profile", "*")
        if args[:len(fixed)] != fixed:
            raise AssertionError("unexpected fake command")
        tail = args[len(fixed):]
        if tail[:1] == ("config",):
            return json.dumps(self.config)
        if tail[:2] == ("ps", "-q"):
            return tail[-1]
        if tail[:2] == ("images", "-q"):
            return "image-" + tail[-1]
        if tail[:1] == ("exec",) and tail[-1] == "SELECT current_database()||':'||current_user":
            return "articles:articles_owner"
        raise AssertionError("unexpected fake compose command")

    # Git 假結果只回傳這個暫存來源集合，不讀原 repository 或任何秘密。
    def git(self, args, **kwargs):
        if "ls-files" in args:
            return "\0".join(self.source_names).encode()
        if args[-1] == "HEAD:ops/nginx/default.conf":
            return b"candidate nginx"
        raise AssertionError("unexpected fake git command")

    # health/前端僅接受 GET URL；沒有 Request write 可被此假回應放行。
    def opened(self, url, **kwargs):
        if not isinstance(url, str):
            raise AssertionError("guard must not write")
        special = any(url == driver.BASES[name] + "/up" for name in ("symfony", "aspnet-core"))
        return Response(b'{"status":"ok"}' if special else b"OK")

    # 安裝 I/O 依賴及暫存路徑，保留 guard/check/有限目標與分支本身。
    @contextlib.contextmanager
    def active(self):
        with entered(self.root), patch.object(driver, "REPO", self.repo), patch.object(driver, "ROOT", self.root), \
                patch.object(driver, "command", self.command), patch.object(driver.subprocess, "check_output", self.git), \
                patch.object(driver.OPENER, "open", self.opened), \
                patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": driver.PROJECT, "COMPOSE_FILE": ":".join(driver.FILES)}):
            yield


# BoundaryDone 區分 protected 段正常完成與故障；不執行後續內容或 DB 停止案例。
class BoundaryDone(Exception):
    pass


# Connection 保留 psycopg 的 context/execute/fetch 邊界，資料由每個明確案例提供。
class Connection:
    # 僅注入 query 結果，不連線、不模擬通用資料庫。
    def __init__(self, query):
        self.query = query
        self.result = None

    # 讓真函式進入原本交易 with，例外由原函式決定處置。
    def __enter__(self):
        return self

    # 不吞錯，否則無法驗 finally 與原錯誤可追溯性。
    def __exit__(self, *args):
        return False

    # 記住本次假查詢結果，提供原 fetchone/fetchall 介面。
    def execute(self, statement, *args):
        self.result = self.query(statement)
        return self

    # 回傳此案例指定的單筆值，不放寬原程式斷言。
    def fetchone(self):
        return self.result

    # 排序 oracle 案例可提供多筆，仍由真正 SQL 或明確 fixture 決定結果。
    def fetchall(self):
        return self.result

    # 假交易提交不代表真 DB 提交，只保留原控制流。
    def commit(self):
        return None

    # 假回滾不改其他案例狀態，避免把它當成資料恢復證據。
    def rollback(self):
        return None


# Holder 只模擬 psql 的列鎖握手；沒有 timeout 的舊分支會立即報錯而非掛住測試。
class Holder:
    # 每案獨立的假管線及退出狀態，避免依賴真 DB 或睡眠。
    def __init__(self, blocked=False, cleanup_fails=False):
        self.stdin = io.StringIO()
        self.stdout = self
        self.stderr = io.StringIO("synthetic lock timeout")
        self.returncode = None
        self.blocked = blocked
        self.terminated = False
        self.cleanup_fails = cleanup_fails

    # 依真函式送入的 SQL 判斷 server lock timeout 是否存在；舊無界路徑不能假綠。
    def readline(self):
        if self.blocked:
            if "SET LOCAL lock_timeout='8s';" not in self.stdin.getvalue():
                raise RuntimeError("unbounded lock handshake")
            return ""
        return "7\n"

    # 假 process 輪詢只回報本案狀態。
    def poll(self):
        return self.returncode

    # 確認 finally 有實際要求終止 holder，不把 timeout 當成已回收。
    def terminate(self):
        self.terminated = True
        self.returncode = -15

    # 收束假程序狀態，提供原 wait(timeout) 介面。
    def wait(self, timeout):
        if self.terminated and self.cleanup_fails:
            raise OSError("holder cleanup failed")
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


# LegacyRestore 只模擬 auth 依賴來走真 main/finally，expect stub 不構成 HTTP 契約證據。
class LegacyRestore:
    # 固定每張舊票的工具前提與合成狀態；不改 guard/check，不讀原 keys。
    def __init__(self, module, number, backend, enabled, fault):
        self.module, self.number, self.backend, self.fault = module, number, backend, fault
        self.original = f"{enabled},9,17,720"
        self.restored = None
        self.ports = {25: ("8087", "8091", "8090"), 28: ("8088", "18091", "18090"), 31: ("8089", "18312", "18311")}[number]
        self.members, self.failures, self.window = {}, {}, {}
        self.edges, self.races, self.enabled, self.lock_id = 0, 0, True, None
        now = int(time.time())
        claims = {"iss": "ai-workflow-fullstack:fiber", "aud": "ai-workflow-fullstack-api:fiber", "sub": "1",
                  "jti": "a" * 22, "iat": now, "nbf": now, "exp": now + 28800}
        self.token = "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"

    # 原 guard 仍自行解析這些 inspect/config 資料；此假工具不接受未知命令。
    def output(self, args, **kwargs):
        project = f"ticket{self.number}_test"
        if "config" in args:
            return json.dumps({"name": project, "volumes": {"postgres_data": {"name": project + "_postgres_data"}},
                               "services": {self.backend: {"ports": [{"published": self.ports[0], "host_ip": "127.0.0.1"}]},
                                            "laravel": {"ports": [{"published": self.ports[1], "host_ip": "127.0.0.1"}]},
                                            "nginx": {"ports": [{"published": self.ports[2], "host_ip": "127.0.0.1"}]},
                                            "db": {"environment": {f"TICKET{self.number}_FIXTURE": f"isolated-ticket{self.number}"}}}})
        if "ps" in args:
            return args[-1]
        if args[:2] == ["docker", "inspect"]:
            service = args[-1]
            mounts = [{"Name": project + "_postgres_data"}]
            if service != "db":
                mounts.append({"Type": "bind", "Source": os.path.realpath(f".local/ticket{self.number}-backend/{service}.key"),
                               "Destination": f"/run/secrets/{service}-jwt.key", "RW": False})
                mounts.append({"Type": "bind", "Source": os.path.realpath(f".local/ticket{self.number}-backend/cors.php"),
                               "Destination": "/srv/laravel/config/cors.php", "RW": False})
            return json.dumps([{"Config": {"Labels": {"com.docker.compose.project": project, "com.docker.compose.service": service},
                                           "Env": ["CORS_ORIGIN=http://localhost:18311"]},
                                "State": {"Running": True, "Health": {"Status": "healthy"}}, "Mounts": mounts,
                                "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": self.ports[0] if service == self.backend else self.ports[1]}]}}}])
        raise AssertionError("unexpected legacy fake tool")

    # 假註冊 helper 只配置唯一 id/token；真正待證的是其後四欄還原控制流。
    def register(self, tag, nickname="Ticket"):
        email = tag + "@example.test"
        member_id = len(self.members) + 1
        self.members[email] = member_id
        if tag.startswith("lock"):
            self.lock_id = member_id
        return email, {"access_token": self.token, "user": {"id": member_id, "email": email, "nickname": nickname}}

    # 提供 main 分支所需的狀態／計數，不替代 actual check 或 finally 的 UPDATE。
    def sql(self, query):
        if "current_database()" in query:
            return "articles:articles_owner"
        if query.startswith("SELECT enabled||"):
            return self.original
        if query.startswith("UPDATE login_lockout_settings SET enabled="):
            if "window_minutes=9," in query:
                self.restored = query
                if self.fault == "both":
                    raise OSError("restore failed")
            else:
                self.enabled = "enabled=true" in query
                if self.fault in ("write", "both") and "window_minutes=5," in query:
                    raise RuntimeError("setting write failed after effect")
            return ""
        if query.startswith("INSERT INTO articles"):
            raise BoundaryDone("restored boundary completed")
        if "SELECT password_hash" in query:
            return "$argon2id$v=19$m=19456,t=2,p=1$" + "a" * 22 + "$" + "a" * 43
        if "SELECT count(*) FROM accounts WHERE email=" in query:
            return "1"
        if "SELECT count(*) FROM accounts" in query:
            return str(self.edges + len(self.members) + (1 if self.races else 0))
        if "SELECT count(*) FROM login_sessions" in query:
            return "1"
        if "SELECT count(*) FROM login_failures" in query:
            member_id = int(query.split("account_id=")[1].split()[0])
            return str(self.failures.get(member_id, 0))
        if query.startswith("UPDATE accounts SET locked_until="):
            self.window[self.lock_id] = 0
        if query.startswith("UPDATE login_failures SET occurred_at="):
            self.window[self.lock_id] = 0
        return ""

    # 只回傳足以走還原路徑的 HTTP 依賴資料；本類 expect 不驗 HTTP contract。
    def call(self, base, method, path, body=None, token=None, headers=None):
        status = 200
        if method == "POST" and path == "/api/auth/register" and isinstance(body, dict):
            email = body.get("email", "").strip().lower()
            if email.startswith("edge") and body.get("nickname") == "Edge" and re.fullmatch(r"[A-Za-z0-9]{4}|[A-Za-z0-9]{16}", body.get("password", "")):
                self.edges += 1
            if email.startswith("race"):
                self.races += 1
                status = 201 if self.races == 1 else 422
        if method == "POST" and path == "/api/auth/login" and isinstance(body, dict):
            member_id = self.members.get(body.get("email"))
            if member_id and body.get("password") == "wrong123":
                if self.enabled:
                    self.failures[member_id] = self.failures.get(member_id, 0) + 1
                    self.window[member_id] = self.window.get(member_id, 0) + 1
                    status = 429 if self.window[member_id] >= 2 else 401
                else:
                    status = 401
            elif member_id and self.enabled and self.window.get(member_id, 0) >= 2:
                status = 429
        return status, {"access_token": self.token, "user": {"id": 1, "email": body.get("email") if isinstance(body, dict) else "fake@example.test"}}, {"Retry-After": "120", "Access-Control-Expose-Headers": "Retry-After"}

    # 明確隔離 expect 依賴，不宣稱這個 stub 執行了歷史 HTTP envelope 斷言。
    def expect(self, result, *args, **kwargs):
        return result[1]


# DDLDatabase 只提供兩組固定物件的離線 catalog／交易結果，讓實際 lifecycle 自己判定 ownership。
class DDLDatabase:
    # 只建兩個固定物件的假 catalog；不是 SQL／PostgreSQL 模擬平台或 live 證據。
    def __init__(self, ticket):
        self.ticket = ticket
        self.function = "ticket57_http_boundary_shift" if ticket == "57" else "ticket39_fail_epoch"
        self.trigger = "ticket57_http_boundary_shift" if ticket == "57" else "ticket39_rollback_probe"
        self.table = "login_failures" if ticket == "57" else "accounts"
        self.state = {"table_oid": "10", "functions": [], "triggers": [], "comments": [], "dependencies": []}
        self.calls, self.drops = [], 0
        self.fail_stage = None
        self.cleanup_fails = False

    # 由真正 builder 送出的 DDL bytes 產生回應，讓本文／屬性測試能敏感抓到變動。
    def created(self, statement):
        body = re.search(r"AS \$\$(.*?)\$\$", statement, re.S).group(1)
        # PostgreSQL JSON 將 OID 輸出為字串；pronargs／subid 等整數欄位仍保留原型態。
        function = {"row": {"oid": "100", "proname": self.function, "pronargs": 0, "prokind": "f", "proowner": "20",
                            "prosrc": body, "proacl": None, "prosecdef": "SECURITY DEFINER" in statement,
                            "proconfig": ["search_path=pg_catalog, public"] if "SET search_path=pg_catalog,public" in statement else None},
                    "namespace": "public", "owner": "articles_owner", "definition": statement.split("; WITH f AS")[0]}
        result = {"table_oid": "10", "functions": [function], "triggers": [], "comments": [],
                  "dependencies": [{"classid": "1255", "objid": "100", "objsubid": 0, "refclassid": "2615", "refobjid": "2200", "refobjsubid": 0, "deptype": "n"}]}
        if "CREATE TRIGGER" in statement:
            result["triggers"] = [{"row": {"oid": "200", "tgname": self.trigger, "tgrelid": "10", "tgfoid": "100"},
                                   "definition": f"CREATE TRIGGER {self.trigger} BEFORE INSERT ON public.{self.table} FOR EACH ROW EXECUTE FUNCTION {self.function}()"}]
            result["dependencies"].append({"classid": "2620", "objid": "200", "objsubid": 0, "refclassid": "1255", "refobjid": "100", "refobjsubid": 0, "deptype": "n"})
        comment = re.search(r"COMMENT ON FUNCTION.*? IS '([^']*)'", statement)
        if comment:
            result["comments"] = [{"classoid": "1255", "objoid": "100", "objsubid": 0, "description": comment.group(1)}]
        return result

    # 只接受 actual lifecycle 的明確交易與固定 catalog；錯誤不替原函式猜 ownership。
    def execute(self, statement):
        self.calls.append(statement)
        if statement.startswith("WITH f AS"):
            return json.dumps(self.state)
        if statement.startswith("BEGIN; CREATE FUNCTION"):
            if self.fail_stage in (1, 2):
                raise driver.DDLFailure("42501")
            self.state = self.created(statement)
            if self.fail_stage == "unknown-commit":
                raise subprocess.TimeoutExpired("fixed psql", 60)
            if self.fail_stage == "json":
                return "not-json"
            if self.fail_stage == "metadata":
                broken = copy.deepcopy(self.state)
                broken.pop("comments")
                return json.dumps(broken)
            return json.dumps(self.state)
        if statement.startswith("BEGIN; COMMENT ON FUNCTION"):
            value = re.search(r" IS '([^']*)'", statement).group(1)
            self.state["comments"] = [{"classoid": "1255", "objoid": "100", "objsubid": 0, "description": value}]
            return json.dumps(self.state)
        if statement.startswith("BEGIN; DROP "):
            self.drops += 1
            if self.cleanup_fails:
                raise driver.DDLFailure("2BP01")
            self.state = {"table_oid": "10", "functions": [], "triggers": [], "comments": [], "dependencies": []}
            return ""
        raise AssertionError("unexpected fixed DDL test input")


# DDLServer 僅包裝新的固定 psql I/O 邊界；guard 與 executor 本身不被替換。
class DDLServer:
    # 兩份獨立 catalog 防止清錯另一票，同時保留實際命令供有限目標核對。
    def __init__(self):
        self.databases = {ticket: DDLDatabase(ticket) for ticket in ("57", "39")}
        self.scripts = []

    # 實際 ddl_process 的 argv 必指固定 cq32；不允許工具 fake 吞掉任意新增命令。
    def run(self, args, *, input, cwd, capture_output, text, timeout):
        self.check_command(args)
        if input is not None:
            # 核真 stdin 的故障順序；沒有 SET ROLE／rollback 的錯腳本不可被固定回應假綠。
            role, failing, marker, rollback = (input.index(token) for token in (
                "SET LOCAL ROLE articles_app;", "\\set ON_ERROR_STOP off", "\\echo CQ32_SQLSTATE :SQLSTATE", "ROLLBACK;"))
            function = input.index("CREATE FUNCTION")
            if "CREATE TRIGGER" in input:
                target = input.index("CREATE TRIGGER")
                valid = function < role < failing < target < marker < rollback
            else:
                valid = role < failing < function < marker < rollback
            if not valid or not input.startswith("BEGIN;\n") or not input.endswith("SELECT current_user;\n"):
                raise AssertionError("unexpected CREATE denial script")
            self.scripts.append(input)
            return subprocess.CompletedProcess(args, 0, "CQ32_SQLSTATE 42501\narticles_owner\n", "ERROR: 42501: synthetic CREATE permission denied\n")
        statement = args[-1]
        if statement.startswith("BEGIN READ ONLY;"):
            output = "articles_app\narticles_owner"
        elif statement.startswith("SELECT json_build_object('create'"):
            output = json.dumps({"create": False, "accounts": False, "failures": False})
        elif statement.startswith("SELECT row_to_json(a)"):
            output = json.dumps({"id": 7, "email": "cq32-owned@example.test", "role": "member", "failure_epoch": 13})
        elif statement.startswith("SELECT id FROM public.login_failures"):
            output = ""
        else:
            database = self.databases["57" if "ticket57_http_boundary_shift" in statement else "39"]
            output = database.execute(statement)
        return subprocess.CompletedProcess(args, 0, output, "")

    # 比對真 executor 傳出的固定連線與權限旗標；這不是以 source 命名代替行為。
    def check_command(self, args):
        expected = ["docker", "compose", "-p", "cq32_test", "-f", "compose.yaml", "-f", ".local/cq32-compose.yaml", "--profile", "*", "exec", "-T", "db", "psql"]
        if args[:len(expected)] != expected or "articles_owner" not in args or "ON_ERROR_STOP=1" not in args:
            raise AssertionError("unexpected psql target")

    # 前後快照包含兩個 catalog，讓未回收物件不能被固定假 data digest 掩蓋。
    def snapshot(self):
        return {"data": "stable", "catalogs": {ticket: copy.deepcopy(database.state) for ticket, database in self.databases.items()}}


# FreshIO 只供三個 fresh 入口的固定工具結果；原 guard、SQL／HTTP caller 都照常執行。
class FreshIO:
    # 合成固定 Ticket 57 metadata／catalog，不讀 keys，也不建立真正服務。
    def __init__(self, kind):
        self.database = DDLDatabase("57")
        if kind != "absent":
            self.database.state = self.database.created("BEGIN; " + ddl57.boundary_create_sql(7, 13, 41, 1)[0])
            if kind == "overload":
                self.database.state["functions"][0]["row"]["pronargs"] = 1
            elif kind == "namespace":
                self.database.state["functions"][0]["namespace"] = "unknown"
            elif kind == "other-table":
                self.database.state["functions"] = []
                self.database.state["triggers"] = [{"row": {"oid": "200", "tgname": self.database.trigger,
                                                            "tgrelid": "999", "tgfoid": "999"}}]
        self.queries, self.health, self.http, self.posts = [], [], [], []
        self.settings = {"enabled": False, "window_minutes": 9, "failure_threshold": 17, "lock_seconds": 720}

    # 只接受實際 guard／baseline 的既有 Docker 命令；未知 SQL 立即停，不假裝執行成功。
    def output(self, args, **kwargs):
        project = ddl57.PROJECT
        if args[:2] == ["docker", "inspect"]:
            service = args[-1]
            mounts = [{"Name": project + "_postgres_data", "Destination": "/var/lib/postgresql"}]
            bindings = {}
            if service != "db":
                mounts = [{"Source": os.path.realpath(f".local/keys/{service}.key"),
                           "Destination": f"/run/secrets/{service}-jwt.key", "RW": False}]
                bindings["8080/tcp" if service == "aspnet-core" else "8000/tcp"] = [
                    {"HostIp": "127.0.0.1", "HostPort": str(ddl57.PORTS[service])}]
            return json.dumps([{"Config": {"Labels": {"com.docker.compose.project": project,
                                                      "com.docker.compose.service": service},
                                           "Env": ["DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"]},
                                "State": {"Running": True, "Health": {"Status": "healthy"}},
                                "NetworkSettings": {"Networks": {project + "_default": {}}},
                                "Mounts": mounts, "HostConfig": {"PortBindings": bindings}}])
        if args[:4] != ["docker", "compose", "--profile", "*"]:
            raise AssertionError("unexpected fresh Docker command")
        if args[4:] == ["config", "--format", "json"]:
            return json.dumps({"name": project, "volumes": {"postgres_data": {"name": project + "_postgres_data"}},
                               "networks": {"default": {"ipam": {"config": [{"subnet": "10.251.57.0/24"}]}}}})
        if args[4:6] == ["ps", "-q"] and args[-1] in ("db", *ddl57.BACKENDS):
            return args[-1]
        if args[4:8] != ["exec", "-T", "db", "psql"] or args[-2] != "-c":
            raise AssertionError("unexpected fresh psql command")
        query = args[-1]
        self.queries.append(query)
        if query == "SELECT current_database()||':'||current_user":
            return "articles:articles_owner"
        if query == ddl57.boundary_catalog_sql():
            return self.database.execute(query)
        if query.startswith("SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger"):
            # 精確保留舊窄檢查：public 零參數／原 table trigger 會拒絕，其他同名不會。
            state = self.database.state
            exists = any(item["namespace"] == "public" and item["row"]["pronargs"] == 0 for item in state["functions"])
            exists = exists or any(item["row"]["tgrelid"] == "10" for item in state["triggers"])
            return "f" if exists else "t"
        if query.startswith("SELECT count(*) FROM pg_stat_activity") or query.startswith("SELECT count(*) FROM accounts WHERE email LIKE"):
            return "0"
        if query.startswith("SELECT json_build_object('enabled',enabled"):
            return json.dumps(self.settings)
        if query.startswith("SELECT coalesce(json_agg(m ORDER BY id)"):
            return "[]"
        raise AssertionError("unexpected fresh SQL; no write accepted")

    # 原 guard 仍逐一核對十二個固定 /up bytes；這裡不提供業務回應。
    def up(self, url, **kwargs):
        backend = next(name for name, base in ddl57.BASES.items() if url == base + "/up")
        self.health.append(backend)
        return Response(b'{"status":"ok"}' if backend in ("symfony", "aspnet-core") else b"OK")

    # CQ11 的兩筆入口錯誤保持原 envelope／CORS 檢查；第一個真 POST 在傳輸邊界截停。
    def business(self, request, **kwargs):
        method, url = request.get_method(), request.full_url
        self.http.append((method, url))
        if method == "POST":
            self.posts.append((url, json.loads(request.data)))
            raise BoundaryDone("fresh first POST")
        status, code = (404, "E0004") if method == "GET" and "/api/cq09-missing-" in url else (405, "E0005")
        if status == 405 and not (method == "DELETE" and url.endswith("/api/auth/login")):
            raise AssertionError("unexpected fresh HTTP request")
        payload = {"success": False, "message": ddl57.MESSAGES[code], "error_code": code,
                   "log_id": f"00000000-0000-4000-8000-{len(self.http):012d}"}
        response = Response(json.dumps(payload).encode(), status)
        response.headers = Message()
        response.headers["Content-Type"] = "application/json"
        response.headers["Access-Control-Allow-Origin"] = cq09.ORIGIN
        return response

    # 僅替換外部工具與 socket I/O；不替換 guard、SAFE、executor、業務函式或 assertions。
    @contextlib.contextmanager
    def active(self):
        with patch.object(subprocess, "check_output", self.output), patch.object(ddl57, "urlopen", self.up), \
                patch.object(cq09, "urlopen", self.business), patch.object(transport, "urlopen", self.business), \
                patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": ddl57.PROJECT, "COMPOSE_FILE": ddl57.COMPOSE}):
            yield


# SettingsConnection 保留 native execute／cursor 邊界，僅提供固定 probe 的單值回應。
class SettingsConnection(Connection):
    # 真 worker 自行執行 statement；測試只替換 psycopg socket 回應，不改 executor／context。
    def execute(self, statement):
        self.result = self.query(statement)
        self.description = None if statement.startswith("UPDATE ") else ("one-column",)
        return self

    # native cursor 仍是單 row／單 column，讓 worker 保留自己的形狀斷言。
    def fetchall(self):
        return [(self.result,)]


# SettingsIO 只描述固定四欄／own member，不是通用 SQL／HTTP 模擬平台或真 DB 驗收。
class SettingsIO:
    # 原值刻意非預設；錯誤發生在提交 numeric 寫入後或還原時，才能驗真 finally。
    def __init__(self, fault=None):
        self.original = {"enabled": True, "window_minutes": 11, "failure_threshold": 19, "lock_seconds": 777}
        self.state, self.member = self.original.copy(), {"id": 7, "role": "member", "failure_epoch": 13, "locked_until": None}
        self.fault, self.queries, self.requests = fault, [], []
        self.trace_id = "00000000-0000-4000-8000-000000000001"
        self.error_overrides, self.error_status = {}, 500

    # 固定 DB／owner 驗證留在 test I/O 邊界，不把假的連線當成 live 權限證據。
    def connect(self, **kwargs):
        if {key: kwargs[key] for key in ("host", "port", "dbname", "user")} != {
                "host": "db", "port": 5432, "dbname": "articles", "user": "articles_owner"}:
            raise AssertionError("native child target escaped")
        return SettingsConnection(self.query)

    # 只接受原 contexts／固定 worker 真的送出的 query；還原 SQL 必由那些 contexts 產生。
    def query(self, statement):
        self.queries.append(statement)
        if statement == "SELECT current_database()||':'||current_user":
            return "articles:articles_owner"
        if statement.startswith("SELECT enabled||"):
            return f"{str(self.state['enabled']).lower()},{self.state['window_minutes']},{self.state['failure_threshold']},{self.state['lock_seconds']}"
        if statement.startswith("UPDATE login_lockout_settings SET "):
            matched = re.fullmatch(r"UPDATE login_lockout_settings SET enabled=(true|false),window_minutes=(\d+),failure_threshold=(\d+),lock_seconds=(\d+) WHERE id=1", statement)
            if matched is None:
                raise AssertionError("unexpected fixed settings UPDATE")
            enabled, window, threshold, seconds = matched.groups()
            self.state = {"enabled": enabled == "true", "window_minutes": int(window), "failure_threshold": int(threshold), "lock_seconds": int(seconds)}
            return None
        if statement.startswith("UPDATE accounts SET failure_epoch="):
            if not statement.endswith("WHERE id=7"):
                raise AssertionError("epoch target escaped")
            self.member["failure_epoch"] = int(statement.split("failure_epoch=")[1].split()[0])
            return None
        if statement.startswith("SELECT failure_epoch FROM"):
            return self.member["failure_epoch"]
        if statement.startswith("SELECT json_build_object('enabled'"):
            return self.state.copy()
        if statement.startswith("SELECT json_build_object('id'"):
            return self.member.copy()
        if statement.startswith("SELECT coalesce(jsonb_agg"):
            return [self.member.copy()] if "public.accounts t" in statement else []
        raise AssertionError("unexpected native settings query")

    # 原 urllib Request 與嚴格 envelope 都照常執行；假回應只證分支／清理，不證產品 HTTP。
    def opened(self, request, **kwargs):
        backend = request.full_url.split("//", 1)[1].split(":", 1)[0]
        if backend not in ("flask", "fastapi", "echo") or not request.full_url.endswith("/api/settings/login-lockout"):
            raise AssertionError("native HTTP target escaped")
        body = json.loads(request.data) if request.data is not None else None
        self.requests.append((backend, request.get_method(), body))
        if body is None:
            payload, status = {"success": True, "message": "OK", "data": self.state.copy()}, 200
        elif self.state["enabled"] and not body["enabled"]:
            if backend != "echo" or self.member["failure_epoch"] != 9223372036854775807:
                raise AssertionError("numeric-only HTTP unexpectedly clears global state")
            payload, status = {"success": False, "message": "系統暫時無法處理，請稍後再試", "error_code": "E9999",
                               "log_id": self.trace_id} | self.error_overrides, self.error_status
        else:
            if self.fault == "both" and body["window_minutes"] == 9 and self.state["window_minutes"] == 6:
                raise OSError("bounded native restore failure")
            self.state = body.copy()
            if self.fault and body["window_minutes"] == 6:
                raise RuntimeError("bounded committed numeric failure")
            payload, status = {"success": True, "message": "OK", "data": {}}, 200
        return Response(json.dumps(payload).encode(), status)


# Contracts 對實際函式施加單一錯誤，確認不會把未 guard／改期限／殘留 packet 當成功。
class Contracts(unittest.TestCase):
    # log_id 契約是非空 string，非 v4 也須保留供 Main 查日誌；其餘500 envelope 仍逐項拒絕。
    def test_settings_trace_identifier_contract(self):
        import psycopg
        cases = [("non-v4", "01234567-89ab-cdef-0123-456789abcdef", {}, 500, True),
                 ("no-shape-policy", "trace-any-shape", {}, 500, True)]
        cases.extend(("invalid-id", value, {}, 500, False) for value in ("", None, 7, False, {}, []))
        cases.extend(("invalid-envelope", "trace", override, status, False) for override, status in (
            ({"error_code": "E0005"}, 500), ({"message": "wrong"}, 500), ({"success": True}, 500),
            ({"errors": {}}, 500), ({}, 400)))
        for label, trace, overrides, status, accepted in cases:
            with self.subTest(label=label, trace=repr(trace), overrides=overrides, status=status):
                model = SettingsIO()
                model.trace_id, model.error_overrides, model.error_status = trace, overrides, status
                before = model.member.copy()
                payload = {"project": driver.PROJECT, "check_only": False, "source_hashes": {},
                           "original": model.original.copy(), "member_id": 7, "member_before": before,
                           "owner_password": "a" * 64, "tokens": {name: "synthetic" for name in ("flask", "fastapi", "echo")}}
                # 真 worker／context／envelope 判斷不換；只替換 PG／urllib I/O，無真網路或資料寫入。
                with patch.object(psycopg, "connect", model.connect), patch.object(driver, "build_opener", return_value=Mock(open=model.opened)), \
                        patch.dict(os.environ, {"DB_HOST": "db", "DB_DATABASE": "articles", "DB_USERNAME": "articles_app"}):
                    observed = driver.settings_native_worker(payload)
                self.assertEqual(model.state, model.original)
                self.assertEqual(model.member, before)
                self.assertEqual(observed["status"], "complete" if accepted else "pending")
                self.assertEqual(sum(case.get("status") == "pass" for case in observed["cases"]), 25 if accepted else 24)
                if accepted:
                    self.assertEqual(observed["cases"][-1]["log_id"], trace)
                else:
                    self.assertEqual(observed["failure"][0]["type"], "AssertionError")

    # 真 host guard／api／session／O_EXCL 配 child 工具 I/O；不把假的 child 完成回應當真25案例證據。
    def test_settings_host_packet_and_gate(self):
        for fault in (None, "runtime", "phase", "timeout"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                fixture = GuardFixture(folder)
                for name in driver.SETTINGS_MODULES:
                    relative = "ops/tests/" + name
                    fixture.source_names.append(relative)
                    for root in (fixture.repo, fixture.root):
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes((Path(__file__).parent / name).read_bytes())
                (fixture.root / ".env").write_text("DB_OWNER_PASSWORD=" + "a" * 64 + "\n")
                (fixture.root / ".env").chmod(0o600)
                credentials = fixture.root / ".local/cq32-admin.json"
                credentials.write_text(json.dumps({"email": "own-admin@example.test", "nickname": "own-admin", "password": "Cq321234"}))
                credentials.chmod(0o600)
                packet_path = fixture.root / ".local/cq32-settings-probe.json"
                original = {"enabled": True, "window_minutes": 11, "failure_threshold": 19, "lock_seconds": 777}
                member = {"id": 8, "role": "member", "failure_epoch": 0, "locked_until": None}
                sessions, calls, children = {}, [], []

                # 只替換真 psql 工具回應；原 driver.sql 與 session claims／epoch 比對仍執行。
                def command(*args):
                    query = args[-1]
                    if query.startswith("SELECT json_build_object('enabled'"):
                        return json.dumps(original)
                    if query.startswith("SELECT json_build_object('id'"):
                        return json.dumps(member)
                    if query.startswith("SELECT row_to_json(s)"):
                        return json.dumps(sessions[query.split("jti='")[1].split("'")[0]])
                    raise AssertionError("unexpected host settings read")

                # 每次非冪等請求都必須已有同一0600 pending packet；不碰原 baseline／DDL packet。
                def opened(request, **kwargs):
                    persisted = json.loads(packet_path.read_text())
                    self.assertEqual(persisted["status"], "pending")
                    self.assertEqual(packet_path.stat().st_mode & 0o777, 0o600)
                    calls.append(request.full_url)
                    body = json.loads(request.data)
                    if request.full_url.endswith("/api/auth/register"):
                        self.assertEqual(persisted["operation"], "echo:member-register")
                        data = {}
                    else:
                        backend = next(name for name, base in driver.BASES.items() if request.full_url.startswith(base + "/"))
                        login, saved = login_fixture(backend)
                        identifier = 7 if body["email"] == "own-admin@example.test" else 8
                        jti = base64.urlsafe_b64encode(len(calls).to_bytes(16, "big")).decode().rstrip("=")
                        claims = {"jti": jti, "iat": 0, "exp": 28800}
                        login["access_token"] = "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"
                        login["user"] = {"id": identifier, "email": body["email"], "role": "admin" if identifier == 7 else "member"}
                        saved.update(jti=jti, account_id=identifier)
                        sessions[jti] = saved
                        data = login
                    return Response(json.dumps({"success": True, "message": "OK", "data": data}).encode(), 201 if request.full_url.endswith("register") else 200)

                # 固定 fastapi／-B／完整 bootstrap 不可被參數改寫；secret 只在 stdin。
                def child(args, **kwargs):
                    self.assertEqual(args, ["docker", "compose", "-p", driver.PROJECT, "-f", driver.FILES[0], "-f", driver.FILES[1], "--profile", "*",
                                            "exec", "-T", "fastapi", "python", "-B", "-c", driver.SETTINGS_BOOTSTRAP])
                    payload = json.loads(kwargs["input"])
                    self.assertEqual(set(payload["sources"]), set(driver.SETTINGS_MODULES))
                    self.assertEqual(kwargs["cwd"], fixture.root)
                    self.assertNotIn("a" * 64, " ".join(args))
                    children.append(payload["check_only"])
                    if payload["check_only"]:
                        self.assertEqual(calls, [])
                        result = {"status": "pending" if fault == "runtime" else "runtime-only", "source_hashes": payload["source_hashes"], "database": "articles:articles_owner"}
                    else:
                        self.assertEqual(json.loads(packet_path.read_text())["member_before"], member)
                        self.assertEqual(payload["owner_password"], "a" * 64)
                        self.assertEqual(set(payload["tokens"]), {"flask", "fastapi", "echo"})
                        if fault == "timeout":
                            raise subprocess.TimeoutExpired("fixed native child", 360, output=b"partial", stderr=b"bounded timeout")
                        result = {"status": "pending" if fault == "phase" else "complete", "source_hashes": payload["source_hashes"],
                                  "cases": [{"status": "pass"} for _ in range(25)]}
                    return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

                with fixture.active(), patch.object(driver, "SETTINGS_PACKET", packet_path):
                    identity = driver.guard()
                    with patch.object(driver, "command", command), patch.object(driver.OPENER, "open", opened), patch.object(subprocess, "run", child):
                        if fault:
                            with self.assertRaises(subprocess.TimeoutExpired if fault == "timeout" else AssertionError):
                                driver.settings_probe(identity)
                        else:
                            driver.settings_probe(identity)
                        saved = json.loads(packet_path.read_text())
                        self.assertEqual(saved["status"], "pending" if fault else "complete")
                        self.assertEqual(len(calls), 0 if fault == "runtime" else 5)
                        self.assertEqual(children, [True] if fault == "runtime" else [True, False])
                        if fault == "timeout":
                            self.assertEqual(saved["child_stdout"], "partial")
                            self.assertEqual(saved["child_stderr"], "bounded timeout")
                        before = packet_path.read_bytes()
                        with self.assertRaises(FileExistsError):
                            driver.settings_probe(identity)
                        self.assertEqual(packet_path.read_bytes(), before)

    # 完整 bootstrap 真 import 六原 module；network-none container 內只跑 runtime-only，絕不接 DB。
    def test_settings_native_bootstrap(self):
        sources = {name: (Path(__file__).parent / name).read_text() for name in driver.SETTINGS_MODULES}
        hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in sources.items()}
        compile(driver.SETTINGS_BOOTSTRAP, "fixed-settings-bootstrap", "exec")
        payload = {"project": driver.PROJECT, "check_only": True, "sources": sources, "source_hashes": hashes}
        result = subprocess.run([sys.executable, "-B", "-c", driver.SETTINGS_BOOTSTRAP], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed, {"status": "runtime-only", "runtime": "3.13.16", "psycopg": "3.3.6", "source_hashes": hashes})
        # 一個完整模組雜湊不符必在 import／phase 前拒絕，不將已存在來源切割或補 stub。
        payload["source_hashes"]["ticket21_http.py"] = "wrong"
        result = subprocess.run([sys.executable, "-B", "-c", driver.SETTINGS_BOOTSTRAP], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "pending")

    # 真 native worker／25 contexts 配工具 I/O；normal 與提交後錯／雙失敗都須如實回 pending／恢復。
    def test_settings_native_context_phase(self):
        import psycopg
        for fault in (None, "write", "both"):
            with self.subTest(fault=fault):
                model = SettingsIO(fault)
                before = model.member.copy()
                payload = {"project": driver.PROJECT, "check_only": False, "source_hashes": {},
                           "original": model.original.copy(), "member_id": 7, "member_before": before,
                           "owner_password": "a" * 64, "tokens": {name: "synthetic" for name in ("flask", "fastapi", "echo")}}
                with patch.object(psycopg, "connect", model.connect), patch.object(driver, "build_opener", return_value=Mock(open=model.opened)), \
                        patch.dict(os.environ, {"DB_HOST": "db", "DB_DATABASE": "articles", "DB_USERNAME": "articles_app"}):
                    observed = driver.settings_native_worker(payload)
                self.assertEqual(model.state, model.original)
                self.assertEqual(model.member, before)
                if fault:
                    self.assertEqual(observed["status"], "pending")
                    self.assertEqual(observed["failure"][0]["type"], "OSError" if fault == "both" else "RuntimeError")
                    if fault == "both":
                        self.assertEqual(observed["failure"][1]["type"], "RuntimeError")
                else:
                    self.assertEqual(observed["status"], "complete")
                    self.assertEqual(len(observed["cases"]), 25)
                    self.assertEqual(len({case["case"] for case in observed["cases"]}), 25)
                    self.assertTrue(all(case["status"] == "pass" for case in observed["cases"]))
                    self.assertEqual(observed["before"], observed["after"])
                    self.assertTrue(any("failure_epoch=9223372036854775807" in query for query in model.queries))
                    self.assertTrue(any("failure_epoch=13 WHERE id=7" in query for query in model.queries))

    # 實際 settings_cases 要走到 holder／兩請求及 finally；API stub 只證依賴接線，不證 HTTP 契約。
    def test_administration_regress_binding(self):
        for fault in (None, "request", "holder"):
            with self.subTest(fault=fault):
                original = {"enabled": False, "window_minutes": 9, "failure_threshold": 17, "lock_seconds": 720}
                state, epoch, locked, counts = [original.copy()], [13], ["until"], {}
                holder = Mock()
                holder.poll.return_value = None
                regress = Mock()
                regress.locked_sql.return_value = holder

                # 本輪 holder 完成才標 terminal；wait 失敗也須由原 finally 終止並收回輸出。
                def finish(process):
                    self.assertIs(process, holder)
                    holder.poll.return_value = 0

                regress.finish_lock.side_effect = finish
                if fault == "holder":
                    regress.wait_lock.side_effect = RuntimeError("bounded holder failure")

                # 固定三個 fixture actor 即可走完整設定段；不替換被測 check、context 或清理。
                def fixture(api, tag, nickname):
                    label = tag.rsplit("-", 1)[-1]
                    return {"id": {"lock": 7, "race": 8, "transition": 9}[label], "email": label + "@example.test"}

                # 只供 controlflow 所需四欄／計數，invalid HTTP assertions 另由既有原 suite 負責。
                def call(base, method, path, body=None, token=None):
                    if method == "GET":
                        return 200, {"data": state[0].copy()}, {}
                    if path == "/api/auth/login":
                        if "email" in body and body["email"] == "admin@example.test":
                            return 200, {"data": {"access_token": "laravel-admin"}}, {}
                        email = body["email"]
                        counts[email] = counts.get(email, 0) + (1 if state[0]["enabled"] else 0)
                        status = 429 if state[0]["enabled"] and counts[email] >= 2 else 401
                        if fault == "request" and email.startswith("transition"):
                            raise ValueError("bounded request failure")
                        return status, {"data": {}}, {"Retry-After": "120"}
                    valid = isinstance(body, dict) and set(body) == set(original) and type(body["enabled"]) is bool and all(
                        type(body[key]) is int and 1 <= body[key] <= maximum
                        for key, maximum in (("window_minutes", 60), ("failure_threshold", 100), ("lock_seconds", 86400)))
                    if valid:
                        if state[0]["enabled"] and not body["enabled"]:
                            if epoch[0] == 9223372036854775807:
                                return 500, {"data": {}}, {}
                            epoch[0], locked[0] = epoch[0] + 1, ""
                        state[0] = body.copy()
                    return (200 if valid else 422), {"data": {}}, {}

                # 原 epoch finally 的精確 UPDATE／讀回仍執行；其他依賴回傳已知 fixture 值。
                def sql(query):
                    if query.startswith("UPDATE accounts SET failure_epoch="):
                        epoch[0] = int(query.split("failure_epoch=")[1].split()[0])
                        return ""
                    if "locked_until IS NULL" in query:
                        return "t" if locked[0] == "" else "f"
                    if "locked_until FROM" in query:
                        return locked[0]
                    if "failure_epoch=(SELECT" in query:
                        return "0"
                    if query.startswith("SELECT failure_epoch"):
                        return str(epoch[0])
                    if "count(*) FROM login_failures" in query:
                        return "2"
                    raise AssertionError("unexpected bounded settings query")

                # stub 只解包資料，明示不把這段 normal-controlflow 測試當成後端錯誤文案 PASS。
                def expect(result, *args, **kwargs):
                    return result[1]["data"]

                api = Mock(call=call, expect=expect)
                with patch.object(administration, "fixture", fixture), patch.object(administration, "sql", sql), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        administration.settings_cases(api, {"token": "admin", "email": "admin@example.test"}, {"token": "member"}, "bounded", regress=regress)
                    except (RuntimeError, ValueError):
                        self.assertIsNotNone(fault)
                    else:
                        self.assertIsNone(fault)
                regress.locked_sql.assert_called_once_with("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE", "SELECT 1", seconds=8)
                regress.wait_lock.assert_called_once_with(minimum=2)
                if fault == "holder":
                    holder.terminate.assert_called_once()
                    holder.communicate.assert_called_once()
                else:
                    regress.finish_lock.assert_called_once_with(holder)
                    self.assertEqual(holder.poll(), 0)
                self.assertEqual(state[0], original)

    # 原生 OID 字串只做嚴格驗證、不正規化 receipt；型態正確也不代表可認領未知物件。
    def test_ddl_native_oid_receipt(self):
        for spec in driver.ddl_specs(7, 13, 41):
            database = DDLDatabase(spec["ticket"])
            database.state = database.created("BEGIN; " + "; ".join(spec["statements"]) + ";")
            # 正整數 user ID／epoch 留在 DDL；catalog OID 依實際 PostgreSQL JSON 保留字串。
            for value in ("1", "16444", "4294967295"):
                with self.subTest(ticket=spec["ticket"], accepted=value):
                    catalog = copy.deepcopy(database.state)
                    catalog["table_oid"] = catalog["triggers"][0]["row"]["tgrelid"] = value
                    before = copy.deepcopy(catalog)
                    self.assertIs(spec["receipt"](catalog), catalog)
                    self.assertEqual(catalog, before)
            # 限 ASCII canonical uint32，不把 bool／int、前導零或越界數值轉成可接受的 OID。
            for value in (None, 1, True, False, 0, 1.0, [], {}, b"10", "", "0", "00", "01", " 1", "1 ",
                          "+1", "-1", "1.0", "1e1", "١", "4294967296", "99999999999"):
                with self.subTest(ticket=spec["ticket"], rejected=repr(value)):
                    catalog = copy.deepcopy(database.state)
                    catalog["table_oid"] = catalog["triggers"][0]["row"]["tgrelid"] = value
                    with self.assertRaisesRegex(AssertionError, "receipt 不完整"):
                        spec["receipt"](catalog)
            # 沒有本輪 receipt 時仍只准 absence；不得把有效 catalog 當成先前失敗 phase 的 ownership。
            before = copy.deepcopy(database.state)
            with self.assertRaisesRegex(AssertionError, "DDL 已存在"):
                spec["clear"](execute=database.execute)
            self.assertEqual(database.state, before)
            self.assertEqual(database.drops, 0)
            self.assertFalse(any(query.startswith("BEGIN;") for query in database.calls))

    # 三個 fresh 入口必在首筆 POST／baseline 前保留未知同名物件；absence 才可繼續原流程。
    def test_fresh_missing_preflight(self):
        for entry in ("cq10", "cq11", "capture"):
            for kind in ("absent", "public", "overload", "namespace", "other-table"):
                with self.subTest(entry=entry, kind=kind), tempfile.TemporaryDirectory() as folder, entered(folder):
                    fixture = FreshIO(kind)
                    before = copy.deepcopy(fixture.database.state)
                    recovery = cq18.RECOVERIES["cq18"]
                    # 原 main 自己執行 guard；capture 的 caller 先執行相同 guard 再交合法 marker。
                    with fixture.active(), patch.object(sys, "argv", [entry + "_http.py"]), contextlib.redirect_stdout(io.StringIO()):
                        if entry == "capture":
                            ddl57.guard()
                            if kind == "absent":
                                packet, path = cq18.capture_baseline("cq180123456789ab")
                                self.assertEqual(json.loads(path.read_text()), packet)
                                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                                self.assertEqual(packet["settings"], fixture.settings)
                                self.assertFalse(packet["restored"])
                            else:
                                with self.assertRaisesRegex(AssertionError, "DDL 已存在"):
                                    cq18.capture_baseline("cq180123456789ab")
                        else:
                            main = cq10.main if entry == "cq10" else cq11.main
                            if kind == "absent":
                                with self.assertRaisesRegex(BoundaryDone, "fresh first POST"):
                                    main()
                            else:
                                with self.assertRaisesRegex(AssertionError, "DDL 已存在"):
                                    main()
                    # 每案真的跑完原 guard；catalog 讀取與沒有 SQL 寫入分開核對，不能只憑呼叫名稱。
                    self.assertEqual(fixture.health, list(ddl57.BACKENDS))
                    self.assertEqual(fixture.queries[0], "SELECT current_database()||':'||current_user")
                    self.assertIn(ddl57.boundary_catalog_sql(), fixture.queries)
                    self.assertTrue(all(query.startswith(("SELECT ", "WITH f AS")) for query in fixture.queries))
                    self.assertEqual(fixture.database.state, before)
                    self.assertEqual(fixture.database.drops, 0)
                    if kind != "absent":
                        self.assertEqual(fixture.http, [])
                        self.assertFalse(recovery.exists())
                    elif entry == "capture":
                        self.assertEqual(fixture.http, [])
                        self.assertTrue(any("idle in transaction" in query for query in fixture.queries))
                    else:
                        self.assertEqual(len(fixture.posts), 1)
                        self.assertTrue(fixture.posts[0][0].endswith("/api/auth/register"))
                        self.assertEqual(set(fixture.posts[0][1]), {"email", "password", "nickname"})

    # 原 default strict_sql caller 在 function-only 時先拒絕；不替換 guard/executor 或送出 HTTP。
    def test_ddl_default39_function_only_before_case(self):
        database = DDLDatabase("39")
        database.state = database.created("BEGIN; CREATE FUNCTION public.ticket39_fail_epoch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;")
        queries = []

        # 只替換 psql 工具 I/O；舊實作會 CREATE 失敗後 DROP 已存在的未知 function。
        def output(args, **kwargs):
            query = args[-1]
            queries.append(query)
            if query.startswith("WITH f AS"):
                return database.execute(query)
            if query.startswith("SELECT count(*)"):
                return "0"
            if query.startswith("SELECT row_to_json"):
                return "{}"
            if query.startswith("CREATE FUNCTION"):
                raise subprocess.CalledProcessError(1, "fixed psql")
            if query.startswith("DROP FUNCTION"):
                database.state["functions"] = []
                return ""
            if query.startswith("DROP TRIGGER"):
                return ""
            raise AssertionError("unexpected default psql input")

        failure = None
        with patch.object(ddl39.subprocess, "check_output", output), patch.object(ddl39, "call") as http:
            try:
                ddl39.rollback_probe("not-sent", 7, {})
            except Exception as error:
                failure = error
            http.assert_not_called()
        self.assertTrue(database.state["functions"], "function-only 原物件遭原 caller 清除")
        self.assertIsInstance(failure, AssertionError)
        self.assertIn("DDL 已存在", str(failure))
        self.assertFalse(any(query.startswith(("CREATE", "DROP", "UPDATE", "INSERT")) for query in queries))

    # 原 caller 與副本共用的 actual contextmanager：成功／案例錯／雙失敗均核 owned cleanup。
    def test_ddl_owned_lifecycle(self):
        for spec in driver.ddl_specs(7, 13, 41):
            for fail, cleanup_fails in ((False, False), (True, False), (True, True)):
                with self.subTest(ticket=spec["ticket"], fail=fail, cleanup_fails=cleanup_fails):
                    database = DDLDatabase(spec["ticket"])
                    database.cleanup_fails = cleanup_fails
                    try:
                        with spec["context"](*spec["args"], execute=database.execute) as owned:
                            self.assertTrue(owned["functions"] and owned["triggers"])
                            if fail:
                                raise driver.DDLCaseFailure("synthetic case failure")
                    except driver.DDLFailure as error:
                        self.assertTrue(cleanup_fails)
                        self.assertIsInstance(error.__context__, driver.DDLCaseFailure)
                    except driver.DDLCaseFailure:
                        self.assertTrue(fail and not cleanup_fails)
                    self.assertEqual(database.drops, 1)
                    self.assertEqual(bool(database.state["functions"]), cleanup_fails)

    # CREATE1/2、未知 COMMIT、JSON/metadata 失敗都不能進案例或按名字猜 DROP。
    def test_ddl_create_failure_no_ownership(self):
        for spec in driver.ddl_specs(7, 13, 41):
            for failure in (1, 2, "unknown-commit", "json", "metadata"):
                with self.subTest(ticket=spec["ticket"], failure=failure):
                    database = DDLDatabase(spec["ticket"])
                    database.fail_stage = failure
                    expected = driver.DDLFailure if failure in (1, 2) else subprocess.TimeoutExpired if failure == "unknown-commit" else json.JSONDecodeError if failure == "json" else AssertionError
                    with self.assertRaises(expected):
                        with spec["context"](*spec["args"], execute=database.execute):
                            self.fail("failed CREATE/receipt must not enter")
                    self.assertTrue(any(statement.startswith("BEGIN; CREATE FUNCTION") for statement in database.calls))
                    self.assertEqual(database.drops, 0)
                    self.assertEqual(bool(database.state["functions"]), failure not in (1, 2))

    # 任一同名 function/trigger（含其他 namespace/overload）必先拒絕，catalog 不變。
    def test_ddl_unknown_preserves_identity(self):
        for spec in driver.ddl_specs(7, 13, 41):
            for kind in ("function-only", "pair", "namespace", "overload", "trigger-only"):
                with self.subTest(ticket=spec["ticket"], kind=kind):
                    database = DDLDatabase(spec["ticket"])
                    database.state = database.created("BEGIN; " + "; ".join(spec["statements"]) + ";")
                    if kind != "pair":
                        database.state["triggers"] = []
                    if kind == "namespace":
                        database.state["functions"][0]["namespace"] = "unknown"
                    elif kind == "overload":
                        database.state["functions"][0]["row"]["pronargs"] = 1
                    elif kind == "trigger-only":
                        database.state["functions"] = []
                        database.state["triggers"] = [{"row": {"oid": "200", "tgname": database.trigger, "tgrelid": "999", "tgfoid": "999"}}]
                    before = copy.deepcopy(database.state)
                    with self.assertRaisesRegex(AssertionError, "DDL 已存在"):
                        with spec["context"](*spec["args"], execute=database.execute):
                            self.fail("unknown objects must not enter")
                    self.assertEqual(database.state, before)
                    self.assertEqual(database.drops, 0)
                    self.assertFalse(any(statement.startswith("BEGIN;") for statement in database.calls))

    # stale COMMENT/owner/ACL/dependency 不可沿用舊 receipt；只有本輪明示更新後 receipt 可清。
    def test_ddl_stale_receipt_and_builder_cleanup(self):
        for spec in driver.ddl_specs(7, 13, 41):
            for kind in ("comments", "owner", "acl", "dependencies"):
                with self.subTest(ticket=spec["ticket"], kind=kind):
                    database = DDLDatabase(spec["ticket"])
                    database.state = database.created("BEGIN; " + "; ".join(spec["statements"]) + ";")
                    stale = copy.deepcopy(database.state)
                    if kind == "comments":
                        database.state["comments"] = [{"classoid": "1255", "objoid": "100", "objsubid": 0, "description": "cq32:changed"}]
                    elif kind == "owner":
                        database.state["functions"][0]["owner"] = "unknown"
                    elif kind == "acl":
                        database.state["functions"][0]["row"]["proacl"] = ["unknown=X/unknown"]
                    else:
                        database.state["dependencies"].append({"unknown": "dependent"})
                    current = copy.deepcopy(database.state)
                    with self.assertRaisesRegex(AssertionError, "身分已變"):
                        spec["clear"](stale, execute=database.execute)
                    self.assertEqual(database.state, current)
                    self.assertEqual(database.drops, 0)
                    # 只以 COMMENT 模擬本輪已知更新的 builder，其他未知改動絕不補授 ownership。
                    if kind == "comments":
                        spec["clear"](current, execute=database.execute)
                        self.assertFalse(database.state["functions"])

    # 新 SQL payload 的實際本文與屬性仍通過原 CQ24 有限資格，不改其 checker 或恢復政策。
    def test_ddl_boundary_body_compatibility(self):
        for microseconds in (0, 1):
            statements = ddl57.boundary_create_sql(7, 13, 41, microseconds)
            database = DDLDatabase("57")
            receipt = database.created("BEGIN; " + "; ".join(statements) + ";")
            row = receipt["functions"][0]["row"]
            catalog = {"function": {"oid": "100", "namespace": "public", "name": database.function, "owner": "articles_owner",
                                    "language": "plpgsql", "kind": "f", "returns": "trigger", "arguments": "",
                                    "security_definer": row["prosecdef"], "strict": False, "returns_set": False, "leakproof": False,
                                    "volatility": "v", "parallel": "u", "cost": 100, "rows": 0, "config": row["proconfig"],
                                    "acl": row["proacl"], "source": row["prosrc"]},
                       "triggers": [{"oid": "200", "function_oid": "100", "namespace": "public", "table": "login_failures",
                                     "name": database.trigger, "type": 7, "enabled": "O", "internal": False, "arguments": "", "condition": None,
                                     "definition": receipt["triggers"][0]["definition"]}],
                       "dependents": [{"class": "pg_trigger", "oid": "200", "subid": 0, "type": "n"}]}
            self.assertEqual(cq24.boundary_identity(catalog, "cq240123abcd"), {"member_id": 7, "epoch": 13, "first_id": 41, "microseconds": microseconds})
            self.assertEqual(row["prosrc"], cq24.BOUNDARY_BODY.format(member_id=7, epoch=13, first_id=41, microseconds=microseconds))

    # 未知原 CLI 在任何 guard／Docker／HTTP 前拒絕，不設 SAFE 或替換 guard。
    def test_ddl_original_unknown_cli_before_tools(self):
        with patch.object(sys, "argv", ["ticket57_http.py", "--cq32-unknown"]), \
                patch.object(ddl57.subprocess, "check_output") as tool, patch.object(ddl57, "urlopen") as http:
            with self.assertRaisesRegex(AssertionError, "未知驗證模式"):
                ddl57.main()
            tool.assert_not_called()
            http.assert_not_called()

    # 真 guard→固定 executor→完整 phase，含同 session 42501/rollback 與 O_EXCL，不跑舊 CLI。
    def test_ddl_fixed_driver_phase(self):
        for unknown_commit in (False, True):
            with self.subTest(unknown_commit=unknown_commit), tempfile.TemporaryDirectory() as folder:
                fixture, server = GuardFixture(folder), DDLServer()
                if unknown_commit:
                    server.databases["57"].fail_stage = "unknown-commit"
                baseline, packet = fixture.root / ".local/original-baseline.json", fixture.root / ".local/ddl.json"
                baseline.write_text(json.dumps({"format": 1, "status": "complete", "members": [
                    {"backend": backend, "login": {"user": {"id": 7 + index, "email": "cq32-owned@example.test"}}}
                    for index, backend in enumerate(driver.BACKENDS)]}))
                baseline.chmod(0o600)
                original = baseline.read_bytes()
                with fixture.active(), patch.object(driver, "PACKET", baseline), patch.object(driver, "DDL_PACKET", packet), \
                        patch.object(driver.subprocess, "run", server.run), patch.object(driver, "database_state", server.snapshot):
                    identity = driver.guard()
                    if unknown_commit:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            driver.ddl_probe(identity)
                    else:
                        driver.ddl_probe(identity)
                    saved = json.loads(packet.read_text())
                    self.assertEqual(saved["status"], "pending" if unknown_commit else "complete")
                    self.assertEqual(bool(server.databases["57"].state["functions"]), unknown_commit)
                    self.assertEqual(packet.stat().st_mode & 0o777, 0o600)
                    self.assertFalse(driver.DDL_ACTIVE)
                    with self.assertRaises(FileExistsError):
                        driver.ddl_probe(identity)
                    self.assertEqual(baseline.read_bytes(), original)
                    if not unknown_commit:
                        self.assertEqual(len(saved["cases"]), 16)
                        self.assertEqual(len(server.scripts), 4)

    # 安全 CLI 診斷不得因還原錯誤隱藏原型別，也不輸出前序秘密內容。
    def test_cleanup_failure_reporting(self):
        for module in (flask_settings, fast_settings):
            initial = RuntimeError("private-marker")
            cleanup = OSError("private-cleanup")
            cleanup.__context__ = initial
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                module.report_failure(cleanup)
            self.assertIn("OSError", output.getvalue())
            self.assertIn("RuntimeError", output.getvalue())
            self.assertNotIn("private", output.getvalue())

    # 真 seed 保存實際列表給出的 ID；再用原 packet 只讀驗證，不新增登入或資料。
    def test_seed_and_restart_control_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            packet_path = Path(folder) / "baseline.json"
            logins, states, writes, content = {}, {}, [], {}
            identity = {"source": {"all": "source", "application": "app"}, "keys": {}, "images": {}, "volume": "cq32_test_postgres_data"}
            baseline = {"accounts": {"count": 12, "sha256": "synthetic"}, "schema": "schema"}

            # 此假 API 僅實作種一篇／則的邊界，實際 driver 負責 ID 尋找與十二 reader 比較。
            def api(backend, method, path, body=None, token=None, status=200):
                if method == "POST":
                    writes.append((backend, path))
                if path == "/api/auth/register":
                    return {}
                if path == "/api/auth/login":
                    self.assertLessEqual(len(logins), 12)
                    login, state = login_fixture(backend)
                    number = driver.BACKENDS.index(backend) + 1
                    claims = {"jti": base64.urlsafe_b64encode(bytes([number]) * 16).decode().rstrip("="), "iat": 0, "exp": 28800}
                    login["access_token"] = "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"
                    login["user"]["id"] = number
                    state.update(jti=claims["jti"], account_id=number)
                    logins[backend], states[claims["jti"]] = login, state
                    return login
                if path == "/api/me":
                    self.assertEqual(token, logins[backend]["access_token"])
                    return logins[backend]["user"]
                if path == "/api/articles" and method == "POST":
                    content["article"] = {**body, "id": 41, "author": {"id": 1}, "version": 1, "created_backend": "laravel"}
                    return {}
                if path == "/api/articles/41/comments" and method == "POST":
                    content["comment"] = {**body, "id": 63, "article_id": 41, "version": 1, "created_backend": "laravel"}
                    return {}
                if path == "/api/articles/41":
                    return content["article"]
                return {"list": [content["comment"] if "comments" in path else content["article"]]}

            # 回傳已提交的真 session 形狀，actual session 函式仍核 JWT/期限/帳號。
            def sql(query):
                if "FROM login_sessions" in query:
                    return json.dumps(states[query.split("WHERE jti='")[1].split("'")[0]])
                return "original-settings"

            with patch.object(driver, "PACKET", packet_path), patch.object(driver, "READY", True), patch.object(driver, "api", api), \
                    patch.object(driver, "sql", sql), patch.object(driver, "database_state", return_value=baseline), \
                    patch.object(driver, "validate_migrations"), patch.object(driver, "schema_digest", return_value="schema"):
                driver.seed(identity)
                packet = json.loads(packet_path.read_text())
                self.assertEqual((packet["article_id"], packet["comment_id"]), (41, 63))
                self.assertEqual(len(packet["members"]), 12)
                before = len(writes)
                driver.check_restart(identity)
                self.assertEqual(len(writes), before)
                self.assertEqual(packet_path.stat().st_mode & 0o777, 0o600)

    # 七張表的真 digest/count 與八版 checksum 讀回，來源或持久值變更必可辨識。
    def test_database_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sql_folder = root / "database/sql"
            sql_folder.mkdir(parents=True)
            migrations = []
            for number in range(1, 9):
                path = sql_folder / f"{number:04}_fixture.sql"
                path.write_text("SELECT 1;\n")
                migrations.append({"version": path.stem, "checksum": hashlib.sha256(path.read_bytes()).hexdigest()})
            rows = {table: [{"id": 1}, {"id": 2}] for table in ("accounts", "articles", "comments", "login_sessions", "login_failures", "login_lockout_settings")}
            rows["schema_migrations"] = migrations
            catalog = ["synthetic schema before"]

            # 只回傳對應整表快照，保留被測函式真正的 hash/count/checksum 比較。
            def sql(query):
                if query.startswith("SELECT row_to_json(c)"):
                    return catalog[0]
                table = query.split(" FROM ")[1].split()[0]
                return "\n".join(json.dumps(row) for row in rows[table])

            with patch.object(driver, "ROOT", root), patch.object(driver, "sql", sql):
                before = driver.database_state()
                self.assertEqual(before["accounts"]["count"], 2)
                rows["accounts"][0]["id"] = 9
                self.assertNotEqual(driver.database_state()["accounts"], before["accounts"])
                catalog[0] = "synthetic schema after"
                self.assertNotEqual(driver.database_state()["schema"], before["schema"])
                migrations[0]["checksum"] = "wrong"
                with self.assertRaises(AssertionError):
                    driver.database_state()

    # 指定 runtime 直接 compile 全部維護來源，不產生 pycache；不把它當 lint/type。
    def test_compile_maintained_python(self):
        paths = list(Path(__file__).parent.glob("*.py"))
        self.assertTrue(paths)
        for path in paths:
            compile(path.read_bytes(), str(path), "exec")

    # 未知 CLI 先拒絕，不能先執行 Docker 或讀寫 fixture。
    def test_unknown_cli_before_tools(self):
        with patch.object(sys, "argv", ["cq32_http.py", "--unknown"]), patch.object(driver, "command") as command:
            with self.assertRaises(AssertionError):
                driver.main()
            command.assert_not_called()

    # restart 階段只能讀原 packet；ops/tests 變更不續簽，期限／資料／app 變更仍拒絕。
    def test_restart_packet_read_only(self):
        for fault in (None, "session", "database", "application", "pending"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                identity = {"keys": {"k": "digest"}, "images": {"i": "image"}, "volume": "cq32_test_postgres_data",
                            "source": {"all": "before-test-format", "application": "app"}}
                members = []
                for backend in driver.BACKENDS:
                    login, state = login_fixture(backend)
                    members.append({"backend": backend, "login": login, "session": state})
                content = {"public": "unchanged"}
                packet = {"format": 1, "status": "pending" if fault == "pending" else "complete", "identity": identity,
                          "members": members, "article_id": 41, "comment_id": 63, "content": content,
                          "settings": "settings", "database": {"accounts": "digest"}}
                path = Path(folder) / "baseline.json"
                path.write_text(json.dumps(packet))
                path.chmod(0o600)
                original = path.read_bytes()
                current = json.loads(json.dumps(identity))
                current["source"]["all"] = "after-test-format"
                if fault == "application":
                    current["source"]["application"] = "changed-app"

                # 此依賴只讀已存登入基準；actual session 三方期限驗證另有篡改測試。
                def session(backend, login):
                    state = login_fixture(backend)[1]
                    if fault == "session":
                        state["expires_at"] = "extended"
                    return state

                # 任何新登入或寫入都直接使 test 失敗，不能更新 token 掩蓋問題。
                def api(backend, method, endpoint, **kwargs):
                    self.assertEqual((method, endpoint), ("GET", "/api/me"))
                    return {"id": 7}

                with patch.object(driver, "PACKET", path), patch.object(driver, "session", session), patch.object(driver, "api", api), \
                        patch.object(driver, "shared_content", return_value=content), patch.object(driver, "sql", return_value="settings"), \
                        patch.object(driver, "database_state", return_value={"accounts": "changed" if fault == "database" else "digest"}):
                    if fault:
                        with self.assertRaises(AssertionError):
                            driver.check_restart(current)
                    else:
                        driver.check_restart(current)
                self.assertEqual(path.read_bytes(), original)

    # 已持鎖時 server timeout 使 actual race 進 finally；正常仍回傳原請求結果。
    def test_lock_handshake_timeout(self):
        for blocked, cleanup_fails in ((False, False), (True, False), (True, True)):
            holder = Holder(blocked, cleanup_fails)
            pools, original_pool = [], fiber.ThreadPoolExecutor

            # 觀察真 executor 的 shutdown；holder 再失敗時也必到達取消工作邊界。
            def pool_factory(*args, **kwargs):
                pool = original_pool(*args, **kwargs)
                pool.shutdown = Mock(wraps=pool.shutdown)
                pools.append(pool)
                return pool

            with patch.object(fiber.subprocess, "Popen", return_value=holder), patch.object(fiber, "sql", return_value="1"), \
                    patch.object(fiber, "call", return_value=(200, {})), patch.object(fiber, "ThreadPoolExecutor", pool_factory), contextlib.redirect_stdout(io.StringIO()):
                if blocked:
                    with self.assertRaises(OSError if cleanup_fails else AssertionError) as raised:
                        fiber.race("accounts", 7, [("fake", "GET", "/api/me")], ["accounts"])
                    self.assertTrue(holder.terminated)
                    if cleanup_fails:
                        self.assertIsInstance(raised.exception.__context__, AssertionError)
                else:
                    self.assertEqual(fiber.race("accounts", 7, [("fake", "GET", "/api/me")], ["accounts"]), [(200, {})])
                    self.assertEqual(holder.returncode, 0)
            pools[0].shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    # 21/24 真設定函式的正常／首寫失聯／雙失敗，均須還原非預設四欄並讀回。
    def test_settings_restore_boundaries(self):
        for module, function in ((flask_settings, flask_settings.settings_cases), (fast_settings, fast_settings.settings_cases), (fast_settings, fast_settings.settings_race)):
            for enabled in (True, False):
                for fault in (None, "write", "both"):
                    with self.subTest(module=module.__name__, function=function.__name__, enabled=enabled, fault=fault):
                        original = {"enabled": enabled, "window_minutes": 9, "failure_threshold": 17, "lock_seconds": 720}
                        state, writes = [original.copy()], []
                        account = {"locked_until": "old", "failure_epoch": 13}

                        # 假 API 只供此四欄案例，不宣稱測過後端驗證或副作用實作。
                        def call(base, method, path, body=None, token=None):
                            if method == "GET":
                                payload = {"success": True, "message": "OK", "data": state[0].copy()}
                                return (200, payload) if module is flask_settings else (200, payload, {})
                            errors = {}
                            for key in original:
                                if key not in body:
                                    errors[key] = ["此欄位為必填"]
                                elif type(body[key]) is not (bool if key == "enabled" else int):
                                    errors[key] = ["欄位型態不正確"]
                                elif key != "enabled" and not 1 <= body[key] <= {"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400}[key]:
                                    errors[key] = ["欄位格式或範圍不正確"]
                            if errors:
                                return 422, {"success": False, "message": module.MESSAGES["E0001"], "error_code": "E0001", "log_id": "fake", "errors": errors}, {}
                            writes.append(body.copy())
                            if body == original and len(writes) >= 2 and fault == "both":
                                raise OSError("restore failed")
                            if state[0]["enabled"] and not body["enabled"]:
                                account["locked_until"], account["failure_epoch"] = None, account["failure_epoch"] + 1
                            state[0] = body.copy()
                            if len(writes) == 1 and fault:
                                raise RuntimeError("write applied before disconnect")
                            payload = {"success": True, "message": "OK", "data": {}}
                            return (200, payload) if module is flask_settings else (200, payload, {})

                        # 只提供案例已知列；真設定函式仍做 same-value/epoch/事件斷言。
                        def query(statement):
                            return {**account, "n": 2}

                        # 回傳副本，避免前後快照因共用同一 dict 而形成自證。
                        def account_state(account_id):
                            return account.copy()

                        connection = Connection(query)
                        with patch.object(module, "call", call), patch.object(module, "owner_connection", return_value=connection):
                            # account_state 只是讀回依賴；不改 must/check、put_settings 或 finally。
                            with patch.object(flask_settings, "account_state", account_state), patch.object(fast_settings, "login", return_value="fake-token"):
                                args = ({"token": "admin"},) if function is fast_settings.settings_race else ({"token": "admin"}, {"id": 7})
                                if fault:
                                    with self.assertRaises(Exception) as raised:
                                        function(*args)
                                    if fault == "both":
                                        self.assertIsInstance(raised.exception.__context__, RuntimeError)
                                else:
                                    function(*args)
                        self.assertEqual(writes[-1], original)
                        if fault != "both":
                            self.assertEqual(state[0], original)

    # 門檻 2/3 的 actual concurrent_failures 狀態 oracle；mock 不代表真列鎖已驗。
    def test_concurrent_threshold_oracle(self):
        for threshold in (2, 3):
            statuses = [401, 429] if threshold == 2 else [401, 401]

            # fake DB 提供同一門檻與兩個等待／已提交事件。
            def query(statement):
                return {"enabled": True, "failure_threshold": threshold, "n": 2}

            # 回應用原 expect 逐字檢查；錯誤門檻 oracle 會使本案失敗。
            def call(*args, **kwargs):
                status = statuses.pop(0)
                code = "E0105" if status == 429 else "E0101"
                return status, {"success": False, "message": reading.MESSAGES[code], "error_code": code, "log_id": "fake"}, {"Retry-After": "120"}

            with patch.object(reading, "owner_connection", return_value=Connection(query)), patch.object(reading, "member", return_value={"id": 7, "email": "synthetic@example.test"}), \
                    patch.object(reading, "call", call), contextlib.redirect_stdout(io.StringIO()):
                reading.concurrent_failures("offline")
            self.assertEqual(statuses, [])

    # 以真正 SQL 執行反序時間與同時刻 tie；只證 oracle，不宣稱 PostgreSQL/HTTP 已驗。
    def test_created_time_order_oracle(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("CREATE TABLE articles(id integer,created_at text,visibility text)")
            connection.executemany("INSERT INTO articles VALUES(?,?,?)", [(1, "2026-11-05", "visible"), (2, "2026-11-04", "visible"), (3, "2026-11-04", "visible"), (4, "2026-11-06", "hidden")])
            with patch.object(reading, "owner_connection", return_value=connection):
                self.assertEqual(reading.public_article_ids(), [1, 3, 2])

    # 三支 actual main 的 true/false × 正常／寫入失敗／雙失敗都核精確四欄 UPDATE。
    def test_legacy_boolean_restore_control_flow(self):
        for number, backend in ((25, "gin"), (28, "echo"), (31, "fiber")):
            module = importlib.import_module(f"ticket{number}_http")
            for enabled in ("true", "false"):
                for fault in (None, "write", "both"):
                    with self.subTest(ticket=number, enabled=enabled, fault=fault), tempfile.TemporaryDirectory() as folder:
                        model = LegacyRestore(module, number, backend, enabled, fault)
                        root = Path(folder)
                        for service in (backend, "laravel"):
                            for location, value in ((f".local/ticket{number}-backend", base64.b64encode(b"t" * 32)), (".local/keys", base64.b64encode(b"d" * 32))):
                                key = root / location / f"{service}.key"
                                key.parent.mkdir(parents=True, exist_ok=True)
                                key.write_bytes(value)
                        with entered(root), patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": f"ticket{number}_test", "COMPOSE_FILE": f"compose.yaml:ops/tests/ticket{number}_compose.yaml"}), \
                                patch.object(module.subprocess, "check_output", model.output), patch.object(module, "urlopen", return_value=Response()), \
                                patch.object(module, "sql", model.sql), patch.object(module, "call", model.call), \
                                patch.object(module, "register", model.register), patch.object(module, "expect", model.expect), \
                                contextlib.redirect_stdout(io.StringIO()):
                            expected_error = OSError if fault == "both" else RuntimeError if fault == "write" else BoundaryDone
                            with self.assertRaises(expected_error) as raised:
                                module.main()
                            if fault == "both":
                                self.assertIsInstance(raised.exception.__context__, RuntimeError)
                        self.assertEqual(model.restored, f"UPDATE login_lockout_settings SET enabled={enabled},window_minutes=9,failure_threshold=17,lock_seconds=720 WHERE id=1")

    # 停止呼叫本身／停止後檢查失敗都應啟回一次；cleanup 失敗保留原例外鏈。
    def test_stop_recovery(self):
        for target in (lifecycle, echo):
            for fault in (None, "stop", "read", "both"):
                with self.subTest(target=target.__name__, fault=fault), tempfile.TemporaryDirectory() as folder:
                    key = Path(folder) / "fake.key"
                    key.write_bytes(b"synthetic")
                    calls = []

                    # 僅模擬工具結果，原 try/finally/check 都必須執行。
                    def tool(args, **kwargs):
                        starting = args[0] == "ops/start.sh" or "up" in args
                        calls.append("start" if starting else "stop")
                        if not starting and fault in ("stop", "both"):
                            raise RuntimeError("stop failed after effect")
                        if starting and fault == "both":
                            raise OSError("restart failed")

                    # 停止後僅回傳固定本票 volume，或在唯讀查核故意失敗。
                    def stopped(args, **kwargs):
                        if fault == "read":
                            raise ValueError("stopped assertion read failed")
                        return "ticket37_test_postgres_data\n" if "volume" in args else ""

                    with patch.object(target.subprocess, "check_call", tool), patch.object(target.subprocess, "run", tool), \
                            patch.object(target.subprocess, "check_output", stopped), \
                            patch.object(echo, "call", return_value=(500, {"success": False, "message": echo.MESSAGES["E9999"], "error_code": "E9999", "log_id": "fake"}, {})), \
                            contextlib.redirect_stdout(io.StringIO()):
                        function, args = (lifecycle.stop_and_restart, ([key], [b"synthetic"])) if target is lifecycle else (echo.database_unavailable, ("fake-token",))
                        # Echo 的 read fault 放在 HTTP 依賴，仍由原 finally 啟回。
                        with patch.object(echo, "call", side_effect=ValueError("fake HTTP read") if fault == "read" else None,
                                          return_value=(500, {"success": False, "message": echo.MESSAGES["E9999"], "error_code": "E9999", "log_id": "fake"}, {})):
                            if fault:
                                with self.assertRaises(Exception) as raised:
                                    function(*args)
                                if fault == "both":
                                    self.assertIsInstance(raised.exception.__context__, RuntimeError)
                            else:
                                function(*args)
                    self.assertEqual(calls, ["stop", "start"])

    # 故障注入只還原本輪帳號；正常／HTTP 失敗／斷言失敗與還原雙失敗都測原函式。
    def test_owned_epoch_cleanup(self):
        for fault in (None, "transport", "status", "both"):
            with self.subTest(fault=fault):
                epoch, updates = [13], []
                changed = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 600}

                # 模擬精確帳號的 DB 值，不修改其他帳號或事件。
                def sql(query):
                    if query.startswith("UPDATE"):
                        self.assertIn("WHERE id=7", query)
                        updates.append(query)
                        if "failure_epoch=13 " in query and fault == "both":
                            raise OSError("cleanup failed")
                        epoch[0] = int(query.split("failure_epoch=")[1].split()[0])
                        return ""
                    return "until" if "locked_until" in query else str(epoch[0])

                # 真 expect 保留；假的 HTTP 可以失敗或回錯 status 來觸發 rollback 斷言。
                def call(base, method, path, body=None, token=None):
                    if method == "GET":
                        return 200, {"success": True, "message": "OK", "data": changed}, {}
                    if fault in ("transport", "both"):
                        raise RuntimeError("fake transport")
                    return (200, {"success": True, "message": "OK", "data": {}}, {}) if fault == "status" else \
                        (500, {"success": False, "message": echo.MESSAGES["E9999"], "error_code": "E9999", "log_id": "fake"}, {})

                with patch.object(administration, "sql", sql), patch.object(echo, "call", call):
                    if fault:
                        with self.assertRaises(Exception) as raised:
                            administration.clear_rollback_probe(echo, "/api/settings/login-lockout", {"token": "fake"}, {"id": 7}, changed, "until", {**changed, "enabled": False})
                        if fault == "both":
                            self.assertIsInstance(raised.exception.__context__, RuntimeError)
                    else:
                        self.assertEqual(administration.clear_rollback_probe(echo, "/api/settings/login-lockout", {"token": "fake"}, {"id": 7}, changed, "until", {**changed, "enabled": False}), 13)
                if fault != "both":
                    self.assertEqual(epoch[0], 13)
                self.assertEqual(len(updates), 2)

    # 30 設定首寫已生效但 HTTP 斷線時，finally 仍恢復非預設 true/false 四欄。
    def test_administration_settings_restore(self):
        for enabled in (True, False):
            for cleanup_fails in (False, True):
                with self.subTest(enabled=enabled, cleanup_fails=cleanup_fails):
                    original = {"enabled": enabled, "window_minutes": 9, "failure_threshold": 17, "lock_seconds": 720}
                    state, writes = [original.copy()], []

                    # 首寫視為已提交後失聯；第二次是 actual finally 的精確原值還原。
                    def call(base, method, path, body=None, token=None):
                        if method == "PUT":
                            writes.append(body.copy())
                            if len(writes) == 2 and cleanup_fails:
                                raise OSError("restore failed")
                            state[0] = body.copy()
                            if len(writes) == 1:
                                raise RuntimeError("committed but disconnected")
                            return 200, {"success": True, "message": "OK", "data": {}}, {}
                        return 200, {"success": True, "message": "OK", "data": state[0].copy()}, {}

                    with patch.object(echo, "call", call):
                        with self.assertRaises(Exception) as raised:
                            administration.settings_cases(echo, {"token": "admin"}, {"token": "member"}, "offline", regress=Mock())
                    self.assertEqual(writes[-1], original)
                    if cleanup_fails:
                        self.assertIsInstance(raised.exception.__context__, RuntimeError)
                    else:
                        self.assertEqual(state[0], original)

    # 真 guard 正例及各身分負例；所有 key/source 均為 tempfile 合成資料。
    def test_fixed_guard(self):
        for fault in (None, "project", "files", "volume", "key", "key_value", "template", "nginx_source", "port", "health", "cwd_label"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                fixture = GuardFixture(folder)
                if fault == "volume":
                    fixture.services["db"]["Mounts"][0]["Name"] = "demo_postgres_data"
                elif fault == "key":
                    fixture.services["laravel"]["Mounts"][0]["RW"] = True
                elif fault == "key_value":
                    (fixture.root / ".local/cq32-keys/laravel.key").write_bytes((fixture.root / ".local/keys/laravel.key").read_bytes())
                elif fault == "template":
                    (fixture.root / ".local/cq32-compose.yaml").write_text("unapproved override")
                elif fault == "nginx_source":
                    (fixture.root / "ops/nginx/default.conf").write_text("user working nginx")
                elif fault == "port":
                    fixture.services["laravel"]["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostPort"] = "9999"
                elif fault == "health":
                    fixture.services["laravel"]["State"]["Health"]["Status"] = "starting"
                elif fault == "cwd_label":
                    fixture.services["db"]["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/tmp/unknown"
                with fixture.active():
                    if fault in ("project", "files"):
                        os.environ["COMPOSE_PROJECT_NAME" if fault == "project" else "COMPOSE_FILE"] = "wrong"
                    if fault:
                        with self.assertRaises(AssertionError):
                            driver.guard()
                        self.assertFalse(driver.READY)
                        with self.assertRaises(AssertionError):
                            driver.api("laravel", "POST", "/api/auth/register", {})
                    else:
                        self.assertEqual(driver.guard()["volume"], "cq32_test_postgres_data")

    # 改持久期限、issued_at 或撤銷時，兩個實際讀回函式都必拒絕，不能比較 local dict 自證。
    def test_persistent_expiry(self):
        for function, module in ((lifecycle.session_state, lifecycle), (driver.session, driver)):
            for changed in (None, "expires_epoch", "issued_epoch", "revoked_at"):
                with self.subTest(function=function.__name__, changed=changed):
                    login, state = login_fixture()
                    if changed:
                        state[changed] = "28801.000001" if changed != "revoked_at" else "1970-01-01"
                    with patch.object(module, "sql", return_value=json.dumps(state)):
                        args = (login,) if module is lifecycle else ("fastify", login)
                        if changed:
                            with self.assertRaises(AssertionError):
                                function(*args)
                        else:
                            self.assertEqual(function(*args), state)

    # 既有 private packet 不得被 seed 覆寫，連第一個 API 都不能送出。
    def test_seed_reentry(self):
        with tempfile.TemporaryDirectory() as folder:
            packet = Path(folder) / "baseline.json"
            packet.write_text("keep")
            with patch.object(driver, "PACKET", packet), patch.object(driver, "READY", True), patch.object(driver, "api") as api:
                with self.assertRaises(FileExistsError):
                    driver.seed({})
                api.assert_not_called()
                self.assertEqual(packet.read_text(), "keep")

    # 未知 migration 必須在任何公開寫入前停止，保留 pending 而非空白成功基準。
    def test_migration_rejection_before_seed_write(self):
        with tempfile.TemporaryDirectory() as folder:
            packet = Path(folder) / "baseline.json"
            with patch.object(driver, "PACKET", packet), patch.object(driver, "READY", True), \
                    patch.object(driver, "validate_migrations", side_effect=AssertionError("checksum")), patch.object(driver, "api") as api:
                with self.assertRaises(AssertionError):
                    driver.seed({})
                api.assert_not_called()
                self.assertEqual(json.loads(packet.read_text())["operation"], "schema-check")

    # 非冪等 register 失敗時留 pending/0600，再次 seed 拒絕而不盲目重送。
    def test_seed_failure_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            packet = Path(folder) / "baseline.json"
            with patch.object(driver, "PACKET", packet), patch.object(driver, "READY", True), patch.object(driver, "validate_migrations"), \
                    patch.object(driver, "schema_digest", return_value="schema"), patch.object(driver, "api", side_effect=RuntimeError("fake transport")) as api:
                with self.assertRaises(RuntimeError):
                    driver.seed({})
                self.assertEqual(json.loads(packet.read_text())["status"], "pending")
                self.assertEqual(packet.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    driver.seed({})
                self.assertEqual(api.call_count, 1)

    # prepare 只在自有 copied root 建置；實際替換／重入／unexpected 列表由原函式檢查。
    def test_prepare_and_source_consumers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tests = Path(prepare.__file__).parent
            (root / "frontend/vue/src").mkdir(parents=True)
            (root / "backends/laravel/config").mkdir(parents=True)
            (root / "frontend/vue/src/auth.js").write_bytes((tests.parents[1] / "frontend/vue/src/auth.js").read_bytes())
            (root / "backends/laravel/config/cors.php").write_text("http://localhost:8080")
            local, frontend = root / ".local/ticket33-backend", root / ".local/ticket33-frontend"
            with patch.object(prepare, "ROOT", root), patch.object(prepare, "LOCAL", local), patch.object(prepare, "FRONTEND", frontend), contextlib.redirect_stdout(io.StringIO()):
                prepare.source_check()
                prepare.prepare()
                keys = [(local / f"{name}.key").read_bytes() for name in ("laravel", "fiber")]
                prepare.prepare()
                self.assertEqual(keys, [(local / f"{name}.key").read_bytes() for name in ("laravel", "fiber")])
                auth = (frontend / "src/auth.js").read_text()
                self.assertIn("http://localhost:18332", auth)
                self.assertIn("http://localhost:18331", auth)
                self.assertEqual(auth.count("http://localhost:0"), 10)
                (frontend / "src/auth.js").write_text(auth.replace("http://localhost:0", "http://localhost:19000", 1))
                with self.assertRaises(AssertionError):
                    prepare.prepare()
                self.assertIn("http://localhost:19000", (frontend / "src/auth.js").read_text())
                (frontend / "src/auth.js").write_text("http://localhost:8081")
                with self.assertRaises(AssertionError):
                    prepare.prepare()
                # 非十二項來源在建立任何新 fixture 前拒絕，不能留下新的 key 或副本。
                source = root / "frontend/vue/src/auth.js"
                source.write_text(source.read_text() + "\nhttp://localhost:8081")
                fresh_local, fresh_frontend = root / ".local/invalid-backend", root / ".local/invalid-frontend"
                with patch.object(prepare, "LOCAL", fresh_local), patch.object(prepare, "FRONTEND", fresh_frontend):
                    with self.assertRaises(AssertionError):
                        prepare.prepare()
                    self.assertFalse(fresh_local.exists())
                    self.assertFalse(fresh_frontend.exists())

    # 30 的實際有限副本轉寫須仍可重入、解析；不把編譯副本當原 HTTP suite 通過。
    def test_echo_copy_consumer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tests = root / "ops/tests"
            tests.mkdir(parents=True)
            for name in ("ticket28_http.py", "ticket29_http.py"):
                (tests / name).write_bytes((Path(administration.__file__).parent / name).read_bytes())
            with patch.object(administration, "ROOT", root), patch.object(administration, "LOCAL", root / ".local/ticket30-backend"), contextlib.redirect_stdout(io.StringIO()):
                destination = administration.prepare_reuse()
                originals = {name: (destination / name).read_bytes() for name in ("ticket28_http.py", "ticket29_http.py")}
                administration.prepare_reuse()
                for name, content in originals.items():
                    self.assertEqual((destination / name).read_bytes(), content)
                    compile(content, str(destination / name), "exec")


if __name__ == "__main__":
    print("Runtime: Python " + sys.version.split()[0], flush=True)
    unittest.main(verbosity=2)
