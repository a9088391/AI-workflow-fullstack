<?php

namespace App\Http\Controllers;

use App\Http\ApiResponse;
use App\Support\AuthenticatedAccount;
use Carbon\CarbonImmutable;
use Illuminate\Database\Query\Builder;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use JsonException;
use stdClass;

class ArticleController extends Controller
{
    /** 公開列表預設只列公開文章；指定管理範圍時驗證目前管理員身分。 */
    public function index(Request $request): JsonResponse
    {
        // 先辨識管理範圍以套用端點角色門禁，然後才檢查其餘 Query 與 GET Body。
        return DB::transaction(function () use ($request): JsonResponse {
            $management = $this->managementRequested($request);
            $viewer = AuthenticatedAccount::resolve($request, ! $management)[0] ?? null;
            if ($management && $viewer?->role !== 'admin') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $input = $this->query($request, true, true);
            if ($input instanceof JsonResponse) {
                return $input;
            }
            if ($input['scope'] === 'management' && ($viewer?->role !== 'admin')) {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }

            $articles = $this->articles();
            if ($input['scope'] !== 'management') {
                $articles->where('articles.visibility', 'visible');
            }

            return $this->listing($articles, $input, $viewer);
        });
    }

    /** 登入者可發文；原始 JSON 僅允許 title/body，由資料庫產生作者以外的識別資料。 */
    public function store(Request $request): JsonResponse
    {
        // 認證和寫入同一交易，停用／登出與發文必須有一致的先後結果。
        return DB::transaction(function () use ($request): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $body = $this->body($request);
            if ($body instanceof JsonResponse) {
                return $body;
            }

            // 建立時間由資料庫時鐘一次取得；建立與更新顯示值必須一致。
            $now = DB::selectOne('SELECT clock_timestamp() AS now')->now;
            DB::table('articles')->insert([
                'author_id' => $viewer->id,
                'title' => $body->title,
                'body' => $body->body,
                'visibility' => 'visible',
                'restore_control' => null,
                'version' => 1,
                'created_backend' => 'laravel',
                'created_at' => $now,
                'updated_at' => $now,
            ]);

            return $this->success([], 201);
        });
    }

    /** 詳情先驗 ID 與 Query，對無權讀取的下架文章隱藏其存在。 */
    public function show(Request $request, string $article_id): JsonResponse
    {
        // 認證後在同一讀取範圍內檢查可見性，避免下架內容經公開詳情洩漏。
        return DB::transaction(function () use ($request, $article_id): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request, true)[0] ?? null;
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $id = $this->integer($article_id);
            if ($id === null) {
                return $this->invalid(['article_id' => ['欄位格式或範圍不正確']]);
            }
            $article = $this->articles()->where('articles.id', $id)->first();
            if ($article === null || ($article->visibility === 'hidden' && $viewer?->role !== 'admin' && $viewer?->id !== $article->author_id)) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }

            return $this->success($this->resource($article, $viewer));
        });
    }

    /** 個人列表包含自己的下架文章，管理員作為作者亦使用同一規則。 */
    public function mine(Request $request): JsonResponse
    {
        // 以目前 session 的帳號 ID 查詢，不能由 Query 指定另一位作者。
        return DB::transaction(function () use ($request): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $input = $this->query($request, true, false);
            if ($input instanceof JsonResponse) {
                return $input;
            }

            return $this->listing($this->articles()->where('articles.author_id', $viewer->id), $input, $viewer);
        });
    }

    /** 在完整 Query 驗證前辨識明確管理範圍，使未登入／非管理員優先得到認證錯誤。 */
    private function managementRequested(Request $request): bool
    {
        foreach (explode('&', $request->server('QUERY_STRING', '')) as $pair) {
            [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
            if (urldecode($name) === 'filters[scope]' && urldecode($value) === 'management') {
                return true;
            }
        }

        return false;
    }

    /** 直接檢查原始 Query，以辨認重複參數與巢狀欄位，避免框架靜默取最後值。 */
    private function query(Request $request, bool $pagination, bool $scope = true): array|JsonResponse
    {
        // GET 不接受 Body，先拒絕以維持 Request Body 錯誤的驗證先後。
        if ($request->isMethod('GET') && $request->getContent() !== '') {
            return $this->invalid(['_body' => ['此 API 不接受 Request Body']]);
        }
        $values = [];
        $errors = [];
        $raw = $request->server('QUERY_STRING', '');
        // 原始參數逐一解碼以辨識重複與陣列；無效 UTF-8 名稱改用百分號編碼，讓錯誤鍵可輸出為 JSON。
        if ($raw !== '') {
            foreach (explode('&', $raw) as $pair) {
                [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
                $name = urldecode($name);
                $name = mb_check_encoding($name, 'UTF-8') ? $name : rawurlencode($name);
                $arrayValue = preg_match('/\A(page|per_page|filters\[scope\])\[/', $name, $parts) === 1;
                $canonical = $arrayValue ? $parts[1] : $name;
                $errorName = preg_replace('/\Afilters\[([^\]]+)\]\z/', 'filters.$1', $canonical);
                if (array_key_exists($canonical, $values)) {
                    $errors[$errorName] = ['此參數不可重複'];
                } elseif ($arrayValue && in_array($canonical, $pagination ? ($scope ? ['page', 'per_page', 'filters[scope]'] : ['page', 'per_page']) : [], true)) {
                    $errors[$errorName] = ['欄位型態不正確'];
                } elseif (! in_array($canonical, $pagination ? ($scope ? ['page', 'per_page', 'filters[scope]'] : ['page', 'per_page']) : [], true)) {
                    $errors[$errorName] = ['不支援此欄位'];
                }
                $values[$canonical] = urldecode($value);
            }
        }
        // 列表必填數字與管理範圍在欄位型態及未知名稱判定後驗證，保留每欄先出現的錯誤。
        if ($pagination) {
            foreach (['page', 'per_page'] as $name) {
                if (! array_key_exists($name, $values)) {
                    $errors[$name] = ['此欄位為必填'];
                } elseif (! isset($errors[$name]) && (($number = $this->integer($values[$name])) === null || ($name === 'per_page' && $number > 100))) {
                    $errors[$name] = ['欄位格式或範圍不正確'];
                }
            }
            if ($scope && isset($values['filters[scope]']) && ! isset($errors['filters.scope']) && $values['filters[scope]'] !== 'management') {
                $errors['filters.scope'] = ['欄位格式或範圍不正確'];
            }
        }

        return $errors === [] ? ['page' => $pagination ? (int) $values['page'] : 1, 'per_page' => $pagination ? (int) $values['per_page'] : 20, 'scope' => $values['filters[scope]'] ?? null] : $this->invalid($errors);
    }

    /** JSON 寫入必須是 object；保留原文，但拒絕 PostgreSQL text 無法儲存的 U+0000。 */
    private function body(Request $request): stdClass|JsonResponse
    {
        // 先分開判定媒體型態、JSON 語法與 object 形狀，維持各自既定的 422／400 回應。
        $raw = $request->getContent();
        if ($raw === '' || ! str_starts_with(strtolower($request->header('Content-Type', '')), 'application/json')) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            return $this->error(400, 'E0005', '系統暫時無法處理，請稍後再試');
        }
        if (! is_object($body)) {
            return $this->invalid(['_body' => ['Request Body 必須為 JSON object']]);
        }
        // 逐欄按必填、null、型態、長度與內容順序驗證，只回該欄第一個錯誤。
        $errors = [];
        foreach (['title' => 200, 'body' => 20000] as $name => $limit) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif (! is_string($body->$name)) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif (str_contains($body->$name, "\0")
                || mb_strlen($body->$name, 'UTF-8') < 1 || mb_strlen($body->$name, 'UTF-8') > $limit
                || preg_match('/[^\x{0009}-\x{000D}\x{0020}\x{0085}\x{00A0}\x{1680}\x{2000}-\x{200A}\x{2028}-\x{2029}\x{202F}\x{205F}\x{3000}]/u', $body->$name) !== 1) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        // 最後拒絕唯讀或未知欄位，避免它們影響已列欄位的驗證順序。
        foreach ($body as $name => $_) {
            if (! in_array($name, ['title', 'body'], true)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }

        return $errors === [] ? $body : $this->invalid($errors);
    }

    /** URL 整數接受前導零，但仍受共同安全整數上限約束。 */
    private function integer(string $value): ?int
    {
        if (preg_match('/\A[0-9]+\z/D', $value) !== 1) {
            return null;
        }
        $digits = ltrim($value, '0');
        if ($digits === '' || strlen($digits) > 16 || (strlen($digits) === 16 && strcmp($digits, '9007199254740991') > 0)) {
            return null;
        }

        return (int) $digits;
    }

    /** 查詢只選公開資源所需欄位，避免作者 Email、雜湊或內部鎖定資料外洩。 */
    private function articles(): Builder
    {
        return DB::table('articles')->join('accounts', 'accounts.id', '=', 'articles.author_id')
            ->select(['articles.id', 'articles.author_id', 'articles.title', 'articles.body', 'articles.visibility', 'articles.restore_control', 'articles.version', 'articles.created_backend', 'articles.created_at', 'articles.updated_at', 'accounts.nickname']);
    }

    /** 先取得範圍內總數，再取穩定排序頁面；巨大合法頁碼直接回空頁避免 offset 溢位。 */
    private function listing(Builder $articles, array $input, ?stdClass $viewer): JsonResponse
    {
        $total = $articles->count();
        $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1 ? null : ($input['page'] - 1) * $input['per_page'];
        $rows = $offset === null || $offset >= $total ? [] : $articles->orderByDesc('articles.created_at')->orderByDesc('articles.id')->offset($offset)->limit($input['per_page'])->get();
        $list = [];
        foreach ($rows as $row) {
            $list[] = $this->resource($row, $viewer);
        }

        return $this->success(['list' => $list, 'pagination' => ['page' => $input['page'], 'per_page' => $input['per_page'], 'total' => $total, 'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0)]]);
    }

    /** 文章作者只公開 ID 與暱稱；恢復權依目前讀者與下架控制者即時計算。 */
    private function resource(stdClass $article, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $article->author_id;

        return ['id' => (int) $article->id, 'title' => $article->title, 'body' => $article->body,
            'author' => ['id' => (int) $article->author_id, 'nickname' => $article->nickname],
            'version' => (int) $article->version, 'created_backend' => $article->created_backend,
            'created_at' => $this->displayTime($article->created_at), 'updated_at' => $this->displayTime($article->updated_at),
            'visibility' => $article->visibility,
            'can_restore' => $article->visibility === 'hidden' && ($viewer?->role === 'admin' || ($own && $article->restore_control === 'author'))];
    }

    /** 業務時間統一以台北時區顯示到秒，保留資料庫中的實際時間點。 */
    private function displayTime(string $time): string
    {
        return CarbonImmutable::parse($time)->setTimezone('Asia/Taipei')->format('Y-m-d H:i:s');
    }

    /** 維持共同成功 envelope；一般寫入回空 object。 */
    private function success(array $data, int $status = 200): JsonResponse
    {
        return response()->json(['success' => true, 'message' => 'OK', 'data' => (object) $data], $status);
    }

    /** 驗證錯誤只回每欄第一則固定文案。 */
    private function invalid(array $errors): JsonResponse
    {
        return $this->error(422, 'E0001', '欄位資料不正確，請確認後再送出', $errors);
    }

    /** 以共同錯誤格式回應，log_id 由外層 middleware 補入。 */
    private function error(int $status, string $code, string $message, ?array $errors = null): JsonResponse
    {
        return ApiResponse::error($status, $code, $message, $errors);
    }
}
