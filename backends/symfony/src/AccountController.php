<?php

namespace App;

use DateTimeImmutable;
use JsonException;
use PDO;
use PDOException;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排註冊、登入、本人資料與登出；共用認證及回應由 HTTP 基底提供。 */
class AccountController extends HttpController
{
    /** 驗證原始 JSON 後建立固定 member/active 帳號；唯一索引裁決並行註冊。 */
    public function register(Request $request): JsonResponse
    {
        // 公開端點不沿用舊憑證；先驗 Query 與全部欄位，格式錯誤不接觸資料庫。
        $this->noQuery($request);
        $body = $this->jsonBody($request, ['email', 'password', 'nickname']);

        $db = $this->database->connection();
        $exists = $db->prepare('SELECT 1 FROM accounts WHERE email = :email');
        $exists->execute(['email' => $body->email]);
        if ($exists->fetchColumn()) {
            throw new ApiFailure('E0001', ['email' => ['此 Email 已註冊']]);
        }
        // 預查只供明確回應；跨後端同時註冊仍由既有唯一索引裁決。
        try {
            $insert = $db->prepare(
                "INSERT INTO accounts (email, password_hash, nickname, role, status) "
                . "VALUES (:email, :hash, :nickname, 'member', 'active')"
            );
            $insert->execute([
                'email' => $body->email,
                'hash' => Rules::hash($body->password),
                'nickname' => $body->nickname,
            ]);
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
        // 格式錯誤不累計失敗；公開登入依本次帳密處理，不先驗舊 JWT。
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
            // 等到設定與帳號鎖取得後才取一次時鐘，後續判定共用同一時刻。
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
            // 只有未停用的一般會員在啟用設定時累計；左開右閉視窗包含這次失敗。
            if (! Rules::verify($body->password, $account->password_hash)) {
                if ($settings->enabled && $account->role === 'member') {
                    $insert = $db->prepare(
                        'INSERT INTO login_failures (account_id, failure_epoch, occurred_at) VALUES (:id, :epoch, :now)'
                    );
                    $insert->execute([
                        'id' => $account->id,
                        'epoch' => $account->failure_epoch,
                        'now' => $this->sqlTime($now),
                    ]);
                    $count = $db->prepare(
                        'SELECT count(*) FROM login_failures WHERE account_id = :id AND failure_epoch = :epoch '
                        . 'AND occurred_at > :start AND occurred_at <= :now'
                    );
                    $count->execute([
                        'id' => $account->id,
                        'epoch' => $account->failure_epoch,
                        'start' => $this->sqlTime($now->modify('-' . (int) $settings->window_minutes . ' minutes')),
                        'now' => $this->sqlTime($now),
                    ]);
                    if ((int) $count->fetchColumn() >= (int) $settings->failure_threshold) {
                        $until = $now->modify('+' . (int) $settings->lock_seconds . ' seconds');
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
            $session = $db->prepare(
                "INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) "
                . "VALUES (:jti, :id, 'symfony', :issued, :expires)"
            );
            $session->execute([
                'jti' => $jti,
                'id' => $account->id,
                'issued' => $this->sqlTime($issued),
                'expires' => $this->sqlTime($expires),
            ]);

            return $this->success([
                'access_token' => $token,
                'token_type' => 'Bearer',
                'expires_at' => $this->displayTime($expires),
                'user' => $this->account($account),
            ]);
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

    /** 同一交易驗證目前身分並只撤銷目前 jti；其他登入不受影響。 */
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

    /** 從原始 Body 區分媒體型態、JSON 語法、object 與欄位錯誤。 */
    private function jsonBody(Request $request, array $fields, bool $emptyAllowed = false): stdClass
    {
        $raw = $request->getContent();
        if ($emptyAllowed && $raw === '') {
            return new stdClass();
        }
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
        // 先收集必填、null、型態及未知欄位，避免後續格式檢查覆蓋第一個錯誤。
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
        if (
            in_array('password', $fields, true)
            && ! isset($errors['password'])
            && ! Rules::validPassword($body->password)
        ) {
            $errors['password'] = ['欄位格式或範圍不正確'];
        }
        if (
            in_array('nickname', $fields, true)
            && ! isset($errors['nickname'])
            && ! Rules::validNickname($body->nickname)
        ) {
            $errors['nickname'] = ['欄位格式或範圍不正確'];
        }
        $this->rejectErrors($errors);

        return $body;
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
}
