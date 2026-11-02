"""CQ25：沿用內容編排與固定 guard，驗證 FastAPI 並只收束本票 fixture。"""

import sys

import cq23_http as content


# 固定票號與後端交給既有編排，CLI 不開放改寫目標或恢復其他票資料。
def main():
    content.main(backend="fastapi", ticket="cq25")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # 與原入口共用安全例外鏈輸出，避免把測試輸入或憑證寫入 artifact。
        content.report_failure(error, ticket="cq25")
        sys.exit(1)
