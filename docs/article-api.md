# 會員文章 API

本文件描述本專案的 **22 個共同操作**。十三個後端只更換基底位址，例如 Laravel 是 `http://localhost:8081`，NestJS 是 `http://localhost:8092`，Rails 是 `http://localhost:8093`；HTTP 方法、`/api/...` 路徑、欄位、權限與錯誤契約一致。所有基底位址見[系統架構](architecture.md)。

## 請求與回應慣例

JSON 請求使用 `Accept: application/json` 和 `Content-Type: application/json`；需要登入的操作再帶 `Authorization: Bearer {access_token}`。註冊與登入不沿用舊 token。公開文章列表、文章詳情及文章留言列表可以不帶 token；**若帶了 token，後端會驗證**，無效或過期 token 不會被當成訪客略過。跨來源前端直接呼叫 API，不經 NGINX 代理。

成功回應有固定外層；建立成功通常是 HTTP `201`，其餘成功為 `200`：

```json
{"success":true,"message":"OK","data":{}}
```

一般寫入回 `data:{}`，不回新資源 ID；讀取回 `data` 物件，列表回 `data.list` 與 `data.pagination`。錯誤回應包含 `success:false`、固定 `message`、`error_code` 和可供追查的 `log_id`；**只有欄位驗證錯誤**另有 `errors` 物件，例如：

