#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
fixture=$(mktemp -d)
# 只移除本測試建立的副本；Docker double 不存取 daemon 或真實資料。
cleanup() { rm -rf "$fixture"; }
trap cleanup EXIT
mkdir -p "$fixture/ops" "$fixture/bin" "$fixture/.local/keys"
cp "$repo_root/ops/start.sh" "$repo_root/ops/stop.sh" "$repo_root/ops/setup.sh" "$repo_root/ops/check-keys.sh" "$fixture/ops/"
if [ -f "$repo_root/ops/service-selection.sh" ]; then cp "$repo_root/ops/service-selection.sh" "$fixture/ops/"; fi
touch "$fixture/.env" "$fixture/.local/setup-complete-rails05_test"
cat > "$fixture/bin/docker" <<'DOUBLE'
#!/bin/sh
printf '%s\n' "$*" >> "$RAILS05_DOCKER_LOG"
case "$*" in
  *'--exit-code-from sql sql'*) [ "${RAILS05_FAIL_SQL:-0}" != 1 ] ;;
  *'up -d --wait db'*) [ "${RAILS05_FAIL_DB:-0}" != 1 ] ;;
  *'up -d --build --wait frontend-'*) [ "${RAILS05_FAIL_FRONTEND:-0}" != 1 ] ;;
  *'up -d --build --wait '*) [ "${RAILS05_FAIL_HEALTH:-0}" != 1 ] ;;
esac
DOUBLE
chmod +x "$fixture/bin/docker"
export PATH="$fixture/bin:$PATH" COMPOSE_PROJECT_NAME=rails05_test
export RAILS05_DOCKER_LOG="$fixture/docker.log"
for backend in laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs; do
    (umask 077; openssl rand -base64 32 > "$fixture/.local/keys/$backend.key")
done

# 在副本真入口執行，回傳真exit並保留實際argv順序供斷言。
run_start() {
    : > "$RAILS05_DOCKER_LOG"
    "$fixture/ops/start.sh" "$@" > "$fixture/output.log" 2>&1
}
# 檢查啟動而非停止argv，避免服務只被停掉仍讓案例誤過。
assert_started() {
    if ! grep -F 'up -d --build --wait ' "$RAILS05_DOCKER_LOG" | grep -Fq "$1"; then
        echo "missing startup services: $1" >&2
        exit 1
    fi
}
# fail-closed 不能輸出就緒；參數和金鑰拒絕也必須早於Docker副作用。
assert_rejected_before_docker() {
    if run_start "$@"; then echo "unexpected accepted arguments/state: $*" >&2; exit 1; fi
    test ! -s "$RAILS05_DOCKER_LOG"
}

