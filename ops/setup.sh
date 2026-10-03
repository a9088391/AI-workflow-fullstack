#!/bin/sh
set -eu
umask 077

cd "$(dirname "$0")/.."
project_name="${COMPOSE_PROJECT_NAME:-ai-workflow-fullstack}"
tty_hidden=0
# 管理員輸入若中斷或失敗，恢復終端顯示並停掉未完成的後端。
cleanup() {
    if [ "$tty_hidden" = '1' ]; then
        stty echo
        printf '\n'
    fi
    if [ ! -f ".local/setup-complete-$project_name" ]; then
        docker compose stop laravel symfony aspnet-core frontend-vue nginx >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p .local/keys .local/backups .local/laravel-logs
if [ ! -f .env ]; then
    if [ -n "$(find .local/keys -type f -print -quit)" ]; then
        echo '發現未完成的首次金鑰設定；拒絕重新產生金鑰' >&2
        exit 1
    fi
    # 首次產生持久化設定，日後重跑不得改變資料庫憑證與應用金鑰。
    owner_password="$(openssl rand -hex 32)"
    app_password="$(openssl rand -hex 32)"
    app_key="$(openssl rand -base64 32 | tr -d '\n')"
    {
        printf 'DB_OWNER_PASSWORD=%s\n' "$owner_password"
        printf 'DB_APP_PASSWORD=%s\n' "$app_password"
        printf 'APP_KEY=base64:%s\n' "$app_key"
    } > .env.pending

    # .env 最後才落地；若中途失敗，下一次會因殘留金鑰停下供人工查核。
    for backend in laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs; do
        openssl rand -base64 32 > ".local/keys/$backend.key"
    done
    mv .env.pending .env
fi

ops/check-keys.sh

# 先啟動資料與後端供管理員指令使用，成功建立後才開放前端入口。
SETUP_IN_PROGRESS=1 ops/start.sh --backend-only

printf '首次管理員 Email：'
IFS= read -r admin_email
printf '首次管理員暱稱：'
IFS= read -r admin_nickname
printf '首次管理員密碼（輸入不顯示）：'
if [ -t 0 ]; then
    stty -echo
    tty_hidden=1
fi
IFS= read -r admin_password
if [ "$tty_hidden" = '1' ]; then
    stty echo
    tty_hidden=0
fi
printf '\n'

# printf 是 shell builtin；密碼只透過 stdin 送給容器，不進命令列參數或日誌。
printf '%s\n%s\n%s\n' "$admin_email" "$admin_nickname" "$admin_password" | docker compose exec -T laravel php artisan app:create-initial-admin
unset admin_password
touch ".local/setup-complete-$project_name"

if [ "${1:-}" != '--backend-only' ]; then
    docker compose up -d --build --wait frontend-vue nginx
    echo 'Vue／Laravel 展示已就緒：http://localhost:8080/vue/'
fi
