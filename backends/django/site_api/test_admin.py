"""管理 API 的輸入邊界與權限先後回歸測試。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from .core import ApiError
from .views import json_body, list_input, login_lockout_settings, member_status, members


# 用原始 Query 與 JSON 保留框架測試客戶端可能正規化掉的錯誤輸入。
class AdminInputTests(unittest.TestCase):
    # 關鍵字只裁切 ASCII 空格；重複、陣列、非法 UTF-8 不能被當成正常搜尋。
    def test_member_keyword_boundaries(self):
        request = SimpleNamespace(body=b"", META={"QUERY_STRING": "page=1&per_page=20&filters%5Bkeyword%5D=++%25_%5C++"})
        self.assertEqual(list_input(request, allow_keyword=True), (1, 20, "%_\\"))
        for query, expected in (
            ("page=1&per_page=20&filters%5Bkeyword%5D=%FF", {"filters.keyword": ["欄位格式或範圍不正確"]}),
            ("page=1&per_page=20&filters%5Bkeyword%5D=a&filters%5Bkeyword%5D=b", {"filters.keyword": ["此參數不可重複"]}),
            ("page=1&per_page=20&filters%5Bkeyword%5D%5B0%5D=a", {"filters.keyword": ["欄位型態不正確"]}),
            ("page=1&per_page=20&filters%5Bkeyword%5D=" + "x" * 255, {"filters.keyword": ["欄位格式或範圍不正確"]}),
        ):
            with self.subTest(query=query):
                request.META["QUERY_STRING"] = query
                with self.assertRaises(ApiError) as caught:
                    list_input(request, allow_keyword=True)
                self.assertEqual(caught.exception.errors, expected)

    # 布林與整數依原始 JSON 區分，整數按業務範圍判定；小數及指數不能湊成整數。
    def test_settings_and_status_body_types(self):
        fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
        types = {"enabled": bool, "window_minutes": int, "failure_threshold": int, "lock_seconds": int}
        limits = {"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400}
        for raw, expected in (
            (b'{"enabled":1,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}', {"enabled": ["欄位型態不正確"]}),
            (b'{"enabled":true,"window_minutes":1.0,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位型態不正確"]}),
            (b'{"enabled":true,"window_minutes":1e0,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位型態不正確"]}),
            (b'{"enabled":true,"window_minutes":0,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位格式或範圍不正確"]}),
            (b'{"enabled":true,"window_minutes":9223372036854775807,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位格式或範圍不正確"]}),
            (b'{"enabled":true,"window_minutes":9223372036854775808,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位格式或範圍不正確"]}),
            (b'{"enabled":true,"window_minutes":-9223372036854775808,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位格式或範圍不正確"]}),
            (b'{"enabled":true,"window_minutes":-9223372036854775809,"failure_threshold":5,"lock_seconds":300}', {"window_minutes": ["欄位格式或範圍不正確"]}),
        ):
            with self.subTest(raw=raw):
                request = SimpleNamespace(body=raw, content_type="application/json")
                with self.assertRaises(ApiError) as caught:
                    json_body(request, fields, content_limits=limits, field_types=types)
                self.assertEqual(caught.exception.errors, expected)
        # 重複 JSON key 依共同解析器採最後一值；仍要求最後值符合型態。
        request = SimpleNamespace(body=b'{"enabled":false,"enabled":true,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}', content_type="application/json")
        self.assertIs(json_body(request, fields, content_limits=limits, field_types=types)["enabled"], True)
        for value, expected in (("active", None), ("paused", ["欄位格式或範圍不正確"]), (True, ["欄位型態不正確"])):
            with self.subTest(status=value):
                request.body = json.dumps({"status": value}).encode()
                if expected is None:
                    self.assertEqual(json_body(request, ("status",), content_limits={"status": 0})["status"], value)
                else:
                    with self.assertRaises(ApiError) as caught:
                        json_body(request, ("status",), content_limits={"status": 0})
                    self.assertEqual(caught.exception.errors["status"], expected)

    # 達到或超過 Python 預設字數限制仍屬已解析的 JSON 整數；最後同名鍵可覆蓋它。
    def test_long_json_integer_keeps_field_and_duplicate_semantics(self):
        fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
        types = {"enabled": bool, "window_minutes": int, "failure_threshold": int, "lock_seconds": int}
        limits = {"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400}
        prefix = b'{"enabled":true,"window_minutes":'
        suffix = b',"failure_threshold":5,"lock_seconds":300}'
        for digits in (4300, 4301):
            with self.subTest(digits=digits):
                oversized = b"9" * digits
                request = SimpleNamespace(body=prefix + oversized + suffix, content_type="application/json")
                with self.assertRaises(ApiError) as caught:
                    json_body(request, fields, content_limits=limits, field_types=types)
                self.assertEqual(caught.exception.code, "E0001")
                self.assertEqual(caught.exception.errors, {"window_minutes": ["欄位格式或範圍不正確"]})
                # 內容 version 與字串型態不同：原始超大整數仍是範圍錯誤。
                request.body = b'{"body":"ok","version":' + oversized + b'}'
                with self.assertRaises(ApiError) as caught:
                    json_body(request, ("body", "version"), content_limits={"body": 2000, "version": 0})
                self.assertEqual(caught.exception.errors, {"version": ["欄位格式或範圍不正確"]})
                request.body = b'{"body":"ok","version":"' + oversized + b'"}'
                with self.assertRaises(ApiError) as caught:
                    json_body(request, ("body", "version"), content_limits={"body": 2000, "version": 0})
                self.assertEqual(caught.exception.errors, {"version": ["欄位型態不正確"]})
                request.body = prefix + oversized + b',"window_minutes":5' + suffix
                self.assertEqual(json_body(request, fields, content_limits=limits, field_types=types)["window_minutes"], 5)

    # 拒絕超長設定值時只發生前置驗權，不進入設定更新交易。
    def test_long_json_integer_does_not_write_settings(self):
        request = SimpleNamespace(
            method="PUT", path="/api/settings/login-lockout", headers={}, META={"QUERY_STRING": ""},
            body=b'{"enabled":true,"window_minutes":' + b"9" * 4301 + b',"failure_threshold":5,"lock_seconds":300}',
            content_type="application/json",
        )
        connection = MagicMock()
        manager = MagicMock()
        manager.__enter__.return_value = connection
        with patch("site_api.views.connect", return_value=manager) as connect, patch(
            "site_api.views.authenticate", return_value=({"id": 1, "role": "admin"}, {})
        ):
            response = login_lockout_settings(request)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.content)["errors"], {"window_minutes": ["欄位格式或範圍不正確"]})
        connect.assert_called_once_with()
        connection.execute.assert_not_called()


# 端點必須先拒絕無權角色，避免 Query、Path 或 Body 錯誤洩漏管理資料。
class AdminAuthorizationTests(unittest.TestCase):
    # 一般會員的錯誤輸入仍回 403，且不執行管理資料查詢。
    def test_role_precedes_input_and_target(self):
        connection = MagicMock()
        manager = MagicMock()
        manager.__enter__.return_value = connection
        request = SimpleNamespace(
            method="GET", path="/api/members", headers={}, body=b"unexpected",
            META={"QUERY_STRING": "page=bad"},
        )
        with patch("site_api.views.connect", return_value=manager), patch(
            "site_api.views.authenticate", return_value=({"id": 1, "role": "member"}, {})
        ):
            response = members(request)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(json.loads(response.content)["error_code"], "E0003")
            connection.execute.assert_not_called()
            request.method = "PUT"
            request.path = "/api/members/not-an-id/status"
            response = member_status(request, "not-an-id")
            self.assertEqual(response.status_code, 403)
            connection.execute.assert_not_called()

    # PostgreSQL text 無法包含 NUL；有效管理員搜尋此字元應得空結果而非 500。
    def test_nul_keyword_has_no_matches(self):
        connection = MagicMock()
        manager = MagicMock()
        manager.__enter__.return_value = connection
        request = SimpleNamespace(
            method="GET", path="/api/members", headers={}, body=b"",
            META={"QUERY_STRING": "page=3&per_page=2&filters%5Bkeyword%5D=%00"},
        )
        with patch("site_api.views.connect", return_value=manager), patch(
            "site_api.views.authenticate", return_value=({"id": 1, "role": "admin"}, {})
        ):
            response = members(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["data"], {
            "list": [], "pagination": {"page": 3, "per_page": 2, "total": 0, "total_pages": 0},
        })
        connection.execute.assert_not_called()
