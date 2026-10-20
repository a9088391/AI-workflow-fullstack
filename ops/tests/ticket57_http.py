"""Ticket 57：隔離 Compose 上的十二後端共同 HTTP 操作與資料互通驗證。"""

import base64
from collections import deque
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import quote
from urllib.request import urlopen
from pathlib import Path

from ticket40_http import call


PROJECT = "ticket57_test"
COMPOSE = "compose.yaml:.local/ticket57-compose.yaml"
BACKENDS = (
    "laravel", "symfony", "aspnet-core", "django", "flask", "fastapi",
    "gin", "echo", "fiber", "express", "fastify", "nestjs",
)
PORTS = {backend: 8081 + index for index, backend in enumerate(BACKENDS)}
BASES = {backend: f"http://127.0.0.1:{port}" for backend, port in PORTS.items()}
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
    "E0005": "系統暫時無法處理，請稍後再試",
    "E0101": "帳號或密碼錯誤",
    "E0102": "此帳號已停用，請聯絡系統管理員",
    "E0105": "登入失敗次數過多，請稍後再試",
    "E0201": "內容已更新，請確認最新內容後再送出",
}
SAFE = False


# check 在第一個不符契約處停止，避免後續測試寫入掩蓋真正偏差。
def check(condition, message):
    if not condition:
        raise AssertionError(message)


# compose 只使用呼叫者已核對的固定專案與檔案，不接受任意測試目標。
def compose(*args):
    return subprocess.check_output(["docker", "compose", "--profile", "*", *args], text=True).strip()


# sql 限制在 guard 通過後的隔離 DB，並讓任何 SQL 錯誤立即中止。
def sql(statement):
    check(SAFE, "尚未核對隔離資料庫")
    return compose("exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
                   "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c", statement)


# container 核對唯一容器的實際專案、服務、網路及健康狀態，避免只相信宣告設定。
def container(service):
    ids = compose("ps", "-q", service).splitlines()
    check(len(ids) == 1, f"{service} 容器不唯一或未啟動")
    item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    labels = item["Config"]["Labels"]
    check(labels.get("com.docker.compose.project") == PROJECT and
          labels.get("com.docker.compose.service") == service and
          item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy" and
          PROJECT + "_default" in item["NetworkSettings"]["Networks"], f"{service} 非隔離健康容器")
    return item


# guard 先核對執行中的容器、DB volume、金鑰掛載與固定 loopback 埠，再允許任何寫入。
def guard():
    global SAFE
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT, "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == COMPOSE, "錯誤 Compose 檔")
    config = json.loads(compose("config", "--format", "json"))
    check(config["name"] == PROJECT and
          config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data" and
          config["networks"]["default"]["ipam"]["config"][0]["subnet"] == "10.251.57.0/24", "隔離設定不符")
    db = container("db")
    check(any(m.get("Name") == PROJECT + "_postgres_data" and m.get("Destination") == "/var/lib/postgresql"
              for m in db["Mounts"]), "DB 使用非隔離資料卷")
    for service in BACKENDS:
        item = container(service)
        internal = "8080/tcp" if service == "aspnet-core" else "8000/tcp"
        check(item["HostConfig"]["PortBindings"].get(internal) ==
              [{"HostIp": "127.0.0.1", "HostPort": str(PORTS[service])}], f"{service} 埠綁定不符")
        check({"DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"}.issubset(item["Config"]["Env"]),
              f"{service} 未連隔離 DB")
        key_target = f"/run/secrets/{service}-jwt.key"
        check(any(m.get("Destination") == key_target and m.get("RW") is False and
                  os.path.realpath(m.get("Source", "")) == os.path.realpath(f".local/keys/{service}.key")
                  for m in item["Mounts"]), f"{service} 金鑰掛載不符")
    # 所有後端實際 HTTP 就緒後才開啟測試寫入權；健康檢查失敗不能留下可寫狀態。
    for service, base in BASES.items():
        with urlopen(base + "/up", timeout=5) as response:
            expected = b'{"status":"ok"}' if service in ("symfony", "aspnet-core") else b"OK"
            check(response.status == 200 and response.read() == expected, f"{service} HTTP 未就緒")
    SAFE = True
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 身分不符")


# expect 精確核對共用 envelope、狀態碼、錯誤文案與 errors，忽略每請求獨立的 log_id。
def expect(result, status, code=None, fields=None):
    actual, payload, _ = result
    check(actual == status, f"HTTP {actual}，預期 {status}/{code}")
    if code is None:
        check(isinstance(payload, dict) and set(payload) == {"success", "message", "data"} and
              payload["success"] is True and payload["message"] == "OK" and isinstance(payload["data"], dict),
              "成功 envelope 不符")
        return payload["data"]
    keys = {"success", "message", "error_code", "log_id"} | ({"errors"} if fields is not None else set())
    check(isinstance(payload, dict) and set(payload) == keys and payload["success"] is False and
          payload["error_code"] == code and payload["message"] == MESSAGES[code] and
          str(uuid.UUID(payload["log_id"])) == payload["log_id"], "錯誤 envelope 不符")
    if fields is not None:
        check(payload["errors"] == fields, "錯誤欄位或文案不符")
    return {key: value for key, value in payload.items() if key != "log_id"}


# request 保留相同 method、path 與 body，讓每個後端走自己的公開 HTTP 入口。
def request(backend, method, path, body=None, token=None):
    return call(BASES[backend], method, path, body, token)


# api_log_lines 從已核對的隔離服務讀近期安全事件，避免輸出日誌中的請求內容。
def api_log_lines(backend):
    if backend in ("laravel", "symfony"):
        folder = Path(f".local/{backend}-logs")
        files = list(folder.glob("api-*.log"))
        check(files, f"{backend} API 日誌不存在")
        with max(files, key=lambda item: item.stat().st_mtime).open(encoding="utf-8", errors="replace") as source:
            return list(deque(source, maxlen=2000))
    return compose("logs", "--no-color", "--no-log-prefix", "--tail", "2000", backend).splitlines()


# trace_errors 核對每個錯誤有獨立 UUID，且可在該後端近期日誌找到同次 IN／EX／OUT。
def trace_errors(backend, observed):
    identifiers = [item[0] for item in observed]
    check(len(identifiers) == len(set(identifiers)), f"{backend} 錯誤 log_id 重複")
    for _ in range(10):
        lines = api_log_lines(backend)
        if all(any(log_id in line and re.search(r"\bOUT\b", line) for line in lines)
               for log_id in identifiers):
            break
        time.sleep(0.2)
    for log_id, method, path, status in observed:
        matches = [line for line in lines if log_id in line]
        events = {event: [index for index, line in enumerate(matches) if re.search(rf"\b{event}\b", line)]
                  for event in ("IN", "EX", "OUT")}
        # Symfony 的 JSON 日誌會把路徑斜線寫成 \/，比對前還原此表示法。
        check(len(events["IN"]) == 1 and 1 <= len(events["EX"]) <= 2 and len(events["OUT"]) == 1 and
              events["IN"][0] < events["EX"][0] < events["OUT"][0] and len(matches) <= 4 and
              method in matches[events["IN"][0]] and
              path.lstrip("/") in matches[events["IN"][0]].replace("\\/", "/") and
              str(status) in matches[events["OUT"][0]], f"{backend} log_id IN／EX／OUT 對應不符")


# checked_error 逐字比對 API15 回應，並保留當次追蹤碼供後續核對服務日誌。
def checked_error(backend, method, path, body, token, status, code, fields, observed):
    result = request(backend, method, path, body, token)
    expect(result, status, code, fields)
    log_id = result[1]["log_id"]
    observed.append((log_id, method, path.split("?", 1)[0], status))


