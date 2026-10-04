#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
# 啟用 Django profile 以納入已啟動服務；down 不加 -v，保留資料與金鑰。
docker compose --profile django down
