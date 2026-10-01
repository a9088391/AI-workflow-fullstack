<?php

namespace App;

use DateTimeImmutable;
use DateTimeZone;
use JsonException;
use PDO;
use PDOException;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 七個核准端點共用原始輸入驗證與目前身分查核，直接讀寫既有 PostgreSQL 結構。 */
class ApiController
{
    /** 共用同一資料庫連線與交易包裝，避免各端點自行決定鎖順序。 */
    public function __construct(private readonly Database $database)
    {
    }

    /** 驗證原始 JSON 後建立固定 member/active 帳號；唯一索引裁決並行註冊。 */
    public function register(Request $request): JsonResponse
    {
        $this->noQuery($request);
        $body = $this->jsonBody($request, ['email', 'password', 'nickname']);

        $db = $this->database->connection();
        $exists = $db->prepare('SELECT 1 FROM accounts WHERE email = :email');
        $exists->execute(['email' => $body->email]);
        if ($exists->fetchColumn()) {
            throw new ApiFailure('E0001', ['email' => ['此 Email 已註冊']]);
        }
        try {
            $insert = $db->prepare("INSERT INTO accounts (email, password_hash, nickname, role, status) VALUES (:email, :hash, :nickname, 'member', 'active')");
            $insert->execute(['email' => $body->email, 'hash' => Rules::hash($body->password), 'nickname' => $body->nickname]);
        } catch (PDOException $exception) {
            if ($exception->getCode() === '23505') {
                throw new ApiFailure('E0001', ['email' => ['此 Email 已註冊']]);
            }
            throw $exception;
        }

        return $this->success([], 201);
    }

    /** 依設定列→帳號列取鎖，使用鎖後的資料庫時鐘裁決失敗視窗及八小時登入。 */
    public function login(Request $request): JsonResponse
    {
        $this->noQuery($request);
        $body = $this->jsonBody($request, ['email', 'password']);

        // 業務錯誤以正常回傳提交；資料庫／金鑰例外才回滾整筆登入交易。
        return $this->database->transaction(function (PDO $db) use ($body): JsonResponse {
            $settings = $db->query('SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE')->fetch();
            if ($settings === false) {
                throw new ApiFailure('E0104');
            }
            $select = $db->prepare('SELECT * FROM accounts WHERE email = :email FOR UPDATE');
            $select->execute(['email' => $body->email]);
            $account = $select->fetch();
            $now = $this->now($db);
            if ($account === false) {
                return $this->businessError('E0101');
            }
            if ($account->status === 'disabled') {
                return $this->businessError('E0102');
            }

            // 停用優先；已到期或設定關閉的舊鎖須先清除並重設失敗世代。
            if ($account->role === 'member' && $account->locked_until !== null) {
                $until = new DateTimeImmutable($account->locked_until);
                if ($settings->enabled && $now < $until) {
                    return $this->businessError('E0105', $this->secondsLeft($now, $until));
                }
                $account->failure_epoch++;
                $clear = $db->prepare('UPDATE accounts SET failure_epoch = :epoch, locked_until = NULL WHERE id = :id');
                $clear->execute(['epoch' => $account->failure_epoch, 'id' => $account->id]);
            }
            if (! Rules::verify($body->password, $account->password_hash)) {
                if ($settings->enabled && $account->role === 'member') {
                    $insert = $db->prepare('INSERT INTO login_failures (account_id, failure_epoch, occurred_at) VALUES (:id, :epoch, :now)');
                    $insert->execute(['id' => $account->id, 'epoch' => $account->failure_epoch, 'now' => $this->sqlTime($now)]);
                    $count = $db->prepare('SELECT count(*) FROM login_failures WHERE account_id = :id AND failure_epoch = :epoch AND occurred_at > :start AND occurred_at <= :now');
                    $count->execute(['id' => $account->id, 'epoch' => $account->failure_epoch, 'start' => $this->sqlTime($now->modify('-'.(int) $settings->window_minutes.' minutes')), 'now' => $this->sqlTime($now)]);
                    if ((int) $count->fetchColumn() >= (int) $settings->failure_threshold) {
                        $until = $now->modify('+'.(int) $settings->lock_seconds.' seconds');
                        $lock = $db->prepare('UPDATE accounts SET locked_until = :until WHERE id = :id');
                        $lock->execute(['until' => $this->sqlTime($until), 'id' => $account->id]);

                        return $this->businessError('E0105', $this->secondsLeft($now, $until));
                    }
                }

                return $this->businessError('E0101');
            }

            // session 與重設計數必須一起提交；金鑰異常時不得留下部分登入。
            $issued = $now->setTime((int) $now->format('H'), (int) $now->format('i'), (int) $now->format('s'));
            $expires = $issued->modify('+8 hours');
            $jti = Jwt::encode(random_bytes(16));
            $token = Jwt::issue((int) $account->id, $jti, $issued->getTimestamp());
            $reset = $db->prepare('UPDATE accounts SET failure_epoch = :epoch, locked_until = NULL WHERE id = :id');
            $reset->execute(['epoch' => $account->failure_epoch + 1, 'id' => $account->id]);
            $session = $db->prepare("INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) VALUES (:jti, :id, 'symfony', :issued, :expires)");
            $session->execute(['jti' => $jti, 'id' => $account->id, 'issued' => $this->sqlTime($issued), 'expires' => $this->sqlTime($expires)]);

            return $this->success(['access_token' => $token, 'token_type' => 'Bearer', 'expires_at' => $this->displayTime($expires), 'user' => $this->account($account)]);
        });
    }

