<?php

namespace App\Http\Controllers;

use App\Support\AuthenticatedAccount;
use App\Support\ContentContract;
use Illuminate\Database\Query\Builder;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use stdClass;

class ArticleController extends Controller
{
    use ContentContract;

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
            $body = $this->body($request, ['title' => 200, 'body' => 20000]);
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
            if (
                $article === null
                || ($article->visibility === 'hidden'
                    && $viewer?->role !== 'admin' && $viewer?->id !== $article->author_id)
            ) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }

            return $this->success($this->resource($article, $viewer));
        });
    }

    /** 作者編修原文；共用交易流程保持認證、可讀性、操作權與版本的判定順序。 */
    public function update(Request $request, string $article_id): JsonResponse
    {
        return $this->mutate($request, $article_id, false);
    }

    /** 作者或管理員調整下架狀態；恢復權與版本必須在同一交易中判定及更新。 */
    public function visibility(Request $request, string $article_id): JsonResponse
    {
        return $this->mutate($request, $article_id, true);
    }

    /** 先鎖定認證狀態再鎖內容，以最新資料原子判定權限、同值及版本競爭。 */
    private function mutate(Request $request, string $article_id, bool $visibility): JsonResponse
    {
        // callback 內保留認證鎖到提交，避免停用或撤銷先提交後仍接受寫入。
        return DB::transaction(function () use ($request, $article_id, $visibility): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $id = $this->integer($article_id);
            if ($id === null) {
                return $this->invalid(['article_id' => ['欄位格式或範圍不正確']]);
            }
            $body = $this->body($request, $visibility
                ? ['visibility' => 'visibility', 'version' => 'version']
                : ['title' => 200, 'body' => 20000, 'version' => 'version']);
            if ($body instanceof JsonResponse) {
                return $body;
            }

            $content = DB::table('articles')->where('id', $id)->lockForUpdate()->first();
            // 先隱藏無權閱讀內容的存在，再檢查作者操作資格；版本不得洩漏隱藏資料。
            if (
                $content === null
                || ($content->visibility === 'hidden'
                    && $viewer->role !== 'admin' && $viewer->id !== $content->author_id)
            ) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }
            if ($viewer->id !== $content->author_id && (! $visibility || $viewer->role !== 'admin')) {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $changes = $visibility
                ? $this->visibilityChanges($content, $viewer, $body->visibility)
                : ['title' => $body->title, 'body' => $body->body];
            if ($changes instanceof JsonResponse) {
                return $changes;
            }

            return $this->saveChanges('articles', $content, $body, $changes);
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

    /** 查詢只選公開資源所需欄位，避免作者 Email、雜湊或內部鎖定資料外洩。 */
    private function articles(): Builder
    {
        return DB::table('articles')->join('accounts', 'accounts.id', '=', 'articles.author_id')
            ->select([
                'articles.id', 'articles.author_id', 'articles.title', 'articles.body',
                'articles.visibility', 'articles.restore_control', 'articles.version',
                'articles.created_backend', 'articles.created_at', 'articles.updated_at', 'accounts.nickname'
            ]);
    }

    /** 先取得範圍內總數，再取穩定排序頁面；巨大合法頁碼直接回空頁避免 offset 溢位。 */
    private function listing(Builder $articles, array $input, ?stdClass $viewer): JsonResponse
    {
        $total = $articles->count();
        $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1
            ? null : ($input['page'] - 1) * $input['per_page'];
        $rows = $offset === null || $offset >= $total
            ? []
            : $articles->orderByDesc('articles.created_at')->orderByDesc('articles.id')
                ->offset($offset)->limit($input['per_page'])->get();
        $list = [];
        foreach ($rows as $row) {
            $list[] = $this->resource($row, $viewer);
        }

        return $this->success([
            'list' => $list,
            'pagination' => [
                'page' => $input['page'],
                'per_page' => $input['per_page'],
                'total' => $total,
                'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0)
            ]
        ]);
    }

    /** 文章作者只公開 ID 與暱稱；恢復權依目前讀者與下架控制者即時計算。 */
    private function resource(stdClass $article, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $article->author_id;

        return [
            'id' => (int) $article->id,
            'title' => $article->title,
            'body' => $article->body,
            'author' => ['id' => (int) $article->author_id, 'nickname' => $article->nickname],
            'version' => (int) $article->version,
            'created_backend' => $article->created_backend,
            'created_at' => $this->displayTime($article->created_at),
            'updated_at' => $this->displayTime($article->updated_at),
            'visibility' => $article->visibility,
            'can_restore' => $article->visibility === 'hidden'
                && ($viewer?->role === 'admin' || ($own && $article->restore_control === 'author'))
        ];
    }
}
