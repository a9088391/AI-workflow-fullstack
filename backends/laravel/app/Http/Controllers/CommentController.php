<?php

namespace App\Http\Controllers;

use App\Support\AuthenticatedAccount;
use App\Support\ContentContract;
use Illuminate\Database\Query\Builder;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use stdClass;

class CommentController extends Controller
{
    use ContentContract;

    /** 先確認父文章可讀，再列留言；文章作者的下架預覽仍只含公開留言。 */
    public function index(Request $request, string $article_id): JsonResponse
    {
        // 認證先於格式與可見性；帶無效 JWT 不可降為訪客。
        return DB::transaction(function () use ($request, $article_id): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request, true)[0] ?? null;
            $input = $this->query($request, true, false);
            if ($input instanceof JsonResponse) {
                return $input;
            }
            $id = $this->integer($article_id);
            if ($id === null) {
                return $this->invalid(['article_id' => ['欄位格式或範圍不正確']]);
            }
            $article = DB::table('articles')->where('id', $id)->first(['author_id', 'visibility']);
            if ($article === null || ($article->visibility === 'hidden' && $viewer?->role !== 'admin' && $viewer?->id !== $article->author_id)) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }

            // 管理員可查看個別下架留言；其他可讀父文章者均只看 visible。
            $comments = $this->comments()->where('comments.article_id', $id);
            if ($viewer?->role !== 'admin') {
                $comments->where('comments.visibility', 'visible');
            }

