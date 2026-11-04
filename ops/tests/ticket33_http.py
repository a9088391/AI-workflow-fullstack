"""Ticket 33：在專用 Compose fixture 重用 Fiber 公開／作者與管理驗收。"""

import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "ops/tests"
LOCAL = ROOT / ".local/ticket33-backend"
FRONTEND = ROOT / ".local/ticket33-frontend"
PROJECT = "ticket33_test"
COMPOSE_FILE = "compose.yaml:ops/tests/ticket33_compose.yaml"
FIBER = "http://localhost:18331"
LARAVEL = "http://localhost:18332"
ORIGIN = "http://localhost:18330"
SOURCES = {
    "ticket28_http.py": "8ce8269327da45a1ae6161d0bcb70736b767bce50c4606edd1ad9e3655a25704",
    "ticket29_http.py": "d0bff8f8357ce9e27081e1bdbee9645de4c3a4349f5b6e8083750e3bd8c03af8",
    "ticket30_http.py": "f695b905662ea731946c9ea77b53de690cc267cf767cdf3972ad6ba147613dc9",
    "ticket31_http.py": "e26f2611d105a8204edf294d6ddaf6bcebf3194707a31fd9c39e932f58c42710",
    "ticket32_http.py": "5ebfac60ad327e4840146b7f2cb0fc7cddff0a78a77a38c55af4b37e8a47243c",
}


# check 讓第一個隔離或契約偏差立即中止，避免後續測試掩蓋問題。
def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


