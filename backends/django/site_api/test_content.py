"""內容驗證及同值／過期狀態轉移的窄範圍回歸。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from .core import ApiError
from .views import json_body, save_content, visibility_changes


# 純函式案例隔離共同 JSON 型態與恢復權規則，HTTP 案例另驗資料庫副作用。
class ContentContractTests(unittest.TestCase):
    # 內容保留換行與原文，且拒絕布林版本、全空白及 NUL。
    def test_content_input_boundaries(self):
        cases = [
            ({"body": " \u3000", "version": 1}, {"body": ["欄位格式或範圍不正確"]}),
            ({"body": "ok", "version": True}, {"version": ["欄位型態不正確"]}),
            ({"body": "a\u0000b", "version": 1}, {"body": ["欄位格式或範圍不正確"]}),
        ]
        for payload, errors in cases:
            with self.subTest(payload=payload):
                request = SimpleNamespace(body=json.dumps(payload).encode(), content_type="application/json")
                with self.assertRaises(ApiError) as caught:
                    json_body(request, ("body", "version"), content_limits={"body": 2000, "version": 0})
                self.assertEqual(caught.exception.errors, errors)
        request = SimpleNamespace(body=b'{"body":"a\\r\\nb","version":1}', content_type="application/json")
        self.assertEqual(json_body(request, ("body", "version"), content_limits={"body": 2000, "version": 0})["body"], "a\r\nb")

    # 版本先於同值比較，過期拒絕且正確同值不執行 UPDATE。
    def test_stale_and_same_value(self):
        connection = MagicMock()
        row = {"id": 3, "version": 2, "body": "same"}
        with self.assertRaises(ApiError) as caught:
            save_content(connection, "comments", row, 1, {"body": "same"})
        self.assertEqual(caught.exception.code, "E0201")
        save_content(connection, "comments", row, 2, {"body": "same"})
        connection.execute.assert_not_called()

    # 管理員接管他人隱藏內容後，作者不能以再次下架移除恢復限制。
    def test_restore_control(self):
        author = {"id": 1, "role": "member"}
        admin = {"id": 2, "role": "admin"}
        row = {"author_id": 1, "visibility": "hidden", "restore_control": "author"}
        self.assertEqual(visibility_changes(row, admin, "hidden")["restore_control"], "admin")
        row["restore_control"] = "admin"
        self.assertEqual(visibility_changes(row, author, "hidden")["restore_control"], "admin")
        with self.assertRaises(ApiError) as caught:
            visibility_changes(row, author, "visible")
        self.assertEqual(caught.exception.code, "E0003")
