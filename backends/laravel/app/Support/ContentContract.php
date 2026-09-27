<?php

namespace App\Support;

use App\Http\ApiResponse;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use JsonException;
use stdClass;

trait ContentContract
{
    /** 直接檢查原始 Query，以辨認重複參數與巢狀欄位，避免框架靜默取最後值。 */
    private function query(Request $request, bool $pagination, bool $scope = true): array|JsonResponse
    {
        // GET 不接受 Body，先拒絕以維持 Request Body 錯誤的驗證先後。
        if ($request->isMethod('GET') && $request->getContent() !== '') {
            return $this->invalid(['_body' => ['此 API 不接受 Request Body']]);
        }
        $values = [];
        $errors = [];
        $raw = $request->server('QUERY_STRING', '');
        // 原始參數逐一解碼以辨識重複與陣列；無效 UTF-8 名稱改用百分號編碼，讓錯誤鍵可輸出為 JSON。
        if ($raw !== '') {
            foreach (explode('&', $raw) as $pair) {
                [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
                $name = urldecode($name);
                $name = mb_check_encoding($name, 'UTF-8') ? $name : rawurlencode($name);
                $arrayValue = preg_match('/\A(page|per_page|filters\[scope\])\[/', $name, $parts) === 1;
                $canonical = $arrayValue ? $parts[1] : $name;
                $errorName = preg_replace('/\Afilters\[([^\]]+)\]\z/', 'filters.$1', $canonical);
                if (array_key_exists($canonical, $values)) {
                    $errors[$errorName] = ['此參數不可重複'];
                } elseif ($arrayValue && in_array($canonical, $pagination ? ($scope ? ['page', 'per_page', 'filters[scope]'] : ['page', 'per_page']) : [], true)) {
                    $errors[$errorName] = ['欄位型態不正確'];
                } elseif (! in_array($canonical, $pagination ? ($scope ? ['page', 'per_page', 'filters[scope]'] : ['page', 'per_page']) : [], true)) {
                    $errors[$errorName] = ['不支援此欄位'];
                }
                $values[$canonical] = urldecode($value);
            }
        }
        // 列表必填數字與管理範圍在欄位型態及未知名稱判定後驗證，保留每欄先出現的錯誤。
        if ($pagination) {
            foreach (['page', 'per_page'] as $name) {
                if (! array_key_exists($name, $values)) {
                    $errors[$name] = ['此欄位為必填'];
                } elseif (! isset($errors[$name]) && (($number = $this->integer($values[$name])) === null || ($name === 'per_page' && $number > 100))) {
                    $errors[$name] = ['欄位格式或範圍不正確'];
                }
            }
            if ($scope && isset($values['filters[scope]']) && ! isset($errors['filters.scope']) && $values['filters[scope]'] !== 'management') {
                $errors['filters.scope'] = ['欄位格式或範圍不正確'];
            }
        }

        return $errors === [] ? ['page' => $pagination ? (int) $values['page'] : 1, 'per_page' => $pagination ? (int) $values['per_page'] : 20, 'scope' => $values['filters[scope]'] ?? null] : $this->invalid($errors);
    }

    /** JSON 寫入必須是 object；保留原文，但拒絕 PostgreSQL text 無法儲存的 U+0000。 */
    private function body(Request $request, array $fields): stdClass|JsonResponse
    {
        // 先分開判定媒體型態、JSON 語法與 object 形狀，維持各自既定的 422／400 回應。
        $raw = $request->getContent();
        if ($raw === '' || ! str_starts_with(strtolower($request->header('Content-Type', '')), 'application/json')) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            return $this->error(400, 'E0005', '系統暫時無法處理，請稍後再試');
        }
        if (! is_object($body)) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }
        // 逐欄按必填、null、型態、長度與內容順序驗證，只回該欄第一個錯誤。
        $errors = [];
        foreach ($fields as $name => $limit) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif (! is_string($body->$name)) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif (str_contains($body->$name, "\0")
                || mb_strlen($body->$name, 'UTF-8') < 1 || mb_strlen($body->$name, 'UTF-8') > $limit
                || preg_match('/[^\x{0009}-\x{000D}\x{0020}\x{0085}\x{00A0}\x{1680}\x{2000}-\x{200A}\x{2028}-\x{2029}\x{202F}\x{205F}\x{3000}]/u', $body->$name) !== 1) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        // 最後拒絕唯讀或未知欄位，避免它們影響已列欄位的驗證順序。
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $fields)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }

        return $errors === [] ? $body : $this->invalid($errors);
    }

    /** URL 整數接受前導零，但仍受共同安全整數上限約束。 */
    private function integer(string $value): ?int
    {
        if (preg_match('/\A[0-9]+\z/D', $value) !== 1) {
            return null;
        }
        $digits = ltrim($value, '0');
        if ($digits === '' || strlen($digits) > 16 || (strlen($digits) === 16 && strcmp($digits, '9007199254740991') > 0)) {
            return null;
        }

        return (int) $digits;
    }

    /** 業務時間統一以台北時區顯示到秒，保留資料庫中的實際時間點。 */
    private function displayTime(string $time): string
    {
        return CarbonImmutable::parse($time)->setTimezone('Asia/Taipei')->format('Y-m-d H:i:s');
    }

    /** 維持共同成功 envelope；一般寫入回空 object。 */
    private function success(array $data, int $status = 200): JsonResponse
    {
        return response()->json(['success' => true, 'message' => 'OK', 'data' => (object) $data], $status);
    }

    /** 驗證錯誤只回每欄第一則固定文案。 */
    private function invalid(array $errors): JsonResponse
    {
        return $this->error(422, 'E0001', '欄位資料不正確，請確認後再送出', $errors);
    }

    /** 以共同錯誤格式回應，log_id 由外層 middleware 補入。 */
    private function error(int $status, string $code, string $message, ?array $errors = null): JsonResponse
    {
        return ApiResponse::error($status, $code, $message, $errors);
    }
}
