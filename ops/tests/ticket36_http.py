"""Ticket 36：先核對隔離目標，再測 Express 管理與跨 Laravel 狀態。"""

import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from urllib.request import urlopen

from ticket34_http import call, check, expect, sql

EXPRESS = "http://127.0.0.1:18363"
LARAVEL = "http://127.0.0.1:18362"
PROJECT = "ticket36_test"
COMPOSE = "compose.yaml:ops/tests/ticket36_compose.yaml"


# guard 核對實際容器、專用 DB 資料卷、埠及金鑰，通過前禁止任何 fixture 寫入。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT, "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == COMPOSE, "錯誤 Compose 檔")
    config = json.loads(subprocess.check_output(["docker", "compose", "--profile", "express", "config", "--format", "json"], text=True))
    check(config["name"] == PROJECT and config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data", "非隔離資料卷")
    check(config["services"]["db"]["environment"]["TICKET36_FIXTURE"] == "isolated-ticket36", "fixture 標記錯誤")
    for service, port in (("laravel", "18362"), ("express", "18363")):
        ids = subprocess.check_output(["docker", "compose", "--profile", "express", "ps", "-q", service], text=True).splitlines()
        check(len(ids) == 1, "後端容器未唯一啟動：" + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == PROJECT, "容器不屬於本票")
        check(item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy", "後端不健康")
        check(item["HostConfig"]["PortBindings"]["8000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": port}], "實際埠錯誤")
        key = os.path.realpath(f".local/ticket36-backend/{service}.key")
        check(any(os.path.realpath(m.get("Source", "")) == key and m.get("Destination") == f"/run/secrets/{service}-jwt.key" and m.get("RW") is False for m in item["Mounts"]), "測試金鑰掛載錯誤")
        with open(key, "rb") as test_key, open(f".local/keys/{service}.key", "rb") as demo_key:
            check(test_key.read() != demo_key.read(), "測試與展示金鑰共用")
        image = subprocess.check_output(["docker", "compose", "--profile", "express", "images", "-q", service], text=True).strip()
        check("sha256:" + image == item["Image"], "執行中容器不是目前 Compose 映像")
    ids = subprocess.check_output(["docker", "compose", "ps", "-q", "db"], text=True).splitlines()
    check(len(ids) == 1, "隔離 DB 未唯一啟動")
    database = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    check(database["Config"]["Labels"].get("com.docker.compose.project") == PROJECT and
          "TICKET36_FIXTURE=isolated-ticket36" in database["Config"]["Env"], "執行中 DB 身分錯誤")
    check(any(m.get("Name") == PROJECT + "_postgres_data" for m in database["Mounts"]), "DB 資料卷錯誤")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 身分錯誤")
    for base in (EXPRESS, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")


# create_user 只在隔離 DB 中建立唯一會員；管理員角色由受控 fixture 更新。
def create_user(tag, backend=EXPRESS, admin=False):
    email = f"t36-{tag}@example.test"
    expect(call(backend, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": "票36測試"}), 201)
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE email='{email}'")
    data = expect(call(backend, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    return {"id": data["user"]["id"], "email": email, "token": data["access_token"]}


# settings 讀取四欄並拒絕額外資料，便於跨後端逐值比較。
def settings(base, token):
    data = expect(call(base, "GET", "/api/settings/login-lockout", token=token), 200)
    check(set(data) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "設定欄位不精確")
    return data


# locked_sql 只在 guard 通過後持有指定隔離資料列，供兩後端請求實際重疊。
def locked_sql(statement):
    command = ["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c",
               "SET application_name='ticket36_holder'; BEGIN; " + statement + "; SELECT pg_sleep(6); COMMIT;"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, error = process.communicate()
            raise AssertionError("隔離鎖交易未持鎖：" + error.strip())
        if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='ticket36_holder' AND wait_event='PgSleep'") == "1":
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
    guard()
    if sys.argv[1:] == ["--guard-only"]:
        print("PASS ticket36_test guard")
        return
    nonce = secrets.token_hex(5)
    admin = create_user(nonce + "-admin", admin=True)
    member = create_user(nonce + "-member")
    other = create_user(nonce + "-other", backend=LARAVEL)
    admin_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
    member_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    original = settings(EXPRESS, admin["token"])
    check(settings(LARAVEL, admin_laravel) == original, "初始跨後端設定不同")
    # 身分先於無效參數；管理員 ID 對管理讀取及狀態修改都隱藏。
    for method, path, body in (("GET", "/api/members?bad=1", None),
                               ("GET", "/api/members/bad?bad=1", None),
                               ("PUT", "/api/members/bad/status", {"status": "bad"}),
                               ("GET", "/api/comments?bad=1", None),
                               ("GET", "/api/settings/login-lockout?bad=1", None),
                               ("PUT", "/api/settings/login-lockout", {"bad": 1})):
        expect(call(EXPRESS, method, path, body), 401, "E0002")
        expect(call(EXPRESS, method, path, body, member["token"]), 403, "E0003")
    for path in (f"/api/members/{admin['id']}", "/api/members/9007199254740991"):
        expect(call(EXPRESS, "GET", path, token=admin["token"]), 404, "E0004")
    expect(call(EXPRESS, "PUT", f"/api/members/{admin['id']}/status", {"status": "disabled"}, admin["token"]), 404, "E0004")
    # 精確 errors 與搜尋：短字、ASCII 折疊、Unicode、萬用符號、NUL、超頁。
    expect(call(EXPRESS, "GET", "/api/members?page=1&per_page=20&filters[keyword][]=x", token=admin["token"]), 422, "E0001", {"filters.keyword": ["欄位型態不正確"]})
    expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", {"enabled": "true", "window_minutes": 0, "failure_threshold": 5, "lock_seconds": 300}, admin["token"]), 422, "E0001", {"enabled": ["欄位型態不正確"], "window_minutes": ["欄位格式或範圍不正確"]})
    for nickname in ("Aa%_\\Ä陳" + nonce, "aa%_\\ä陳" + nonce):
        email = f"t36-{nonce}-search{len(nickname.encode())}{secrets.token_hex(2)}@example.test"
        expect(call(EXPRESS, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": nickname}), 201)
    # 一、二碼搜尋比對新增前後精確集合，重跑時也不假設隔離資料卷為空。
    short = ("𒀀", "𒀀𒀁")
    before_short = [expect(call(EXPRESS, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(word), token=admin["token"]), 200) for word in short]
    short_email = f"t36-{nonce}-short@example.test"
    expect(call(EXPRESS, "POST", "/api/auth/register", {"email": short_email, "password": "Abcd1234", "nickname": short[1] + nonce}), 201)
    short_id = int(sql(f"SELECT id FROM accounts WHERE email='{short_email}'"))
    for word, before in zip(short, before_short):
        after = expect(call(EXPRESS, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(word), token=admin["token"]), 200)
        check(after["pagination"]["total"] == before["pagination"]["total"] + 1 and
              set(row["id"] for row in after["list"]) == set(row["id"] for row in before["list"]) | {short_id}, "一／二碼搜尋集合錯誤")
    for keyword, expected in (("aA%_\\Ä陳" + nonce, 1), ("aa%_\\ä陳" + nonce, 1), ("ä陳" + nonce, 1), ("\0", 0)):
        path = "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(keyword)
        data = expect(call(EXPRESS, "GET", path, token=admin["token"]), 200)
        check(data["pagination"]["total"] == expected and len(data["list"]) == expected, "搜尋結果或 total 錯誤：" + repr(keyword))
    page = expect(call(EXPRESS, "GET", "/api/members?page=1&per_page=100", token=admin["token"]), 200)
    check(all(row["role"] == "member" and set(row) == {"id", "email", "nickname", "role", "status", "created_at"} for row in page["list"]), "會員列表範圍／欄位錯誤")
    deep = expect(call(EXPRESS, "GET", "/api/members?page=999999&per_page=1", token=admin["token"]), 200)
    check(deep["list"] == [] and deep["pagination"]["total"] == page["pagination"]["total"] and deep["pagination"]["page"] == 999999, "超頁總數錯誤")
    check(expect(call(EXPRESS, "GET", f"/api/members/{member['id']}", token=admin["token"]), 200)["email"] == member["email"], "會員詳情錯誤")
    # 建立與下架內容，驗證管理文章範圍及全部留言都可讀。
    title = "ticket36-" + nonce
    expect(call(EXPRESS, "POST", "/api/articles", {"title": title, "body": "本文"}, member["token"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE title='{title}' AND author_id={member['id']}"))
    expect(call(EXPRESS, "POST", f"/api/articles/{article}/comments", {"body": "下架父文留言"}, member["token"]), 201)
    comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body='下架父文留言'"))
    # 舊 01–16 操作逐一路過至少一個契約拒絕，避免新管理路由改動共同驗證順序。
    expect(call(EXPRESS, "POST", "/api/auth/register", {"email": "bad@example.test", "nickname": "測試"}), 422, "E0001", {"password": ["此欄位為必填"]})
    expect(call(EXPRESS, "POST", "/api/auth/login", {"email": member["email"]}), 422, "E0001", {"password": ["此欄位為必填"]})
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
        expect(call(EXPRESS, method, path, body), 401, "E0002")
    expect(call(EXPRESS, "GET", "/api/articles/bad"), 422, "E0001", {"article_id": ["欄位格式或範圍不正確"]})
    expect(call(EXPRESS, "GET", f"/api/articles/{article}/comments?page=bad&per_page=20"), 422, "E0001", {"page": ["欄位格式或範圍不正確"]})
    # 同一隔離來源依公開契約逐一走過既有操作 03–16 的正向路徑。
    check(article in [row["id"] for row in expect(call(EXPRESS, "GET", "/api/articles?page=1&per_page=100"), 200)["list"]], "公開文章列表缺新文章")
    check(expect(call(EXPRESS, "GET", f"/api/articles/{article}"), 200)["title"] == title, "文章詳情錯誤")
    expect(call(EXPRESS, "PUT", f"/api/articles/{article}", {"title": title, "body": "本文", "version": 1}, member["token"]), 200)
    check(article in [row["id"] for row in expect(call(EXPRESS, "GET", "/api/me/articles?page=1&per_page=100", token=member["token"]), 200)["list"]], "我的文章缺新文章")
    check(comment in [row["id"] for row in expect(call(EXPRESS, "GET", "/api/me/comments?page=1&per_page=100", token=member["token"]), 200)["list"]], "我的留言缺新留言")
    check(comment in [row["id"] for row in expect(call(EXPRESS, "GET", f"/api/articles/{article}/comments?page=1&per_page=100"), 200)["list"]], "文章留言缺新留言")
    check(expect(call(EXPRESS, "GET", f"/api/comments/{comment}", token=member["token"]), 200)["body"] == "下架父文留言", "留言詳情錯誤")
    expect(call(EXPRESS, "PUT", f"/api/comments/{comment}", {"body": "下架父文留言", "version": 1}, member["token"]), 200)
    expect(call(EXPRESS, "PUT", f"/api/comments/{comment}/visibility", {"visibility": "hidden", "version": 1}, member["token"]), 200)
    expect(call(EXPRESS, "PUT", f"/api/comments/{comment}/visibility", {"visibility": "visible", "version": 2}, member["token"]), 200)
    spare = expect(call(EXPRESS, "POST", "/api/auth/login", {"email": other["email"], "password": "Abcd1234"}), 200)["access_token"]
    expect(call(EXPRESS, "POST", "/api/auth/logout", token=spare), 200)
    expect(call(EXPRESS, "GET", "/api/me", token=spare), 401, "E0002")
    expect(call(EXPRESS, "PUT", f"/api/articles/{article}/visibility", {"visibility": "hidden", "version": 1}, member["token"]), 200)
    expect(call(EXPRESS, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management"), 401, "E0002")
    expect(call(EXPRESS, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=member["token"]), 403, "E0003")
    managed = expect(call(EXPRESS, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=admin["token"]), 200)
    check(article in [row["id"] for row in managed["list"]], "管理文章漏下架資料")
    comments = expect(call(EXPRESS, "GET", "/api/comments?page=1&per_page=100", token=admin["token"]), 200)
    check(comment in [row["id"] for row in comments["list"]], "管理留言漏下架父文資料")
    # 同值狀態不撤銷；實際停用撤銷兩個後端 session，恢復後舊憑證不復活。
    expect(call(EXPRESS, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin["token"]), 200)
    expect(call(EXPRESS, "GET", "/api/me", token=member["token"]), 200)
    expect(call(EXPRESS, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin["token"]), 200)
    expect(call(EXPRESS, "GET", "/api/me", token=member["token"]), 403, "E0102")
    expect(call(LARAVEL, "GET", "/api/me", token=member_laravel), 403, "E0102")
    expect(call(LARAVEL, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin_laravel), 200)
    expect(call(EXPRESS, "GET", "/api/me", token=member["token"]), 401, "E0002")
    check(sql(f"SELECT status FROM accounts WHERE id={member['id']}") == "active" and sql(f"SELECT title FROM articles WHERE id={article}") == title, "恢復或內容持久狀態錯誤")
    # Laravel 的失敗事件由 Express 讀取；只有實際關閉才清鎖與計數，數值變更保留期限。
    try:
        enabled = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 300}
        expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        check(settings(LARAVEL, admin_laravel) == enabled, "Express 設定未跨後端生效")
        # 先後排隊兩個完整設定寫入；共同設定列只允許最後提交的四欄一起保留。
        first = {**enabled, "failure_threshold": 3}
        last = {**enabled, "failure_threshold": 4, "lock_seconds": 600}
        holder = locked_sql("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                express_put = pool.submit(call, EXPRESS, "PUT", "/api/settings/login-lockout", first, admin["token"])
                wait_lock()
                laravel_put = pool.submit(call, LARAVEL, "PUT", "/api/settings/login-lockout", last, admin_laravel)
                wait_lock(2)
                finish_lock(holder)
                expect(express_put.result(), 200)
                expect(laravel_put.result(), 200)
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.communicate()
        check(settings(EXPRESS, admin["token"]) == last and settings(LARAVEL, admin_laravel) == last, "設定競爭混合欄位或順序錯誤")
        expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": other["email"], "password": "Wrong1234"}), 401, "E0101")
        expect(call(EXPRESS, "POST", "/api/auth/login", {"email": other["email"], "password": "Wrong1234"}), 429, "E0105")
        until = sql(f"SELECT locked_until::text FROM accounts WHERE id={other['id']}")
        changed = {**enabled, "lock_seconds": 600}
        expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", changed, admin["token"]), 200)
        check(sql(f"SELECT locked_until::text FROM accounts WHERE id={other['id']}") == until, "數值變更改動既有期限")
        # 以資料庫實際時間設邊界：Express 在鎖內回 429，Laravel 到期後能登入並清鎖。
        boundary = create_user(nonce + "-boundary")
        sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '3 seconds' WHERE id={boundary['id']}")
        expect(call(EXPRESS, "POST", "/api/auth/login", {"email": boundary["email"], "password": "Abcd1234"}), 429, "E0105")
        deadline = time.monotonic() + 5
        while sql(f"SELECT clock_timestamp() >= locked_until FROM accounts WHERE id={boundary['id']}") != "t":
            check(time.monotonic() < deadline, "資料庫鎖定期限未到")
            time.sleep(0.05)
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": boundary["email"], "password": "Abcd1234"}), 200)
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={boundary['id']}") == "t", "跨後端到期未清鎖")
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}"))
        disabled = {**changed, "enabled": False}
        expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", disabled, admin["token"]), 200)
        check(settings(LARAVEL, admin_laravel) == disabled and sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={other['id']}") == "t", "關閉未清鎖")
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}")) == epoch + 1, "關閉未清計數世代")
        expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", disabled, admin["token"]), 200)
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}")) == epoch + 1, "同值關閉重做副作用")
    finally:
        expect(call(EXPRESS, "PUT", "/api/settings/login-lockout", original, admin["token"]), 200)
    # 先排隊停用，再送 Laravel 寫入；帳號列鎖釋放後舊登入不能寫入。
    fresh = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    race_title = "ticket36-race-" + nonce
    holder = locked_sql(f"SELECT id FROM accounts WHERE id={member['id']} FOR UPDATE")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            disable = pool.submit(call, EXPRESS, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin["token"])
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
    expect(call(EXPRESS, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin["token"]), 200)
    print("PASS Ticket 36 Express 管理 HTTP/SQL 驗收")


if __name__ == "__main__":
    main()
