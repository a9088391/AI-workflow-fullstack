"""CQ33 固定隔離環境的單次 seed 與唯讀搜尋驗收；啟停／建置由 Main 負責。

從原工作區執行此檔，cwd 必須是 .local/cq33-fixture，並明確設定
COMPOSE_PROJECT_NAME=cq33_test、COMPOSE_FILE=compose.yaml:.local/cq33-compose.yaml。
pending packet 代表結果尚未完全確認，禁止重送；本腳本沒有恢復或清理命令。
"""

import base64
from collections import deque
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote_to_bytes
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / ".local/cq33-fixture"
PROJECT = "cq33_test"
FILES = ("compose.yaml", ".local/cq33-compose.yaml")
BACKENDS = ("laravel", "symfony", "aspnet-core", "django", "flask", "fastapi",
            "gin", "echo", "fiber", "express", "fastify", "nestjs")
FRONTENDS = ("vue", "nuxt", "react", "next", "angular", "sveltekit")
BASES = {name: f"http://127.0.0.1:{8081 + index}" for index, name in enumerate(BACKENDS)}
PACKET = ROOT / ".local/cq33-packet.json"
ADMIN = ROOT / ".local/cq33-admin.json"
NICKNAMES = (r"字面\u0000", "CQ33A%_\\字", "CQ33A基準一", "CQ33A基準二", "Ä符號", "ä符號",
             "前é後", "前e\u0301後", "前\t後", "前\u00a0後", "中文", "不相符")
TABLES = ("accounts", "articles", "comments", "login_sessions", "login_failures",
          "login_lockout_settings", "schema_migrations")
MESSAGES = {"E0001": "欄位資料不正確，請確認後再送出", "E0002": "登入已逾期，請重新登入",
            "E0003": "沒有權限使用此功能"}
INVALID, REQUIRED, TYPE = "欄位格式或範圍不正確", "此欄位為必填", "欄位型態不正確"
READY = False
WRITING = False
# SQL 僅有固定 SELECT；快照在 DB 內雜湊，密碼雜湊與整列 session 不輸出到主機。
QUERIES = {
    "migrations": "SELECT row_to_json(t) FROM schema_migrations t ORDER BY version",
    "tables": "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name",
    "accounts": "SELECT row_to_json(t) FROM (SELECT id,email,nickname,role,status,"
                "to_char(created_at AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD HH24:MI:SS') AS created_at,"
                "extract(epoch FROM created_at)::text AS order_epoch FROM accounts) t",
    "sessions": "SELECT row_to_json(t) FROM (SELECT jti,account_id,backend_id,"
                "extract(epoch FROM issued_at)::text AS issued_epoch,"
                "extract(epoch FROM expires_at)::text AS expires_epoch,revoked_at FROM login_sessions) t",
}
for _table in TABLES:
    QUERIES["snapshot:" + _table] = (
        "SELECT json_build_object('count',count(*),'sha256',encode(sha256(convert_to("
        "coalesce(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),''),'UTF8')),'hex')) "
        f"FROM {_table} t"
    )
# 空表的結構、索引與授權也納入基準，不能只憑 migration 列或資料列 hash 推定 schema 未變。
_catalogs = (("information_schema.tables", "table_schema"), ("information_schema.columns", "table_schema"),
             ("information_schema.table_constraints", "table_schema"), ("information_schema.key_column_usage", "table_schema"),
             ("information_schema.referential_constraints", "constraint_schema"), ("information_schema.check_constraints", "constraint_schema"),
             ("information_schema.role_table_grants", "table_schema"), ("information_schema.routines", "routine_schema"),
             ("information_schema.triggers", "event_object_schema"), ("pg_catalog.pg_indexes", "schemaname"),
             ("pg_catalog.pg_namespace", "nspname"))
QUERIES["schema"] = (
    "SELECT encode(sha256(convert_to(coalesce(string_agg(kind||':'||value::text,E'\\n' ORDER BY kind,value::text),''),'UTF8')),'hex') FROM ("
    + " UNION ALL ".join(f"SELECT '{view}' AS kind,to_jsonb(t) AS value FROM {view} t WHERE {field}='public'" for view, field in _catalogs)
    + ") c"
)


# 受控失敗只攜帶固定原因，不讓工具輸出、HTTP Body 或憑證進入錯誤報告。
class Failure(RuntimeError):
    pass


# 明確斷言在 python -O 仍有效，避免驗收與目的地保護被停用。
def require(condition, reason):
    if not condition:
        raise Failure(reason)


# 不跟隨 redirect，避免固定 API 的 token 或 POST 被轉送其他位置。
class NoRedirect(HTTPRedirectHandler):
    # 讓 urllib 回傳原 3xx，交由共同回應斷言判為失敗。
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect)


# 固定工具在副本 cwd 執行；輸出只留記憶體，失敗不回顯可能含秘密的內容。
def command(*args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0, "固定工具執行失敗")
    return result.stdout.strip()


# 所有 Compose 操作都使用同一固定 project、檔案與 profile，不接受任意目標參數。
def compose(*args):
    return command("docker", "compose", "-p", PROJECT, "-f", FILES[0], "-f", FILES[1], "--profile", "*", *args)


