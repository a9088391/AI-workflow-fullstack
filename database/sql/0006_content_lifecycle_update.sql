-- 編修與下架／恢復只需更新內容、狀態、版本及時間；作者、來源與建立資料仍不可改。
GRANT UPDATE (title, body, visibility, restore_control, version, updated_at) ON articles TO articles_app;
GRANT UPDATE (body, visibility, restore_control, version, updated_at) ON comments TO articles_app;
