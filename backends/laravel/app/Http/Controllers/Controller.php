<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;

abstract class Controller
{
    /** 只比對分號前的完整媒體型態，容許大小寫與參數但拒絕 application/jsonish 等前綴。 */
    protected function hasJsonContentType(Request $request): bool
    {
        $mediaType = explode(';', $request->header('Content-Type', ''), 2)[0];

        return strtolower(trim($mediaType)) === 'application/json';
    }
}
