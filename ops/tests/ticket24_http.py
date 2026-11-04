"""Ticket 24：隔離資料庫上的 FastAPI 管理 API 與 Laravel 互通驗證。"""

from contextlib import contextmanager

import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from ticket22_http import (FASTAPI, LARAVEL, OPENER, PASSWORD, call, login,
                           member, owner_connection)


MESSAGES = {
    "E0001": "欄位資料不正確，請確認後再送出",
    "E0002": "登入已逾期，請重新登入",
    "E0003": "沒有權限使用此功能",
    "E0004": "找不到指定資料或 API",
    "E0102": "此帳號已停用，請聯絡系統管理員",
}


# 失敗即停止後續寫入，保留第一個違反契約的情境。
def require(condition, reason):
    if not condition:
        raise AssertionError("Ticket 24 驗證失敗：" + reason)


# 寫入前同時驗 Compose 身分、隔離 fixture 標記、資料庫角色與固定服務。
def guard():
    require(os.environ.get("COMPOSE_PROJECT_NAME") == "ticket24_test", "錯誤 Compose 專案")
    require(os.environ.get("DB_HOST") == "db" and os.environ.get("DB_DATABASE") == "articles"
            and os.environ.get("DB_OWNER_USER") == "articles_owner"
            and os.environ.get("TICKET24_FIXTURE") == "isolated-ticket24", "隔離環境設定不符")
    require(bool(os.environ.get("DB_OWNER_PASSWORD")) and bool(os.environ.get("DB_APP_PASSWORD")),
            "隔離資料庫憑證缺失")
    with owner_connection() as connection:
        identity = connection.execute("SELECT current_database() AS db, current_user AS role").fetchone()
        require(identity == {"db": "articles", "role": "articles_owner"}, "資料庫身分不符")
        tables = connection.execute(
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_name IN ('accounts','login_sessions','login_failures',"
            "'login_lockout_settings','articles','comments')"
        ).fetchone()["n"]
        require(tables == 6, "共同結構不完整")
    for base in (FASTAPI, LARAVEL):
        try:
            with OPENER.open(base + "/up", timeout=5) as response:
                require(response.status == 200 and response.read() == b"OK", "健康端點不符")
        except (HTTPError, URLError, TimeoutError):
            raise AssertionError("Ticket 24 驗證失敗：隔離 API 不可用") from None


# 對共同 envelope 和逐字文案驗證，避免只檢查 HTTP 狀態。
def check(result, status, code=None, errors=None):
    actual, payload, _ = result
    require(actual == status, f"HTTP {actual}，預期 {status}/{code}")
    if code is None:
        require(set(payload) == {"success", "message", "data"} and payload["success"] is True
                and payload["message"] == "OK" and isinstance(payload["data"], dict), "成功 envelope")
        return payload["data"]
    expected = {"success", "message", "error_code", "log_id"}
    if errors is not None:
        expected.add("errors")
    require(set(payload) == expected and payload["success"] is False
            and payload["error_code"] == code and payload["message"] == MESSAGES[code]
            and isinstance(payload["log_id"], str) and bool(payload["log_id"]),
            f"錯誤 envelope 或文案：預期 {code}，實際 {payload.get('error_code')} / {payload.get('message')}")
    if errors is not None:
        require(payload["errors"] == errors, "欄位 errors")
    return payload


# 公開註冊後僅在隔離資料庫設置測試管理員，避免透過 API 擴權。
def admin(namespace):
    account = member(FASTAPI, namespace, "admin")
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET role = 'admin' WHERE id = %s", (account["id"],))
    account["token"] = login(FASTAPI, account)
    return account


# 計算帳號已持久保存的登入狀態，核對同值與停用副作用。
def session_state(account_id):
    with owner_connection() as connection:
        return connection.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE revoked_at IS NULL) AS live "
            "FROM login_sessions WHERE account_id = %s", (account_id,),
        ).fetchone()


