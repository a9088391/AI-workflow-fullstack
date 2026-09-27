-- 文章的作者與建立來源由後端寫入；下架時保留恢復控制者以免作者繞過管理員處置。
CREATE TABLE articles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY CHECK (id BETWEEN 1 AND 9007199254740991),
    author_id bigint NOT NULL REFERENCES accounts(id),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 20000),
    visibility text NOT NULL CHECK (visibility IN ('visible', 'hidden')),
    restore_control text CHECK (restore_control IN ('author', 'admin')),
    version bigint NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 9007199254740991),
    created_backend text NOT NULL CHECK (created_backend IN ('laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi', 'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK ((visibility = 'visible' AND restore_control IS NULL) OR (visibility = 'hidden' AND restore_control IS NOT NULL))
);

-- 公開、管理及個人列表各按建立時間與 ID 降冪；避免為未實作搜尋建立索引。
CREATE INDEX articles_public_order_idx ON articles(created_at DESC, id DESC) WHERE visibility = 'visible';
CREATE INDEX articles_management_order_idx ON articles(created_at DESC, id DESC);
CREATE INDEX articles_author_order_idx ON articles(author_id, created_at DESC, id DESC);

GRANT SELECT, INSERT ON articles TO articles_app;
GRANT USAGE ON SEQUENCE articles_id_seq TO articles_app;
