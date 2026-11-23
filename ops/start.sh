#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
# 先拒絕未知／重複參數，不能在輸入錯誤時停掉既有展示或變更金鑰。
backend_only=0
without_rails=0
for option in "$@"; do
    case "$option" in
        --backend-only) [ "$backend_only" = 0 ] || { echo '重複參數：--backend-only' >&2; exit 2; }; backend_only=1 ;;
        --without-rails) [ "$without_rails" = 0 ] || { echo '重複參數：--without-rails' >&2; exit 2; }; without_rails=1 ;;
        *) echo "未知參數：$option；用法：ops/start.sh [--backend-only] [--without-rails]" >&2; exit 2 ;;
    esac
done
if [ ! -f .env ]; then
    echo '尚未完成首次設定；請先執行 ops/setup.sh' >&2
    exit 1
fi
project_name="${COMPOSE_PROJECT_NAME:-ai-workflow-fullstack}"
if [ ! -f ".local/setup-complete-$project_name" ] && [ "${SETUP_IN_PROGRESS:-0}" != '1' ]; then
    echo '首次管理員尚未設定完成；請執行 ops/setup.sh' >&2
    exit 1
fi

# 僅在原十二份完整時明確執行一次Rails加入；完成後缺漏一律停止、不換key。
ops/check-keys.sh --initialize-rails
. ops/service-selection.sh
if [ "$without_rails" = 1 ] && [ "$backend_services" = "$all_backends" ]; then
    backend_services="$legacy_backends"
fi

# 既有資料若有待套用 SQL，先停所有本專案寫入，讓備份代表一致的更新前狀態。
docker compose --profile rails stop $all_backends frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx >/dev/null
docker compose up -d --wait db
PROJECT_WRITERS_STOPPED=1 docker compose up --build --exit-code-from sql sql

if [ "$backend_only" = 1 ]; then
    docker compose up -d --build --wait $backend_services
    if [ "$backend_services" = "laravel flask" ]; then
        echo 'Laravel／Flask 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "$all_backends" ]; then
        echo '十三後端已就緒（含 Rails：http://localhost:8093）；前端尚未啟動'
    elif [ "$backend_services" = "$legacy_backends" ]; then
        echo '十二後端模式已就緒；Rails 已停止；前端尚未啟動'
    elif [ "$backend_services" = "laravel fiber" ]; then
        echo 'Laravel／Fiber 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel express" ]; then
        echo 'Laravel／Express 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel fastify" ]; then
        case "${COMPOSE_FILE:-}" in
            *ops/tests/ticket39_compose.yaml*) echo 'Laravel／Fastify 專用後端已就緒；Laravel API：http://localhost:18392；Fastify API：http://localhost:18393；前端尚未啟動' ;;
            *) echo 'Laravel／Fastify 專用後端已就緒；前端尚未啟動' ;;
        esac
    elif [ "$backend_services" = "laravel nestjs" ]; then
        echo 'Laravel／NestJS 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel gin" ]; then
        echo 'Laravel／Gin 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel echo" ]; then
        echo 'Laravel／Echo 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel fastapi" ]; then
        echo 'Laravel／FastAPI 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel symfony aspnet-core django" ]; then
        echo 'Laravel／Symfony／ASP.NET Core／Django 後端已就緒；前端尚未啟動'
    else
        echo 'Laravel／Symfony／ASP.NET Core 後端已就緒；前端尚未啟動'
    fi
else
    # Compose --wait涵蓋所選全部應用，Rails不健康時正常模式不會宣告十三版就緒。
    docker compose up -d --build --wait $backend_services $frontend_services nginx
    if [ "$backend_services" = "laravel flask" ]; then
        echo 'Vue／Laravel／Flask 專用展示已就緒：http://localhost:8080/vue/；Flask API：http://localhost:8085'
    elif [ "$backend_services" = "$all_backends" ] || [ "$backend_services" = "$legacy_backends" ]; then
        if [ "$backend_services" = "$all_backends" ]; then
            echo '十三後端及六前端已就緒；Rails API：http://localhost:8093'
        else
            echo '十二後端模式及六前端已就緒；Rails 已停止'
        fi
        echo 'Vue：http://localhost:8080/vue/；Nuxt：http://localhost:8080/nuxt/；React：http://localhost:8080/react/；Next.js：http://localhost:8080/next/；Angular：http://localhost:8080/angular/；SvelteKit：http://localhost:8080/sveltekit/'
    elif [ "$backend_services" = "laravel fiber" ]; then
        echo 'Vue／Laravel／Fiber 專用展示已就緒：http://localhost:18311/vue/；Fiber API：http://localhost:8089'
    elif [ "$backend_services" = "laravel express" ]; then
        echo 'Vue／Laravel／Express 專用展示已就緒：http://localhost:18341/vue/；Express API：http://localhost:18343'
    elif [ "$backend_services" = "laravel fastify" ]; then
        case "${COMPOSE_FILE:-}" in
            *ops/tests/ticket39_compose.yaml*) echo 'Vue／Laravel／Fastify 專用展示已就緒：http://localhost:18391/vue/；Laravel API：http://localhost:18392；Fastify API：http://localhost:18393' ;;
            *ops/tests/ticket38_compose.yaml*) echo 'Vue／Laravel／Fastify 專用展示已就緒：http://localhost:18381/vue/；Laravel API：http://localhost:18382；Fastify API：http://localhost:18383' ;;
            *) echo 'Vue／Laravel／Fastify 專用展示已就緒：http://localhost:18371/vue/；Fastify API：http://localhost:18373' ;;
        esac
    elif [ "$backend_services" = "laravel nestjs" ]; then
        case "${COMPOSE_FILE:-}" in
            *ops/tests/ticket41_compose.yaml*) echo 'Vue／Laravel／NestJS 專用展示已就緒：http://localhost:18411/vue/；Laravel API：http://localhost:18412；NestJS API：http://localhost:18413' ;;
            *) echo 'Vue／Laravel／NestJS 專用展示已就緒：http://localhost:18401/vue/；Laravel API：http://localhost:18402；NestJS API：http://localhost:18403' ;;
        esac
    elif [ "$backend_services" = "laravel gin" ]; then
        echo 'Vue／Laravel／Gin 專用展示已就緒：http://localhost:8090/vue/；Gin API：http://localhost:8087'
    elif [ "$backend_services" = "laravel echo" ]; then
        echo 'Vue／Laravel／Echo 專用展示已就緒：http://localhost:18090/vue/；Echo API：http://localhost:8088'
    elif [ "$backend_services" = "laravel fastapi" ]; then
        echo 'Vue／Laravel／FastAPI 專用展示已就緒：http://localhost:8080/vue/；FastAPI API：http://localhost:8086'
    elif [ "$backend_services" = "laravel symfony aspnet-core django" ]; then
        echo 'Vue／Laravel 展示已就緒：http://localhost:8080/vue/；Symfony API：http://localhost:8082；ASP.NET Core API：http://localhost:8083；Django API：http://localhost:8084'
    else
        echo 'Vue／Laravel 展示已就緒：http://localhost:8080/vue/；Symfony API：http://localhost:8082；ASP.NET Core API：http://localhost:8083'
    fi
fi
