# 會員文章展示：Vue／Nuxt 與 Laravel／Symfony／ASP.NET Core／Django／Flask／FastAPI／Gin／Echo／Fiber／Express

目前 Vue 提供註冊、登入、文章／留言閱讀與會員及管理頁；Nuxt 提供註冊、登入、公開文章列表與詳情。共同頁面可在同一分頁切換 Vue／Nuxt；各後端使用同一 PostgreSQL 資料庫，也可由頁面切換 API 來源。文章發表即公開，只接受純文字；發文成功返回我的文章第一頁，公開作者只顯示暱稱。登入使用固定八小時、可撤銷的 JWT；會員預設五分鐘內五次錯密碼，鎖定三百秒。Vue 可發表、編修、下架與恢復文章／留言；送出結果不明時保留同帳號的頁面記憶體草稿，須先查證再手動重試。留言保留原文及換行；含 U+0000 回 `422/E0001`，不移除、替換或寫入。

## 本機操作

需求：Docker Compose、OpenSSL；固定入口使用 `localhost:8080`（前端）、`localhost:8081`（Laravel API）、`localhost:8082`（Symfony API）、`localhost:8083`（ASP.NET Core API）、`localhost:8084`（Django API）、`localhost:8085`（Flask API）、`localhost:8086`（FastAPI）、`localhost:8087`（Gin API）、`localhost:8088`（Echo API）、`localhost:8089`（Fiber API）及 `localhost:8090`（Express API）。PostgreSQL 不對宿主機公開埠。

首次執行 `ops/setup.sh`，依提示輸入管理員 Email、暱稱及密碼。密碼為 4–16 碼 ASCII 英數，輸入不回顯，也不經命令列參數。設定會產生未納入 Git 的 `.env`、`.local/keys/` 十二份獨立金鑰、備份目錄與持久化 PostgreSQL volume。重跑只接受原管理員，保留其密碼；若同 Email 是會員則停止，不會升級角色。既有金鑰缺漏或損壞時也會停止，不會重生。

日常使用 `ops/start.sh` 與 `ops/stop.sh`。啟動會先停止本專案寫入，再查 SQL 版本；既有資料若有待套用版本，必須先成功產生 `.local/backups/` 的 `pg_dump` 備份。備份保留既有角色權限；還原須由人員在具備原有角色的目標環境操作。checksum 不同、未知資料庫版本、備份或 SQL 失敗都會阻止後端就緒，不會自動還原或清除資料。停止命令保留 volume、金鑰及備份。新增 SQL 檔放在 `database/sql/`，已成功套用的檔案不可修改。

Vue 位於 `frontend/vue/`，容器監聽 `0.0.0.0:4173` 並提供 `/vue/`；Nuxt 位於 `frontend/nuxt/` 並提供 `/nuxt/`。NGINX 只轉送前端，瀏覽器依頁面選擇直接呼叫各後端固定入口的 `/api/...`，包含 Fiber 的 `http://localhost:8089/api/...` 與 Express 的 `http://localhost:8090/api/...`。只啟動後端時可用 `ops/start.sh --backend-only`。`GET http://localhost:8081/up` 只有在應用帳號能查詢 `accounts`、三張認證表、`articles` 與 `comments` 時回 200。

Symfony 使用獨立 JWT 金鑰，直連同一資料庫；`GET http://localhost:8082/up` 確認應用帳號可讀共用表。切換後端會清除原登入並轉到登入頁，重新登入後返回合法的本機頁面；不會把舊 JWT 送給新後端。Ticket 08 隔離驗證可用 `COMPOSE_PROJECT_NAME=ticket08_test COMPOSE_FILE=compose.yaml:ops/tests/symfony_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only` 啟動，再執行 `COMPOSE_PROJECT_NAME=ticket08_test COMPOSE_FILE=compose.yaml:ops/tests/symfony_compose.yaml php -d zend.assertions=1 -d assert.exception=1 ops/tests/symfony_http.php`；測試會建立帳號與內容，僅在隔離資料庫執行，測試專用金鑰需先放入 `.local/ticket08-backend/laravel.key` 與 `symfony.key`。