# boundary_cases 以新帳號測 API14／API15，拒絕後讀回 session 與資料確認無部分副作用。
def boundary_cases(backend, nonce):
    member = user(backend, nonce + "b", "member")
    token = member["token"]
    observed = []
    before = sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL")
    check(before == "0", "邊界測資已有撤銷登入")
    checked_error(backend, "POST", "/api/auth/logout", {"extra": 1}, token, 422, "E0001",
                  {"extra": ["不支援此欄位"]}, observed)
    checked_error(backend, "GET", "/api/me", "{}", token, 422, "E0001",
                  {"_body": ["此 API 不接受 Request Body"]}, observed)
    checked_error(backend, "POST", "/api/auth/login", "{", None, 400, "E0005", None, observed)
    checked_error(backend, "POST", "/api/auth/login", [], None, 422, "E0001",
                  {"_body": ["Request Body 必須為 JSON object"]}, observed)
    for field, value, message in (("password", None, "此欄位不可為 null"),
                                  ("password", 123, "欄位型態不正確")):
        email = f"t57-{nonce}-{backend}-boundary@example.test"
        checked_error(backend, "POST", "/api/auth/register",
                      {"email": email, "password": value, "nickname": "邊界"}, None,
                      422, "E0001", {field: [message]}, observed)
        check(sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "0", "無效註冊留下帳號")
    check(sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL") == "0" and
          sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}") == "0" and
          expect(request(backend, "GET", "/api/me", token=token), 200)["id"] == member["id"],
          "API14／API15 拒絕後改變登入狀態或計數")
    trace_errors(backend, observed)
    check(expect(request(backend, "POST", "/api/auth/logout", token=token), 200) == {}, "無 Body 登出")
    expect(request(backend, "GET", "/api/me", token=token), 401, "E0002")
    second = expect(request(backend, "POST", "/api/auth/login",
        {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    check(expect(request(backend, "POST", "/api/auth/logout", {}, second), 200) == {}, "空 object 登出")
    check(sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL") == "2",
          "兩種合法登出未各撤銷登入")
    print(f"PASS {backend} API14／API15 拒絕無副作用、兩種登出及六筆 log_id 對應", flush=True)


# user 透過公開 API 建立唯一帳號；管理員角色僅在已核對的隔離 DB fixture 提升。
def user(backend, nonce, kind, nickname=None):
    email = f"t57-{nonce}-{backend}-{kind}@example.test"
    body = {"email": email, "password": "Abcd1234", "nickname": nickname or f"票57{kind}"}
    check(expect(request(backend, "POST", "/api/auth/register", body), 201) == {}, "註冊回應")
    account_id = int(sql(f"SELECT id FROM accounts WHERE email='{email}'"))
    if kind == "admin":
        check(sql(f"UPDATE accounts SET role='admin' WHERE id={account_id} RETURNING role") == "admin", "管理員 fixture 失敗")
    login = expect(request(backend, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    role = "admin" if kind == "admin" else "member"
    check(login["user"]["id"] == account_id and login["user"]["role"] == role and
          login["user"]["email"] == email and login["token_type"] == "Bearer", "登入身分不符")
    return {"id": account_id, "email": email, "token": login["access_token"]}


# record_id 從已核對的隔離 DB 取回 API 寫入 ID；唯一測資名稱避免重跑相互污染。
def record_id(table, field, value):
    check(re.fullmatch(r"[a-z0-9-]+", value) is not None, "fixture 識別不得含 SQL 特殊字元")
    result = sql(f"SELECT id FROM {table} WHERE {field}='{value}'")
    check(result.isdecimal(), f"{table} 寫入數量或 ID 不符")
    return int(result)


# listed 確認分頁中含指定 ID，並核對共同 list 與 pagination 結構。
def listed(data, record_id):
    check(set(data) == {"list", "pagination"} and isinstance(data["list"], list) and
          {"page", "per_page", "total", "total_pages"} == set(data["pagination"]) and
          record_id in [item["id"] for item in data["list"]], "列表或分頁未包含新資料")


# backend_cases 以各自新帳號重播 22 種方法／路徑，檢查寫入後讀回與主要錯誤順序。
def backend_cases(backend, nonce):
    member = user(backend, nonce, "member")
    admin = user(backend, nonce, "admin")
    token, admin_token = member["token"], admin["token"]
    check(expect(request(backend, "GET", "/api/me", token=token), 200)["id"] == member["id"], "04 本人讀取")
    expect(request(backend, "GET", "/api/me"), 401, "E0002")
    print(f"PASS {backend} 01–04 註冊、登入、本人與認證錯誤", flush=True)

    # 先由該後端建立文章與留言，再以完整 ID／來源／版本檢查其讀取與修改生命週期。
    title = f"t57-{nonce}-{backend}"
    comment_body = f"t57-{nonce}-{backend}-comment"
    check(expect(request(backend, "POST", "/api/articles", {"title": title, "body": "繁體中文內文"}, token), 201) == {}, "06 發文")
    article_id = record_id("articles", "title", title)
    article_path = f"/api/articles/{article_id}"
    article = expect(request(backend, "GET", article_path), 200)
    check(article["created_backend"] == backend and article["author"]["id"] == member["id"] and
          article["version"] == 1 and article["title"] == title, "07 文章來源／作者／版本")
    listed(expect(request(backend, "GET", "/api/articles?page=1&per_page=100"), 200), article_id)
    listed(expect(request(backend, "GET", "/api/me/articles?page=1&per_page=100", token=token), 200), article_id)
    expect(request(backend, "GET", "/api/articles?page=bad&per_page=1"), 422, "E0001",
           {"page": ["欄位格式或範圍不正確"]})
    expect(request(backend, "PUT", article_path, {"title": title, "body": "他人不得修改", "version": 1}, admin_token), 403, "E0003")
    check(expect(request(backend, "PUT", article_path, {"title": title, "body": "更新後內文", "version": 1}, token), 200) == {}, "08 修改文章")
    expect(request(backend, "PUT", article_path, {"title": title, "body": "舊版修改", "version": 1}, token), 409, "E0201")
    check(expect(request(backend, "GET", article_path), 200)["version"] == 2, "08 版本遞增")
    print(f"PASS {backend} 05–09 文章列表、建立、詳情、修改與我的文章", flush=True)

    check(expect(request(backend, "POST", article_path + "/comments", {"body": comment_body}, token), 201) == {}, "13 留言")
    comment_id = record_id("comments", "body", comment_body)
    comment_path = f"/api/comments/{comment_id}"
    comment = expect(request(backend, "GET", comment_path, token=token), 200)
    check(comment["created_backend"] == backend and comment["article_id"] == article_id and
          comment["author"]["id"] == member["id"] and comment["version"] == 1, "14 留言來源／作者／版本")
    listed(expect(request(backend, "GET", article_path + "/comments?page=1&per_page=100"), 200), comment_id)
    listed(expect(request(backend, "GET", "/api/me/comments?page=1&per_page=100", token=token), 200), comment_id)
    check(expect(request(backend, "PUT", comment_path, {"body": "更新後留言", "version": 1}, token), 200) == {}, "15 修改留言")
    expect(request(backend, "PUT", comment_path, {"body": "舊版留言", "version": 1}, token), 409, "E0201")
    check(expect(request(backend, "GET", comment_path, token=token), 200)["body"] == "更新後留言", "15 留言讀回")
    check(expect(request(backend, "PUT", comment_path + "/visibility", {"visibility": "hidden", "version": 2}, token), 200) == {}, "16 留言下架")
    check(expect(request(backend, "GET", comment_path, token=token), 200)["visibility"] == "hidden",
          "14 作者可讀自己下架的留言")
    check(expect(request(backend, "PUT", comment_path + "/visibility", {"visibility": "visible", "version": 3}, token), 200) == {}, "16 留言恢復")
    check(expect(request(backend, "PUT", article_path + "/visibility", {"visibility": "hidden", "version": 2}, token), 200) == {}, "11 文章下架")
    expect(request(backend, "GET", article_path), 404, "E0004")
    check(expect(request(backend, "PUT", article_path + "/visibility", {"visibility": "visible", "version": 3}, token), 200) == {}, "11 文章恢復")
    print(f"PASS {backend} 10–16 留言與文章下架／恢復生命週期", flush=True)

    # 管理 API 只能由管理員使用；會員停用應撤銷舊 token，恢復後仍須重新登入。
    expect(request(backend, "GET", "/api/members?page=1&per_page=20", token=token), 403, "E0003")
    search = f"/api/members?page=1&per_page=100&filters%5Bkeyword%5D={nonce}-{backend}-member"
    listed(expect(request(backend, "GET", search, token=admin_token), 200), member["id"])
    detail = expect(request(backend, "GET", f"/api/members/{member['id']}", token=admin_token), 200)
    check(detail["email"] == member["email"] and detail["role"] == "member", "18 會員詳情")
    expect(request(backend, "GET", f"/api/members/{admin['id']}", token=admin_token), 404, "E0004")
    expect(request(backend, "PUT", f"/api/members/{admin['id']}/status", {"status": "disabled"}, admin_token), 404, "E0004")
    listed(expect(request(backend, "GET", "/api/comments?page=1&per_page=100", token=admin_token), 200), comment_id)
    check(expect(request(backend, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin_token), 200) == {}, "19 停用會員")
    expect(request(backend, "GET", "/api/me", token=token), 403, "E0102")
    check(expect(request(backend, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin_token), 200) == {}, "19 恢復會員")
    expect(request(backend, "GET", "/api/me", token=token), 401, "E0002")
    new_token = expect(request(backend, "POST", "/api/auth/login",
        {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    print(f"PASS {backend} 17–20 會員搜尋、詳情、狀態與管理留言", flush=True)

    # 設定寫入以 try/finally 恢復四欄，避免測試失敗留下非預設管理設定。
    original = expect(request(backend, "GET", "/api/settings/login-lockout", token=admin_token), 200)
    check(set(original) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "21 設定欄位")
    expect(request(backend, "GET", "/api/settings/login-lockout", token=new_token), 403, "E0003")
    changed = {**original, "lock_seconds": original["lock_seconds"] + (1 if original["lock_seconds"] < 86400 else -1)}
    try:
        check(expect(request(backend, "PUT", "/api/settings/login-lockout", changed, admin_token), 200) == {}, "22 設定寫入")
        for reader in BACKENDS:
            reader_token = admin_token if reader == backend else expect(request(reader, "POST", "/api/auth/login",
                {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
            check(expect(request(reader, "GET", "/api/settings/login-lockout", token=reader_token), 200) == changed,
                  f"22 設定跨後端讀取：{reader}")
    finally:
        check(expect(request(backend, "PUT", "/api/settings/login-lockout", original, admin_token), 200) == {}, "設定恢復失敗")
    print(f"PASS {backend} 21–22 設定讀寫及十二後端互通", flush=True)

    # 登出只撤銷這次新登入；所有內容測資保留供跨後端讀取核對。
    check(expect(request(backend, "POST", "/api/auth/logout", {}, new_token), 200) == {}, "03 登出")
    expect(request(backend, "GET", "/api/me", token=new_token), 401, "E0002")
    return article_id, comment_id


# competing_update 同時送出同版本的兩筆修改；成功者未預先指定，只檢查資料庫不變條件。
def competing_update(backend, path, title, body, token, barrier):
    barrier.wait(timeout=10)
    return request(backend, "PUT", path, {"title": title, "body": body, "version": 1}, token)


# synchronized_request 讓兩個公開 API 請求在同一門檻後開始，仍以提交結果判定順序。
def synchronized_request(backend, method, path, body, token, barrier):
    barrier.wait(timeout=10)
    return request(backend, method, path, body, token)


# contract_cases 用同樣的有效與無效輸入重播資料、副作用、搜尋及並行版本規則。
def contract_cases(backend, nonce):
    peer = BACKENDS[(BACKENDS.index(backend) + 1) % len(BACKENDS)]
    marker = nonce + backend
    member = user(backend, nonce + "x", "member", "Aa%_\\Ä陳" + marker)
    other = user(backend, nonce + "x", "other", "aa%_\\ä陳" + marker)
    decoy = user(backend, nonce + "x", "decoy", "AaXY\\Ä陳" + marker)
    admin = user(backend, nonce + "x", "admin")
    token, admin_token = member["token"], admin["token"]

    # 密碼與特殊字元先驗證才可能計數或寫入，逐字核對錯誤並確認沒有新增帳號。
    bad_email = f"t57-{nonce}-{backend}-invalid@example.test"
    invalid_nickname = {"email": bad_email, "password": "Abcd1234", "nickname": "壞\x00名"}
    check("\x00" in invalid_nickname["nickname"] and json.loads(json.dumps(invalid_nickname)) == invalid_nickname,
          "NUL 測試資料未正確序列化")
    expect(request(backend, "POST", "/api/auth/register", invalid_nickname), 422, "E0001",
           {"nickname": ["欄位格式或範圍不正確"]})
    check(sql(f"SELECT count(*) FROM accounts WHERE email='{bad_email}'") == "0", "無效暱稱留下帳號")
    expect(request(backend, "POST", "/api/auth/login", {"email": member["email"], "password": "Bad!"}),
           422, "E0001", {"password": ["欄位格式或範圍不正確"]})
    check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}") == "0", "無效密碼被列入鎖定計數")
    expect(request(backend, "GET", "/api/articles?page=1&page=2&per_page=20"), 422, "E0001",
           {"page": ["此參數不可重複"]})
    expect(request(backend, "GET", "/api/articles?page=1&per_page=20&other=1"), 422, "E0001",
           {"other": ["不支援此欄位"]})
    print(f"PASS {backend} 共同驗證：密碼、NUL、重複／未知 Query 與無副作用", flush=True)

    # 各後端可驗證彼此儲存的 Argon2id 密碼，但不能接受另一個 issuer 的 JWT。
    check(sql(f"SELECT password_hash LIKE '$argon2id$%' FROM accounts WHERE id={member['id']}") == "t", "密碼雜湊格式")
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    check(claims["iss"] == f"ai-workflow-fullstack:{backend}" and
          claims["aud"] == f"ai-workflow-fullstack-api:{backend}" and claims["sub"] == str(member["id"]),
          "JWT issuer／audience／subject 不符")
    expect(request(peer, "GET", "/api/me", token=token), 401, "E0002")
    peer_token = expect(request(peer, "POST", "/api/auth/login",
        {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    check(expect(request(peer, "GET", "/api/me", token=peer_token), 200)["id"] == member["id"], "跨後端密碼互通")
    expect(request(backend, "GET", "/api/articles?page=1&per_page=20", token="invalid"), 401, "E0002")
    print(f"PASS {backend} JWT issuer 隔離與跨後端 Argon2id 登入", flush=True)

    # 搜尋把 %、_、反斜線當字面，僅 ASCII 折疊；nonce 讓精確集合不受舊測資干擾。
    for keyword, expected_id in (("aa%_\\Ä陳" + marker, member["id"]),
                                 ("AA%_\\ä陳" + marker, other["id"])):
        result = expect(request(backend, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" +
            quote(keyword), token=admin_token), 200)
        check(result["pagination"]["total"] == 1 and
              [item["id"] for item in result["list"]] == [expected_id] and decoy["id"] != expected_id,
              "搜尋字面／ASCII 或精確 total 不符")
    print(f"PASS {backend} 會員搜尋字面、ASCII 折疊與精確總數", flush=True)

    # 拒絕唯讀欄位、NUL 及他人修改，並直接讀回 DB 證明沒有留下部分寫入。
    title = f"t57-{nonce}-{backend}-contract"
    expect(request(backend, "POST", "/api/articles", {"title": title, "body": "原文"}, token), 201)
    article_id = record_id("articles", "title", title)
    path = f"/api/articles/{article_id}"
    before = sql(f"SELECT title||'|'||body||'|'||version FROM articles WHERE id={article_id}")
    expect(request(backend, "PUT", path,
        {"title": title, "body": "改寫", "version": 1, "created_backend": "laravel"}, token),
        422, "E0001", {"created_backend": ["不支援此欄位"]})
    nul_body = {"title": title, "body": "錯\x00文", "version": 1}
    check("\x00" in nul_body["body"] and json.loads(json.dumps(nul_body)) == nul_body, "文章 NUL 測資未正確序列化")
    expect(request(backend, "PUT", path, nul_body, token), 422, "E0001",
           {"body": ["欄位格式或範圍不正確"]})
    expect(request(backend, "PUT", path, {"title": title, "body": "他人改寫", "version": 1}, other["token"]),
           403, "E0003")
    check(sql(f"SELECT title||'|'||body||'|'||version FROM articles WHERE id={article_id}") == before,
          "無效或越權修改留下部分寫入")

    # 兩個請求同時持有 version=1，只有一個能提交；同值正確版本不再增加版本。
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(competing_update, backend, path, title, body, token, barrier)
                   for body in ("競爭甲", "競爭乙")]
        results = [future.result(timeout=20) for future in results]
    check(sorted(item[0] for item in results) == [200, 409], "同版本並行寫入結果不符")
    for result in results:
        expect(result, result[0], "E0201" if result[0] == 409 else None)
    winner = expect(request(backend, "GET", path), 200)
    check(winner["version"] == 2 and winner["body"] in ("競爭甲", "競爭乙") and
          sql(f"SELECT body||'|'||version FROM articles WHERE id={article_id}") == winner["body"] + "|2",
          "並行結果未原子提交")
    expect(request(backend, "PUT", path, {"title": title, "body": winner["body"], "version": 2}, token), 200)
    check(expect(request(backend, "GET", path), 200) == winner, "同值更新改變版本或時間")
    expect(request(backend, "PUT", path, {"title": title, "body": winner["body"], "version": 1}, token), 409, "E0201")
    check(sql(f"SELECT body||'|'||version FROM articles WHERE id={article_id}") == winner["body"] + "|2",
          "過期同值寫入改變資料")
    print(f"PASS {backend} 唯讀／NUL／越權拒絕與並行、同值／過期寫入", flush=True)

    # 下架資料不洩給訪客與其他會員；本人恢復後兩端讀回相同內容。
    expect(request(backend, "PUT", path + "/visibility", {"visibility": "hidden", "version": 2}, token), 200)
    expect(request(backend, "GET", path), 404, "E0004")
    expect(request(backend, "GET", path, token=other["token"]), 404, "E0004")
    check(expect(request(backend, "GET", path, token=token), 200)["visibility"] == "hidden", "作者隱藏讀取")
    expect(request(backend, "PUT", path + "/visibility", {"visibility": "visible", "version": 3}, token), 200)
    check(expect(request(peer, "GET", path), 200)["body"] == winner["body"], "跨後端恢復讀取")
    print(f"PASS {backend} 隱藏內容權限與跨後端恢復", flush=True)


# lockout_cases 暫時調低門檻，核對跨後端共同計數與剩餘時間，並必定恢復設定。
def lockout_cases(backend, nonce):
    peer = BACKENDS[(BACKENDS.index(backend) + 1) % len(BACKENDS)]
    admin = user(backend, nonce + "l", "admin")
    member = user(backend, nonce + "l", "member")
    admin_token = admin["token"]
    original = expect(request(backend, "GET", "/api/settings/login-lockout", token=admin_token), 200)
    changed = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 30}
    try:
        expect(request(backend, "PUT", "/api/settings/login-lockout", changed, admin_token), 200)
        check(expect(request(peer, "GET", "/api/settings/login-lockout", token=expect(request(peer,
            "POST", "/api/auth/login", {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]),
            200) == changed, "跨後端鎖定設定不同")
        # 兩後端同時競爭設定／帳號列鎖，門檻 2 必須得到一次 401 與一次 429。
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(synchronized_request, current, "POST", "/api/auth/login",
                {"email": member["email"], "password": "Wrong1234"}, None, barrier)
                for current in (backend, peer)]
            attempts = [future.result(timeout=20) for future in attempts]
        check(sorted(item[0] for item in attempts) == [401, 429], "跨後端並行登入遺失失敗計數")
        for result in attempts:
            expect(result, result[0], "E0101" if result[0] == 401 else "E0105")
        retry = next(item[2].get("Retry-After") for item in attempts if item[0] == 429)
        check(retry is not None and retry.isdecimal() and 1 <= int(retry) <= 30, "Retry-After 秒數不符")
        locked_until = sql(f"SELECT extract(epoch FROM locked_until) FROM accounts WHERE id={member['id']}")
        check(locked_until and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}") == "2", "跨後端鎖定未累計兩次")
        repeated = request(backend, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"})
        expect(repeated, 429, "E0105")
        check(int(repeated[2]["Retry-After"]) <= int(retry) and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}") == "2" and
              sql(f"SELECT extract(epoch FROM locked_until) FROM accounts WHERE id={member['id']}") == locked_until,
              "重試延長鎖定或新增失敗")

        # 只調整本次隔離帳號的期限；資料庫時間在期限兩側各驅動一次真實登入。
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}"))
        sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '4 seconds' WHERE id={member['id']}")
        check(sql(f"SELECT clock_timestamp()<locked_until FROM accounts WHERE id={member['id']}") == "t",
              "到期前測資未生效")
        expect(request(peer, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}),
               429, "E0105")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and sql(f"SELECT clock_timestamp()>=locked_until FROM accounts WHERE id={member['id']}") != "t":
            time.sleep(0.1)
        check(sql(f"SELECT clock_timestamp()>=locked_until FROM accounts WHERE id={member['id']}") == "t", "隔離鎖定期限未到")
        expect(request(backend, "POST", "/api/auth/login",
            {"email": member["email"], "password": "Abcd1234"}), 200)
        check(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}") == str(epoch + 2) and
              sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={member['id']}") == "t",
              "到期後未清鎖並重設登入世代")
        print(f"PASS {backend}／{peer} 並行失敗計數、Retry-After 與資料庫時間兩側到期", flush=True)
    finally:
        expect(request(backend, "PUT", "/api/settings/login-lockout", original, admin_token), 200)
        check(expect(request(backend, "GET", "/api/settings/login-lockout", token=admin_token), 200) == original,
              "鎖定設定未恢復原四欄")


# retry_remaining 用資料庫目前時間核對 429 標頭的剩餘秒數，容許傳輸及讀回跨秒。
def retry_remaining(backend, member_id, result):
    expect(result, 429, "E0105")
    header = result[2].get("Retry-After")
    check(header is not None and re.fullmatch(r"[1-9][0-9]*", header) is not None, "Retry-After 非正整數秒")
    received = time.monotonic()
    remaining = int(sql(f"SELECT greatest(0,ceil(extract(epoch FROM locked_until-clock_timestamp())))::integer "
                        f"FROM accounts WHERE id={member_id}"))
    elapsed = time.monotonic() - received
    check(1 <= remaining <= int(header) <= remaining + int(elapsed) + 2 and int(header) <= 30,
          f"{backend} Retry-After 與 DB 到期剩餘秒數不符")


# clear_boundary_trigger 只清理本測試的固定名稱物件，並核對中斷後沒有留下資料庫 trigger。
def clear_boundary_trigger():
    sql("DROP TRIGGER IF EXISTS ticket57_http_boundary_shift ON public.login_failures; "
        "DROP FUNCTION IF EXISTS public.ticket57_http_boundary_shift()")
    check(sql("SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
              "WHERE tgrelid='public.login_failures'::regclass AND tgname='ticket57_http_boundary_shift') "
              "AND to_regprocedure('public.ticket57_http_boundary_shift()') IS NULL") == "t",
          "Ticket 57 邊界 trigger／function 未清除")


# exact_boundary_cases 以真實第二次登入時間對齊舊事件，分別驗證 HTTP 左界相等與晚一微秒。
def exact_boundary_cases(backend, nonce, admin_token):
    settings = {"enabled": True, "window_minutes": 1, "failure_threshold": 2, "lock_seconds": 30}
    expect(request(backend, "PUT", "/api/settings/login-lockout", settings, admin_token), 200)
    for microseconds, status in ((0, 401), (1, 429)):
        member = user(backend, nonce + f"x{microseconds}", "member")
        member_id = member["id"]
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member_id}"))
        wrong = {"email": member["email"], "password": "Wrong1234"}
        expect(request(backend, "POST", "/api/auth/login", wrong), 401, "E0101")
        first_id = sql(f"SELECT id FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch}")
        check(first_id.isdecimal(), "精確邊界第一筆事件不唯一")

        # 應用只有 INSERT 權限；owner 建立的 SECURITY DEFINER 僅可改此帳號／世代的第一筆事件。
        clear_boundary_trigger()
        try:
            sql("CREATE FUNCTION public.ticket57_http_boundary_shift() RETURNS trigger "
                "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$ "
                f"BEGIN IF NEW.account_id={member_id} AND NEW.failure_epoch={epoch} THEN "
                "UPDATE public.login_failures SET occurred_at=NEW.occurred_at-interval '1 minute' "
                f"+interval '{microseconds} microseconds' WHERE id={first_id} "
                "AND account_id=NEW.account_id AND failure_epoch=NEW.failure_epoch; "
                "IF NOT FOUND THEN RAISE EXCEPTION 'ticket57 boundary fixture missing'; END IF; "
                "END IF; RETURN NEW; END; $$; "
                "CREATE TRIGGER ticket57_http_boundary_shift BEFORE INSERT ON public.login_failures "
                "FOR EACH ROW EXECUTE FUNCTION public.ticket57_http_boundary_shift()")
            result = request(backend, "POST", "/api/auth/login", wrong)
            if status == 429:
                retry_remaining(backend, member_id, result)
            else:
                expect(result, 401, "E0101")

            # 兩筆均已提交，且第一筆必須與第二筆實際 DB 時間精確相差 N 或 N−1μs。
            check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} "
                      f"AND failure_epoch={epoch}") == "2", "精確邊界事件未提交兩筆")
            aligned = sql("SELECT (f1.occurred_at=f2.occurred_at-interval '1 minute' "
                          f"+interval '{microseconds} microseconds')::text "
                          f"FROM login_failures f1 JOIN login_failures f2 ON f2.account_id={member_id} "
                          f"AND f2.failure_epoch={epoch} AND f2.id<>{first_id} WHERE f1.id={first_id}")
            check(aligned == "true", "精確邊界事件時間未對齊")
            check(sql(f"SELECT locked_until IS NOT NULL FROM accounts WHERE id={member_id}") ==
                  ("t" if status == 429 else "f"), "精確邊界鎖定狀態不符")
        finally:
            clear_boundary_trigger()
    print(f"PASS {backend} DBV02 真實 HTTP 左界相等／晚一微秒", flush=True)


# lockout_edge_cases 以隔離帳號檢查左開視窗、調大 N、關閉／重啟及已提交失敗計數。
def lockout_edge_cases(backend, nonce):
    admin = user(backend, nonce + "e", "admin")
    member = user(backend, nonce + "e", "member")
    token, member_id = admin["token"], member["id"]
    original = expect(request(backend, "GET", "/api/settings/login-lockout", token=token), 200)
    short = {"enabled": True, "window_minutes": 1, "failure_threshold": 3, "lock_seconds": 30}
    wrong = {"email": member["email"], "password": "Wrong1234"}
    try:
        expect(request(backend, "PUT", "/api/settings/login-lockout", short, token), 200)
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member_id}"))
        expect(request(backend, "POST", "/api/auth/login", wrong), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch}") == "1",
              "401 未保存第一筆失敗")
        expect(request(backend, "POST", "/api/auth/login", wrong), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch}") == "2",
              "401 未保存第二筆失敗")
        # 兩筆都移至短視窗之外；第三次若錯算這兩筆，會錯誤達到門檻 3。
        check(sql(f"WITH shifted AS (UPDATE login_failures SET occurred_at=clock_timestamp()-interval '62 seconds' "
                  f"WHERE account_id={member_id} AND failure_epoch={epoch} RETURNING id) "
                  "SELECT count(*) FROM shifted") == "2", "左邊界 fixture 筆數不符")
        expect(request(backend, "POST", "/api/auth/login", wrong), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch}") == "3" and
              sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={member_id}") == "t",
              "短視窗錯誤計入舊事件或 401 遺失新事件")

        # 只放大 N，第四筆須因兩筆舊事件重新納入而達門檻 3。
        wide = {**short, "window_minutes": 5}
        expect(request(backend, "PUT", "/api/settings/login-lockout", wide, token), 200)
        check(sql(f"SELECT failure_epoch FROM accounts WHERE id={member_id}") == str(epoch), "調大 N 重設計數世代")
        locked = request(backend, "POST", "/api/auth/login", wrong)
        retry_remaining(backend, member_id, locked)
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch}") == "4" and
              sql(f"SELECT locked_until IS NOT NULL FROM accounts WHERE id={member_id}") == "t",
              "N 調大後 429 未提交第四筆失敗與到期時間")

        # 縮短既有鎖定期限後再重試，檢查 429 回傳剩餘秒數，且請求不得延長期限或新增失敗。
        shortened_until = sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '4 seconds' "
                              f"WHERE id={member_id} RETURNING locked_until::text")
        check(sql(f"SELECT clock_timestamp()<locked_until FROM accounts WHERE id={member_id}") == "t",
              "縮短後鎖定期限已過")
        retry_remaining(backend, member_id, request(backend, "POST", "/api/auth/login", wrong))
        check(sql(f"SELECT locked_until::text FROM accounts WHERE id={member_id}") == shortened_until and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch}") == "4",
              "既有鎖定的 429 延長期限或新增失敗")

        # 關閉鎖定須同時清鎖、換世代；重新啟用後舊失敗事件仍在但不可算入新視窗。
        expect(request(backend, "PUT", "/api/settings/login-lockout", {**wide, "enabled": False}, token), 200)
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={member_id}") == "t" and
              sql(f"SELECT failure_epoch FROM accounts WHERE id={member_id}") == str(epoch + 1),
              "關閉鎖定未清鎖及換世代")
        expect(request(backend, "PUT", "/api/settings/login-lockout", wide, token), 200)
        expect(request(backend, "POST", "/api/auth/login", wrong), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id} AND failure_epoch={epoch + 1}") == "1" and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={member_id}") == "5" and
              sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={member_id}") == "t",
              "重新啟用後誤算舊世代或未保存 401")
        exact_boundary_cases(backend, nonce, token)
        print(f"PASS {backend} DBV02 左開視窗、N 調大、既有鎖定剩餘秒數與停用／重啟", flush=True)
    finally:
        expect(request(backend, "PUT", "/api/settings/login-lockout", original, token), 200)
        check(expect(request(backend, "GET", "/api/settings/login-lockout", token=token), 200) == original,
              "DBV02 設定未恢復原四欄")


