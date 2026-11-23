# AI-workflow-fullstack

這是一個以 AI 協作開發的會員、文章與留言網站，用來展示不同語言和框架如何實作同一套功能。專案包含 **6 種前端與 13 種後端，共 19 個版本**，各版本共用 PostgreSQL 與 22 個 API 操作。

- 訪客可以瀏覽文章與留言，也可以註冊和登入。
- 會員可以發表文章、留言，並編修、下架或恢復自己的內容。
- 管理員可以管理會員、文章與留言，並調整登入錯誤鎖定設定。

| 類別 | 語言與框架 |
| --- | --- |
| 前端 | JavaScript／TypeScript：Vue 3 + Vite、Nuxt、React + Vite、Next.js、Angular、SvelteKit |
| 後端 | PHP：Laravel、Symfony |
| 後端 | C#：ASP.NET Core Controllers |
| 後端 | Python：Django、Flask、FastAPI |
| 後端 | Go：Gin、Echo、Fiber |
| 後端 | JavaScript（Node.js）：Express、Fastify、NestJS |
| 後端 | Ruby：Ruby on Rails |

本機服務由 Docker Compose 啟動，NGINX 提供共用的前端入口。

首次執行 `ops/setup.sh`，之後使用 `ops/start.sh` 等待十三後端、六前端與入口就緒。Rails 故障時可用 `ops/start.sh --without-rails` 啟動十二後端模式；修復後再執行 `ops/start.sh`。`ops/stop.sh` 保留資料卷及金鑰。六版畫面的切換仍由使用者操作，切換後端須重新登入；完整驗證範圍與尚未執行項目以[驗證紀錄](docs/verification.md)為準。

[本機啟動](docs/database-operations.md) · [使用指南](docs/user-guide.md) · [系統架構](docs/architecture.md) · [API](docs/article-api.md) · [開發指南](docs/developer-guide.md) · [驗證紀錄](docs/verification.md)