# 私有檔案必須由目前使用者持有、0600 且非 symlink，不能誤讀其他工作的憑證。
def private_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                stat.S_IMODE(info.st_mode) == 0o600, "私有檔案 owner／mode 不符")
        return source.read()


# 敏感 JSON 只從通過檔案權限檢查的固定路徑取得，不提供環境變數或 URL 替代入口。
def private_json(path):
    return json.loads(private_bytes(path))


# 驗證合成帳密形狀，再序列化到 API；不 trim、修復或自動重試不合法憑證。
def credentials(value):
    require(isinstance(value, dict) and set(value) == {"email", "nickname", "password"}, "帳密欄位不符")
    require(all(isinstance(item, str) for item in value.values()), "帳密型態不符")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]*@example\.test", value["email"]) is not None and
            len(value["email"]) <= 254 and re.fullmatch(r"[A-Za-z0-9]{4,16}", value["password"]) is not None and
            1 <= len(value["nickname"]) <= 30 and "\0" not in value["nickname"], "帳密格式不符")
    return value


# 副本逐檔對原 tracked 白名單與本次兩個新檔；NGINX 唯一以 HEAD blob 保留使用者修改。
def source_identity():
    names = command("git", "-C", str(REPO), "ls-files", "-z", "--cached", "--",
                    ".dockerignore", ".env.example", "compose.yaml", "backends", "frontend", "ops", "database").split("\0")
    names += ["ops/tests/cq33_http.py", "ops/tests/cq33_compose.yaml"]
    ignored = {".local", ".git", ".scratch", "docs", "node_modules", "vendor", "dist", "__pycache__"}
    digest, application, count = hashlib.sha256(), hashlib.sha256(), 0
    for name in sorted(set(names) - {""}):
        relative = Path(name)
        if any(part in ignored for part in relative.parts) or relative.name == ".env":
            continue
        original, copied = REPO / relative, ROOT / relative
        require(original.is_file() and copied.is_file() and original.resolve() == original and
                copied.resolve() == copied, "來源缺檔或含 symlink")
        expected = subprocess.check_output(["git", "-C", str(REPO), "show", "HEAD:ops/nginx/default.conf"]) if name == "ops/nginx/default.conf" else original.read_bytes()
        mode = stat.S_IMODE(copied.stat().st_mode)
        require(copied.read_bytes() == expected and mode == stat.S_IMODE(original.stat().st_mode), "來源副本與候選不符")
        record = name.encode() + b"\0" + str(mode).encode() + b"\0" + expected
        digest.update(record)
        if relative.parts[:2] != ("ops", "tests"):
            application.update(record)
        count += 1
    require(count > 0 and all((ROOT / f"backends/{name}/Dockerfile").is_file() for name in BACKENDS), "必要來源不完整")
    return {"all": digest.hexdigest(), "application": application.hexdigest(), "files": count}


# 實際容器必須唯一、健康，並與固定 Compose cwd/config、網路及目前 image 相符。
def inspect_service(service):
    ids = compose("ps", "-q", service).splitlines()
    require(len(ids) == 1, "服務未唯一啟動：" + service)
    item = json.loads(command("docker", "inspect", ids[0]))[0]
    labels = item["Config"]["Labels"]
    require(labels.get("com.docker.compose.project") == PROJECT and
            labels.get("com.docker.compose.service") == service and
            labels.get("com.docker.compose.project.working_dir") == str(ROOT) and
            labels.get("com.docker.compose.project.config_files", "").split(",") == [str(ROOT / file) for file in FILES], "容器來源不符：" + service)
    require(item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy" and
            set(item["NetworkSettings"]["Networks"]) == {PROJECT + "_default"}, "容器健康／網路不符：" + service)
    image = compose("images", "-q", service)
    require(image and item["Image"].removeprefix("sha256:") == image.removeprefix("sha256:"), "執行映像不符：" + service)
    # 任何 bind 都只能來自此副本，避免額外掛入原展示資料或秘密。
    require(all(mount.get("Type") != "bind" or Path(mount["Source"]).resolve().is_relative_to(ROOT)
                for mount in item["Mounts"]), "容器掛入副本外來源：" + service)
    return item


