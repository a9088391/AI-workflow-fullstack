"""在 ticket28_test 隔離資料庫暫開 PostgreSQL 日誌，量測 Echo 單次讀取的 SQL 數。"""

import subprocess
import time
import re
from urllib.request import urlopen

from ticket28_http import ECHO, call, check, expect, guard, sql


# compose 只操作已由 guard 驗證的隔離專案，避免碰觸展示服務。
def compose(*args):
    return subprocess.check_output(["docker", "compose", *args], text=True)


# ready 等待同一 Echo 容器恢復健康，讓新連線讀取暫時的角色日誌設定。
def ready():
    for _ in range(40):
        try:
            with urlopen(ECHO + "/up", timeout=2) as response:
                if response.status == 200 and response.read() == b"OK":
                    return
        except OSError:
            pass
        time.sleep(0.25)
    raise AssertionError("Echo 重啟後未恢復健康")


# db_logs 只讀取隔離 DB 容器的 stderr，不修改 PostgreSQL 的持久日誌檔。
def db_logs():
    return compose("logs", "--no-color", "--no-log-prefix", "db")


# statements 截取單次 HTTP 請求期間的 execute 日誌，排除週期性 /up 查核。
def statements(before, after):
    check(after.startswith(before), "PostgreSQL 日誌未保持可比的追加順序")
    return [line for line in after[len(before):].splitlines() if " LOG:  execute " in line and "FROM accounts,login_sessions" not in line]


# main 僅暫改隔離應用角色的 log_statement；finally 重設設定並重啟 Echo 清除連線設定。
def main():
    guard()
    check(sql("SELECT coalesce(array_to_string(rolconfig,','),'') FROM pg_roles WHERE rolname='articles_app'") == "", "應用角色已有設定，拒絕覆寫")
    try:
        sql("ALTER ROLE articles_app SET log_statement='all'")
        compose("restart", "echo")
        ready()
        # 每次只送一個請求；固定 SQL 文字與參數由 DB 原生日誌提供，不靠靜態程式推斷。
        for name, path, expected in (
            ("articles", "/api/articles?page=1&per_page=20", ("SELECT count(*) FROM articles JOIN", "SELECT articles.id,articles.author_id")),
            ("comments", "/api/articles/1/comments?page=1&per_page=20", ("SELECT author_id,visibility FROM articles", "SELECT count(*) FROM comments JOIN", "SELECT comments.id,comments.author_id")),
        ):
            before = db_logs()
            # 留言父文章必須存在；使用隔離資料庫中的第一筆文章 ID 取得確定可讀目標。
            if name == "comments":
                article_id = sql("SELECT id FROM articles WHERE visibility='visible' ORDER BY id LIMIT 1")
                check(article_id.isdecimal(), "隔離資料缺少公開文章")
                check(int(sql(f"SELECT count(*) FROM comments WHERE article_id={article_id} AND visibility='visible'")) > 0, "隔離文章缺少公開留言")
                path = f"/api/articles/{article_id}/comments?page=1&per_page=20"
                before = db_logs()
            expect(call(ECHO, "GET", path), 200)
            emitted = statements(before, db_logs())
            check(len(emitted) == len(expected), f"{name} 實際 SQL 數={len(emitted)}，預期 {len(expected)}: {emitted}")
            check(all(fragment in line for fragment, line in zip(expected, emitted)), f"{name} SQL 種類或順序不符: {emitted}")
            check(len({re.search(r"\[(\d+)\]", line).group(1) for line in emitted}) == 1, f"{name} SQL 來自不同 DB 連線: {emitted}")
            print(f"PASS {name} SQL count={len(emitted)}")
            for line in emitted:
                print(line)
    finally:
        sql("ALTER ROLE articles_app RESET log_statement")
        compose("restart", "echo")
        ready()
        check(sql("SELECT coalesce(array_to_string(rolconfig,','),'') FROM pg_roles WHERE rolname='articles_app'") == "", "應用角色日誌設定未恢復")
        print("PASS ticket28 SQL logging restored")


if __name__ == "__main__":
    main()
