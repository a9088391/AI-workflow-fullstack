-- 管理員狀態端點只可修改會員狀態，不取得角色或登入鎖定欄位的寫入權。
GRANT UPDATE (status) ON accounts TO articles_app;
