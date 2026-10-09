"""在隔離 PostgreSQL 日誌量測 Express 公開文章與留言列表的實際 SQL 次數。"""

import subprocess

from ticket34_http import EXPRESS, call, check, expect, guard, sql


# db_logs 只讀 ticket34_test 資料庫 stderr，不改角色設定或持久資料。
def db_logs():
    return subprocess.check_output(["docker", "compose", "logs", "--no-color", "--no-log-prefix", "db"], text=True)


# statements 取單次請求的簡單 statement 或參數化 execute，略過健康檢查。
def statements(before, after):
    check(after.startswith(before), "資料庫日誌順序改變，無法量測")
    return [line for line in after[len(before):].splitlines()
            if "articles_app@articles LOG:" in line and (" execute " in line or " statement: " in line)
            and "FROM accounts,login_sessions" not in line]


# main 以已存在的隔離內容讀一次各列表，核對 SQL 種類與執行次數。
def main():
    guard()
    article_id = sql("SELECT id FROM articles WHERE visibility='visible' AND EXISTS(SELECT 1 FROM comments WHERE comments.article_id=articles.id AND comments.visibility='visible') ORDER BY id DESC LIMIT 1")
    check(article_id.isdecimal(), "隔離文章/留言測資不足")
    admin_email = sql("SELECT email FROM accounts WHERE role='admin' AND email LIKE 'ticket34-%admin@example.test' ORDER BY id DESC LIMIT 1")
    check(admin_email.startswith("ticket34-") and admin_email.endswith("admin@example.test"), "隔離管理員測資不足")
    admin = expect(call(EXPRESS, "POST", "/api/auth/login", {"email": admin_email, "password": "Abcd1234"}), 200)
    for name, path, token, fragments in (
        ("articles-public", "/api/articles?page=1&per_page=20", None, ("SELECT count(*)::bigint AS total FROM articles JOIN", "SELECT articles.id,articles.author_id")),
        ("articles-management", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management", admin["access_token"], ("SELECT a.id,a.email,a.nickname", "SELECT count(*)::bigint AS total FROM articles JOIN", "SELECT articles.id,articles.author_id")),
        ("comments-public", f"/api/articles/{article_id}/comments?page=1&per_page=20", None, ("SELECT author_id,visibility FROM articles", "SELECT count(*)::bigint AS total FROM comments JOIN", "SELECT comments.id,comments.author_id")),
        ("comments-management", f"/api/articles/{article_id}/comments?page=1&per_page=20", admin["access_token"], ("SELECT a.id,a.email,a.nickname", "SELECT author_id,visibility FROM articles", "SELECT count(*)::bigint AS total FROM comments JOIN", "SELECT comments.id,comments.author_id")),
    ):
        before = db_logs()
        expect(call(EXPRESS, "GET", path, token=token), 200)
        emitted = statements(before, db_logs())
        check(len(emitted) == len(fragments), f"{name} SQL 次數 {len(emitted)}，預期 {len(fragments)}：{emitted}")
        check(all(fragment in line for fragment, line in zip(fragments, emitted)), f"{name} SQL 順序不符：{emitted}")
        print(f"PASS {name} SQL count={len(emitted)}")
        for line in emitted:
            print(line)
    # 用測試資料的實際條件執行 count/列表計畫；小表掃描型態只記證據不強迫索引。
    for name, query in (
        ("articles-public-count", "SELECT count(*)::bigint AS total FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible'"),
        ("articles-public-list", "SELECT articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible' ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0"),
        ("articles-management-list", "SELECT articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0"),
        ("comments-public-count", f"SELECT count(*)::bigint AS total FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id={article_id} AND comments.visibility='visible'"),
        ("comments-public-list", f"SELECT comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id={article_id} AND comments.visibility='visible' ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0"),
        ("comments-management-list", f"SELECT comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id={article_id} ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0"),
    ):
        plan = sql("EXPLAIN (ANALYZE, BUFFERS) " + query)
        check("Execution Time" in plan and "Buffers" in plan, name + " 計畫缺漏")
        print("PLAN", name, plan.replace("\n", " | "))


if __name__ == "__main__":
    main()
