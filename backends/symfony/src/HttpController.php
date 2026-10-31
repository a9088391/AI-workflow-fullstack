<?php

namespace App;

use DateTimeImmutable;
use DateTimeZone;
use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 集中帳號與其他端點實際共用的認證、輸入邊界與回應，避免各自偏離契約。 */
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
