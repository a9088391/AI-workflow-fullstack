"""Ticket 30：只對 ticket30_test 隔離資料庫執行 Echo 管理 HTTP/SQL 驗收。"""

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local/ticket30-backend"
ECHO = "http://localhost:18094"
LARAVEL = "http://localhost:18093"
# check 讓第一個契約偏差立即中止，避免後續寫入掩蓋原因。
def check(condition, message):
    if not condition:
        raise AssertionError(message)


# command 僅執行固定參數的 Docker 指令，失敗時保留原始診斷。
def command(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


# sql 僅對守衛確認的隔離資料庫執行測試 SQL，任何 SQL 錯誤立即中止。
def sql(query):
    return command("docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
                   "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c", query)


# literal 僅將本腳本產生的 fixture 文字轉為 SQL 字面值。
def literal(value):
    return "'" + value.replace("'", "''") + "'"


# prepare_reuse 建立可稽核的隔離回歸副本，避免修改原測試。
# 副本調整專案、埠與根路徑，並移除 Ticket28 對已實作路由的兩個過時斷言。
def prepare_reuse():
    destination = LOCAL / "suites"
    destination.mkdir(parents=True, exist_ok=True)
    replacements = {
        "ticket28_test": "ticket30_test", "ticket28_compose.yaml": "ticket30_compose.yaml",
        "ticket28-backend": "ticket30-backend", "TICKET28_FIXTURE": "TICKET30_FIXTURE",
        "isolated-ticket28": "isolated-ticket30", "18091": "18093", "18090": "18092",
        "8088": "18094",
    }
    for name in ("ticket28_http.py", "ticket29_http.py"):
        source = (ROOT / "ops/tests" / name).read_bytes()
        content = source.decode()
        for old, new in replacements.items():
            content = content.replace(old, new)
        if name == "ticket28_http.py":
            # Ticket 29 已實作這兩個舊票當時尚未支援的路由；其餘斷言原樣保留。
            obsolete = (
                '    expect(call(ECHO, "POST", "/api/articles", {"title": "尚未支援"}), 405, "E0005")\n',
                '    expect(call(ECHO, "GET", "/api/me/articles?page=1&per_page=20", token=author), 404, "E0004")\n',
            )
            for assertion in obsolete:
                check(content.count(assertion) == 1, "舊票斷言位置已變，須重新確認改版範圍")
                content = content.replace(assertion, "")
        if name == "ticket29_http.py":
            original_root = "os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))"
            copied_root = "os.path.dirname(" + original_root + ")"
            check(content.count(original_root) == 1, "搬移副本後的專案根路徑已變")
            content = content.replace(original_root, copied_root)
        target = destination / name
        target.write_text(content)
        print(f"REUSE {name} source_sha256={hashlib.sha256(source).hexdigest()} "
              f"copy_sha256={hashlib.sha256(content.encode()).hexdigest()}")
    return destination


# guard_backend 核對目前容器的專案、健康、loopback 埠、金鑰及 image。
def guard_backend(service, port):
    ids = command("docker", "compose", "--profile", "echo", "ps", "-q", service).splitlines()
    check(len(ids) == 1, f"{service} 容器未唯一啟動")
    info = json.loads(command("docker", "inspect", ids[0]))[0]
    check(info["Config"]["Labels"].get("com.docker.compose.project") == "ticket30_test"
          and info["Config"]["Labels"].get("com.docker.compose.service") == service, f"{service} 專案不符")
    check(info["State"]["Running"] and info["State"].get("Health", {}).get("Status") == "healthy",
          f"{service} 未健康執行")
    check(info["HostConfig"]["PortBindings"].get("8000/tcp") ==
          [{"HostIp": "127.0.0.1", "HostPort": port}], f"{service} 實際埠不符")
    check(any(m.get("Type") == "bind" and os.path.realpath(m.get("Source", "")) ==
              os.path.realpath(LOCAL / f"{service}.key") and
              m.get("Destination") == f"/run/secrets/{service}-jwt.key" and m.get("RW") is False
              for m in info["Mounts"]), f"{service} 金鑰掛載不符")
    built = command("docker", "compose", "--profile", "echo", "images", "-q", service)
    check(info["Image"] == "sha256:" + built, f"{service} 執行 image 非目前 build")


# guard 在任何 HTTP/SQL 寫入前驗證設定與實際容器，拒絕指向展示環境。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket30_test", "錯誤 Compose project")
    check(os.environ.get("COMPOSE_FILE") == "compose.yaml:ops/tests/ticket30_compose.yaml", "錯誤 Compose 檔")
    config = json.loads(command("docker", "compose", "--profile", "echo", "config", "--format", "json"))
    check(config["name"] == "ticket30_test" and
          config["volumes"]["postgres_data"]["name"] == "ticket30_test_postgres_data", "資料卷不符")
    check(config["services"]["db"]["environment"]["TICKET30_FIXTURE"] == "isolated-ticket30",
          "fixture 標記不符")
    for service, port in (("laravel", "18093"), ("echo", "18094"), ("nginx", "18092")):
        published = config["services"][service]["ports"][0]
        check(published["published"] == port and published["host_ip"] == "127.0.0.1",
              f"{service} 設定埠不符")
    cors_source = os.path.realpath(LOCAL / "cors.php")
    cors_destination = "/srv/laravel/config/cors.php"
    cors_volumes = [mount for mount in config["services"]["laravel"]["volumes"]
                    if mount["target"] == cors_destination]
    check(len(cors_volumes) == 1 and cors_volumes[0]["type"] == "bind" and
          os.path.realpath(cors_volumes[0]["source"]) == cors_source and
          cors_volumes[0]["read_only"] is True, "Laravel CORS 測試設定未唯讀掛載")
    db_ids = command("docker", "compose", "--profile", "echo", "ps", "-q", "db").splitlines()
    check(len(db_ids) == 1, "DB 容器未唯一啟動")
    db = json.loads(command("docker", "inspect", db_ids[0]))[0]
    check(db["Config"]["Labels"].get("com.docker.compose.project") == "ticket30_test" and
          db["State"]["Running"] and db["State"].get("Health", {}).get("Status") == "healthy" and
          any(m.get("Name") == "ticket30_test_postgres_data" for m in db["Mounts"]), "DB 實際身分不符")
    for service, port in (("laravel", "18093"), ("echo", "18094")):
        guard_backend(service, port)
        check((LOCAL / f"{service}.key").read_bytes() != (ROOT / f".local/keys/{service}.key").read_bytes(),
              f"{service} 共用展示金鑰")
    laravel_id = command("docker", "compose", "--profile", "echo", "ps", "-q", "laravel")
    laravel = json.loads(command("docker", "inspect", laravel_id))[0]
    check(any(mount.get("Type") == "bind" and
              os.path.realpath(mount.get("Source", "")) == cors_source and
              mount.get("Destination") == cors_destination and mount.get("RW") is False
              for mount in laravel["Mounts"]), "Laravel 實際 CORS 掛載不符")
    # 以瀏覽器相同 Origin 讀公開 API，確認容器實際回傳允許的 CORS Origin。
    request = Request(LARAVEL + "/api/articles?page=1&per_page=20",
                      headers={"Origin": "http://localhost:18092", "Accept": "application/json"})
    with urlopen(request, timeout=5) as response:
        check(response.status == 200 and
              response.headers.get("Access-Control-Allow-Origin") == "http://localhost:18092",
              "Laravel 公開 API 未允許隔離前端 Origin")
    nginx_ids = command("docker", "compose", "--profile", "echo", "ps", "-q", "nginx").splitlines()
    check(len(nginx_ids) == 1, "NGINX 容器未唯一啟動")
    nginx = json.loads(command("docker", "inspect", nginx_ids[0]))[0]
    check(nginx["Config"]["Labels"].get("com.docker.compose.project") == "ticket30_test" and
          nginx["Config"]["Labels"].get("com.docker.compose.service") == "nginx" and
          nginx["State"].get("Health", {}).get("Status") == "healthy" and
          nginx["HostConfig"]["PortBindings"].get("80/tcp") ==
          [{"HostIp": "127.0.0.1", "HostPort": "18092"}], "NGINX 實際身分／埠不符")
    vue_ids = command("docker", "compose", "--profile", "echo", "ps", "-q", "frontend-vue").splitlines()
    check(len(vue_ids) == 1, "Vue 容器未唯一啟動")
    vue = json.loads(command("docker", "inspect", vue_ids[0]))[0]
    built_vue = command("docker", "compose", "--profile", "echo", "images", "-q", "frontend-vue")
    check(vue["Config"]["Labels"].get("com.docker.compose.project") == "ticket30_test" and
          vue["Config"]["Labels"].get("com.docker.compose.service") == "frontend-vue" and
          vue["State"].get("Health", {}).get("Status") == "healthy" and
          vue["Image"] == "sha256:" + built_vue and
          config["services"]["frontend-vue"]["build"]["context"] ==
          str(ROOT / ".local/ticket30-frontend"), "Vue 執行 image／來源不符")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 角色不符")
    auth = (ROOT / ".local/ticket30-frontend/src/auth.js").read_text()
    check("http://localhost:18093" in auth and "http://localhost:18094" in auth and
          "http://localhost:8081" not in auth and "http://localhost:8088" not in auth, "Vue 副本 API 位址不安全")
    with urlopen("http://localhost:18092/vue/", timeout=5) as response:
        html = response.read().decode()
    scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
    check(bool(scripts), "NGINX 未提供 Vue bundle")
    for script in scripts:
        with urlopen(urljoin("http://localhost:18092/vue/", script), timeout=5) as response:
            bundle = response.read().decode()
        check("http://localhost:18093" in bundle and "http://localhost:18094" in bundle and
              "http://localhost:8081" not in bundle and "http://localhost:8088" not in bundle,
              "Vue bundle API 位址不安全")


# fixture 以正式註冊／登入建立本次唯一帳號，僅在隔離資料庫賦予管理角色。
def fixture(api, tag, nickname, admin=False):
    email = f"t30-{tag}-{secrets.token_hex(4)}@example.test"
    api.expect(api.call(ECHO, "POST", "/api/auth/register",
                        {"email": email, "password": "Abc1234", "nickname": nickname}), 201)
    data = api.expect(api.call(ECHO, "POST", "/api/auth/login",
                               {"email": email, "password": "Abc1234"}), 200)
    account_id = data["user"]["id"]
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE id={int(account_id)}")
    return {"id": account_id, "email": email, "token": data["access_token"]}


# expected_members 在測試端獨立計算 ASCII 摺疊與字面包含，校對完整 ID 與 total。
def expected_members(keyword):
    alphabet = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    folded = keyword.translate(alphabet)
    rows = sql("SELECT row_to_json(t) FROM (SELECT id,email,nickname FROM accounts WHERE role='member') t")
    return {item["id"] for item in (json.loads(line) for line in rows.splitlines())
            if folded in item["email"].translate(alphabet) or
            folded in item["nickname"].translate(alphabet)}


# measured 以隔離 PostgreSQL 原生日誌記錄單次 HTTP 路徑實際 SQL 數量。
def measured(api, path, token):
    since = datetime.now(timezone.utc).isoformat()
    result = api.call(ECHO, "GET", path, token=token)
    db_id = command("docker", "compose", "--profile", "echo", "ps", "-q", "db")
    output = subprocess.run(("docker", "logs", "--since", since, db_id), cwd=ROOT,
                            capture_output=True, text=True, check=True)
    logs = output.stdout + output.stderr
    # Laravel 使用 PDO、Echo 健康檢查送固定 SELECT 1；兩者都不是本次 API 查詢。
    queries = [line for line in logs.splitlines() if "articles_app@articles" in line and
               "duration:" in line and " execute stmtcache_" in line and
               "SELECT 1 FROM accounts,login_sessions,login_failures,login_lockout_settings,articles,comments LIMIT 1" not in line]
    print(f"QUERY_COUNT path={path} count={len(queries)} statements={queries}")
    return result, queries


# plan 執行與已讀取的 Echo SQL 相同的 WHERE/ORDER/LIMIT，保存隔離 fixture 的實際計畫。
def plan(name, query):
    print(f"PLAN {name}:\n{sql('EXPLAIN (ANALYZE, BUFFERS) ' + query)}")


# main 先做 fail-closed 守衛，再驗證管理範圍、設定副作用與跨後端登入。
def main():
    os.chdir(ROOT)
    check(len(sys.argv) <= 2 and (len(sys.argv) == 1 or sys.argv[1] in
          ("--guard-only", "--prepare-reuse")), "不支援此參數")
    if len(sys.argv) == 2 and sys.argv[1] == "--prepare-reuse":
        prepare_reuse()
        return
    guard()
    if len(sys.argv) == 2:
        print("PASS ticket30_test guard")
        return
    suites = prepare_reuse()
    sys.path.insert(0, str(suites))
    api = importlib.import_module("ticket28_http")
    regress = importlib.import_module("ticket29_http")
    api.guard()
    nonce = secrets.token_hex(4)
    admin = fixture(api, nonce + "-admin", "管理員", True)
    member = fixture(api, nonce + "-member", "A_B%\\É中")
    second = fixture(api, nonce + "-second", "普通會員é")
    wildcard = fixture(api, nonce + "-wildcard", "A1B")
    members = "/api/members?page=1&per_page=20"
    # 非管理員在無效格式前被拒；管理員才能看 active/disabled 的一般會員。
    api.expect(api.call(ECHO, "GET", "/api/members/nope?x=1", token=member["token"]), 403, "E0003")
    api.expect(api.call(ECHO, "GET", "/api/comments?page=bad&per_page=20", token=member["token"]), 403, "E0003")
    api.expect(api.call(ECHO, "GET", members), 401, "E0002")
    api.expect(api.call(ECHO, "GET", f"/api/members/{admin['id']}", token=admin["token"]), 404, "E0004")
    api.expect(api.call(ECHO, "GET", "/api/members/9007199254740991", token=admin["token"]), 404, "E0004")
    sql(f"UPDATE accounts SET status='disabled' WHERE id={second['id']}")
    detail = api.expect(api.call(ECHO, "GET", f"/api/members/{second['id']}", token=admin["token"]), 200)
    check(set(detail) == {"id", "email", "nickname", "role", "status", "created_at"} and
          detail["status"] == "disabled", "會員詳情範圍／欄位不符")
    all_members = api.expect(api.call(ECHO, "GET", "/api/members?page=1&per_page=100",
                                      token=admin["token"]), 200)
    listed = {item["id"]: item for item in all_members["list"]}
    check(member["id"] in listed and listed[member["id"]]["status"] == "active" and
          second["id"] in listed and listed[second["id"]]["status"] == "disabled" and
          admin["id"] not in listed and all(item["role"] == "member" for item in listed.values()) and
          all_members["pagination"]["total"] == int(sql("SELECT count(*) FROM accounts WHERE role='member'")),
          "會員列表 active/disabled/admin 範圍或 total 不符")
    for keyword, expected in (("a_b", member["id"]), ("%", member["id"]),
                              ("\\", member["id"]), ("É", member["id"]),
                              ("中", member["id"]), ("é", second["id"])):
        path = members + "&filters%5Bkeyword%5D=" + quote(keyword, safe="")
        page = api.expect(api.call(ECHO, "GET", path, token=admin["token"]), 200)
        all_expected = expected_members(keyword)
        check(expected in all_expected and wildcard["id"] not in all_expected and
              {item["id"] for item in page["list"]} == all_expected and
              page["pagination"]["total"] == len(all_expected), f"搜尋字面比對不符：{keyword}")
    # 短字、多結果、Unicode 精確與 NUL 分別阻止 LIKE 萬用及 Unicode casefold 誤判。
    for keyword, expected in (("É中", member["id"]),
                              ("ÉÉ", None), ("Ｅ", None), ("\x00", None)):
        path = members + "&filters%5Bkeyword%5D=" + quote(keyword, safe="")
        page = api.expect(api.call(ECHO, "GET", path, token=admin["token"]), 200)
        all_expected = expected_members(keyword)
        check((expected is None or expected in all_expected) and
              {item["id"] for item in page["list"]} == all_expected and
              page["pagination"]["total"] == len(all_expected), f"搜尋集合不符：{keyword!r}")
    deep = api.expect(api.call(ECHO, "GET", "/api/members?page=9007199254740991&per_page=100",
                               token=admin["token"]), 200)
    check(deep["list"] == [] and deep["pagination"]["page"] == 9007199254740991,
          "深頁查詢不符")
    api.expect(api.call(ECHO, "GET", members + "&page=2", token=admin["token"]), 422,
               "E0001", {"page": ["此參數不可重複"]})
    api.expect(api.call(ECHO, "GET", members + "&filters%5Bkeyword%5D%5B%5D=x",
                        token=admin["token"]), 422, "E0001", {"filters.keyword": ["欄位型態不正確"]})
    api.expect(api.call(ECHO, "GET", members, body="{}", token=admin["token"]), 422,
               "E0001", {"_body": ["此 API 不接受 Request Body"]})
    print("PASS member scope/search/validation")

    # 管理文章與留言包含下架資料；作者與訪客的列表仍遵守各自範圍。
    title = "Ticket30 " + nonce
    api.expect(api.call(ECHO, "POST", "/api/articles", {"title": title, "body": "本文"},
                        member["token"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE author_id={member['id']} AND title={literal(title)}"))
    body = "Ticket30 留言 " + nonce
    api.expect(api.call(ECHO, "POST", f"/api/articles/{article}/comments", {"body": body},
                        member["token"]), 201)
    comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body={literal(body)}"))
    hidden_body = "Ticket30 個別下架 " + nonce
    api.expect(api.call(ECHO, "POST", f"/api/articles/{article}/comments", {"body": hidden_body},
                        wildcard["token"]), 201)
    hidden_comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body={literal(hidden_body)}"))
    sql(f"UPDATE comments SET visibility='hidden',restore_control='admin' WHERE id={hidden_comment}")
    before_parent = api.expect(api.call(ECHO, "GET", "/api/comments?page=1&per_page=20",
                                        token=admin["token"]), 200)
    check({comment, hidden_comment} <= {item["id"] for item in before_parent["list"]},
          "管理留言漏掉公開父文下的個別下架留言")
    sql(f"UPDATE articles SET visibility='hidden',restore_control='admin' WHERE id={article}")
    management = api.expect(api.call(ECHO, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management",
                                     token=admin["token"]), 200)
    check(article in [item["id"] for item in management["list"]], "管理文章未包含下架資料")
    public = api.expect(api.call(ECHO, "GET", "/api/articles?page=1&per_page=20"), 200)
    check(article not in [item["id"] for item in public["list"]], "公開文章洩漏下架資料")
    api.expect(api.call(ECHO, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management",
                        token=member["token"]), 403, "E0003")
    comments = api.expect(api.call(ECHO, "GET", "/api/comments?page=1&per_page=20",
                                   token=admin["token"]), 200)
    listed_comments = {item["id"]: item for item in comments["list"]}
    check({comment, hidden_comment} <= set(listed_comments) and
          listed_comments[comment]["visibility"] == "visible" and
          listed_comments[hidden_comment]["visibility"] == "hidden" and
          listed_comments[hidden_comment]["can_restore"] is True and
          listed_comments[comment]["article_id"] == article,
          "管理留言未區分父文下架與個別下架")
    api.expect(api.call(ECHO, "GET", "/api/comments?page=1&per_page=20&filters%5Bkeyword%5D=x",
                        token=admin["token"]), 422, "E0001", {"filters.keyword": ["不支援此欄位"]})
    print("PASS management article/comment scope")

    # 同值狀態不撤銷；真正停用撤銷所有舊 session，恢復不復活且不改鎖與內容。
    status = f"/api/members/{member['id']}/status"
    api.expect(api.call(ECHO, "PUT", "/api/members/nope/status", {"status": False},
                        member["token"]), 403, "E0003")
    api.expect(api.call(ECHO, "PUT", f"/api/members/{admin['id']}/status",
                        {"status": "disabled"}, admin["token"]), 404, "E0004")
    api.expect(api.call(ECHO, "PUT", "/api/members/9007199254740991/status",
                        {"status": "disabled"}, admin["token"]), 404, "E0004")
    api.expect(api.call(ECHO, "PUT", status, {"status": True}, admin["token"]), 422,
               "E0001", {"status": ["欄位型態不正確"]})
    second_token = api.expect(api.call(LARAVEL, "POST", "/api/auth/login",
                                       {"email": member["email"], "password": "Abc1234"}), 200)["access_token"]
    before = sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL")
    api.expect(api.call(ECHO, "PUT", status, {"status": "active"}, admin["token"]), 200)
    check(sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL") == before,
          "同值狀態撤銷 session")
    sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '1 hour' WHERE id={member['id']}")
    locked_until = sql(f"SELECT locked_until FROM accounts WHERE id={member['id']}")
    content = sql(f"SELECT visibility FROM articles WHERE id={article}")
    api.expect(api.call(ECHO, "PUT", status, {"status": "disabled"}, admin["token"]), 200)
    check(sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NULL") == "0" and
          sql(f"SELECT locked_until FROM accounts WHERE id={member['id']}") == locked_until and
          sql(f"SELECT visibility FROM articles WHERE id={article}") == content, "停用副作用不符")
    api.expect(api.call(ECHO, "GET", "/api/me", token=member["token"]), 403, "E0102")
    api.expect(api.call(LARAVEL, "GET", "/api/me", token=second_token), 403, "E0102")
    api.expect(api.call(ECHO, "PUT", status, {"status": "active"}, admin["token"]), 200)
    api.expect(api.call(ECHO, "GET", "/api/me", token=member["token"]), 401, "E0002")
    api.expect(api.call(LARAVEL, "GET", "/api/me", token=second_token), 401, "E0002")
    print("PASS member status revoke/restore/content/lock")

    # 設定四欄完整更新；跨後端錯密碼共用事件，數值修改不改既有期限。
    settings = "/api/settings/login-lockout"
    defaults = {"enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 300}
    read = api.expect(api.call(ECHO, "GET", settings, token=admin["token"]), 200)
    check(set(read) == set(defaults) and isinstance(read["enabled"], bool) and
          all(type(read[key]) is int for key in ("window_minutes", "failure_threshold", "lock_seconds")),
          "設定欄位／型態不符")
    if read != defaults:
        # 舊回歸套件可能留有其他合法設定；透過管理 API 建立本案例前提並讀回確認。
        api.expect(api.call(ECHO, "PUT", settings, defaults, admin["token"]), 200)
        check(api.expect(api.call(ECHO, "GET", settings, token=admin["token"]), 200) == defaults,
              "設定前提未恢復為預設值")
    api.expect(api.call(ECHO, "PUT", settings, "{", wildcard["token"]), 403, "E0003")
    api.expect(api.call(ECHO, "GET", settings, token=wildcard["token"]), 403, "E0003")
    api.expect(api.call(ECHO, "GET", settings + "?x=1", token=admin["token"]), 422,
               "E0001", {"x": ["不支援此欄位"]})
    api.expect(api.call(ECHO, "GET", settings, body="{}", token=admin["token"]), 422,
               "E0001", {"_body": ["此 API 不接受 Request Body"]})
    api.expect(api.call(ECHO, "PUT", settings, "{", admin["token"]), 400, "E0005")
    for field, value in defaults.items():
        invalid = dict(defaults)
        invalid.pop(field)
        api.expect(api.call(ECHO, "PUT", settings, invalid, admin["token"]), 422,
                   "E0001", {field: ["此欄位為必填"]})
        invalid[field] = 1 if isinstance(value, bool) else str(value)
        api.expect(api.call(ECHO, "PUT", settings, invalid, admin["token"]), 422,
                   "E0001", {field: ["欄位型態不正確"]})
        invalid[field] = None
        api.expect(api.call(ECHO, "PUT", settings, invalid, admin["token"]), 422,
                   "E0001", {field: ["此欄位不可為 null"]})
    for field, value in (("window_minutes", 0), ("failure_threshold", 101), ("lock_seconds", 86401)):
        api.expect(api.call(ECHO, "PUT", settings, {**defaults, field: value}, admin["token"]),
                   422, "E0001", {field: ["欄位格式或範圍不正確"]})
    api.expect(api.call(ECHO, "PUT", settings, {**defaults, "extra": 1}, admin["token"]),
               422, "E0001", {"extra": ["不支援此欄位"]})
    first = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 120}
    api.expect(api.call(ECHO, "PUT", settings, first, admin["token"]), 200)
    lock_user = fixture(api, nonce + "-lock", "鎖定測試")
    wrong = {"email": lock_user["email"], "password": "Wrong123"}
    api.expect(api.call(ECHO, "POST", "/api/auth/login", wrong), 401, "E0101")
    locked = api.call(LARAVEL, "POST", "/api/auth/login", wrong)
    api.expect(locked, 429, "E0105")
    check(int(locked[2]["Retry-After"]) > 0 and
          sql(f"SELECT count(*) FROM login_failures WHERE account_id={lock_user['id']}") == "2",
          "跨後端鎖定／計數不符")
    until = sql(f"SELECT locked_until FROM accounts WHERE id={lock_user['id']}")
    changed = {**first, "lock_seconds": 600}
    api.expect(api.call(ECHO, "PUT", settings, changed, admin["token"]), 200)
    check(sql(f"SELECT locked_until FROM accounts WHERE id={lock_user['id']}") == until,
          "數值更新改了現有期限")
    before_epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={lock_user['id']}"))
    off = {**changed, "enabled": False}
    # 用隔離會員的 bigint 上限讓清除階段失敗，驗證四欄設定與既有鎖一同回滾。
    sql(f"UPDATE accounts SET failure_epoch=9223372036854775807 WHERE id={lock_user['id']}")
    failed = api.call(ECHO, "PUT", settings, off, admin["token"])
    api.expect(failed, 500, "E9999")
    check(api.expect(api.call(ECHO, "GET", settings, token=admin["token"]), 200) == changed and
          sql(f"SELECT locked_until FROM accounts WHERE id={lock_user['id']}") == until and
          sql(f"SELECT failure_epoch FROM accounts WHERE id={lock_user['id']}") == "9223372036854775807",
          "清除失敗未完整 rollback")
    sql(f"UPDATE accounts SET failure_epoch={before_epoch} WHERE id={lock_user['id']}")
    api.expect(api.call(ECHO, "PUT", settings, off, admin["token"]), 200)
    check(api.expect(api.call(LARAVEL, "GET", settings, token=
          api.expect(api.call(LARAVEL, "POST", "/api/auth/login",
                              {"email": admin['email'], "password": "Abc1234"}), 200)["access_token"]), 200) == off,
          "Laravel 未讀到完整設定")
    check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={lock_user['id']}") == "t" and
          int(sql(f"SELECT failure_epoch FROM accounts WHERE id={lock_user['id']}")) == before_epoch + 1,
          "關閉未原子清除鎖／世代")
    api.expect(api.call(ECHO, "PUT", settings, off, admin["token"]), 200)
    check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={lock_user['id']}")) == before_epoch + 1,
          "同值關閉重做副作用")
    api.expect(api.call(ECHO, "POST", "/api/auth/login", wrong), 401, "E0101")
    # 兩後端同時錯密碼，只能留下兩筆事件並在門檻當次鎖定。
    api.expect(api.call(ECHO, "PUT", settings, first, admin["token"]), 200)
    race_user = fixture(api, nonce + "-race", "並行登入")
    barrier = threading.Barrier(2)

    # concurrent_wrong 在同一同步點送出請求，防止串行測試假裝覆蓋競爭。
    def concurrent_wrong(base):
        barrier.wait(timeout=5)
        return api.call(base, "POST", "/api/auth/login",
                        {"email": race_user["email"], "password": "Wrong123"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(pool.map(concurrent_wrong, (ECHO, LARAVEL)))
    check(sorted(item[0] for item in race_results) == [401, 429] and
          sql(f"SELECT count(*) FROM login_failures WHERE account_id={race_user['id']}") == "2",
          "跨後端並行登入漏計或錯誤順序不符")
    api.expect(next(item for item in race_results if item[0] == 429), 429, "E0105")
    # 先持有設定列鎖，讓關閉與另一後端登入都確實等待；不預設兩者取得鎖的順序。
    transition = fixture(api, nonce + "-transition", "設定競爭")
    holder = regress.locked_sql("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE",
                                "SELECT 1", seconds=8)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            turning_off = pool.submit(api.call, ECHO, "PUT", settings, off, admin["token"])
            wrong_during_off = pool.submit(api.call, LARAVEL, "POST", "/api/auth/login",
                                           {"email": transition["email"], "password": "Wrong123"})
            regress.wait_lock(minimum=2)
            regress.finish_lock(holder)
            api.expect(turning_off.result(), 200)
            api.expect(wrong_during_off.result(), 401, "E0101")
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate()
    check(api.expect(api.call(ECHO, "GET", settings, token=admin["token"]), 200) == off and
          sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={transition['id']}") == "t" and
          sql(f"SELECT count(*) FROM login_failures WHERE account_id={transition['id']} "
              f"AND failure_epoch=(SELECT failure_epoch FROM accounts WHERE id={transition['id']})") == "0",
          "關閉與登入競爭留下目前世代計數／鎖定")
    # 兩份完整設定同時提交，最後讀回只能等於其中一份，不接受欄位混合。
    choices = ({"enabled": True, "window_minutes": 7, "failure_threshold": 11, "lock_seconds": 333},
               {"enabled": True, "window_minutes": 9, "failure_threshold": 13, "lock_seconds": 555})
    barrier = threading.Barrier(2)

    # concurrent_settings 讓兩個完整 PUT 重疊執行，交由設定列鎖序列化。
    def concurrent_settings(values):
        barrier.wait(timeout=5)
        return api.call(ECHO, "PUT", settings, values, admin["token"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        settings_results = list(pool.map(concurrent_settings, choices))
    for result in settings_results:
        api.expect(result, 200)
    check(api.expect(api.call(ECHO, "GET", settings, token=admin["token"]), 200) in choices,
          "最後完整提交值被欄位混合")
    api.expect(api.call(ECHO, "PUT", settings, defaults, admin["token"]), 200)
    print("PASS settings validation/rollback/cross-backend lock/concurrent writes")

    # 僅在隔離資料庫量測無篩選與搜尋的實際語句及查詢計畫。
    for path in (members, members + "&filters%5Bkeyword%5D=" + quote("é"),
                 "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management",
                 "/api/comments?page=1&per_page=20"):
        result, queries = measured(api, path, admin["token"])
        api.expect(result, 200)
        check(len(queries) == 4 and sum("SELECT count(*)" in query for query in queries) == 1 and
              sum("ORDER BY" in query for query in queries) == 1,
              "實際 SQL 應為身分兩筆、精確 count 一筆、列表一筆")
    count_sql = "SELECT count(*) FROM accounts WHERE role='member' AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'é')>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'é')>0)"
    list_sql = "SELECT id,email,nickname,role,status,created_at FROM accounts WHERE role='member' AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'é')>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'é')>0) ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET 0"
    sql("ANALYZE accounts; ANALYZE articles; ANALYZE comments")
    print("FIXTURE members=" + sql("SELECT count(*) FROM accounts WHERE role='member'") +
          " matching_e_acute=" + sql(count_sql) + " comments=" + sql("SELECT count(*) FROM comments"))
    plan("members_count_e_acute", count_sql)
    plan("members_list_e_acute", list_sql)
    plan("management_articles_count", "SELECT count(*) FROM articles JOIN accounts ON accounts.id=articles.author_id")
    plan("management_articles_list", "SELECT articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0")
    plan("comments_count", "SELECT count(*) FROM comments JOIN accounts ON accounts.id=comments.author_id")
    plan("comments_list", "SELECT comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname FROM comments JOIN accounts ON accounts.id=comments.author_id ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0")
    print("PASS Ticket 30 Echo administration HTTP/SQL")


if __name__ == "__main__":
    main()
