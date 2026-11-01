<?php

namespace App\Http\Controllers;

use App\Http\ApiResponse;
use App\Support\AccountRules;
use Illuminate\Database\UniqueConstraintViolationException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use JsonException;

class RegisterController extends Controller
{
    /** 先驗原始 JSON 再建立固定 member/active 帳號，避免框架正規化改動文字或接受自行提權。 */
    public function __invoke(Request $request): JsonResponse
    {
        // Query 不接受任何欄位；從原始字串辨識重複鍵，避免框架先覆蓋值。
        $query = $request->server('QUERY_STRING', '');
        if ($query !== '') {
            $seen = [];
            $errors = [];
            foreach (explode('&', $query) as $pair) {
                $rawName = urldecode(explode('=', $pair, 2)[0]);
                // 原始位元組辨認參數身分；百分號表示只供 JSON 錯誤鍵，不能合併不同名稱。
                $errorName = mb_check_encoding($rawName, 'UTF-8') ? $rawName : rawurlencode($rawName);
                if (isset($seen[$rawName])) {
                    $errors[$errorName] = ['此參數不可重複'];
                } else {
                    // 不同名稱可能共用顯示鍵，後來的未知欄位不可覆蓋已確定的重複錯誤。
                    $errors[$errorName] ??= ['不支援此欄位'];
                }
                $seen[$rawName] = true;
            }

            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', $errors);
        }

        // 媒體型態與空 Body 屬結構錯誤；JSON 語法錯誤另回 400，不混成欄位驗證失敗。
        if (! str_starts_with(strtolower($request->header('Content-Type', '')), 'application/json')) {
            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        if ($request->getContent() === '') {
            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', ['_body' => ['Request Body 必須為 JSON object']]);
        }

        try {
            $body = json_decode($request->getContent(), false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            return ApiResponse::error(400, 'E0005', '系統暫時無法處理，請稍後再試');
        }
        if (! is_object($body)) {
            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', ['_body' => ['Request Body 必須為 JSON object']]);
        }

        // 每欄只回第一項錯誤；必填/null、型態、格式依共同順序判斷。
        $errors = [];
        foreach (['email', 'password', 'nickname'] as $field) {
            if (! property_exists($body, $field)) {
                $errors[$field] = ['此欄位為必填'];
            } elseif ($body->$field === null) {
                $errors[$field] = ['此欄位不可為 null'];
            } elseif (! is_string($body->$field)) {
                $errors[$field] = ['欄位型態不正確'];
            }
        }
        foreach ($body as $field => $_) {
            if (! in_array($field, ['email', 'password', 'nickname'], true)) {
                $errors[$field] = ['不支援此欄位'];
            }
        }
        if (! isset($errors['email'])) {
            $body->email = AccountRules::normalizeEmail($body->email);
            if (! AccountRules::validEmail($body->email)) {
                $errors['email'] = ['欄位格式或範圍不正確'];
            }
        }
        if (! isset($errors['password']) && ! AccountRules::validPassword($body->password)) {
            $errors['password'] = ['欄位格式或範圍不正確'];
        }
        if (! isset($errors['nickname']) && ! AccountRules::validNickname($body->nickname)) {
            $errors['nickname'] = ['欄位格式或範圍不正確'];
        }
        if ($errors !== []) {
            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', $errors);
        }

        // 唯一約束是並行註冊的裁決點；先查詢只作快速友善回應。
        if (DB::table('accounts')->where('email', $body->email)->exists()) {
            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', ['email' => ['此 Email 已註冊']]);
        }
        try {
            DB::table('accounts')->insert([
                'email' => $body->email,
                'password_hash' => AccountRules::hashPassword($body->password),
                'nickname' => $body->nickname,
                'role' => 'member',
                'status' => 'active',
            ]);
        } catch (UniqueConstraintViolationException) {
            return ApiResponse::error(422, 'E0001', '欄位資料不正確，請確認後再送出', ['email' => ['此 Email 已註冊']]);
        }

        return response()->json(['success' => true, 'message' => 'OK', 'data' => (object) []], 201);
    }
}
