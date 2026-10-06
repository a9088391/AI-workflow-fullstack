"""FastAPI 原始輸入、JWT 與公開讀取的最小契約回歸。"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from . import app, core
from .input import json_body, list_input


# 最小請求替身保留原始 Query 與 Body，不讓測試用戶端先正規化。
class Request:
    # 保留呼叫端指定的原始欄位供同步處理函式讀取。
    def __init__(self, query=b"", body=b"", content_type="application/json", authorization=None):
        self.query_string = query
        self.body = body
        self.content_type = content_type
        self.headers = {} if authorization is None else {"Authorization": authorization}

    # 配合正式 Input 的同一讀取介面。
    def get_data(self, cache=True):
        return self.body


# 用固定測試列記錄 SQL，驗證權限條件與回應，而不連接真實資料庫。
class Connection:
    # 依呼叫順序提供固定資料列並記錄每句 SQL。
    def __init__(self, rows):
        self.rows = iter(rows)
        self.queries = []

    # 模擬 psycopg context manager 的進入介面。
    def __enter__(self):
        return self

    # 不改測試列狀態，讓斷言檢查查詢與明確提交。
    def __exit__(self, *_):
        pass

    # 留下 SQL 與參數供鎖順序和篩選條件斷言。
    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        return self

    # 按查詢順序提供單筆結果。
    def fetchone(self):
        return next(self.rows)

    # 按查詢順序提供列表結果。
    def fetchall(self):
        return next(self.rows)


# 記錄登入鎖順序與提交，避免 HTTP 例外導致門檻當次的資料回滾。
class LoginConnection(Connection):
    # 提供剛好達到門檻的會員與設定狀態。
    def __init__(self, now):
        super().__init__([
            {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 30},
            {"id": 7, "email": "a@example.test", "password_hash": "test-phc", "nickname": "A",
             "role": "member", "status": "active", "failure_epoch": 0, "locked_until": None},
            {"now": now}, {"count": 1}, {"now": now},
        ])
        self.commits = 0

    # 記錄回 429 前是否已提交失敗事件。
    def commit(self):
        self.commits += 1


# 只保留影響共同契約的區辨案例：型態、重複 Query、簽章與隱藏資料。
class ApiTests(unittest.TestCase):
    # 驗證公開的 PHC 參數及跨入口驗證規則。
    def test_argon2id_phc(self):
        # 產生共用原生 PHC 並由獨立驗證入口確認參數與密碼。
        encoded = core.password_hash("demo1234")
        self.assertRegex(encoded, core.PHC)
        self.assertTrue(core.verify_password("demo1234", encoded))
        self.assertFalse(core.verify_password("other123", encoded))

    # 驗證實際 ASGI 路由的錯誤 envelope、CORS 與尾斜線行為。
    def test_asgi_validation_and_cors(self):
        # 實際走 FastAPI 路由，確認框架預設沒有取代共同驗證與 CORS。
        # 最小 ASGI 呼叫保留原始 request bytes 並蒐集實際 response 事件。
        async def call(method, path, body=b"", headers=()):
            sent = []
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                     "root_path": "", "query_string": b"", "headers": headers,
                     "client": ("127.0.0.1", 12345), "server": ("localhost", 8000)}

            # 只送一個完整請求事件，避免測試改變 Body 語意。
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            # 保存狀態與 Body 事件供逐欄比較。
            async def send(message):
                sent.append(message)

            await app.app(scope, receive, send)
            return sent

        response = asyncio.run(call("POST", "/api/auth/register", b'{"password":12}',
                                    [(b"content-type", b"application/json"),
                                     (b"origin", b"http://localhost:8080")]))
        self.assertEqual(response[0]["status"], 422)
        self.assertEqual(json.loads(response[1]["body"])["error_code"], "E0001")
        self.assertIn((b"access-control-allow-origin", b"http://localhost:8080"),
                      response[0]["headers"])
        options = asyncio.run(call("OPTIONS", "/api/articles"))
        self.assertEqual(options[0]["status"], 204)
        self.assertIn((b"access-control-allow-methods", b"GET, POST, OPTIONS"), options[0]["headers"])
        missing = asyncio.run(call("GET", "/api/articles/"))
        self.assertEqual(missing[0]["status"], 404)
        self.assertEqual(json.loads(missing[1]["body"])["error_code"], "E0004")

    # 驗證原始 Query、JSON 型態、媒體型態與 GET Body 的邊界。
    def test_raw_input_validation(self):
        with self.assertRaises(core.ApiError) as duplicate:
            list_input(Request(query=b"page=1&page=2&per_page=20"))
        self.assertEqual(duplicate.exception.errors["page"], ["此參數不可重複"])
        with self.assertRaises(core.ApiError) as array:
            list_input(Request(query=b"page[]=1&per_page=20"))
        self.assertEqual(array.exception.errors["page"], [core.TYPE])
        with self.assertRaises(core.ApiError) as password:
            json_body(Request(body=json.dumps({"email": "a@example.test", "password": 1234}).encode()),
                      ("email", "password"))
        self.assertEqual(password.exception.errors["password"], [core.TYPE])
        with self.assertRaises(core.ApiError) as media:
            json_body(Request(body=b"{}", content_type="application/jsonish"), ())
        self.assertEqual(media.exception.errors["_body"], ["Request Body 必須為 JSON object"])
        with self.assertRaises(core.ApiError) as body:
            list_input(Request(query=b"page=1&per_page=20", body=b"{}"))
        self.assertEqual(body.exception.errors["_body"], ["此 API 不接受 Request Body"])

    # 內容欄位採 Unicode 碼點與固定空白集合；版本保持 JSON 整數型態。
    def test_content_input_boundaries(self):
        fields = ("title", "body", "version")
        limits = {"title": 200, "body": 20000, "version": 0}
        valid = {"title": "😀" * 200, "body": "\r\n保留\r原文", "version": 1}
        self.assertEqual(json_body(Request(body=json.dumps(valid).encode()), fields, content_limits=limits), valid)
        for value, error in ((True, core.TYPE), ("1", core.TYPE), (0, core.INVALID),
                             (9007199254740992, core.INVALID)):
            invalid = valid | {"version": value}
            with self.subTest(value=value), self.assertRaises(core.ApiError) as failure:
                json_body(Request(body=json.dumps(invalid).encode()), fields, content_limits=limits)
            self.assertEqual(failure.exception.errors["version"], [error])
        for title in (" 　\r\n", "x" * 201, "a\0b"):
            with self.subTest(title=title), self.assertRaises(core.ApiError) as failure:
                json_body(Request(body=json.dumps(valid | {"title": title}).encode()),
                          fields, content_limits=limits)
            self.assertEqual(failure.exception.errors["title"], [core.INVALID])

    # 同值更新不得觸發 SQL；過期同值仍須先回衝突。
    def test_version_and_restore_control(self):
        row = {"id": 3, "author_id": 1, "title": "原題", "body": "原文", "version": 2,
               "visibility": "hidden", "restore_control": "author"}
        connection = Connection([])
        app.save_content(connection, "articles", row, 2, {"title": "原題", "body": "原文"})
        self.assertEqual(connection.queries, [])
        with self.assertRaises(core.ApiError) as stale:
            app.save_content(connection, "articles", row, 1, {"title": "原題", "body": "原文"})
        self.assertEqual(stale.exception.code, "E0201")
        admin = {"id": 9, "role": "admin"}
        self.assertEqual(app.visibility_changes(row, admin, "hidden"),
                         {"visibility": "hidden", "restore_control": "admin"})
        with self.assertRaises(core.ApiError) as forbidden:
            app.visibility_changes(row | {"restore_control": "admin"},
                                   {"id": 1, "role": "member"}, "visible")
        self.assertEqual(forbidden.exception.code, "E0003")

    # 門檻當次應在回 429 前提交失敗事件，且視窗排除左邊界。
    def test_lockout_threshold_commits_before_429(self):
        now = datetime.now(timezone.utc)
        connection = LoginConnection(now)
        request = Request(body=b'{"email":"a@example.test","password":"wrong1"}')
        with patch.object(app, "connect", return_value=connection), \
             patch.object(app, "verify_password", return_value=False):
            error = app.login(request, None)
        self.assertEqual(error.code, "E0105")
        self.assertEqual(error.retry_after, 30)
        self.assertEqual(connection.commits, 1)
        self.assertIn("FOR UPDATE", connection.queries[0][0])
        self.assertIn("FOR UPDATE", connection.queries[1][0])
        window = next(params for sql, params in connection.queries if "SELECT count(*)" in sql)
        self.assertEqual(window[2], now - timedelta(minutes=5))
        self.assertEqual(window[3], now)

    # 驗證固定後端 JWT 的簽章與嚴格到期時間。
    def test_jwt_backend_and_time(self):
        with patch.object(core, "jwt_key", return_value=b"test-key-only-32-bytes-long-000000"):
            token = core.issue_token(1, core.b64url(b"0123456789abcdef"),
                                     int(datetime.now(timezone.utc).timestamp()))
            self.assertEqual(core.verify_token(token)["sub"], "1")
            with self.assertRaises(core.ApiError) as invalid:
                core.verify_token(token[:-1] + ("A" if token[-1] != "A" else "B"))
            self.assertEqual(invalid.exception.code, "E0002")
            expired = core.issue_token(1, core.b64url(b"0123456789abcdef"), 1)
            with self.assertRaises(core.ApiError) as old:
                core.verify_token(expired)
            self.assertEqual(old.exception.code, "E0002")

    # 公開列表只查公開資料，隱藏詳情不可洩漏存在性。
    def test_public_list_sql_and_hidden_article(self):
        connection = Connection([{"count": 1}, []])
        with patch.object(app, "connect", return_value=connection):
            response = app.articles(Request(query=b"page=2&per_page=1"), None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["data"]["pagination"],
                         {"page": 2, "per_page": 1, "total": 1, "total_pages": 1})
        self.assertIn("articles.visibility = 'visible'", connection.queries[0][0])
        self.assertEqual(len(connection.queries), 1)
        hidden = Connection([{"visibility": "hidden", "author_id": 9}])
        with patch.object(app, "connect", return_value=hidden):
            with self.assertRaises(core.ApiError) as missing:
                app.article(Request(), "1")
        self.assertEqual(missing.exception.code, "E0004")

    # 就緒必須同時確認資料表與正式 JWT 金鑰，避免健康但無法登入。
    def test_health_requires_valid_jwt_key(self):
        invalid_db = Connection([])
        with patch.object(app, "connect", return_value=invalid_db), \
             patch.object(app, "jwt_key", side_effect=core.ApiError("E0104")):
            self.assertEqual(app.health().status_code, 503)
        self.assertEqual(len(invalid_db.queries), 1)
        valid_db = Connection([])
        with patch.object(app, "connect", return_value=valid_db), \
             patch.object(app, "jwt_key", return_value=b"test-key"):
            self.assertEqual(app.health().status_code, 200)
        self.assertEqual(len(valid_db.queries), 1)


if __name__ == "__main__":
    unittest.main()
