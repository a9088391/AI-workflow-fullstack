"""量測 Ticket 42 真實 HTTP SQL 次數與相同 emitted SQL 的查詢計畫。"""

import re
import secrets
from urllib.parse import quote

from ticket40_http import call, check, expect, sql
from ticket42_http import NESTJS, create_user, db_logs, guard


# statements 同時計入無參數 simple query 與 prepared execute，不漏算 count。
def statements(before, after):
    check(after.startswith(before), "DB 日誌順序改變")
    found = []
    active = False
    for line in after[len(before):].splitlines():
        if "articles_app@articles LOG:" in line:
            active = " execute <unnamed>: " in line or " statement: " in line
            if active and not any(marker in line for marker in
                                  ("SELECT 1 FROM accounts,login_sessions", "select exists(select *", "set time zone", "set search_path")):
                found.append(line)
            else:
                active = False
        elif active and (line.startswith("\t") or "articles_app@articles DETAIL:  Parameters:" in line):
            found[-1] += " " + line.strip()
        else:
            active = False
    return found


# emitted_sql 從日誌取 SQL 本文，不把 session 的 bind 值寫到輸出。
def emitted_sql(line):
    marker = " execute <unnamed>: " if " execute <unnamed>: " in line else " statement: "
    check(marker in line, "SQL 日誌形狀不符")
    body = line.split(marker, 1)[1]
    return re.split(r" \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ UTC \[\d+\] articles_app@articles DETAIL:", body, maxsplit=1)[0].strip()


# probe 從隔離 DB 日誌核對單次真實 HTTP 的認證交易、計數與列表查詢。
def probe(name, path, token, fragments, expected_id=None, deep=False):
    before = db_logs()
    data = expect(call(NESTJS, "GET", path, token=token), 200)
    if expected_id is not None:
        check(expected_id in {row["id"] for row in data["list"]}, name + " 未取得 fixture")
    emitted = [emitted_sql(line) for line in statements(before, db_logs())]
    check(len(emitted) == (7 if deep else 8), f"{name} SQL 次數 {len(emitted)}：{emitted}")
    check("FROM login_sessions s JOIN accounts a" in emitted[0] and emitted[1] == "BEGIN" and
          "FROM accounts WHERE id=$1 FOR UPDATE" in emitted[2] and
          "FROM login_sessions WHERE jti=$1 FOR UPDATE" in emitted[3] and
          "clock_timestamp()" in emitted[4] and emitted[-1] == "COMMIT", name + " 認證交易 SQL 形狀不同")
    check(fragments[0] in re.sub(r"\s+", " ", emitted[5]), name + " count SQL 路徑不同")
    if deep:
        check(data["list"] == [] and data["pagination"]["total"] > 0, name + " 深頁結果不符")
    else:
        check(fragments[1] in re.sub(r"\s+", " ", emitted[6]) and
              "created_at DESC" in emitted[6] and "id DESC" in emitted[6], name + " list SQL 路徑不同")
    print(f"PASS {name} emitted_sql={len(emitted)} total={data['pagination']['total']} list={'no' if deep else 'yes'}")
    return emitted[5], None if deep else emitted[6]


# plan 對該 HTTP 實際送出的 SQL 與相同參數執行 EXPLAIN，不用手寫近似查詢。
def plan(name, query, signature, arguments):
    check(query.lstrip().upper().startswith("SELECT ") and ";" not in query, name + " 非單一 SELECT")
    check({int(value) for value in re.findall(r"\$(\d+)", query)} == set(range(1, len(arguments) + 1)), name + " bind 不符")
    values = ",".join("'" + value.replace("'", "''") + "'" if isinstance(value, str) else str(value) for value in arguments)
    prepared = "ticket42_plan(" + signature + ")" if signature else "ticket42_plan"
    executed = "ticket42_plan(" + values + ")" if arguments else "ticket42_plan"
    output = sql(f"PREPARE {prepared} AS {query}; EXPLAIN (ANALYZE, BUFFERS) EXECUTE {executed}; DEALLOCATE ticket42_plan")
    check("Execution Time" in output and "Buffers" in output, name + " 缺查詢計畫")
    print("PLAN", name, output.replace("\n", " | "))


# main 先跑隔離 guard，才建立量測資料並比對 HTTP 實際送出的 SQL。
def main():
    guard()
    nonce = secrets.token_hex(5)
    admin = create_user(nonce + "-admin", admin=True)
    member = create_user(nonce + "-member")
    # 此暱稱使一字、二字、中文與字面特殊符號各有非空列表可量。
    expect(call(NESTJS, "POST", "/api/auth/register", {"email": f"t42-{nonce}-plan-search@example.test", "password": "Abcd1234", "nickname": "aa陳%_\\" + nonce}), 201)
    title = "ticket42-plan-" + nonce
    expect(call(NESTJS, "POST", "/api/articles", {"title": title, "body": "計畫本文"}, member["token"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE title='{title}'"))
    expect(call(NESTJS, "POST", f"/api/articles/{article}/comments", {"body": title}, member["token"]), 201)
    comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body='{title}'"))
    # 讓計畫統計對應本次隔離測資，避免以舊估算推論掃描選擇。
    sql("ANALYZE accounts,articles,comments")
    print("FIXTURE accounts=" + sql("SELECT count(*) FROM accounts") + " articles=" + sql("SELECT count(*) FROM articles") +
          " comments=" + sql("SELECT count(*) FROM comments") + " (small fixture, not load SLA)")
    unfiltered = probe("members-unfiltered", "/api/members?page=1&per_page=100", admin["token"],
                       ("SELECT count(*)::bigint AS total FROM accounts", "SELECT id,email,nickname,role,status,created_at FROM accounts"), member["id"])
    plan("members-unfiltered-count", unfiltered[0], "", ())
    plan("members-unfiltered-list", unfiltered[1], "integer,bigint", (100, 0))
    for keyword in ("a", "aa", "陳", "%_\\", member["email"]):
        # 每個 keyword 都由真實請求取出同形 count/list，重現該次 bind 值。
        path = "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(keyword)
        count, listing = probe("members-keyword-" + repr(keyword), path, admin["token"],
                               ("strpos(translate(nickname", "SELECT id,email,nickname,role,status,created_at FROM accounts"))
        plan("members-count-" + repr(keyword), count, "text", (keyword,))
        plan("members-list-" + repr(keyword), listing, "text,integer,bigint", (keyword, 100, 0))
    deep = probe("members-deep", "/api/members?page=999999&per_page=1", admin["token"],
                 ("SELECT count(*)::bigint AS total FROM accounts", ""), deep=True)
    plan("members-deep-count", deep[0], "", ())
    for name, path, fragments, expected in (
        ("comments-management", "/api/comments?page=1&per_page=100", ("SELECT count(*)::bigint AS total FROM comments JOIN", "SELECT comments.id,comments.author_id"), comment),
        ("articles-management", "/api/articles?page=1&per_page=100&filters[scope]=management", ("SELECT count(*)::bigint AS total FROM articles JOIN", "SELECT articles.id,articles.author_id"), article),
    ):
        count, listing = probe(name, path, admin["token"], fragments, expected)
        plan(name + "-count", count, "", ())
        plan(name + "-list", listing, "integer,bigint", (100, 0))
    print("PASS Ticket 42 emitted SQL counts and EXPLAIN (ANALYZE, BUFFERS)")


if __name__ == "__main__":
    main()
