#!/bin/sh
set -eu

cd "$(dirname "$0")/../.."
project_name="${SQL_TEST_PROJECT:-ticket01_sql_$(date +%s)}"
case "$project_name" in
    ticket01_sql_*) ;;
    *) echo 'SQL_TEST_PROJECT 必須使用 ticket01_sql_ 前綴' >&2; exit 1 ;;
esac
# 專案識別同時用於檔案路徑；先限縮為 Compose 名稱字元，避免輸入變成其他目錄。
case "$project_name" in
    *[!a-z0-9_-]*) echo 'SQL_TEST_PROJECT 只允許小寫英數、底線與連字號' >&2; exit 1 ;;
esac
work="$(pwd)/.local/$project_name"
# 已保留的 SQL、備份與診斷即使沒有資料卷也不能覆寫；新測試須使用全新名稱。
if [ -e "$work" ] || [ -L "$work" ]; then
    echo '測試工作目錄已存在；請換全新 SQL_TEST_PROJECT，避免覆寫' >&2
    exit 1
fi
if docker volume inspect "${project_name}_postgres_data" >/dev/null 2>&1; then
    echo '測試專案已有資料；請換全新 SQL_TEST_PROJECT，避免覆寫' >&2
    exit 1
fi
export COMPOSE_PROJECT_NAME="$project_name"
mkdir -p "$work/sql" "$work/backups" "$work/bin"
cp database/sql/0001_accounts.sql "$work/sql/0001_accounts.sql"
cat > "$work/override.yaml" <<EOF
services:
  sql:
    volumes:
      - type: bind
        source: $work/sql
        target: /srv/database/sql
        read_only: true
      - type: bind
        source: $work/backups
        target: /backups
      - type: bind
        source: $work/bin
        target: /srv/testbin
        read_only: true
EOF

# 所有操作鎖在新的 Compose project 與測試 SQL 目錄，不碰正式資料及已套用的 0001。
compose() {
    docker compose -f compose.yaml -f "$work/override.yaml" "$@"
}

# 只停止本次測試容器；保留 volume 與日誌供失敗追查，不自動清資料。
cleanup() {
    compose down >/dev/null 2>&1 || true
}
trap cleanup EXIT

compose up -d --wait db > "$work/db.log" 2>&1
compose run --rm sql > "$work/initial.log" 2>&1
test "$(compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT count(*) FROM schema_migrations')" = '1'
test -z "$(find "$work/backups" -name '*.dump' -print -quit)"

# 管理員 stdin 只應移除一個傳輸 LF；尾 CR 的密碼和 Email 不可被默默改成有效值。
if printf 'admin-password-%s@example.test\nAdmin\n%s\r\n' "$(openssl rand -hex 4)" "$(openssl rand -hex 4)" | compose run --rm -T --no-deps laravel php artisan app:create-initial-admin > "$work/admin-password-cr.log" 2>&1; then
    echo '預期尾 CR 密碼拒絕，管理員卻建立成功' >&2
    exit 1
fi
grep -Fq '管理員 Email、暱稱或密碼不符合共同格式' "$work/admin-password-cr.log"
test "$(compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT count(*) FROM accounts')" = '0'
if printf 'admin-email-%s@example.test\r\nAdmin\n%s\n' "$(openssl rand -hex 4)" "$(openssl rand -hex 4)" | compose run --rm -T --no-deps laravel php artisan app:create-initial-admin > "$work/admin-email-cr.log" 2>&1; then
    echo '預期尾 CR Email 拒絕，管理員卻建立成功' >&2
    exit 1
fi
grep -Fq '管理員 Email、暱稱或密碼不符合共同格式' "$work/admin-email-cr.log"
test "$(compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT count(*) FROM accounts')" = '0'

# 暱稱尾 CR 有其他可見字元時仍合法，且資料庫應保存原始 CR。
printf 'admin-nickname-%s@example.test\nAdmin\r\n%s\n' "$(openssl rand -hex 4)" "$(openssl rand -hex 4)" | compose run --rm -T --no-deps laravel php artisan app:create-initial-admin > "$work/admin-nickname-cr.log" 2>&1
test "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT encode(convert_to(nickname, 'UTF8'), 'hex') FROM accounts WHERE role = 'admin'")" = '41646d696e0d'

# PDO 原始診斷與檔案讀取 warning 不可攜帶測試 marker 流入啟動輸出。
if compose run --rm -e DB_PORT=RAW_DB_SECRET_MARKER sql > "$work/raw-db-fail.log" 2>&1; then
    echo '預期資料庫連線失敗，入口卻成功' >&2
    exit 1