# 全部目的地條件成立後才開啟 API／SQL；guard 本身不寫 packet、不建立服務或資料。
def guard():
    global READY
    READY = False
    require(Path.cwd().resolve() == ROOT and ROOT.resolve() == ROOT and ROOT.is_dir(), "cwd 不是固定 CQ33 副本")
    local = ROOT / ".local"
    require(local.is_dir() and local.resolve() == local and local.stat().st_uid == os.getuid() and
            not local.stat().st_mode & 0o022, "私有目錄 owner／路徑不符")
    require(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT and os.environ.get("COMPOSE_FILE") == ":".join(FILES), "Compose 環境不符")
    require((ROOT / FILES[1]).resolve() == ROOT / FILES[1] and
            (ROOT / FILES[1]).read_bytes() == (REPO / "ops/tests/cq33_compose.yaml").read_bytes(), "固定 Compose template 不符")
    source = source_identity()
    config = json.loads(compose("config", "--format", "json"))
    services = {"db", "nginx", *BACKENDS, *("frontend-" + name for name in FRONTENDS)}
    require(set(config["services"]) == services | {"sql"} and config["name"] == PROJECT and
            config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data" and
            config["services"]["db"]["environment"].get("CQ33_FIXTURE") == "isolated-cq33" and
            config["networks"]["default"]["ipam"]["config"] == [{"subnet": "10.251.133.0/24"}], "固定 Compose 設定不符")
    require(set(compose("ps", "--services", "--status", "running").splitlines()) == services, "執行服務集合不符")
    network = json.loads(command("docker", "network", "inspect", PROJECT + "_default"))[0]
    require(network["Name"] == PROJECT + "_default" and network["Labels"].get("com.docker.compose.project") == PROJECT and
            network["IPAM"]["Config"] == [{"Subnet": "10.251.133.0/24", "Gateway": "10.251.133.1"}], "實際網段不符")
    volume = json.loads(command("docker", "volume", "inspect", PROJECT + "_postgres_data"))[0]
    require(volume["Name"] == PROJECT + "_postgres_data" and volume["Labels"].get("com.docker.compose.project") == PROJECT and
            volume["Labels"].get("com.docker.compose.volume") == "postgres_data" and not volume.get("Options"), "實際資料卷不符")
    database = inspect_service("db")
    require("CQ33_FIXTURE=isolated-cq33" in database["Config"]["Env"] and not database["HostConfig"]["PortBindings"] and
            len(database["Mounts"]) == 1 and database["Mounts"][0].get("Name") == PROJECT + "_postgres_data" and
            database["Mounts"][0].get("Destination") == "/var/lib/postgresql", "DB 資料卷／私有性不符")
    keys, images = {}, {"db": database["Image"]}
    defaults = {hashlib.sha256((root / f".local/keys/{backend}.key").read_bytes().strip()).hexdigest()
                for root in (ROOT, REPO) for backend in BACKENDS}
    for index, backend in enumerate(BACKENDS):
        item = inspect_service(backend)
        internal = "8080/tcp" if backend == "aspnet-core" else "8000/tcp"
        require(item["HostConfig"]["PortBindings"] == {internal: [{"HostIp": "127.0.0.1", "HostPort": str(8081 + index)}]} and
                {"DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app", "DB_PASSWORD=" + config["services"][backend]["environment"]["DB_PASSWORD"]}.issubset(item["Config"]["Env"]), "API 埠／DB 不符：" + backend)
        path = ROOT / f".local/cq33-keys/{backend}.key"
        require(path.resolve() == path, "測試金鑰不可含 symlink")
        encoded = private_bytes(path).rstrip(b"\n")
        require(len(base64.b64decode(encoded, validate=True)) == 32 and
                base64.b64encode(base64.b64decode(encoded, validate=True)) == encoded and
                hashlib.sha256(encoded).hexdigest() not in defaults, "測試金鑰隔離不符")
        require(any(mount.get("Type") == "bind" and mount.get("Source") == str(path) and
                    mount.get("Destination") == f"/run/secrets/{backend}-jwt.key" and mount.get("RW") is False
                    for mount in item["Mounts"]), "測試金鑰未唯讀掛入：" + backend)
        require(compose("exec", "-T", backend, "cat", f"/run/secrets/{backend}-jwt.key").encode() == encoded, "容器金鑰與來源不符")
        keys[backend], images[backend] = hashlib.sha256(encoded).hexdigest(), item["Image"]
    require(len(set(keys.values())) == 12, "十二後端不可共用金鑰")
    for frontend in FRONTENDS:
        service = "frontend-" + frontend
        item = inspect_service(service)
        require(not item["HostConfig"]["PortBindings"], "前端不可直接發布 host port")
        images[service] = item["Image"]
    nginx = inspect_service("nginx")
    require(nginx["HostConfig"]["PortBindings"] == {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}, "NGINX 埠不符")
    images["nginx"] = nginx["Image"]
    for backend, base in BASES.items():
        with OPENER.open(base + "/up", timeout=10) as response:
            expected = b'{"status":"ok"}' if backend in ("symfony", "aspnet-core") else b"OK"
            require(response.status == 200 and response.read() == expected, "API HTTP health 不符：" + backend)
    for frontend in FRONTENDS:
        with OPENER.open("http://127.0.0.1:8080/" + frontend + "/", timeout=10) as response:
            require(response.status == 200, "前端入口未就緒：" + frontend)
    identity = compose("exec", "-T", "db", "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c", "SELECT current_database()||':'||current_user")
    require(identity == "articles:articles_owner", "DB 身分不符")
    READY = True
    return {"project": PROJECT, "root": str(ROOT), "volume": PROJECT + "_postgres_data",
            "network_id": network["Id"], "volume_created": volume["CreatedAt"], "source": source, "images": images, "keys": keys}


# SQL 名稱只能選固定唯讀查詢，且交易強制 READ ONLY，不能傳入任意 SQL 或目標。
def sql(name):
    require(READY and name in QUERIES, "SQL 未 guard 或非固定查詢")
    return compose("exec", "-T", "db", "psql", "-XqAt", "-1", "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles",
                   "-c", "SET TRANSACTION READ ONLY", "-c", QUERIES[name])


