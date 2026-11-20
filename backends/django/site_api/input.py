"""保留各框架原始 Body／Query 語意的共同 HTTP 輸入驗證。"""

from urllib.parse import unquote_plus, unquote_to_bytes
import re

from .core import (
    ApiError,
    DECIMAL,
    INVALID,
    NULL,
    PASSWORD,
    REQUIRED,
    SPACE,
    TYPE,
    normalize_email,
    reject_non_json_number,
    valid_email,
    valid_nickname,
)
from .dto import PaginationDto


# 先解析原始 Query 鍵，保留重複與不支援欄位；非法 UTF-8 值不能被替代字元吞掉。
def query_values(request, allowed):
    raw = request.META.get("QUERY_STRING", "")
    values = {}
    errors = {}
    seen = set()
    for pair in raw.split("&") if raw else ():
        encoded_name, _, encoded_value = pair.partition("=")
        # 合法 U+FFFD 保留原名；非法 UTF-8 另存 bytes 身分，不與字面百分比名稱混同。
        try:
            name = unquote_plus(encoded_name, errors="strict")
            invalid_name = None
        except UnicodeDecodeError:
            name = encoded_name.upper()
            invalid_name = unquote_to_bytes(encoded_name.replace("+", " "))
        # 陣列式鍵先對回原欄位，才能回型態錯誤或與單值鍵的重複錯誤。
        array_match = re.match(r"(page|per_page|filters\[(?:scope|keyword)\])\[", name)
        canonical = array_match.group(1) if array_match else name
        label = re.sub(r"^filters\[([^]]+)\]$", r"filters.\1", canonical)
        identity = canonical if invalid_name is None else invalid_name
        # 真重複優先；不同身分撞同一 errors 名稱時，後來的未知欄位不能蓋掉已判定的重複。
        if identity in seen:
            errors[label] = ["此參數不可重複"]
        elif label not in errors:
            if array_match and canonical in allowed:
                errors[label] = [TYPE]
            elif canonical not in allowed:
                errors[label] = ["不支援此欄位"]
        seen.add(identity)
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
    for pair in request.META.get("QUERY_STRING", "").split("&"):
        name, _, value = pair.partition("=")
        if unquote_plus(name) == "filters[scope]" and unquote_plus(value) == "management":
            return True
    return False


# 無 Query 的端點也拒絕未知與重複參數，避免框架忽略多餘輸入。
def no_query(request):
    _, errors = query_values(request, set())
    if errors:
        raise ApiError("E0001", errors)


# GET 的非空 Body 一律拒絕，避免 Content-Type 或框架預設改變共同驗證順序。
def no_get_body(request):
    if request.body:
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


# 解析 JSON object 後彙整所有欄位；每欄依必填、null、型態、格式順序只回第一個錯誤。
def json_body(request, fields, empty_allowed=False, content_limits=None, field_types=None):
    if empty_allowed and not request.body:
        return {}
    # 精確比對媒體型態並保留合法參數；近似前綴不能進入 JSON 欄位驗證。
    if not request.body or (request.content_type or "").split(";", 1)[0].strip().lower() != "application/json":
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    try:
        import json
        # bytes 直接給 json.loads 會自動接受 UTF-16／32；共用 API 只接受 UTF-8。
        decoded = request.body.decode("utf-8")
        # 解析前計算原始容器深度；同名鍵被覆蓋後也不能抹掉過深的舊值。
        depth = 0
        quoted = False
        escaped = False
        for character in decoded:
            # 字串內的括號不是結構；反斜線會跳過下一字元，含跳脫引號。
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
        # PHP 等後端把不成對的 UTF-16 surrogate 視為解析錯誤。
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError("E0005") from None
    if not isinstance(body, dict):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    errors = {}
    field_types = field_types or {}
    # JSON 整數不因 Python／其他 runtime 的表示寬度改變型態；數值界線由欄位契約判斷。
    for field in fields:
        if field not in body:
            errors[field] = [REQUIRED]
        elif body[field] is None:
            errors[field] = [NULL]
        elif type(body[field]) is OversizedJsonInteger:
            errors[field] = [INVALID if field == "version" or field_types.get(field) is int else TYPE]
        elif type(body[field]) is not field_types.get(
            field, int if field == "version" and content_limits is not None else str
        ):
            errors[field] = [TYPE]
    # 未知欄位與已知欄位錯誤一起回報；只對通過型態檢查的帳號欄位套格式驗證。
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
    # 內容欄位保留原文；長度用 Python Unicode 碼點，NUL 在進入 PostgreSQL 前拒絕。
    if content_limits is not None:
        for field, limit in content_limits.items():
            if field in errors:
                continue
            value = body[field]
            if field == "version":
                valid = 1 <= value <= 9007199254740991
            elif field == "visibility":
                valid = value in ("visible", "hidden")
            elif field == "status":
                valid = value in ("active", "disabled")
            elif type(value) is int:
                valid = 1 <= value <= limit
            else:
                valid = 1 <= len(value) <= limit and "\0" not in value and any(character not in SPACE for character in value)
            if not valid:
                errors[field] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    return body


# 列表先驗 Body 與 Query，會員關鍵字只裁切 ASCII 空格；管理權門禁由呼叫端先處理。
def list_input(request, allow_scope=False, allow_keyword=False):
    no_get_body(request)
    allowed = {"page", "per_page"}
    if allow_scope:
        allowed.add("filters[scope]")
    if allow_keyword:
        allowed.add("filters[keyword]")
    values, errors = query_values(request, allowed)
    numbers = {}
    # 兩個分頁欄位都驗完才回錯誤，避免第一欄錯誤遮蔽其他欄位的必填或範圍問題。
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
    if allow_keyword and "filters[keyword]" in values and "filters.keyword" not in errors:
        # 只裁切 ASCII 空格；拒絕 NUL 與超長值，同欄既有型態錯誤仍優先保留。
        values["filters[keyword]"] = values["filters[keyword]"].strip(" ")
        if "\0" in values["filters[keyword]"] or len(values["filters[keyword]"]) > 254:
            errors["filters.keyword"] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    # 沿用已驗證數值，避免大量前導零在原字串二次轉換時觸發 Python 上限。
    return numbers["page"], numbers["per_page"], values.get("filters[keyword]" if allow_keyword else "filters[scope]")


# 精確總數與原頁碼組成共同分頁格式，超頁仍保留 total。
def pagination(page, per_page, total) -> PaginationDto:
    return {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    }
