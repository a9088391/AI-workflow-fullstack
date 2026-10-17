#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
fixture=$(mktemp -d)

# 只刪除本腳本建立的暫存目錄；不碰展示服務、資料卷或金鑰。
cleanup() { rm -rf "$fixture"; }
trap cleanup EXIT
mkdir -p "$fixture/ops" "$fixture/bin" "$fixture/.local"
cp "$repo_root/ops/start.sh" "$repo_root/ops/stop.sh" "$repo_root/ops/setup.sh" "$fixture/ops/"
touch "$fixture/.env"
cat > "$fixture/ops/check-keys.sh" <<'EOF'
#!/bin/sh
exit 0
EOF
cat > "$fixture/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$TICKET45_DOCKER_LOG"
case "$*" in
  *' exec -T laravel '*) [ "${TICKET45_FAIL_EXEC:-0}" != '1' ] ;;
esac
EOF
chmod +x "$fixture/ops/check-keys.sh" "$fixture/bin/docker"
export PATH="$fixture/bin:$PATH"
export TICKET45_DOCKER_LOG="$fixture/docker.log"
export SETUP_IN_PROGRESS=1

# 執行原始腳本的隔離副本，以實際命令順序驗證服務選取而不啟動 Docker。
run_start() {
  : > "$TICKET45_DOCKER_LOG"
  "$fixture/ops/start.sh" "$@" > "$fixture/output.log"
}

# 只比對指定的啟動命令，避免停止階段出現 React 名稱造成假陽性。
assert_up_contains() {
  if ! grep -F "up -d --build --wait" "$TICKET45_DOCKER_LOG" | grep -Fq "$1"; then
    echo "missing startup service: $1" >&2
    exit 1
  fi
}

# 正常入口需啟動已完成前端，且 SQL 更新前先停止舊前端服務。
run_start
assert_up_contains 'frontend-vue frontend-nuxt frontend-react frontend-next nginx'
grep -Fq 'frontend-next nginx' "$TICKET45_DOCKER_LOG"
grep -Fq 'http://localhost:8080/react/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/next/' "$fixture/output.log"

# backend-only 只啟動後端，舊票隔離 fixture 仍維持原 Vue 啟動範圍。
run_start --backend-only
if grep -F 'up -d --build --wait' "$TICKET45_DOCKER_LOG" | grep -Eq 'frontend-(react|next|nuxt)'; then
  echo 'backend-only unexpectedly started a frontend' >&2
  exit 1
fi
COMPOSE_FILE=compose.yaml:ops/tests/ticket12_compose.yaml run_start
assert_up_contains 'frontend-vue nginx'
if grep -F 'up -d --build --wait' "$TICKET45_DOCKER_LOG" | grep -Eq 'frontend-(react|next|nuxt)'; then
  echo 'old fixture unexpectedly started an additional frontend' >&2
  exit 1
fi
unset COMPOSE_FILE

# 停止腳本使用 Compose down，保留 volume，前端也在同一專案生命週期內。
: > "$TICKET45_DOCKER_LOG"
"$fixture/ops/stop.sh"
grep -Fq ' down' "$TICKET45_DOCKER_LOG"
if grep -Fq 'down -v' "$TICKET45_DOCKER_LOG"; then
  echo 'stop unexpectedly removed volumes' >&2
  exit 1
fi

# 管理員建立失敗時，首次設定應停止前端，保留原有資料及金鑰。
: > "$TICKET45_DOCKER_LOG"
mkdir -p "$fixture/.local/keys"
printf 'existing key\n' > "$fixture/.local/keys/existing.key"
if printf 'admin@example.test\n管理員\nPass1234\n' | TICKET45_FAIL_EXEC=1 COMPOSE_PROJECT_NAME=ticket45-setup-failure "$fixture/ops/setup.sh" > "$fixture/output.log" 2>&1; then
  echo 'setup unexpectedly accepted failed admin creation' >&2
  exit 1
fi
grep -F ' stop ' "$TICKET45_DOCKER_LOG" | tail -n 1 | grep -Fq 'frontend-vue frontend-nuxt frontend-react frontend-next nginx'
test ! -e "$fixture/.local/setup-complete-ticket45-setup-failure"
test -f "$fixture/.env"
grep -Fxq 'existing key' "$fixture/.local/keys/existing.key"

# 成功首次設定應啟動已完成前端，且只宣告 Compose 實際綁定的 8080 入口。
: > "$TICKET45_DOCKER_LOG"
printf 'admin@example.test\n管理員\nPass1234\n' | COMPOSE_PROJECT_NAME=ticket45-setup-success "$fixture/ops/setup.sh" > "$fixture/output.log"
assert_up_contains 'frontend-vue frontend-nuxt frontend-react frontend-next nginx'
test -f "$fixture/.local/setup-complete-ticket45-setup-success"
grep -Fq 'http://localhost:8080/vue/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/react/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/next/' "$fixture/output.log"

# 舊票隔離 Compose 僅有 Vue，首次設定不得要求它啟動 React。
: > "$TICKET45_DOCKER_LOG"
printf 'admin@example.test\n管理員\nPass1234\n' | COMPOSE_FILE=compose.yaml:ops/tests/ticket12_compose.yaml COMPOSE_PROJECT_NAME=ticket45-setup-legacy "$fixture/ops/setup.sh" > "$fixture/output.log"
if grep -F 'up -d --build --wait' "$TICKET45_DOCKER_LOG" | grep -Eq 'frontend-(react|next|nuxt)'; then
  echo 'old setup fixture unexpectedly started an additional frontend' >&2
  exit 1
fi
grep -Fq 'http://localhost:8080/vue/' "$fixture/output.log"
if grep -Fq 'http://localhost:8080/react/' "$fixture/output.log"; then
  echo 'old setup fixture unexpectedly advertised React' >&2
  exit 1
fi
if grep -Fq 'http://localhost:8080/next/' "$fixture/output.log"; then
  echo 'old setup fixture unexpectedly advertised Next.js' >&2
  exit 1
fi
echo 'ticket45 start/stop/setup service selection PASS'
