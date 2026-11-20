# Rails：Ticket 01 隔離入口

Ruby `4.0.10`、Rails HTTP／啟動元件 `8.1.4.1`；只提供註冊、登入、登出、me、文章列表／詳情及文章留言列表。使用共用 PostgreSQL 與集中 SQL 入口，不執行 Rails migration。`filters[scope]=management` 先驗登入／角色及參數，管理員有效請求暫回 `404/E0004`；完整管理用途留待 Ticket 03。

Dockerfile 的 builder／runtime 均鎖定官方 Ruby digest，Gemfile.lock 鎖定套件與 checksum。pg 與 Argon2id 使用 ARM64 原生套件，runtime 不另外安裝系統 libpq。

測試僅使用 `isolated-rails01-*` 專案及其原生 local `postgres_data` volume。先建立專用絕對路徑 `RAILS01_FIXTURE_ROOT`，其中備妥 `backups/`、`laravel-logs/`、兩份各自獨立 32-byte 隨機金鑰的正規 Base64 檔 `laravel.key`、`rails.key`。金鑰只初始化一次；重跑保留原檔，不使用展示目錄。

專用且不提交的 `fixture.env` 須包含 `COMPOSE_PROJECT_NAME`、`RAILS01_FIXTURE_ROOT`、`RAILS01_LARAVEL_PORT`、`RAILS01_RAILS_PORT`、`DB_OWNER_PASSWORD`、`DB_APP_PASSWORD`、Laravel `APP_KEY`。兩個 port 使用未占用的隔離埠；原前端 CORS origin 仍是 `http://localhost:8080`。首次空庫不需備份；既有庫升級須先停止寫入，確認備份目錄，再明確設定 `PROJECT_WRITERS_STOPPED=1`。

從 repository root 執行，且明列測試服務，不作一般展示啟動：

```sh
docker compose --env-file "$RAILS01_FIXTURE_ROOT/fixture.env" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f ops/tests/rails01_compose.yaml build rails
docker compose --env-file "$RAILS01_FIXTURE_ROOT/fixture.env" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f ops/tests/rails01_compose.yaml up -d --wait db laravel rails
```

第一個測試寫入前，以 `docker inspect` 核對這三個服務的 Compose project/service、健康狀態、network、資料卷及掛載來源：DB 不發布 port、Rails／Laravel 僅綁 loopback，兩份金鑰與 Laravel logs 均在專用 fixture 目錄。核對執行中的 Rails image 與本次 build 相同；`/up` 回 `OK` 只證明健康，不代替建置身分證據。

將 owner 密碼從同一私有 fixture 載入 shell 環境，避免放進命令列文字或輸出。HTTP suite 會先查實際 DB `cluster_name`、應用角色與健康，再建立唯一命名帳號。owner 只用於該隔離庫的故障／時間／角色 fixture；不得指向展示庫。

```sh
docker compose --env-file "$RAILS01_FIXTURE_ROOT/fixture.env" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f ops/tests/rails01_compose.yaml exec -T rails bundle exec ruby test/protocol_test.rb
docker compose --env-file "$RAILS01_FIXTURE_ROOT/fixture.env" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f ops/tests/rails01_compose.yaml exec -T -e DB_OWNER_PASSWORD rails bundle exec ruby test/http_test.rb
```

`http_test.rb` 的一般案例使用真實 Puma HTTP、Laravel 與 PostgreSQL；精確時間等號另外經同一 Rails 公開 Rack 入口與真實交易，僅在測試程序固定 `Time.now`／DB 時鐘查詢，完成後恢復。金鑰／DB 不可用另以外部系統邊界替身驗證；它們不代表 live 容器故障或瀏覽器跨來源已通過。

重跑不清空資料，設定 fixture 在 ensure 復原；停止時使用同一組 Compose 參數 `stop rails laravel db`，保留 volume 與金鑰。SQL 更新失敗先停止相依應用，不自動倒退成功版本或還原備份。0009 只擴充 CHECK 白名單，套用仍會取得表鎖，須沿用停寫及成功備份的更新順序。
