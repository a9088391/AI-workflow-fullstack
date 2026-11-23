# 文章集｜AI-workflow-fullstack

一套可在本機操作的會員文章與留言網站，用來比較不同框架如何實作同一套功能。**六種前端與十三種後端可獨立切換、自由搭配，共 78 種組合、19 個版本。** 所有組合透過同一份 API 契約存取同一個 PostgreSQL 資料庫。

![文章集的六個桌面版文章列表，依序為裝幀書牆、海岸玻璃、午夜放映、雕塑展廳、水墨長卷與孔版海報](docs/images/frontend-overview.jpg)

*六種版型以同一批合成文章在本機展示。*

## 主要功能

- **公開閱讀與帳號**　訪客可直接瀏覽公開文章及留言。網站提供會員註冊、登入與登出。
- **發文與留言**　會員可發表純文字文章和留言，並編修、下架或恢復自己的內容。
- **後台管理**　管理員可搜尋會員、停用或恢復帳號、管理文章與留言，並調整登入失敗鎖定規則。
- **自由切換前後端**　工具列分別顯示目前使用的前端與後端。可為同一篇文章換版型，也可保留版型改用另一個 API 後端。

## 使用技術

### 六種前端

六版使用 JavaScript 或 TypeScript，由 NGINX 提供同一個入口 `http://localhost:8080`。每版有自己的視覺風格與頁面路徑。

| 前端 | 視覺風格 | 頁面路徑 |
| --- | --- | --- |
| Vue 3 + Vite | 裝幀書牆 | `/vue/` |
| Nuxt | 海岸玻璃 | `/nuxt/` |
| React + Vite | 午夜放映 | `/react/` |
| Next.js | 雕塑展廳 | `/next/` |
| Angular | 水墨長卷 | `/angular/` |
| SvelteKit | 孔版海報 | `/sveltekit/` |

### 十三種後端

| 後端 | 語言或執行環境 | API 基底位址 |
| --- | --- | --- |
| Laravel | PHP | `http://localhost:8081` |
| Symfony | PHP | `http://localhost:8082` |
| ASP.NET Core Controllers | C# | `http://localhost:8083` |
| Django | Python | `http://localhost:8084` |
| Flask | Python | `http://localhost:8085` |
| FastAPI | Python | `http://localhost:8086` |
| Gin | Go | `http://localhost:8087` |
| Echo | Go | `http://localhost:8088` |
| Fiber | Go | `http://localhost:8089` |
| Express | Node.js | `http://localhost:8090` |
| Fastify | Node.js | `http://localhost:8091` |
| NestJS | Node.js | `http://localhost:8092` |
| Ruby on Rails | Ruby | `http://localhost:8093` |

### 共用資料庫與 API

六個前端透過目前選用的後端讀寫資料。十三種後端遵守相同的 22 個 API 操作，讀寫**同一個 PostgreSQL 資料庫**。帳號、文章、留言與登入鎖定設定因此在各版本共用。資料庫結構由集中維護的版本化 SQL 更新。 [系統架構](docs/architecture.md) · [API 文件](docs/article-api.md)

本機環境使用 Docker Compose，驗證涵蓋框架測試、HTTP／PostgreSQL 契約測試與 Playwright／Chrome 瀏覽器測試。

## 前後端如何切換

在工具列分別選擇前端與後端。各版透過「切換版本」面板或直接顯示選單。首次開啟網站使用 Vue 3 + Vite 與 Laravel。

| 操作 | 切換後的行為 |
| --- | --- |
| **換前端** | 同一分頁會開啟對應頁面，並保留目前後端與有效登入。登入期限沿用原到期時間。例如 `/vue/articles/101` 切到 Nuxt 後會開啟 `/nuxt/articles/101`。 |
| **換後端** | 保留目前前端，清除舊憑證並前往登入頁。可用原帳號重新登入，在權限允許時回到原目標頁。各後端只接受自己簽發的 JWT，既有的登入鎖定也會在新後端生效。 |

### 切換限制

- **瀏覽環境**　登入延續與版本選擇以同一個瀏覽器分頁為限，需要瀏覽器允許分頁儲存。請使用 `http://localhost:8080` 作為展示入口。
- **頁面與權限**　對應頁面限六版支援的路徑與有效的資源 ID。目標會重新讀取資料並驗權，權限不足或資料不可用時會顯示對應狀態。
- **列表與草稿**　列表頁碼、搜尋條件和捲動位置會從目標頁的初始狀態開始。遇到草稿提示時可取消切換，或捨棄草稿後繼續。已送出的請求仍可能完成，切換後請以重新讀取的結果為準。
- **連線失敗**　目標服務未就緒或連線失敗時，可手動重試或返回上一頁。