```json
{
  "success": false,
  "message": "欄位資料不正確，請確認後再送出",
  "error_code": "E0001",
  "errors": {"email": ["此 Email 已註冊"]},
  "log_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

範例 Email、ID、`log_id` 和 token 都是示意值。API 的業務時間字串使用 `YYYY-MM-DD HH:mm:ss`（Asia/Taipei）；JWT 內部時間為 UTC Unix 整數秒。沒有分頁快照保證：多人同時新增資料時，相鄰頁可能變動。

## 共用輸入規則

| 欄位或參數 | 規則 |
| --- | --- |
| `email` | 最長 254 個 ASCII 字元；移除前後 U+0020 並將 ASCII 大寫轉小寫。只有一個 `@`；不接受國際化地址、引號或 IP 網域。正規化後全站唯一。 |
| `password` | 4–16 碼 ASCII 英數，區分大小寫；可只有字母或只有數字，不 trim。 |
| `nickname` | 1–30 個 Unicode 碼點，不可全空白或含 U+0000。 |
| 文章 `title`／`body` | 1–200／1–20,000 個 Unicode 碼點，不可全空白或含 U+0000；純文字，保留原文與換行。 |
| 留言 `body` | 1–2,000 個 Unicode 碼點；其餘純文字規則同上。 |
| URL 中的 ID、`page`、`per_page` | ASCII 十進位整數，可有前導零；不接受符號、小數、指數或空白。ID 與 `page` 範圍均為 1–9,007,199,254,740,991，`per_page` 為 1–100。 |
| JSON `version` | 1–9,007,199,254,740,991 的整數；字串及 boolean 不當成整數。 |

表中列出的 JSON 業務欄位都必填、不可為 `null`；未知、唯讀或多餘欄位會回 `422/E0001`。未知、重複或以陣列代替單值的 query 也會回 422。GET 不接受非空 Body；登出可沒有 Body 或送空物件。六個列表操作的 `page`、`per_page` **兩者都必填**，前端起始使用 `page=1&per_page=20`。

## 22 個操作

`公開*` 表示無 token 可讀，但攜帶 token 時會驗證。表中未列 query 的操作不接受業務 query；未列 Body 的操作不接受業務欄位。`admin` 指目前帳號角色，不是 JWT 內的角色聲明。

| # | 方法與路徑 | 權限 | Query／JSON Body | 成功 |
| ---: | --- | --- | --- | --- |
| 01 | `POST /api/auth/register` | 公開 | Body `email`, `password`, `nickname` | `201`；空 `data`，須另行登入 |
| 02 | `POST /api/auth/login` | 公開 | Body `email`, `password` | `200`；token、到期時間、帳號 |
| 03 | `POST /api/auth/logout` | 登入 | 無 Body 或 `{}` | `200`；撤銷目前登入 |
| 04 | `GET /api/me` | 登入 | 無 | `200`；目前帳號 |
| 05 | `GET /api/articles` | 公開*；管理範圍須 admin | `page`, `per_page`；選填 `filters[scope]=management` | `200`；文章列表 |
| 06 | `POST /api/articles` | 登入 | Body `title`, `body` | `201`；文章立即公開 |
| 07 | `GET /api/articles/{article_id}` | 公開*；下架內容限作者／admin | Path `article_id` | `200`；文章詳情 |
| 08 | `PUT /api/articles/{article_id}` | 文章作者 | Path `article_id`；Body `title`, `body`, `version` | `200`；空 `data` |
| 09 | `GET /api/me/articles` | 登入 | `page`, `per_page` | `200`；自己的文章列表 |
| 10 | `GET /api/me/comments` | 登入 | `page`, `per_page` | `200`；自己的留言列表 |
| 11 | `PUT /api/articles/{article_id}/visibility` | 作者／admin，恢復另驗權 | Path `article_id`；Body `visibility`, `version` | `200`；空 `data` |
| 12 | `GET /api/articles/{article_id}/comments` | 公開*；須可讀父文章 | Path `article_id`；`page`, `per_page` | `200`；文章留言列表 |
| 13 | `POST /api/articles/{article_id}/comments` | 登入；父文章須公開 | Path `article_id`；Body `body` | `201`；空 `data` |
| 14 | `GET /api/comments/{comment_id}` | 留言作者／admin | Path `comment_id` | `200`；留言詳情 |
| 15 | `PUT /api/comments/{comment_id}` | 留言作者；父文章須公開 | Path `comment_id`；Body `body`, `version` | `200`；空 `data` |
| 16 | `PUT /api/comments/{comment_id}/visibility` | 作者／admin，恢復另驗權 | Path `comment_id`；Body `visibility`, `version` | `200`；空 `data` |
| 17 | `GET /api/members` | admin | `page`, `per_page`；選填 `filters[keyword]` | `200`；一般會員列表 |
| 18 | `GET /api/members/{member_id}` | admin | Path `member_id` | `200`；一般會員詳情 |
| 19 | `PUT /api/members/{member_id}/status` | admin | Path `member_id`；Body `status` | `200`；空 `data` |
| 20 | `GET /api/comments` | admin | `page`, `per_page` | `200`；全部留言列表 |
| 21 | `GET /api/settings/login-lockout` | admin | 無 | `200`；四個設定欄位 |
| 22 | `PUT /api/settings/login-lockout` | admin | Body 四個設定欄位 | `200`；空 `data` |

`visibility` 僅接受 `visible` 或 `hidden`；`status` 僅接受 `active` 或 `disabled`。設定的四欄是 `enabled`（boolean）、`window_minutes`（1–60 整數）、`failure_threshold`（1–100 整數）、`lock_seconds`（1–86,400 整數）。更新設定時四欄都要送出，整筆一起提交。

## 讀取資料的形狀

- `GET /api/me`、管理會員詳情回 `Account`：`id`, `email`, `nickname`, `role`, `status`, `created_at`。`role` 為 `member` 或 `admin`；`status` 為 `active` 或 `disabled`。公開作者僅有 `id`、`nickname`。
- 登入成功的 `data` 為 `access_token`, `token_type:"Bearer"`, `expires_at`, `user:Account`。`GET /api/me` 的 `data` **直接是 Account**，不再包 `user`。登入期限固定八小時；後端 token 不互通。
- 文章詳情及文章列表項目有 `id`, `title`, `body`, `author:{id,nickname}`, `version`, `created_backend`, `created_at`, `updated_at`, `visibility`, `can_restore`。留言同形但無 `title`，另有 `article_id`。`created_backend` 是建立時使用的十三種後端 ID 之一，不因後來改用別的後端而變更。
- 列表的 `data` 有 `list:[...]` 和 `pagination:{page,per_page,total,total_pages}`，按 `created_at`、`id` 降冪。合法超頁仍回 `200`、空 `list`、原請求頁碼及真實 `total`；`total=0` 時 `total_pages=0`。
- `can_restore` 是後端按目前使用者與內容狀態算出的提示值；寫入時仍會重新驗權，不能當作永久授權。

`created_backend` 使用的精確 ID 為 `laravel`、`symfony`、`aspnet-core`、`django`、`flask`、`fastapi`、`gin`、`echo`、`fiber`、`express`、`fastify`、`nestjs`、`rails`。例如一筆文章及分頁的成功回應：

```json
{
  "success": true,
  "message": "OK",
  "data": {
    "list": [{
      "id": 101,
      "title": "第一篇展示文章",
      "body": "純文字內文\n第二行",
      "author": {"id": 1, "nickname": "展示讀者"},
      "version": 1,
      "created_backend": "laravel",
      "created_at": "2026-10-20 10:00:00",
      "updated_at": "2026-10-20 10:00:00",
      "visibility": "visible",
      "can_restore": false
    }],
    "pagination": {"page": 1, "per_page": 20, "total": 1, "total_pages": 1}
  }
}
```

例如公開文章列表：

```sh
curl -i 'http://localhost:8081/api/articles?page=1&per_page=20' -H 'Accept: application/json'
```

註冊與登入的 JSON 請求示例；範例帳號不是預設帳號：

```sh
curl -i 'http://localhost:8081/api/auth/register' \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  --data '{"email":"reader@example.test","password":"Demo1234","nickname":"展示讀者"}'