ASP.NET Core Controllers 使用獨立 JWT 金鑰，直連同一資料庫；`GET http://localhost:8083/up` 也會確認共用表可讀。目前提供註冊、登入／登出、本人與公開文章／留言閱讀；寫入文章或留言仍須選擇已提供該端點的後端。

Ticket 13 的真實 HTTP 驗證只在 `ticket13_` 開頭的隔離 Compose 專案執行；它會建立測試帳號／內容並透過 Laravel 管理 API 測試停用。先準備 `.local/ticket13-backend/` 下對應的三份測試金鑰、`laravel-logs/`、`symfony-logs/` 與 `backups/`，再執行：

```bash
COMPOSE_PROJECT_NAME=ticket13_test COMPOSE_FILE=compose.yaml:ops/tests/aspnet_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
COMPOSE_PROJECT_NAME=ticket13_test COMPOSE_FILE=compose.yaml:ops/tests/aspnet_compose.yaml php -d zend.assertions=1 -d assert.exception=1 ops/tests/aspnet_http.php
```

Django API 固定於 `http://localhost:8084`，提供全部 22 項共用操作，包括作者內容、會員與內容管理及登入鎖定設定；Vue 的既有管理頁可選用 Django。使用 Django 5.2.17、Python 3.13.16、psycopg 3.3.6（LGPL-3.0-only）及 argon2-cffi 25.1.0（MIT）；直接讀寫共用 SQL 結構，JWT 金鑰讀取 `.local/keys/django.key`。Python 完整版本列於 `backends/django/requirements.txt`，ARM64 Python 基底映像以 digest 鎖定。Ticket 16 的 HTTP 驗證只對 `ticket16_` 開頭的隔離 Compose 專案寫入；準備 `.local/ticket16-backend/` 下四份測試專用金鑰與 `laravel-logs/`、`symfony-logs/`、`backups/` 後執行：

主 Compose 的 Django profile 讓舊票隔離 fixture 預設不啟動 Django，維持原本專用金鑰與埠；`ops/start.sh` 在一般展示與 Ticket 16 專用 fixture 明確啟動它，`ops/stop.sh` 則一併停止。

Flask API 固定於 `http://localhost:8085`，提供全部 22 項共用操作，包括註冊、登入／登出、文章／留言及會員管理與登入鎖定設定。Vue 可選用 Flask 完成作者與管理流程；Nuxt 尚無管理頁。它使用獨立的 `.local/keys/flask.key`，並讀寫同一 PostgreSQL 資料。一般 `ops/start.sh` 會啟動 Flask；舊票隔離 fixture 預設不啟動。容器內可用 `python -m unittest discover -s site_api -t . -p 'test_*.py'` 執行 Flask 專屬單元測試。Ticket 20 與 Ticket 21 的真實 HTTP／SQL 驗證僅可在 `ticket20_test` 隔離 Compose 專案執行，使用 `ops/tests/ticket20_compose.yaml` 與各自的 `ops/tests/ticket20_http.py`、`ops/tests/ticket21_http.py`；腳本會先核對專案、資料庫與後端身分，再建立唯一合成測資。

```sh
COMPOSE_PROJECT_NAME=ticket16_test COMPOSE_FILE=compose.yaml:ops/tests/ticket16_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
COMPOSE_PROJECT_NAME=ticket16_test COMPOSE_FILE=compose.yaml:ops/tests/ticket16_compose.yaml php -d zend.assertions=1 -d assert.exception=1 ops/tests/django_http.php
```

FastAPI 固定於 `http://localhost:8086`，提供全部 22 項共用操作；Vue 可選用 FastAPI 完成作者、管理及登入鎖定設定流程。Nuxt 尚未支援切換至 FastAPI。FastAPI 使用獨立的 `.local/keys/fastapi.key`，讀寫同一 PostgreSQL 資料；一般 `ops/start.sh` 會啟動它。Ticket 24 的管理 API 真實 HTTP 驗證使用隔離的 `ticket24_test` 專案、`ops/tests/ticket24_compose.yaml` 與 `ops/tests/ticket24_http.py`；腳本會先核對專案及資料庫身分，不能對展示資料執行。

