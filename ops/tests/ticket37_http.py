"""Ticket 37：隔離資料庫上的 Fastify HTTP、跨後端與查詢計畫驗證。"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError
from urllib.request import Request, urlopen

FASTIFY = "http://127.0.0.1:18373"
LARAVEL = "http://127.0.0.1:18372"
MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出", "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能", "E0004": "找不到指定資料或 API",
    "E0005": "系統暫時無法處理，請稍後再試", "E0101": "帳號或密碼錯誤",
    "E0102": "此帳號已停用，請聯絡系統管理員", "E0104": "後台認證設定異常，請聯絡系統管理員",
    "E0105": "登入失敗次數過多，請稍後再試", "E9999": "系統暫時無法處理，請稍後再試",
}


# check 在首個契約偏差立即停止，避免後續寫入掩蓋原因。
def check(condition, message):
    if not condition:
        raise AssertionError(message)


# sql 只在 guard 核對的隔離專案中執行固定測試查詢。
def sql(query):
    command = ["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A", "-U", "articles_owner", "-d", "articles", "-c", query]
    return subprocess.check_output(command, text=True).strip()


# guard 實際核對專案、資料卷、金鑰掛載、埠與健康狀態，再允許新增資料。
def guard():
    check(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket37_test", "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == "compose.yaml:ops/tests/ticket37_compose.yaml", "錯誤 Compose 檔")
    config = json.loads(subprocess.check_output(["docker", "compose", "--profile", "fastify", "config", "--format", "json"], text=True))
    check(config["name"] == "ticket37_test" and config["volumes"]["postgres_data"]["name"] == "ticket37_test_postgres_data", "非隔離資料卷")
    check(config["services"]["db"]["environment"]["TICKET37_FIXTURE"] == "isolated-ticket37", "fixture 標記錯誤")
    for service, port in (("laravel", "18372"), ("fastify", "18373")):
        ids = subprocess.check_output(["docker", "compose", "--profile", "fastify", "ps", "-q", service], text=True).splitlines()
        check(len(ids) == 1, "後端容器未唯一啟動：" + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == "ticket37_test", "容器不屬於本票")
        check(item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy", "後端不健康")
        check(item["HostConfig"]["PortBindings"]["8000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": port}], "實際埠錯誤")
        path = os.path.realpath(f".local/ticket37-backend/{service}.key")
        check(any(os.path.realpath(m.get("Source", "")) == path and m.get("Destination") == f"/run/secrets/{service}-jwt.key" and m.get("RW") is False for m in item["Mounts"]), "測試金鑰掛載錯誤")
        with open(path, "rb") as test_key, open(f".local/keys/{service}.key", "rb") as demo_key:
            check(test_key.read() != demo_key.read(), "測試與展示金鑰共用")
    ids = subprocess.check_output(["docker", "compose", "ps", "-q", "db"], text=True).splitlines()
    check(len(ids) == 1, "隔離 DB 未唯一啟動")
    database = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    check(any(m.get("Name") == "ticket37_test_postgres_data" for m in database["Mounts"]), "DB 資料卷錯誤")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 身分錯誤")
    for base in (FASTIFY, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")


# call 保留原始 Body/Query，避免客戶端預先正規化特別輸入。
def call(base, method, path, body=None, token=None, headers=None):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = "Bearer " + token
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode()
        request_headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        body = body.encode()
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(base + path, data=body, headers=request_headers, method=method)
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, response.headers


# expect 精確比對狀態、共同文案、envelope 與指定欄位錯誤。
def expect(result, status, code=None, fields=None):
    actual, payload, _ = result
    check(actual == status, f"HTTP {actual}，預期 {status}/{code}: {payload}")
    if code is None:
        check(set(payload) == {"success", "message", "data"} and payload["success"] is True and payload["message"] == "OK", "成功 envelope")
        return payload["data"]
    keys = {"success", "message", "error_code", "log_id"}
    if fields is not None:
        keys.add("errors")
    check(set(payload) == keys and payload["success"] is False and payload["error_code"] == code and payload["message"] == MESSAGES[code] and payload["log_id"], "錯誤 envelope")
    if fields is not None:
        check(payload["errors"] == fields, f"errors 不符：{payload['errors']}")
    return payload


# register 建立每次執行都唯一的會員，測試可在同一隔離資料卷重跑。
def register(tag):
    email = f"ticket37-{tag}@example.test"
    expect(call(FASTIFY, "POST", "/api/auth/register", {"email": " " + email.upper() + " ", "password": "Abcd1234", "nickname": "測試作者"}), 201)
    data = expect(call(FASTIFY, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    check(data["user"]["email"] == email and data["user"]["role"] == "member", "註冊與登入資料")
    return email, data


# update_lock_settings 透過管理 API 寫入完整四欄，保留關閉時的清鎖與世代副作用。
def update_lock_settings(token, **changes):
    current = expect(call(LARAVEL, "GET", "/api/settings/login-lockout", token=token), 200)
    wanted = {**current, **changes}
    expect(call(LARAVEL, "PUT", "/api/settings/login-lockout", wanted, token=token), 200)
    check(expect(call(LARAVEL, "GET", "/api/settings/login-lockout", token=token), 200) == wanted, "共用設定更新不完整")
    return wanted


# temporary_lock_settings 無論案例成功或拋錯都透過管理 API 恢復原本的四欄設定。
@contextmanager
def temporary_lock_settings(token, threshold, seconds):
    original = expect(call(LARAVEL, "GET", "/api/settings/login-lockout", token=token), 200)
    check(original["enabled"] is True, "鎖定測試須從啟用狀態開始")
    try:
        update_lock_settings(token, failure_threshold=threshold, lock_seconds=seconds)
        yield
    finally:
        update_lock_settings(token, **original)


# boundary_login 使用已核對的隔離 Fastify 容器執行 stdin 測試，避免修改生產映像。
def boundary_login():
    source = os.path.join(os.path.dirname(__file__), "ticket37_boundary.js")
    with open(source, encoding="utf-8") as test_file:
        script = test_file.read()
    result = subprocess.run(["docker", "compose", "--profile", "fastify", "exec", "-T", "fastify", "node", "-"],
                            input=script, text=True, capture_output=True, timeout=60)
    check(result.returncode == 0, f"真實 SQL 邊界案例失敗：{result.stderr.strip()}")
    check("PASS ticket37 real login SQL boundary" in result.stdout, "真實 SQL 邊界案例未執行")
    print(result.stdout.strip())


# cross_attempt 讓兩個後端同時進入登入流程，檢查共用設定列鎖不會漏計。
def cross_attempt(barrier, base, email):
    barrier.wait(timeout=10)
    return call(base, "POST", "/api/auth/login", {"email": email, "password": "Wrong1234"})


# parity_error 比對共同錯誤內容，逐請求驗證不同的 UUID 追查碼與後端 IN／EX／OUT 記錄。
def parity_error(base, result, status, code, fields=None):
    payload = expect(result, status, code, fields)
    log_id = payload["log_id"]
    check(str(uuid.UUID(log_id)) == log_id and uuid.UUID(log_id).version == 4, "log_id 格式不符")
    if base == FASTIFY:
        logs = subprocess.check_output(["docker", "compose", "logs", "--no-color", "fastify"], text=True)
    else:
        logs = "\n".join(path.read_text(errors="replace") for path in Path(".local/ticket37-backend/laravel-logs").glob("api-*.log"))
    check(all(any(re.search(rf"\b{event}\b", line) and log_id in line for line in logs.splitlines())
              for event in ("IN", "EX", "OUT")), "log_id 缺少可追查記錄")
    return {key: value for key, value in payload.items() if key != "log_id"}


# parity_token 驗證各後端自己的簽章、claims、資料庫 session 與回應到期時間關聯。
def parity_token(backend, data, account_id):
    token = data["access_token"]
    parts = token.split(".")
    check(len(parts) == 3, "JWT 段數")
    header, claims = [json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts[:2]]
    key = base64.b64decode(Path(f".local/ticket37-backend/{backend}.key").read_text().strip())
    signature = base64.urlsafe_b64encode(hmac.new(key, ".".join(parts[:2]).encode(), hashlib.sha256).digest()).decode().rstrip("=")
    check(hmac.compare_digest(signature, parts[2]) and header == {"alg": "HS256", "typ": "JWT"}, "JWT 簽章或 header")
    check(set(claims) == {"iss", "aud", "sub", "jti", "iat", "nbf", "exp"} and
          claims["iss"] == f"ai-workflow-fullstack:{backend}" and claims["aud"] == f"ai-workflow-fullstack-api:{backend}" and
          claims["sub"] == str(account_id) and isinstance(claims["iat"], int) and claims["nbf"] == claims["iat"] and
          claims["exp"] == claims["iat"] + 28800, "JWT claims")
    check(re.fullmatch(r"[A-Za-z0-9_-]{22}", claims["jti"]) and len(base64.urlsafe_b64decode(claims["jti"] + "==")) == 16 and
          data["expires_at"] == datetime.fromtimestamp(claims["exp"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"), "JWT jti／到期時間")
    check(sql(f"SELECT account_id||'|'||backend_id||'|'||(revoked_at IS NULL)::text FROM login_sessions WHERE jti='{claims['jti']}'") ==
          f"{account_id}|{backend}|true", "登入紀錄未與 JWT 關聯")
    return claims


# parity_auth_case 用兩筆獨立但同狀態的帳號重播 01–04，避免前次寫入改變後次前提。
def parity_auth_case(base, backend, tag):
    email = f"ticket37-parity-{tag}-{backend}@example.test"
    body = {"email": " " + email.upper() + " ", "password": "Abcd1234", "nickname": "共同作者"}
    check(expect(call(base, "POST", "/api/auth/register", body), 201) == {}, "註冊寫入回應")
    account_id = int(sql(f"SELECT id FROM accounts WHERE email='{email}'"))
    created = sql(f"SELECT to_char(created_at AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD HH24:MI:SS') FROM accounts WHERE id={account_id}")
    check(account_id > 0 and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", created), "帳號 ID／時間")
    errors = [parity_error(base, call(base, "POST", "/api/auth/register", {"password": "Abcd1234", "nickname": "共同作者"}), 422, "E0001", {"email": ["此欄位為必填"]}),
              parity_error(base, call(base, "POST", "/api/auth/register", body), 422, "E0001", {"email": ["此 Email 已註冊"]}),
              parity_error(base, call(base, "POST", "/api/auth/login", {"email": email, "password": "Bad!"}), 422, "E0001", {"password": ["欄位格式或範圍不正確"]}),
              parity_error(base, call(base, "POST", "/api/auth/login", {"email": email, "password": "Wrong1234"}), 401, "E0101")]
    check(sql(f"SELECT count(*) FROM accounts WHERE email='{email}'") == "1" and
          sql(f"SELECT count(*) FROM login_failures WHERE account_id={account_id}") == "1", "註冊／登入錯誤副作用")
    login = expect(call(base, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    account = {"id": account_id, "email": email, "nickname": "共同作者", "role": "member", "status": "active", "created_at": created}
    check(set(login) == {"access_token", "token_type", "expires_at", "user"} and login["token_type"] == "Bearer" and login["user"] == account, "登入欄位")
    claims = parity_token(backend, login, account_id)
    token = login["access_token"]
    check(expect(call(base, "GET", "/api/me", token=token), 200) == account, "目前登入者")
    errors += [parity_error(base, call(base, "GET", "/api/me"), 401, "E0002"),
               parity_error(base, call(base, "GET", "/api/me", body="{}", token=token), 422, "E0001", {"_body": ["此 API 不接受 Request Body"]}),
               parity_error(base, call(base, "POST", "/api/auth/logout", {"unexpected": 1}, token=token), 422, "E0001", {"unexpected": ["不支援此欄位"]})]
    check(sql(f"SELECT revoked_at IS NULL FROM login_sessions WHERE jti='{claims['jti']}'") == "t", "無效登出不得撤銷")
    check(expect(call(base, "POST", "/api/auth/logout", {}, token=token), 200) == {}, "登出回應")
    check(sql(f"SELECT revoked_at IS NOT NULL FROM login_sessions WHERE jti='{claims['jti']}'") == "t", "登出未撤銷 session")
    errors.append(parity_error(base, call(base, "GET", "/api/me", token=token), 401, "E0002"))
    return errors


# parity_read_case 在共同資料完成後以相同快照讀取 05、07、12，比對完整物件、分頁與錯誤。
def parity_read_case(base, article_ids, hidden_id, comment_ids):
    article_pages = [expect(call(base, "GET", f"/api/articles?page={page}&per_page=1"), 200) for page in (1, 2)]
    details = [expect(call(base, "GET", f"/api/articles/{article_id}"), 200) for article_id in article_ids]
    comments = [expect(call(base, "GET", f"/api/articles/{article_ids[0]}/comments?page={page}&per_page=1"), 200) for page in (1, 2, 3)]
    errors = [parity_error(base, call(base, "GET", "/api/articles?page=bad&per_page=1"), 422, "E0001", {"page": ["欄位格式或範圍不正確"]}),
              parity_error(base, call(base, "GET", f"/api/articles/{hidden_id}"), 404, "E0004"),
              parity_error(base, call(base, "GET", f"/api/articles/{article_ids[0]}/comments?page=0&per_page=1"), 422, "E0001", {"page": ["欄位格式或範圍不正確"]}),
              parity_error(base, call(base, "GET", f"/api/articles/{hidden_id}/comments?page=1&per_page=1"), 404, "E0004")]
    total = int(sql("SELECT count(*) FROM articles WHERE visibility='visible'"))
    check([page["list"][0]["id"] for page in article_pages] == article_ids[::-1] and
          all(page["pagination"] == {"page": index, "per_page": 1, "total": total, "total_pages": total}
              for index, page in enumerate(article_pages, 1)), "文章排序／分頁")
    check([page["list"][0]["id"] for page in comments[:2]] == comment_ids[::-1] and
          comments[2]["list"] == [] and all(page["pagination"] == {"page": index, "per_page": 1, "total": 2, "total_pages": 2}
              for index, page in enumerate(comments, 1)), "留言排序／分頁")
    check(all(item["title"].endswith(str(index)) and item["body"] == "共同內文\n第二行" and
              item["visibility"] == "visible" and item["version"] == 1 and item["created_backend"] == "laravel" and
              item["created_at"] == item["updated_at"] and item["author"]["nickname"] == "共同作者"
              for index, item in enumerate(details, 1)), "文章欄位／時間")
    check(all(item["body"].endswith(str(index)) and item["article_id"] == article_ids[0] and
              item["visibility"] == "visible" and item["version"] == 1 and item["created_backend"] == "laravel" and
              item["created_at"] == item["updated_at"] and item["author"] == details[0]["author"]
              for index, item in enumerate((comments[1]["list"][0], comments[0]["list"][0]), 1)), "留言欄位／時間")
    # 新生 ID 與顯示時間逐筆連回 SQL；兩個讀取後端不能只因彼此相同就略過產生規則。
    for article_id, item in zip(article_ids, details):
        check(item["id"] == article_id and item["author"]["id"] == int(sql(f"SELECT author_id FROM articles WHERE id={article_id}")) and
              item["created_at"] == sql(f"SELECT to_char(created_at AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD HH24:MI:SS') FROM articles WHERE id={article_id}"), "文章 ID／時間未關聯 SQL")
    for comment_id, item in zip(comment_ids, (comments[1]["list"][0], comments[0]["list"][0])):
        check(item["id"] == comment_id and item["created_at"] == sql(f"SELECT to_char(created_at AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD HH24:MI:SS') FROM comments WHERE id={comment_id}"), "留言 ID／時間未關聯 SQL")
    return article_pages, details, comments, errors


# parity_cases 以各後端重播認證，再由 Laravel API 建立共同內容供兩後端讀取。
def parity_cases(tag):
    auth = [parity_auth_case(base, backend, tag) for base, backend in ((LARAVEL, "laravel"), (FASTIFY, "fastify"))]
    check(auth[0] == auth[1], "01–04 共同錯誤或欄位不一致")
    article_ids = []
    email = f"ticket37-parity-{tag}-laravel@example.test"
    account_id = int(sql(f"SELECT id FROM accounts WHERE email='{email}'"))
    token = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)["access_token"]
    for index in (1, 2):
        title = f"共同文章 {tag} {index}"
        check(expect(call(LARAVEL, "POST", "/api/articles", {"title": title, "body": "共同內文\n第二行"}, token=token), 201) == {}, "文章測資")
        article_id = int(sql(f"SELECT id FROM articles WHERE title='{title}'"))
        article_ids.append(article_id)
        check(sql(f"SELECT created_backend||'|'||author_id FROM articles WHERE id={article_id}") ==
              f"laravel|{account_id}", "文章來源／作者寫入")
    for index in (1, 2):
        body = f"共同留言 {tag} {index}"
        check(expect(call(LARAVEL, "POST", f"/api/articles/{article_ids[0]}/comments", {"body": body}, token=token), 201) == {}, "留言測資")
        check(sql(f"SELECT created_backend||'|'||author_id FROM comments WHERE article_id={article_ids[0]} AND body='{body}'") ==
              f"laravel|{account_id}", "留言來源／作者寫入")
    title = f"共同下架 {tag}"
    expect(call(LARAVEL, "POST", "/api/articles", {"title": title, "body": "隱藏"}, token=token), 201)
    hidden_id = int(sql(f"SELECT id FROM articles WHERE title='{title}'"))
    expect(call(LARAVEL, "PUT", f"/api/articles/{hidden_id}/visibility", {"visibility": "hidden", "version": 1}, token=token), 200)
    # 兩次讀取之間無寫入，所有原始 ID、來源與時間均是同一筆 DB 資料，可逐欄完全比較。
    comment_ids = [int(value) for value in sql(f"SELECT id FROM comments WHERE article_id={article_ids[0]} ORDER BY id").splitlines()]
    first = parity_read_case(LARAVEL, article_ids, hidden_id, comment_ids)
    second = parity_read_case(FASTIFY, article_ids, hidden_id, comment_ids)
    check(first == second, "05／07／12 共同讀取、分頁或錯誤不一致")
    print("PASS ticket37 PARITY01 operations 01–05, 07, 12")


# main 先檢查隔離條件，再驗實際 HTTP、跨後端互通與 SQL 計畫。
def main():
    guard()
    tag = secrets.token_hex(5)
    parity_cases(tag)
    email, login = register(tag)
    token = login["access_token"]
    user_id = login["user"]["id"]
    check(sql(f"SELECT count(*) FROM accounts WHERE id={user_id} AND email='{email}'") == "1", "共用帳號未落庫")
    check(sql(f"SELECT password_hash ~ '^\\$argon2id\\$v=19\\$m=19456,t=2,p=1\\$' FROM accounts WHERE id={user_id}") == "t", "PHC 表示不符")
    expect(call(FASTIFY, "GET", "/api/me", token=token), 200)
    expect(call(FASTIFY, "GET", "/api/me", token="invalid"), 401, "E0002")
    expect(call(FASTIFY, "GET", "/api/me", body="{}", token=token), 422, "E0001", {"_body": ["此 API 不接受 Request Body"]})
    expect(call(FASTIFY, "POST", "/api/auth/login", "{"), 400, "E0005")
    expect(call(FASTIFY, "GET", "/api/articles/%ZZ"), 400, "E0005")
    expect(call(FASTIFY, "POST", "/api/auth/login", "{}", headers={"Content-Encoding": "not-supported"}), 415, "E0005")
    malformed = '{"email":"bad@example.test","password":"Abcd1234","nickname":"\\ud800"}'
    check(b"\\ud800" in malformed.encode(), "特別輸入沒有攜帶預期 escape 位元組")
    expect(call(FASTIFY, "POST", "/api/auth/register", malformed), 400, "E0005")
    expect(call(FASTIFY, "POST", "/api/auth/register", {"email": "bad@example.test", "password": "Abcd1234", "nickname": "a"}, headers={"Content-Type": "text/plain"}), 422, "E0001", {"_body": ["Request Body 必須為 JSON object"]})
    expect(call(FASTIFY, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": "另一位"}), 422, "E0001", {"email": ["此 Email 已註冊"]})
    duplicate = {"email": f"ticket37-{tag}same@example.test", "password": "Abcd1234", "nickname": "相同帳號"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call, FASTIFY, "POST", "/api/auth/register", duplicate) for _ in range(2)]
        registrations = [future.result() for future in futures]
    check(sorted(item[0] for item in registrations) == [201, 422], "並行註冊唯一性")
    # Laravel 必須可驗 Fastify 的 Argon2id；不同後端 JWT 則必須被拒絕。
    laravel_login = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    expect(call(FASTIFY, "GET", "/api/me", token=laravel_login["access_token"]), 401, "E0002")
    expect(call(LARAVEL, "GET", "/api/me", token=token), 401, "E0002")
    laravel_email = f"ticket37-{tag}laravel@example.test"
    expect(call(LARAVEL, "POST", "/api/auth/register", {"email": laravel_email, "password": "Abcd1234", "nickname": "Laravel 作者"}), 201)
    check(sql(f"SELECT password_hash ~ '^\\$argon2id\\$v=19\\$m=19456,t=2,p=1\\$' FROM accounts WHERE email='{laravel_email}'") == "t", "Laravel PHC 表示不符")
    fastify_from_laravel = expect(call(FASTIFY, "POST", "/api/auth/login", {"email": laravel_email, "password": "Abcd1234"}), 200)
    check(fastify_from_laravel["user"]["email"] == laravel_email, "Fastify 未驗證 Laravel 雜湊")
    # Laravel 以真實認證 API 建立公開文章與留言；API 故意回空 data，寫後以唯一標題/本文查 ID。
    title = f"Ticket37 Laravel 公開 {tag}"
    article_body = f"Laravel 文章本文 {tag}\n第二行"
    comment_body = f"Laravel 留言 {tag}\n第二行"
    expect(call(LARAVEL, "POST", "/api/articles", {"title": title, "body": article_body}, token=laravel_login["access_token"]), 201)
    visible = int(sql(f"SELECT id FROM articles WHERE author_id={user_id} AND title='{title}' AND created_backend='laravel'"))
    expect(call(LARAVEL, "POST", f"/api/articles/{visible}/comments", {"body": comment_body}, token=laravel_login["access_token"]), 201)
    comment_id = int(sql(f"SELECT id FROM comments WHERE article_id={visible} AND author_id={user_id} AND body='Laravel 留言 {tag}'||chr(10)||'第二行' AND created_backend='laravel'"))
    # 下架可見性測資也由 Laravel API 寫入，來源欄位反映真實建立者。
    hidden_title = f"Ticket37 Laravel 下架 {tag}"
    expect(call(LARAVEL, "POST", "/api/articles", {"title": hidden_title, "body": "private"}, token=laravel_login["access_token"]), 201)
    hidden = int(sql(f"SELECT id FROM articles WHERE author_id={user_id} AND title='{hidden_title}' AND created_backend='laravel'"))
    expect(call(LARAVEL, "PUT", f"/api/articles/{hidden}/visibility", {"visibility": "hidden", "version": 1}, token=laravel_login["access_token"]), 200)
    hidden_comment_body = f"Laravel 下架留言 {tag}"
    expect(call(LARAVEL, "POST", f"/api/articles/{visible}/comments", {"body": hidden_comment_body}, token=laravel_login["access_token"]), 201)
    hidden_comment = int(sql(f"SELECT id FROM comments WHERE article_id={visible} AND author_id={user_id} AND body='{hidden_comment_body}' AND created_backend='laravel'"))
    expect(call(LARAVEL, "PUT", f"/api/comments/{hidden_comment}/visibility", {"visibility": "hidden", "version": 1}, token=laravel_login["access_token"]), 200)
    public = expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=20"), 200)
    check(any(item["id"] == visible for item in public["list"]) and all(item["id"] != hidden for item in public["list"]), "公開文章可見性")
    check(public["pagination"]["total"] == int(sql("SELECT count(*) FROM articles WHERE visibility='visible'")), "公開文章精確總數")
    article_list_item = next(item for item in public["list"] if item["id"] == visible)
    check(article_list_item["title"] == title and article_list_item["body"] == article_body and article_list_item["author"] == {"id": user_id, "nickname": "測試作者"} and article_list_item["created_backend"] == "laravel", "Fastify 公開列表未讀取 Laravel 文章原文/來源")
    detail = expect(call(FASTIFY, "GET", f"/api/articles/{visible}"), 200)
    check(detail["id"] == visible and detail["title"] == title and detail["body"] == article_body and detail["author"] == article_list_item["author"] and detail["created_backend"] == "laravel", "Fastify 詳情未讀取 Laravel 文章原文/來源")
    beyond = expect(call(FASTIFY, "GET", "/api/articles?page=9007199254740991&per_page=100"), 200)
    check(beyond["list"] == [] and beyond["pagination"]["page"] == 9007199254740991 and beyond["pagination"]["total"] >= 1, "合法超頁回原頁與空列表")
    expect(call(FASTIFY, "GET", f"/api/articles/{hidden}"), 404, "E0004")
    own = expect(call(FASTIFY, "GET", f"/api/articles/{hidden}", token=token), 200)
    check(own["can_restore"] is True and own["author"] == {"id": user_id, "nickname": "測試作者"}, "作者預覽與恢復權")
    comments = expect(call(FASTIFY, "GET", f"/api/articles/{visible}/comments?page=1&per_page=20", token=token), 200)
    check(len([item for item in comments["list"] if item["article_id"] == visible]) == 1, "作者不應看到個別隱藏留言")
    check(comments["pagination"] == {"page": 1, "per_page": 20, "total": 1, "total_pages": 1}, "公開留言精確分頁")
    check(expect(call(FASTIFY, "GET", f"/api/articles/{visible}/comments?page=2&per_page=1"), 200)["list"] == [], "超頁留言應為空")
    check(comments["list"][0]["id"] == comment_id and comments["list"][0]["body"] == comment_body and comments["list"][0]["author"] == {"id": user_id, "nickname": "測試作者"} and comments["list"][0]["created_backend"] == "laravel", "Fastify 留言列表未讀取 Laravel 原文/來源")
    expect(call(FASTIFY, "GET", f"/api/articles/{hidden}/comments?page=1&per_page=20"), 404, "E0004")
    admin_email, admin_login = register(tag + "admin")
    sql(f"UPDATE accounts SET role='admin' WHERE email='{admin_email}'")
    laravel_admin = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": admin_email, "password": "Abcd1234"}), 200)
    settings_token = laravel_admin["access_token"]
    management = expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management", token=admin_login["access_token"]), 200)
    check(any(item["id"] == hidden and item["can_restore"] is True for item in management["list"]), "管理員可讀隱藏文章")
    check(management["pagination"]["total"] == int(sql("SELECT count(*) FROM articles")), "管理文章精確總數")
    admin_comments = expect(call(FASTIFY, "GET", f"/api/articles/{visible}/comments?page=1&per_page=20", token=admin_login["access_token"]), 200)
    check(len([item for item in admin_comments["list"] if item["article_id"] == visible]) == 2, "管理員可讀全部留言")
    check(admin_comments["pagination"] == {"page": 1, "per_page": 20, "total": 2, "total_pages": 1}, "管理留言精確分頁")
    print("LARAVEL_INTEROP_ARTICLE_ID:", visible)
    expect(call(FASTIFY, "GET", "/api/articles?page=bad&page=1&per_page=20"), 422, "E0001", {"page": ["此參數不可重複"]})
    expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D%5Bx%5D=management"), 422, "E0001", {"filters.scope": ["欄位型態不正確"]})
    for value in ("+1", "%201", "1e0", "-1", "1.0"):
        expect(call(FASTIFY, "GET", f"/api/articles?page={value}&per_page=20"), 422, "E0001", {"page": ["欄位格式或範圍不正確"]})
    expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=20", token="bad"), 401, "E0002")
    expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management", token=token), 403, "E0003")
    # 固定微秒測資在資料庫端驗證開閉邊界；鎖定測試的共用設定於例外時也恢復。
    check(sql("SELECT ('2026-01-01 00:00:00.000500+00'::timestamptz > '2026-01-01 00:00:00.000400+00'::timestamptz)::text || '|' || ('2026-01-01 00:00:00.000400+00'::timestamptz > '2026-01-01 00:00:00.000400+00'::timestamptz)::text") == "true|false", "微秒鎖定邊界")
    with temporary_lock_settings(settings_token, 2, 30):
        boundary_login()
        lock_email, lock_login = register(tag + "lock")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(call, FASTIFY, "POST", "/api/auth/login", {"email": lock_email, "password": "Wrong1234"}) for _ in range(2)]
            attempts = [future.result() for future in futures]
        check(sorted(item[0] for item in attempts) == [401, 429], f"並行鎖定結果：{attempts}")
        for result in attempts:
            expect(result, result[0], "E0101" if result[0] == 401 else "E0105")
        retry = next(item[2].get("Retry-After") for item in attempts if item[0] == 429)
        check(retry is not None and retry.isdecimal() and 1 <= int(retry) <= 30, "Retry-After 剩餘秒數")
        repeated = call(FASTIFY, "POST", "/api/auth/login", {"email": lock_email, "password": "Abcd1234"}, headers={"Origin": "http://localhost:18371"})
        expect(repeated, 429, "E0105")
        check(int(repeated[2]["Retry-After"]) <= int(retry) and repeated[2]["Access-Control-Expose-Headers"] == "Retry-After", "重試未延長鎖與 CORS")
        preflight = call(FASTIFY, "OPTIONS", "/api/auth/login", headers={"Origin": "http://localhost:18371", "Access-Control-Request-Method": "POST"})
        check(preflight[0] == 204 and preflight[2]["Access-Control-Allow-Origin"] == "http://localhost:18371" and "Authorization" in preflight[2]["Access-Control-Allow-Headers"], "CORS 預檢")
        rejected_origin = call(FASTIFY, "OPTIONS", "/api/auth/login", headers={"Origin": "http://untrusted.example", "Access-Control-Request-Method": "POST"})
        check(rejected_origin[0] == 204 and rejected_origin[2].get("Access-Control-Allow-Origin") is None, "未核准 Origin 不可取得 CORS")
        before_disable = sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{lock_email}'")
        check(before_disable == "2", "失敗事件數")
        sql(f"UPDATE accounts SET status='disabled' WHERE email='{lock_email}'")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": lock_email, "password": "Abcd1234"}), 403, "E0102")
        expect(call(FASTIFY, "GET", "/api/me", token=lock_login["access_token"]), 403, "E0102")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{lock_email}'") == before_disable, "停用帳號不得計數")
        cross_email, _ = register(tag + "cross")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cross_attempt, barrier, LARAVEL, cross_email)
            second = executor.submit(cross_attempt, barrier, FASTIFY, cross_email)
            cross_results = [first.result(), second.result()]
        check(sorted(result[0] for result in cross_results) == [401, 429], f"跨後端並行鎖定結果：{cross_results}")
        for result in cross_results:
            expect(result, result[0], "E0101" if result[0] == 401 else "E0105")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{cross_email}' AND f.failure_epoch=a.failure_epoch") == "2", "跨後端並行失敗事件漏計")
        reset_email, _ = register(tag + "reset")
        prior_epoch = sql(f"SELECT failure_epoch FROM accounts WHERE email='{reset_email}'")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": reset_email, "password": "Wrong1234"}), 401, "E0101")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": reset_email, "password": "Abcd1234"}), 200)
        check(sql(f"SELECT failure_epoch FROM accounts WHERE email='{reset_email}'") == str(int(prior_epoch) + 1), "成功登入未重設世代")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": reset_email, "password": "Wrong1234"}), 401, "E0101")
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE email='{reset_email}'") == "t", "成功登入後仍鎖定")
        window_email, _ = register(tag + "window")
        sql(f"INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '6 minutes' FROM accounts WHERE email='{window_email}'")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": window_email, "password": "Wrong1234"}), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{window_email}'") == "2", "舊失敗事件應保留供稽核")
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE email='{window_email}'") == "t", "視窗外事件不得觸發鎖定")
        # 與視窗外帳號分開：一分鐘前的事件須和本次錯誤合計，立即達門檻。
        recent_email, _ = register(tag + "recent")
        sql(f"INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '1 minute' FROM accounts WHERE email='{recent_email}'")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": recent_email, "password": "Wrong1234"}), 429, "E0105")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{recent_email}' AND f.failure_epoch=a.failure_epoch") == "2", "視窗內事件未計入")
        # 真實 HTTP 先觀察未到期 429；以 DB 時鐘輪詢到期，再驗到期換世代與成功登入。
        expiry_email, _ = register(tag + "expiry")
        expiry_epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE email='{expiry_email}'"))
        until = sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '4 seconds' WHERE email='{expiry_email}' RETURNING locked_until::text")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": expiry_email, "password": "Abcd1234"}), 429, "E0105")
        check(sql(f"SELECT locked_until::text FROM accounts WHERE email='{expiry_email}'") == until, "鎖定中重試不得延長期限")
        deadline = time.monotonic() + 10
        while sql(f"SELECT clock_timestamp()>=locked_until FROM accounts WHERE email='{expiry_email}'") != "t":
            check(time.monotonic() < deadline, "隔離資料庫鎖定期限未到")
            time.sleep(0.1)
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": expiry_email, "password": "Wrong1234"}), 401, "E0101")
        check(sql(f"SELECT failure_epoch::text || '|' || (locked_until IS NULL)::text FROM accounts WHERE email='{expiry_email}'") == f"{expiry_epoch + 1}|true", "到期後未清鎖換世代")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": expiry_email, "password": "Abcd1234"}), 200)
        check(sql(f"SELECT failure_epoch::text || '|' || (locked_until IS NULL)::text FROM accounts WHERE email='{expiry_email}'") == f"{expiry_epoch + 2}|true", "到期後成功登入未再重設世代")
        # 調大 N 後，同世代七分鐘前的事件應重新納入；本次錯誤立刻達兩次門檻。
        expanded_email, _ = register(tag + "expanded")
        sql(f"INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '7 minutes' FROM accounts WHERE email='{expanded_email}'")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{expanded_email}' AND f.failure_epoch=a.failure_epoch AND f.occurred_at>clock_timestamp()-interval '5 minutes'") == "0", "擴張前舊事件應在視窗外")
        update_lock_settings(settings_token, window_minutes=10)
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": expanded_email, "password": "Wrong1234"}), 429, "E0105")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{expanded_email}' AND f.failure_epoch=a.failure_epoch") == "2", "擴張後舊事件未計入")
        update_lock_settings(settings_token, window_minutes=5)
        corrupt_email, _ = register(tag + "corrupt")
        sql(f"UPDATE accounts SET password_hash='bad-phc' WHERE email='{corrupt_email}'")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": corrupt_email, "password": "Abcd1234"}), 500, "E0104")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{corrupt_email}'") == "0", "雜湊毀損不得計數")
        # 管理 API 關閉時應原子清鎖換世代；關閉期間不累積，重新啟用從零計。
        toggle_email, _ = register(tag + "toggle")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": toggle_email, "password": "Wrong1234"}), 401, "E0101")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": toggle_email, "password": "Wrong1234"}), 429, "E0105")
        before_epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE email='{toggle_email}'"))
        before_events = sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{toggle_email}'")
        update_lock_settings(settings_token, enabled=False)
        check(sql(f"SELECT failure_epoch::text || '|' || (locked_until IS NULL)::text FROM accounts WHERE email='{toggle_email}'") == f"{before_epoch + 1}|true", "關閉設定未清鎖換世代")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": toggle_email, "password": "Wrong1234"}), 401, "E0101")
        check(sql(f"SELECT count(*) FROM login_failures f JOIN accounts a ON a.id=f.account_id WHERE a.email='{toggle_email}'") == before_events, "關閉期間仍新增失敗事件")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": toggle_email, "password": "Abcd1234"}), 200)
        update_lock_settings(settings_token, enabled=True)
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": toggle_email, "password": "Wrong1234"}), 401, "E0101")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": toggle_email, "password": "Wrong1234"}), 429, "E0105")
    # 至少保留一份實際執行計畫，供 Main 核對掃描、排序與成本。
    plan = sql("EXPLAIN (ANALYZE, BUFFERS) SELECT articles.id FROM articles WHERE visibility='visible' ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET 0")
    check("Execution Time" in plan and "Buffers" in plan, "SQL 查詢計畫缺漏")
    print("PLAN articles-public:", plan.replace("\n", " | "))
    expect(call(FASTIFY, "POST", "/api/auth/logout", {}, token=token), 200)
    expect(call(FASTIFY, "GET", "/api/me", token=token), 401, "E0002")
    expect(call(FASTIFY, "GET", "/api/me", token=laravel_login["access_token"]), 401, "E0002")
    print("PASS ticket37 HTTP/SQL isolated fixture")


if __name__ == "__main__":
    main()
