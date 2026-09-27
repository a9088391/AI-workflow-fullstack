-- PostgreSQL 的 SELECT FOR UPDATE 需要至少一欄 UPDATE 權限，才能鎖住父文章避免下架競爭。
-- 留言流程只鎖文章、不修改文章；僅授予 visibility 欄位，不擴大為整張表的 UPDATE 權限。
GRANT UPDATE (visibility) ON articles TO articles_app;
