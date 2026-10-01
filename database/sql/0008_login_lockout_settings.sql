-- 管理員端點須原子更新四欄；設定表仍由集中 SQL 入口維護，不擴大至其他表欄位。
GRANT UPDATE (window_minutes, failure_threshold, lock_seconds) ON login_lockout_settings TO articles_app;
