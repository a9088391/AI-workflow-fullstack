#!/bin/sh
set -eu
umask 077

case "$#:${1:-}" in
    0:) initialize_rails=0 ;;
    1:--initialize-rails) initialize_rails=1 ;;
    *) echo '用法：ops/check-keys.sh [--initialize-rails]' >&2; exit 2 ;;
esac

# macOS/Linux使用各自stat格式；只讀權限，不替既有敏感檔案悄悄修正。
private_file() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    file_mode=$(stat -f '%Lp' "$1" 2>/dev/null) || file_mode=$(stat -c '%a' "$1" 2>/dev/null) || return 1
    [ "$file_mode" = 600 ]
}

# 長度與重新編碼一起查核，拒絕解碼器可能寬鬆接受的雜字元／不正規base64。
valid_key() {
    private_file "$1" || return 1
    key_text=$(cat "$1")
    [ "${#key_text}" = 44 ] || return 1
    case "$(wc -c < "$1" | tr -d ' ')" in 44|45) ;; *) return 1 ;; esac
    [ "$(openssl base64 -d -A -in "$1" 2>/dev/null | wc -c | tr -d ' ')" = 32 ] || return 1
    encoded=$(openssl base64 -d -A -in "$1" 2>/dev/null | openssl base64 -A 2>/dev/null) || return 1
    [ "$encoded" = "$key_text" ]
}

# 原十二份必須先完整；不因升級Rails而重生既有簽章key。
for backend in laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs; do
    if ! valid_key ".local/keys/$backend.key"; then
        echo "金鑰缺漏、毀損或非0600：${backend}；拒絕覆寫" >&2
        exit 1
    fi
done

marker=.local/keys/.rails-initialized
rails_key=.local/keys/rails.key
if [ "$initialize_rails" = 1 ] && [ ! -e "$marker" ] && [ ! -L "$marker" ]; then
    # 已存在Rails key僅驗證並採用；錯誤／目錄／symlink不能被初始化覆蓋。
    if [ -e "$rails_key" ] || [ -L "$rails_key" ]; then
        if ! valid_key "$rails_key"; then
            echo 'Rails 金鑰無效；拒絕覆寫或建立初始化標記' >&2
            exit 1
        fi
    fi
    # 先以noclobber留下意圖：後續失敗會停在需人工查核的狀態，不能再隨機換key。
    (set -C; printf 'rails-jwt-v1\n' > "$marker") || exit 1
    if [ ! -e "$rails_key" ]; then
        pending=$(mktemp .local/keys/.rails-pending.XXXXXX)
        if ! openssl rand -base64 32 > "$pending" || ! valid_key "$pending" || ! ln "$pending" "$rails_key"; then
            rm -f "$pending"
            echo 'Rails 金鑰初始化失敗；已保留標記，請查核並恢復原金鑰，拒絕自動重生' >&2
            exit 1
        fi
        rm -f "$pending"
    fi
fi

# 一次性標記與key須同時有效；即使without-rails也不能繞過已初始化金鑰損毀。
if ! private_file "$marker" || [ "$(cat "$marker")" != rails-jwt-v1 ] || ! valid_key "$rails_key"; then
    echo 'Rails 金鑰或初始化標記缺漏、毀損或非0600；拒絕覆寫，請查核並恢復原檔' >&2
    exit 1
fi

# 任一後端共用同一key都違反隔離契約；檢查只回報，不輸出敏感值。
unique_count=$(for backend in laravel symfony aspnet-core django flask fastapi gin echo fiber express fastify nestjs rails; do
    tr -d '\n' < ".local/keys/$backend.key"
    printf '\n'
done | sort -u | wc -l | tr -d ' ')
if [ "$unique_count" != 13 ]; then
    echo '後端金鑰重複；拒絕啟動或自動換key' >&2
    exit 1
fi