# 搜尋由資料庫完成，並逐筆比對完整集合、總數、排序與深頁。
def search_cases(manager, namespace):
    people = [member(FASTAPI, namespace, f"find{i}") for i in range(4)]
    marker = "T24" + namespace
    # 選目前尚未出現在暱稱的漢字，重跑時一、二字搜尋仍有精確集合。
    with owner_connection() as connection:
        used = {character for row in connection.execute("SELECT nickname FROM accounts").fetchall()
                for character in row["nickname"]}
    ideograph = next((chr(code) for code in range(0x20000, 0x2A6E0) if chr(code) not in used), None)
    require(ideograph is not None, "隔離測資已用盡一字搜尋字元")
    names = (marker + "Alpha%_\\" + ideograph + "文", marker + "alpha" + ideograph + "中文",
             marker + "É", marker + "é")
    with owner_connection() as connection:
        for person, nickname in zip(people, names):
            connection.execute("UPDATE accounts SET nickname = %s WHERE id = %s", (nickname, person["id"]))
        connection.execute("UPDATE accounts SET nickname = %s WHERE id = %s", (marker + "Admin", manager["id"]))
    path = "/api/members?page=1&per_page=20&filters%5Bkeyword%5D="
    full = check(call(FASTAPI, "GET", path + marker, token=manager["token"]), 200)
    require({row["id"] for row in full["list"]} == {person["id"] for person in people}
            and full["pagination"]["total"] == 4, "會員集合、管理員排除或精確計數")
    require(full == check(call(LARAVEL, "GET", path + marker, token=login(LARAVEL, manager)), 200),
            "同資料會員列表跨後端不一致")
    for suffix, expected in ((marker + "alpha%25_%5C", {people[0]["id"]}),
                             (marker + "%C3%89", {people[2]["id"]}),
                             (marker + "%C3%A9", {people[3]["id"]}),
                             (quote(ideograph), {people[0]["id"], people[1]["id"]}),
                             (quote(ideograph + "中"), {people[1]["id"]}),
                             (quote(marker + "alpha" + ideograph + "中文"), {people[1]["id"]}),
                             ("%00", set())):
        result = check(call(FASTAPI, "GET", path + suffix, token=manager["token"]), 200)
        require({row["id"] for row in result["list"]} == expected
                and result["pagination"]["total"] == len(expected), f"搜尋字面與 Unicode：{suffix}")
    # 空字串等同未篩選；依隔離庫現況比對完整總數與第一頁順序，支援重跑。
    with owner_connection() as connection:
        all_ids = [row["id"] for row in connection.execute(
            "SELECT id FROM accounts WHERE role = 'member' ORDER BY created_at DESC, id DESC"
        ).fetchall()]
    empty = check(call(FASTAPI, "GET", path, token=manager["token"]), 200)
    require([row["id"] for row in empty["list"]] == all_ids[:20]
            and empty["pagination"]["total"] == len(all_ids), "空搜尋條件與精確總數")
    no_match = check(call(FASTAPI, "GET", path + quote(marker + "無符合"), token=manager["token"]), 200)
    require(no_match["list"] == [] and no_match["pagination"]["total"] == 0, "一般無結果")
    deep = check(call(FASTAPI, "GET", "/api/members?page=999&per_page=1&filters%5Bkeyword%5D=" + marker,
                      token=manager["token"]), 200)
    require(deep["list"] == [] and deep["pagination"] ==
            {"page": 999, "per_page": 1, "total": 4, "total_pages": 4}, "深頁及總數")
    for person in people:
        detail = check(call(FASTAPI, "GET", f"/api/members/{person['id']}", token=manager["token"]), 200)
        require(set(detail) == {"id", "email", "nickname", "role", "status", "created_at"}, "會員資源欄位")
    check(call(FASTAPI, "GET", f"/api/members/{manager['id']}", token=manager["token"]), 404, "E0004")
    # 使用與 API 相同的 count/list SQL，保留實際掃描、排序與 BUFFERS 證據。
    with owner_connection() as connection:
        connection.execute("ANALYZE accounts")
        folded = marker.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        predicate = (" FROM accounts WHERE role = 'member' AND "
                     "(strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0 "
                     "OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)")
        for sql, params in (("SELECT count(*)" + predicate, (folded, folded)),
                            ("SELECT id, email, nickname, role, status, created_at" + predicate
                             + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", (folded, folded, 20, 0))):
            plan = connection.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params).fetchall()
            print("PLAN " + ("count" if "count(*)" in sql else "list") + ": " +
                  " | ".join(row["QUERY PLAN"] for row in plan))
    return people


# 權限優先於格式，且欄位型態、範圍、重複及未知欄位逐字相符。
def rejection_cases(manager, ordinary):
    token = ordinary["token"]
    for method, path, body in (("GET", "/api/members?page=x&per_page=1", None),
                               ("GET", "/api/members/+1", None),
                               ("PUT", "/api/members/+1/status", {"status": "disabled"}),
                               ("GET", "/api/comments?page=x&per_page=1", None),
                               ("GET", "/api/settings/login-lockout?x=1", None),
                               ("PUT", "/api/settings/login-lockout", {"enabled": "true"})):
        check(call(FASTAPI, method, path, body, token), 403, "E0003")
    check(call(FASTAPI, "GET", "/api/members?page=1&per_page=20&filters%5Bkeyword%5D=" + "x" * 255,
               token=manager["token"]), 422, "E0001", {"filters.keyword": ["欄位格式或範圍不正確"]})
    check(call(FASTAPI, "GET", "/api/members?page=1&page=2&per_page=20", token=manager["token"]),
          422, "E0001", {"page": ["此參數不可重複"]})
    check(call(FASTAPI, "GET", "/api/members?page=1&per_page=20&filters%5Bkeyword%5D%5B%5D=x",
               token=manager["token"]), 422, "E0001", {"filters.keyword": ["欄位型態不正確"]})
    check(call(FASTAPI, "GET", "/api/comments?page=1&per_page=20&x=1", token=manager["token"]),
          422, "E0001", {"x": ["不支援此欄位"]})
    check(call(FASTAPI, "GET", "/api/settings/login-lockout", b"{}", manager["token"]),
          422, "E0001", {"_body": ["此 API 不接受 Request Body"]})
    # 同一請求的 status 格式與未知欄位須一起回報，且與 Laravel 逐字一致。
    endpoint = f"/api/members/{ordinary['id']}/status"
    expected = {"status": ["欄位格式或範圍不正確"], "extra": ["不支援此欄位"]}
    for base, manager_token in ((FASTAPI, manager["token"]), (LARAVEL, login(LARAVEL, manager))):
        check(call(base, "PUT", endpoint, {"status": "invalid", "extra": 1}, manager_token),
              422, "E0001", expected)


# 會員停用立即使多次登入失效，恢復僅允許新登入且不改既有內容。
def status_cases(manager, ordinary, article_id):
    second = login(FASTAPI, ordinary)
    before = session_state(ordinary["id"])
    endpoint = f"/api/members/{ordinary['id']}/status"
    check(call(FASTAPI, "PUT", endpoint, {"status": "active"}, manager["token"]), 200)
    require(session_state(ordinary["id"]) == before, "同值狀態不應撤銷")
    check(call(FASTAPI, "PUT", endpoint, {"status": "disabled"}, manager["token"]), 200)
    require(session_state(ordinary["id"])["live"] == 0, "停用未撤銷全部登入")
    for token in (ordinary["token"], second):
        check(call(FASTAPI, "GET", "/api/me", token=token), 403, "E0102")
        check(call(FASTAPI, "PUT", f"/api/articles/{article_id}",
                   {"title": "不得寫入", "body": "正文", "version": 1}, token), 403, "E0102")
    check(call(FASTAPI, "PUT", endpoint, {"status": "active"}, manager["token"]), 200)
    check(call(FASTAPI, "GET", "/api/me", token=ordinary["token"]), 401, "E0002")
    with owner_connection() as connection:
        row = connection.execute("SELECT title FROM articles WHERE id = %s", (article_id,)).fetchone()
    require(row["title"] != "不得寫入", "停用後內容被改變")
    ordinary["token"] = login(FASTAPI, ordinary)
    # Laravel 停用同一帳號，也必須使 FastAPI 舊登入立即失效。
    laravel_token = login(LARAVEL, manager)
    check(call(LARAVEL, "PUT", endpoint, {"status": "disabled"}, laravel_token), 200)
    check(call(FASTAPI, "GET", "/api/me", token=ordinary["token"]), 403, "E0102")
    check(call(LARAVEL, "PUT", endpoint, {"status": "active"}, laravel_token), 200)
    check(call(FASTAPI, "GET", "/api/me", token=ordinary["token"]), 401, "E0002")
    ordinary["token"] = login(FASTAPI, ordinary)


# settings_restore 保留呼叫者既有 snapshot 時點，僅共用完整四欄的 finally／精確讀回。
@contextmanager
def settings_restore(manager, *, execute=None, original=None):
    if execute is None:
        # 原 caller 仍用固定 FastAPI；明確 executor 僅供已核對的隔離 probe。
        def execute(method, values=None):
            return check(call(FASTAPI, method, "/api/settings/login-lockout", values, manager["token"]), 200)
    if original is None:
        original = execute("GET")
    require(set(original) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "設定欄位")
    try:
        yield original
    finally:
        # 不宣稱恢復會員世代／鎖定；原錯與恢復錯保持可追溯。
        execute("PUT", original)
        require(execute("GET") == original, "原設定未完整還原")


# 設定四欄完整提交；關閉才清鎖與世代，數值變更保留期限。
def settings_cases(manager, ordinary):
    endpoint = "/api/settings/login-lockout"
    original = check(call(FASTAPI, "GET", endpoint, token=manager["token"]), 200)
    require(set(original) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "設定欄位")
    for body, errors in ((original | {"enabled": 1}, {"enabled": ["欄位型態不正確"]}),
                         (original | {"window_minutes": True}, {"window_minutes": ["欄位型態不正確"]}),
                         (original | {"lock_seconds": 0}, {"lock_seconds": ["欄位格式或範圍不正確"]}),
                         ({"enabled": True}, {"window_minutes": ["此欄位為必填"],
                                              "failure_threshold": ["此欄位為必填"],
                                              "lock_seconds": ["此欄位為必填"]})):
        check(call(FASTAPI, "PUT", endpoint, body, manager["token"]), 422, "E0001", errors)
    # PostgreSQL bigint 上限之外的 JSON 整數也須由兩後端回共同欄位錯誤。
    overflow = original | {"window_minutes": 9223372036854775808}
    for base, manager_token in ((FASTAPI, manager["token"]), (LARAVEL, login(LARAVEL, manager))):
        check(call(base, "PUT", endpoint, overflow, manager_token), 422, "E0001",
              {"window_minutes": ["欄位格式或範圍不正確"]})
    with owner_connection() as connection:
        connection.execute("UPDATE accounts SET locked_until = clock_timestamp() + interval '1 hour' "
                           "WHERE id = %s", (ordinary["id"],))
        before = connection.execute("SELECT locked_until, failure_epoch FROM accounts WHERE id = %s",
                                    (ordinary["id"],)).fetchone()
    with settings_restore(manager, original=original):
        changed = original | {"enabled": True, "window_minutes": 6, "failure_threshold": 7, "lock_seconds": 60}
        check(call(FASTAPI, "PUT", endpoint, changed, manager["token"]), 200)
        require(check(call(LARAVEL, "GET", endpoint, token=login(LARAVEL, manager)), 200) == changed,
                "Laravel 未讀到完整設定")
        with owner_connection() as connection:
            same = connection.execute("SELECT locked_until, failure_epoch FROM accounts WHERE id = %s",
                                      (ordinary["id"],)).fetchone()
        require(same == before, "數值變更改動既有鎖定")
        check(call(FASTAPI, "PUT", endpoint, changed | {"enabled": False}, manager["token"]), 200)
        with owner_connection() as connection:
            cleared = connection.execute("SELECT locked_until, failure_epoch FROM accounts WHERE id = %s",
                                         (ordinary["id"],)).fetchone()
        require(cleared["locked_until"] is None and cleared["failure_epoch"] == before["failure_epoch"] + 1,
                "關閉未原子清鎖換世代")
        check(call(FASTAPI, "PUT", endpoint, changed | {"enabled": False}, manager["token"]), 200)
        with owner_connection() as connection:
            repeated = connection.execute("SELECT failure_epoch FROM accounts WHERE id = %s",
                                          (ordinary["id"],)).fetchone()
        require(repeated["failure_epoch"] == cleared["failure_epoch"], "同值關閉重做副作用")


# 兩後端在設定列上受控重疊，最後值須是其中一個請求的完整四欄。
def settings_race(manager):
    endpoint = "/api/settings/login-lockout"
    with settings_restore(manager) as original:
        first = original | {"window_minutes": 8, "failure_threshold": 11, "lock_seconds": 70}
        second = original | {"window_minutes": 9, "failure_threshold": 12, "lock_seconds": 80}
        laravel_token = login(LARAVEL, manager)
        with owner_connection() as connection, ThreadPoolExecutor(max_workers=2) as pool:
            connection.execute("SELECT id FROM login_lockout_settings WHERE id = 1 FOR UPDATE")
            pending = [pool.submit(call, FASTAPI, "PUT", endpoint, first, manager["token"]),
                       pool.submit(call, LARAVEL, "PUT", endpoint, second, laravel_token)]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with owner_connection() as observer:
                    waiting = observer.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = 'articles_app' "
                        "AND wait_event_type = 'Lock' AND query ILIKE '%login_lockout_settings%'"
                    ).fetchone()["n"]
                if waiting >= 2:
                    break
                time.sleep(0.05)
            if waiting < 2:
                connection.rollback()
                raise AssertionError("Ticket 24 驗證失敗：未觀察到跨後端設定列鎖重疊")
            connection.commit()
            for request in pending:
                check(request.result(), 200)
        final = check(call(FASTAPI, "GET", endpoint, token=manager["token"]), 200)
        require(final in (first, second), "跨後端並行設定混用部分欄位")