# database_cases 對相同隔離測資驗證跨後端唯一性及互斥寫入的提交結果。
def database_cases(backend, nonce):
    peer = BACKENDS[(BACKENDS.index(backend) + 1) % len(BACKENDS)]
    email = f"t57-{nonce}-{backend}-race@example.test"
    body = {"email": "  " + email.upper() + "  ", "password": "Abcd1234", "nickname": "並行作者"}
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(synchronized_request, current, "POST", "/api/auth/register", body, None, barrier)
                    for current in (backend, peer)]
        attempts = [future.result(timeout=20) for future in attempts]
    check(sorted(item[0] for item in attempts) == [201, 422], "跨後端同 Email 註冊未維持唯一")
    for result in attempts:
        expect(result, result[0], "E0001" if result[0] == 422 else None,
               {"email": ["此 Email 已註冊"]} if result[0] == 422 else None)
    check(sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "1", "並行註冊留下重複帳號")
    member_id = int(sql(f"SELECT id FROM accounts WHERE email='{email}'"))
    check(sql(f"SELECT password_hash LIKE '$argon2id$%' FROM accounts WHERE id={member_id}") == "t", "並行註冊雜湊格式")
    member_token = expect(request(backend, "POST", "/api/auth/login",
        {"email": email, "password": "Abcd1234"}), 200)["access_token"]
    peer_login = expect(request(peer, "POST", "/api/auth/login",
        {"email": email, "password": "Abcd1234"}), 200)
    check(peer_login["user"]["id"] == member_id, "並行註冊帳號無法跨後端登入")
    peer_token = peer_login["access_token"]
    admin = user(backend, nonce + "d", "admin")
    admin_token = admin["token"]
    print(f"PASS {backend}／{peer} 同正規化 Email 並行註冊與跨後端 PHC 驗證", flush=True)

    # 文章先由公開 API 建立；父文章下架與留言建立同時開始，拒絕後不得有留言殘留。
    title = f"t57-{nonce}-{backend}-dbv"
    expect(request(backend, "POST", "/api/articles", {"title": title, "body": "競爭本文"}, member_token), 201)
    article_id = record_id("articles", "title", title)
    detail = expect(request(peer, "GET", f"/api/articles/{article_id}"), 200)
    check(set(detail["author"]) == {"id", "nickname"} and detail["author"]["id"] == member_id,
          "公開作者物件洩漏 Email")
    article_path = f"/api/articles/{article_id}"
    comment_body = f"t57-{nonce}-{backend}-racing-comment"
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        hide = pool.submit(synchronized_request, backend, "PUT", article_path + "/visibility",
                           {"visibility": "hidden", "version": 1}, member_token, barrier)
        comment = pool.submit(synchronized_request, peer, "POST", article_path + "/comments",
                              {"body": comment_body}, peer_token, barrier)
        hide, comment = hide.result(timeout=20), comment.result(timeout=20)
    expect(hide, 200)
    check(comment[0] in (201, 403), "下架競爭留言結果不符")
    expect(comment, comment[0], "E0003" if comment[0] == 403 else None)
    check(sql(f"SELECT count(*) FROM comments WHERE article_id={article_id} AND body='{comment_body}'") ==
          ("1" if comment[0] == 201 else "0"), "下架競爭留言回應與提交資料不一致")
    # 下架提交後，對同一帳號的新留言必須拒絕，不能沿用早先的可見性快照。
    expect(request(peer, "POST", article_path + "/comments", {"body": comment_body}, peer_token), 403, "E0003")
    check(sql(f"SELECT count(*) FROM comments WHERE article_id={article_id} AND body='{comment_body}'") ==
          ("1" if comment[0] == 201 else "0"), "下架後新增留言留下部分寫入")
    print(f"PASS {backend}／{peer} 下架後留言拒絕與無部分寫入", flush=True)

    # 管理員再次下架會接管恢復權，增加 version；作者舊權限不能繞過。
    check(sql(f"SELECT restore_control||'|'||version FROM articles WHERE id={article_id}") == "author|2",
          "作者下架控制者或版本不符")
    expect(request(backend, "PUT", article_path + "/visibility",
        {"visibility": "hidden", "version": 2}, admin_token), 200)
    check(sql(f"SELECT restore_control||'|'||version FROM articles WHERE id={article_id}") == "admin|3",
          "管理員接管未增加版本")
    expect(request(backend, "PUT", article_path + "/visibility",
        {"visibility": "visible", "version": 3}, member_token), 403, "E0003")
    check(sql(f"SELECT restore_control||'|'||version FROM articles WHERE id={article_id}") == "admin|3",
          "作者拒絕恢復卻改變控制者")
    expect(request(backend, "PUT", article_path + "/visibility",
        {"visibility": "visible", "version": 3}, admin_token), 200)
    check(sql(f"SELECT visibility||'|'||version FROM articles WHERE id={article_id}") == "visible|4",
          "管理員恢復後狀態錯誤")
    print(f"PASS {backend} 管理員接管恢復權與版本", flush=True)

    # 登出與寫入同時開始，回應與資料列必須對應；停用優先於已撤銷 token。
    write_title = f"t57-{nonce}-{backend}-logout-write"
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        logout = pool.submit(synchronized_request, backend, "POST", "/api/auth/logout", {}, member_token, barrier)
        write = pool.submit(synchronized_request, backend, "POST", "/api/articles",
                            {"title": write_title, "body": "受保護寫入"}, member_token, barrier)
        logout, write = logout.result(timeout=20), write.result(timeout=20)
    expect(logout, 200)
    check(write[0] in (201, 401), "登出競爭寫入回應不符")
    expect(write, write[0], "E0002" if write[0] == 401 else None)
    check(sql(f"SELECT count(*) FROM articles WHERE title='{write_title}'") ==
          ("1" if write[0] == 201 else "0"), "登出競爭寫入回應與提交資料不一致")
    expect(request(backend, "PUT", f"/api/members/{member_id}/status",
        {"status": "disabled"}, admin_token), 200)
    expect(request(backend, "GET", "/api/me", token=member_token), 403, "E0102")
    denied_title = f"t57-{nonce}-{backend}-disabled-write"
    expect(request(backend, "POST", "/api/articles",
        {"title": denied_title, "body": "不得寫入"}, member_token), 403, "E0102")
    check(sql(f"SELECT count(*) FROM articles WHERE title='{denied_title}'") == "0", "停用帳號仍寫入內容")
    expect(request(backend, "PUT", f"/api/members/{member_id}/status",
        {"status": "active"}, admin_token), 200)
    expect(request(backend, "GET", "/api/me", token=member_token), 401, "E0002")
    print(f"PASS {backend} 登出競爭寫入、停用優先、恢復後撤銷與無部分寫入", flush=True)


