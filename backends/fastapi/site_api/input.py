"""共同 API 的原始 Query、Body 與分頁驗證。"""

import re
from urllib.parse import unquote_plus

from .core import (ApiError, DECIMAL, INVALID, NULL, PASSWORD, REQUIRED, TYPE,
                   SPACE, normalize_email, reject_non_json_number, valid_email,
                   valid_nickname)

# 解析原始 Query 鍵與值，保留重複、陣列形式及非法 UTF-8 的錯誤。
def query_values(request, allowed):
    raw = request.query_string.decode("latin1")
    values = {}
    errors = {}
    for pair in raw.split("&") if raw else ():
        encoded_name, _, encoded_value = pair.partition("=")
        decoded = unquote_plus(encoded_name, errors="replace")
        # 非法 UTF-8 鍵保留原始百分比編碼，避免替代字元與合法欄位撞名。
        name = decoded if "\ufffd" not in decoded else encoded_name.upper()
        # 陣列式鍵先對回原欄位，才能回型態錯誤或與單值鍵的重複錯誤。
        array_match = re.match(r"(page|per_page|filters\[(?:scope|keyword)\])\[", name)
        canonical = array_match.group(1) if array_match else name
        label = re.sub(r"^filters\[([^]]+)\]$", r"filters.\1", canonical)
        # 重複優先於陣列型態，其後才是未知欄位，維持共用錯誤先後。
        if canonical in values:
            errors[label] = ["此參數不可重複"]
        elif array_match and canonical in allowed:
            errors[label] = [TYPE]
        elif canonical not in allowed:
            errors[label] = ["不支援此欄位"]
        # 值採嚴格 UTF-8；遇到非法位元組保留欄位錯誤，不交給資料庫搜尋。
        try:
            values[canonical] = unquote_plus(encoded_value, errors="strict")
        except UnicodeDecodeError:
            values[canonical] = ""
            if canonical in allowed and label not in errors:
                errors[label] = [INVALID]
    return values, errors


# 只把明確的 filters[scope]=management 當管理請求，先執行角色門禁。
def management_requested(request):
    for pair in request.query_string.decode("latin1").split("&"):
        name, _, value = pair.partition("=")
        if unquote_plus(name) == "filters[scope]" and unquote_plus(value) == "management":
            return True
    return False


# 無 Query 的端點也拒絕未知與重複參數，避免框架忽略多餘輸入。
def no_query(request):
    _, errors = query_values(request, set())
    if errors:
        raise ApiError("E0001", errors)


# GET 的任何非空 Body 均為驗證錯誤，不先依 Content-Type 解讀。
def no_get_body(request):
    if request.get_data(cache=True):
        raise ApiError("E0001", {"_body": ["此 API 不接受 Request Body"]})


# URL 整數接受前導零，拒絕符號、空白及超過安全整數的值。
def url_integer(value, field):
    digits = value.lstrip("0")
    if (
        DECIMAL.fullmatch(value) is None or not digits
        or len(digits) > 16
        or (len(digits) == 16 and digits > "9007199254740991")
    ):
        raise ApiError("E0001", {field: [INVALID]})
    return int(digits)


# 為極長 JSON 整數保留來源型態，避免與真正的 JSON 字串混淆。
class OversizedJsonInteger(str):
    pass


# 只轉換最多 19 位的整數；更長的數字用固定標記避免 Python 字數上限與巨量轉換。
def parse_json_integer(value):
    if len(value.lstrip("-")) > 19:
        return OversizedJsonInteger("")
    return int(value)