fi
grep -Fq '資料庫連線失敗' "$work/raw-db-fail.log"
if grep -Fq 'RAW_DB_SECRET_MARKER' "$work/raw-db-fail.log"; then
    echo 'PDO 原始診斷洩漏至啟動輸出' >&2
    exit 1
fi
ln -s /RAW_READ_SECRET_MARKER "$work/sql/0002_read_marker.sql"
if compose run --rm sql > "$work/raw-read-fail.log" 2>&1; then
    echo '預期版本檔讀取失敗，入口卻成功' >&2
    exit 1
fi
grep -Fq 'SQL 版本檔讀取失敗：0002_read_marker' "$work/raw-read-fail.log"
if grep -Fq 'RAW_READ_SECRET_MARKER' "$work/raw-read-fail.log"; then
    echo '檔案讀取原始警告洩漏至啟動輸出' >&2
    exit 1
fi
mv "$work/sql/0002_read_marker.sql" "$work/0002_read_marker.sql"

# 待套用新版本時，備份路徑失敗必須早於任何 SQL 寫入。
cat > "$work/sql/0002_lock.sql" <<'EOF'
SELECT pg_sleep(2);
CREATE TABLE sql_lock_probe (id integer);
CREATE TABLE sql_timezone_probe AS SELECT current_setting('TimeZone') AS name;
EOF
if compose run --rm -e PROJECT_WRITERS_STOPPED=1 -e BACKUP_DIR=/dev/null sql > "$work/backup-fail.log" 2>&1; then
    echo '預期備份失敗，SQL 卻成功' >&2
    exit 1
fi
grep -Fq '備份目錄不可寫' "$work/backup-fail.log"
test "$(compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT count(*) FROM schema_migrations')" = '1'

# 兩個入口競爭同一 advisory lock，只有一個能套用 0002，另一個查到已套用。
compose run --rm -e PROJECT_WRITERS_STOPPED=1 sql > "$work/lock-a.log" 2>&1 &
first=$!
compose run --rm -e PROJECT_WRITERS_STOPPED=1 sql > "$work/lock-b.log" 2>&1 &
second=$!
wait "$first"
wait "$second"
test "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT count(*) FROM schema_migrations WHERE version='0002_lock'")" = '1'
test "$(compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT name FROM sql_timezone_probe')" = 'Asia/Taipei'
test -n "$(find "$work/backups" -name '*.dump' -print -quit)"

# 使用實際備份還原至同 project 的新資料庫，確認既有 app 角色仍可讀、寫及取 identity 序號。
backup_name="$(find "$work/backups" -name '*.dump' -print -quit | xargs basename)"
compose exec -T db createdb -U articles_owner restore_probe > "$work/restore-create.log" 2>&1
compose run --rm -T --no-deps -e TEST_BACKUP_FILE="$backup_name" sql sh -c 'PGPASSWORD="$DB_OWNER_PASSWORD" pg_restore --host=db --username=articles_owner --dbname=restore_probe "/backups/$TEST_BACKUP_FILE"' > "$work/restore.log" 2>&1
test "$(compose exec -T db psql -U articles_owner -d restore_probe -Atc "SELECT has_table_privilege('articles_app', 'public.accounts', 'SELECT'), has_table_privilege('articles_app', 'public.accounts', 'INSERT'), has_sequence_privilege('articles_app', 'public.accounts_id_seq', 'USAGE')")" = 't|t|t'
compose exec -T db psql -v ON_ERROR_STOP=1 -U articles_owner -d restore_probe -c "BEGIN; SET ROLE articles_app; SELECT count(*) FROM accounts; INSERT INTO accounts(email, password_hash, nickname, role, status) VALUES ('restore-check@example.test', 'test-only', '還原測試', 'member', 'active') RETURNING id; ROLLBACK;" > "$work/restore-role.log" 2>&1

# 等 runner 已排在 advisory lock 後才原子替換檔案，版本 checksum 必須對應實際執行的內容。
cat > "$work/sql/0003_consistency.sql" <<'EOF'
CREATE TABLE sql_consistency_probe (value text);
INSERT INTO sql_consistency_probe (value) VALUES ('before');
EOF
compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT pg_advisory_lock(1907202601); SELECT pg_sleep(15)' > "$work/held-lock.log" 2>&1 &
holder=$!
attempt=0
until [ "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted")" -ge 1 ]; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 30 || { echo '未取得測試用 advisory lock' >&2; exit 1; }
    sleep 0.2
