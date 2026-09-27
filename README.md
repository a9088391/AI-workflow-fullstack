# 會員文章展示：Vue／Laravel

目前提供 Vue 註冊／登入／登出、公開文章列表／閱讀、發文與我的文章，以及對應 Laravel API 和共用 PostgreSQL。文章發表即公開，只接受純文字；發文成功返回我的文章第一頁，公開作者只顯示暱稱。登入使用固定八小時、可撤銷的 JWT；會員預設五分鐘內五次錯密碼，鎖定三百秒。文章編修／下架介面、留言和其他框架尚未提供。

## 本機操作

需求：Docker Compose、OpenSSL；固定入口使用 `localhost:8080`（前端）與 `localhost:8081`（Laravel API）。PostgreSQL 不對宿主機公開埠。

首次執行 `ops/setup.sh`，依提示輸入管理員 Email、暱稱及密碼。密碼為 4–16 碼 ASCII 英數，輸入不回顯，也不經命令列參數。設定會產生未納入 Git 的 `.env`、`.local/keys/` 十二份獨立金鑰、備份目錄與持久化 PostgreSQL volume。重跑只接受原管理員，保留其密碼；若同 Email 是會員則停止，不會升級角色。既有金鑰缺漏或損壞時也會停止，不會重生。

日常使用 `ops/start.sh` 與 `ops/stop.sh`。啟動會先停止本專案寫入，再查 SQL 版本；既有資料若有待套用版本，必須先成功產生 `.local/backups/` 的 `pg_dump` 備份。備份保留既有角色權限；還原須由人員在具備原有角色的目標環境操作。checksum 不同、未知資料庫版本、備份或 SQL 失敗都會阻止後端就緒，不會自動還原或清除資料。停止命令保留 volume、金鑰及備份。新增 SQL 檔放在 `database/sql/`，已成功套用的檔案不可修改。

Vue 位於 `frontend/vue/`，容器監聽 `0.0.0.0:4173` 並提供 `/vue/`；NGINX 只轉送前端，瀏覽器直接向 `http://localhost:8081/api/...` 呼叫 Laravel。只啟動後端時可用 `ops/start.sh --backend-only`。`GET http://localhost:8081/up` 只有在應用帳號能查詢 `accounts`、三張認證表與 `articles` 時回 200。

Vue 登入後將 `{backend,accessToken,expiresAt}` 存於同一分頁的 `sessionStorage` key `article-collection.auth.v1`；`backend` 為 API 基底位址 `http://localhost:8081`，`expiresAt` 是原登入到期時間。未保存密碼或帳號資料；重新整理會先用 JWT 呼叫 `GET /api/me` 確認身分，確認失敗時可手動重試。登入鎖定倒數只留在頁面記憶體，歸零後由使用者重新送出。

文章頁路徑為 `/vue/`、`/vue/articles/{article_id}`、`/vue/articles/new`、`/vue/me/articles`。發文遇登入失效時，文字只保留在原頁記憶體；同帳號重登後可手動送出，換帳號則清除原草稿。重新整理或關頁不保留草稿；寫入結果不明時須先到我的文章查證，不自動重送。

文章契約測試只在 `ticket03_` 開頭的隔離 Compose 專案執行：`COMPOSE_PROJECT_NAME=ticket03_test API_BASE=http://localhost:8081 php -d zend.assertions=1 ops/tests/article_http.php`。測試會建立帳號及文章，並透過隔離資料庫 fixture 驗證下架可讀性、管理範圍、來源／作者欄位、純文字、排序分頁與錯誤回應；不對正式展示資料執行。

本機整合測試請設 `COMPOSE_PROJECT_NAME` 為隔離名稱，避免把測試帳號寫進展示資料。例如啟動隔離的後端後執行 `COMPOSE_PROJECT_NAME=ticket01_test php -d zend.assertions=1 ops/tests/register_http.php`。測試會產生隨機 Email，確認暱稱含 U+0000 時回 422/E0001；字面六字元 `\u0000` 仍是合法文字。`ops/tests/sql_entry.sh` 會另建隔離資料庫 volume 測試 SQL 版本、備份、鎖與回滾，結束時保留 volume 與 `.local/` 證據供查核。


## 管理員本機密碼重設

在已啟動的 Compose 專案內執行 `app:reset-admin-password`。指令只接受既有管理員，不新增帳號、不將會員升級；密碼更新與該管理員所有後端的既有登入撤銷在同一交易完成。一般登入互不撤銷，API 登出只撤銷目前 token。

以下在 **Bash** 執行，密碼不回顯、不進命令列參數；stdin 使用兩行 LF 結尾，第一行為 Email、第二行為新密碼。密碼維持 4–16 碼 ASCII 英數。

```bash
read -r -p '管理員 Email：' admin_email
read -r -s -p '新密碼：' admin_password
printf '\n'
printf '%s\n%s\n' "$admin_email" "$admin_password" | docker compose exec -T laravel php artisan app:reset-admin-password
unset admin_password admin_email
```

## 認證契約驗證

`ops/tests/auth_http.php` 使用真實 HTTP、隔離資料庫 fixture 與本機維運命令；必須設定 `COMPOSE_PROJECT_NAME=ticket02_...`、`API_BASE`、`COMPOSE_FILE` 及 `AUTH_TEST_KEY`。`AUTH_TEST_KEY` 必須是 `.local/ticket02-backend/` 內的測試專用金鑰副本，並掛載到測試 Laravel 的 `/run/secrets/laravel-jwt.key`，不可直接使用展示金鑰。測試包含暫停／恢復隔離 DB、短暫毀損／復原該測試金鑰、精確時間邊界及交易回滾 fixture，會保留測試資料；不對正式展示專案執行。

首次準備隔離環境（已有 `.env`／展示金鑰；先完成一般首次設定）：

```bash
mkdir -p .local/ticket02-backend/logs .local/ticket02-backend/backups
test -f .local/ticket02-backend/laravel.key || cp .local/keys/laravel.key .local/ticket02-backend/laravel.key
COMPOSE_PROJECT_NAME=ticket02_backend COMPOSE_FILE=compose.yaml:ops/tests/auth_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
```

完成後執行測試，再用相同兩個 Compose 環境變數執行 `ops/stop.sh`，保留 volume、金鑰和日誌。

```bash
COMPOSE_PROJECT_NAME=ticket02_backend \
COMPOSE_FILE=compose.yaml:ops/tests/auth_compose.yaml \
API_BASE=http://localhost:18081 \
AUTH_TEST_KEY="$PWD/.local/ticket02-backend/laravel.key" \
php -d zend.assertions=1 ops/tests/auth_http.php
```
