-- 留言的作者與建立來源由後端寫入；下架時保留恢復控制者以免作者繞過管理員處置。
CREATE TABLE comments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY CHECK (id BETWEEN 1 AND 9007199254740991),
    author_id bigint NOT NULL REFERENCES accounts(id),
    article_id bigint NOT NULL REFERENCES articles(id),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
    visibility text NOT NULL CHECK (visibility IN ('visible', 'hidden')),
    restore_control text CHECK (restore_control IN ('author', 'admin')),
    version bigint NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 9007199254740991),
    created_backend text NOT NULL CHECK (created_backend IN ('laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi', 'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK ((visibility = 'visible' AND restore_control IS NULL) OR (visibility = 'hidden' AND restore_control IS NOT NULL))
);

-- 公開及管理的文章留言列表、個人列表維持建立時間與 ID 降冪；索引兼顧外鍵查詢。
CREATE INDEX comments_public_article_order_idx ON comments(article_id, created_at DESC, id DESC) WHERE visibility = 'visible';
CREATE INDEX comments_article_order_idx ON comments(article_id, created_at DESC, id DESC);
CREATE INDEX comments_author_order_idx ON comments(author_id, created_at DESC, id DESC);

GRANT SELECT, INSERT ON comments TO articles_app;
GRANT USAGE ON SEQUENCE comments_id_seq TO articles_app;