curl -i 'http://localhost:8081/api/auth/login' \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  --data '{"email":"reader@example.test","password":"Demo1234"}'
```

取得 token 後，對**同一後端**的受保護操作加上 `Authorization: Bearer <登入回傳的 access_token>`。不要把真實 token、密碼或金鑰放入 Git、網址或公開紀錄。

編修文章要帶**讀取時**的版本，且回應不會替前端提供新的版本；成功後重新讀取同一篇文章：

```http
PUT /api/articles/101
Accept: application/json
Content-Type: application/json
Authorization: Bearer <登入回傳的 access_token>

{"title":"修訂後的標題","body":"保留純文字與換行","version":1}
```

管理員更新鎖定設定時必須送齊四欄；讀取設定的 `data` 使用同四欄：

```json
{"enabled":true,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}
```

## 權限、版本與搜尋細節

- 公開文章列表預設只顯示公開文章，管理員也必須明確傳 `filters[scope]=management` 才讀管理範圍；留言管理用獨立的 `GET /api/comments`。管理文章／留言列表包含已下架內容。
- `GET /api/members` 只列一般會員。搜尋 `filters[keyword]` 只移除前後 U+0020 空格，最多 254 個 Unicode 碼點，省略或移除空格後為空代表不篩選。Email 或暱稱任一欄位以字面包含比對，ASCII 英文字母不分大小寫，其餘碼點精確比對。百分號、底線及跳脫字元均依字面比對。
- 關鍵字解碼後若含實際 U+0000（例如 query 中的 `%00`），回 `422/E0001`，`errors["filters.keyword"]` 為 `["欄位格式或範圍不正確"]`。後端會直接拒絕原輸入，文字形式的 `\u0000` 則按字面搜尋。管理會員詳情或狀態更新的目標若為管理員，回 `404/E0004`。
- 作者能修改自己的內文；管理員不能修改他人內文。作者可下架並恢復自己控制的內容；管理員下架或接管後，由具權限的管理員恢復。父文章下架不刪留言，也不會因恢復文章而自動恢復個別下架的留言。
- 文章留言列表先確認父文章可讀；一般讀者和文章作者預覽只看未個別下架的留言，管理員可看全部。留言詳情只供留言作者或管理員；知道 `article_id` 不會因此取得父文章的讀取權。
- 更新文章或留言時，先確認登入與角色、欄位格式、目標可讀性、操作權，再檢查 `version`。過期版本即使送相同值仍回 `409/E0201`；版本正確且沒有實際變更時成功，但不增加版本或 `updated_at`。寫入結果不明時先讀取，不自動重送。
- 新增或編修留言要求父文章公開。父文章已下架時，留言作者仍可依權限查看自己的留言、下架或恢復留言；恢復留言不會使父文章公開。
- 停用一般會員會撤銷舊登入；恢復不恢復舊登入。關閉登入鎖定功能會清計數與解除現有鎖定；只改數值不改既有鎖定的到期時間。同值設定更新不重做副作用。

## 錯誤代碼

| HTTP／代碼 | 固定訊息 | 常見情況 |
| --- | --- | --- |
| `422/E0001` | 欄位資料不正確，請確認後再送出 | 缺漏、型態、範圍、未知欄位或重複 query；附 `errors` |
| `401/E0002` | 登入已逾期，請重新登入 | 缺少、無效、過期、撤銷或其他後端的 token |
| `403/E0003` | 沒有權限使用此功能 | 角色或可讀目標的操作權不足 |
| `404/E0004` | 找不到指定資料或 API | 不存在或對目前身分隱藏 |
| `400/E0005` | 系統暫時無法處理，請稍後再試 | JSON 解析失敗；其他共同 HTTP 錯誤可依實際 4xx |
| `401/E0101` | 帳號或密碼錯誤 | 格式合法但帳號不存在或密碼錯誤 |
| `403/E0102` | 此帳號已停用，請聯絡系統管理員 | 已停用帳號 |
| `500/E0104` | 後台認證設定異常，請聯絡系統管理員 | 認證設定或儲存雜湊格式異常 |
| `429/E0105` | 登入失敗次數過多，請稍後再試 | 暫時鎖定；附 `Retry-After` 秒數 |
| `409/E0201` | 內容已更新，請確認最新內容後再送出 | 文章／留言版本過期 |
| `500/E9999` | 系統暫時無法處理，請稍後再試 | 未預期系統錯誤 |

格式錯誤的密碼不計入鎖定；符合格式但錯誤的會員密碼才可能計數。到達門檻的**當次**登入即回 `429/E0105`。`Retry-After` 是剩餘秒數向上取整，鎖定中至少 1 秒，並由 CORS 暴露給前端。停用帳號優先回 `E0102`；登入計數的視窗為 `t−N 分鐘 < 發生時間 ≤ t`，左邊界恰好相等的失敗不計入。