# extended_invalid_cases 由端點欄位契約產生有限等價類，逐案核對錯誤與拒絕後狀態。
def extended_invalid_cases(backend, nonce, reference):
    member = user(backend, nonce + "i", "member")
    other = user(backend, nonce + "i", "other")
    admin = user(backend, nonce + "i", "admin")
    title = f"t57-{nonce}-{backend}-invalid"
    expect(request(backend, "POST", "/api/articles", {"title": title, "body": "原文"}, member["token"]), 201)
    article_id = record_id("articles", "title", title)
    comment_body = title + "-comment"
    expect(request(backend, "POST", f"/api/articles/{article_id}/comments", {"body": comment_body}, member["token"]), 201)
    comment_id = record_id("comments", "body", comment_body)
    settings = expect(request(backend, "GET", "/api/settings/login-lockout", token=admin["token"]), 200)
    email = f"t57-{nonce}-{backend}-rejected@example.test"
    article_path = f"/api/articles/{article_id}"
    comment_path = f"/api/comments/{comment_id}"
    # 每個描述子保留同一端點的有效基準；所有產生的變體都必須被拒絕。
    endpoints = (
        ("01", "POST", "/api/auth/register", None, {"email": email, "password": "Abcd1234", "nickname": "驗證"}),
        ("02", "POST", "/api/auth/login", None, {"email": member["email"], "password": "Abcd1234"}),
        ("03", "POST", "/api/auth/logout", member["token"], {}),
        ("04", "GET", "/api/me", member["token"], None),
        ("05", "GET", "/api/articles?page=1&per_page=20", None, None),
        ("06", "POST", "/api/articles", member["token"], {"title": title + "-new", "body": "內文"}),
        ("07", "GET", article_path, None, None),
        ("08", "PUT", article_path, member["token"], {"title": title, "body": "改文", "version": 1}),
        ("09", "GET", "/api/me/articles?page=1&per_page=20", member["token"], None),
        ("10", "GET", "/api/me/comments?page=1&per_page=20", member["token"], None),
        ("11", "PUT", article_path + "/visibility", member["token"], {"visibility": "hidden", "version": 1}),
        ("12", "GET", article_path + "/comments?page=1&per_page=20", None, None),
        ("13", "POST", article_path + "/comments", member["token"], {"body": comment_body + "-new"}),
        ("14", "GET", comment_path, member["token"], None),
        ("15", "PUT", comment_path, member["token"], {"body": "改留言", "version": 1}),
        ("16", "PUT", comment_path + "/visibility", member["token"], {"visibility": "hidden", "version": 1}),
        ("17", "GET", "/api/members?page=1&per_page=20", admin["token"], None),
        ("18", "GET", f"/api/members/{member['id']}", admin["token"], None),
        ("19", "PUT", f"/api/members/{member['id']}/status", admin["token"], {"status": "disabled"}),
        ("20", "GET", "/api/comments?page=1&per_page=20", admin["token"], None),
        ("21", "GET", "/api/settings/login-lockout", admin["token"], None),
        ("22", "PUT", "/api/settings/login-lockout", admin["token"], settings),
    )
    messages = {"missing": "此欄位為必填", "null": "此欄位不可為 null",
                "type": "欄位型態不正確", "range": "欄位格式或範圍不正確"}
    counts = {}
    for number, method, path, token, body in endpoints:
        cases = []
        base_path = path.split("?", 1)[0]
        # 未知 Query 適用全部端點；列表另外逐一驗證缺漏、重複、陣列與範圍。
        cases.append(("query-unknown", path + ("&" if "?" in path else "?") + "unexpected=1", body,
                      422, "E0001", {"unexpected": ["不支援此欄位"]}))
        if method == "GET":
            cases.append(("get-body", path, '{"unexpected":1}', 422, "E0001",
                          {"_body": ["此 API 不接受 Request Body"]}))
        else:
            if number != "03":
                cases.append(("root-missing", path, None, 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]}))
            cases.extend((("root-array", path, [], 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]}),
                          ("root-malformed", path, "{", 400, "E0005", None),
                          ("field-unknown", path, {**body, "unexpected": 1}, 422, "E0001",
                           {"unexpected": ["不支援此欄位"]})))
            if body:
                for field, value in body.items():
                    invalid_type = "wrong" if isinstance(value, (bool, int)) else 1
                    invalid_range = 0 if isinstance(value, int) and not isinstance(value, bool) else (
                        "maybe" if field in ("visibility", "status") else "")
                    variants = (("missing", None), ("null", None), ("type", invalid_type))
                    if not isinstance(value, bool):
                        variants += (("range", invalid_range),)
                    for kind, replacement in variants:
                        changed = {key: item for key, item in body.items() if key != field} if kind == "missing" else {**body, field: replacement}
                        cases.append((f"{field}-{kind}", path, changed, 422, "E0001",
                                      {field: [messages[kind]]}))
                    if field in ("nickname", "title", "body"):
                        special = {**body, field: "錯\x00字"}
                        encoded = json.dumps(special, ensure_ascii=False).encode("utf-8")
                        check(b"\\u0000" in encoded and b"\x00" not in encoded and
                              json.loads(encoded) == special, "NUL 送出前序列化檢查失敗")
                        cases.append((f"{field}-nul", path, encoded.decode("utf-8"), 422, "E0001",
                                      {field: [messages["range"]]}))
                if len(body) >= 2:
                    first, second = sorted(body)[:2]
                    cases.append(("aggregate-missing-null", path,
                                  {key: value for key, value in body.items() if key != first} | {second: None},
                                  422, "E0001", {first: [messages["missing"]], second: [messages["null"]]}))
                cases.append(("readonly", path, {**body, "created_backend": "laravel"}, 422, "E0001",
                              {"created_backend": ["不支援此欄位"]}))
        if "?" in path:
            cases.extend((("query-missing", base_path + "?per_page=20", None, 422, "E0001", {"page": [messages["missing"]]}),
                          ("query-per-page-missing", base_path + "?page=1", None, 422, "E0001", {"per_page": [messages["missing"]]}),
                          ("query-format", base_path + "?page=bad&per_page=20", None, 422, "E0001", {"page": [messages["range"]]}),
                          ("query-per-page-format", base_path + "?page=1&per_page=bad", None, 422, "E0001", {"per_page": [messages["range"]]}),
                          ("query-page-lower", base_path + "?page=0&per_page=20", None, 422, "E0001", {"page": [messages["range"]]}),
                          ("query-per-page-lower", base_path + "?page=1&per_page=0", None, 422, "E0001", {"per_page": [messages["range"]]}),
                          ("query-range", base_path + "?page=1&per_page=101", None, 422, "E0001", {"per_page": [messages["range"]]}),
                          ("query-duplicate", base_path + "?page=bad&page=1&per_page=20", None, 422, "E0001", {"page": ["此參數不可重複"]}),
                          ("query-per-page-duplicate", base_path + "?page=1&per_page=bad&per_page=20", None, 422, "E0001", {"per_page": ["此參數不可重複"]}),
                          ("query-array", base_path + "?page%5B%5D=1&per_page=20", None, 422, "E0001", {"page": [messages["type"]]}),
                          ("query-per-page-array", base_path + "?page=1&per_page%5B%5D=20", None, 422, "E0001", {"per_page": [messages["type"]]}),
                          ("query-aggregate", base_path + "?page=bad&per_page=101", None, 422, "E0001",
                           {"page": [messages["range"]], "per_page": [messages["range"]]})))
            if number == "05":
                cases.append(("query-scope", base_path + "?page=1&per_page=20&filters%5Bscope%5D=bad",
                              None, 422, "E0001", {"filters.scope": [messages["range"]]}))
            if number == "17":
                cases.append(("query-keyword", base_path + "?page=1&per_page=20&filters%5Bkeyword%5D=" + "x" * 255,
                              None, 422, "E0001", {"filters.keyword": [messages["range"]]}))
            if number in ("05", "17"):
                cases.append(("query-nested-unknown", base_path + "?page=1&per_page=20&filters%5Bunexpected%5D=1",
                              None, 422, "E0001", {"filters.unexpected": ["不支援此欄位"]}))
        if "{" not in base_path and any(base_path.startswith(prefix) for prefix in
                                        ("/api/articles/", "/api/comments/", "/api/members/")):
            for invalid in ("bad", "9007199254740992"):
                segment = str(article_id if base_path.startswith("/api/articles/") else
                              comment_id if base_path.startswith("/api/comments/") else member["id"])
                new_path = base_path.replace("/" + segment, "/" + invalid, 1)
                if "?" in path:
                    new_path += "?page=1&per_page=20"
                key = "article_id" if base_path.startswith("/api/articles/") else "comment_id" if base_path.startswith("/api/comments/") else "member_id"
                cases.append(("path-" + invalid, new_path, body, 422, "E0001", {key: [messages["range"]]}))
        # 使用相同登入與資料快照，確認任何拒絕請求都沒有部分寫入或鎖定計數。
        snapshot = sql(f"SELECT title||'|'||body||'|'||version||'|'||visibility FROM articles WHERE id={article_id}")
        comment_snapshot = sql(f"SELECT body||'|'||version||'|'||visibility FROM comments WHERE id={comment_id}")
        failure_count = sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}")
        revoked_count = sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL")
        session_count = sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']}")
        article_count = sql(f"SELECT count(*) FROM articles WHERE author_id={member['id']}")
        comment_count = sql(f"SELECT count(*) FROM comments WHERE author_id={member['id']}")
        for label, case_path, case_body, status, code, fields in cases:
            result = request(backend, method, case_path, case_body, token)
            try:
                normalized = expect(result, status, code, fields)
            except AssertionError as error:
                raise AssertionError(f"{backend} {number}/{label} {method} {case_path}: {error}; actual={result[:2]}") from error
            key = number + "/" + label
            if key in reference:
                check(normalized == reference[key],
                      f"{backend} {key} 與第一後端錯誤回應不同：expected={reference[key]} actual={normalized}")
            else:
                reference[key] = normalized
            counts[key] = counts.get(key, 0) + 1
        check(sql(f"SELECT title||'|'||body||'|'||version||'|'||visibility FROM articles WHERE id={article_id}") == snapshot and
              sql(f"SELECT body||'|'||version||'|'||visibility FROM comments WHERE id={comment_id}") == comment_snapshot and
              sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}") == failure_count and
              sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL") == revoked_count and
              sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']}") == session_count and
              sql(f"SELECT count(*) FROM articles WHERE author_id={member['id']}") == article_count and
              sql(f"SELECT count(*) FROM comments WHERE author_id={member['id']}") == comment_count and
              sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "0" and
              expect(request(backend, "GET", "/api/me", token=member["token"]), 200)["id"] == member["id"] and
              expect(request(backend, "GET", "/api/settings/login-lockout", token=admin["token"]), 200) == settings,
              f"{backend} {number} 拒絕後資料、session、失敗計數或設定變動")
    # 公開認證不沿用舊 JWT；受保護端點依 API08 先驗身分／角色再驗參數與版本。
    expect(request(backend, "POST", "/api/auth/register",
                   {"email": email, "password": None, "nickname": "驗證"}, "invalid"),
           422, "E0001", {"password": [messages["null"]]})
    expect(request(backend, "POST", "/api/auth/login", "{", "invalid"), 400, "E0005")
    expect(request(backend, "PUT", article_path, {"title": title, "body": "拒絕", "version": 0}), 401, "E0002")
    expect(request(backend, "PUT", article_path, "{", "invalid"), 401, "E0002")
    expect(request(backend, "GET", article_path, '{"unexpected":1}', "invalid"), 401, "E0002")
    expect(request(backend, "GET", "/api/members/bad", token=member["token"]), 403, "E0003")
    expect(request(backend, "GET", "/api/members?page=bad&per_page=20", token=member["token"]), 403, "E0003")
    expect(request(backend, "PUT", f"/api/members/{member['id']}/status", {"status": "bad"},
                   member["token"]), 403, "E0003")
    expect(request(backend, "PUT", article_path, {"title": title, "body": "拒絕", "version": 2},
                   other["token"]), 403, "E0003")
    expect(request(backend, "PUT", article_path.replace(str(article_id), "9007199254740991"),
                   {"title": title, "body": "拒絕", "version": 1}, member["token"]), 404, "E0004")
    expect(request(backend, "PUT", comment_path, {"body": "拒絕", "version": 2}, other["token"]), 403, "E0003")
    check(sql(f"SELECT body||'|'||version FROM articles WHERE id={article_id}") == "原文|1",
          "錯誤順序案例寫入文章")
    required = {number for number, *_ in endpoints}
    for number, method, path, _, body in endpoints:
        labels = {key.split("/", 1)[1] for key in counts if key.startswith(number + "/")}
        expected = {"query-unknown", "get-body"} if method == "GET" else {
            "query-unknown", "root-array", "root-malformed", "field-unknown"}
        if method != "GET" and number != "03":
            expected.add("root-missing")
        if body:
            expected |= {f"{field}-{kind}" for field, value in body.items()
                         for kind in (("missing", "null", "type") if isinstance(value, bool)
                                      else ("missing", "null", "type", "range"))}
            expected.add("readonly")
            expected |= {f"{field}-nul" for field in body if field in ("nickname", "title", "body")}
            if len(body) >= 2:
                expected.add("aggregate-missing-null")
        if "?" in path:
            expected |= {"query-missing", "query-format", "query-range", "query-duplicate",
                         "query-array", "query-aggregate", "query-per-page-missing",
                         "query-per-page-format", "query-page-lower", "query-per-page-lower",
                         "query-per-page-duplicate", "query-per-page-array"}
        if number in ("05", "17"):
            expected.add("query-scope" if number == "05" else "query-keyword")
            expected.add("query-nested-unknown")
        if number in ("07", "08", "11", "12", "13", "14", "15", "16", "18", "19"):
            expected |= {"path-bad", "path-9007199254740992"}
        check(expected <= labels, f"{backend} {number} 必備等價類缺漏：{expected - labels}")
    check({key.split("/", 1)[0] for key in counts} == required and
          all(count == 1 for count in counts.values()), f"{backend} 端點或案例覆蓋記帳缺漏")
    print(f"PASS {backend} extended invalid：{len(required)} 端點、{len(counts)} 案例", flush=True)
    return counts