    /** 驗證目前身分後只回公開 Account 欄位，不延長原登入期限。 */
    public function me(Request $request): JsonResponse
    {
        // 在同一交易查核目前帳號與 session，停用或撤銷先提交時不能讀到舊身分。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            [$account] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $this->noGetBody($request);

            return $this->success($this->account($account));
        });
    }

    /** 同一交易再次驗證並只撤銷目前 jti；其他登入不受影響。 */
    public function logout(Request $request): JsonResponse
    {
        // 認證與撤銷共用交易，任何失敗都不能回報登出完成。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            [, $jti] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $this->jsonBody($request, [], true);
            $revoke = $db->prepare('UPDATE login_sessions SET revoked_at = :now WHERE jti = :jti');
            $revoke->execute(['now' => $this->sqlTime($this->now($db)), 'jti' => $jti]);

            return $this->success([]);
        });
    }

    /** 公開列表只含 visible；明確 management 範圍先驗管理員再驗 Query。 */
    public function articles(Request $request): JsonResponse
    {
        // 管理範圍門禁先於 Query；讀取時保留憑證狀態到回應形成。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $management = $this->managementRequested($request);
            $identity = $this->authenticate($request, $db, ! $management);
            $viewer = $identity[0] ?? null;
            if ($management && $viewer?->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $input = $this->listQuery($request, true);
            if ($input['scope'] === 'management' && $viewer?->role !== 'admin') {
                throw new ApiFailure('E0003');
            }

            $where = $input['scope'] === 'management' ? '' : " WHERE articles.visibility = 'visible'";
            $from = ' FROM articles JOIN accounts ON accounts.id = articles.author_id'.$where;
            $total = (int) $db->query('SELECT count(*)'.$from)->fetchColumn();
            $rows = $this->pageRows($db, 'SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname'.$from.' ORDER BY articles.created_at DESC, articles.id DESC', $input, $total);
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->articleResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 先查核可選 JWT 與參數，再依作者／管理員資格決定下架文章可讀性。 */
    public function article(Request $request, string $article_id): JsonResponse
    {
        // 在同一交易取得目前身分，再以可讀性隱藏不應曝光的下架內容。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            $identity = $this->authenticate($request, $db, true);
            $viewer = $identity[0] ?? null;
            $this->noGetBody($request);
            $this->noQuery($request);
            $id = $this->id($article_id, 'article_id');
            $select = $db->prepare('SELECT articles.id, articles.author_id, articles.title, articles.body, articles.visibility, articles.restore_control, articles.version, articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname FROM articles JOIN accounts ON accounts.id = articles.author_id WHERE articles.id = :id');
            $select->execute(['id' => $id]);
            $article = $select->fetch();
            if ($article === false || ($article->visibility === 'hidden' && $viewer?->role !== 'admin' && (int) ($viewer?->id ?? 0) !== (int) $article->author_id)) {
                throw new ApiFailure('E0004');
            }

            return $this->success($this->articleResource($article, $viewer));
        });
    }

    /** 父文章先判定可讀；預覽下架文章只列 visible 留言，管理員可見個別下架留言。 */
    public function comments(Request $request, string $article_id): JsonResponse
    {
        // 父文章可讀資格與留言可見性在同一讀取流程中決定。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            $identity = $this->authenticate($request, $db, true);
            $viewer = $identity[0] ?? null;
            $input = $this->listQuery($request, false);
            $id = $this->id($article_id, 'article_id');
            $parent = $db->prepare('SELECT author_id, visibility FROM articles WHERE id = :id');
            $parent->execute(['id' => $id]);
            $article = $parent->fetch();
            if ($article === false || ($article->visibility === 'hidden' && $viewer?->role !== 'admin' && (int) ($viewer?->id ?? 0) !== (int) $article->author_id)) {
                throw new ApiFailure('E0004');
            }

            $where = ' WHERE comments.article_id = :id'.($viewer?->role === 'admin' ? '' : " AND comments.visibility = 'visible'");
            $from = ' FROM comments JOIN accounts ON accounts.id = comments.author_id'.$where;
            $count = $db->prepare('SELECT count(*)'.$from);
            $count->execute(['id' => $id]);
            $total = (int) $count->fetchColumn();
            $rows = $this->pageRows($db, 'SELECT comments.id, comments.author_id, comments.article_id, comments.body, comments.visibility, comments.restore_control, comments.version, comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname'.$from.' ORDER BY comments.created_at DESC, comments.id DESC', $input, $total, ['id' => $id]);
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->commentResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 從原始 Query 找明確管理範圍，讓角色門禁先於其他錯誤。 */
    private function managementRequested(Request $request): bool
    {
        foreach (explode('&', $request->server->get('QUERY_STRING', '')) as $pair) {
            [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
            if (urldecode($name) === 'filters[scope]' && urldecode($value) === 'management') {
                return true;
            }
        }

        return false;
    }

    /** 逐一解碼原始 Query，驗證重複、陣列、必填分頁及 scope。 */
    private function listQuery(Request $request, bool $scopeAllowed): array
    {
        $this->noGetBody($request);
        $values = [];
        $errors = [];
        $allowed = $scopeAllowed ? ['page', 'per_page', 'filters[scope]'] : ['page', 'per_page'];
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
        foreach (['page', 'per_page'] as $name) {
            if (! array_key_exists($name, $values)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif (! isset($errors[$name]) && (($number = $this->positiveInteger($values[$name])) === null || ($name === 'per_page' && $number > 100))) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        if ($scopeAllowed && isset($values['filters[scope]']) && ! isset($errors['filters.scope']) && $values['filters[scope]'] !== 'management') {
            $errors['filters.scope'] = ['欄位格式或範圍不正確'];
        }
        $this->rejectErrors($errors);

        return ['page' => (int) $values['page'], 'per_page' => (int) $values['per_page'], 'scope' => $values['filters[scope]'] ?? null];
    }

    /** URL 整數接受前導零，但值須落在 JavaScript 安全整數範圍。 */
    private function positiveInteger(string $value): ?int
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

    /** 將 Path ID 的格式錯誤固定回傳對應欄位，不查詢不存在的資源。 */
    private function id(string $value, string $field): int
    {
        $id = $this->positiveInteger($value);
        if ($id === null) {
            throw new ApiFailure('E0001', [$field => ['欄位格式或範圍不正確']]);
        }

        return $id;
    }

    /** 精確 count 後使用穩定排序；巨大合法頁碼直接回空頁，避免 offset 溢位。 */
    private function pageRows(PDO $db, string $sql, array $input, int $total, array $parameters = []): array
    {
        $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1 ? null : ($input['page'] - 1) * $input['per_page'];
        if ($offset === null || $offset >= $total) {
            return [];
        }
        $query = $db->prepare($sql.' LIMIT :limit OFFSET :offset');
        foreach ($parameters as $name => $value) {
            $query->bindValue(':'.$name, $value, PDO::PARAM_INT);
        }
        $query->bindValue(':limit', $input['per_page'], PDO::PARAM_INT);
        $query->bindValue(':offset', $offset, PDO::PARAM_INT);
        $query->execute();

        return $query->fetchAll();
    }

    /** 分頁保留請求頁碼與範圍內精確總數，空集合的總頁數為零。 */
    private function pagination(array $input, int $total): array
    {
        return ['page' => $input['page'], 'per_page' => $input['per_page'], 'total' => $total, 'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0)];
    }

    /** 文章只回共同欄位與公開作者資料，恢復權依目前身分即時計算。 */
    private function articleResource(stdClass $article, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $article->author_id;

        return ['id' => (int) $article->id, 'title' => $article->title, 'body' => $article->body,
            'author' => ['id' => (int) $article->author_id, 'nickname' => $article->nickname],
            'version' => (int) $article->version, 'created_backend' => $article->created_backend,
            'created_at' => $this->displayTime($article->created_at), 'updated_at' => $this->displayTime($article->updated_at),
            'visibility' => $article->visibility,
            'can_restore' => $article->visibility === 'hidden' && ($viewer?->role === 'admin' || ($own && $article->restore_control === 'author'))];
    }

    /** 留言只回共同欄位與 article_id，不用父文章內容擴張可讀範圍。 */
    private function commentResource(stdClass $comment, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $comment->author_id;

        return ['id' => (int) $comment->id, 'body' => $comment->body,
            'author' => ['id' => (int) $comment->author_id, 'nickname' => $comment->nickname],
            'article_id' => (int) $comment->article_id, 'version' => (int) $comment->version,
            'created_backend' => $comment->created_backend,
            'created_at' => $this->displayTime($comment->created_at), 'updated_at' => $this->displayTime($comment->updated_at),
            'visibility' => $comment->visibility,
            'can_restore' => $comment->visibility === 'hidden' && ($viewer?->role === 'admin' || ($own && $comment->restore_control === 'author'))];
    }

    /** 可選憑證僅在完全沒有 Authorization 時視為訪客；任何無效值都必須拒絕。 */
    private function authenticate(Request $request, PDO $db, bool $optional = false): ?array
    {
        if ($optional && ! $request->headers->has('Authorization')) {
            return null;
        }
        if (preg_match('/\ABearer ([^\s]+)\z/Di', $request->headers->get('Authorization', ''), $match) !== 1) {
            throw new ApiFailure('E0002');
        }
        $claims = Jwt::verify($match[1]);
        // 帳號→session 的鎖順序與 Laravel 一致；鎖後才讀資料庫時鐘。
        $accountQuery = $db->prepare('SELECT * FROM accounts WHERE id = :id FOR UPDATE');
        $accountQuery->execute(['id' => $claims->sub]);
        $account = $accountQuery->fetch();
        $sessionQuery = $db->prepare('SELECT * FROM login_sessions WHERE jti = :jti FOR UPDATE');
        $sessionQuery->execute(['jti' => $claims->jti]);
        $session = $sessionQuery->fetch();
        $now = $this->now($db)->getTimestamp();
        if ($now < $claims->nbf || $now >= $claims->exp || $account === false || $session === false
            || (string) $session->account_id !== $claims->sub || $session->backend_id !== 'symfony'
            || (new DateTimeImmutable($session->issued_at))->getTimestamp() !== $claims->iat
            || (new DateTimeImmutable($session->expires_at))->getTimestamp() !== $claims->exp) {
            throw new ApiFailure('E0002');
        }
        if ($account->status === 'disabled') {
            throw new ApiFailure('E0102');
        }
        if ($session->revoked_at !== null) {
            throw new ApiFailure('E0002');
        }

        return [$account, $claims->jti];
    }

    /** 從原始 Body 區分媒體型態、JSON 語法、object 與欄位錯誤。 */
    private function jsonBody(Request $request, array $fields, bool $emptyAllowed = false): stdClass
    {
        $raw = $request->getContent();
        if ($emptyAllowed && $raw === '') {
            return new stdClass();
        }
        if ($raw === '' || ! str_starts_with(strtolower($request->headers->get('Content-Type', '')), 'application/json')) {
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
        $errors = [];
        foreach ($fields as $field) {
            if (! property_exists($body, $field)) {
                $errors[$field] = ['此欄位為必填'];
            } elseif ($body->$field === null) {
                $errors[$field] = ['此欄位不可為 null'];
            } elseif (! is_string($body->$field)) {
                $errors[$field] = ['欄位型態不正確'];
            }
        }
        foreach ($body as $field => $_) {
            if (! in_array($field, $fields, true)) {
                $errors[$field] = ['不支援此欄位'];
            }
        }
        // 帳號格式與必填/null/型態錯誤一次回報；各欄只保留最先發生的錯誤。
        if (in_array('email', $fields, true) && ! isset($errors['email'])) {
            $body->email = Rules::email($body->email);
            if (! Rules::validEmail($body->email)) {
                $errors['email'] = ['欄位格式或範圍不正確'];
            }
        }
        if (in_array('password', $fields, true) && ! isset($errors['password']) && ! Rules::validPassword($body->password)) {
            $errors['password'] = ['欄位格式或範圍不正確'];
        }
        if (in_array('nickname', $fields, true) && ! isset($errors['nickname']) && ! Rules::validNickname($body->nickname)) {
            $errors['nickname'] = ['欄位格式或範圍不正確'];
        }
        $this->rejectErrors($errors);

        return $body;
    }

    /** 無 Query 的端點仍正規化巢狀欄位並辨識重複名稱，不讓 Symfony 靜默覆蓋。 */
    private function noQuery(Request $request): void
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
    private function noGetBody(Request $request): void
    {
        if ($request->getContent() !== '') {
            throw new ApiFailure('E0001', ['_body' => ['此 API 不接受 Request Body']]);
        }
    }

    /** 每欄只保留第一個共同驗證錯誤，空集合則繼續處理。 */
    private function rejectErrors(array $errors): void
    {
        if ($errors !== []) {
            throw new ApiFailure('E0001', $errors);
        }
    }

    /** 取得鎖之後的實際資料庫時間，避免等鎖期間使用過期的交易開始時間。 */
    private function now(PDO $db): DateTimeImmutable
    {
        return new DateTimeImmutable($db->query('SELECT clock_timestamp()')->fetchColumn());
    }

    /** SQL 參數保留微秒與時區，讓滑動視窗維持左開右閉的精確邊界。 */
    private function sqlTime(DateTimeImmutable $time): string
    {
        return $time->format('Y-m-d H:i:s.uP');
    }

    /** 對外時間統一轉為台北秒級字串，JWT 仍使用 UTC Unix 秒。 */
    private function displayTime(DateTimeImmutable|string $time): string
    {
        return ($time instanceof DateTimeImmutable ? $time : new DateTimeImmutable($time))->setTimezone(new DateTimeZone('Asia/Taipei'))->format('Y-m-d H:i:s');
    }

    /** 只公開核准的帳號欄位，不洩漏雜湊、失敗計數或登入紀錄。 */
    private function account(stdClass $account): array
    {
        return ['id' => (int) $account->id, 'email' => $account->email, 'nickname' => $account->nickname, 'role' => $account->role, 'status' => $account->status, 'created_at' => $this->displayTime($account->created_at)];
    }

    /** 依既有鎖到期時間向上取整秒數，重試不延長期限。 */
    private function secondsLeft(DateTimeImmutable $now, DateTimeImmutable $until): int
    {
        return max(1, (int) ceil(((float) $until->format('U.u')) - ((float) $now->format('U.u'))));
    }

    /** 需保存失敗事件的認證錯誤以正常交易結果回傳，避免例外自動回滾。 */
    private function businessError(string $code, ?int $retry = null): JsonResponse
    {
        return self::failure(new ApiFailure($code, null, $retry));
    }

    /** 成功資料固定為 object，包含登出的空 data。 */
    private function success(array $data, int $status = 200): JsonResponse
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
            default => [500, '系統暫時無法處理，請稍後再試'],
        };
        if ($failure->codeName === 'E0005' && $failure->httpStatus !== null) {
            $status = $failure->httpStatus;
        }
        $body = ['success' => false, 'message' => $message, 'error_code' => $failure->codeName];
        if ($failure->errors !== null) {
            $body['errors'] = (object) $failure->errors;
        }
        $body['log_id'] = $logId;
        $response = new JsonResponse($body, $status);
        if ($failure->retryAfter !== null) {
            $response->headers->set('Retry-After', (string) $failure->retryAfter);
        }

        return $response;
    }
}
