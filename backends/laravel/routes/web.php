<?php

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;

// 本階段認證與文章表皆可查詢才就緒，避免 SQL 未完成時接受 API 請求。
Route::get('/up', function () {
    foreach (['accounts', 'login_sessions', 'login_failures', 'login_lockout_settings', 'articles'] as $table) {
        DB::table($table)->limit(1)->exists();
    }

    return response('OK', 200);
});
