-- 本票先建立註冊必需欄位；其他業務表由各自核准票逐版加入。
CREATE TABLE accounts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY CHECK (id BETWEEN 1 AND 9007199254740991),
    email varchar(254) NOT NULL UNIQUE,
    password_hash text NOT NULL,
    nickname text NOT NULL CHECK (char_length(nickname) BETWEEN 1 AND 30),
    role text NOT NULL CHECK (role IN ('member', 'admin')),
    status text NOT NULL CHECK (status IN ('active', 'disabled')),
    failure_epoch bigint NOT NULL DEFAULT 1,
    locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON accounts TO articles_app;
GRANT USAGE ON SEQUENCE accounts_id_seq TO articles_app;
