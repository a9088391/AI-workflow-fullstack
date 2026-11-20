<?php

namespace App;

use DateTimeImmutable;
use DateTimeZone;
use JsonException;
use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 集中端點實際共用的認證、輸入邊界、分頁與回應，避免各自偏離契約。 */
abstract class HttpController
{
    /** 注入既有連線與交易包裝，讓所有端點沿用同一套提交與回滾行為。 */
    public function __construct(protected readonly Database $database)
    {
    }

    /** 可選憑證僅在完全沒有 Authorization 時視為訪客；任何無效值都必須拒絕。 */
    protected function authenticate(Request $request, PDO $db, bool $optional = false): ?array
    {
        if ($optional && ! $request->headers->has('Authorization')) {
            return null;
        }
        if (preg_match('/\ABearer ([^\s]+)\z/Di', $request->headers->get('Authorization', ''), $match) !== 1) {
            throw new ApiFailure('E0002');
        }
        $claims = Jwt::verify($match[1]);
        // 呼叫端須持有交易；帳號→session 的鎖保留到提交，鎖後才讀資料庫時鐘。
        $accountQuery = $db->prepare('SELECT * FROM accounts WHERE id = :id FOR UPDATE');
        $accountQuery->execute(['id' => $claims->sub]);
        $account = $accountQuery->fetch();
        $sessionQuery = $db->prepare('SELECT * FROM login_sessions WHERE jti = :jti FOR UPDATE');
        $sessionQuery->execute(['jti' => $claims->jti]);
        $session = $sessionQuery->fetch();
        $now = $this->now($db)->getTimestamp();
        if (
            $now < $claims->nbf || $now >= $claims->exp || $account === false || $session === false
            || (string) $session->account_id !== $claims->sub || $session->backend_id !== 'symfony'
            || (new DateTimeImmutable($session->issued_at))->getTimestamp() !== $claims->iat
            || (new DateTimeImmutable($session->expires_at))->getTimestamp() !== $claims->exp
        ) {
            throw new ApiFailure('E0002');
        }
        // 先確認登入紀錄身分相符，再讓停用優先於撤銷，避免把停用誤報為逾期。
        if ($account->status === 'disabled') {
            throw new ApiFailure('E0102');
        }
        if ($session->revoked_at !== null) {
            throw new ApiFailure('E0002');
        }

        return [$account, $claims->jti];
    }

    /** 無 Query 的端點仍正規化巢狀欄位並辨識重複名稱，不讓 Symfony 靜默覆蓋。 */
    protected function noQuery(Request $request): void
    {
        $raw = $request->server->get('QUERY_STRING', '');
        if ($raw === '') {
            return;
        }
        $seen = [];
        $errors = [];
        foreach (explode('&', $raw) as $pair) {
            $name = urldecode(explode('=', $pair, 2)[0]);
            // JSON 錯誤鍵須是 UTF-8；保留無效 bytes 的百分比表示以穩定回報欄位。
            $name = mb_check_encoding($name, 'UTF-8') ? $name : rawurlencode($name);
            $arrayValue = preg_match('/\A(page|per_page|filters\[(?:scope|keyword)\])\[/', $name, $parts) === 1;
            $canonical = $arrayValue ? $parts[1] : $name;
            $errorName = preg_replace('/\Afilters\[([^\]]+)\]\z/', 'filters.$1', $canonical);
            $errors[$errorName] = [isset($seen[$canonical]) ? '此參數不可重複' : '不支援此欄位'];
            $seen[$canonical] = true;
        }
        $this->rejectErrors($errors);
    }

    /** GET 不接受非空 Body，避免同一路徑的輸入因伺服器解析方式而不同。 */
    protected function noGetBody(Request $request): void
    {
        if ($request->getContent() !== '') {
            throw new ApiFailure('E0001', ['_body' => ['此 API 不接受 Request Body']]);
        }
    }

    /** 只比對分號前的完整媒體型態，容許大小寫與參數但拒絕 application/jsonish 等前綴。 */
    protected function hasJsonContentType(Request $request): bool
    {
        $mediaType = explode(';', $request->headers->get('Content-Type', ''), 2)[0];

        return strtolower(trim($mediaType)) === 'application/json';
    }

