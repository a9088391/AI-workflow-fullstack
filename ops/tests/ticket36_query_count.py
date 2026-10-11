"""量測 Ticket36 管理列表真正送出的 SQL 次數與相同形狀的查詢計畫。"""

import re
import secrets
from urllib.parse import quote

from ticket34_http import call, check, expect, sql
from ticket35_query_count import db_logs
from ticket36_http import EXPRESS, create_user, guard


# statements 同時計入無參數 simple query 與 prepared execute，不漏算 count。
def statements(before, after):
    check(after.startswith(before), "DB 日誌順序改變")
    found = []
    active = False
    for line in after[len(before):].splitlines():
        if "articles_app@articles LOG:" in line:
            active = " execute <unnamed>: " in line or " statement: " in line
            if active:
                found.append(line)
        elif active and (line.startswith("\t") or "articles_app@articles DETAIL:  Parameters:" in line):
            found[-1] += " " + line.strip()
        else:
            active = False
    return found


# probe 從隔離 DB 日誌核對單次真實 HTTP 的認證、計數與列表查詢。
def probe(name, path, token, fragments, expected_id):
    before = db_logs()
    data = expect(call(EXPRESS, "GET", path, token=token), 200)
    check(expected_id in [row["id"] for row in data["list"]], name + " 未取得 fixture")
    emitted = statements(before, db_logs())
    normalized = [re.sub(r"\s+", " ", line) for line in emitted]
    check(len(emitted) == len(fragments), f"{name} SQL 次數 {len(emitted)}：{emitted}")
    check(all(fragment in line for fragment, line in zip(fragments, normalized)), name + " SQL 路徑不同")
    check("ORDER BY" in normalized[-1] and "created_at DESC" in normalized[-1] and "id DESC" in normalized[-1], name + " 排序缺漏")
    print(f"PASS {name} emitted_sql={len(emitted)} total={data['pagination']['total']}")
    # 認證參數含 jti，僅輸出所需 SQL 型態，避免測試紀錄洩漏 session 識別。
    for line in normalized:
        print(line.split(" articles_app@articles DETAIL:")[0])


# plan 對與實際查詢相同的 WHERE/ORDER/LIMIT 形狀執行帶 fixture 參數計畫。
def plan(name, signature, query, arguments):
    values = ",".join("'" + value.replace("'", "''") + "'" if isinstance(value, str) else str(value) for value in arguments)
    prepared = "ticket36_plan(" + signature + ")" if signature else "ticket36_plan"
    executed = "ticket36_plan(" + values + ")" if arguments else "ticket36_plan"
    output = sql(f"PREPARE {prepared} AS {query}; EXPLAIN (ANALYZE, BUFFERS) EXECUTE {executed}; DEALLOCATE ticket36_plan")
    check("Execution Time" in output and "Buffers" in output, name + " 缺查詢計畫")
    print("PLAN", name, output.replace("\n", " | "))


# main 先跑隔離 guard，才建立量測資料並比對 HTTP 實際送出的 SQL。
def main():
    guard()
    nonce = secrets.token_hex(5)
    admin = create_user(nonce + "-admin", admin=True)
    member = create_user(nonce + "-member")
    title = "ticket36-plan-" + nonce
    expect(call(EXPRESS, "POST", "/api/articles", {"title": title, "body": "計畫本文"}, member["token"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE title='{title}'"))
    expect(call(EXPRESS, "POST", f"/api/articles/{article}/comments", {"body": title}, member["token"]), 201)
    comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body='{title}'"))
    probe("members-unfiltered", "/api/members?page=1&per_page=100", admin["token"],
          ("SELECT a.id,a.email", "SELECT count(*)::bigint AS total FROM accounts", "SELECT id,email,nickname,role,status,created_at FROM accounts"), member["id"])
    probe("members-keyword", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(member["email"]), admin["token"],
          ("SELECT a.id,a.email", "strpos(translate(nickname", "SELECT id,email,nickname,role,status,created_at FROM accounts"), member["id"])
    probe("comments-management", "/api/comments?page=1&per_page=100", admin["token"],
          ("SELECT a.id,a.email", "SELECT count(*)::bigint AS total FROM comments JOIN", "SELECT comments.id,comments.author_id"), comment)
    probe("articles-management", "/api/articles?page=1&per_page=100&filters[scope]=management", admin["token"],
          ("SELECT a.id,a.email", "SELECT count(*)::bigint AS total FROM articles JOIN", "SELECT articles.id,articles.author_id"), article)
    # 讓計畫統計對應本次隔離測資，避免以舊估算推論掃描選擇。
    sql("ANALYZE accounts,articles,comments")
    print("FIXTURE accounts=" + sql("SELECT count(*) FROM accounts") + " articles=" + sql("SELECT count(*) FROM articles") +
          " comments=" + sql("SELECT count(*) FROM comments"))
    member_base = "FROM accounts WHERE role='member'"
    match = " AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0)"
    for keyword in ("a", "aa", "陳", "%_\\", member["email"]):
        # 一、二字與無可抽取 trigram 的字面符號都使用同一精確查詢形狀。
        plan("members-count-" + repr(keyword), "text", "SELECT count(*)::bigint AS total " + member_base + match, (keyword,))
        plan("members-list-" + repr(keyword), "text,integer,bigint", "SELECT id,email,nickname,role,status,created_at " + member_base + match +
             " ORDER BY created_at DESC,id DESC LIMIT $2 OFFSET $3", (keyword, 100, 0))
    plan("members-no-filter-count", "", "SELECT count(*)::bigint AS total " + member_base, ())
    plan("members-no-filter-list", "integer,bigint", "SELECT id,email,nickname,role,status,created_at " + member_base +
         " ORDER BY created_at DESC,id DESC LIMIT $1 OFFSET $2", (100, 0))
    base = "FROM comments JOIN accounts ON accounts.id=comments.author_id"
    columns = "comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname"
    plan("comments-management-count", "", "SELECT count(*)::bigint AS total " + base, ())
    plan("comments-management-list", "integer,bigint", "SELECT " + columns + " " + base +
         " ORDER BY comments.created_at DESC,comments.id DESC LIMIT $1 OFFSET $2", (100, 0))
    article_base = "FROM articles JOIN accounts ON accounts.id=articles.author_id"
    article_columns = "articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname"
    plan("articles-management-count", "", "SELECT count(*)::bigint AS total " + article_base, ())
    plan("articles-management-list", "integer,bigint", "SELECT " + article_columns + " " + article_base +
         " ORDER BY articles.created_at DESC,articles.id DESC LIMIT $1 OFFSET $2", (100, 0))
    print("PASS Ticket 36 emitted SQL 與 EXPLAIN (ANALYZE, BUFFERS)")


if __name__ == "__main__":
    main()
