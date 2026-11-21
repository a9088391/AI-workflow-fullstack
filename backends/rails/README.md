# Rails：Ticket 01–03 隔離入口

Ruby `4.0.10`、Rails HTTP／啟動元件 `8.1.4.1`；提供全部 22 個共同 API 操作，包含會員、文章與留言管理及登入鎖定設定。使用共用 PostgreSQL 與集中 SQL 入口，不執行 Rails migration。只有明確 `filters[scope]=management` 的管理員文章列表包含下架內容；管理留言列表包含下架父文的留言，公開列表與原作者操作保留原範圍。

受保護內容寫入於同一交易依帳號→登入紀錄→文章→留言取得必要鎖，鎖後重驗身分；父文章公開檢查、操作權限、版本及內容更新不可分割。本文編修不自動恢復下架內容，管理員接管他人內容的恢復權算實際變更；同值且版本正確不更新版本與時間。建立來源由伺服器填 `rails`，後續更新保留原來源。

管理員只能讀取／停用 `role=member` 的帳號。會員搜尋以暱稱或 Email 字面包含、只摺疊 ASCII 大小寫，關鍵字只去除前後 U+0020；實際 U+0000 拒絕 422，`%`、`_`、反斜線皆無特殊語意。實際停用與全部後端登入撤銷在同一交易；恢復不復活舊登入，也不解除未到期暫時鎖定。

登入與設定更新依設定列→帳號（多筆按 ID）→登入紀錄取鎖。設定四欄原子更新，最後完整提交為準；只有 `true→false` 才在同交易切換所有一般會員的失敗世代、解除鎖定，不刪失敗歷史、不解除停用、不撤銷有效登入。同值或單純數字修改不重做副作用。回退程式不能復原已撤銷登入或已切換的計數世代。

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
docker compose --env-file "$RAILS01_FIXTURE_ROOT/fixture.env" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f ops/tests/rails01_compose.yaml exec -T -e DB_OWNER_PASSWORD rails bundle exec ruby test/content_test.rb
docker compose --env-file "$RAILS01_FIXTURE_ROOT/fixture.env" -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f ops/tests/rails01_compose.yaml exec -T -e DB_OWNER_PASSWORD rails bundle exec ruby test/administration_test.rb
```

第 02 票沿用同一 Compose YAML 及 `RAILS01_*` guard，但必須另建專屬 project、埠、fixture 和資料卷，不共用第 01 票或展示資料。`content_test.rb` 重用並包含 `http_test.rb` 的全部回歸，再加文章／留言契約、兩方向 Laravel 互通、受控 PostgreSQL 鎖重疊、撤銷／停用競爭、失敗回滾及個人分頁。查詢量與 `EXPLAIN (ANALYZE, BUFFERS)` 從真實公開 Rack 入口擷取 SQL，在容器 `/tmp/rails02-query-plans-*.json` 留存測資量、統計、綁定值與耗時；證據只含合成資料，不記 JWT／jti。測試不自動重送結果不明的寫入。

第 03 票同樣使用獨立 project／埠／資料卷／金鑰。`administration_test.rb` 包含既有 HTTP／內容全部回歸，再加管理角色與逐欄驗證、兩向撤銷、設定／登入／停用／內容／登出真實鎖競爭、故障回滾、時間邊界及會員精確集合／分頁。會員查詢計畫位於容器 `/tmp/rails03-query-plans-*.json`，涵蓋無關鍵字、Email、短字、中文、非 ASCII 大小寫及字面符號，statement 數包含 PG 兩次 type-map 初始化。重跑完整 suite 兩次以確認同一隔離庫可重用，不清空前次資料。

`http_test.rb` 的一般案例使用真實 Puma HTTP、Laravel 與 PostgreSQL；精確時間等號另外經同一 Rails 公開 Rack 入口與真實交易，僅在測試程序固定 `Time.now`／DB 時鐘查詢，完成後恢復。金鑰／DB 不可用另以外部系統邊界替身驗證；它們不代表 live 容器故障或瀏覽器跨來源已通過。

重跑不清空資料，設定 fixture 在 ensure 復原；停止時使用同一組 Compose 參數 `stop rails laravel db`，保留 volume 與金鑰。SQL 更新失敗先停止相依應用，不自動倒退成功版本或還原備份。0009 只擴充 CHECK 白名單，套用仍會取得表鎖，須沿用停寫及成功備份的更新順序。