# 舊十二金鑰齊全時只補Rails；一般入口必須等待13+6及nginx。
mv "$fixture/.local/keys/laravel.key" "$fixture/laravel.saved"
assert_rejected_before_docker
test ! -e "$fixture/.local/keys/rails.key"
test ! -e "$fixture/.local/keys/.rails-initialized"
mv "$fixture/laravel.saved" "$fixture/.local/keys/laravel.key"
cksum "$fixture"/.local/keys/*.key > "$fixture/old-keys.before"
run_start
assert_started 'nestjs rails frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
grep -Fq '十三後端' "$fixture/output.log"
grep -Fq 'http://localhost:8093' "$fixture/output.log"
for backend in laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs; do cksum "$fixture/.local/keys/$backend.key"; done > "$fixture/old-keys.after"
# glob與明列順序不同，排序後核對內容完整保留。
sort "$fixture/old-keys.before" > "$fixture/before.sorted"
sort "$fixture/old-keys.after" > "$fixture/after.sorted"
cmp "$fixture/before.sorted" "$fixture/after.sorted"
test -f "$fixture/.local/keys/.rails-initialized"
cp "$fixture/.local/keys/rails.key" "$fixture/rails.original"
run_start
cmp "$fixture/rails.original" "$fixture/.local/keys/rails.key"

# SQL前的停寫包含Rails；DB→SQL→應用順序不可顛倒。
sed -n '1p' "$RAILS05_DOCKER_LOG" | grep -F ' stop ' | grep -Fq 'nestjs rails frontend-vue'
sed -n '2p' "$RAILS05_DOCKER_LOG" | grep -Fq 'up -d --wait db'
sed -n '3p' "$RAILS05_DOCKER_LOG" | grep -Fq -- '--exit-code-from sql sql'
sed -n '4p' "$RAILS05_DOCKER_LOG" | grep -Fq 'up -d --build --wait '

# recovery必須停止Rails且僅等12+6；backend-only與旗標順序均可組合。
run_start --without-rails
assert_started 'nestjs frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
if grep -F 'up -d --build --wait ' "$RAILS05_DOCKER_LOG" | grep -Eq '(^| )rails( |$)'; then exit 1; fi
grep -Fq '十二後端模式' "$fixture/output.log"
run_start --backend-only --without-rails
assert_started 'nestjs'
if grep -F 'up -d --build --wait ' "$RAILS05_DOCKER_LOG" | grep -Eq 'frontend-|(^| )rails( |$)'; then exit 1; fi
run_start --without-rails --backend-only
assert_started 'nestjs'

# 明確fixture選擇：新05不會掉回舊3+Vue，舊12票維持原scope。
COMPOSE_FILE=compose.yaml:ops/tests/rails05_compose.yaml run_start
assert_started 'nestjs rails frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
COMPOSE_FILE=compose.yaml:.local/rails05-compose.yaml run_start
assert_started 'nestjs rails frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
COMPOSE_FILE=compose.yaml:ops/tests/ticket12_compose.yaml run_start
assert_started 'laravel symfony aspnet-core frontend-vue nginx'
if grep -F 'up -d --build --wait ' "$RAILS05_DOCKER_LOG" | grep -Eq 'frontend-react|(^| )rails( |$)'; then exit 1; fi
COMPOSE_FILE=compose.yaml:ops/tests/cq34_compose.yaml run_start
assert_started 'nestjs frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
if grep -F 'up -d --build --wait ' "$RAILS05_DOCKER_LOG" | grep -Eq '(^| )rails( |$)'; then exit 1; fi
unset COMPOSE_FILE

# 未知或重複旗標不可藉入口誤啟動任何服務。
assert_rejected_before_docker --unknown
assert_rejected_before_docker --without-rails --without-rails
assert_rejected_before_docker --backend-only --backend-only
assert_rejected_before_docker --backend-only unexpected

# 每個必要階段失敗皆回非零、不輸出成功，SQL失敗後更不能啟動應用。
for failure in RAILS05_FAIL_DB RAILS05_FAIL_SQL RAILS05_FAIL_HEALTH; do
    if (export "$failure=1"; run_start); then echo "unexpected success: $failure" >&2; exit 1; fi
    if grep -Fq '已就緒' "$fixture/output.log"; then echo "false ready: $failure" >&2; exit 1; fi
    if [ "$failure" != RAILS05_FAIL_HEALTH ] && grep -Fq 'up -d --build --wait ' "$RAILS05_DOCKER_LOG"; then exit 1; fi
done

# 純檢查不產金鑰；已完成標記下遺失、毀損、不正規base64或錯權限均拒絕覆寫。
rm "$fixture/.local/keys/rails.key"
assert_rejected_before_docker
test ! -e "$fixture/.local/keys/rails.key"
cp "$fixture/rails.original" "$fixture/.local/keys/rails.key"
printf 'broken\n' > "$fixture/.local/keys/rails.key"
assert_rejected_before_docker
grep -Fxq broken "$fixture/.local/keys/rails.key"
cp "$fixture/rails.original" "$fixture/.local/keys/rails.key"
chmod 644 "$fixture/.local/keys/rails.key"
assert_rejected_before_docker
chmod 600 "$fixture/.local/keys/rails.key"
printf '\n' >> "$fixture/.local/keys/rails.key"
assert_rejected_before_docker
cp "$fixture/rails.original" "$fixture/.local/keys/rails.key"
cp "$fixture/.local/keys/laravel.key" "$fixture/.local/keys/rails.key"
assert_rejected_before_docker
cp "$fixture/rails.original" "$fixture/.local/keys/rails.key"
printf 'bad marker\n' > "$fixture/.local/keys/.rails-initialized"
assert_rejected_before_docker
printf 'rails-jwt-v1\n' > "$fixture/.local/keys/.rails-initialized"
chmod 600 "$fixture/.local/keys/.rails-initialized"

# 十二份不完整時不得先寫Rails或marker；已初始化損毀不能以recovery跳過。
cp "$fixture/.local/keys/laravel.key" "$fixture/laravel.original"
rm "$fixture/.local/keys/laravel.key"
assert_rejected_before_docker --without-rails
cp "$fixture/laravel.original" "$fixture/.local/keys/laravel.key"
chmod 600 "$fixture/.local/keys/laravel.key"

# 既有Rails key尚未有marker時只能採用原值；初次rand失敗要留下failclosed狀態。
rm "$fixture/.local/keys/.rails-initialized"
run_start
cmp "$fixture/rails.original" "$fixture/.local/keys/rails.key"
rm "$fixture/.local/keys/.rails-initialized" "$fixture/.local/keys/rails.key"
if (cd "$fixture"; ops/check-keys.sh); then echo 'plain check unexpectedly accepted missing Rails key' >&2; exit 1; fi
test ! -e "$fixture/.local/keys/rails.key"
real_openssl=$(command -v openssl)
cat > "$fixture/bin/openssl" <<DOUBLE
#!/bin/sh
case "\$*" in 'rand -base64 32') exit 71 ;; esac
exec '$real_openssl' "\$@"
DOUBLE
chmod +x "$fixture/bin/openssl"
assert_rejected_before_docker
test -f "$fixture/.local/keys/.rails-initialized"
test ! -e "$fixture/.local/keys/rails.key"
rm "$fixture/bin/openssl"
assert_rejected_before_docker
cp "$fixture/rails.original" "$fixture/.local/keys/rails.key"
chmod 600 "$fixture/.local/keys/rails.key"

# stop會納入Rails但保留volume；setup首次正常產生13份獨立0600key。
: > "$RAILS05_DOCKER_LOG"
"$fixture/ops/stop.sh"
grep -Fq -- '--profile rails down' "$RAILS05_DOCKER_LOG"
if grep -Fq -- 'down -v' "$RAILS05_DOCKER_LOG"; then exit 1; fi
rm -rf "$fixture/.local/keys"
rm "$fixture/.env"
: > "$RAILS05_DOCKER_LOG"
printf 'admin@example.test\n管理員\nPass1234\n' | "$fixture/ops/setup.sh" > "$fixture/setup.log" 2>&1
assert_started 'nestjs rails'
test "$(find "$fixture/.local/keys" -name '*.key' | wc -l | tr -d ' ')" = 13
(cd "$fixture"; ops/check-keys.sh)
test "$(cat "$fixture"/.local/keys/*.key | sort -u | wc -l | tr -d ' ')" = 13
cksum "$fixture"/.local/keys/*.key > "$fixture/all.before"
run_start
cksum "$fixture"/.local/keys/*.key > "$fixture/all.after"
cmp "$fixture/all.before" "$fixture/all.after"

# 正規base64也可沒有檔尾換行；逐檔比較獨立性不能把十三份串成一行誤拒絕。
for key_file in "$fixture"/.local/keys/*.key; do
    (umask 077; tr -d '\n' < "$key_file" > "$fixture/key-no-newline")
    mv "$fixture/key-no-newline" "$key_file"
done
cksum "$fixture"/.local/keys/*.key > "$fixture/no-newline.before"
run_start
cksum "$fixture"/.local/keys/*.key > "$fixture/no-newline.after"
cmp "$fixture/no-newline.before" "$fixture/no-newline.after"
# 後續setup失敗案例也須保留這組原始bytes。
cp "$fixture/no-newline.before" "$fixture/all.before"

# 管理員已建立但前端不健康時仍未完成首次設定，必須停止含Rails全部應用。
: > "$RAILS05_DOCKER_LOG"
if printf 'admin@example.test\n管理員\nPass1234\n' | RAILS05_FAIL_FRONTEND=1 COMPOSE_PROJECT_NAME=rails05_setup_frontend_fail "$fixture/ops/setup.sh" > "$fixture/setup-failure.log" 2>&1; then
    echo 'setup unexpectedly accepted unhealthy frontend' >&2
    exit 1
fi
test ! -e "$fixture/.local/setup-complete-rails05_setup_frontend_fail"
tail -n 1 "$RAILS05_DOCKER_LOG" | grep -F ' stop ' | grep -Fq 'nestjs rails frontend-vue'
cksum "$fixture"/.local/keys/*.key > "$fixture/all.after"
cmp "$fixture/all.before" "$fixture/all.after"
echo 'Rails05 simulated start/recovery/key lifecycle PASS (no Docker daemon)'
