<?php

namespace App;

use PDO;
use Throwable;

/** 直接使用共用 SQL schema，不讓 Symfony 建立另一套 migration 或資料模型。 */
class Database
{
    private ?PDO $connection = null;

    /** 延後連線至實際需要資料庫時，讓純參數錯誤仍可依契約回應。 */
    public function connection(): PDO
    {
        if ($this->connection === null) {
            $this->connection = new PDO(
                sprintf('pgsql:host=%s;port=%s;dbname=%s', getenv('DB_HOST') ?: 'db', getenv('DB_PORT') ?: '5432', getenv('DB_DATABASE') ?: 'articles'),
                getenv('DB_USERNAME') ?: 'articles_app',
                getenv('DB_PASSWORD') ?: '',
                [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_OBJ]
            );
            // 與 Laravel 共用資料庫 session 時區，讓查詢時間及鎖定邊界一致。
            $this->connection->exec("SET TIME ZONE 'Asia/Taipei'");
        }

        return $this->connection;
    }

    /** 對認證查核與登入鎖定保持單一交易；例外必須回滾，業務錯誤由正常返回提交。 */
    public function transaction(callable $operation): mixed
    {
        $db = $this->connection();
        $db->beginTransaction();
        try {
            $result = $operation($db);
            $db->commit();

            return $result;
        } catch (Throwable $exception) {
            $db->rollBack();
            throw $exception;
        }
    }
}