    /** 共同 Body 驗證保留欄位順序及逐欄第一個錯誤，嚴格檢查版本整數。 */
    protected function contentBody(Request $request, array $fields): stdClass
    {
        $raw = $request->getContent();
        if ($raw === '' || ! $this->hasJsonContentType($request)) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiFailure('E0005');
        }
        if (! is_object($body)) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        // 必填、null、型態、格式順序固定；未知與唯讀欄位最後加入明細。
        $errors = [];
        foreach ($fields as $name => $rule) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif (
                ($rule === 'version' && ! is_int($body->$name))
                || ($rule !== 'version' && ! is_string($body->$name))
            ) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif (
                ($rule === 'version' && ($body->$name < 1 || $body->$name > 9007199254740991))
                || ($rule === 'visibility' && ! in_array($body->$name, ['visible', 'hidden'], true))
                || ($rule === 'status' && ! in_array($body->$name, ['active', 'disabled'], true))
                || (is_int($rule) && ! Rules::validContent($body->$name, $rule))
            ) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $fields)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }
        $this->rejectErrors($errors);

        return $body;
    }

    /** 逐一解碼原始 Query，驗證重複、陣列、必填分頁及限定的篩選欄位。 */
    protected function listQuery(Request $request, bool $scopeAllowed, bool $keywordAllowed = false): array
    {
        $this->noGetBody($request);
        $values = [];
        $errors = [];
        $allowed = [
            'page',
            'per_page',
            ...($scopeAllowed ? ['filters[scope]'] : []),
            ...($keywordAllowed ? ['filters[keyword]'] : []),
        ];
        // 直接解析原始 Query，保留重複名稱及單值被包成陣列時的錯誤資訊。
        $raw = $request->server->get('QUERY_STRING', '');
        if ($raw !== '') {
            foreach (explode('&', $raw) as $pair) {
                [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
                $name = urldecode($name);
                $name = mb_check_encoding($name, 'UTF-8') ? $name : rawurlencode($name);
                $arrayValue = preg_match('/\A(page|per_page|filters\[(?:scope|keyword)\])\[/', $name, $parts) === 1;
                $canonical = $arrayValue ? $parts[1] : $name;
                $errorName = preg_replace('/\Afilters\[([^\]]+)\]\z/', 'filters.$1', $canonical);
                if (array_key_exists($canonical, $values)) {
                    $errors[$errorName] = ['此參數不可重複'];
                } elseif ($arrayValue && in_array($canonical, $allowed, true)) {
                    $errors[$errorName] = ['欄位型態不正確'];
                } elseif (! in_array($canonical, $allowed, true)) {
                    $errors[$errorName] = ['不支援此欄位'];
                }
                $values[$canonical] = urldecode($value);
            }
        }
        // 分頁先驗必填及範圍，再驗端點明確允許的篩選值。
        foreach (['page', 'per_page'] as $name) {
            if (! array_key_exists($name, $values)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif (
                ! isset($errors[$name])
                && (($number = $this->positiveInteger($values[$name])) === null
                    || ($name === 'per_page' && $number > 100))
            ) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        if (
            $scopeAllowed && isset($values['filters[scope]'])
            && ! isset($errors['filters.scope']) && $values['filters[scope]'] !== 'management'
        ) {
            $errors['filters.scope'] = ['欄位格式或範圍不正確'];
        }
        if ($keywordAllowed && isset($values['filters[keyword]']) && ! isset($errors['filters.keyword'])) {
            // 關鍵字只去除 ASCII 空格；無效 UTF-8 與 NUL 不可送入 PostgreSQL text。
            $keyword = trim($values['filters[keyword]'], ' ');
            if (
                ! mb_check_encoding($keyword, 'UTF-8')
                || str_contains($keyword, "\0") || mb_strlen($keyword, 'UTF-8') > 254
            ) {
                $errors['filters.keyword'] = ['欄位格式或範圍不正確'];
            }
        }
        $this->rejectErrors($errors);

        return ['page' => (int) $values['page'], 'per_page' => (int) $values['per_page'],
            'scope' => $values['filters[scope]'] ?? null,
            'keyword' => isset($values['filters[keyword]']) ? trim($values['filters[keyword]'], ' ') : null];
    }

    /** URL 整數接受前導零，但值須落在 JavaScript 安全整數範圍。 */
    private function positiveInteger(string $value): ?int
    {
        if (preg_match('/\A[0-9]+\z/D', $value) !== 1) {
            return null;
        }
        $digits = ltrim($value, '0');
        if (
            $digits === '' || strlen($digits) > 16
            || (strlen($digits) === 16 && strcmp($digits, '9007199254740991') > 0)
        ) {
            return null;
        }

        return (int) $digits;
    }

    /** 將 Path ID 的格式錯誤固定回傳對應欄位，不查詢不存在的資源。 */
    protected function id(string $value, string $field): int
    {
        $id = $this->positiveInteger($value);
        if ($id === null) {
            throw new ApiFailure('E0001', [$field => ['欄位格式或範圍不正確']]);
        }

        return $id;
    }

    /** 精確 count 後使用穩定排序；巨大合法頁碼直接回空頁，避免 offset 溢位。 */
    protected function pageRows(PDO $db, string $sql, array $input, int $total, array $parameters = []): array
    {
        $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1
            ? null
            : ($input['page'] - 1) * $input['per_page'];
        if ($offset === null || $offset >= $total) {
            return [];
        }
        $query = $db->prepare($sql . ' LIMIT :limit OFFSET :offset');
        foreach ($parameters as $name => $value) {
            $query->bindValue(':' . $name, $value, is_int($value) ? PDO::PARAM_INT : PDO::PARAM_STR);
        }
        $query->bindValue(':limit', $input['per_page'], PDO::PARAM_INT);
        $query->bindValue(':offset', $offset, PDO::PARAM_INT);
        $query->execute();

        return $query->fetchAll();
    }

    /** 分頁保留請求頁碼與範圍內精確總數，空集合的總頁數為零。 */
    protected function pagination(array $input, int $total): array
    {
        return [
            'page' => $input['page'],
            'per_page' => $input['per_page'],
            'total' => $total,
            'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0),
        ];
    }

    /** 一次回傳呼叫端依優先順序整理的欄位錯誤，空集合則繼續處理。 */
    protected function rejectErrors(array $errors): void
    {
        if ($errors !== []) {
            throw new ApiFailure('E0001', $errors);
        }
    }

    /** 取得鎖之後的實際資料庫時間，避免等鎖期間使用過期的交易開始時間。 */
    protected function now(PDO $db): DateTimeImmutable
    {
        return new DateTimeImmutable($db->query('SELECT clock_timestamp()')->fetchColumn());
    }

    /** SQL 參數保留微秒與時區，讓滑動視窗維持左開右閉的精確邊界。 */
    protected function sqlTime(DateTimeImmutable $time): string
    {
        return $time->format('Y-m-d H:i:s.uP');
    }

    /** 對外時間統一轉為台北秒級字串，JWT 仍使用 UTC Unix 秒。 */
    protected function displayTime(DateTimeImmutable|string $time): string
    {
        return ($time instanceof DateTimeImmutable ? $time : new DateTimeImmutable($time))
            ->setTimezone(new DateTimeZone('Asia/Taipei'))
            ->format('Y-m-d H:i:s');
    }

    /** 只公開核准的帳號欄位，不洩漏雜湊、失敗計數或登入紀錄。 */
    protected function account(stdClass $account): array
    {
        return [
            'id' => (int) $account->id,
            'email' => $account->email,
            'nickname' => $account->nickname,
            'role' => $account->role,
            'status' => $account->status,
            'created_at' => $this->displayTime($account->created_at),
        ];
    }

    /** 成功資料固定為 object，包含登出的空 data。 */
    protected function success(array $data, int $status = 200): JsonResponse
    {
        return new JsonResponse(['success' => true, 'message' => 'OK', 'data' => (object) $data], $status);
    }

    /** 共同錯誤逐字對齊其他後端，log_id 由前端控制器於實際請求時補入。 */
    public static function failure(ApiFailure $failure, ?string $logId = null): JsonResponse
    {
        [$status, $message] = match ($failure->codeName) {
            'E0001' => [422, '欄位資料不正確，請確認後再送出'],
            'E0002' => [401, '登入已逾期，請重新登入'],
            'E0003' => [403, '沒有權限使用此功能'],
            'E0004' => [404, '找不到指定資料或 API'],
            'E0005' => [400, '系統暫時無法處理，請稍後再試'],
            'E0101' => [401, '帳號或密碼錯誤'],
            'E0102' => [403, '此帳號已停用，請聯絡系統管理員'],
            'E0104' => [500, '後台認證設定異常，請聯絡系統管理員'],
            'E0105' => [429, '登入失敗次數過多，請稍後再試'],
            'E0201' => [409, '內容已更新，請確認最新內容後再送出'],
            default => [500, '系統暫時無法處理，請稍後再試'],
        };
        // HTTP 例外保留原狀態；其餘錯誤只輸出安全文案與必要追查資訊。
        if ($failure->codeName === 'E0005' && $failure->httpStatus !== null) {
            $status = $failure->httpStatus;
        }
        $body = ['success' => false, 'message' => $message, 'error_code' => $failure->codeName];
        if ($failure->errors !== null) {
            $body['errors'] = (object) $failure->errors;
        }
        $body['log_id'] = $logId;
        $response = new JsonResponse($body, $status);
        // 鎖定回應沿用同一剩餘秒數，供前端倒數；CORS expose 由入口統一處理。
        if ($failure->retryAfter !== null) {
            $response->headers->set('Retry-After', (string) $failure->retryAfter);
        }

        return $response;
    }
}
