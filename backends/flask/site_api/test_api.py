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
from .views import LOGGER, create_app


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


if __name__ == "__main__":
    unittest.main()