# 直接解析 UTF-8 JSON，依共用順序驗證帳號、內容與設定欄位型態。
def json_body(request, fields, empty_allowed=False, content_limits=None, field_types=None, numeric_limits=None):
    raw = request.get_data(cache=True)
    if empty_allowed and not raw:
        return {}
    if not raw or (request.content_type or "").split(";", 1)[0].strip().lower() != "application/json":
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    try:
        import json
        decoded = raw.decode("utf-8")
        # 原始容器過深即拒絕，避免同名鍵覆蓋後隱藏過深內容。
        depth = 0
        quoted = escaped = False
        for character in decoded:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth >= 512:
                    raise ApiError("E0005")
            elif character in "]}":
                depth -= 1
        body = json.loads(decoded, parse_int=parse_json_integer, parse_constant=reject_non_json_number)
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError("E0005") from None
    if not isinstance(body, dict):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    errors = {}
    for field in fields:
        if field not in body:
            errors[field] = [REQUIRED]
        elif body[field] is None:
            errors[field] = [NULL]
        elif type(body[field]) is OversizedJsonInteger:
            errors[field] = [INVALID if field == "version" or numeric_limits and field in numeric_limits else TYPE]
        elif type(body[field]) is not (
            field_types[field] if field_types and field in field_types
            else int if field == "version" and content_limits is not None else str
        ):
            errors[field] = [TYPE]
    for field in body:
        if field not in fields:
            errors[field] = ["不支援此欄位"]
    if "email" in fields and "email" not in errors:
        body["email"] = normalize_email(body["email"])
        if not valid_email(body["email"]):
            errors["email"] = [INVALID]
    if "password" in fields and "password" not in errors and PASSWORD.fullmatch(body["password"]) is None:
        errors["password"] = [INVALID]
    if "nickname" in fields and "nickname" not in errors and not valid_nickname(body["nickname"]):
        errors["nickname"] = [INVALID]
    # 會員狀態與其他欄位一起彙整錯誤，避免未知欄位遮蔽狀態格式錯誤。
    if "status" in fields and "status" not in errors and body["status"] not in ("active", "disabled"):
        errors["status"] = [INVALID]
    # 內容原樣保存；只依已核准的碼點、NUL 與空白集合判斷是否合法。
    if content_limits is not None:
        for field, limit in content_limits.items():
            if field in errors:
                continue
            value = body[field]
            if field == "version":
                valid = 1 <= value <= 9007199254740991
            elif field == "visibility":
                valid = value in ("visible", "hidden")
            else:
                valid = 1 <= len(value) <= limit and "\0" not in value and any(char not in SPACE for char in value)
            if not valid:
                errors[field] = [INVALID]
    # 設定整數與 JSON boolean 分開驗型，避免 Python 把 true 當成 1 接受。
    if numeric_limits is not None:
        for field, maximum in numeric_limits.items():
            if field not in errors and not 1 <= body[field] <= maximum:
                errors[field] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    return body


# 列表欄位先驗證完整，再將安全整數傳入分頁 SQL。
def list_input(request, allow_scope=False, allow_keyword=False):
    no_get_body(request)
    allowed = {"page", "per_page"}
    if allow_scope:
        allowed.add("filters[scope]")
    if allow_keyword:
        allowed.add("filters[keyword]")
    values, errors = query_values(request, allowed)
    numbers = {}
    for name in ("page", "per_page"):
        if name not in values:
            errors[name] = [REQUIRED]
        elif name not in errors:
            try:
                number = url_integer(values[name], name)
            except ApiError:
                errors[name] = [INVALID]
            else:
                if name == "per_page" and number > 100:
                    errors[name] = [INVALID]
                else:
                    numbers[name] = number
    if allow_scope and "filters[scope]" in values and "filters.scope" not in errors and values["filters[scope]"] != "management":
        errors["filters.scope"] = [INVALID]
    keyword = values.get("filters[keyword]", "").strip(" ")
    if allow_keyword and "filters[keyword]" in values and "filters.keyword" not in errors and len(keyword) > 254:
        errors["filters.keyword"] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    # 沿用已驗證數值，避免大量前導零在原字串二次轉換時觸發 Python 上限。
    return numbers["page"], numbers["per_page"], keyword if allow_keyword else values.get("filters[scope]")


# 精確總數與原頁碼組成共同分頁格式，超頁仍保留 total。
def pagination(page, per_page, total):
    return {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    }