[前後端選擇與頁面對照](docs/user-guide.md#選擇畫面與後端)

## 本機啟動、停止與重設

以下指令在專案根目錄執行。需要 macOS、Linux 或 Windows WSL2 的 POSIX shell、已啟動的 Docker daemon、支援 `docker compose up --wait` 的 Compose plugin，以及主機上的 `sh` 和 OpenSSL；本次只在 macOS 實測，其他平台尚未確認。首次建置需要網路下載容器映像與套件，且本機連接埠 `8080`–`8093` 需可使用。

先取得專案，再進入專案根目錄：

```sh
git clone https://github.com/a9088391/AI-workflow-fullstack.git
cd AI-workflow-fullstack
```

主機不需安裝各語言的 SDK，也不需取得作者的備份或金鑰。首次設定會為這個本機安裝建立自己的資料庫設定、應用金鑰及後端金鑰，並提示輸入管理員 Email、暱稱與密碼；密碼須為 4–16 碼 ASCII 英文字母或數字，沒有預設管理員帳密。空資料庫一開始沒有文章，管理員登入後可自行發文，或註冊一般會員。既有安裝請保留原有 `.env` 與 `.local/keys/`，不要刪除它們來強制產生新金鑰。

### 首次啟動

```sh
./ops/setup.sh
```

依終端提示建立第一位管理員。設定完成後開啟 [本機展示站](http://localhost:8080/)，預設使用 Vue 3 + Vite 與 Laravel。一般會員可從畫面註冊。

### 停止與重新啟動

```sh
./ops/stop.sh
```

`stop.sh` 會停止並移除本機容器及 Compose 網路，保留 PostgreSQL 資料卷與本機金鑰。需要繼續使用時，執行日常啟動指令。

```sh
./ops/start.sh
```

一般入口會等待 DB、SQL、十三個後端、六個前端及 NGINX 就緒。Rails 故障時，可執行 `./ops/start.sh --without-rails` 暫用十二後端模式；修復後再執行 `./ops/start.sh` 恢復十三版。此模式不重設資料或金鑰，SQL 版本與 checksum 保護維持不變。

需要查看服務狀態時可執行 `docker compose ps`。

### 重設管理員密碼

目前的重設功能限於管理員密碼。請在 Laravel 服務就緒時，於 Bash 執行下列命令。新密碼須為 4–16 碼 ASCII 英數，重設成功後會撤銷該管理員原有的登入工作階段。 [本機維運與備份](docs/database-operations.md)

```bash
read -r -p '管理員 Email：' admin_email
read -r -s -p '新密碼：' admin_password
printf '\n'
printf '%s\n%s\n' "$admin_email" "$admin_password" | docker compose exec -T laravel php artisan app:reset-admin-password
unset admin_password admin_email
```

## 如何測試

### 手動驗證

1. 開啟展示站，註冊一般會員並登入。發表文章後進入文章詳情，新增一則留言。
2. 留在同一分頁，從工具列切換前端，確認同一篇文章、留言與登入狀態都能讀取。
3. 切換後端，確認畫面要求重新登入。登入後核對原文章與留言。
4. 使用首次設定建立的管理員登入，開啟會員管理與登入鎖定設定，確認可以搜尋會員及讀取目前的鎖定設定。

[完整操作指南](docs/user-guide.md)

### 自動化測試

前端套件各有獨立鎖檔及測試指令。使用對應 Dockerfile 的 Node.js 版本。下列命令會安裝 Vue 版依賴、執行測試並建置。

```sh
(
  cd frontend/vue &&
  npm ci &&
  npm test &&
  npm run build
)
```

專案根目錄可用 Python 3.13 執行驗收工具的離線自測。

```sh
python3 -B ops/tests/cq34_http.py --self-test
python3 -B -m unittest discover -s ops/tests -p 'perf01_test.py'
```

涉及 HTTP 寫入、PostgreSQL 或跨版本的整合測試使用獨立的 Compose 專案與資料卷。 [開發與測試指南](docs/developer-guide.md) · [驗證紀錄](docs/verification.md)