done
compose run --rm -e PROJECT_WRITERS_STOPPED=1 sql > "$work/consistency-run.log" 2>&1 &
runner=$!
attempt=0
until [ "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")" -ge 1 ]; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 30 || { echo 'SQL runner 未進入鎖定等待' >&2; exit 1; }
    sleep 0.2
done
cat > "$work/0003.after" <<'EOF'
CREATE TABLE sql_consistency_probe (value text);
INSERT INTO sql_consistency_probe (value) VALUES ('after');
EOF
mv "$work/0003.after" "$work/sql/0003_consistency.sql"
wait "$holder"
wait "$runner"
test "$(compose exec -T db psql -U articles_owner -d articles -Atc 'SELECT value FROM sql_consistency_probe')" = 'after'
recorded_checksum="$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT trim(checksum) FROM schema_migrations WHERE version = '0003_consistency'")"
actual_checksum="$(shasum -a 256 "$work/sql/0003_consistency.sql" | awk '{print $1}')"
test "$recorded_checksum" = "$actual_checksum" || { echo '執行 SQL 與已記錄 checksum 不一致' >&2; exit 1; }

# pg_dump 大量原始 stderr 不可塞滿 pipe 或流入啟動輸出；失敗必須阻止新版本。
cat > "$work/sql/0004_backup_probe.sql" <<'EOF'
CREATE TABLE sql_backup_probe (id integer);
EOF
cat > "$work/bin/pg_dump" <<'EOF'
#!/bin/sh
echo 'RAW_PGDUMP_SECRET_MARKER' >&2
head -c 262144 /dev/zero >&2
exit 42
EOF
chmod +x "$work/bin/pg_dump"
if compose run --rm -e PROJECT_WRITERS_STOPPED=1 -e PATH=/srv/testbin:/usr/local/bin:/usr/bin:/bin sql > "$work/pgdump-stderr-fail.log" 2>&1; then
    echo '預期 pg_dump 失敗，入口卻成功' >&2
    exit 1
fi
grep -Fq 'pg_dump 備份失敗' "$work/pgdump-stderr-fail.log"
if grep -Fq 'RAW_PGDUMP_SECRET_MARKER' "$work/pgdump-stderr-fail.log"; then
    echo 'pg_dump 原始 stderr 洩漏至啟動輸出' >&2
    exit 1
fi
test "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT count(*) FROM schema_migrations WHERE version = '0004_backup_probe'")" = '0'
mv "$work/sql/0004_backup_probe.sql" "$work/0004_backup_probe.sql"

# 一支 SQL 內先建表再出錯；表與版本紀錄須在同一交易回滾。
cat > "$work/sql/0003_rollback.sql" <<'EOF'
CREATE TABLE rollback_probe (id integer);
SELECT 1 / 0;
EOF
if compose run --rm -e PROJECT_WRITERS_STOPPED=1 sql > "$work/rollback-fail.log" 2>&1; then
    echo '預期 SQL 失敗，入口卻成功' >&2
    exit 1
fi
grep -Fq 'SQL 套用失敗：0003_rollback' "$work/rollback-fail.log"
test "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT CASE WHEN to_regclass('public.rollback_probe') IS NULL THEN 'absent' ELSE 'present' END")" = 'absent'
test "$(compose exec -T db psql -U articles_owner -d articles -Atc "SELECT count(*) FROM schema_migrations WHERE version='0003_rollback'")" = '0'

# 已成功版本內容不可改；未知版本也應在備份或新 SQL 前停止。
cp "$work/sql/0002_lock.sql" "$work/0002.original"
printf '%s\n' '-- checksum test' >> "$work/sql/0002_lock.sql"
if compose run --rm -e PROJECT_WRITERS_STOPPED=1 sql > "$work/checksum-fail.log" 2>&1; then
    echo '預期 checksum 不符，入口卻成功' >&2
    exit 1
fi
grep -Fq 'SQL checksum 不符：0002_lock' "$work/checksum-fail.log"
cp "$work/0002.original" "$work/sql/0002_lock.sql"
compose exec -T db psql -U articles_owner -d articles -c "INSERT INTO schema_migrations(version, checksum) VALUES ('9999_unknown', repeat('0', 64))" > "$work/unknown-insert.log" 2>&1
if compose run --rm -e PROJECT_WRITERS_STOPPED=1 sql > "$work/unknown-fail.log" 2>&1; then
    echo '預期未知版本停止，入口卻成功' >&2
    exit 1
fi
grep -Fq '未知資料庫版本：9999_unknown' "$work/unknown-fail.log"

printf 'SQL entry checks passed; project=%s evidence=%s\n' "$project_name" "$work"
