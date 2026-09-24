# 會員文章展示：Ticket 01

目前只提供 Vue／Laravel 的註冊，以及共用 PostgreSQL 的第一版資料結構。登入、文章、留言和其他框架尚未提供。

## 本機操作

需求：Docker Compose、OpenSSL；固定入口使用 `localhost:8080`（前端）與 `localhost:8081`（Laravel API）。PostgreSQL 不對宿主機公開埠。

首次執行 `ops/setup.sh`，依提示輸入管理員 Email、暱稱及密碼。密碼為 4–16 碼 ASCII 英數，輸入不回顯，也不經命令列參數。設定會產生未納入 Git 的 `.env`、`.local/keys/` 十二份獨立金鑰、備份目錄與持久化 PostgreSQL volume。重跑只接受原管理員，保留其密碼；若同 Email 是會員則停止，不會升級角色。既有金鑰缺漏或損壞時也會停止，不會重生。

日常使用 `ops/start.sh` 與 `ops/stop.sh`。啟動會先停止本專案寫入，再查 SQL 版本；既有資料若有待套用版本，必須先成功產生 `.local/backups/` 的 `pg_dump` 備份。備份保留既有角色權限；還原須由人員在具備原有角色的目標環境操作。checksum 不同、未知資料庫版本、備份或 SQL 失敗都會阻止後端就緒，不會自動還原或清除資料。停止命令保留 volume、金鑰及備份。新增 SQL 檔放在 `database/sql/`，已成功套用的檔案不可修改。

Vue 位於 `frontend/vue/`，容器監聽 `0.0.0.0:4173` 並提供 `/vue/`；NGINX 只轉送前端，瀏覽器直接向 `http://localhost:8081/api/...` 呼叫 Laravel。只啟動後端時可用 `ops/start.sh --backend-only`。`GET http://localhost:8081/up` 只有在應用帳號能查詢 `accounts` 時回 200。

本機整合測試請設 `COMPOSE_PROJECT_NAME` 為隔離名稱，避免把測試帳號寫進展示資料。例如啟動隔離的後端後執行 `COMPOSE_PROJECT_NAME=ticket01_test php -d zend.assertions=1 ops/tests/register_http.php`。測試會產生隨機 Email，確認暱稱含 U+0000 時回 422/E0001；字面六字元 `\u0000` 仍是合法文字。`ops/tests/sql_entry.sh` 會另建隔離資料庫 volume 測試 SQL 版本、備份、鎖與回滾，結束時保留 volume 與 `.local/` 證據供查核。
