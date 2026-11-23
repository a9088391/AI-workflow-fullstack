#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
fixture=$(mktemp -d)

# 只刪除本腳本建立的暫存目錄；不碰展示服務、資料卷或金鑰。
cleanup() { rm -rf "$fixture"; }
trap cleanup EXIT
mkdir -p "$fixture/ops/tests" "$fixture/bin" "$fixture/.local" "$fixture/database/sql"
cp "$repo_root/ops/start.sh" "$repo_root/ops/stop.sh" "$repo_root/ops/setup.sh" "$repo_root/ops/service-selection.sh" "$fixture/ops/"
cp "$repo_root/ops/tests/sql_entry.sh" "$fixture/ops/tests/"
cp "$repo_root/database/sql/0001_accounts.sql" "$fixture/database/sql/"
touch "$fixture/.env"
cat > "$fixture/ops/check-keys.sh" <<'EOF'
#!/bin/sh
[ "${TICKET45_FAIL_KEYS:-0}" != '1' ]
EOF
cat > "$fixture/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$TICKET45_DOCKER_LOG"
case "$*" in
  *' exec -T laravel '*) [ "${TICKET45_FAIL_EXEC:-0}" != '1' ] ;;
  *'--exit-code-from sql sql'*) [ "${TICKET45_FAIL_SQL:-0}" != '1' ] ;;
  'volume inspect '*) exit 1 ;;
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
assert_up_contains 'frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
grep -Fq 'frontend-angular frontend-sveltekit nginx' "$TICKET45_DOCKER_LOG"
grep -Fq 'http://localhost:8080/react/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/next/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/angular/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/sveltekit/' "$fixture/output.log"

# backend-only 只啟動後端，舊票隔離 fixture 仍維持原 Vue 啟動範圍。
run_start --backend-only
if grep -F 'up -d --build --wait' "$TICKET45_DOCKER_LOG" | grep -Fq 'frontend-'; then
  echo 'backend-only unexpectedly started a frontend' >&2
  exit 1
fi
COMPOSE_FILE=compose.yaml:ops/tests/ticket12_compose.yaml run_start
assert_up_contains 'frontend-vue nginx'
if grep -F 'up -d --build --wait' "$TICKET45_DOCKER_LOG" | grep -Eq 'frontend-(react|next|nuxt|angular|sveltekit)'; then
  echo 'old fixture unexpectedly started an additional frontend' >&2
  exit 1
fi
unset COMPOSE_FILE

# 必要設定與首次標記被拒絕時，不可先呼叫 Docker 或啟動部分服務。
: > "$TICKET45_DOCKER_LOG"
mv "$fixture/.env" "$fixture/.env.saved"
if "$fixture/ops/start.sh" > "$fixture/output.log" 2>&1; then
  echo 'start unexpectedly accepted missing .env' >&2
  exit 1
fi
mv "$fixture/.env.saved" "$fixture/.env"
test ! -s "$TICKET45_DOCKER_LOG"
if TICKET45_FAIL_KEYS=1 "$fixture/ops/start.sh" > "$fixture/output.log" 2>&1; then
  echo 'start unexpectedly accepted invalid keys' >&2
  exit 1
fi
test ! -s "$TICKET45_DOCKER_LOG"
if SETUP_IN_PROGRESS=0 COMPOSE_PROJECT_NAME=ticket45-incomplete "$fixture/ops/start.sh" > "$fixture/output.log" 2>&1; then
  echo 'start unexpectedly accepted incomplete setup' >&2
  exit 1
fi
test ! -s "$TICKET45_DOCKER_LOG"

# SQL 非零後保留停止狀態，不可繼續送出後端或前端的啟動命令。
if (TICKET45_FAIL_SQL=1 run_start); then
  echo 'start unexpectedly accepted failed SQL' >&2
  exit 1
fi
if grep -Fq 'up -d --build --wait' "$TICKET45_DOCKER_LOG"; then
  echo 'SQL failure unexpectedly started application services' >&2
  exit 1
