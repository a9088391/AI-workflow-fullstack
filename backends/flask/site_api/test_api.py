"""在 Flask HTTP 邊界驗證共同輸入規則與本後端 JWT 身分。"""

import base64
import hashlib
import hmac
import json
import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from .core import ApiError, b64url, issue_token, verify_token
from .views import LOGGER, authenticate_write, create_app, save_content, visibility_changes


# 不接觸共用資料庫；測試無效輸入必須在任何 SQL 前被拒絕。
class FlaskContractTests(unittest.TestCase):
    # 每個案例取得獨立 client，避免請求狀態相互影響。
    def setUp(self):
        self.client = create_app().test_client()

    # 4／16 碼接受且 3／17 碼拒絕，確認格式錯誤不進入 DB。
    def test_password_boundaries(self):
        for password in ("abc", "a" * 17, "測試1234", "ab c"):
            with self.subTest(password=password), patch("site_api.views.connect") as connect:
                response = self.client.post("/api/auth/register", json={
                    "email": "demo@example.test", "password": password, "nickname": "讀者",
                })
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json["errors"]["password"], ["欄位格式或範圍不正確"])
                connect.assert_not_called()
        for password in ("abcd", "a" * 16):
            with self.subTest(password=password), patch("site_api.views.connect") as connect, patch(
                "site_api.views.password_hash", return_value="hash"
            ):
                connection = connect.return_value.__enter__.return_value
                connection.execute.return_value.fetchone.return_value = None
                response = self.client.post("/api/auth/register", json={
                    "email": " Demo@Example.Test ", "password": password, "nickname": "讀者",
                }, headers={"Authorization": "Bearer expired"})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(connection.execute.call_args_list[-1].args[1][0], "demo@example.test")

    # 驗證錯誤依欄位、Query 重複與 GET Body 契約逐字回傳。
    def test_request_validation_precedes_public_database_read(self):
        cases = (
            ("/api/auth/register", b'{"email":"a@b.co","password":"abcd","nickname":"\\u0000"}', "nickname", "欄位格式或範圍不正確"),
            ("/api/auth/register?x=1", b'{}', "x", "不支援此欄位"),
        )
        for path, body, field, message in cases:
            with self.subTest(path=path), patch("site_api.views.connect") as connect:
                response = self.client.post(path, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json["errors"][field], [message])
                connect.assert_not_called()
        with patch("site_api.views.connect") as connect:
            response = self.client.get("/api/articles?page=1&page=2&per_page=20")
            self.assertEqual(response.json["errors"]["page"], ["此參數不可重複"])
            response = self.client.get("/api/articles?page=1&per_page=20", data="x")
            self.assertEqual(response.json["errors"]["_body"], ["此 API 不接受 Request Body"])
            self.assertEqual(response.status_code, 422)
            self.assertEqual(connect.call_count, 2)

    # JWT 必須採 Flask issuer/audience；即使其他後端可用同金鑰重簽也不可跨用。
    def test_jwt_backend_and_claims_are_fixed(self):
        with patch("site_api.core.jwt_key", return_value=b"k" * 32):
            token = issue_token(1, b64url(b"j" * 16), int(time.time()))
            self.assertEqual(verify_token(token)["sub"], "1")
            header, payload, _ = token.split(".")
            claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
            claims["iss"] = "ai-workflow-fullstack:django"
            foreign_payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
            signing_input = f"{header}.{foreign_payload}"
            foreign = signing_input + "." + b64url(hmac.new(b"k" * 32, signing_input.encode(), hashlib.sha256).digest())
            with self.assertRaises(ApiError) as result:
                verify_token(foreign)
            self.assertEqual(result.exception.code, "E0002")

    # 停用帳號先於鎖定處理，且不驗密碼或增加失敗紀錄。
    def test_login_disabled_precedes_lock(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        rows = (
            {"enabled": True},
            {"id": 1, "status": "disabled", "role": "member", "locked_until": now + timedelta(minutes=5)},
            {"now": now},
        )
        # 每次 SQL 呼叫依順序回傳固定列，讓鎖定順序可直接檢查。
        results = [MagicMock() for _ in rows]
        for result, row in zip(results, rows):
            result.fetchone.return_value = row
        connection.execute.side_effect = results
        with patch("site_api.views.connect") as connect, patch("site_api.views.verify_password") as verify:
            connect.return_value.__enter__.return_value = connection
            response = self.client.post("/api/auth/login", json={"email": "demo@example.test", "password": "abcd"})
        self.assertEqual((response.status_code, response.json["error_code"]), (403, "E0102"))
        verify.assert_not_called()
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(len(statements), 3)
        self.assertIn("login_lockout_settings", statements[0])
        self.assertIn("accounts", statements[1])
        self.assertIn("clock_timestamp", statements[2])

    # 門檻當次先提交鎖定；延遲後標頭只回鎖定剩餘秒數。
    def test_login_threshold_uses_remaining_seconds_after_commit(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        account = {
            "id": 1, "status": "active", "role": "member", "locked_until": None,
            "failure_epoch": 1, "password_hash": "phc",
        }
        rows = (
            {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 300},
            account, {"now": now}, None, {"count": 2}, None,
            {"now": now + timedelta(seconds=2)},
        )
        # 寫入 SQL 不讀結果；可讀結果依設定、帳號、時鐘與計數順序提供。
        results = [MagicMock() for _ in rows]
        for result, row in zip(results, rows):
            result.fetchone.return_value = row
        connection.execute.side_effect = results
        with patch("site_api.views.connect") as connect, patch("site_api.views.verify_password", return_value=False):
            connect.return_value.__enter__.return_value = connection
            response = self.client.post(
                "/api/auth/login", json={"email": "demo@example.test", "password": "abcd"},
                headers={"Origin": "http://localhost:8080"},
            )
        self.assertEqual((response.status_code, response.json["error_code"]), (429, "E0105"))
        self.assertEqual(response.headers["Retry-After"], "298")
        self.assertEqual(response.headers["Access-Control-Expose-Headers"], "Retry-After")
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertIn("INSERT INTO login_failures", statements[3])
        self.assertIn("SELECT count(*)", statements[4])
        self.assertIn("UPDATE accounts SET locked_until", statements[5])
        self.assertIn("clock_timestamp", statements[6])
        connection.commit.assert_called_once()
        # 第二次讀時鐘必須排在提交之後，才反映等待與提交耗時。
        calls = connection.mock_calls
        commit_index = next(index for index, call in enumerate(calls) if call[0] == "commit")
        fresh_clock_index = max(index for index, call in enumerate(calls) if call[0] == "execute" and "clock_timestamp" in call.args[0])
        self.assertLess(commit_index, fresh_clock_index)

    # 既有鎖定同樣按提交後的 DB 時鐘計算，不重驗密碼或延長鎖定。
    def test_existing_lock_uses_remaining_seconds(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        rows = (
            {"enabled": True},
            {"id": 1, "status": "active", "role": "member", "locked_until": now + timedelta(seconds=15)},
            {"now": now}, {"now": now + timedelta(seconds=2, milliseconds=400)},
        )
        # 只有設定、帳號與兩次 DB 時鐘查詢；不允許額外寫入。
        results = [MagicMock() for _ in rows]
        for result, row in zip(results, rows):
            result.fetchone.return_value = row
        connection.execute.side_effect = results
        with patch("site_api.views.connect") as connect, patch("site_api.views.verify_password") as verify:
            connect.return_value.__enter__.return_value = connection
            response = self.client.post("/api/auth/login", json={"email": "demo@example.test", "password": "abcd"})
        self.assertEqual((response.status_code, response.json["error_code"]), (429, "E0105"))
        self.assertEqual(response.headers["Retry-After"], "13")
        verify.assert_not_called()
        connection.commit.assert_called_once()
        self.assertEqual(len(connection.execute.call_args_list), 4)

    # 啟動後 INFO 與錯誤記錄須逐請求共用 log_id，重建 app 不加重複 handler。
    def test_api_logs_are_correlated_once(self):
        handlers = len(LOGGER.handlers)
        create_app()
        self.assertEqual(len(LOGGER.handlers), handlers)
        self.assertEqual(LOGGER.level, logging.INFO)
        self.assertFalse(LOGGER.propagate)
        with patch("site_api.views.connect") as connect, self.assertLogs("site_api", level="INFO") as captured:
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = {"count": 0}
            success = self.client.get("/api/articles?page=1&per_page=20")
            business_error = self.client.post("/api/auth/register", json={"password": "privatevalue!"})
            missing = self.client.get("/api/not-found")
            wrong_method = self.client.post("/api/me")
        self.assertEqual(success.status_code, 200)
        self.assertEqual([response.status_code for response in (business_error, missing, wrong_method)], [422, 404, 405])
        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(sum(message.startswith("IN ") for message in messages), 4)
        self.assertEqual(sum(message.startswith("EX ") for message in messages), 3)
        self.assertEqual(sum(message.startswith("OUT ") for message in messages), 4)
        self.assertNotIn("privatevalue!", "\n".join(messages))
        for response in (business_error, missing, wrong_method):
            log_id = response.json["log_id"]
            related = [message for message in messages if f"log_id={log_id}" in message]
            self.assertEqual([message.split()[0] for message in related], ["IN", "EX", "OUT"])

    # 受保護寫入先查身分，欄位型態、空白與唯讀欄位都不能進入目標查詢。
    def test_author_write_auth_and_validation_order(self):
        with patch("site_api.views.connect") as connect, patch(
            "site_api.views.authenticate_write", side_effect=ApiError("E0002")
        ):
            response = self.client.put("/api/articles/bad?x=1", data="bad")
            self.assertEqual((response.status_code, response.json["error_code"]), (401, "E0002"))
            connect.return_value.__enter__.return_value.execute.assert_not_called()
        invalid = (
            ("/api/articles/bad", {"title": "好", "body": "內容", "version": 1}, "article_id", "欄位格式或範圍不正確"),
            ("/api/articles/1", {"title": "\u3000", "body": "內容", "version": 1}, "title", "欄位格式或範圍不正確"),
            ("/api/articles/1", {"title": "好", "body": "內容", "version": True}, "version", "欄位型態不正確"),
            ("/api/articles/1", {"title": "好", "body": "內容", "version": 1, "author_id": 2}, "author_id", "不支援此欄位"),
        )
        for path, body, field, message in invalid:
            with self.subTest(field=field), patch("site_api.views.connect") as connect, patch(
                "site_api.views.authenticate_write", return_value={"id": 1, "role": "member"}
            ):
                response = self.client.put(path, json=body)
                self.assertEqual((response.status_code, response.json["errors"][field]), (422, [message]))
                connect.return_value.__enter__.return_value.execute.assert_not_called()

    # 發文由登入者及 Flask 寫入作者、來源，成功只回空資料而不推測新 ID。
    def test_article_create_fixed_author_and_source(self):
        with patch("site_api.views.connect") as connect, patch(
            "site_api.views.authenticate_write", return_value={"id": 8, "role": "member"}
        ):
            response = self.client.post("/api/articles", json={"title": "原題", "body": "第一行\r\n第二行"})
            connection = connect.return_value.__enter__.return_value
        self.assertEqual((response.status_code, response.json["data"]), (201, {}))
        sql, params = connection.execute.call_args.args
        self.assertIn("INSERT INTO articles", sql)
        self.assertIn("'flask'", sql)
        self.assertEqual(params, (8, "原題", "第一行\r\n第二行"))

    # 隱藏他人內容先回 404，公開他人內容回 403；作者過期與同值各有固定結果。
    def test_article_mutation_order_and_noop(self):
        row = {"id": 7, "author_id": 1, "title": "舊", "body": "內文", "visibility": "visible",
               "restore_control": None, "version": 2}
        for viewer, changes, expected in (
            ({"id": 3, "role": "member"}, {"visibility": "hidden"}, 404),
            ({"id": 3, "role": "member"}, {"visibility": "visible"}, 403),
            ({"id": 1, "role": "member"}, {"version": 1}, 409),
            ({"id": 1, "role": "member"}, {}, 200),
        ):
            with self.subTest(expected=expected, changes=changes), patch("site_api.views.connect") as connect, patch(
                "site_api.views.authenticate_write", return_value=viewer
            ):
                current = row | {key: value for key, value in changes.items() if key == "visibility"}
                if current["visibility"] == "hidden":
                    current["restore_control"] = "author"
                connection = connect.return_value.__enter__.return_value
                connection.execute.return_value.fetchone.return_value = current
                response = self.client.put("/api/articles/7", json={"title": "舊", "body": "內文", "version": changes.get("version", 2)})
                self.assertEqual(response.status_code, expected)
                updates = [call for call in connection.execute.call_args_list if call.args[0].startswith("UPDATE")]
                self.assertEqual(updates, [])

    # 管理員接管須改恢復權；過期同值先 409，正確同值不更新時間。
    def test_restore_control_and_version_save(self):
        row = {"id": 4, "author_id": 1, "visibility": "hidden", "restore_control": "author", "version": 2}
        changes = visibility_changes(row, {"id": 2, "role": "admin"}, "hidden")
        self.assertEqual(changes, {"visibility": "hidden", "restore_control": "admin"})
        self.assertEqual(
            visibility_changes(row, {"id": 1, "role": "admin"}, "hidden"),
            {"visibility": "hidden", "restore_control": "author"},
        )
        with self.assertRaises(ApiError) as no_restore:
            visibility_changes(row | {"restore_control": "admin"}, {"id": 1, "role": "member"}, "visible")
        self.assertEqual(no_restore.exception.code, "E0003")
        with self.assertRaises(ApiError) as stale:
            save_content(MagicMock(), "articles", row, 1, {"visibility": "hidden"})
        self.assertEqual(stale.exception.code, "E0201")
        connection = MagicMock()
        save_content(connection, "articles", row, 2, {"visibility": "hidden"})
        connection.execute.assert_not_called()
        connection.execute.return_value.rowcount = 1
        save_content(connection, "articles", row, 2, changes)
        statement = connection.execute.call_args.args[0]
        self.assertIn("version = version + 1", statement)
        self.assertIn("WHERE id = %s AND version = %s", statement)
        self.assertNotIn("author_id", statement)
        self.assertNotIn("created_backend", statement)
        self.assertEqual(connection.execute.call_args.args[1], ("hidden", "admin", 4, 2))

    # 登出或停用的競爭須在帳號與 session 鎖後重驗，禁止用初次結果寫入。
    def test_write_identity_locks_and_rechecks(self):
        connection = MagicMock()
        identity = ({"id": 8, "role": "member"}, {"jti": "session"})
        with patch("site_api.views.authenticate", side_effect=(identity, ApiError("E0002"))) as auth:
            with self.assertRaises(ApiError) as rejected:
                authenticate_write(MagicMock(), connection)
        self.assertEqual(rejected.exception.code, "E0002")
        self.assertEqual(auth.call_count, 2)
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("accounts", statements[0])
        self.assertIn("login_sessions", statements[1])
        self.assertTrue(all("FOR UPDATE" in statement for statement in statements))

    # 個人列表由 SQL 限作者、精確計數、時間與 ID 降冪，超頁保留原分頁資料。
    def test_my_content_scope_and_over_page(self):
        viewer = {"id": 8, "role": "member"}
        for path, table in (("/api/me/articles", "articles"), ("/api/me/comments", "comments")):
            with self.subTest(table=table), patch("site_api.views.connect") as connect, patch(
                "site_api.views.authenticate", return_value=(viewer, {})
            ):
                connection = connect.return_value.__enter__.return_value
                connection.execute.return_value.fetchone.return_value = {"count": 3}
                connection.execute.return_value.fetchall.return_value = []
                response = self.client.get(path + "?page=9&per_page=2")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["data"], {
                    "list": [], "pagination": {"page": 9, "per_page": 2, "total": 3, "total_pages": 2},
                })
                self.assertEqual(len(connection.execute.call_args_list), 1)
                self.assertIn(f"FROM {table} WHERE author_id = %s", connection.execute.call_args.args[0])
                self.assertEqual(connection.execute.call_args.args[1], (8,))
                # 第一頁應使用同一作者條件及穩定雙鍵排序。
                response = self.client.get(path + "?page=1&per_page=2")
                self.assertEqual(response.status_code, 200)
                sql = connection.execute.call_args.args[0]
                self.assertIn(f"WHERE {table}.author_id = %s", sql)
                self.assertIn(f"ORDER BY {table}.created_at DESC, {table}.id DESC", sql)
                self.assertEqual(connection.execute.call_args.args[1], (8, 2, 0))

    # 父文章下架時，無預覽權回 404；作者預覽權回 403，均不能新增留言。
    def test_comment_create_hidden_parent_order(self):
        for viewer, expected in (({"id": 2, "role": "member"}, 404), ({"id": 1, "role": "member"}, 403)):
            with self.subTest(expected=expected), patch("site_api.views.connect") as connect, patch(
                "site_api.views.authenticate_write", return_value=viewer
            ):
                connection = connect.return_value.__enter__.return_value
                connection.execute.return_value.fetchone.return_value = {"author_id": 1, "visibility": "hidden"}
                response = self.client.post("/api/articles/5/comments", json={"body": "留言"})
                self.assertEqual(response.status_code, expected)
                self.assertEqual(len(connection.execute.call_args_list), 1)
                self.assertIn("FOR UPDATE", connection.execute.call_args.args[0])

    # 修改留言先鎖父文章再鎖留言；父文章隱藏時即使作者也不得改內文。
    def test_comment_edit_hidden_parent_uses_article_then_comment_locks(self):
        connection = MagicMock()
        rows = ({"article_id": 7}, {"id": 7, "visibility": "hidden"},
                {"id": 5, "article_id": 7, "author_id": 1, "visibility": "visible", "version": 1})
        results = [MagicMock() for _ in rows]
        for result, row in zip(results, rows):
            result.fetchone.return_value = row
        connection.execute.side_effect = results
        with patch("site_api.views.connect") as connect, patch(
            "site_api.views.authenticate_write", return_value={"id": 1, "role": "member"}
        ):
            connect.return_value.__enter__.return_value = connection
            response = self.client.put("/api/comments/5", json={"body": "新內文", "version": 1})
        self.assertEqual((response.status_code, response.json["error_code"]), (403, "E0003"))
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(len(statements), 3)
        self.assertNotIn("FOR UPDATE", statements[0])
        self.assertIn("articles", statements[1])
        self.assertIn("FOR UPDATE", statements[1])
        self.assertIn("comments", statements[2])
        self.assertIn("FOR UPDATE", statements[2])

    # 父文章下架不阻止留言作者詳情；他人對公開配對只得 403。
    def test_comment_detail_author_scope(self):
        from datetime import datetime
        now = datetime.now(timezone.utc)
        row = {"id": 5, "author_id": 1, "nickname": "作者", "article_id": 7, "body": "留言",
               "visibility": "hidden", "article_visibility": "hidden", "restore_control": "author",
               "version": 1, "created_backend": "flask", "created_at": now, "updated_at": now}
        for viewer, target, expected in (
            ({"id": 1, "role": "member"}, row, 200),
            ({"id": 2, "role": "member"}, row, 404),
            ({"id": 2, "role": "member"}, row | {"visibility": "visible", "article_visibility": "visible"}, 403),
        ):
            with self.subTest(expected=expected), patch("site_api.views.connect") as connect, patch(
                "site_api.views.authenticate", return_value=(viewer, {})
            ):
                connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = target
                response = self.client.get("/api/comments/5")
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    self.assertEqual(response.json["data"]["article_id"], 7)


if __name__ == "__main__":
    unittest.main()