Gin API 固定於 `http://localhost:8087`，提供全部 22 項共用操作，包括公開與作者文章／留言、會員管理及登入鎖定設定；Vue 可選用 Gin 完成作者與管理流程。Gin 使用獨立 `.local/keys/gin.key`，與其他後端讀寫同一 PostgreSQL，日常 `ops/start.sh` 會啟動。Ticket 25–27 的真實 HTTP 驗證使用隔離 `ticket25_test` 資料卷、`ops/tests/ticket25_compose.yaml`、測試專用金鑰及 `ops/tests/ticket25_http.py`、`ops/tests/ticket26_http.py`、`ops/tests/ticket27_http.py`；測試入口為 Vue `http://localhost:8090/vue/`、Laravel `http://localhost:8091` 與 Gin `http://localhost:8087`，腳本在寫入前核對專案、實際資料卷及入口埠。

Echo API 固定於 `http://localhost:8088`，提供全部 22 項共用操作，包含作者內容、會員與內容管理及登入鎖定設定。Vue 可選用 Echo 完成作者與管理流程；它使用獨立 `.local/keys/echo.key` 與共用 PostgreSQL。Ticket 28／29 的隔離驗證使用 `ticket28_test` 資料卷、`ops/tests/ticket28_compose.yaml`、測試專用金鑰及 `ops/tests/ticket28_http.py`、`ops/tests/ticket29_http.py`，隔離 Vue 入口為 `http://localhost:18090/vue/`、Laravel API 為 `http://localhost:18091`。先在 `.local/ticket28-backend/` 準備獨立的 `laravel.key`、`echo.key`、`laravel-logs/` 與 `backups/`，再以 `COMPOSE_PROJECT_NAME=ticket28_test COMPOSE_FILE=compose.yaml:ops/tests/ticket28_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only` 啟動，並以相同 Compose 環境執行 `python3 -B ops/tests/ticket28_http.py` 與 `python3 -B ops/tests/ticket29_http.py`；腳本會在寫入前核對專案、金鑰掛載、資料卷和實際埠。

Fiber API 固定於 `http://localhost:8089`，提供全部 22 項共用操作，包含公開與作者文章／留言、會員與內容管理及登入鎖定設定。Vue 可選用 Fiber 完成作者與管理流程；Nuxt 尚未支援切換至 Fiber。Fiber 使用獨立 `.local/keys/fiber.key` 與共用 PostgreSQL。Ticket 31 隔離測試使用 `ticket31_test` 專用資料卷、`ops/tests/ticket31_compose.yaml`、`.local/ticket31-backend/` 的測試金鑰及 CORS 設定；隔離入口為 Vue `http://localhost:18311/vue/`、Laravel API `http://localhost:18312`、Fiber API `http://localhost:8089`。Ticket 32 作者操作的隔離 API 使用 `ticket32_test` 專用資料卷、`ops/tests/ticket32_compose.yaml` 及 `.local/ticket32-backend/` 的測試金鑰；Fiber API 為 `http://localhost:18321`、Laravel API 為 `http://localhost:18322`。Ticket 33 管理驗證使用 `ticket33_test` 專用資料卷、`ops/tests/ticket33_compose.yaml`、`.local/ticket33-backend/` 的測試金鑰，以及 `.local/ticket33-frontend/` 的 Vue 副本。先執行 `python3 -B ops/tests/ticket33_http.py --prepare`，再以 `COMPOSE_PROJECT_NAME=ticket33_test COMPOSE_FILE=compose.yaml:ops/tests/ticket33_compose.yaml docker compose --profile fiber up -d --build --wait db sql laravel fiber frontend-vue nginx` 啟動，並在相同 Compose 環境執行 `python3 -B ops/tests/ticket33_http.py --run`；腳本會在寫入前核對實際容器、資料卷、金鑰、埠與 Vue bundle。隔離入口為 Vue `http://localhost:18330/vue/`、Fiber API `http://localhost:18331`、Laravel API `http://localhost:18332`。這些測試會新增帳號／內容並更改鎖定設定，不得對展示資料執行。

