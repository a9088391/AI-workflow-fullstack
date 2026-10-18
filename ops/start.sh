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

# 舊票隔離 override 沒有新增後端的專用埠；按 fixture 所屬票選擇服務。
backend_services="laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs"
case "${COMPOSE_FILE:-}" in
    *ops/tests/*)
        case "${COMPOSE_FILE}" in
            *ops/tests/ticket22_compose.yaml*) backend_services="laravel fastapi" ;;
            *ops/tests/ticket25_compose.yaml*) backend_services="laravel gin" ;;
            *ops/tests/ticket28_compose.yaml*) backend_services="laravel echo" ;;
            *ops/tests/ticket31_compose.yaml*) backend_services="laravel fiber" ;;
            *ops/tests/ticket34_compose.yaml*) backend_services="laravel express" ;;
            *ops/tests/ticket37_compose.yaml*|*ops/tests/ticket38_compose.yaml*|*ops/tests/ticket39_compose.yaml*) backend_services="laravel fastify" ;;
            *ops/tests/ticket40_compose.yaml*|*ops/tests/ticket41_compose.yaml*) backend_services="laravel nestjs" ;;
            *ops/tests/ticket19_compose.yaml*|*ops/tests/ticket20_compose.yaml*) backend_services="laravel flask" ;;
            *ops/tests/ticket16_compose.yaml*) backend_services="laravel symfony aspnet-core django" ;;
            *) backend_services="laravel symfony aspnet-core" ;;
        esac
        ;;
esac

# 既有資料若有待套用 SQL，先停所有本專案寫入，讓備份代表一致的更新前狀態。
docker compose --profile fiber --profile express --profile fastify --profile nestjs stop laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular nginx >/dev/null
docker compose up -d --wait db
PROJECT_WRITERS_STOPPED=1 docker compose up --build --exit-code-from sql sql

if [ "${1:-}" = '--backend-only' ]; then
    docker compose up -d --build --wait $backend_services
    if [ "$backend_services" = "laravel flask" ]; then
        echo 'Laravel／Flask 專用後端已就緒；前端尚未啟動'
    elif [ "$backend_services" = "laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs" ]; then
        echo 'Laravel／Symfony／ASP.NET Core／Django／Flask／FastAPI／Gin／Echo／Fiber／Express／Fastify／NestJS 後端已就緒；前端尚未啟動'
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
    # 一般展示啟動已完成前端；舊票隔離 fixture 保留原先僅 Vue 的服務範圍。
    frontend_services="frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular"
    case "${COMPOSE_FILE:-}" in *ops/tests/*) frontend_services="frontend-vue" ;; esac
    docker compose up -d --build --wait $backend_services $frontend_services nginx
    if [ "$backend_services" = "laravel flask" ]; then
        echo 'Vue／Laravel／Flask 專用展示已就緒：http://localhost:8080/vue/；Flask API：http://localhost:8085'
    elif [ "$backend_services" = "laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs" ]; then
        echo 'Vue／Nuxt／React／Next.js／Angular／Laravel 展示已就緒：http://localhost:8080/vue/、http://localhost:8080/nuxt/、http://localhost:8080/react/、http://localhost:8080/next/、http://localhost:8080/angular/；Symfony API：http://localhost:8082；ASP.NET Core API：http://localhost:8083；Django API：http://localhost:8084；Flask API：http://localhost:8085；FastAPI API：http://localhost:8086；Gin API：http://localhost:8087；Echo API：http://localhost:8088；Fiber API：http://localhost:8089；Express API：http://localhost:8090；Fastify API：http://localhost:8091；NestJS API：http://localhost:8092'
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
