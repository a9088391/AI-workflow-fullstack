"""在 Ticket 35 隔離 PostgreSQL 量測 Express 作者列表與留言查詢的實際 SQL。"""

import re
import secrets
import subprocess

from ticket35_http import EXPRESS, call, check, create_user, expect, guard, one, sql


# db_logs 唯讀取用隔離 DB 的 statement 日誌，便於鎖定單次 HTTP 範圍。
def db_logs():
    return subprocess.check_output(["docker", "compose", "logs", "--no-color", "--no-log-prefix", "db"], text=True)


# statements 僅擷取本次新增的應用角色 SQL，不混入 owner 的計畫與 fixture 查詢。
def statements(before, after):
    check(after.startswith(before), "DB 日誌順序改變")
    found = []
    active = False
    for line in after[len(before):].splitlines():
        if "articles_app@articles LOG:" in line:
            active = " execute <unnamed>: " in line
            if active:
                found.append(line)
        elif active and (line.startswith("\t") or "articles_app@articles DETAIL:  Parameters:" in line):
            found[-1] += " " + line.strip()
        else:
            active = False
    return found


# probe 比對真正 HTTP 請求的 SQL 次數、參數化條件及排序，排除逐筆查詢。
def probe(name, path, token, fragments, expected_id, parameter_id):
    before = db_logs()
    data = expect(call(EXPRESS, "GET", path, token=token), 200)
    if "list" in data:
        check(any(item["id"] == expected_id for item in data["list"]), name + " 未取得 fixture")
    else:
        check(data["id"] == expected_id, name + " 詳情錯誤")
    emitted = statements(before, db_logs())
    normalized = [re.sub(r"\s+", " ", line) for line in emitted]
    check(len(emitted) == len(fragments), f"{name} SQL 次數 {len(emitted)} != {len(fragments)}: {emitted}")
    check(all(fragment in line for fragment, line in zip(fragments, normalized)), name + " SQL 類型／順序不符")
    check(all("$1" in line for line in normalized[1:]), name + " 查詢未參數化")
    check(all(f"Parameters: $1 = '{parameter_id}'" in line for line in normalized[1:]), name + " fixture 參數不符")
    if "list" in data:
        check("ORDER BY" in normalized[-1] and "created_at DESC" in normalized[-1] and "id DESC" in normalized[-1], name + " 排序缺漏")
    print(f"PASS {name} emitted_sql={len(emitted)}")
    for index, line in enumerate(emitted):
        # 認證查詢的 jti 雖非完整 JWT，也不寫入驗證紀錄。
        print(line.split(" articles_app@articles DETAIL:")[0] if index == 0 and token else line)


# plan 對相同 WHERE/JOIN/ORDER/LIMIT 形狀執行帶參數的實際 fixture 計畫。
def plan(name, query, arguments):
    statement = "PREPARE ticket35_plan(bigint,bigint,bigint) AS " + query
    values = ",".join(str(int(value)) for value in arguments)
    output = sql(statement + "; EXPLAIN (ANALYZE, BUFFERS) EXECUTE ticket35_plan(" + values + "); DEALLOCATE ticket35_plan")
    check("Execution Time" in output and "Buffers" in output, name + " 計畫缺漏")
    print("PLAN", name, output.replace("\n", " | "))


# main 先驗隔離身分，再以唯一資料觀察單次 HTTP 與查詢計畫。
def main():
    guard()
    actor = create_user(secrets.token_hex(6) + "-query")
    title = "Ticket35 SQL " + secrets.token_hex(6)
    comment_body = "Ticket35 留言 SQL " + secrets.token_hex(6)
    expect(call(EXPRESS, "POST", "/api/articles", {"title": title, "body": "查詢本文"}, actor["token"]), 201)
    article = one("articles", actor["id"], "title", title)
    expect(call(EXPRESS, "POST", f"/api/articles/{article}/comments", {"body": comment_body}, actor["token"]), 201)
    comment = one("comments", actor["id"], "body", comment_body)
    check(sql(f"SELECT count(*) FROM articles WHERE author_id={int(actor['id'])}") == "1", "作者文章 fixture 不唯一")
    check(sql(f"SELECT count(*) FROM comments WHERE author_id={int(actor['id'])}") == "1", "作者留言 fixture 不唯一")
    auth = "SELECT a.id,a.email,a.nickname"
    for table, target in (("articles", article), ("comments", comment)):
        probe("owned-" + table, f"/api/me/{table}?page=1&per_page=20", actor["token"],
              (auth, f"SELECT count(*)::bigint AS total FROM {table} JOIN", f"SELECT {table}.id,{table}.author_id"), target, actor["id"])
    probe("comment-detail", f"/api/comments/{comment}", actor["token"],
          (auth, "SELECT comments.id,comments.author_id"), comment, comment)
    probe("article-comments", f"/api/articles/{article}/comments?page=1&per_page=20", None,
          ("SELECT author_id,visibility FROM articles", "SELECT count(*)::bigint AS total FROM comments JOIN",
           "SELECT comments.id,comments.author_id"), comment, article)
    columns = {
        "articles": "articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname",
        "comments": "comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname",
    }
    for table in ("articles", "comments"):
        base = f"FROM {table} JOIN accounts ON accounts.id={table}.author_id WHERE {table}.author_id=$1"
        plan(table + "-owned-count", "SELECT count(*)::bigint AS total " + base, (actor["id"], 20, 0))
        plan(table + "-owned-list", "SELECT " + columns[table] + " " + base +
             f" ORDER BY {table}.created_at DESC,{table}.id DESC LIMIT $2 OFFSET $3", (actor["id"], 20, 0))
    plan("comment-detail", "SELECT " + columns["comments"] + ",articles.visibility AS parent_visibility FROM comments "
         "JOIN accounts ON accounts.id=comments.author_id JOIN articles ON articles.id=comments.article_id WHERE comments.id=$1", (comment, 20, 0))
    plan("article-comments-count", "SELECT count(*)::bigint AS total FROM comments JOIN accounts ON accounts.id=comments.author_id "
         "WHERE comments.article_id=$1 AND comments.visibility='visible'", (article, 20, 0))
    plan("article-comments-list", "SELECT " + columns["comments"] + " FROM comments JOIN accounts ON accounts.id=comments.author_id "
         "WHERE comments.article_id=$1 AND comments.visibility='visible' ORDER BY comments.created_at DESC,comments.id DESC LIMIT $2 OFFSET $3", (article, 20, 0))


if __name__ == "__main__":
    main()