fi

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
grep -F ' stop ' "$TICKET45_DOCKER_LOG" | tail -n 1 | grep -Fq 'frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
test ! -e "$fixture/.local/setup-complete-ticket45-setup-failure"
test -f "$fixture/.env"
grep -Fxq 'existing key' "$fixture/.local/keys/existing.key"

# 首次輸入 EOF 與管理員拒絕同樣走 cleanup，不能留下未完成應用或完成標記。
: > "$TICKET45_DOCKER_LOG"
if COMPOSE_PROJECT_NAME=ticket45-setup-abort "$fixture/ops/setup.sh" < /dev/null > "$fixture/output.log" 2>&1; then
  echo 'setup unexpectedly accepted input EOF' >&2
  exit 1
fi
grep -F ' stop ' "$TICKET45_DOCKER_LOG" | tail -n 1 | grep -Fq 'frontend-angular frontend-sveltekit nginx'
test ! -e "$fixture/.local/setup-complete-ticket45-setup-abort"

# 成功首次設定應啟動已完成前端，且只宣告 Compose 實際綁定的 8080 入口。
: > "$TICKET45_DOCKER_LOG"
printf 'admin@example.test\n管理員\nPass1234\n' | COMPOSE_PROJECT_NAME=ticket45-setup-success "$fixture/ops/setup.sh" > "$fixture/output.log"
assert_up_contains 'frontend-vue frontend-nuxt frontend-react frontend-next frontend-angular frontend-sveltekit nginx'
test -f "$fixture/.local/setup-complete-ticket45-setup-success"
grep -Fq 'http://localhost:8080/vue/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/react/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/next/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/angular/' "$fixture/output.log"
grep -Fq 'http://localhost:8080/sveltekit/' "$fixture/output.log"

# 舊票隔離 Compose 僅有 Vue，首次設定不得要求它啟動 React。
: > "$TICKET45_DOCKER_LOG"
printf 'admin@example.test\n管理員\nPass1234\n' | COMPOSE_FILE=compose.yaml:ops/tests/ticket12_compose.yaml COMPOSE_PROJECT_NAME=ticket45-setup-legacy "$fixture/ops/setup.sh" > "$fixture/output.log"
if grep -F 'up -d --build --wait' "$TICKET45_DOCKER_LOG" | grep -Eq 'frontend-(react|next|nuxt|angular|sveltekit)'; then
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

# SQL 專案名稱不能變成路徑；拒絕必須早於 Docker 及任何測試工作目錄寫入。
: > "$TICKET45_DOCKER_LOG"
if SQL_TEST_PROJECT=ticket01_sql_cq32/escape "$fixture/ops/tests/sql_entry.sh" > "$fixture/output.log" 2>&1; then
  echo 'SQL entry unexpectedly accepted a project path' >&2
  exit 1
fi
if [ -s "$TICKET45_DOCKER_LOG" ] || [ -e "$fixture/.local/ticket01_sql_cq32/escape" ]; then
  echo 'invalid SQL project reached Docker or created a work directory' >&2
  exit 1
fi

# 即使資料卷已不在，也不可覆寫先前保留的測試 SQL／診斷目錄。
work="$fixture/.local/ticket01_sql_cq32_preserve"
mkdir -p "$work/sql"
printf 'prior diagnostic sentinel\n' > "$work/sql/0001_accounts.sql"
if SQL_TEST_PROJECT=ticket01_sql_cq32_preserve "$fixture/ops/tests/sql_entry.sh" > "$fixture/output.log" 2>&1; then
  echo 'SQL entry unexpectedly reused an existing work directory' >&2
  exit 1
fi
grep -Fxq 'prior diagnostic sentinel' "$work/sql/0001_accounts.sql"
test ! -s "$TICKET45_DOCKER_LOG"

echo 'ticket45 start/stop/setup and SQL input safety PASS'
