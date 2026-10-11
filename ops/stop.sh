#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
# 啟用已實作後端 profile 以納入停止範圍；不加 -v，保留資料與金鑰。
docker compose --profile django --profile flask --profile fastapi --profile gin --profile echo --profile fiber --profile express down