同一隔離 Compose 環境可執行 `python3 -B ops/tests/ticket28_query_count.py`，暫開 PostgreSQL 應用角色日誌，量測 Echo 文章與留言列表的實際 SQL 次數；腳本結束時會重設日誌設定並重啟隔離 Echo。

`python3 -B ops/tests/ticket29_query_count.py` 以同一隔離環境量測 Echo 個人文章與留言列表的實際 SQL 次數，並於結束時重設日誌設定與確認 Echo 健康。

Express API 固定於 `http://localhost:8090`，提供共用契約的 22 個操作，包括會員管理、全部留言列表與共用登入鎖定設定。Express 5.2.1、pg 8.23.0、argon2 0.45.1 以 `backends/express/package-lock.json` 鎖定，使用 Node 24.21.0 ARM64 可用的映像 digest。它以 `.local/keys/express.key` 的獨立金鑰和同一 PostgreSQL 資料庫運作；`GET /up` 驗證共用表可讀。原生單元測試可在 `backends/express/` 執行 `npm test`。

Ticket 34 真實 HTTP／SQL 驗證僅對 `ticket34_test` 專用資料卷執行。先在此 checkout 產生未入 Git 的 `.env`、十二個 `.local/keys/*.key`，並在 `.local/ticket34-backend/` 準備不同於展示金鑰的 `laravel.key`、`express.key`、`laravel-logs/`、`backups/`。將 Laravel CORS 設定複製為該目錄 `cors.php`，僅把允許 origin 改成 `http://localhost:18341`；如需瀏覽器驗證，再把 Vue 來源複製至 `.local/ticket34-frontend/`，在測試副本將 Laravel API 改成 `18342`、Express API 改成 `18343`。測試腳本會在寫入前核對 Compose 專案、資料卷、容器、金鑰、埠及健康狀態。

```sh
COMPOSE_PROJECT_NAME=ticket34_test COMPOSE_FILE=compose.yaml:ops/tests/ticket34_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
COMPOSE_PROJECT_NAME=ticket34_test COMPOSE_FILE=compose.yaml:ops/tests/ticket34_compose.yaml python3 -B ops/tests/ticket34_http.py
COMPOSE_PROJECT_NAME=ticket34_test COMPOSE_FILE=compose.yaml:ops/tests/ticket34_compose.yaml python3 -B ops/tests/ticket34_query_count.py
COMPOSE_PROJECT_NAME=ticket34_test COMPOSE_FILE=compose.yaml:ops/tests/ticket34_compose.yaml python3 -B ops/tests/ticket34_db_failure.py
COMPOSE_PROJECT_NAME=ticket34_test COMPOSE_FILE=compose.yaml:ops/tests/ticket34_compose.yaml ops/stop.sh
```

Ticket 35 作者操作測試使用新建的 `ticket35_test` 專用資料卷、`ops/tests/ticket35_compose.yaml`、Vue 入口 `http://localhost:18351/vue/`、Laravel API `http://127.0.0.1:18352` 與 Express API `http://127.0.0.1:18353`。先保留現有 `.env`，在 `.local/ticket35-backend/` 準備與展示不同的 `laravel.key`、`express.key`（各為 32-byte 隨機值的 base64）、`laravel-logs/`、`backups/`；複製 `backends/laravel/config/cors.php` 至該目錄的 `cors.php`，將允許來源改成 `http://localhost:18351`。瀏覽器驗證時，將 `frontend/vue/` 複製至 `.local/ticket35-frontend/`（排除 `node_modules/` 與 `dist/`），只在副本 `src/auth.js` 將 Laravel `8081`、Express `8090` 改為 `18352`、`18353`。啟動前先檢查 `18351`–`18353` 未占用；HTTP 腳本會在任何資料寫入前核對實際容器、資料卷、金鑰與埠。SQL 腳本讀取隔離 DB 日誌並執行 `EXPLAIN (ANALYZE, BUFFERS)`；兩者只對本票資料卷執行。

