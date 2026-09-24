-- 共用登入身分只保存隨機 jti，不保存完整 JWT；後端與固定八小時期限由資料約束保護。
CREATE TABLE login_sessions (
    jti varchar(22) PRIMARY KEY CHECK (jti ~ '^[A-Za-z0-9_-]{21}[AQgw]$'),
    account_id bigint NOT NULL REFERENCES accounts(id),
    backend_id text NOT NULL CHECK (backend_id IN ('laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi', 'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs')),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at = issued_at + interval '8 hours'),
    revoked_at timestamptz,
    CHECK (issued_at = date_trunc('second', issued_at))
);
CREATE INDEX login_sessions_account_id_idx ON login_sessions(account_id);

-- 成功或解鎖僅增加世代；保留舊失敗事件，但不再納入新世代的滑動視窗。
CREATE TABLE login_failures (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts(id),
    failure_epoch bigint NOT NULL,
    occurred_at timestamptz NOT NULL
);
CREATE INDEX login_failures_window_idx ON login_failures(account_id, failure_epoch, occurred_at);

CREATE TABLE login_lockout_settings (
    id smallint PRIMARY KEY CHECK (id = 1),
    enabled boolean NOT NULL,
    window_minutes integer NOT NULL CHECK (window_minutes BETWEEN 1 AND 60),
    failure_threshold integer NOT NULL CHECK (failure_threshold BETWEEN 1 AND 100),
    lock_seconds integer NOT NULL CHECK (lock_seconds BETWEEN 1 AND 86400)
);
INSERT INTO login_lockout_settings VALUES (1, true, 5, 5, 300);

-- 應用只能寫入認證所需欄位；設定尚無管理端點，但 FOR UPDATE 必須有 UPDATE 權限。
GRANT UPDATE (password_hash, failure_epoch, locked_until) ON accounts TO articles_app;
GRANT SELECT, INSERT ON login_sessions, login_failures TO articles_app;
GRANT UPDATE (revoked_at) ON login_sessions TO articles_app;
GRANT SELECT ON login_lockout_settings TO articles_app;
GRANT UPDATE (enabled) ON login_lockout_settings TO articles_app;
GRANT USAGE ON SEQUENCE login_failures_id_seq TO articles_app;
