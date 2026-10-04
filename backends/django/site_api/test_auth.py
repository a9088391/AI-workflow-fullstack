"""用受控登入紀錄驗證 DB 時間與 JWT claims 的精確對照。"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from .core import ApiError, authenticate
from .views import json_body


# 資料庫約束會拒絕半秒紀錄；此測試直接檢查驗證器的額外防線。
class SessionTimeTests(unittest.TestCase):
    # 正常整數秒可登入，任一欄多半秒都必須拒絕，避免截斷比較。
    def test_session_times_match_claims_exactly(self):
        issued = datetime.fromtimestamp(1_600_000_000, timezone.utc)
        claims = {"sub": "42", "jti": "test-jti", "iat": 1_600_000_000, "exp": 1_600_028_800}
        account = {"id": 42, "status": "active"}
        session = {
            "account_id": 42, "backend_id": "django", "issued_at": issued,
            "expires_at": issued + timedelta(hours=8), "revoked_at": None,
        }
        request = SimpleNamespace(headers={"Authorization": "Bearer fixture"})
        connection = MagicMock()

        # 固定 JWT 已驗證的前提，將檢查聚焦在兩次 DB 讀取的時間一致性。
        with patch("site_api.core.verify_token", return_value=claims):
            connection.execute.return_value.fetchone.side_effect = [account, session]
            self.assertEqual(authenticate(request, connection)[0], account)
            for column in ("issued_at", "expires_at"):
                with self.subTest(column=column):
                    altered = session | {column: session[column] + timedelta(milliseconds=500)}
                    connection.execute.return_value.fetchone.side_effect = [account, altered]
                    with self.assertRaises(ApiError) as caught:
                        authenticate(request, connection)
                    self.assertEqual(caught.exception.code, "E0002")


# 直接以原始 JSON 驗證解析深度，避免 HTTP 服務切換時失去回歸保護。
class JsonDepthTests(unittest.TestCase):
    # 被同名鍵覆蓋的深值仍過深；字串括號與正常重複鍵不應誤判。
    def test_raw_depth_preserves_duplicate_key_behavior(self):
        fields = ("email", "password", "nickname")
        prefix = '{"email":"depth@example.test","password":"Abc1234","nickname":'
        deep = prefix + "[" * 511 + "0" + "]" * 511 + ',"nickname":"Reader"}'
        request = SimpleNamespace(body=deep.encode("utf-8"), content_type="application/json")
        with self.assertRaises(ApiError) as caught:
            json_body(request, fields)
        self.assertEqual(caught.exception.code, "E0005")

        # 跳脫引號與大量字串括號由 JSON 解析器處理，仍回原本的未知欄位錯誤。
        quoted = json.dumps({
            "email": "depth@example.test", "password": "Abc1234", "nickname": "Reader",
            "unexpected": "[" * 600 + '"' + "]" * 600,
        })
        request = SimpleNamespace(body=quoted.encode("utf-8"), content_type="application/json")
        with self.assertRaises(ApiError) as caught:
            json_body(request, fields)
        self.assertEqual(caught.exception.code, "E0001")
        self.assertEqual(caught.exception.errors, {"unexpected": ["不支援此欄位"]})

        shallow = prefix + 'null,"nickname":"Reader"}'
        request = SimpleNamespace(body=shallow.encode("utf-8"), content_type="application/json")
        self.assertEqual(json_body(request, fields)["nickname"], "Reader")