```sh
docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml config --quiet
docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml up -d --wait db
PROJECT_WRITERS_STOPPED=1 docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml up --build --exit-code-from sql sql
docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml --profile express up -d --build --wait laravel express
docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml --profile express exec -T express npm test
COMPOSE_PROJECT_NAME=ticket35_test COMPOSE_FILE=compose.yaml:ops/tests/ticket35_compose.yaml python3 -B ops/tests/ticket35_http.py
COMPOSE_PROJECT_NAME=ticket35_test COMPOSE_FILE=compose.yaml:ops/tests/ticket35_compose.yaml python3 -B ops/tests/ticket35_query_count.py
docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml --profile express up -d --build --wait frontend-vue nginx
docker compose -p ticket35_test -f compose.yaml -f ops/tests/ticket35_compose.yaml --profile express down
```

Ticket 36 管理 API 測試使用新的 `ticket36_test` 資料卷、`.local/ticket36-backend/` 專用金鑰與備份目錄，以及 Vue `127.0.0.1:18361`、Laravel `127.0.0.1:18362`、Express `127.0.0.1:18363`。沿用目前 `.env`，但不要沿用展示金鑰；各金鑰須為獨立 32-byte 隨機值的 base64。從 `backends/laravel/config/cors.php` 複製 `cors.php` 至此專用目錄，將允許 origin 改為 `http://localhost:18361`。瀏覽器測試前將 `frontend/vue/` 複製到 ignored `.local/ticket36-frontend/`（不複製 `node_modules/`、`dist/`），只在副本的 `src/auth.js` 將 Laravel `8081`、Express `8090` 分別改為 `18362`、`18363`，其餘後端埠改為無效的 `0`。啟動前確認三個埠未占用。HTTP 與 SQL 腳本在首次寫入前核對實際專案、容器、資料卷、金鑰及埠；負向 guard 可直接執行不帶環境變數的 `python3 -B ops/tests/ticket36_http.py --guard-only`，預期拒絕。SQL 腳本量測實際查詢次數與 `EXPLAIN (ANALYZE, BUFFERS)`；測試只寫入本票資料卷。

```sh
docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml --profile express config --quiet
docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml up -d --wait db
PROJECT_WRITERS_STOPPED=1 docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml up --build --exit-code-from sql sql
docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml --profile express up -d --build --wait laravel express
docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml --profile express up -d --build --wait frontend-vue nginx
docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml --profile express exec -T express npm test
COMPOSE_PROJECT_NAME=ticket36_test COMPOSE_FILE=compose.yaml:ops/tests/ticket36_compose.yaml python3 -B ops/tests/ticket36_http.py
COMPOSE_PROJECT_NAME=ticket36_test COMPOSE_FILE=compose.yaml:ops/tests/ticket36_compose.yaml python3 -B ops/tests/ticket36_query_count.py
docker compose -p ticket36_test -f compose.yaml -f ops/tests/ticket36_compose.yaml --profile express down
```


Fastify API 固定於 `http://localhost:8091`，提供註冊／登入／登出、文章與留言、會員管理及登入鎖定設定（操作 01–22）。Fastify 5.12.5、pg 8.23.0、argon2 0.45.1 由 `backends/fastify/package-lock.json` 鎖定，Node 24.21.0 ARM64 映像以 digest 鎖定；它使用 `.local/keys/fastify.key` 與共用 PostgreSQL。`GET /up` 會確認必要共用表可讀。

Ticket 38 作者操作驗證沿用 Ticket 37 的隔離方式，改用 `ticket38_test` 資料卷、`.local/ticket38-backend/` 的專用金鑰及 `18381`–`18383` loopback 埠。將 Laravel CORS 設定複製到 `.local/ticket38-backend/cors.php`，只把允許 origin 改成 `http://localhost:18381`；Vue 測試副本放在 `.local/ticket38-frontend/`，其 Laravel／Fastify API 位址分別改為 `18382`／`18383`。這些檔案僅供本機測試，不改展示設定。先確認專用資料卷、金鑰與埠，再執行：