# 真實版本表、表集合與八支集中 SQL checksum 相符，才接受資料快照作為驗收前提。
def validate_schema():
    require(sql("tables").splitlines() == sorted(TABLES), "資料表集合不符")
    expected = {path.stem: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT / "database/sql").glob("[0-9][0-9][0-9][0-9]_*.sql")}
    migrations = [json.loads(line) for line in sql("migrations").splitlines()]
    require(len(expected) == 8 and {row["version"]: row["checksum"].strip() for row in migrations} == expected, "八版 migration checksum 不符")


# 七表一次批次前後各留 count／SHA-256；hash 包含完整列，公開報告不含敏感列值。
def database_state():
    result = {table: json.loads(sql("snapshot:" + table)) for table in TABLES}
    result["schema"] = sql("schema")
    return result


# 原始碼點先本機驗證，再轉百分比編碼，避免把文字 escape 誤當真正 NUL。
def encoded_keyword(value):
    encoded = quote(value, safe="")
    require(unquote_to_bytes(encoded) == value.encode("utf-8"), "關鍵字編碼不符")
    return encoded


# API 限固定三條路徑；POST 只在 seed 旗標與 pending packet 已落盤後允許，永不重試。
def request(backend, method, path, body=None, token=None):
    endpoint = path.split("?", 1)[0]
    require(READY and backend in BACKENDS and
            ((method == "GET" and endpoint in {"/api/members", "/api/me"}) or
             (WRITING and method == "POST" and endpoint in {"/api/auth/register", "/api/auth/login"})), "API 未 guard 或操作超出範圍")
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    require(data is None or json.loads(data) == body, "序列化未保留原始輸入碼點")
    item = Request(BASES[backend] + path, data=data, headers=headers, method=method)
    try:
        response = OPENER.open(item, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


# 成功與錯誤 envelope 逐欄核對，錯誤只忽略每請求獨立的 log_id 並另驗 UUID 格式。
def expect(result, status, code=None, fields=None):
    actual, payload = result
    require(actual == status, "HTTP 狀態不符")
    require(isinstance(payload, dict), "回應不是 JSON object")
    if code is None:
        require(set(payload) == {"success", "message", "data"} and payload["success"] is True and
                payload["message"] == "OK" and isinstance(payload["data"], dict), "成功 envelope 不符")
        return payload["data"]
    keys = {"success", "message", "error_code", "log_id"} | ({"errors"} if fields is not None else set())
    require(set(payload) == keys and payload["success"] is False and payload["error_code"] == code and
            payload["message"] == MESSAGES[code] and isinstance(payload["log_id"], str) and
            str(uuid.UUID(payload["log_id"])) == payload["log_id"], "錯誤 envelope 不符")
    require(fields is None or payload["errors"] == fields, "錯誤欄位或文案不符")
    return payload["log_id"]


# 初次 O_EXCL 建立 pending；後續以同目錄 0600 暫存檔原子替換，斷線也不丟失已確認 receipt。
def save_packet(packet, initial=False):
    data = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    if initial:
        fd = os.open(PACKET, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    else:
        private_bytes(PACKET)
        temporary = PACKET.with_name(".cq33-packet-" + secrets.token_hex(8) + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, PACKET)
    directory = os.open(PACKET.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# 以 DB 全部帳號核對 seed 身分，排序另採完整 epoch 與 ID，避免展示秒數造成同秒順序誤判。
def accounts():
    rows = [json.loads(line) for line in sql("accounts").splitlines()]
    for row in rows:
        require(type(row["id"]) is int and 1 <= row["id"] <= 9007199254740991, "帳號 ID 型態／範圍不符")
    return sorted(rows, key=account_order, reverse=True)


# DB 的完整時間精度與 ID 共同決定列表排序，不從其他後端的結果反推預期值。
def account_order(row):
    return Decimal(row["order_epoch"]), row["id"]


# 只取契約允許的公開帳號欄位，讓列表與登入回應都檢查不多洩漏欄位。
def resource(row):
    return {name: row[name] for name in ("id", "email", "nickname", "role", "status", "created_at")}


# 登入 receipt 必須對到同後端的持久 session 與原八小時期限，不重新登入掩蓋過期。
def validate_login(backend, login, account, sessions):
    require(set(login) == {"access_token", "token_type", "expires_at", "user"} and
            login["token_type"] == "Bearer" and login["user"] == resource(account) and
            isinstance(login["access_token"], str), "登入回應身分／欄位不符")
    parts = login["access_token"].split(".")
    require(len(parts) == 3, "JWT 形狀不符")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "===").decode())
    require(isinstance(claims.get("jti"), str) and re.fullmatch(r"[A-Za-z0-9_-]{21}[AQgw]", claims["jti"]) is not None and
            type(claims.get("iat")) is int and type(claims.get("exp")) is int and
            claims["exp"] - claims["iat"] == 28800 and claims["exp"] > time.time() and
            claims.get("sub") == str(account["id"]), "JWT 身分／期限不符")
    matches = [row for row in sessions if row["jti"] == claims["jti"]]
    require(len(matches) == 1, "session 未唯一讀回")
    row = matches[0]
    expiry = datetime.strptime(login["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Taipei"))
    require(row["account_id"] == account["id"] and row["backend_id"] == backend and row["revoked_at"] is None and
            Decimal(row["issued_epoch"]) == claims["iat"] and Decimal(row["expires_epoch"]) == claims["exp"] and
            expiry.timestamp() == claims["exp"], "持久 session／原登入期限不符")
    return claims["jti"]


# 單次建立十二會員與二十四 session；任何既存 packet（含 pending）都必須人工釐清，不能續跑。
def seed(identity):
    global WRITING
    require(READY, "seed 未 guard")
    admin = credentials(private_json(ADMIN))
    nonce = secrets.token_hex(8)
    packet = {"format": 1, "status": "pending", "operation": "preconditions", "identity": identity,
              "nonce": nonce, "admin": admin, "members": [], "logins": {name: {} for name in BACKENDS}}
    save_packet(packet, initial=True)
    validate_schema()
    before = database_state()
    require(all(before[table]["count"] == 0 for table in ("articles", "comments", "login_sessions", "login_failures")) and
            before["accounts"]["count"] == 1 and before["login_lockout_settings"]["count"] == 1, "新 fixture 不為單一管理員空資料集")
    rows = accounts()
    require(len(rows) == 1 and rows[0]["role"] == "admin" and rows[0]["status"] == "active" and
            rows[0]["email"] == admin["email"] and rows[0]["nickname"] == admin["nickname"], "管理員初始化身分不符")
    packet["admin_id"] = rows[0]["id"]
    save_packet(packet)
    WRITING = True
    try:
        for backend, nickname in zip(BACKENDS, NICKNAMES, strict=True):
            member = credentials({"email": f"cq33-{nonce}-{backend}@example.test", "nickname": nickname, "password": secrets.token_hex(8)})
            packet["operation"] = backend + ":register"
            save_packet(packet)
            require(expect(request(backend, "POST", "/api/auth/register", member), 201) == {}, "註冊 data 不為空")
            packet["members"].append({"backend": backend, **member})
            save_packet(packet)
            matches = [row for row in accounts() if row["email"] == member["email"]]
            require(len(matches) == 1 and matches[0]["nickname"] == nickname and matches[0]["role"] == "member" and
                    matches[0]["status"] == "active", "註冊帳號未精確讀回")
            packet["members"][-1]["id"] = matches[0]["id"]
            save_packet(packet)
        # 同一管理員與第一會員各在十二後端登入；不為每個測試再產生 session。
        for backend in BACKENDS:
            for role, actor in (("admin", admin), ("member", packet["members"][0])):
                packet["operation"] = backend + ":login:" + role
                save_packet(packet)
                login = expect(request(backend, "POST", "/api/auth/login", {"email": actor["email"], "password": actor["password"]}), 200)
                packet["logins"][backend][role] = login
                save_packet(packet)
                account = next(row for row in accounts() if row["email"] == actor["email"])
                sessions = [json.loads(line) for line in sql("sessions").splitlines()]
                validate_login(backend, login, account, sessions)
                require(expect(request(backend, "GET", "/api/me", token=login["access_token"]), 200) == resource(account), "登入後本人讀回不符")
        after = database_state()
        require(after["accounts"]["count"] == 13 and after["login_sessions"]["count"] == 24 and
                all(after[table] == before[table] for table in ("articles", "comments", "login_failures", "login_lockout_settings", "schema_migrations", "schema")), "seed 數量或未授權副作用不符")
        packet["database"] = after
        packet["status"] = "complete"
        packet.pop("operation")
        save_packet(packet)
    finally:
        WRITING = False
    return {"status": "complete", "members": 12, "sessions": 24, "identity": identity, "database": after}


# 完整 packet 核對固定資源與資料集；允許候選程式修正／重建，但不能更換 keys、DB 或 SQL 基準。
def validate_packet(packet, identity):
    require(isinstance(packet, dict) and set(packet) == {"format", "status", "identity", "nonce", "admin", "admin_id", "members", "logins", "database"} and
            packet["format"] == 1 and packet["status"] == "complete", "packet 非完整 schema；禁止自動恢復或重送")
    require(re.fullmatch(r"[0-9a-f]{16}", packet["nonce"]) is not None, "packet nonce 不符")
    # 允許來源更新，但公開輸出的 seed 證據只能是既定 hash／image ID，不能帶入任意私有欄位。
    saved = packet["identity"]
    require(isinstance(saved, dict) and set(saved) == set(identity) and
            isinstance(saved["source"], dict) and set(saved["source"]) == {"all", "application", "files"} and
            all(isinstance(saved["source"][name], str) and re.fullmatch(r"[0-9a-f]{64}", saved["source"][name]) is not None for name in ("all", "application")) and
            type(saved["source"]["files"]) is int and saved["source"]["files"] > 0 and
            isinstance(saved["images"], dict) and set(saved["images"]) == set(identity["images"]) and
            all(isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None for value in saved["images"].values()), "packet 來源證據形狀不符")
    for name in ("project", "root", "volume", "network_id", "volume_created", "keys"):
        require(packet["identity"][name] == identity[name], "packet 與固定資源身分不符")
    credentials(packet["admin"])
    require(len(packet["members"]) == 12 and set(packet["logins"]) == set(BACKENDS), "packet 帳號／登入數量不符")
    rows = accounts()
    require(len(rows) == 13, "資料集帳號總數不符")
    by_id = {row["id"]: row for row in rows}
    require(len(by_id) == 13 and type(packet["admin_id"]) is int and packet["admin_id"] in by_id, "管理員 ID 不符")
    admin = by_id[packet["admin_id"]]
    require(admin["email"] == packet["admin"]["email"] and admin["nickname"] == packet["admin"]["nickname"] and admin["role"] == "admin" and admin["status"] == "active", "管理員資料集不符")
    ids = []
    for backend, nickname, member in zip(BACKENDS, NICKNAMES, packet["members"], strict=True):
        require(set(member) == {"backend", "id", "email", "nickname", "password"} and member["backend"] == backend and
                type(member["id"]) is int and member["id"] in by_id and member["email"] == f"cq33-{packet['nonce']}-{backend}@example.test" and
                member["nickname"] == nickname, "會員 packet 身分不符")
        credentials({name: member[name] for name in ("email", "nickname", "password")})
        row = by_id[member["id"]]
        require(row["email"] == member["email"] and row["nickname"] == nickname and row["role"] == "member" and row["status"] == "active", "會員資料集讀回不符")
        ids.append(member["id"])
    require(len(set(ids)) == 12 and set(ids) | {admin["id"]} == set(by_id), "資料集混入其他帳號")
    sessions = [json.loads(line) for line in sql("sessions").splitlines()]
    require(len(sessions) == 24, "持久 session 總數不符")
    jtis = []
    for backend in BACKENDS:
        require(set(packet["logins"][backend]) == {"admin", "member"}, "登入 packet 角色不符")
        for role, actor in (("admin", admin), ("member", by_id[ids[0]])):
            jtis.append(validate_login(backend, packet["logins"][backend][role], actor, sessions))
    require(len(set(jtis)) == 24 and packet["database"] == database_state(), "seed 後資料或 session 已變")
    return rows


# 所有後端使用同一組原始 Query 與明確 fixture 預期值，包含 NUL、驗證順序與合法邊界。
def cases(packet):
    items = []
    invalid = {"filters.keyword": [INVALID]}
    for name, value in (("nul-alone", "\0"), ("nul-start", "\0abc"), ("nul-middle", "ab\0c"), ("nul-end", "abc\0"), ("nul-spaces", " \0 ")):
        items.append({"name": name, "query": "page=1&per_page=20&filters[keyword]=" + encoded_keyword(value), "fields": invalid})
    for name, query, fields in (
        ("duplicate-nul-first", "page=1&per_page=20&filters[keyword]=%00&filters[keyword]=a", {"filters.keyword": ["此參數不可重複"]}),
        ("duplicate-nul-last", "page=1&per_page=20&filters[keyword]=a&filters[keyword]=%00", {"filters.keyword": ["此參數不可重複"]}),
        ("array-nul", "page=1&per_page=20&filters[keyword][]=%00", {"filters.keyword": [TYPE]}),
        ("invalid-page-nul", "page=bad&per_page=20&filters[keyword]=%00", {"page": [INVALID], **invalid}),
        ("missing-page-nul", "per_page=20&filters[keyword]=%00", {"page": [REQUIRED], **invalid}),
        ("body-before-query", "page=bad&filters[keyword]=%00", {"_body": ["此 API 不接受 Request Body"]}),
    ):
        items.append({"name": name, "query": query, "fields": fields, "body": {} if name == "body-before-query" else None})
    for role, status, code in (("absent", 401, "E0002"), ("bad", 401, "E0002"), ("member", 403, "E0003")):
        items.append({"name": "auth-before-input-" + role, "query": "page=bad&filters[keyword]=%00", "body": {}, "role": role, "status": status, "code": code})
    member_ids = [member["id"] for member in packet["members"]]
    # 各集合依固定 nickname 指定，不使用被測搜尋或另一後端當 oracle。
    for name, keyword, indices in (
        ("omitted", None, range(12)), ("empty", "", range(12)), ("spaces", "   ", range(12)),
        ("literal-u0000", r"\u0000", (0,)), ("percent", "%", (1,)), ("underscore", "_", (1,)),
        ("backslash", "\\", (0, 1)), ("ascii-upper", "EXAMPLE", range(12)), ("ascii-lower", "example", range(12)),
        ("unicode-upper", "Ä", (4,)), ("unicode-lower", "ä", (5,)), ("precomposed", "é", (6,)),
        ("decomposed", "e\u0301", (7,)), ("tab-preserved", "\t", (8,)), ("nbsp-preserved", "\u00a0", (9,)),
        ("chinese-one", "中", (10,)), ("chinese-two", "中文", (10,)), ("no-match", "CQ33查無", ()),
        ("254-codepoints", "😀" * 254, ()),
    ):
        query = "page=1&per_page=20" + ("" if keyword is None else "&filters[keyword]=" + encoded_keyword(keyword))
        items.append({"name": name, "query": query, "ids": [member_ids[index] for index in indices], "page": 1, "per_page": 20})
    for page in (1, 2, 3):
        items.append({"name": "trimmed-ascii-page-" + str(page), "query": f"page={page}&per_page=2&filters[keyword]=" + encoded_keyword(" CQ33A "), "ids": member_ids[1:4], "page": page, "per_page": 2})
    items.append({"name": "255-codepoints", "query": "page=1&per_page=20&filters[keyword]=" + encoded_keyword("😀" * 255), "fields": invalid})
    return items


# 列表驗證完整 ID／公開欄位／精確總數與原頁碼，包含合法超頁與管理員排除。
def expect_list(data, case, rows):
    selected = [row for row in rows if row["id"] in case["ids"]]
    page, per_page, total = case["page"], case["per_page"], len(selected)
    pagination = {"page": page, "per_page": per_page, "total": total, "total_pages": (total + per_page - 1) // per_page}
    require(set(data) == {"list", "pagination"} and isinstance(data["list"], list) and
            isinstance(data["pagination"], dict) and all(type(value) is int for value in data["pagination"].values()) and
            data["pagination"] == pagination, "列表精確分頁不符")
    expected = [resource(row) for row in selected[(page - 1) * per_page:page * per_page]]
    require(data["list"] == expected and all(type(row["id"]) is int for row in data["list"]), "列表完整資料／排序不符")


# 日誌只在記憶體讀取，不輸出內容；PHP API log 與其他服務 stderr 使用各自既有入口。
def trace_errors(backend, identifiers):
    require(len(identifiers) == len(set(identifiers)), "錯誤 log_id 重複")
    # 只等日誌輸送完成，不重送 HTTP；最多十次讀取、間隔 0.2 秒，容許 stderr 短暫延遲。
    for attempt in range(10):
        if backend in ("laravel", "symfony"):
            files = list((ROOT / f".local/{backend}-logs").glob("api-*.log"))
            require(bool(files), "API 日誌缺漏")
            with max(files, key=log_mtime).open(encoding="utf-8", errors="replace") as source:
                lines = list(deque(source, maxlen=2000))
        else:
            lines = compose("logs", "--no-color", "--no-log-prefix", "--tail", "2000", backend).splitlines()
        if all(any(identifier in line and re.search(r"\bOUT\b", line) for line in lines) for identifier in identifiers):
            break
        if attempt < 9:
            time.sleep(0.2)
    for identifier in identifiers:
        matched = [line for line in lines if identifier in line]
        events = {event: [index for index, line in enumerate(matched) if re.search(rf"\b{event}\b", line)] for event in ("IN", "EX", "OUT")}
        require(len(events["IN"]) == 1 and 1 <= len(events["EX"]) <= 2 and len(events["OUT"]) == 1 and
                events["IN"][0] < events["EX"][0] < events["OUT"][0], "log_id 無法對應 IN／EX／OUT")


# 選取最新 PHP API log，避免跨日驗收誤讀前一天紀錄。
def log_mtime(path):
    return path.stat().st_mtime


# 每版 GET 批次獨立記錄所有失敗；即使政策案例失敗也繼續其餘安全讀取，最後整體非零。
def check_suite(identity):
    require(READY and not WRITING, "check 必須唯讀且已 guard")
    packet = private_json(PACKET)
    validate_schema()
    rows = validate_packet(packet, identity)
    results = []
    for backend in BACKENDS:
        before = database_state()
        require(before == packet["database"], "GET 批次開始前資料已變")
        failures, passed, identifiers = [], [], []
        for case in cases(packet):
            try:
                role = case.get("role", "admin")
                token = None if role == "absent" else "invalid.cq33.token" if role == "bad" else packet["logins"][backend][role]["access_token"]
                result = request(backend, "GET", "/api/members?" + case["query"], case.get("body"), token)
                if "ids" in case:
                    expect_list(expect(result, 200), case, rows)
                else:
                    identifiers.append(expect(result, case.get("status", 422), case.get("code", "E0001"), case.get("fields")))
                passed.append(case["name"])
            except Exception as error:
                failures.append({"case": case["name"], "reason": str(error) if isinstance(error, Failure) else type(error).__name__})
        try:
            trace_errors(backend, identifiers)
        except Exception as error:
            failures.append({"case": "error-log-trace", "reason": str(error) if isinstance(error, Failure) else type(error).__name__})
        after = database_state()
        if after != before:
            failures.append({"case": "database-unchanged", "reason": "GET 批次修改資料"})
        results.append({"backend": backend, "passed": passed, "failures": failures, "before": before, "after": after})
        if after != before:
            break
    complete = len(results) == 12 and all(not result["failures"] for result in results)
    return {"status": "pass" if complete else "fail", "identity": identity, "seed_source": packet["identity"]["source"],
            "seed_images": packet["identity"]["images"], "results": results}


# 離線檢查編碼、精確回應斷言及私有檔案安全；不載入 fixture 憑證、不呼叫 Docker 或網路。
def self_test():
    import tempfile
    from unittest.mock import patch

    require(len(NICKNAMES) == 12 and len(set(NICKNAMES)) == 12 and
            all(1 <= len(name) <= 30 and "\0" not in name for name in NICKNAMES), "nickname fixture 不合法")
    require(NICKNAMES[0].count("\\") == 1 and "\u0301" in NICKNAMES[7] and "\t" in NICKNAMES[8] and "\u00a0" in NICKNAMES[9], "特殊碼點 fixture 不符")
    for value in ("\0", "a\0b", r"\u0000", *NICKNAMES):
        encoded_keyword(value)
        require(json.loads(json.dumps({"nickname": value}, ensure_ascii=False))["nickname"] == value, "JSON 碼點未保留")
    require(encoded_keyword("\0") == "%00" and encoded_keyword(r"\u0000") == "%5Cu0000", "NUL 與 literal escape 混淆")
    sample = {"members": [{"id": index + 2} for index in range(12)]}
    matrix = cases(sample)
    require(len(matrix) == 37 and len({case["name"] for case in matrix}) == len(matrix), "共用案例漏列或重名")
    payload = {"success": False, "message": MESSAGES["E0001"], "error_code": "E0001", "errors": {"filters.keyword": [INVALID]}, "log_id": str(uuid.uuid4())}
    expect((422, payload), 422, "E0001", {"filters.keyword": [INVALID]})
    rejected = 0
    for result in ((200, payload), (422, payload | {"message": "wrong"}), (422, payload | {"errors": {}}), (422, payload | {"log_id": "bad"})):
        try:
            expect(result, 422, "E0001", {"filters.keyword": [INVALID]})
        except (Failure, ValueError):
            rejected += 1
    require(rejected == 4, "回應驗證接受已知錯誤")
    with tempfile.TemporaryDirectory(prefix="cq33-offline-") as folder:
        path = Path(folder) / "private.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            target.write('{"synthetic":true}')
        require(private_json(path) == {"synthetic": True}, "0600 合成檔未讀回")
        path.chmod(0o644)
        try:
            private_json(path)
        except Failure:
            rejected += 1
        link = Path(folder) / "link.json"
        link.symlink_to(path)
        try:
            private_json(link)
        except OSError:
            rejected += 1
    require(rejected == 6, "私有檔權限或 symlink 保護失效")
    # 錯誤 cwd 或 Compose project 必須在呼叫任何 Docker 工具之前拒絕，不為 self-test 改固定目的地。
    module = sys.modules[__name__]
    with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "cq33-invalid"}), patch.object(module, "command") as running:
        try:
            guard()
        except Failure:
            rejected += 1
        require(not running.called and not module.READY, "guard 拒絕前已呼叫工具或保留可寫狀態")
    # 直接執行初次保存的 O_EXCL 分支，只替換 OS I/O；不能覆寫已存在或 pending packet。
    with patch.object(os, "open", side_effect=FileExistsError) as opening:
        try:
            save_packet({"status": "pending"}, initial=True)
        except FileExistsError:
            rejected += 1
        require(opening.call_count == 1 and opening.call_args.args ==
                (PACKET, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "pending 未使用固定 O_EXCL 保護")
    # 實際 seed 控制流遇到首次 POST timeout 必須只送一次，保留最後 pending receipt，且撤回可寫旗標。
    admin = {"email": "cq33-admin@example.test", "nickname": "離線管理員", "password": secrets.token_hex(8)}
    initial_state = {table: {"count": 1 if table in ("accounts", "login_lockout_settings") else 0} for table in TABLES}
    receipts = []
    previous_ready = module.READY
    try:
        module.READY = True
        with patch.object(module, "private_json", return_value=admin), patch.object(module, "validate_schema"), \
             patch.object(module, "database_state", return_value=initial_state), \
             patch.object(module, "accounts", return_value=[{"id": 1, "role": "admin", "status": "active", **admin}]), \
             patch.object(module, "save_packet", side_effect=receipts_record(receipts)), \
             patch.object(module, "request", side_effect=TimeoutError) as sending:
            try:
                seed({})
            except TimeoutError:
                rejected += 1
            require(sending.call_count == 1 and not module.WRITING and receipts[-1]["status"] == "pending" and
                    receipts[-1]["operation"] == "laravel:register" and receipts[-1]["members"] == [], "未知 POST 結果被重送或當成完成")
        # pending 再次啟動必須在任何 HTTP 前停止，不能將讀取／恢復誤當新的 seed。
        with patch.object(module, "private_json", return_value=admin), \
             patch.object(module, "save_packet", side_effect=FileExistsError), patch.object(module, "request") as sending:
            try:
                seed({})
            except FileExistsError:
                rejected += 1
            require(not sending.called, "既存 packet 仍送出 HTTP")
    finally:
        module.READY = previous_ready
    require(rejected == 10, "guard／pending／timeout 保護未執行")
    return {"status": "pass", "scope": "offline-only", "cases_per_backend": len(matrix), "known_invalid_rejected": rejected}


# 離線測試僅深複製各次 receipt，讓後續記憶體變更不能掩蓋當時落盤狀態。
def receipts_record(receipts):
    # callback 模擬落盤，不更改固定 packet 路徑或碰觸真實 fixture。
    def record(packet, initial=False):
        receipts.append(json.loads(json.dumps(packet)))
    return record


# CLI 只選固定模式；所有失敗輸出受控原因或型別，pending packet 永遠保留供人工釐清。
def main():
    require(len(sys.argv) == 2 and sys.argv[1] in {"--guard", "--seed", "--check", "--self-test"}, "用法：cq33_http.py --guard|--seed|--check|--self-test")
    if sys.argv[1] == "--self-test":
        result = self_test()
    else:
        identity = guard()
        result = {"status": "guarded", "identity": identity} if sys.argv[1] == "--guard" else seed(identity) if sys.argv[1] == "--seed" else check_suite(identity)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"status": "fail", "reason": str(error) if isinstance(error, Failure) else type(error).__name__, "seed_policy": "既存或 pending packet 不得自動重送"}, ensure_ascii=False), flush=True)
        sys.exit(1)