# 管理列表包含父文章與個別下架留言，完整資源與排序不逐筆查詢。
def comment_cases(manager, ordinary, article_id):
    with owner_connection() as connection:
        comment_id = connection.execute(
            "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, "
            "created_backend, created_at, updated_at) VALUES (%s, %s, '管理留言', 'hidden', 'author', "
            "'fastapi', clock_timestamp(), clock_timestamp()) RETURNING id",
            (article_id, ordinary["id"]),
        ).fetchone()["id"]
        connection.execute("UPDATE articles SET visibility = 'hidden', restore_control = 'author' WHERE id = %s",
                           (article_id,))
    result = check(call(FASTAPI, "GET", "/api/comments?page=1&per_page=100", token=manager["token"]), 200)
    item = next(row for row in result["list"] if row["id"] == comment_id)
    require(set(item) == {"id", "body", "author", "article_id", "version", "created_backend",
                          "created_at", "updated_at", "visibility", "can_restore"}
            and item["visibility"] == "hidden" and item["can_restore"] is True
            and item["article_id"] == article_id, "管理留言完整資源或下架範圍")
    ids = [row["id"] for row in result["list"]]
    require(ids[0] == comment_id and result["pagination"]["total"] >= len(ids), "排序或精確總數")
    article_path = "/api/articles?page=1&per_page=100&filters%5Bscope%5D=management"
    managed = check(call(FASTAPI, "GET", article_path, token=manager["token"]), 200)
    require(article_id in {row["id"] for row in managed["list"]}, "API05 管理範圍漏掉下架文章")
    check(call(FASTAPI, "GET", article_path, token=ordinary["token"]), 403, "E0003")