```sh
COMPOSE_PROJECT_NAME=ticket38_test COMPOSE_FILE=compose.yaml:ops/tests/ticket38_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
COMPOSE_PROJECT_NAME=ticket38_test COMPOSE_FILE=compose.yaml:ops/tests/ticket38_compose.yaml python3 -B ops/tests/ticket38_http.py
COMPOSE_PROJECT_NAME=ticket38_test COMPOSE_FILE=compose.yaml:ops/tests/ticket38_compose.yaml docker compose --profile fastify exec -T fastify npm test
COMPOSE_PROJECT_NAME=ticket38_test COMPOSE_FILE=compose.yaml:ops/tests/ticket38_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh
COMPOSE_PROJECT_NAME=ticket38_test COMPOSE_FILE=compose.yaml:ops/tests/ticket38_compose.yaml ops/stop.sh
```

Ticket 39 管理操作使用獨立 `ticket39_test` 資料卷、`.local/ticket39-backend/` 金鑰與備份、`18391`–`18393` loopback 埠；不得與 Ticket 38 或展示資料共用。先在 `.local/ticket39-backend/` 建立與展示不同的 `laravel.key`、`fastify.key`（各為 32-byte 隨機值的 base64）、`laravel-logs/`、`backups/`；複製 Laravel `config/cors.php` 為 `cors.php`，將前端 origin 改為 `http://localhost:18391`。將 `frontend/vue/` 複製到 ignored `.local/ticket39-frontend/`（排除 `node_modules/`、`dist/`），只在副本 `src/auth.js` 將 Laravel `8081`、Fastify `8091` 改為 `18392`、`18393`。確認三個埠未占用後執行；HTTP 腳本在寫入前核對專案、埠、金鑰與資料卷，可重跑並產生獨立測資。

```sh
COMPOSE_PROJECT_NAME=ticket39_test COMPOSE_FILE=compose.yaml:ops/tests/ticket39_compose.yaml docker compose --profile fastify config --quiet
COMPOSE_PROJECT_NAME=ticket39_test COMPOSE_FILE=compose.yaml:ops/tests/ticket39_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
COMPOSE_PROJECT_NAME=ticket39_test COMPOSE_FILE=compose.yaml:ops/tests/ticket39_compose.yaml docker compose --profile fastify exec -T fastify npm test
COMPOSE_PROJECT_NAME=ticket39_test COMPOSE_FILE=compose.yaml:ops/tests/ticket39_compose.yaml python3 -B ops/tests/ticket39_http.py
COMPOSE_PROJECT_NAME=ticket39_test COMPOSE_FILE=compose.yaml:ops/tests/ticket39_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh
COMPOSE_PROJECT_NAME=ticket39_test COMPOSE_FILE=compose.yaml:ops/tests/ticket39_compose.yaml ops/stop.sh
```

Ticket 37 HTTP／SQL 驗證只在 `ticket37_test` 專用資料卷執行。先在 `.local/ticket37-backend/` 準備不同於展示金鑰的 `laravel.key`、`fastify.key`（各為 32-byte 隨機值的 base64）、`laravel-logs/`、`backups/`；將 `backends/laravel/config/cors.php` 複製為 `cors.php`，並只把允許 origin 改成 `http://localhost:18371`。沿用現有 `.env`，先確認 18371–18373 埠未占用。腳本在任何寫入前核對實際容器、隔離資料卷、金鑰及埠；SQL 腳本量測真正送出的查詢次數並執行 `EXPLAIN (ANALYZE, BUFFERS)`。Vue 瀏覽器驗證時另將前端複製到 ignored `.local/ticket37-frontend/`，在副本把 Laravel 與 Fastify API 指向 18372、18373；不在展示前端改測試位址。

