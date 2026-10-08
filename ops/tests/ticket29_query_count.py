"""只在 ticket28_test 量測 Echo 個人列表單次 HTTP 請求的實際 PostgreSQL SQL。"""

import json
import re
import secrets
import time

from ticket28_http import ECHO, call, check, expect, sql
from ticket28_query_count import compose, db_logs, ready, statements
from ticket29_http import create_user, guard, one


# ready_guard 等待 Compose 的健康狀態與 HTTP 就緒一致，再重新核對完整隔離身分。
def ready_guard():
    ready()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = [json.loads(line) for line in compose("--profile", "echo", "ps", "--format", "json", "echo").splitlines()]
        if len(rows) == 1 and rows[0].get("Health") == "healthy":
            guard()
            return
        time.sleep(0.25)
    raise AssertionError("Echo 重啟後未達 Docker healthy 狀態")


# probe 以單次 HTTP 請求前後的 DB 日誌核對認證、精確計數與列表查詢。
def probe(kind, actor, expected_id):
    before = db_logs()
    data = expect(call(ECHO, "GET", f"/api/me/{kind}?page=1&per_page=20",
                       token=actor["token"]), 200)
    after = db_logs()
    check([item["id"] for item in data["list"]] == [expected_id]
          and data["pagination"]["total"] == 1, "個人列表 fixture 或總數不符：" + kind)
    emitted = statements(before, after)
    table = kind
    expected = (
        "SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1",
        "SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1",
        f"SELECT count(*) FROM {table} JOIN accounts ON accounts.id={table}.author_id WHERE {table}.author_id=$1",
        f"SELECT {table}.id,{table}.author_id",
    )
    check(len(emitted) == len(expected),
          f"{kind} 單次 HTTP 實際 SQL 數={len(emitted)}，預期 {len(expected)}: {emitted}")
    check(all(fragment in re.sub(r"\s+", " ", line) for fragment, line in zip(expected, emitted)),
          f"{kind} SQL 類型或順序不符: {emitted}")
    check(len({re.search(r"\[(\d+)\]", line).group(1) for line in emitted}) == 1,
          f"{kind} SQL 並非同一 DB 連線: {emitted}")

    # PostgreSQL 參數日誌須顯示作者 ID 與分頁值，證明清單 SQL 是本次 fixture 的執行。
    lines = after[len(before):].splitlines()
    for fragment in expected[2:]:
        index = next(i for i, line in enumerate(lines) if " LOG:  execute " in line and fragment in re.sub(r"\s+", " ", line))
        detail = lines[index + 1] if index + 1 < len(lines) else ""
        check(" DETAIL:  Parameters: " in detail and f"$1 = '{actor['id']}'" in detail,
              f"{kind} SQL 作者參數不符: {detail}")
        if fragment.startswith(f"SELECT {table}.id"):
            check("$2 = '20'" in detail and "$3 = '0'" in detail,
                  f"{kind} SQL 分頁參數不符: {detail}")
        print(lines[index])
        print(detail)
    print(f"PASS {kind} authenticated SQL count={len(emitted)} (auth=2, owned count/list=2)")


# main 先守衛並建立唯一測資，暫開隔離角色日誌後一定清除設定與舊連線。
def main():
    guard()
    check(sql("SELECT coalesce(array_to_string(rolconfig,','),'') FROM pg_roles WHERE rolname='articles_app'") == "",
          "應用角色已有設定，拒絕覆寫")
    nonce = secrets.token_hex(6)
    actor = create_user(nonce + "-query")
    title = "個人 SQL " + nonce
    body = "個人 SQL 留言 " + nonce
    expect(call(ECHO, "POST", "/api/articles", {"title": title, "body": "原文"}, actor["token"]), 201)
    article = one("articles", actor["id"], "title", title)
    expect(call(ECHO, "POST", f"/api/articles/{article}/comments", {"body": body}, actor["token"]), 201)
    comment = one("comments", actor["id"], "body", body)
    check(sql(f"SELECT count(*) FROM articles WHERE author_id={int(actor['id'])}") == "1"
          and sql(f"SELECT count(*) FROM comments WHERE author_id={int(actor['id'])}") == "1",
          "個人 SQL 測資不唯一")
    print(f"FIXTURE user={actor['id']} article={article} comment={comment}")

    # 只有隔離應用角色會暫時開啟 SQL 日誌；finally 同時復原角色與 Echo 連線。
    try:
        sql("ALTER ROLE articles_app SET log_statement='all'")
        compose("restart", "echo")
        ready_guard()
        probe("articles", actor, article)
        probe("comments", actor, comment)
    finally:
        sql("ALTER ROLE articles_app RESET log_statement")
        compose("restart", "echo")
        ready_guard()
        check(sql("SELECT coalesce(array_to_string(rolconfig,','),'') FROM pg_roles WHERE rolname='articles_app'") == "",
              "應用角色日誌設定未恢復")
        print("PASS ticket29 SQL logging restored")


if __name__ == "__main__":
    main()
