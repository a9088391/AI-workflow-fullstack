<?php

use Illuminate\Database\QueryException;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;

// 本階段認證、文章與留言表皆可查詢才就緒，避免 SQL 未完成時接受 API 請求。
Route::get('/up', function () {
    try {
        $tables = ['accounts', 'login_sessions', 'login_failures', 'login_lockout_settings', 'articles', 'comments'];
        foreach ($tables as $table) {
            DB::table($table)->limit(1)->exists();
        }
    } catch (QueryException) {
        // 資料庫或必要資料表不可查詢時回報未就緒，避免健康檢查暴露 SQL 細節。
        return response('Unavailable', 503);
    }

    return response('OK', 200);
});
