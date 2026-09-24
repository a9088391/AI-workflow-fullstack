#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
# down 不加 -v，保留資料庫、備份及金鑰供下次啟動沿用。
docker compose down