# command 固定工作目錄，使 Compose 與資料庫指令只解析本工作樹的 fixture。
def command(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


# source_check 鎖定被重用測試的原始版本，避免上游改動後沿用舊接線。
def source_check():
    for name, expected in SOURCES.items():
        actual = hashlib.sha256((TESTS / name).read_bytes()).hexdigest()
        check(actual == expected, f"測試來源已變，須重審接線：{name} {actual}")
        print(f"REUSE {name} source_sha256={actual}")


# public_copy_content 調整隔離金鑰／CORS，並移除作者路由完成前的兩個過時斷言。
def public_copy_content():
    original = (TESTS / "ticket31_http.py").read_text()
    check(original.count(".local/ticket31-backend/fiber.key") == 1 and
          original.count("http://localhost:18311") == 3, "Ticket 31 可調整位置已變")
    content = original.replace(".local/ticket31-backend/fiber.key",
                               ".local/ticket33-backend/fiber.key").replace("http://localhost:18311", ORIGIN)
    # Ticket 32 已實作這兩條路由；其餘公開 API 斷言與原始套件相同。
    obsolete = (
        '    expect(call(FIBER, "POST", "/api/articles", {"title": "尚未支援"}), 405, "E0005")\n',
        '    expect(call(FIBER, "GET", "/api/me/articles?page=1&per_page=20", token=author), 404, "E0004")\n',
    )
    for assertion in obsolete:
        check(content.count(assertion) == 1, "過時斷言位置已變，須重新確認")
        content = content.replace(assertion, "")
    return content


# prepare 只建立本票忽略版控的金鑰與前端副本；重入時保留已產生金鑰。
def prepare():
    source = (ROOT / "frontend/vue/src/auth.js").read_text()
    # isolated_url 把未啟動的其他後端導向無效埠，防止測試頁誤寫展示服務。
    def isolated_url(match):
        port = match.group(1)
        return "http://localhost:" + ("18332" if port == "8081" else "18331" if port == "8089" else "0")
    changed, count = re.subn(r"http://localhost:(808[1-9]|809[0-2])", isolated_url, source)
    check(count == 12, "Vue API 基底位址已變")
    # 先核有限來源才產生 keys；重入也只接受本次來源的精確映射，不接受額外可達位址。
    if not LOCAL.exists() and not FRONTEND.exists():
        LOCAL.mkdir(parents=True)
        (LOCAL / "backups").mkdir()
        (LOCAL / "laravel-logs").mkdir()
        (LOCAL / "suites").mkdir()
        (LOCAL / "suites/ticket31_http.py").write_text(public_copy_content())
        for service in ("laravel", "fiber"):
            key = LOCAL / f"{service}.key"
            key.write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
            key.chmod(0o600)
        cors = (ROOT / "backends/laravel/config/cors.php").read_text()
        check(cors.count("http://localhost:8080") == 1, "Laravel CORS 來源已變")
        (LOCAL / "cors.php").write_text(cors.replace("http://localhost:8080", ORIGIN))
        (LOCAL / "nginx.conf").write_text(
            "server {\n  listen 80;\n  location = / { return 302 /vue/; }\n"
            "  location /vue/ { proxy_pass http://frontend-vue:4173; }\n}\n"
        )
        # 原前端碼保持不變；隔離副本只讓 Laravel/Fiber 可達，其餘選項無法碰觸現有服務。
        shutil.copytree(ROOT / "frontend/vue", FRONTEND,
                        ignore=shutil.ignore_patterns("node_modules", "dist", ".DS_Store"))
    else:
        check(LOCAL.is_dir() and FRONTEND.is_dir() and
              (LOCAL / "suites/ticket31_http.py").read_text() == public_copy_content() and
              all((LOCAL / f"{service}.key").is_file() for service in ("laravel", "fiber")),
              "既有 Ticket 33 fixture 不完整或已變更")
    auth = FRONTEND / "src/auth.js"
    original = auth.read_text()
    check(original in (source, changed), "Vue 隔離副本已變，拒絕覆寫")
    if original == source:
        auth.write_text(changed)
    source_auth = (ROOT / "frontend/vue/src/auth.js").read_bytes()
    print(f"PREPARED {LOCAL} {FRONTEND} source_auth_sha256={hashlib.sha256(source_auth).hexdigest()}")


# inspect_service 同時檢查 Docker 標籤、健康、image 與實際埠，阻止連到別票容器。
def inspect_service(service, internal_port, host_port):
    ids = command("docker", "compose", "--profile", "fiber", "ps", "-q", service).splitlines()
    check(len(ids) == 1, f"容器未唯一啟動：{service}")
    info = json.loads(command("docker", "inspect", ids[0]))[0]
    labels = info["Config"]["Labels"]
    check(labels.get("com.docker.compose.project") == PROJECT and
          labels.get("com.docker.compose.service") == service, f"容器身分不符：{service}")
    check(info["State"]["Running"] and info["State"].get("Health", {}).get("Status") == "healthy",
          f"容器未就緒：{service}")
    if host_port:
        check(info["HostConfig"]["PortBindings"].get(f"{internal_port}/tcp") ==
              [{"HostIp": "127.0.0.1", "HostPort": host_port}], f"實際埠不符：{service}")
    image = command("docker", "compose", "--profile", "fiber", "images", "-q", service)
    check(info["Image"] == "sha256:" + image, f"執行 image 非目前 build：{service}")
    return info


# mounted 確認金鑰、設定與備份路徑實際掛在本票目錄，且敏感設定為唯讀。
def mounted(info, source, destination, readonly=True):
    expected = os.path.realpath(source)
    check(any(m.get("Type") == "bind" and os.path.realpath(m.get("Source", "")) == expected and
              m.get("Destination") == destination and m.get("RW") is (not readonly)
              for m in info["Mounts"]), f"掛載不符：{destination}")


# guard 在任何 API/SQL 寫入前核對有效 Compose 設定、實際容器與前端 bundle。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT and
          os.environ.get("COMPOSE_FILE") == COMPOSE_FILE, "Compose fixture 身分不符")
    source_check()
    check((LOCAL / "suites/ticket31_http.py").read_text() == public_copy_content(),
          "公開套件副本已變")
    config = json.loads(command("docker", "compose", "--profile", "fiber", "config", "--format", "json"))
    check(config["name"] == PROJECT and config["volumes"]["postgres_data"]["name"] ==
          PROJECT + "_postgres_data", "資料卷不符")
    check(config["services"]["db"]["environment"].get("TICKET33_FIXTURE") == "isolated-ticket33",
          "DB fixture 標記不符")
    for service, port in (("laravel", "18332"), ("fiber", "18331"), ("nginx", "18330")):
        published = config["services"][service]["ports"]
        check(len(published) == 1 and published[0]["published"] == port and
              published[0]["host_ip"] == "127.0.0.1", f"宣告埠不符：{service}")
    check(config["services"]["frontend-vue"]["build"]["context"] == str(FRONTEND),
          "Vue build 來源不符")
    keys = {service: (LOCAL / f"{service}.key").read_bytes() for service in ("laravel", "fiber")}
    check(all(len(key) >= 40 for key in keys.values()) and keys["laravel"] != keys["fiber"],
          "Ticket 33 金鑰缺失或共用")
    for service, key in keys.items():
        demo = ROOT / f".local/keys/{service}.key"
        check(not demo.exists() or key != demo.read_bytes(), f"{service} 測試與展示共用金鑰")
    db = inspect_service("db", 5432, None)
    check(any(m.get("Name") == PROJECT + "_postgres_data" and
              m.get("Destination") == "/var/lib/postgresql" for m in db["Mounts"]),
          "實際 DB 資料卷不符")
    laravel = inspect_service("laravel", 8000, "18332")
    fiber = inspect_service("fiber", 8000, "18331")
    inspect_service("frontend-vue", 4173, None)
    nginx = inspect_service("nginx", 80, "18330")
    mounted(laravel, LOCAL / "laravel.key", "/run/secrets/laravel-jwt.key")
    mounted(fiber, LOCAL / "fiber.key", "/run/secrets/fiber-jwt.key")
    mounted(laravel, LOCAL / "cors.php", "/srv/laravel/config/cors.php")
    mounted(nginx, LOCAL / "nginx.conf", "/etc/nginx/conf.d/default.conf")
    mounted(laravel, LOCAL / "laravel-logs", "/srv/laravel/storage/logs", False)
    check("CORS_ORIGIN=" + ORIGIN in fiber["Config"]["Env"], "Fiber CORS 來源不符")
    check(any(mount["source"] == str(LOCAL / "backups") and mount["target"] == "/backups"
              for mount in config["services"]["sql"]["volumes"]),
          "SQL 備份位置不符")
    check(command("docker", "compose", "exec", "-T", "db", "psql", "-XAt", "-U",
                  "articles_owner", "-d", "articles", "-c", "SELECT current_database()||':'||current_user") ==
          "articles:articles_owner", "DB 身分不符")
    for base in (FIBER, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", f"API 未就緒：{base}")
    for base in (FIBER, LARAVEL):
        request = Request(base + "/api/articles?page=1&per_page=20", headers={"Origin": ORIGIN})
        with urlopen(request, timeout=5) as response:
            check(response.status == 200 and response.headers.get("Access-Control-Allow-Origin") == ORIGIN,
                  f"CORS 未允許隔離 Vue：{base}")
    auth = (FRONTEND / "src/auth.js").read_text()
    check(FIBER in auth and LARAVEL in auth and not re.search(r"http://localhost:80\d\d", auth),
          "Vue 副本 API 位址不安全")
    with urlopen(ORIGIN + "/vue/", timeout=5) as response:
        html = response.read().decode()
    scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
    check(bool(scripts), "Vue bundle 不存在")
    for script in scripts:
        with urlopen(urljoin(ORIGIN + "/vue/", script), timeout=5) as response:
            bundle = response.read().decode()
        check(FIBER in bundle and LARAVEL in bundle and not re.search(r"http://localhost:80\d\d", bundle),
              "Vue bundle API 位址不安全")
    print("PASS ticket33_test guard")


# measured 以 SQL 日誌標記框住單一 Fiber 列表請求，供舊管理套件核對查詢次數。
def measured(api, path, credential):
    marker = secrets.token_hex(8)
    db_id = command("docker", "compose", "--profile", "fiber", "ps", "-q", "db")
    since = datetime.now(timezone.utc).isoformat()
    command("docker", "compose", "exec", "-T", "db", "psql", "-XAt", "-U", "articles_owner",
            "-d", "articles", "-c", "SELECT 'T33START" + marker + "'")
    result = api.call(FIBER, "GET", path, token=credential)
    command("docker", "compose", "exec", "-T", "db", "psql", "-XAt", "-U", "articles_owner",
            "-d", "articles", "-c", "SELECT 'T33END" + marker + "'")
    # PostgreSQL 把查詢記錄寫到 stderr；Docker 的 stdout 與 stderr 都須納入標記範圍。
    output = subprocess.run(("docker", "logs", "--since", since, db_id), cwd=ROOT,
                            capture_output=True, text=True, check=True)
    lines = (output.stdout + output.stderr).splitlines()
    starts = [i for i, line in enumerate(lines) if "T33START" + marker in line]
    ends = [i for i, line in enumerate(lines) if "T33END" + marker in line]
    check(len(starts) == len(ends) == 1 and starts[0] < ends[0], "SQL 日誌標記不完整")
    queries = [line for line in lines[starts[0]+1:ends[0]]
               if "articles_app@articles" in line and " execute stmtcache_" in line and
               "SELECT 1 FROM accounts,login_sessions" not in line]
    print(f"QUERY_COUNT path={path} count={len(queries)} statements={queries}")
    return result, queries


# reuse_path 令舊管理套件直接匯入唯讀原始 helper，跳過它自己的 Ticket 30 副本產生器。
def reuse_path():
    return TESTS


# boundary_case 以短命 trigger 對齊登入實際 DB 時間，驗證視窗左界不計、右移 1µs 計入。
def boundary_case(api, admin):
    guard()
    manager = admin.fixture(api, "boundary-admin", "邊界管理員", True)
    settings_path = "/api/settings/login-lockout"
    original = api.expect(api.call(FIBER, "GET", settings_path, token=manager["token"]), 200)
    check(original["enabled"] is True, "邊界案例需要先前套件恢復啟用設定")
    boundary = {"enabled": True, "window_minutes": 5, "failure_threshold": 3, "lock_seconds": 300}
    try:
        api.expect(api.call(FIBER, "PUT", settings_path, boundary, manager["token"]), 200)
        laravel_manager = api.expect(api.call(LARAVEL, "POST", "/api/auth/login",
                                              {"email": manager["email"], "password": "Abc1234"}), 200)
        check(api.expect(api.call(LARAVEL, "GET", settings_path,
                                  token=laravel_manager["access_token"]), 200) == boundary,
              "Laravel 未讀到左界測試設定")
        for microseconds, expected in ((0, [401, 401]), (1, [401, 429])):
            member = admin.fixture(api, "boundary-" + str(microseconds), "邊界會員")
            account_id = int(member["id"])
            epoch = int(admin.sql(f"SELECT failure_epoch FROM accounts WHERE id={account_id}"))
            seed = int(admin.sql("INSERT INTO login_failures(account_id,failure_epoch,occurred_at) "
                                 f"VALUES ({account_id},{epoch},clock_timestamp()-interval '10 minutes') RETURNING id"))
            name = "t33_boundary_" + secrets.token_hex(8)
            # trigger 只調整本會員的種子事件；無論斷言或請求失敗都移除資料庫物件。
            try:
                admin.sql(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER "
                          "SET search_path=pg_catalog,public AS $$ BEGIN "
                          f"IF NEW.account_id={account_id} THEN UPDATE public.login_failures SET "
                          f"occurred_at=NEW.occurred_at-interval '5 minutes'+interval '{microseconds} microseconds' "
                          f"WHERE id={seed}; END IF; RETURN NEW; END; $$; "
                          f"CREATE TRIGGER {name} BEFORE INSERT ON login_failures "
                          f"FOR EACH ROW EXECUTE FUNCTION {name}();")
                barrier = threading.Barrier(2)

                # wrong_login 使兩個真實後端重疊提交，觸發各自登入時間的左界計算。
                def wrong_login(base):
                    barrier.wait(timeout=5)
                    return api.call(base, "POST", "/api/auth/login",
                                    {"email": member["email"], "password": "Wrong123"})

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(wrong_login, (FIBER, LARAVEL)))
            finally:
                admin.sql(f"DROP TRIGGER IF EXISTS {name} ON login_failures; DROP FUNCTION IF EXISTS {name}();")
            check(sorted(result[0] for result in results) == expected, f"視窗左界 HTTP 結果不符：{microseconds}")
            for result in results:
                api.expect(result, result[0], "E0105" if result[0] == 429 else "E0101")
                if result[0] == 429:
                    check(int(result[2]["Retry-After"]) > 0, "左界鎖定缺少 Retry-After")
            check(admin.sql(f"SELECT count(*) FROM login_failures WHERE account_id={account_id} "
                            f"AND failure_epoch={epoch}") == "3", "左界失敗事件未完整提交")
            in_window = admin.sql("SELECT count(*) FROM login_failures WHERE "
                                  f"account_id={account_id} AND failure_epoch={epoch} AND "
                                  "occurred_at>(SELECT max(occurred_at) FROM login_failures WHERE "
                                  f"account_id={account_id} AND failure_epoch={epoch})-interval '5 minutes'")
            check(in_window == ("2" if microseconds == 0 else "3"), "左界計數不符")
            check(admin.sql(f"SELECT locked_until IS NOT NULL FROM accounts WHERE id={account_id}") ==
                  ("f" if microseconds == 0 else "t"), "左界鎖定狀態不符")
    finally:
        api.expect(api.call(FIBER, "PUT", settings_path, original, manager["token"]), 200)
    print("PASS Ticket 33 exact sliding-window left boundary across Fiber/Laravel")