```sh
COMPOSE_PROJECT_NAME=ticket37_test COMPOSE_FILE=compose.yaml:ops/tests/ticket37_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only
COMPOSE_PROJECT_NAME=ticket37_test COMPOSE_FILE=compose.yaml:ops/tests/ticket37_compose.yaml python3 -B ops/tests/ticket37_http.py
COMPOSE_PROJECT_NAME=ticket37_test COMPOSE_FILE=compose.yaml:ops/tests/ticket37_compose.yaml python3 -B ops/tests/ticket37_query_count.py
COMPOSE_PROJECT_NAME=ticket37_test COMPOSE_FILE=compose.yaml:ops/tests/ticket37_compose.yaml docker compose --profile fastify exec -T fastify npm test
COMPOSE_PROJECT_NAME=ticket37_test COMPOSE_FILE=compose.yaml:ops/tests/ticket37_compose.yaml ops/stop.sh
```

Ticket 30 的管理驗證使用獨立 `ticket30_test` 資料卷、`ops/tests/ticket30_compose.yaml`、`.local/ticket30-backend/` 下的測試專用 `laravel.key`、`echo.key`、`laravel-logs/`、`backups/`，以及 `.local/ticket30-frontend/` 的 Vue 測試副本。從 `frontend/vue/` 複製副本（不用複製 `node_modules/`、`dist/`），只在副本的 `src/auth.js` 將 Laravel `8081`、Echo `8088` 改為 `18093`、`18094`。從 `backends/laravel/config/cors.php` 複製 `.local/ticket30-backend/cors.php`，只將允許來源改為 `http://localhost:18092`，供隔離 Laravel 唯讀掛載；Vue 入口為 `http://localhost:18092/vue/`。以 `COMPOSE_PROJECT_NAME=ticket30_test COMPOSE_FILE=compose.yaml:ops/tests/ticket30_compose.yaml SETUP_IN_PROGRESS=1 ops/start.sh --backend-only` 啟動，再以相同 Compose 環境執行 `python3 -B ops/tests/ticket30_http.py`。腳本會在寫入前核對隔離容器、資料卷、金鑰、埠、CORS 與實際送達的 Vue bundle；測試會新增資料並修改隔離鎖定設定，不能指向展示資料。

Ticket 18 的完整 Django／Laravel 管理與鎖定設定比對使用 `ops/tests/ticket18_compose.yaml`、`ops/tests/ticket18_http.php` 與 `ticket18_test` 專用資料卷；測試會建立帳號及內容、改動設定，並在寫入前核對實際容器、埠位、金鑰掛載與資料卷。測試專用金鑰置於 `.local/ticket18-backend/`，不得對展示資料執行。

登入後將 `{backend,accessToken,expiresAt}` 存於同一分頁的 `sessionStorage` key `article-collection.auth.v1`；`backend` 為所選 API 基底位址，`expiresAt` 是原登入到期時間。前端切換沿用這份資料與原到期時間，重新整理會先用 JWT 呼叫 `GET /api/me` 確認身分；另以 `article-collection.backend.v1` 保存未登入時的後端選擇。未保存密碼或帳號資料；登入鎖定倒數只留在頁面記憶體，歸零後由使用者重新送出。Nuxt 尚無作者及管理頁，這些頁面不能切到 Nuxt；其他四版前端尚未提供。

管理員可在 `/vue/admin/settings/login-lockout` 讀取與儲存會員登入錯誤鎖定設定；對應端點為 `GET/PUT /api/settings/login-lockout`。設定包含啟用狀態、錯誤次數上限（1–100）、計算視窗（1–60 分鐘）及鎖定時間（1–86400 秒）；儲存時四欄須一起送出。停用鎖定會立即解除既有會員鎖定，但不影響停用帳號的狀態。集中 SQL `0008_login_lockout_settings.sql` 提供設定更新權限，既有資料庫會在下次 `ops/start.sh` 時依前述備份與版本流程套用。寫入結果不明時，頁面保留輸入並提示管理員手動重讀確認，不會自動重送。

文章頁路徑為 `/vue/`、`/vue/articles/{article_id}`、`/vue/articles/new`、`/vue/articles/{article_id}/edit`、`/vue/me/articles`。發文遇登入失效時，文字只保留在原頁記憶體；同帳號重登後可手動送出，換帳號則清除原草稿。重新整理或關頁不保留草稿；寫入結果不明時須先到我的文章查證，不自動重送。