            return $this->listing($comments, $input, $viewer);
        });
    }

    /** 登入者可在公開文章留言；父文章狀態檢查與新增共用交易，避免下架競爭。 */
    public function store(Request $request, string $article_id): JsonResponse
    {
        // 依帳號→session→文章取得鎖，先認證與格式驗證，再判定目標可讀及可新增。
        return DB::transaction(function () use ($request, $article_id): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $id = $this->integer($article_id);
            if ($id === null) {
                return $this->invalid(['article_id' => ['欄位格式或範圍不正確']]);
            }
            $body = $this->body($request, ['body' => 2000]);
            if ($body instanceof JsonResponse) {
                return $body;
            }

            // 等到文章鎖後才查目前狀態；有預覽權的下架文章仍禁止留言，管理員亦同。
            $article = DB::table('articles')->where('id', $id)->lockForUpdate()->first(['author_id', 'visibility']);
            if ($article === null || ($article->visibility === 'hidden' && $viewer->role !== 'admin' && $viewer->id !== $article->author_id)) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }
            if ($article->visibility === 'hidden') {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }

            // 作者與來源只能由後端指定；一次資料庫時間確保建立及更新時間相同。
            $now = DB::selectOne('SELECT clock_timestamp() AS now')->now;
            DB::table('comments')->insert([
                'article_id' => $id,
                'author_id' => $viewer->id,
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

    /** 作者編修原文；共用交易流程保持認證、可讀性、操作權與版本的判定順序。 */
    public function update(Request $request, string $comment_id): JsonResponse
    {
        return $this->mutate($request, $comment_id, false);
    }

    /** 作者或管理員調整下架狀態；恢復權與版本必須在同一交易中判定及更新。 */
    public function visibility(Request $request, string $comment_id): JsonResponse
    {
        return $this->mutate($request, $comment_id, true);
    }

    /** 先鎖定認證狀態再鎖內容，以最新資料原子判定權限、同值及版本競爭。 */
    private function mutate(Request $request, string $comment_id, bool $visibility): JsonResponse
    {
        // callback 內保留認證鎖到提交，避免停用或撤銷先提交後仍接受寫入。
        return DB::transaction(function () use ($request, $comment_id, $visibility): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $query = $this->query($request, false, false);
            if ($query instanceof JsonResponse) {
                return $query;
            }
            $id = $this->integer($comment_id);
            if ($id === null) {
                return $this->invalid(['comment_id' => ['欄位格式或範圍不正確']]);
            }
            $body = $this->body($request, $visibility ? ['visibility' => 'visibility', 'version' => 'version'] : ['body' => 2000, 'version' => 'version']);
            if ($body instanceof JsonResponse) {
                return $body;
            }

            // 探索父 ID 不先鎖留言，固定文章→留言順序；取得鎖後重新核對關聯。
            $parentId = DB::table('comments')->where('id', $id)->value('article_id');
            $article = $parentId === null ? null : DB::table('articles')->where('id', $parentId)->lockForUpdate()->first();
            $content = $article === null ? null : DB::table('comments')->where('id', $id)->lockForUpdate()->first();
            // 先隱藏無權閱讀內容的存在，再檢查作者操作資格；版本不得洩漏隱藏資料。
            if ($content === null || $content->article_id !== $article->id
                || ($viewer->role !== 'admin' && $viewer->id !== $content->author_id
                    && ($content->visibility === 'hidden' || $article->visibility === 'hidden'))) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }
            if (($viewer->id !== $content->author_id && (! $visibility || $viewer->role !== 'admin'))
                || (! $visibility && $article->visibility !== 'visible')) {
                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }
            $changes = $visibility ? $this->visibilityChanges($content, $viewer, $body->visibility) : ['body' => $body->body];
            if ($changes instanceof JsonResponse) {
                return $changes;
            }

            return $this->saveChanges('comments', $content, $body, $changes);
        });
    }

    /** 我的留言獨立於父文章可見性，只回留言欄位與 article_id。 */
    public function mine(Request $request): JsonResponse
    {
        // 由交易內驗證的帳號限制作者，不接受用戶端指定作者或管理範圍。
        return DB::transaction(function () use ($request): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $input = $this->query($request, true, false);
            if ($input instanceof JsonResponse) {
                return $input;
            }

            return $this->listing($this->comments()->where('comments.author_id', $viewer->id), $input, $viewer);
        });
    }

    /** 作者及管理員可獨立讀取留言；其他會員依可見性先回 404，再判定 403。 */
    public function show(Request $request, string $comment_id): JsonResponse
    {
        // 作者例外不授予父文章讀取權，查詢只取父文章可見性而不取內文。
        return DB::transaction(function () use ($request, $comment_id): JsonResponse {
            $viewer = AuthenticatedAccount::resolve($request)[0];
            $input = $this->query($request, false, false);
            if ($input instanceof JsonResponse) {
                return $input;
            }
            $id = $this->integer($comment_id);
            if ($id === null) {
                return $this->invalid(['comment_id' => ['欄位格式或範圍不正確']]);
            }
            $comment = $this->comments()->join('articles', 'articles.id', '=', 'comments.article_id')
                ->addSelect('articles.visibility as article_visibility')->where('comments.id', $id)->first();
            if ($comment === null) {
                return $this->error(404, 'E0004', '找不到指定資料或 API');
            }
            if ($viewer->role !== 'admin' && $viewer->id !== $comment->author_id) {
                if ($comment->visibility === 'hidden' || $comment->article_visibility === 'hidden') {
                    return $this->error(404, 'E0004', '找不到指定資料或 API');
                }

                return $this->error(403, 'E0003', '沒有權限使用此功能');
            }

            return $this->success($this->resource($comment, $viewer));
        });
    }

    /** 只選公開留言契約欄位與作者暱稱，避免帳號私密資料及父文章內容外洩。 */
    private function comments(): Builder
    {
        return DB::table('comments')->join('accounts', 'accounts.id', '=', 'comments.author_id')
            ->select(['comments.id', 'comments.author_id', 'comments.article_id', 'comments.body', 'comments.visibility', 'comments.restore_control', 'comments.version', 'comments.created_backend', 'comments.created_at', 'comments.updated_at', 'accounts.nickname']);
    }

    /** 計算權限範圍內總數及穩定排序頁面；巨大合法頁碼回空頁以免 offset 溢位。 */
    private function listing(Builder $comments, array $input, ?stdClass $viewer): JsonResponse
    {
        $total = $comments->count();
        $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1 ? null : ($input['page'] - 1) * $input['per_page'];
        $rows = $offset === null || $offset >= $total ? [] : $comments->orderByDesc('comments.created_at')->orderByDesc('comments.id')->offset($offset)->limit($input['per_page'])->get();
        $list = [];
        foreach ($rows as $row) {
            $list[] = $this->resource($row, $viewer);
        }

        return $this->success(['list' => $list, 'pagination' => ['page' => $input['page'], 'per_page' => $input['per_page'], 'total' => $total, 'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0)]]);
    }

    /** 恢復權即時計算；article_id 只識別父文章，回應不包含父文章的標題或內文。 */
    private function resource(stdClass $comment, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $comment->author_id;

        return ['id' => (int) $comment->id, 'body' => $comment->body,
            'author' => ['id' => (int) $comment->author_id, 'nickname' => $comment->nickname],
            'article_id' => (int) $comment->article_id, 'version' => (int) $comment->version,
            'created_backend' => $comment->created_backend,
            'created_at' => $this->displayTime($comment->created_at), 'updated_at' => $this->displayTime($comment->updated_at),
            'visibility' => $comment->visibility,
            'can_restore' => $comment->visibility === 'hidden' && ($viewer?->role === 'admin' || ($own && $comment->restore_control === 'author'))];
    }
}