# ordered_cases 用新資料重播每對操作的兩種確定先後，核對回應與已提交狀態。
def ordered_cases(backend, nonce):
    peer = BACKENDS[(BACKENDS.index(backend) + 1) % len(BACKENDS)]
    admin = user(backend, nonce + "o", "admin")
    original = expect(request(backend, "GET", "/api/settings/login-lockout", token=admin["token"]), 200)
    count = 0
    try:
        for reverse in (False, True):
            # 文章與留言的第二次同版本更新必須失敗，且第一個提交值和版本不變。
            member = user(backend, nonce + "o" + str(int(reverse)), "member")
            title = f"t57-{nonce}-{backend}-order-{int(reverse)}"
            expect(request(backend, "POST", "/api/articles", {"title": title, "body": "原文"}, member["token"]), 201)
            article_id = record_id("articles", "title", title)
            article_path = f"/api/articles/{article_id}"
            comment_body = title + "-comment"
            expect(request(backend, "POST", article_path + "/comments", {"body": comment_body}, member["token"]), 201)
            comment_id = record_id("comments", "body", comment_body)
            comment_path = f"/api/comments/{comment_id}"
            for kind, path, sql_table, values in (("article", article_path, "articles", ("甲文", "乙文")),
                                                  ("comment", comment_path, "comments", ("甲留言", "乙留言"))):
                first, second = values[::-1] if reverse else values
                first_body = {"body": first, "version": 1}
                second_body = {"body": second, "version": 1}
                if kind == "article":
                    first_body["title"] = second_body["title"] = title
                expect(request(backend, "PUT", path, first_body, member["token"]), 200)
                expect(request(peer, "PUT", path, second_body,
                               expect(request(peer, "POST", "/api/auth/login", {"email": member["email"],
                                           "password": "Abcd1234"}), 200)["access_token"]), 409, "E0201")
                row_id = article_id if kind == "article" else comment_id
                check(sql(f"SELECT body||'|'||version FROM {sql_table} WHERE id={row_id}") == first + "|2",
                      f"{kind} 已提交的第一筆被過期寫入覆蓋")
                count += 1

            # 正規化後相同 Email 在兩個後端各先送一次，僅建立一筆帳號。
            email = f"t57-{nonce}-{backend}-email-{int(reverse)}@example.test"
            registration = {"email": "  " + email.upper() + "  ", "password": "Abcd1234", "nickname": "先註冊"}
            first_backend, second_backend = (peer, backend) if reverse else (backend, peer)
            expect(request(first_backend, "POST", "/api/auth/register", registration), 201)
            expect(request(second_backend, "POST", "/api/auth/register", registration), 422, "E0001",
                   {"email": ["此 Email 已註冊"]})
            check(sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "1", "同 Email 順序註冊重複")
            count += 1

            # 門檻 2 的兩次合法格式錯誤密碼，第一筆保存為 401，第二筆保存並鎖定為 429。
            lock_member = user(backend, nonce + "o" + str(int(reverse)), "lock")
            lock_settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 30}
            expect(request(backend, "PUT", "/api/settings/login-lockout", lock_settings, admin["token"]), 200)
            wrong = {"email": lock_member["email"], "password": "Wrong1234"}
            expect(request(first_backend, "POST", "/api/auth/login", wrong), 401, "E0101")
            expect(request(second_backend, "POST", "/api/auth/login", wrong), 429, "E0105")
            check(sql(f"SELECT count(*) FROM login_failures WHERE account_id={lock_member['id']}") == "2" and
                  sql(f"SELECT locked_until IS NOT NULL FROM accounts WHERE id={lock_member['id']}") == "t",
                  "門檻順序沒有保存兩次失敗及鎖定")
            count += 1

            # 留言先提交才下架可保留；文章先下架時新留言須拒絕且不留下資料。
            hide_title = title + "-hide"
            expect(request(backend, "POST", "/api/articles", {"title": hide_title, "body": "原文"}, member["token"]), 201)
            hide_id = record_id("articles", "title", hide_title)
            hide_path = f"/api/articles/{hide_id}"
            new_comment = hide_title + "-comment"
            if reverse:
                expect(request(backend, "PUT", hide_path + "/visibility",
                               {"visibility": "hidden", "version": 1}, member["token"]), 200)
                expect(request(backend, "POST", hide_path + "/comments", {"body": new_comment}, member["token"]),
                       403, "E0003")
            else:
                expect(request(backend, "POST", hide_path + "/comments", {"body": new_comment}, member["token"]), 201)
                expect(request(backend, "PUT", hide_path + "/visibility",
                               {"visibility": "hidden", "version": 1}, member["token"]), 200)
            check(sql(f"SELECT visibility||'|'||version FROM articles WHERE id={hide_id}") == "hidden|2" and
                  sql(f"SELECT count(*) FROM comments WHERE article_id={hide_id} AND body='{new_comment}'") ==
                  ("0" if reverse else "1"), "文章下架／新留言提交先後不符")
            count += 1

            # 登出與停用各用全新帳號，避免已撤銷憑證混淆先後判斷。
            for action in ("logout", "disable"):
                actor = user(backend, nonce + "o" + str(int(reverse)), action)
                write_title = f"t57-{nonce}-{backend}-{action}-{int(reverse)}"
                if action == "logout":
                    denied_status, denied_code = 401, "E0002"
                else:
                    denied_status, denied_code = 403, "E0102"
                if reverse:
                    if action == "logout":
                        expect(request(backend, "POST", "/api/auth/logout", {}, actor["token"]), 200)
                    else:
                        expect(request(backend, "PUT", f"/api/members/{actor['id']}/status",
                                       {"status": "disabled"}, admin["token"]), 200)
                    expect(request(backend, "POST", "/api/articles",
                                   {"title": write_title, "body": "有序寫入"}, actor["token"]),
                           denied_status, denied_code)
                else:
                    expect(request(backend, "POST", "/api/articles",
                                   {"title": write_title, "body": "有序寫入"}, actor["token"]), 201)
                    if action == "logout":
                        expect(request(backend, "POST", "/api/auth/logout", {}, actor["token"]), 200)
                    else:
                        expect(request(backend, "PUT", f"/api/members/{actor['id']}/status",
                                       {"status": "disabled"}, admin["token"]), 200)
                check(sql(f"SELECT count(*) FROM articles WHERE title='{write_title}'") ==
                      ("0" if reverse else "1"), f"{action}／寫入提交先後不符")
                count += 1

            # 兩筆管理員設定均提供完整四欄；最後提交的整組值必須可由對端讀回。
            changed_a = {"enabled": True, "window_minutes": 7, "failure_threshold": 3, "lock_seconds": 31}
            changed_b = {"enabled": True, "window_minutes": 8, "failure_threshold": 4, "lock_seconds": 32}
            first, second = (changed_b, changed_a) if reverse else (changed_a, changed_b)
            peer_admin = expect(request(peer, "POST", "/api/auth/login",
                {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
            expect(request(backend, "PUT", "/api/settings/login-lockout", first, admin["token"]), 200)
            expect(request(peer, "PUT", "/api/settings/login-lockout", second, peer_admin), 200)
            check(expect(request(backend, "GET", "/api/settings/login-lockout", token=admin["token"]), 200) == second and
                  expect(request(peer, "GET", "/api/settings/login-lockout", token=peer_admin), 200) == second,
                  "管理設定最後完整提交值不一致")
            count += 1
            # 同時完整寫入時不指定贏家；只接受其中一組完整四欄提交。
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(synchronized_request, current, "PUT", "/api/settings/login-lockout",
                                        payload, credential, barrier)
                            for current, payload, credential in ((backend, changed_a, admin["token"]),
                                                                 (peer, changed_b, peer_admin))]
                attempts = [future.result(timeout=20) for future in attempts]
            for result in attempts:
                expect(result, 200)
            committed = expect(request(backend, "GET", "/api/settings/login-lockout", token=admin["token"]), 200)
            check(committed in (changed_a, changed_b) and
                  expect(request(peer, "GET", "/api/settings/login-lockout", token=peer_admin), 200) == committed and
                  sql("SELECT enabled::text||'|'||window_minutes||'|'||failure_threshold||'|'||lock_seconds "
                      "FROM login_lockout_settings WHERE id=1") ==
                  f"true|{committed['window_minutes']}|{committed['failure_threshold']}|{committed['lock_seconds']}",
                  "同時管理設定留下混合欄位或跨後端讀值不符")
            count += 1
    finally:
        expect(request(backend, "PUT", "/api/settings/login-lockout", original, admin["token"]), 200)
        check(expect(request(backend, "GET", "/api/settings/login-lockout", token=admin["token"]), 200) == original,
              "有序案例未恢復管理設定")
    check(count == 18, "雙序操作覆蓋記帳缺漏")
    print(f"PASS {backend} ordered：{count} 雙序操作案例", flush=True)


# main 先核對隔離容器與資料庫，再依模式驗證無效輸入、雙序操作、共用邊界或完整流程，避免誤寫展示資料。
def main():
    guard()
    # 上次測試若被中斷，只移除本測試固定名稱的殘留物件，再開始建立新測資。
    clear_boundary_trigger()
    nonce = secrets.token_hex(5)
    # 擴充模式分開執行，以免管理設定與登入計數互相干擾。
    check(sys.argv[1:] in ([], ["--review-boundaries"], ["--extended-invalid"], ["--extended-ordered"]), "未知驗證模式")
    if sys.argv[1:] == ["--extended-invalid"]:
        reference = {}
        for backend in BACKENDS:
            extended_invalid_cases(backend, nonce, reference)
        print(f"PASS Ticket 57 extended invalid：十二後端各 {len(reference)} 案例，完整錯誤回應逐案一致", flush=True)
        return
    if sys.argv[1:] == ["--extended-ordered"]:
        for backend in BACKENDS:
            ordered_cases(backend, nonce)
        print("PASS Ticket 57 extended ordered：十二後端各 18 個雙序／同步案例", flush=True)
        return
    if sys.argv[1:] == ["--review-boundaries"]:
        # Review 修正只跑新增的共用邊界，避免重做已具有效證據的文章生命週期。
        for backend in BACKENDS:
            boundary_cases(backend, nonce)
            lockout_edge_cases(backend, nonce)
        print("PASS Ticket 57 review 邊界：十二後端 API14／API15／log_id／DBV02", flush=True)
        return
    records = {}
    for backend in BACKENDS:
        records[backend] = backend_cases(backend, nonce)
        contract_cases(backend, nonce)
        boundary_cases(backend, nonce)
        lockout_cases(backend, nonce)
        lockout_edge_cases(backend, nonce)
        database_cases(backend, nonce)
    for creator, (article_id, comment_id) in records.items():
        for reader in BACKENDS:
            article = expect(request(reader, "GET", f"/api/articles/{article_id}"), 200)
            comments = expect(request(reader, "GET", f"/api/articles/{article_id}/comments?page=1&per_page=100"), 200)
            comment = next((item for item in comments["list"] if item["id"] == comment_id), None)
            check(article["created_backend"] == creator and article["id"] == article_id and
                  article["body"] == "更新後內文" and comment is not None and comment["created_backend"] == creator and
                  comment["id"] == comment_id and comment["body"] == "更新後留言", "跨後端資料或來源不符")
        print(f"PASS {creator} 建立資料由十二後端讀取", flush=True)
    print("PASS Ticket 57 HTTP：十二後端共同操作、隔離資料庫競爭與跨後端讀取", flush=True)
    print("HTTP 腳本範圍限制：未覆蓋全部競爭先後與完整 API 矩陣；瀏覽器、Compose 故障與 SQL 查詢計畫由其他驗證處理")


if __name__ == "__main__":
    main()
