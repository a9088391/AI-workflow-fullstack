-- 只擴充來源識別白名單；既有列、角色權限與歷史 SQL 皆不變，交集中入口原子套用。
ALTER TABLE login_sessions DROP CONSTRAINT login_sessions_backend_id_check;
ALTER TABLE login_sessions ADD CONSTRAINT login_sessions_backend_id_check
    CHECK (backend_id IN ('laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi', 'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs', 'rails'));

ALTER TABLE articles DROP CONSTRAINT articles_created_backend_check;
ALTER TABLE articles ADD CONSTRAINT articles_created_backend_check
    CHECK (created_backend IN ('laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi', 'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs', 'rails'));

ALTER TABLE comments DROP CONSTRAINT comments_created_backend_check;
ALTER TABLE comments ADD CONSTRAINT comments_created_backend_check
    CHECK (created_backend IN ('laravel', 'symfony', 'aspnet-core', 'django', 'flask', 'fastapi', 'gin', 'echo', 'fiber', 'express', 'fastify', 'nestjs', 'rails'));
