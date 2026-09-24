#!/bin/sh
set -eu

# 既有任何後端金鑰缺漏或毀損都停止，避免重啟後在不同金鑰下繼續提供服務。
for backend in laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs; do
    key_file=".local/keys/$backend.key"
    if [ ! -f "$key_file" ] || [ "$(openssl base64 -d -A -in "$key_file" 2>/dev/null | wc -c | tr -d ' ')" != '32' ]; then
        echo "金鑰缺漏或毀損：${backend}；拒絕覆寫" >&2
        exit 1
    fi
done
