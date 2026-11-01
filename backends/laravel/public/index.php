<?php

use Illuminate\Foundation\Application;
use Illuminate\Http\Request;

define('LARAVEL_START', microtime(true));

// 維護期間先交給 Laravel 的維護入口，避免繼續執行一般請求。
if (file_exists($maintenance = __DIR__ . '/../storage/framework/maintenance.php')) {
    require $maintenance;
}

// 載入已安裝依賴，讓框架與本專案類別使用同一份自動載入設定。
require __DIR__ . '/../vendor/autoload.php';

// 由共同 bootstrap 建立路由與錯誤管線，再交給框架處理及結束 HTTP 請求。
/** @var Application $app */
$app = require_once __DIR__ . '/../bootstrap/app.php';

$app->handleRequest(Request::capture());
