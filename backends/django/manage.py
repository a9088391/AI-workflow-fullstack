#!/usr/bin/env python3
"""啟動 Django 專案命令，供本機檢查與展示入口使用。"""

import os
import sys


# 明確指定本專案設定，避免使用主機其他 Django 專案的設定。
def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "site_api.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
