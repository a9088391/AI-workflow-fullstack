#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
    echo '尚未完成首次設定；請先執行 ops/setup.sh' >&2
    exit 1
fi
ops/check-keys.sh
project_name="${COMPOSE_PROJECT_NAME:-ai-workflow-fullstack}"
if [ ! -f ".local/setup-complete-$project_name" ] && [ "${SETUP_IN_PROGRESS:-0}" != '1' ]; then
    echo '首次管理員尚未設定完成；請執行 ops/setup.sh' >&2
    exit 1
fi

# 舊票隔離 override 沒有 Django 專用金鑰與埠；只在一般展示或 Ticket 16 fixture 啟動它。
backend_services="laravel symfony aspnet-core django"
case "${COMPOSE_FILE:-}" in
    *ops/tests/*)
        case "${COMPOSE_FILE}" in
            *ops/tests/ticket16_compose.yaml*) ;;
            *) backend_services="laravel symfony aspnet-core" ;;
        esac
        ;;
esac

# 既有資料若有待套用 SQL，先停所有本專案寫入，讓備份代表一致的更新前狀態。
docker compose stop laravel symfony aspnet-core django frontend-vue nginx >/dev/null
docker compose up -d --wait db
PROJECT_WRITERS_STOPPED=1 docker compose up --build --exit-code-from sql sql

if [ "${1:-}" = '--backend-only' ]; then
    docker compose up -d --build --wait $backend_services
    if [ "$backend_services" = "laravel symfony aspnet-core django" ]; then
        echo 'Laravel／Symfony／ASP.NET Core／Django 後端已就緒；前端尚未啟動'
    else
        echo 'Laravel／Symfony／ASP.NET Core 後端已就緒；前端尚未啟動'
    fi
else
    docker compose up -d --build --wait $backend_services frontend-vue nginx
    if [ "$backend_services" = "laravel symfony aspnet-core django" ]; then
        echo 'Vue／Laravel 展示已就緒：http://localhost:8080/vue/；Symfony API：http://localhost:8082；ASP.NET Core API：http://localhost:8083；Django API：http://localhost:8084'
    else
        echo 'Vue／Laravel 展示已就緒：http://localhost:8080/vue/；Symfony API：http://localhost:8082；ASP.NET Core API：http://localhost:8083'
    fi
fi