# run_suites 在完整守衛後串接前期 Fiber 與管理案例；原測試斷言保持原樣。
def run_suites():
    os.chdir(ROOT)
    guard()
    sys.path.insert(0, str(TESTS))
    sys.path.insert(0, str(LOCAL / "suites"))
    public = importlib.import_module("ticket31_http")
    author = importlib.import_module("ticket32_http")
    admin = importlib.import_module("ticket30_http")
    api = importlib.import_module("ticket28_http")
    regress = importlib.import_module("ticket29_http")
    # 僅改綁目的地、守衛和測量函式；公開案例、錯誤與持久化斷言不刪減。
    public.FIBER, public.LARAVEL, public.guard = FIBER, LARAVEL, guard
    author.FIBER, author.LARAVEL, author.guard = FIBER, LARAVEL, guard
    api.ECHO, api.LARAVEL, api.guard = FIBER, LARAVEL, guard
    regress.ECHO, regress.LARAVEL = FIBER, LARAVEL
    admin.ECHO, admin.LARAVEL, admin.LOCAL = FIBER, LARAVEL, LOCAL
    admin.guard, admin.prepare_reuse, admin.measured = guard, reuse_path, measured
    public.main()
    author.main()
    # 舊管理入口只接受自己的 CLI 參數；暫時換成無參數執行完整套件。
    original_argv = sys.argv
    try:
        sys.argv = [str(TESTS / "ticket30_http.py")]
        admin.main()
    finally:
        sys.argv = original_argv
    boundary_case(api, admin)
    print("EXECUTED Ticket 33 public 01–05, author 01–16, admin 05/17–22; DB outage remains NOT VERIFIED")


# main 明確分開準備、唯讀守衛與寫入驗收，避免誤用參數改動資料。
def main():
    check(len(sys.argv) == 2 and sys.argv[1] in ("--prepare", "--guard-only", "--run"),
          "用法：ticket33_http.py --prepare|--guard-only|--run")
    if sys.argv[1] == "--prepare":
        source_check()
        prepare()
    elif sys.argv[1] == "--guard-only":
        guard()
    else:
        run_suites()


if __name__ == "__main__":
    main()
