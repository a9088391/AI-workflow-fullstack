# 開發指南

先依[本機啟動指引](database-operations.md#首次設定與日常啟停)準備展示環境。修改共同功能前，可先對照[使用指南](user-guide.md)、[系統架構](architecture.md)與 [22 個共同 API 操作](article-api.md)。

## 程式在哪裡

| 路徑 | 職責 |
| --- | --- |
| [frontend/](../frontend/) | 六版介面、導覽、登入狀態與 API 呼叫 |
| [backends/](../backends/) | 十三版 API、驗證、權限與交易，包含 Ruby on Rails |
| [database/sql/](../database/sql/) | 共用 PostgreSQL 的版本化 SQL |
| [ops/sql/apply.php](../ops/sql/apply.php) | SQL 版本檢查、備份與套用 |
| [ops/](../ops/) | 本機啟停、NGINX 設定及隔離驗收入口 |

## 依修改範圍選擇檢查

| 修改範圍 | 檢查重點 |
| --- | --- |
| 單一前端 | 單元測試與正式建置，加上受影響頁面的窄／寬畫面、鍵盤操作與錯誤處理 |
| 共同 API 或登入狀態 | 核對十三版後端的回應和副作用，以及六版前端的讀取、切換和重登 |
| 資料庫結構或啟停流程 | 在專用資料卷驗證空庫建立、既有資料升級、失敗回復與重啟後持久化 |

新增或修改函式、方法與 callback 時，用簡短繁體中文說明用途與原因。驗證順序、權限、交易與非同步清理等關鍵段落，補上讀者需要知道的理由。

各版 Node.js 專案使用自己的鎖檔。先確認 Dockerfile 所用的 Node.js 版本，再依該目錄的 `package.json` 選擇命令。例如在專案根目錄執行下列命令可檢查 Vue 版。首次安裝依賴需要可連線的套件來源。

```sh
cd frontend/vue
npm ci
npm test
npm run build
```

Go 後端可在各自的目錄執行 `go test ./...` 與 `go vet ./...`。其他後端的測試與建置入口見各版程式目錄。

## 從乾淨 checkout 可執行的離線驗收

需要 macOS 或 Linux 上的 Python 3.13。從專案根目錄執行下列命令即可檢查隔離驗收工具本身，無須啟動容器或設定資料庫。

```sh
python3 -B ops/tests/cq34_http.py --self-test
python3 -B ops/tests/cq34_http.py --preset rails05 --self-test
python3 -B -m unittest discover -s ops/tests -p 'perf01_test.py'
sh ops/tests/ticket45_start.sh
sh ops/tests/rails05_start.sh
```

要執行會寫資料的 HTTP 或 PostgreSQL 驗收，先確認測試所需的專用 Compose 專案、連接埠、資料卷與金鑰。各測試入口有自己的隔離檢查，日常展示資料不能用於這類測試。可重用的結果與已知限制見[驗證紀錄](verification.md)。

`rails05_start.sh` 呼叫實際啟停／設定腳本的隔離副本，以 Docker double 驗證參數、命令順序、十三／十二後端模式、SQL／健康失敗、舊 fixture 選擇與金鑰初始化／缺損拒絕；它不存取 Docker daemon。這類模擬不代表空庫／升級、十三版 22 操作、六版真 UI 或 78 組搭配已通過；真實整合與瀏覽器紀錄須另外核對。Rails 使用 Ruby `4.0.10` 與 Rails `8.1.4.1`，精確依賴以現有 Dockerfile／Gemfile.lock 為準，隔離 HTTP 測試入口見 [Rails 說明](../backends/rails/README.md)。

## Rails05 十三後端 HTTP 回歸

沿用 CQ34 同一 driver，明確加上 `--preset rails05`；不帶 preset 仍限定原 CQ34 十二版。Rails05 每版跑原十組案例及共用讀回，保留 22 操作、284 個 invalid response 的逐案比較、原十二版環狀交易配對、鎖定與時間邊界、雙序／並行、session／expiry 及精確恢復。另在共同契約組內檢查全部 156 個有向 pair 的密碼互通、JWT 拒絕與文章／留言共享寫入，保留建立來源。

只能在固定 `.local/rails05-fixture` source 副本執行，project 為 `rails05_test`，override 為 `compose.yaml:.local/rails05-compose.yaml`；override bytes 須與 [rails05_compose.yaml](../ops/tests/rails05_compose.yaml) 相同。副本須包含當前被測程式及相同檔案 mode，不含原環境的 `.local` 資料；NGINX 設定按原 repository 的 HEAD 比對。從乾淨 checkout 準備自有副本後，在副本依[首次設定](database-operations.md#首次設定與日常啟停)建立專用 `.env`、管理員及持久金鑰，再建置／啟動。已有副本或資料卷時先確認其歸屬，不覆寫、重建或清空。

專用 subnet 為 `10.251.135.0/24`，原生 local volume 為 `rails05_test_postgres_data`，DB 標記／cluster name 由 template 設定。十三個 API 固定 localhost `8081–8093`，前端 origin 仍為 `http://localhost:8080`，不得為測試放寬 CORS 或改用其他 port。副本自己的 `.local/keys` 保存十三份獨立 canonical Base64 32-byte key 與 `.rails-initialized`，權限均為 `0600`。這些名稱、env 或 health 不能單獨授予寫入權；guard 還核對實際容器、image、mount、volume、network、DB cluster、來源與 HTTP Origin。

在**原 repository 根目錄**保存入口路徑，切到已核對的副本執行；以下 guard 不建立合成資料，`--all` 會寫入合成資料與有限 fixture：

```sh
rails05_repo=$(pwd -P)
cd "$rails05_repo/.local/rails05-fixture"
export COMPOSE_PROJECT_NAME=rails05_test
export COMPOSE_FILE=compose.yaml:.local/rails05-compose.yaml
python3 -B "$rails05_repo/ops/tests/cq34_http.py" --preset rails05 --guard
python3 -B "$rails05_repo/ops/tests/cq34_http.py" --preset rails05 --all
```

兩輪重跑須使用相同自有目標，不清除測資。每輪首寫前落盤 `.local/rails05-recovery/recovery-r05<8hex>.json`，namespace 固定 `rails05`；短 nonce 保留隨機位元並避免 helper 暱稱超過 30 碼。設定／時間 fixture 恢復並讀回，合成內容保留；這不是清庫或資料庫備份還原。中斷／pending 時禁止新 run，由負責人先查明寫入狀態，再對**原來源及相同完整 target identity**執行 `--preset rails05 --restore .local/rails05-recovery/recovery-r05<8hex>.json`（以實際檔名替換）。未知遠端寫入即使 cleanup 完成仍為 incomplete，不可當作已回滾或自動重送；舊 CQ34 recovery 不可在 Rails05 恢復。

HTTP suite 不證明六版真 UI、78 搭配或 Rails 故障／正常啟停。這些檢查與空庫、升級、備份、SQL 失敗、資料／金鑰／原登入期限保留，須由獨立維運及瀏覽器驗收另行記錄，不能以離線 guard 或 API PASS 代替。

## 更新 SQL 與文件

共用結構改動在 `database/sql/` 加入新的版本檔。已套用檔案由 checksum 保護，改寫會阻止啟動。備份及恢復步驟見[資料庫與本機維運](database-operations.md)。

使用者操作有變更時更新[使用指南](user-guide.md)，API 規則有變更時更新 [API 文件](article-api.md)。驗證紀錄只列已執行的範圍及尚未驗證的界線。
