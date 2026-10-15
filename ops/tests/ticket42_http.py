"""Ticket 42：在隔離 Compose 中驗證 NestJS 管理與 Laravel 共用狀態。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from urllib.request import urlopen

from ticket40_http import call, check, expect, sql

NESTJS = "http://127.0.0.1:18423"
LARAVEL = "http://127.0.0.1:18422"
ORIGIN = "http://localhost:18421"
PROJECT = "ticket42_test"
COMPOSE = "compose.yaml:ops/tests/ticket42_compose.yaml"
ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local/ticket42-backend"
FRONTEND = ROOT / ".local/ticket42-frontend"


# source_identity 逐檔記住目前 Vue 來源，重入及守衛可拒絕過期副本。
def source_identity():
    source = ROOT / "frontend/vue"
    files = [path for path in source.rglob("*") if path.is_file() and
             not any(part in {"node_modules", "dist", ".DS_Store"} for part in path.parts)]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


# prepare 只建立本票忽略版控的金鑰、CORS 與 Vue 副本；重入時核對來源並保留金鑰。
def prepare():
    source = ROOT / "frontend/vue"
    identity = source_identity()
    if LOCAL.exists() or FRONTEND.exists():
        check(LOCAL.is_dir() and FRONTEND.is_dir() and
              (LOCAL / "source.sha256").read_text().strip() == identity and
              all((LOCAL / f"{service}.key").is_file() for service in ("laravel", "nestjs")),
              "既有 Ticket 42 fixture 不完整或來源已改變")
        print("PASS Ticket 42 fixture reuse source_sha256=" + identity)
        return
    # 原始碼副本只改兩個本票 API，其他選項一律連到不可達埠。
    cors = (ROOT / "backends/laravel/config/cors.php").read_text()
    check(cors.count("http://localhost:8080") == 1, "Laravel CORS 來源已變")
    auth = (source / "src/auth.js").read_text()
    ports = {"8081": "18422", "8092": "18423"}
    # callback 只映射本票兩個 API，其餘後端強制指向不可達埠。
    changed, count = re.subn(r"http://localhost:(808[1-9]|809[0-2])",
                             lambda match: "http://localhost:" + ports.get(match.group(1), "0"), auth)
    check(count == 12, "Vue 後端 URL 清單已變")
    LOCAL.mkdir(parents=True)
    (LOCAL / "backups").mkdir()
    (LOCAL / "laravel-logs").mkdir()
    for service in ("laravel", "nestjs"):
        key = LOCAL / f"{service}.key"
        key.write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
        key.chmod(0o600)
    (LOCAL / "cors.php").write_text(cors.replace("http://localhost:8080", ORIGIN))
    shutil.copytree(source, FRONTEND, ignore=shutil.ignore_patterns("node_modules", "dist", ".DS_Store"))
    (FRONTEND / "src/auth.js").write_text(changed)
    (LOCAL / "source.sha256").write_text(identity + "\n")
    print("PASS Ticket 42 fixture prepared source_sha256=" + identity)


# db_logs 讀取本票資料庫的真實 PostgreSQL 日誌，供 SQL 發送量測。
def db_logs():
    return subprocess.check_output(["docker", "compose", "logs", "--no-color", "--no-log-prefix", "db"], text=True)


# guard 核對實際容器、專用 DB 資料卷、埠及金鑰，通過前禁止任何 fixture 寫入。
def guard():
    # 先驗環境與宣告，錯誤專案時完全不查寫入端點。
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT, "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == COMPOSE, "錯誤 Compose 檔")
    config = json.loads(subprocess.check_output(["docker", "compose", "--profile", "nestjs", "config", "--format", "json"], text=True))
    check(config["name"] == PROJECT and config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data", "非隔離資料卷")
    check(config["services"]["db"]["environment"]["TICKET42_FIXTURE"] == "isolated-ticket42", "fixture 標記錯誤")
    check(config["networks"]["default"]["ipam"]["config"] == [{"subnet": "192.168.248.0/24"}], "專用子網宣告不符")
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", PROJECT + "_default"], text=True))[0]
    check(network["Name"] == PROJECT + "_default" and
          network["IPAM"]["Config"][0]["Subnet"] == "192.168.248.0/24", "實際專用子網不符")
    check(config["services"]["db"]["command"][-2:] == ["-c", "log_line_prefix=%m [%p] %u@%d "], "DB 日誌設定不符")
    check(config["services"]["sql"]["volumes"][-1]["source"] == str(LOCAL / "backups"), "備份目錄不符")
    check(config["services"]["nestjs"]["environment"]["CORS_ORIGIN"] == ORIGIN, "NestJS CORS 來源不符")
    check(config["services"]["frontend-vue"]["build"]["context"] == str(FRONTEND), "Vue 建置來源不符")
    check((LOCAL / "source.sha256").read_text().strip() == source_identity(), "Vue 來源已變，需重建隔離 fixture")
    check(config["services"]["nginx"]["ports"] == [{"mode": "ingress", "target": 80, "published": "18421", "protocol": "tcp", "host_ip": "127.0.0.1"}], "NGINX 宣告埠不符")
    check((LOCAL / "cors.php").read_text().count(ORIGIN) == 1, "Laravel CORS fixture 不符")
    auth = (FRONTEND / "src/auth.js").read_text()
    check("http://localhost:18422" in auth and "http://localhost:18423" in auth and
          not re.search(r"http://localhost:(?:808[1-9]|809[0-2])", auth), "Vue API 來源不符")
    # 實際容器的標籤、網路、port、image 與測試金鑰須和宣告一致。
    for service, port in (("laravel", "18422"), ("nestjs", "18423")):
        ids = subprocess.check_output(["docker", "compose", "--profile", "nestjs", "ps", "-q", service], text=True).splitlines()
        check(len(ids) == 1, "後端容器未唯一啟動：" + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == PROJECT and
              item["Config"]["Labels"].get("com.docker.compose.service") == service, "容器不屬於本票")
        check(item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy", "後端不健康")
        check(PROJECT + "_default" in item["NetworkSettings"]["Networks"] and
              {"DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"}.issubset(item["Config"]["Env"]), "後端未連隔離 DB")
        check(item["HostConfig"]["PortBindings"]["8000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": port}], "實際埠錯誤")
        key = os.path.realpath(f".local/ticket42-backend/{service}.key")
        check(any(os.path.realpath(m.get("Source", "")) == key and m.get("Destination") == f"/run/secrets/{service}-jwt.key" and m.get("RW") is False for m in item["Mounts"]), "測試金鑰掛載錯誤")
        with open(key, "rb") as test_key, open(f".local/keys/{service}.key", "rb") as demo_key:
            encoded = test_key.read().strip()
            check(len(base64.b64decode(encoded, validate=True)) == 32 and encoded != demo_key.read().strip(), "測試金鑰長度或隔離不符")
        image = subprocess.check_output(["docker", "compose", "--profile", "nestjs", "images", "-q", service], text=True).strip()
        check("sha256:" + image == item["Image"], "執行中容器不是目前 Compose 映像")
        if service == "laravel":
            for source, destination, readonly in ((LOCAL / "cors.php", "/srv/laravel/config/cors.php", True),
                                                  (LOCAL / "laravel-logs", "/srv/laravel/storage/logs", False)):
                check(any(Path(m.get("Source", "/")).resolve() == source and m.get("Destination") == destination and
                          m.get("RW") is not readonly for m in item["Mounts"]), "Laravel 隔離掛載不符")
    check((LOCAL / "laravel.key").read_bytes() != (LOCAL / "nestjs.key").read_bytes(), "兩後端共用測試金鑰")
    ids = subprocess.check_output(["docker", "compose", "ps", "-q", "db"], text=True).splitlines()
    check(len(ids) == 1, "隔離 DB 未唯一啟動")
    database = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    check(database["Config"]["Labels"].get("com.docker.compose.project") == PROJECT and
          "TICKET42_FIXTURE=isolated-ticket42" in database["Config"]["Env"] and
          PROJECT + "_default" in database["NetworkSettings"]["Networks"] and
          database["State"]["Running"] and database["State"].get("Health", {}).get("Status") == "healthy", "執行中 DB 身分錯誤")
    check(any(m.get("Name") == PROJECT + "_postgres_data" for m in database["Mounts"]), "DB 資料卷錯誤")
    check(not database["HostConfig"]["PortBindings"], "DB 不應發布埠")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 身分錯誤")
    for service in ("frontend-vue", "nginx"):
        ids = subprocess.check_output(["docker", "compose", "ps", "-q", service], text=True).splitlines()
        check(len(ids) == 1, "前端容器未唯一啟動：" + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == PROJECT and
              PROJECT + "_default" in item["NetworkSettings"]["Networks"] and
              item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy", "前端容器不符")
        image = subprocess.check_output(["docker", "compose", "images", "-q", service], text=True).strip()
        check(item["Image"] == "sha256:" + image, "前端非目前 Compose 映像")
        if service == "nginx":
            check(item["HostConfig"]["PortBindings"].get("80/tcp") ==
                  [{"HostIp": "127.0.0.1", "HostPort": "18421"}], "NGINX 實際埠不符")
    for base in (NESTJS, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")
    # 靜態 bundle 也要指向本票埠，不能只信副本來源檔。
    with urlopen(ORIGIN + "/vue/", timeout=5) as response:
        html = response.read().decode()
    asset = re.search(r'src="([^"]+\.js)"', html)
    check(asset is not None, "Vue bundle 入口缺漏")
    with urlopen(ORIGIN + asset.group(1), timeout=5) as response:
        bundle = response.read().decode()
    check("http://localhost:18422" in bundle and "http://localhost:18423" in bundle and
          not re.search(r"http://localhost:(?:808[1-9]|809[0-2])", bundle), "Vue bundle API 來源不符")
    print("PASS Ticket 42 guard: project, volume, keys, images, ports, bundle, health")


# create_user 只在隔離 DB 中建立唯一會員；管理員角色由受控 fixture 更新。
def create_user(tag, backend=NESTJS, admin=False):
    email = f"t42-{tag}@example.test"
    expect(call(backend, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": "票42測試"}), 201)
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE email='{email}'")
    data = expect(call(backend, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    return {"id": data["user"]["id"], "email": email, "token": data["access_token"]}


# settings 讀取四欄並拒絕額外資料，便於跨後端逐值比較。
def settings(base, token):
    data = expect(call(base, "GET", "/api/settings/login-lockout", token=token), 200)
    check(set(data) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "設定欄位不精確")
    return data


# parity_error 略過每次合法變動的 log_id，只逐字比對共用業務錯誤。
def parity_error(method, path, body, nestjs_token, laravel_token, status, code, fields=None):
    first = expect(call(NESTJS, method, path, body, nestjs_token), status, code, fields)
    second = expect(call(LARAVEL, method, path, body, laravel_token), status, code, fields)
    check({key: value for key, value in first.items() if key != "log_id"} ==
          {key: value for key, value in second.items() if key != "log_id"}, "跨後端錯誤不一致：" + path)


# expected_members 從隔離 DB 讀完整會員，再以獨立 Python 字面規則建立精確搜尋 oracle。
def expected_members(keyword):
    raw = sql("SELECT row_to_json(t) FROM (SELECT id,email,nickname FROM accounts WHERE role='member' "
              "ORDER BY created_at DESC,id DESC) t")
    rows = [json.loads(line) for line in raw.splitlines()] if raw else []
    term = keyword.strip(" ").translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    if "\0" in term:
        return []
    return [row["id"] for row in rows if not term or any(term in row[field].translate(
            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")) for field in ("nickname", "email"))]


# listed 比對完整排序 ID 與精確總數，涵蓋第二頁、末頁及合法超頁。
def listed(base, path, token, expected, per_page=2):
    last = max(1, (len(expected) + per_page - 1) // per_page)
    for page in sorted({1, 2, last, 999999}):
        join = "&" if "?" in path else "?"
        data = expect(call(base, "GET", f"{path}{join}page={page}&per_page={per_page}", token=token), 200)
        check([row["id"] for row in data["list"]] == expected[(page - 1) * per_page:page * per_page],
              "列表 ID/排序不符：" + path)
        check(data["pagination"] == {"page": page, "per_page": per_page, "total": len(expected),
                                      "total_pages": (len(expected) + per_page - 1) // per_page}, "列表總數/分頁不符：" + path)


# locked_sql 只在 guard 通過後持有指定隔離資料列，供兩後端請求實際重疊。
def locked_sql(statement):
    command = ["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c",
               "SET application_name='ticket42_holder'; BEGIN; " + statement + "; SELECT pg_sleep(6); COMMIT;"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, error = process.communicate()
            raise AssertionError("隔離鎖交易未持鎖：" + error.strip())
        if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='ticket42_holder' AND wait_event='PgSleep'") == "1":
            return process
        time.sleep(0.05)
    process.terminate()
    process.communicate()
    raise AssertionError("隔離鎖交易未進入等待")


# wait_lock 確認真正的資料庫鎖等待，而非只以固定睡眠推測請求順序。
def wait_lock(minimum=1):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if int(sql("SELECT count(*) FROM pg_stat_activity WHERE usename='articles_app' AND wait_event_type='Lock'")) >= minimum:
            return
        time.sleep(0.05)
    raise AssertionError("未觀察到後端資料庫鎖等待")


# finish_lock 等隔離持鎖交易提交後才檢查結果，避免未知提交狀態被當成測試結論。
def finish_lock(process):
    _, error = process.communicate(timeout=10)
    check(process.returncode == 0, "隔離鎖交易失敗：" + error.strip())


# main 依契約順序測角色、搜尋、停用、內容及設定的 HTTP 與持久效果。
def main():
    check(len(sys.argv) == 2 and sys.argv[1] in ("--prepare", "--guard-only", "--run"),
          "用法：ticket42_http.py --prepare|--guard-only|--run")
    if sys.argv[1] == "--prepare":
        prepare()
        return
    guard()
    if sys.argv[1] == "--guard-only":
        print("PASS ticket42_test guard")
        return
    nonce = secrets.token_hex(5)
    admin = create_user(nonce + "-admin", admin=True)
    member = create_user(nonce + "-member")
    other = create_user(nonce + "-other", backend=LARAVEL)
    admin_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
    member_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    original = settings(NESTJS, admin["token"])
    check(settings(LARAVEL, admin_laravel) == original, "初始跨後端設定不同")
    # 同一批負例在兩後端逐字比較，保留認證／角色先驗的契約順序。
    for method, path, body, status, code, fields in (
        ("GET", "/api/members?page=1&per_page=20&x=1", None, 422, "E0001", {"x": ["不支援此欄位"]}),
        ("GET", "/api/members?page=1&per_page=20&page=2", None, 422, "E0001", {"page": ["此參數不可重複"]}),
        ("GET", "/api/members?page=1&per_page=20&filters%5Bkeyword%5D%5B%5D=x", None, 422, "E0001", {"filters.keyword": ["欄位型態不正確"]}),
        ("GET", f"/api/members/{member['id']}", "{}", 422, "E0001", {"_body": ["此 API 不接受 Request Body"]}),
        ("PUT", f"/api/members/{member['id']}/status", [], 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]}),
        ("PUT", f"/api/members/{member['id']}/status", {"status": 1}, 422, "E0001", {"status": ["欄位型態不正確"]}),
        ("PUT", f"/api/members/{member['id']}/status", {"status": "paused"}, 422, "E0001", {"status": ["欄位格式或範圍不正確"]}),
        ("PUT", "/api/settings/login-lockout", {"enabled": True, "window_minutes": 1.0, "failure_threshold": 2, "lock_seconds": 300}, 422, "E0001", {"window_minutes": ["欄位型態不正確"]}),
        ("PUT", "/api/settings/login-lockout", {"enabled": True, "window_minutes": 61, "failure_threshold": 101, "lock_seconds": 86401}, 422, "E0001", {"window_minutes": ["欄位格式或範圍不正確"], "failure_threshold": ["欄位格式或範圍不正確"], "lock_seconds": ["欄位格式或範圍不正確"]}),
    ):
        parity_error(method, path, body, admin["token"], admin_laravel, status, code, fields)
    # 身分先於無效參數；管理員 ID 對管理讀取及狀態修改都隱藏。
    for method, path, body in (("GET", "/api/members?bad=1", None),
                               ("GET", "/api/members/bad?bad=1", None),
                               ("PUT", "/api/members/bad/status", {"status": "bad"}),
                               ("GET", "/api/comments?bad=1", None),
                               ("GET", "/api/settings/login-lockout?bad=1", None),
                               ("PUT", "/api/settings/login-lockout", {"bad": 1})):
        expect(call(NESTJS, method, path, body), 401, "E0002")
        expect(call(NESTJS, method, path, body, member["token"]), 403, "E0003")
    for path in (f"/api/members/{admin['id']}", "/api/members/9007199254740991"):
        expect(call(NESTJS, "GET", path, token=admin["token"]), 404, "E0004")
    expect(call(NESTJS, "PUT", f"/api/members/{admin['id']}/status", {"status": "disabled"}, admin["token"]), 404, "E0004")
    # 精確 errors 與搜尋：短字、ASCII 折疊、Unicode、萬用符號、NUL、超頁。
    expect(call(NESTJS, "GET", "/api/members?page=1&per_page=20&filters[keyword][]=x", token=admin["token"]), 422, "E0001", {"filters.keyword": ["欄位型態不正確"]})
    expect(call(NESTJS, "PUT", "/api/settings/login-lockout", {"enabled": "true", "window_minutes": 0, "failure_threshold": 5, "lock_seconds": 300}, admin["token"]), 422, "E0001", {"enabled": ["欄位型態不正確"], "window_minutes": ["欄位格式或範圍不正確"]})
    for nickname in ("Aa%_\\Ä陳" + nonce, "aa%_\\ä陳" + nonce):
        email = f"t42-{nonce}-search{len(nickname.encode())}{secrets.token_hex(2)}@example.test"
        expect(call(NESTJS, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": nickname}), 201)
    # 一、二碼搜尋比對新增前後精確集合，重跑時也不假設隔離資料卷為空。
    short = ("𒀀", "𒀀𒀁")
    before_short = [expect(call(NESTJS, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(word), token=admin["token"]), 200) for word in short]
    short_email = f"t42-{nonce}-short@example.test"
    expect(call(NESTJS, "POST", "/api/auth/register", {"email": short_email, "password": "Abcd1234", "nickname": short[1] + nonce}), 201)
    short_id = int(sql(f"SELECT id FROM accounts WHERE email='{short_email}'"))
    for word, before in zip(short, before_short):
        after = expect(call(NESTJS, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(word), token=admin["token"]), 200)
        check(after["pagination"]["total"] == before["pagination"]["total"] + 1 and
              set(row["id"] for row in after["list"]) == set(row["id"] for row in before["list"]) | {short_id}, "一／二碼搜尋集合錯誤")
    for keyword, expected in (("aA%_\\Ä陳" + nonce, 1), ("aa%_\\ä陳" + nonce, 1), ("ä陳" + nonce, 1), ("\0", 0)):
        path = "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(keyword)
        data = expect(call(NESTJS, "GET", path, token=admin["token"]), 200)
        check(data["pagination"]["total"] == expected and len(data["list"]) == expected, "搜尋結果或 total 錯誤：" + repr(keyword))
    page = expect(call(NESTJS, "GET", "/api/members?page=1&per_page=100", token=admin["token"]), 200)
    check(all(row["role"] == "member" and set(row) == {"id", "email", "nickname", "role", "status", "created_at"} for row in page["list"]), "會員列表範圍／欄位錯誤")
    deep = expect(call(NESTJS, "GET", "/api/members?page=999999&per_page=1", token=admin["token"]), 200)
    check(deep["list"] == [] and deep["pagination"]["total"] == page["pagination"]["total"] and deep["pagination"]["page"] == 999999, "超頁總數錯誤")
    check(expect(call(NESTJS, "GET", f"/api/members/{member['id']}", token=admin["token"]), 200)["email"] == member["email"], "會員詳情錯誤")
    # 搜尋 oracle 由完整 DB 資料與獨立字面規則計算，兩後端每頁 ID/總數須一致。
    for term in ("𒀀", "𒀀𒀁", "a", "aa", "Aa%_\\Ä陳" + nonce, "Ä陳", "ä陳", "%", "_", "\\", "\0", "  ", "no-match-" + nonce):
        expected = expected_members(term)
        path = "/api/members?filters%5Bkeyword%5D=" + quote(term, safe="")
        listed(NESTJS, path, admin["token"], expected)
        listed(LARAVEL, path, admin_laravel, expected)
    all_members = expected_members("")
    listed(NESTJS, "/api/members", admin["token"], all_members)
    listed(LARAVEL, "/api/members", admin_laravel, all_members)
    print("PASS Ticket 42 member search exact ID sets, totals, and validation parity")
    # 建立與下架內容，驗證管理文章範圍及全部留言都可讀。
    title = "ticket42-" + nonce
    expect(call(NESTJS, "POST", "/api/articles", {"title": title, "body": "本文"}, member["token"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE title='{title}' AND author_id={member['id']}"))
    expect(call(NESTJS, "POST", f"/api/articles/{article}/comments", {"body": "下架父文留言"}, member["token"]), 201)
    comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body='下架父文留言'"))
    # 舊 01–16 操作逐一路過至少一個契約拒絕，避免新管理路由改動共同驗證順序。
    expect(call(NESTJS, "POST", "/api/auth/register", {"email": "bad@example.test", "nickname": "測試"}), 422, "E0001", {"password": ["此欄位為必填"]})
    expect(call(NESTJS, "POST", "/api/auth/login", {"email": member["email"]}), 422, "E0001", {"password": ["此欄位為必填"]})
    for method, path, body in (("POST", "/api/auth/logout", None), ("GET", "/api/me", None),
                               ("POST", "/api/articles", {"title": "拒絕", "body": "拒絕"}),
                               ("PUT", f"/api/articles/{article}", {"title": title, "body": "本文", "version": 1}),
                               ("GET", "/api/me/articles?page=1&per_page=20", None),
                               ("GET", "/api/me/comments?page=1&per_page=20", None),
                               ("PUT", f"/api/articles/{article}/visibility", {"visibility": "hidden", "version": 1}),
                               ("POST", f"/api/articles/{article}/comments", {"body": "拒絕"}),
                               ("GET", f"/api/comments/{comment}", None),
                               ("PUT", f"/api/comments/{comment}", {"body": "拒絕", "version": 1}),
                               ("PUT", f"/api/comments/{comment}/visibility", {"visibility": "hidden", "version": 1})):
        expect(call(NESTJS, method, path, body), 401, "E0002")
    expect(call(NESTJS, "GET", "/api/articles/bad"), 422, "E0001", {"article_id": ["欄位格式或範圍不正確"]})
    expect(call(NESTJS, "GET", f"/api/articles/{article}/comments?page=bad&per_page=20"), 422, "E0001", {"page": ["欄位格式或範圍不正確"]})
    # 同一隔離來源依公開契約逐一走過既有操作 03–16 的正向路徑。
    check(article in [row["id"] for row in expect(call(NESTJS, "GET", "/api/articles?page=1&per_page=100"), 200)["list"]], "公開文章列表缺新文章")
    check(expect(call(NESTJS, "GET", f"/api/articles/{article}"), 200)["title"] == title, "文章詳情錯誤")
    expect(call(NESTJS, "PUT", f"/api/articles/{article}", {"title": title, "body": "本文", "version": 1}, member["token"]), 200)
    check(article in [row["id"] for row in expect(call(NESTJS, "GET", "/api/me/articles?page=1&per_page=100", token=member["token"]), 200)["list"]], "我的文章缺新文章")
    check(comment in [row["id"] for row in expect(call(NESTJS, "GET", "/api/me/comments?page=1&per_page=100", token=member["token"]), 200)["list"]], "我的留言缺新留言")
    check(comment in [row["id"] for row in expect(call(NESTJS, "GET", f"/api/articles/{article}/comments?page=1&per_page=100"), 200)["list"]], "文章留言缺新留言")
    check(expect(call(NESTJS, "GET", f"/api/comments/{comment}", token=member["token"]), 200)["body"] == "下架父文留言", "留言詳情錯誤")
    expect(call(NESTJS, "PUT", f"/api/comments/{comment}", {"body": "下架父文留言", "version": 1}, member["token"]), 200)
    expect(call(NESTJS, "PUT", f"/api/comments/{comment}/visibility", {"visibility": "hidden", "version": 1}, member["token"]), 200)
    expect(call(NESTJS, "PUT", f"/api/comments/{comment}/visibility", {"visibility": "visible", "version": 2}, member["token"]), 200)
    spare = expect(call(NESTJS, "POST", "/api/auth/login", {"email": other["email"], "password": "Abcd1234"}), 200)["access_token"]
    expect(call(NESTJS, "POST", "/api/auth/logout", token=spare), 200)
    expect(call(NESTJS, "GET", "/api/me", token=spare), 401, "E0002")
    expect(call(NESTJS, "PUT", f"/api/articles/{article}/visibility", {"visibility": "hidden", "version": 1}, member["token"]), 200)
    expect(call(NESTJS, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management"), 401, "E0002")
    expect(call(NESTJS, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=member["token"]), 403, "E0003")
    managed = expect(call(NESTJS, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=admin["token"]), 200)
    check(article in [row["id"] for row in managed["list"]], "管理文章漏下架資料")
    comments = expect(call(NESTJS, "GET", "/api/comments?page=1&per_page=100", token=admin["token"]), 200)
    check(comment in [row["id"] for row in comments["list"]], "管理留言漏下架父文資料")
    article_row = next(row for row in managed["list"] if row["id"] == article)
    comment_row = next(row for row in comments["list"] if row["id"] == comment)
    check(article_row["created_backend"] == "nestjs" and comment_row["created_backend"] == "nestjs" and
          article_row["visibility"] == "hidden" and comment_row["author"]["id"] == member["id"] and
          set(comment_row) == {"id", "body", "author", "article_id", "version", "created_backend", "created_at", "updated_at", "visibility", "can_restore"},
          "管理內容來源／欄位／隱藏狀態不符")
    # 管理分支必須包含 hidden 並依全域 ID 集合精確分頁；公開分支排除 hidden。
    article_ids = [int(value) for value in sql("SELECT id FROM articles ORDER BY created_at DESC,id DESC").splitlines()]
    comment_ids = [int(value) for value in sql("SELECT id FROM comments ORDER BY created_at DESC,id DESC").splitlines()]
    public_ids = [int(value) for value in sql("SELECT id FROM articles WHERE visibility='visible' ORDER BY created_at DESC,id DESC").splitlines()]
    for base, token in ((NESTJS, admin["token"]), (LARAVEL, admin_laravel)):
        listed(base, "/api/articles?filters[scope]=management", token, article_ids)
        listed(base, "/api/comments", token, comment_ids)
        listed(base, "/api/articles", token, public_ids)
    # 同值狀態不撤銷；實際停用撤銷兩個後端 session，恢復後舊憑證不復活。
    expect(call(NESTJS, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin["token"]), 200)
    expect(call(NESTJS, "GET", "/api/me", token=member["token"]), 200)
    expect(call(NESTJS, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin["token"]), 200)
    expect(call(NESTJS, "GET", "/api/me", token=member["token"]), 403, "E0102")
    expect(call(LARAVEL, "GET", "/api/me", token=member_laravel), 403, "E0102")
    expect(call(LARAVEL, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin_laravel), 200)
    expect(call(NESTJS, "GET", "/api/me", token=member["token"]), 401, "E0002")
    check(sql(f"SELECT status FROM accounts WHERE id={member['id']}") == "active" and sql(f"SELECT title FROM articles WHERE id={article}") == title, "恢復或內容持久狀態錯誤")
    # Laravel 的失敗事件由 NestJS 讀取；只有實際關閉才清鎖與計數，數值變更保留期限。
    try:
        enabled = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 300}
        expect(call(NESTJS, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        check(settings(LARAVEL, admin_laravel) == enabled, "NestJS 設定未跨後端生效")
        # 先後排隊兩個完整設定寫入；共同設定列只允許最後提交的四欄一起保留。
        first = {**enabled, "failure_threshold": 3}
        last = {**enabled, "failure_threshold": 4, "lock_seconds": 600}
        holder = locked_sql("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                nestjs_put = pool.submit(call, NESTJS, "PUT", "/api/settings/login-lockout", first, admin["token"])
                wait_lock()
                laravel_put = pool.submit(call, LARAVEL, "PUT", "/api/settings/login-lockout", last, admin_laravel)
                wait_lock(2)
                finish_lock(holder)
                expect(nestjs_put.result(), 200)
                expect(laravel_put.result(), 200)
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.communicate()
        check(settings(NESTJS, admin["token"]) == last and settings(LARAVEL, admin_laravel) == last, "設定競爭混合欄位或順序錯誤")
        expect(call(NESTJS, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": other["email"], "password": "Wrong1234"}), 401, "E0101")
        locked = call(NESTJS, "POST", "/api/auth/login", {"email": other["email"], "password": "Wrong1234"})
        expect(locked, 429, "E0105")
        check(1 <= int(locked[2]["Retry-After"]) <= 300 and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={other['id']}") == "2", "Laravel→NestJS 錯誤計數或倒數不符")
        # 反向用新帳號重播，避免前一帳號的鎖定影響同條件比較。
        reverse = create_user(nonce + "-reverse")
        expect(call(NESTJS, "POST", "/api/auth/login", {"email": reverse["email"], "password": "Wrong1234"}), 401, "E0101")
        reverse_locked = call(LARAVEL, "POST", "/api/auth/login", {"email": reverse["email"], "password": "Wrong1234"})
        expect(reverse_locked, 429, "E0105")
        check(1 <= int(reverse_locked[2]["Retry-After"]) <= 300 and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={reverse['id']}") == "2", "NestJS→Laravel 錯誤計數或倒數不符")
        # 寫入已提交的視窗兩側事件；下一次真登入交易分別應為 401 與 429。
        outside = create_user(nonce + "-outside")
        inside = create_user(nonce + "-inside")
        for account, displacement in ((outside, "- interval '3 seconds'"), (inside, "+ interval '3 seconds'")):
            sql(f"INSERT INTO login_failures(account_id,failure_epoch,occurred_at) "
                f"SELECT id,failure_epoch,clock_timestamp()-interval '5 minutes' {displacement} FROM accounts WHERE id={account['id']}")
        expect(call(NESTJS, "POST", "/api/auth/login", {"email": outside["email"], "password": "Wrong1234"}), 401, "E0101")
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={outside['id']}") == "t", "視窗左外事件不應鎖定")
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": inside["email"], "password": "Wrong1234"}), 429, "E0105")
        check(sql(f"SELECT locked_until > clock_timestamp() FROM accounts WHERE id={inside['id']}") == "t", "視窗左內事件應鎖定")
        exact = sql("WITH t AS (SELECT clock_timestamp() AS stamp), events AS (SELECT stamp-interval '5 minutes' "
                    "AS occurred_at FROM t UNION ALL SELECT stamp-interval '5 minutes'+interval '1 microsecond' "
                    "FROM t UNION ALL SELECT stamp FROM t) SELECT count(*) FROM events,t WHERE "
                    "occurred_at>t.stamp-interval '5 minutes' AND occurred_at<=t.stamp")
        check(exact == "2", "視窗開左閉右 SQL 精確邊界不符")
        until = sql(f"SELECT locked_until::text FROM accounts WHERE id={other['id']}")
        changed = {**enabled, "lock_seconds": 600}
        expect(call(NESTJS, "PUT", "/api/settings/login-lockout", changed, admin["token"]), 200)
        check(sql(f"SELECT locked_until::text FROM accounts WHERE id={other['id']}") == until, "數值變更改動既有期限")
        widened_user = create_user(nonce + "-widened")
        sql(f"INSERT INTO login_failures(account_id,failure_epoch,occurred_at) "
            f"SELECT id,failure_epoch,clock_timestamp()-interval '6 minutes' FROM accounts WHERE id={widened_user['id']}")
        widened = {**changed, "window_minutes": 7}
        expect(call(LARAVEL, "PUT", "/api/settings/login-lockout", widened, admin_laravel), 200)
        expect(call(NESTJS, "POST", "/api/auth/login", {"email": widened_user["email"], "password": "Wrong1234"}), 429, "E0105")
        check(sql(f"SELECT locked_until > clock_timestamp() FROM accounts WHERE id={widened_user['id']}") == "t", "調大視窗未重算舊事件")
        # 以資料庫實際時間設邊界：NestJS 在鎖內回 429，Laravel 到期後能登入並清鎖。
        boundary = create_user(nonce + "-boundary")
        sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '3 seconds' WHERE id={boundary['id']}")
        expect(call(NESTJS, "POST", "/api/auth/login", {"email": boundary["email"], "password": "Abcd1234"}), 429, "E0105")
        deadline = time.monotonic() + 5
        while sql(f"SELECT clock_timestamp() >= locked_until FROM accounts WHERE id={boundary['id']}") != "t":
            check(time.monotonic() < deadline, "資料庫鎖定期限未到")
            time.sleep(0.05)
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": boundary["email"], "password": "Abcd1234"}), 200)
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={boundary['id']}") == "t", "跨後端到期未清鎖")
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}"))
        disabled = {**changed, "enabled": False}
        expect(call(NESTJS, "PUT", "/api/settings/login-lockout", disabled, admin["token"]), 200)
        check(settings(LARAVEL, admin_laravel) == disabled and sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={other['id']}") == "t", "關閉未清鎖")
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}")) == epoch + 1, "關閉未清計數世代")
        expect(call(NESTJS, "PUT", "/api/settings/login-lockout", disabled, admin["token"]), 200)
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}")) == epoch + 1, "同值關閉重做副作用")
    finally:
        expect(call(NESTJS, "PUT", "/api/settings/login-lockout", original, admin["token"]), 200)
    # 先排隊停用，再送 Laravel 寫入；帳號列鎖釋放後舊登入不能寫入。
    fresh = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    race_title = "ticket42-race-" + nonce
    holder = locked_sql(f"SELECT id FROM accounts WHERE id={member['id']} FOR UPDATE")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            disable = pool.submit(call, NESTJS, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin["token"])
            wait_lock()
            write = pool.submit(call, LARAVEL, "POST", "/api/articles", {"title": race_title, "body": "不可提交"}, fresh)
            wait_lock(2)
            finish_lock(holder)
            expect(disable.result(), 200)
            expect(write.result(), 403, "E0102")
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate()
    check(sql(f"SELECT count(*) FROM articles WHERE title='{race_title}'") == "0", "停用後的 Laravel 寫入仍提交")
    expect(call(NESTJS, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin["token"]), 200)
    print("PASS Ticket 42 NestJS 管理 HTTP/SQL 驗收")


if __name__ == "__main__":
    main()