文章契約測試只在 `ticket03_`、`ticket04_` 開頭或 `ticket05_test`的隔離 Compose 專案執行：`COMPOSE_PROJECT_NAME=ticket03_test API_BASE=http://localhost:8081 php -d zend.assertions=1 ops/tests/article_http.php`。測試會建立帳號及文章，並透過隔離資料庫 fixture 驗證下架可讀性、管理範圍、來源／作者欄位、純文字、排序分頁與錯誤回應；不對正式展示資料執行。

留言頁路徑為 `/vue/me/comments`、`/vue/comments/{comment_id}`、`/vue/comments/{comment_id}/edit`，文章閱讀頁亦提供留言分頁。作者可讀自己的下架留言及父文章下架後的留言；只回留言與 `article_id`，不授予父文章內容讀取權。留言契約測試僅允許 `ticket04_` 開頭或 `ticket05_test` 隔離 Compose 專案：`COMPOSE_PROJECT_NAME=ticket04_test API_BASE=http://localhost:8081 php -d zend.assertions=1 ops/tests/comment_http.php`。測試以真實 HTTP 新增留言，並以隔離 SQL fixture 驗證下架狀態與父文章鎖競爭，涵蓋讀取範圍、排序、分頁、原文、Unicode 長度、U+0000 拒絕與錯誤先後。

Vue 編輯頁僅允許作者；留言須另確認父文章公開。編修以讀取時的 `version` 送出，200 空 `data` 後依同一 ID 重讀；409 保留文字與版本，重讀展示最新內容，由使用者確認整合／捨棄後才採用新版本並手動送出。下架／恢復按目前身分、伺服器 `can_restore` 與操作確認呈現，衝突須重讀再確認。結果不明時保留警告及同 ID 查證入口，不自動重送。原頁重登同帳號保留草稿，不同帳號清除文字與衝突狀態。

Laravel 文章／留言生命週期端點為 `PUT /api/articles/{article_id}`、`PUT /api/comments/{comment_id}` 及各自的 `/visibility`。編修只允許作者，文章送出 `title`、`body`、`version`，留言送出 `body`、`version`；下架／恢復送出 `visibility`（`visible`／`hidden`）及 `version`。`version` 必須為 1–9,007,199,254,740,991 的 JSON 整數。先認證與格式驗證，再依可讀性、操作資格及版本回 404／403／409；成功回空 `data:{}`。正確版本的同值操作不更新版本或時間，過期同值仍回 `409/E0201`。

作者可改自己的下架文章；留言作者可改自己的個別下架留言，但父文章必須公開。作者及管理員可下架，作者只能恢復由作者控制的下架內容；管理員再次下架他人作者下架內容會接管恢復權並增加版本。管理員操作自己的內容按作者下架規則，仍具有管理員恢復資格。父文章下架不阻止符合資格的留言下架／恢復，但恢復留言不使父文章公開。文章／留言編修保留原文、換行與 HTML 字面文字；含 U+0000 拒絕為 `422/E0001`。版本達上限的實際變更由資料庫約束拒絕為既有 `500/E9999`，整筆交易回滾，不寫入溢位版本。

生命週期契約測試只允許 `COMPOSE_PROJECT_NAME=ticket05_test`：`COMPOSE_PROJECT_NAME=ticket05_test API_BASE=http://localhost:8081 php -d zend.assertions=1 ops/tests/lifecycle_http.php`。集中 SQL `0006_content_lifecycle_update.sql` 已新增文章與留言生命週期寫入所需的欄位級權限；`ticket05_test` 已備份並套用，欄位權限與 checksum 查驗通過，生命週期真實 HTTP 測試通過。測試會建立帳號／內容及修改 fixture，驗證精確錯誤、權限、同值無副作用、恢復控制、版本上限回滾，並觀察父文章下架、兩個寫入與登入撤銷的真實等鎖競爭。測試不對展示資料執行。

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