# 每次建立新的隔離帳號；輸出可供後續 Vue 驗收的測試帳號名稱。
def main():
    guard()
    namespace = secrets.token_hex(4)
    manager = admin(namespace)
    ordinary = member(FASTAPI, namespace, "ordinary")
    ordinary["token"] = login(FASTAPI, ordinary)
    with owner_connection() as connection:
        article_id = connection.execute(
            "INSERT INTO articles (author_id,title,body,visibility,created_backend,created_at,updated_at) "
            "VALUES (%s,'Ticket24 原題','正文','visible','fastapi',clock_timestamp(),clock_timestamp()) RETURNING id",
            (ordinary["id"],),
        ).fetchone()["id"]
    rejection_cases(manager, ordinary)
    search_cases(manager, namespace)
    status_cases(manager, ordinary, article_id)
    settings_cases(manager, ordinary)
    settings_race(manager)
    comment_cases(manager, ordinary, article_id)
    print("PASS Ticket 24 isolated HTTP; admin=" + manager["email"] +
          " member=" + ordinary["email"] + " password=" + PASSWORD)


# report_failure 延續既有 AssertionError 診斷；finally 失敗後的前序工具例外只列型別。
def report_failure(error):
    print(str(error) if isinstance(error, AssertionError) else
          "Ticket 24 驗證失敗：" + type(error).__name__, file=sys.stderr)
    # 原錯誤維持在 cause/context；只走訪一次，避免循環或重複診斷。
    previous, seen = error.__cause__ or error.__context__, {id(error)}
    while previous is not None and id(previous) not in seen:
        print("Ticket 24 前序失敗類型：" + type(previous).__name__, file=sys.stderr)
        seen.add(id(previous))
        previous = previous.__cause__ or previous.__context__


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        report_failure(error)
        sys.exit(1)
