# 開發指南

先依[本機啟動指引](database-operations.md#首次設定與日常啟停)準備展示環境。修改共同功能前，可先對照[使用指南](user-guide.md)、[系統架構](architecture.md)與 [22 個共同 API 操作](article-api.md)。

## 程式在哪裡

| 路徑 | 職責 |
| --- | --- |
| [frontend/](../frontend/) | 六版介面、導覽、登入狀態與 API 呼叫 |
| [backends/](../backends/) | 十二版 API、驗證、權限與交易 |
| [database/sql/](../database/sql/) | 共用 PostgreSQL 的版本化 SQL |
| [ops/sql/apply.php](../ops/sql/apply.php) | SQL 版本檢查、備份與套用 |
| [ops/](../ops/) | 本機啟停、NGINX 設定及隔離驗收入口 |

## 依修改範圍選擇檢查

| 修改範圍 | 檢查重點 |
| --- | --- |
| 單一前端 | 單元測試與正式建置，加上受影響頁面的窄／寬畫面、鍵盤操作與錯誤處理 |
| 共同 API 或登入狀態 | 核對十二版後端的回應和副作用，以及六版前端的讀取、切換和重登 |
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
python3 -B -m unittest discover -s ops/tests -p 'perf01_test.py'
```

要執行會寫資料的 HTTP 或 PostgreSQL 驗收，先確認測試所需的專用 Compose 專案、連接埠、資料卷與金鑰。各測試入口有自己的隔離檢查，日常展示資料不能用於這類測試。可重用的結果與已知限制見[驗證紀錄](verification.md)。

## 更新 SQL 與文件

共用結構改動在 `database/sql/` 加入新的版本檔。已套用檔案由 checksum 保護，改寫會阻止啟動。備份及恢復步驟見[資料庫與本機維運](database-operations.md)。

使用者操作有變更時更新[使用指南](user-guide.md)，API 規則有變更時更新 [API 文件](article-api.md)。驗證紀錄只列已執行的範圍及尚未驗證的界線。
