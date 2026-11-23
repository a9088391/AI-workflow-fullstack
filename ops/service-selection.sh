#!/bin/sh

# start/setup共用服務範圍，避免新fixture因路徑相似而誤套舊票的縮減模式。
legacy_backends="laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs"
all_backends="$legacy_backends rails"
backend_services="$all_backends"
frontend_services="frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit"
case "${COMPOSE_FILE:-}" in
    *ops/tests/rails05_compose.yaml*|*.local/rails05-compose.yaml*) ;;
    *ops/tests/cq34_compose.yaml*|*.local/cq34-compose.yaml*) backend_services="$legacy_backends" ;;
    *ops/tests/ticket22_compose.yaml*) backend_services="laravel fastapi"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket25_compose.yaml*) backend_services="laravel gin"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket28_compose.yaml*) backend_services="laravel echo"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket31_compose.yaml*) backend_services="laravel fiber"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket34_compose.yaml*) backend_services="laravel express"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket37_compose.yaml*|*ops/tests/ticket38_compose.yaml*|*ops/tests/ticket39_compose.yaml*) backend_services="laravel fastify"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket40_compose.yaml*|*ops/tests/ticket41_compose.yaml*) backend_services="laravel nestjs"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket19_compose.yaml*|*ops/tests/ticket20_compose.yaml*) backend_services="laravel flask"; frontend_services="frontend-vue" ;;
    *ops/tests/ticket16_compose.yaml*) backend_services="laravel symfony aspnet-core django"; frontend_services="frontend-vue" ;;
    *ops/tests/aspnet_compose.yaml*|*ops/tests/auth_compose.yaml*|*ops/tests/symfony_compose.yaml*|*ops/tests/rails01_compose.yaml*|*ops/tests/cq32_compose.yaml*|*ops/tests/cq33_compose.yaml*|*ops/tests/perf01_compose.yaml*|*ops/tests/ticket09_compose.yaml*|*ops/tests/ticket10_compose.yaml*|*ops/tests/ticket12_compose.yaml*|*ops/tests/ticket14_compose.yaml*|*ops/tests/ticket15_compose.yaml*|*ops/tests/ticket17_compose.yaml*|*ops/tests/ticket18_compose.yaml*|*ops/tests/ticket23_compose.yaml*|*ops/tests/ticket24_compose.yaml*|*ops/tests/ticket30_compose.yaml*|*ops/tests/ticket32_compose.yaml*|*ops/tests/ticket33_compose.yaml*|*ops/tests/ticket35_compose.yaml*|*ops/tests/ticket36_compose.yaml*|*ops/tests/ticket42_compose.yaml*|*ops/tests/ticket51_compose.yaml*|*ops/tests/ticket54_compose.yaml*)
        backend_services="laravel symfony aspnet-core"
        frontend_services="frontend-vue"
        ;;
esac
