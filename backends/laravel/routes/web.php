<?php

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;

// 容器就緒要能查到共用 accounts；單靠 PHP 行程存活不足以接收註冊。
Route::get('/up', function () {
    DB::table('accounts')->limit(1)->exists();

    return response('OK', 200);
});
