"""CQ32 固定新副本的 guard、唯一測資與重啟讀回；服務啟停由 Main 執行。

初始化先以 git ls-files 複製固定來源子集（排除秘密、依賴、.git/docs）至全新副本；
NGINX 取候選 HEAD blob，cq32_compose.yaml 複製至副本 .local/cq32-compose.yaml。
產生十二份獨立 cq32-keys，再由副本 ops/setup.sh 產生自有 .env/default keys。
這些步驟由 Main 依核准方案執行；driver 不 bootstrap、改埠或清空既有目標。
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

import ticket39_http as ddl39
import ticket57_http as ddl57

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / ".local/cq32-fixture"
PROJECT = "cq32_test"
FILES = ("compose.yaml", ".local/cq32-compose.yaml")
BACKENDS = ("laravel", "symfony", "aspnet-core", "django", "flask", "fastapi",
            "gin", "echo", "fiber", "express", "fastify", "nestjs")
FRONTENDS = ("vue", "nuxt", "react", "next", "angular", "sveltekit")
BASES = {name: f"http://127.0.0.1:{8081 + index}" for index, name in enumerate(BACKENDS)}
PACKET = ROOT / ".local/cq32-baseline.json"
READY = False
DDL_PACKET = ROOT / ".local/cq32-ddl-probe.json"
DDL_ACTIVE = False
SETTINGS_PACKET = ROOT / ".local/cq32-settings-probe.json"
SETTINGS_MODULES = ("cq32_http.py", "ticket20_http.py", "ticket21_http.py", "ticket22_http.py", "ticket24_http.py",
                    "ticket25_http.py", "ticket28_http.py", "ticket30_http.py", "ticket31_http.py", "ticket37_http.py",
                    "ticket37_query_count.py", "ticket39_http.py", "ticket40_http.py", "ticket57_http.py")
# 固定 child 只解包完整來源至自有暫存目錄正常 import；不切 source、不安裝依賴、不載入任意模組。
SETTINGS_BOOTSTRAP = '''import hashlib,json,sys,tempfile
from pathlib import Path
payload=json.load(sys.stdin)
names=("cq32_http.py","ticket20_http.py","ticket21_http.py","ticket22_http.py","ticket24_http.py","ticket25_http.py","ticket28_http.py","ticket30_http.py","ticket31_http.py","ticket37_http.py","ticket37_query_count.py","ticket39_http.py","ticket40_http.py","ticket57_http.py")
try:
    # 白名單與完整 bytes 雜湊都相符才 import；不讀取容器外來源或任意路徑。
    assert set(payload["sources"])==set(names)==set(payload["source_hashes"])
    with tempfile.TemporaryDirectory(prefix="cq32-settings-") as folder:
        tests=Path(folder)/"ops/tests"
        tests.mkdir(parents=True)
        for name in names:
            source=payload["sources"][name]
            assert hashlib.sha256(source.encode()).hexdigest()==payload["source_hashes"][name]
            (tests/name).write_text(source,encoding="utf-8")
        sys.path.insert(0,str(tests))
        # 正常模組 import 會使用既有 native psycopg；不切函式或偽造套件。
        import cq32_http
        result=cq32_http.settings_native_worker(payload)
    print(json.dumps(result,ensure_ascii=False),flush=True)
except BaseException as error:
    # 未知錯誤只回型別並非零；host 保留 pending，不輸出 stdin 中的憑證。
    print(json.dumps({"status":"pending","failure":[{"type":type(error).__name__}]}),flush=True)
    sys.exit(1)
'''


# DDLFailure 只攜帶固定 SQLSTATE，不把 psql 的語句／內容帶到終端。
class DDLFailure(RuntimeError):
    # 非零或未知工具結果不能當成已提交 receipt，亦不能按名字猜測清理。
    def __init__(self, sqlstate=None):
        super().__init__("固定 DDL 執行失敗")
        self.sqlstate = sqlstate


# DDLCaseFailure 是案例內的受控失敗，只有 finally 成功後才能認定此負例已完成。
class DDLCaseFailure(RuntimeError):
    pass


# SettingsCaseFailure 只標記已提交 numeric fixture 後的受控錯誤；清理錯誤不可當預期成功吞掉。
class SettingsCaseFailure(RuntimeError):
    pass


# check 只輸出受控原因，避免例外回應中的 token／雜湊被帶到終端。
def check(condition, reason):
    if not condition:
        raise AssertionError(reason)


# NoRedirect 禁止固定 API 請求跟隨 redirect，以免憑證或寫入改投別處。
class NoRedirect(HTTPRedirectHandler):
    # 保留原回應供狀態斷言，不允許 HTTP client 自動改變目標。
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


# 固定 loopback 請求不經環境 proxy，避免 URL 已核對卻由代理改投其他目標。
OPENER = build_opener(ProxyHandler({}), NoRedirect)


# command 固定在副本 cwd 執行，任何工具失敗中止；不回顯可能含環境秘密的輸出。
def command(*args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
    check(result.returncode == 0, "CQ32 工具執行失敗")
    return result.stdout.strip()


# compose 總是顯式帶固定 project/files/profile，沒有可改指專案的 CLI 選項。
def compose(*args):
    return command("docker", "compose", "-p", PROJECT, "-f", FILES[0], "-f", FILES[1], "--profile", "*", *args)


# inspect_service 核對實際 labels、來源 cwd/config、網路、健康與目前 Compose image。
def inspect_service(service):
    ids = compose("ps", "-q", service).splitlines()
    check(len(ids) == 1, "CQ32 服務未唯一啟動：" + service)
    item = json.loads(command("docker", "inspect", ids[0]))[0]
    labels = item["Config"]["Labels"]
    check(labels.get("com.docker.compose.project") == PROJECT and
          labels.get("com.docker.compose.service") == service and
          Path(labels.get("com.docker.compose.project.working_dir", "/")).resolve() == ROOT.resolve() and
          labels.get("com.docker.compose.project.config_files", "").split(",") ==
          [str(ROOT / path) for path in FILES], "CQ32 容器來源不符：" + service)
    check(item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy" and
          set(item["NetworkSettings"]["Networks"]) == {PROJECT + "_default"}, "CQ32 服務身分／健康不符：" + service)
    image = compose("images", "-q", service)
    check(image and item["Image"].removeprefix("sha256:") == image.removeprefix("sha256:"), "CQ32 映像不符：" + service)
    return item


# source_identity 逐檔比較副本與目前原來源，重啟後再比 digest；秘密及產生檔不讀取。
def source_identity():
    ignored = {".local", ".git", ".scratch", "docs", "node_modules", "vendor", "dist", "__pycache__", "obj", "bin"}
    digest, application, count = hashlib.sha256(), hashlib.sha256(), 0
    # Git 清單包含本票新增檔；逐一要求副本存在，不能漏檔後只對剩餘部分算 digest。
    names = subprocess.check_output(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                                     ".dockerignore", ".env.example", "compose.yaml", "backends", "frontend", "ops", "database"]).decode().split("\0")
    for name in sorted(set(names) - {""}):
        relative = Path(name)
        path = ROOT / relative
        if any(part in ignored for part in relative.parts) or path.name == ".env":
            continue
        if relative.parts[0] not in {"backends", "frontend", "ops", "database", ".dockerignore", ".env.example", "compose.yaml"}:
            continue
        check(not path.is_symlink(), "CQ32 來源副本不可含 symlink")
        check(path.is_file(), "CQ32 來源副本缺檔")
        original = REPO / relative
        # NGINX 有使用者未納候選提交的變更；副本唯一採 HEAD blob，不覆寫原檔。
        expected = subprocess.check_output(["git", "-C", str(REPO), "show", "HEAD:ops/nginx/default.conf"]) if relative.as_posix() == "ops/nginx/default.conf" else original.read_bytes()
        check(original.is_file() and path.read_bytes() == expected and
              stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(original.stat().st_mode), "CQ32 來源副本已變")
        record = str(relative).encode() + b"\0" + str(stat.S_IMODE(path.stat().st_mode)).encode() + b"\0" + path.read_bytes()
        digest.update(record)
        if relative.parts[:2] != ("ops", "tests"):
            application.update(record)
        count += 1
    check(count > 0 and all((ROOT / f"backends/{name}/Dockerfile").is_file() for name in BACKENDS), "CQ32 必要來源不完整")
    return {"all": digest.hexdigest(), "application": application.hexdigest()}


# guard 在任何 API/packet 寫入之前核定唯一新專案；失敗不能保留可寫旗標。
def guard():
    global READY
    READY = False
    check(Path.cwd().resolve() == ROOT and ROOT.resolve() == ROOT and ROOT.is_dir(), "CQ32 cwd 不是固定副本")
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT and
          os.environ.get("COMPOSE_FILE") == ":".join(FILES), "CQ32 Compose 環境不符")
    check((ROOT / FILES[1]).read_bytes() == (REPO / "ops/tests/cq32_compose.yaml").read_bytes(), "CQ32 override 與固定 template 不符")
    source = source_identity()
    config = json.loads(compose("config", "--format", "json"))
    check(config["name"] == PROJECT and config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data" and
          config["services"]["db"]["environment"].get("CQ32_FIXTURE") == "isolated-cq32" and
          config["networks"]["default"]["ipam"]["config"] == [{"subnet": "10.251.132.0/24"}], "CQ32 固定設定不符")
    network = json.loads(command("docker", "network", "inspect", PROJECT + "_default"))[0]
    check(network["Name"] == PROJECT + "_default" and network["Labels"].get("com.docker.compose.project") == PROJECT and
          network["IPAM"]["Config"] == [{"Subnet": "10.251.132.0/24", "Gateway": "10.251.132.1"}], "CQ32 實際網段不符")
    volume = json.loads(command("docker", "volume", "inspect", PROJECT + "_postgres_data"))[0]
    check(volume["Name"] == PROJECT + "_postgres_data" and volume["Labels"].get("com.docker.compose.project") == PROJECT and
          volume["Labels"].get("com.docker.compose.volume") == "postgres_data" and not volume.get("Options"), "CQ32 實際資料卷來源不符")
    database = inspect_service("db")
    check("CQ32_FIXTURE=isolated-cq32" in database["Config"]["Env"] and
          not database["HostConfig"]["PortBindings"] and
          any(m.get("Type") == "volume" and m.get("Name") == PROJECT + "_postgres_data" and
              m.get("Destination") == "/var/lib/postgresql" for m in database["Mounts"]), "CQ32 DB 資料卷／私有性不符")
    keys, images = {}, {"db": database["Image"]}
    # 比對兩組全部 default keys，避免把別後端的展示 key 換名後當成獨立測試 key。
    defaults = {hashlib.sha256((root / f".local/keys/{backend}.key").read_bytes().strip()).hexdigest()
                for root in (ROOT, REPO) for backend in BACKENDS}
    # 全部十二後端逐一查實際 loopback、DB 及獨立唯讀金鑰，不能只信 config 宣告。
    for index, backend in enumerate(BACKENDS):
        item = inspect_service(backend)
        internal = "8080/tcp" if backend == "aspnet-core" else "8000/tcp"
        check(item["HostConfig"]["PortBindings"] == {internal: [{"HostIp": "127.0.0.1", "HostPort": str(8081 + index)}]} and
              {"DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"}.issubset(item["Config"]["Env"]), "CQ32 API 埠／DB 不符")
        key_path = ROOT / f".local/cq32-keys/{backend}.key"
        check(not key_path.is_symlink() and any(m.get("Type") == "bind" and
              Path(m.get("Source", "/")).resolve() == key_path and m.get("Destination") == f"/run/secrets/{backend}-jwt.key" and
              m.get("RW") is False for m in item["Mounts"]), "CQ32 測試金鑰掛載不符")
        encoded = key_path.read_bytes().strip()
        check(len(base64.b64decode(encoded, validate=True)) == 32 and
              hashlib.sha256(encoded).hexdigest() not in defaults, "CQ32 金鑰隔離不符")
        keys[backend] = hashlib.sha256(encoded).hexdigest()
        images[backend] = item["Image"]
    check(len(set(keys.values())) == 12, "CQ32 後端不可共用金鑰")
    for frontend in FRONTENDS:
        service = "frontend-" + frontend
        images[service] = inspect_service(service)["Image"]
    nginx = inspect_service("nginx")
    images["nginx"] = nginx["Image"]
    check(nginx["HostConfig"]["PortBindings"] == {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}, "CQ32 NGINX 埠不符")
    # 身分全部確認後只做 health/公開頁讀取；最後才允許測試寫入。
    for backend, base in BASES.items():
        with OPENER.open(base + "/up", timeout=10) as response:
            expected = b'{"status":"ok"}' if backend in ("symfony", "aspnet-core") else b"OK"
            check(response.status == 200 and response.read() == expected, "CQ32 API health 不符")
    for frontend in FRONTENDS:
        with OPENER.open(f"http://127.0.0.1:8080/{frontend}/", timeout=10) as response:
            check(response.status == 200, "CQ32 前端路徑未就緒")
    identity = compose("exec", "-T", "db", "psql", "-XAt", "-U", "articles_owner", "-d", "articles", "-c", "SELECT current_database()||':'||current_user")
    check(identity == "articles:articles_owner", "CQ32 DB 身分不符")
    READY = True
    return {"source": source, "keys": keys, "images": images, "volume": PROJECT + "_postgres_data"}


# sql 只提供 driver 內部固定 SELECT，未完成 guard 不可使用。
def sql(statement):
    check(READY and statement.startswith("SELECT "), "CQ32 尚未 guard 或非讀取 SQL")
    return compose("exec", "-T", "db", "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c", statement)


# api 不重試非冪等請求；不跟 redirect，失敗不列出 body 或 token。
def api(backend, method, path, body=None, token=None, status=200):
    check(READY and backend in BACKENDS, "CQ32 API 未 guard")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(BASES[backend] + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    try:
        response = OPENER.open(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        payload = json.loads(response.read())
        check(response.status == status and isinstance(payload, dict) and
              set(payload) == {"success", "message", "data"} and payload["success"] is True and
              payload["message"] == "OK" and isinstance(payload["data"], dict), "CQ32 API 契約不符")
        return payload["data"]


# session 交叉核 JWT、原回應與真實 jti 記錄，精確保留 DB 微秒及撤銷狀態。
def session(backend, login):
    claims = json.loads(base64.urlsafe_b64decode(login["access_token"].split(".")[1] + "==="))
    jti = claims["jti"]
    check(isinstance(jti, str) and re.fullmatch(r"[A-Za-z0-9_-]{21}[AQgw]", jti), "CQ32 jti 格式不符")
    value = json.loads(sql("SELECT row_to_json(s) FROM (SELECT jti,account_id,backend_id,"
                           "issued_at AT TIME ZONE 'UTC' AS issued_at,expires_at AT TIME ZONE 'UTC' AS expires_at,"
                           "extract(epoch FROM issued_at)::text AS issued_epoch,"
                           "extract(epoch FROM expires_at)::text AS expires_epoch,revoked_at "
                           f"FROM login_sessions WHERE jti='{jti}') s"))
    expiry = datetime.strptime(login["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Taipei"))
    check(type(claims["iat"]) is int and type(claims["exp"]) is int and
          Decimal(value["issued_epoch"]) == claims["iat"] and Decimal(value["expires_epoch"]) == claims["exp"] and
          expiry.timestamp() == claims["exp"] and value["account_id"] == login["user"]["id"] and
          value["backend_id"] == backend and value["revoked_at"] is None, "CQ32 原登入期限／持久 session 不符")
    return value


# validate_migrations 在首次 API 寫入前拒絕未知版本或 checksum，讀回時也維持相同前提。
def validate_migrations():
    migrations = [json.loads(line) for line in sql("SELECT row_to_json(t) FROM schema_migrations t ORDER BY version").splitlines()]
    expected = {path.stem: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT / "database/sql").glob("[0-9][0-9][0-9][0-9]_*.sql")}
    check(len(expected) == 8 and {row["version"]: row["checksum"].strip() for row in migrations} == expected, "CQ32 八版 migration／checksum 不符")


# schema_digest 一次讀穩定 catalog view，補足空表結構／索引／授權不在 row digest 的缺口。
def schema_digest():
    # 同次 catalog 查詢保存結構／索引／授權，空表的 DDL 變化也不能逃過 row digest。
    views = (("information_schema.tables", "table_schema"), ("information_schema.columns", "table_schema"),
             ("information_schema.table_constraints", "table_schema"), ("information_schema.key_column_usage", "table_schema"),
             ("information_schema.referential_constraints", "constraint_schema"), ("information_schema.check_constraints", "constraint_schema"),
             ("information_schema.role_table_grants", "table_schema"), ("information_schema.routines", "routine_schema"),
             ("information_schema.triggers", "event_object_schema"), ("pg_catalog.pg_indexes", "schemaname"),
             ("pg_catalog.pg_namespace", "nspname"))
    parts = [f"SELECT '{view}' AS kind,to_jsonb(t) AS value FROM {view} t WHERE {field}='public'" for view, field in views]
    raw = sql("SELECT row_to_json(c) FROM (" + " UNION ALL ".join(parts) + ") c ORDER BY kind,value::text")
    return hashlib.sha256(raw.encode()).hexdigest()


# database_state 為六業務表及版本表留 count/digest，避免只讀 owned row 就漏掉重設副作用。
def database_state():
    validate_migrations()
    result = {}
    for table in ("accounts", "articles", "comments", "login_sessions", "login_failures", "login_lockout_settings", "schema_migrations"):
        raw = sql(f"SELECT row_to_json(t) FROM {table} t ORDER BY row_to_json(t)::text")
        result[table] = {"count": len(raw.splitlines()), "sha256": hashlib.sha256(raw.encode()).hexdigest()}
    result["schema"] = schema_digest()
    return result


# save_packet 每次在非冪等操作前落盤 pending，失敗或中斷後不能重送或覆寫 baseline。
def save_packet(file, packet):
    file.seek(0)
    json.dump(packet, file, ensure_ascii=False)
    file.truncate()
    file.flush()
    os.fsync(file.fileno())


# shared_content 讓每個 reader 讀同一筆文章與公開留言，不把不同 ID 當成互通證據。
def shared_content(article_id, comment_id):
    values = {}
    for backend in BACKENDS:
        article = api(backend, "GET", f"/api/articles/{article_id}")
        comments = api(backend, "GET", f"/api/articles/{article_id}/comments?page=1&per_page=100")["list"]
        matches = [row for row in comments if row["id"] == comment_id]
        check(len(matches) == 1, "CQ32 公開留言未唯一讀回")
        values[backend] = {"article": article, "comment": matches[0]}
    check(all(value == values["laravel"] for value in values.values()), "CQ32 十二 reader 資料不一致")
    return values


# seed 限十二個新會員與一篇／則內容；O_EXCL 先佔有 packet，任何未知結果都保留 pending。
def seed(identity):
    check(READY, "CQ32 seed 未 guard")
    fd = os.open(PACKET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    packet = {"format": 1, "status": "pending", "operation": "schema-check", "identity": identity, "members": []}
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        save_packet(file, packet)
        validate_migrations()
        packet["schema_before"] = schema_digest()
        save_packet(file, packet)
        nonce = secrets.token_hex(8)
        for backend in BACKENDS:
            email = f"cq32-{nonce}-{backend}@example.test"
            packet["operation"] = backend + ":register"
            save_packet(file, packet)
            api(backend, "POST", "/api/auth/register", {"email": email, "password": "Cq321234", "nickname": "CQ32保留測試"}, status=201)
            packet["operation"] = backend + ":login"
            save_packet(file, packet)
            login = api(backend, "POST", "/api/auth/login", {"email": email, "password": "Cq321234"})
            check(api(backend, "GET", "/api/me", token=login["access_token"]) == login["user"], "CQ32 停止前登入身分不符")
            packet["members"].append({"backend": backend, "login": login, "session": session(backend, login)})
            save_packet(file, packet)
        owner = packet["members"][0]["login"]
        title, body = "CQ32-" + nonce, "重啟保留\r\n共同內容"
        packet["operation"] = "laravel:article"
        save_packet(file, packet)
        api("laravel", "POST", "/api/articles", {"title": title, "body": body}, owner["access_token"], 201)
        rows = api("laravel", "GET", "/api/me/articles?page=1&per_page=100", token=owner["access_token"])["list"]
        matches = [row for row in rows if row["title"] == title and row["body"] == body]
        check(len(matches) == 1, "CQ32 新文章未唯一讀回")
        article_id = matches[0]["id"]
        packet["article_id"] = article_id
        packet["operation"] = "laravel:comment"
        save_packet(file, packet)
        api("laravel", "POST", f"/api/articles/{article_id}/comments", {"body": "CQ32留言-" + nonce}, owner["access_token"], 201)
        rows = api("laravel", "GET", "/api/me/comments?page=1&per_page=100", token=owner["access_token"])["list"]
        matches = [row for row in rows if row["article_id"] == article_id and row["body"] == "CQ32留言-" + nonce]
        check(len(matches) == 1, "CQ32 新留言未唯一讀回")
        packet["comment_id"] = matches[0]["id"]
        packet["operation"] = "baseline-readback"
        save_packet(file, packet)
        packet["content"] = shared_content(article_id, packet["comment_id"])
        # 先確認本次公開寫入的內容與來源，再保存原始版本供重啟比較。
        first = packet["content"]["laravel"]
        check(first["article"]["id"] == article_id and first["article"]["title"] == title and
              first["article"]["body"] == body and first["article"]["author"]["id"] == owner["user"]["id"] and
              first["article"]["version"] == 1 and first["article"]["created_backend"] == "laravel" and
              first["comment"]["id"] == packet["comment_id"] and first["comment"]["body"] == "CQ32留言-" + nonce and
              first["comment"]["version"] == 1 and first["comment"]["created_backend"] == "laravel", "CQ32 新內容基準不符")
        packet["settings"] = sql("SELECT row_to_json(t) FROM login_lockout_settings t WHERE id=1")
        packet["database"] = database_state()
        check(packet["database"]["schema"] == packet.pop("schema_before"), "CQ32 寫入前後 schema 不符")
        packet["status"] = "complete"
        packet.pop("operation")
        save_packet(file, packet)


# check_restart 只用原 token 與原持久基準讀回；不可重新登入掩蓋期限或資料重設。
def check_restart(identity):
    info = PACKET.lstat()
    check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(), "CQ32 packet 身分／權限不符")
    packet = json.loads(PACKET.read_text())
    check(packet.get("format") == 1 and packet.get("status") == "complete" and
          all(packet["identity"][key] == identity[key] for key in ("keys", "images", "volume")) and
          packet["identity"]["source"]["application"] == identity["source"]["application"] and
          len(packet["members"]) == 12 and
          [item["backend"] for item in packet["members"]] == list(BACKENDS), "CQ32 baseline 未完成或來源已變")
    for item in packet["members"]:
        backend, login = item["backend"], item["login"]
        check(session(backend, login) == item["session"], "CQ32 session 身分／期限改變")
        check(api(backend, "GET", "/api/me", token=login["access_token"]) == login["user"], "CQ32 原登入失效或帳號改變")
    check(shared_content(packet["article_id"], packet["comment_id"]) == packet["content"], "CQ32 共同內容改變")
    check(sql("SELECT row_to_json(t) FROM login_lockout_settings t WHERE id=1") == packet["settings"] and
          database_state() == packet["database"], "CQ32 資料／設定／版本基準改變")


# ddl_process 固定新專案的 psql；只有已 guard 且持有本輪私有 pending packet 時可進入。
def ddl_process(statement=None, *, script=None):
    check(READY and DDL_ACTIVE, "CQ32 DDL 尚未 guard 或記錄 pending")
    args = ["docker", "compose", "-p", PROJECT, "-f", FILES[0], "-f", FILES[1], "--profile", "*",
            "exec", "-T", "db", "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose",
            "-U", "articles_owner", "-d", "articles"]
    if script is None:
        args.extend(["-c", statement])
    else:
        args.extend(["--file", "-"])
    return subprocess.run(args, input=script, cwd=ROOT, capture_output=True, text=True, timeout=60)


# ddl_execute 是唯一正常 DDL executor；非零（含 COMMIT 不明）不回傳 receipt，沒有重試或 guessed cleanup。
def ddl_execute(statement):
    result = ddl_process(statement)
    if result.returncode != 0:
        state = re.search(r"ERROR:\s+([0-9A-Z]{5}):", result.stderr)
        raise DDLFailure(state.group(1) if state else None)
    return result.stdout.strip()


# ddl_failure_chain 保存原失敗與 finally 失敗型別，既不吞錯也不列印未知例外內容。
def ddl_failure_chain(error):
    errors, seen = [], set()
    while error is not None and id(error) not in seen:
        errors.append({"type": type(error).__name__, "sqlstate": error.sqlstate if isinstance(error, DDLFailure) else None})
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return errors


# ddl_specs 只描述兩組已批准固定物件，沒有任意名稱、namespace、連線或 CLI target。
def ddl_specs(member_id, epoch, first_id):
    return (
        {"ticket": "57", "function": "ticket57_http_boundary_shift", "table": "login_failures",
         "trigger": "ticket57_http_boundary_shift", "catalog": ddl57.boundary_catalog,
         "catalog_sql": ddl57.boundary_catalog_sql(), "receipt": ddl57.boundary_receipt,
         "clear": ddl57.clear_boundary_trigger, "context": ddl57.boundary_trigger,
         "args": (member_id, epoch, first_id, 0), "statements": ddl57.boundary_create_sql(member_id, epoch, first_id, 0)},
        {"ticket": "39", "function": "ticket39_fail_epoch", "table": "accounts",
         "trigger": "ticket39_rollback_probe", "catalog": ddl39.rollback_catalog,
         "catalog_sql": ddl39.rollback_catalog_sql(), "receipt": ddl39.rollback_receipt,
         "clear": ddl39.clear_rollback_probe, "context": ddl39.rollback_trigger,
         "args": (member_id,), "statements": ddl39.rollback_create_sql(member_id)},
    )


# ddl_unknown_fixture 只由本輪 builder 自造無副作用本文與 nonce COMMENT，確認 COMMIT 才持有獨立 receipt。
def ddl_unknown_fixture(spec, nonce, paired):
    check(re.fullmatch(r"[0-9a-f]{16}", nonce) is not None, "CQ32 DDL nonce 不符")
    spec["clear"](execute=ddl_execute)
    statements = [f"CREATE FUNCTION public.{spec['function']}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
                  f"COMMENT ON FUNCTION public.{spec['function']}() IS 'cq32:{nonce}'"]
    if paired:
        statements.append(spec["statements"][1])
    raw = ddl_execute("BEGIN; " + "; ".join(statements) + "; " + spec["catalog_sql"] + "; COMMIT;")
    return spec["receipt"](json.loads(raw))


# ddl_refusal 只認定指定拒絕理由；工具／catalog 故障不能冒充安全負例成功。
def ddl_refusal(spec, record, *, owned=None):
    before = spec["catalog"](execute=ddl_execute)
    reason = (f"Ticket {spec['ticket']} DDL 已存在；保留現場，須人工處理" if owned is None else
              f"Ticket {spec['ticket']} 本輪 DDL 身分已變；拒絕清理")
    try:
        spec["clear"](owned, execute=ddl_execute)
    except AssertionError as error:
        check(str(error) == reason, "CQ32 DDL 未以預期資格拒絕")
    else:
        raise AssertionError("CQ32 未拒絕未知或已變更 DDL")
    after = spec["catalog"](execute=ddl_execute)
    check(before == after, "CQ32 拒絕後 catalog／owner／ACL／依賴／COMMENT 改變")
    record.update(before=before, after=after, refusal=reason)


# ddl_create_denial 將固定角色故障顯式送進同一 contextmanager，實際 CREATE 必回 42501 並同 session 回到 owner。
def ddl_create_denial(spec, stage, record):
    check(stage in (1, 2), "CQ32 CREATE 故障階段不符")
    expected = "BEGIN; " + "; ".join(spec["statements"]) + "; " + spec["catalog_sql"] + "; COMMIT;"

    # callback 只攔已知建立交易；其他 catalog 操作仍走原固定 executor，不改 module globals 或 guard。
    def execute(statement):
        if statement != expected:
            return ddl_execute(statement)
        first, second = spec["statements"]
        # 只在預期失敗的一句關閉 psql stop，記 SQLSTATE 後立即 rollback；不是 COMMIT 或 receipt。
        prefix = "BEGIN;\n" + (first + ";\n" if stage == 2 else "") + "SET LOCAL ROLE articles_app;\n"
        script = (prefix + "\\set ON_ERROR_STOP off\n" + (first if stage == 1 else second) + ";\n"
                  "\\echo CQ32_SQLSTATE :SQLSTATE\nROLLBACK;\n\\set ON_ERROR_STOP on\nSELECT current_user;\n")
        result = ddl_process(script=script)
        record["fault_stdout"], record["fault_stderr"] = result.stdout, result.stderr
        check(result.returncode == 0 and result.stdout.strip().splitlines() == ["CQ32_SQLSTATE 42501", "articles_owner"] and
              re.search(r"ERROR:\s+42501:", result.stderr) is not None, "CQ32 CREATE 未得到精確 42501／rollback owner")
        raise DDLFailure("42501")

    try:
        with spec["context"](*spec["args"], execute=execute):
            raise AssertionError("CQ32 被拒絕的 CREATE 不可進入案例")
    except DDLFailure as error:
        check(error.sqlstate == "42501" and "fault_stdout" in record, "CQ32 工具錯誤不能冒充 CREATE 拒絕")
    else:
        raise AssertionError("CQ32 CREATE 故障未拒絕")
    spec["clear"](execute=ddl_execute)


# ddl_probe 在獨立 O_EXCL packet 中驗兩組實際 lifecycle；不重寫舊 baseline、不新建帳號、不觸發 trigger。
def ddl_probe(identity):
    global DDL_ACTIVE
    check(READY, "CQ32 DDL probe 未 guard")
    fd = os.open(DDL_PACKET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    packet = {"format": 1, "status": "pending", "identity": identity, "cases": [], "trigger_executed": False}
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        save_packet(file, packet)
        DDL_ACTIVE = True
        try:
            info = PACKET.lstat()
            check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(), "CQ32 原 baseline 身分不符")
            baseline = json.loads(PACKET.read_text())
            check(baseline.get("format") == 1 and baseline.get("status") == "complete" and
                  [item["backend"] for item in baseline["members"]] == list(BACKENDS), "CQ32 缺少完整原 owned member packet")
            owner = baseline["members"][0]["login"]["user"]
            member_id = owner["id"]
            check(type(member_id) is int and 1 <= member_id <= 9007199254740991, "CQ32 owned member ID 不符")
            account = json.loads(ddl_execute(f"SELECT row_to_json(a) FROM (SELECT id,email,role,failure_epoch FROM public.accounts WHERE id={member_id}) a"))
            check(account["email"] == owner["email"] and account["role"] == "member", "CQ32 owned member 讀回不符")
            existing = ddl_execute(f"SELECT id FROM public.login_failures WHERE account_id={member_id} ORDER BY id LIMIT 1")
            # 沒有事件時 sentinel 只展開原本文；本階段絕不 INSERT/login，不能當 HTTP 邊界證據。
            first_id = int(existing) if existing else 1
            specs = ddl_specs(member_id, int(account["failure_epoch"]), first_id)
            packet.update(member_id=member_id, first_id_is_sentinel=not bool(existing), before=database_state())
            packet["initial_catalog"] = {spec["ticket"]: spec["catalog"](execute=ddl_execute) for spec in specs}
            save_packet(file, packet)
            for spec in specs:
                spec["clear"](execute=ddl_execute)
                packet["cases"].append({"case": spec["ticket"] + ":known-absent", "status": "pass"})
                save_packet(file, packet)
            # 不改 role/grant；先證明既有 owner 可 SET ROLE 且 app 缺 CREATE/TRIGGER，避免誤認其他故障。
            roles = ddl_execute("BEGIN READ ONLY; SET LOCAL ROLE articles_app; SELECT current_user; ROLLBACK; SELECT current_user;")
            check(roles.splitlines() == ["articles_app", "articles_owner"], "CQ32 SET ROLE 前提不符")
            privileges = json.loads(ddl_execute("SELECT json_build_object('create',has_schema_privilege('articles_app','public','CREATE'),"
                                               "'accounts',has_table_privilege('articles_app','public.accounts','TRIGGER'),"
                                               "'failures',has_table_privilege('articles_app','public.login_failures','TRIGGER'))"))
            check(privileges == {"create": False, "accounts": False, "failures": False}, "CQ32 權限拒絕前提不符")
            packet["role_preflight"] = {"roles": roles.splitlines(), "privileges": privileges}
            save_packet(file, packet)
            for spec in specs:
                # 同一 contextmanager 的正常及案例錯誤出口；finally 失敗不能被預期例外掩蓋。
                for fail in (False, True):
                    record = {"case": spec["ticket"] + (":error-finally" if fail else ":normal")}
                    packet["cases"].append(record)
                    save_packet(file, packet)
                    try:
                        with spec["context"](*spec["args"], execute=ddl_execute) as owned:
                            record["created"] = owned
                            save_packet(file, packet)
                            if fail:
                                raise DDLCaseFailure("CQ32 owned-case failure")
                    except DDLCaseFailure:
                        check(fail, "CQ32 非預期案例錯誤")
                    spec["clear"](execute=ddl_execute)
                    record["status"] = "pass"
                    save_packet(file, packet)
                # 未知 function-only/pair 不把 builder 的 receipt 交給被測 caller；同名不授歷史刪除資格。
                for paired in (False, True):
                    record = {"case": spec["ticket"] + (":unknown-pair" if paired else ":function-only")}
                    packet["cases"].append(record)
                    save_packet(file, packet)
                    owned = None
                    try:
                        owned = ddl_unknown_fixture(spec, secrets.token_hex(8), paired)
                        record["builder_receipt"] = owned
                        save_packet(file, packet)
                        ddl_refusal(spec, record)
                    finally:
                        if owned is not None:
                            spec["clear"](owned, execute=ddl_execute)
                    record["status"] = "pass"
                    save_packet(file, packet)
                # 只有本輪明示 COMMENT 成功才收最新 receipt；不能 ad-hoc 認領未知變動。
                record = {"case": spec["ticket"] + ":changed-comment"}
                packet["cases"].append(record)
                save_packet(file, packet)
                latest = None
                try:
                    latest = ddl_unknown_fixture(spec, secrets.token_hex(8), True)
                    stale = latest
                    record["before_change"] = stale
                    save_packet(file, packet)
                    # 變更提交不明時舊 receipt 不再取得刪除資格，維持 pending 現場。
                    latest = None
                    raw = ddl_execute(f"BEGIN; COMMENT ON FUNCTION public.{spec['function']}() IS 'cq32:{secrets.token_hex(8)}:changed'; " + spec["catalog_sql"] + "; COMMIT;")
                    latest = spec["receipt"](json.loads(raw))
                    record["latest_builder_receipt"] = latest
                    save_packet(file, packet)
                    ddl_refusal(spec, record, owned=stale)
                finally:
                    if latest is not None:
                        spec["clear"](latest, execute=ddl_execute)
                record["status"] = "pass"
                save_packet(file, packet)
                for stage in (1, 2):
                    record = {"case": spec["ticket"] + f":create-{stage}-42501"}
                    packet["cases"].append(record)
                    save_packet(file, packet)
                    ddl_create_denial(spec, stage, record)
                    record["status"] = "pass"
                    save_packet(file, packet)
            packet["after"] = database_state()
            check(packet["before"] == packet["after"], "CQ32 DDL phase 改變資料／schema／設定")
            packet["status"] = "complete"
            save_packet(file, packet)
        except BaseException as error:
            packet["failure"] = ddl_failure_chain(error)
            save_packet(file, packet)
            raise
        finally:
            DDL_ACTIVE = False


# settings_sources 只傳已由全服務 guard 比對的固定完整模組，manifest 不含 env／keys。
def settings_sources():
    check(READY, "CQ32 settings sources 未 guard")
    sources = {name: (ROOT / "ops/tests" / name).read_text(encoding="utf-8") for name in SETTINGS_MODULES}
    check(all(source == (REPO / "ops/tests" / name).read_text(encoding="utf-8") for name, source in sources.items()),
          "CQ32 settings 模組副本不符")
    return sources, {name: hashlib.sha256(source.encode()).hexdigest() for name, source in sources.items()}


# settings_child 固定 FastAPI runtime／project，payload 只走 stdin，不接受另一服務或任意程式。
def settings_child(payload):
    check(READY, "CQ32 settings child 未 guard")
    return subprocess.run(["docker", "compose", "-p", PROJECT, "-f", FILES[0], "-f", FILES[1], "--profile", "*",
                           "exec", "-T", "fastapi", "python", "-B", "-c", SETTINGS_BOOTSTRAP],
                          input=json.dumps(payload), cwd=ROOT, capture_output=True, text=True, timeout=360)


# settings_native_worker 在已核對的 FastAPI runtime 內跑共用 contexts；不是原完整 CLI／HTTP suite。
def settings_native_worker(payload):
    import importlib
    import psycopg
    import signal
    check(payload.get("project") == PROJECT and sys.version_info[:3] == (3, 13, 16) and
          psycopg.__version__ == "3.3.6", "CQ32 settings runtime 不符")
    modules = {number: importlib.import_module(f"ticket{number}_http") for number in (21, 24, 25, 28, 30, 31)}
    # 離線只驗完整模組 import／runtime，不碰 env、socket 或資料庫。
    if payload.get("check_only") is True:
        verified = {"status": "runtime-only", "runtime": sys.version.split()[0], "psycopg": psycopg.__version__,
                    "source_hashes": payload["source_hashes"]}
        # live 預檢另用真 owner 做唯讀連線；離線 package check 不帶 credential、不碰網路。
        if "owner_password" in payload:
            check(os.environ.get("DB_HOST") == "db" and os.environ.get("DB_DATABASE") == "articles" and
                  re.fullmatch(r"[0-9a-f]{64}", payload["owner_password"]) is not None, "CQ32 settings 預檢 DB 不符")
            with psycopg.connect(host="db", port=5432, dbname="articles", user="articles_owner",
                                 password=payload["owner_password"], connect_timeout=3,
                                 options="-c statement_timeout=20000 -c default_transaction_read_only=on") as connection:
                verified["database"] = connection.execute("SELECT current_database()||':'||current_user").fetchone()[0]
            check(verified["database"] == "articles:articles_owner", "CQ32 settings 預檢 DB 身分不符")
        return verified
    check(payload.get("check_only") is False and os.environ.get("DB_HOST") == "db" and
          os.environ.get("DB_DATABASE") == "articles" and os.environ.get("DB_USERNAME") == "articles_app",
          "CQ32 settings child DB 前提不符")
    check(type(payload["member_id"]) is int and payload["member_id"] > 0 and
          re.fullmatch(r"[0-9a-f]{64}", payload["owner_password"]) is not None and
          set(payload["tokens"]) == {"flask", "fastapi", "echo"}, "CQ32 settings phase 身分不符")
    opener = build_opener(ProxyHandler({}), NoRedirect)
    endpoint = "/api/settings/login-lockout"
    result = {"status": "pending", "cases": [], "source_hashes": payload["source_hashes"],
              "runtime": sys.version.split()[0], "psycopg": psycopg.__version__}

    # executor 使用固定 owner／私有 db；保留 psql 單值形狀，原 contexts 自己決定還原 SQL。
    def execute(statement):
        with psycopg.connect(host="db", port=5432, dbname="articles", user="articles_owner",
                             password=payload["owner_password"], connect_timeout=3, autocommit=True,
                             options="-c statement_timeout=20000") as connection:
            cursor = connection.execute(statement)
            if cursor.description is None:
                return ""
            rows = cursor.fetchall()
            check(len(rows) == 1 and len(rows[0]) == 1, "CQ32 settings SQL 非單值")
            value = rows[0][0]
            return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else "" if value is None else str(value)

    # 僅三個固定內網 settings 端點；500 嚴格驗原 Echo E9999，不把 success-only api 當 error transport。
    def http(backend, method, values=None, *, status=200):
        check(backend in ("flask", "fastapi", "echo") and method in ("GET", "PUT"), "CQ32 settings HTTP 目標不符")
        request = Request(f"http://{backend}:8000" + endpoint,
                          data=None if values is None else json.dumps(values).encode(), method=method,
                          headers={"Accept": "application/json", "Content-Type": "application/json",
                                   "Authorization": "Bearer " + payload["tokens"][backend]})
        try:
            response = opener.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            data = json.loads(response.read())
            check(response.status == status, "CQ32 settings HTTP status 不符")
            if status == 500:
                # 契約只要求非空追查字串；保留原值供 Main 對照同次 IN／EX／OUT，不另選 UUID 格式。
                check(set(data) == {"success", "message", "error_code", "log_id"} and data["success"] is False and
                      data["error_code"] == "E9999" and data["message"] == "系統暫時無法處理，請稍後再試" and
                      isinstance(data["log_id"], str) and bool(data["log_id"]),
                      "CQ32 settings 500 envelope 不符")
                return data["log_id"]
            else:
                check(set(data) == {"success", "message", "data"} and data["success"] is True and
                      data["message"] == "OK" and isinstance(data["data"], dict), "CQ32 settings envelope 不符")
                if method == "PUT":
                    check(data["data"] == {}, "CQ32 settings PUT data 不符")
                else:
                    values = data["data"]
                    check(set(values) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"} and
                          type(values["enabled"]) is bool and all(type(values[key]) is int for key in
                          ("window_minutes", "failure_threshold", "lock_seconds")), "CQ32 settings GET 四欄型態不符")
            return data.get("data")

    # fixture 建立前提只寫四欄；還原一律由既有 settings_restore context 負責。
    def stage(enabled, window=9, threshold=17, seconds=720):
        execute(f"UPDATE login_lockout_settings SET enabled={'true' if enabled else 'false'},window_minutes={window},failure_threshold={threshold},lock_seconds={seconds} WHERE id=1")

    # 單值讀回與穩定表 digest 檢查：不輸出密碼雜湊／session，只比較有無非預期變動。
    def settings():
        return json.loads(execute("SELECT json_build_object('enabled',enabled,'window_minutes',window_minutes,'failure_threshold',failure_threshold,'lock_seconds',lock_seconds) FROM login_lockout_settings WHERE id=1"))

    # 這六表在 child 不應新增／刪除；新 member／admin sessions 已由 host 先保存。
    def unchanged_rows():
        return {table: hashlib.sha256(execute(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM public.{table} t").encode()).hexdigest()
                for table in ("accounts", "login_sessions", "login_failures", "articles", "comments", "schema_migrations")}

    # 只讀 host 已持久記錄的 own member；MAX 失敗後須回到同一 epoch／lock。
    def member_state():
        return json.loads(execute(f"SELECT json_build_object('id',id,'role',role,'failure_epoch',failure_epoch,'locked_until',locked_until) FROM accounts WHERE id={payload['member_id']}"))

    # child 先以180秒中止案例並嘗試 finally；host360秒逾時仍屬未知，不能宣稱已收束。
    def deadline(signum, frame):
        raise TimeoutError("CQ32 settings native phase deadline")

    previous_handler = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(180)
    try:
        check(execute("SELECT current_database()||':'||current_user") == "articles:articles_owner", "CQ32 settings DB 身分不符")
        check(settings() == payload["original"] and member_state() == payload["member_before"], "CQ32 settings phase 前值已變")
        result["before"] = unchanged_rows()
        # 外層沿同一原 SQL context 收回 phase 四欄；不另寫影子還原 SQL 或回復全庫世代。
        with modules[25].settings_restore(execute=execute):
            for number, backend in ((21, "flask"), (24, "fastapi"), (30, "echo")):
                # callback 固定當輪 service，只回傳實際 payload，不接受 URL／任意 target。
                def request(method, values=None):
                    return http(backend, method, values)

                for enabled in (True, False):
                    for fail in (False, True):
                        stage(enabled)
                        original = settings()
                        record = {"case": f"{number}:http:{enabled}:{fail}", "before": original}
                        result["cases"].append(record)
                        args = (None, None) if number == 30 else (None,)
                        try:
                            with modules[number].settings_restore(*args, execute=request):
                                http(backend, "PUT", {"enabled": enabled, "window_minutes": 6, "failure_threshold": 7, "lock_seconds": 60})
                                if fail:
                                    raise SettingsCaseFailure("CQ32 settings committed numeric case")
                        except SettingsCaseFailure:
                            check(fail, "CQ32 settings 非預期案例錯誤")
                        check(settings() == original and http(backend, "GET") == original, "CQ32 HTTP context 未精確還原")
                        record["status"] = "pass"
            for number in (25, 28, 31):
                for enabled in (True, False):
                    for fail in (False, True):
                        stage(enabled)
                        original = settings()
                        record = {"case": f"{number}:sql:{enabled}:{fail}", "before": original}
                        result["cases"].append(record)
                        try:
                            with modules[number].settings_restore(execute=execute):
                                stage(not enabled, 6, 7, 60)
                                if fail:
                                    raise SettingsCaseFailure("CQ32 settings SQL case")
                        except SettingsCaseFailure:
                            check(fail, "CQ32 SQL 非預期案例錯誤")
                        check(settings() == original, "CQ32 SQL context 未精確還原")
                        record["status"] = "pass"
            # own member MAX 令真關閉交易 overflow；原 epoch finally 必精確還原，其他帳號／事件不變。
            stage(True, 5, 2, 600)
            changed, before = settings(), member_state()
            record = {"case": "30:epoch:500-rollback", "before": before}
            result["cases"].append(record)
            with modules[30].epoch_probe(payload["member_id"], execute=execute):
                record["log_id"] = http("echo", "PUT", changed | {"enabled": False}, status=500)
                during = member_state()
                check(settings() == changed and during["failure_epoch"] == 9223372036854775807 and
                      during["locked_until"] == before["locked_until"], "CQ32 500 未完整 rollback")
            check(member_state() == before, "CQ32 own epoch／lock 未精確還原")
            record["status"] = "pass"
        result["after"] = unchanged_rows()
        check(settings() == payload["original"] and result["after"] == result["before"] and len(result["cases"]) == 25,
              "CQ32 settings phase 資料／四欄未恢復")
        result["status"] = "complete"
    except BaseException as error:
        result["failure"] = ddl_failure_chain(error)
    finally:
        # 還原 handler，避免嵌入同一離線程序時影響其他測試；現場錯誤仍保留 pending。
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
    return result


# settings_probe 先私有落盤再登入／註冊；只啟用已 guard 的固定 FastAPI child，不提供任意 runtime／目標。
def settings_probe(identity):
    check(READY, "CQ32 settings probe 未 guard")
    fd = os.open(SETTINGS_PACKET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    packet = {"format": 1, "status": "pending", "identity": identity, "operation": "preflight"}
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        save_packet(file, packet)
        try:
            sources, hashes = settings_sources()
            packet["source_hashes"] = hashes
            packet["original"] = json.loads(sql("SELECT json_build_object('enabled',enabled,'window_minutes',window_minutes,'failure_threshold',failure_threshold,'lock_seconds',lock_seconds) FROM login_lockout_settings WHERE id=1"))
            packet["operation"] = "native-runtime-preflight"
            save_packet(file, packet)
            # owner secret 只在記憶體與 stdin；native 唯讀驗真憑證後才允許首次 API 寫入。
            environment = ROOT / ".env"
            info = environment.lstat()
            check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(), "CQ32 owner env 身分不符")
            owner = [line.partition("=")[2] for line in environment.read_text().splitlines() if line.startswith("DB_OWNER_PASSWORD=")]
            check(len(owner) == 1 and re.fullmatch(r"[0-9a-f]{64}", owner[0]) is not None, "CQ32 owner credential 格式不符")
            preflight = settings_child({"project": PROJECT, "check_only": True, "sources": sources, "source_hashes": hashes, "owner_password": owner[0]})
            packet["runtime_stdout"], packet["runtime_stderr"] = preflight.stdout, preflight.stderr
            save_packet(file, packet)
            runtime = json.loads(preflight.stdout)
            check(preflight.returncode == 0 and runtime.get("status") == "runtime-only" and
                  runtime.get("source_hashes") == hashes and runtime.get("database") == "articles:articles_owner", "CQ32 settings runtime 預檢失敗")
            # 所有必要憑證與 native runtime 在首次登入／註冊前確認，不因缺套件先建立半套 fixture。
            credentials_path = ROOT / ".local/cq32-admin.json"
            info = credentials_path.lstat()
            check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(), "CQ32 admin packet 身分不符")
            credentials = json.loads(credentials_path.read_text())
            check(set(credentials) == {"email", "nickname", "password"} and all(isinstance(value, str) and value for value in credentials.values()), "CQ32 admin packet 欄位不符")
            tokens = {}
            for backend in ("flask", "fastapi", "echo"):
                packet["operation"] = backend + ":admin-login"
                save_packet(file, packet)
                login = api(backend, "POST", "/api/auth/login", {"email": credentials["email"], "password": credentials["password"]})
                packet.setdefault("admin_logins", {})[backend] = login
                save_packet(file, packet)
                check(login["user"]["role"] == "admin" and login["user"]["email"] == credentials["email"], "CQ32 settings admin 身分不符")
                session(backend, login)
                tokens[backend] = login["access_token"]
            nonce = secrets.token_hex(8)
            email = f"cq32-settings-{nonce}@example.test"
            packet.update(operation="echo:member-register", member_email=email)
            save_packet(file, packet)
            api("echo", "POST", "/api/auth/register", {"email": email, "password": "Cq321234", "nickname": "CQ32世代probe"}, status=201)
            packet["operation"] = "echo:member-login"
            save_packet(file, packet)
            login = api("echo", "POST", "/api/auth/login", {"email": email, "password": "Cq321234"})
            packet["member_login"] = login
            save_packet(file, packet)
            member_id = login["user"]["id"]
            check(type(member_id) is int and member_id > 0 and login["user"]["role"] == "member" and login["user"]["email"] == email,
                  "CQ32 settings own member 身分不符")
            session("echo", login)
            packet["member_before"] = json.loads(sql(f"SELECT json_build_object('id',id,'role',role,'failure_epoch',failure_epoch,'locked_until',locked_until) FROM accounts WHERE id={member_id}"))
            packet["operation"] = "fixed-fastapi-native-contexts"
            save_packet(file, packet)
            # owner secret 只在記憶體與 stdin；不進 argv、stdout 或 public evidence。
            payload = {"project": PROJECT, "check_only": False, "sources": sources, "source_hashes": hashes,
                       "original": packet["original"], "member_before": packet["member_before"], "member_id": member_id,
                       "tokens": tokens, "owner_password": owner[0]}
            child = settings_child(payload)
            packet["child_exit"] = child.returncode
            packet["child_stdout"], packet["child_stderr"] = child.stdout, child.stderr
            save_packet(file, packet)
            packet["result"] = json.loads(child.stdout)
            save_packet(file, packet)
            check(child.returncode == 0 and packet["result"].get("status") == "complete" and
                  packet["result"].get("source_hashes") == hashes and len(packet["result"].get("cases", [])) == 25 and
                  all(case.get("status") == "pass" for case in packet["result"]["cases"]), "CQ32 native settings probe 未完整通過")
            current = json.loads(sql("SELECT json_build_object('enabled',enabled,'window_minutes',window_minutes,'failure_threshold',failure_threshold,'lock_seconds',lock_seconds) FROM login_lockout_settings WHERE id=1"))
            check(current == packet["original"], "CQ32 settings host 四欄讀回不符")
            packet["status"] = "complete"
            save_packet(file, packet)
        except BaseException as error:
            # host timeout 不代表 child 已停；原始部分輸出僅存私有 packet，禁止猜測重送／清理。
            if isinstance(error, subprocess.TimeoutExpired):
                packet["child_stdout"] = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout
                packet["child_stderr"] = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
            packet["failure"] = ddl_failure_chain(error)
            save_packet(file, packet)
            raise


# main 只允許五個固定階段；成功訊息表示該階段完成，不代表原票或完整矩陣驗收。
def main():
    check(sys.argv[1:] in (["--guard"], ["--seed"], ["--check-restart"], ["--ddl-probe"], ["--settings-probe"]), "CQ32 用法：--guard|--seed|--check-restart|--ddl-probe|--settings-probe")
    identity = guard()
    if sys.argv[1] == "--seed":
        seed(identity)
    elif sys.argv[1] == "--check-restart":
        check_restart(identity)
    elif sys.argv[1] == "--ddl-probe":
        ddl_probe(identity)
    elif sys.argv[1] == "--settings-probe":
        settings_probe(identity)
    print("PASS CQ32 " + sys.argv[1])


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        # check 的理由均為固定文字，可指出失敗邊界而不列出回應或秘密。
        print("FAIL CQ32 " + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        # 工具／JSON 例外可能包含秘密，只輸出固定型別；詳細現場保留於私有 packet。
        print("FAIL CQ32 " + " <- ".join(item["type"] for item in ddl_failure_chain(error)), file=sys.stderr)
        sys.exit(1)
