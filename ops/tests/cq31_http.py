"""CQ31：固定 NestJS 目標，重用 CQ26 兩階段編排、公開案例與精確恢復。"""

import sys

import cq26_http as suite


BACKEND = "nestjs"
TICKET = "cq31"


# 固定後端與票號傳入既有編排，讓 CLI、nonce、baseline 與 finally 都遵守同一流程。
def main(argv=None):
    suite.main(argv, backend=BACKEND, ticket=TICKET)


# 將此薄入口的 code object 加入既有安全清單，診斷只輸出可信位置及固定例外類型。
def report_failure(error):
    suite.report_failure(error, ticket=TICKET, modules=((sys.modules[__name__], "cq31_http.py"),))


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        report_failure(error)
        sys.exit(1)
