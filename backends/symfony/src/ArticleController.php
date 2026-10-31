<?php

namespace App;

use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排公開、作者與管理範圍的文章讀寫，讓文章可讀性與權限判定留在同一責任。 */
class ArticleController extends ContentController
{
    /** 公開列表只含 visible；明確 management 範圍先驗管理員再驗 Query。 */
    public function articles(Request $request): JsonResponse
    {
        // 管理範圍門禁先於 Query；讀取時保留憑證狀態到回應形成。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $management = $this->managementRequested($request);
            $identity = $this->authenticate($request, $db, ! $management);
            $viewer = $identity[0] ?? null;
            if ($management && $viewer?->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $input = $this->listQuery($request, true);
            if ($input['scope'] === 'management' && $viewer?->role !== 'admin') {
                throw new ApiFailure('E0003');
            }

            // 公開與管理列表使用同一範圍計數及查頁，確保分頁不洩漏其他內容。
            $where = $input['scope'] === 'management' ? '' : " WHERE articles.visibility = 'visible'";
            $from = ' FROM articles JOIN accounts ON accounts.id = articles.author_id' . $where;
            $total = (int) $db->query('SELECT count(*)' . $from)->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT articles.id, articles.author_id, articles.title, articles.body, '
                . 'articles.visibility, articles.restore_control, articles.version, '
                . 'articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname'
                . $from . ' ORDER BY articles.created_at DESC, articles.id DESC',
                $input,
                $total,
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->articleResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 先查核可選 JWT 與參數，再依作者／管理員資格決定下架文章可讀性。 */
    public function article(Request $request, string $article_id): JsonResponse
    {
        // 在同一交易取得目前身分，再以可讀性隱藏不應曝光的下架內容。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            $identity = $this->authenticate($request, $db, true);
            $viewer = $identity[0] ?? null;
            $this->noGetBody($request);
            $this->noQuery($request);
            $id = $this->id($article_id, 'article_id');
            // 取回完整資源後先判可讀性，隱藏內容不可用拒絕訊息洩漏存在與版本。
            $select = $db->prepare(
                'SELECT articles.id, articles.author_id, articles.title, articles.body, '
                . 'articles.visibility, articles.restore_control, articles.version, '
                . 'articles.created_backend, articles.created_at, articles.updated_at, '
                . 'accounts.nickname FROM articles JOIN accounts ON accounts.id = articles.author_id '
                . 'WHERE articles.id = :id'
            );
            $select->execute(['id' => $id]);
            $article = $select->fetch();
            if (
                $article === false
                || ($article->visibility === 'hidden' && $viewer?->role !== 'admin'
                    && (int) ($viewer?->id ?? 0) !== (int) $article->author_id)
            ) {
                throw new ApiFailure('E0004');
            }

            return $this->success($this->articleResource($article, $viewer));
        });
    }

    /** 以目前登入者建立公開文章；來源及時間由伺服器指定，不能由 Body 冒填。 */
    public function createArticle(Request $request): JsonResponse
    {
        // 認證鎖保留到寫入提交，與停用及撤銷登入取得一致先後。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $body = $this->contentBody($request, ['title' => 200, 'body' => 20000]);
            // 作者、來源及初始版本由伺服器決定，建立時間與更新時間使用同一值。
            $now = $this->sqlTime($this->now($db));
            $insert = $db->prepare(
                "INSERT INTO articles (author_id, title, body, visibility, restore_control, version, "
                . "created_backend, created_at, updated_at) VALUES (:author, :title, :body, 'visible', "
                . "NULL, 1, 'symfony', :created, :updated)"
            );
            $insert->execute([
                'author' => $viewer->id,
                'title' => $body->title,
                'body' => $body->body,
                'created' => $now,
                'updated' => $now,
            ]);

            return $this->success([], 201);
        });
    }

    /** 個人文章列表包含自己的下架內容，從登入帳號固定作者範圍。 */
    public function myArticles(Request $request): JsonResponse
    {
        // 驗證 Query 後精確計數及穩定分頁，避免由用戶端指定他人作者。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $input = $this->listQuery($request, false);
            $from = ' FROM articles JOIN accounts ON accounts.id = articles.author_id WHERE '
                . 'articles.author_id = :author';
            $count = $db->prepare('SELECT count(*)' . $from);
            $count->execute(['author' => $viewer->id]);
            $total = (int) $count->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT articles.id, articles.author_id, articles.title, articles.body, '
                . 'articles.visibility, articles.restore_control, articles.version, '
                . 'articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname'
                . $from . ' ORDER BY articles.created_at DESC, articles.id DESC',
                $input,
                $total,
                ['author' => (int) $viewer->id],
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->articleResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 作者可編修自己的文章，包含下架內容，但編修不改恢復權。 */
    public function updateArticle(Request $request, string $article_id): JsonResponse
    {
        return $this->mutateArticle($request, $article_id, false);
    }

    /** 作者或管理員可調整文章可見性，管理員接管他人恢復權需增加版本。 */
    public function articleVisibility(Request $request, string $article_id): JsonResponse
    {
        return $this->mutateArticle($request, $article_id, true);
    }

    /** 在帳號、session、文章列鎖內依可讀性、權限、版本順序裁決文章寫入。 */
    private function mutateArticle(Request $request, string $article_id, bool $visibility): JsonResponse
    {
        // 同值也先查版本；鎖到提交避免後續並行覆寫或撤銷登入競爭。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id, $visibility): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $id = $this->id($article_id, 'article_id');
            $body = $this->contentBody(
                $request,
                $visibility
                    ? ['visibility' => 'visibility', 'version' => 'version']
                    : ['title' => 200, 'body' => 20000, 'version' => 'version'],
            );
            // 鎖後依可讀性、操作權限及恢復權裁決，最後才交由共用寫入比對版本。
            $select = $db->prepare('SELECT * FROM articles WHERE id = :id FOR UPDATE');
            $select->execute(['id' => $id]);
            $content = $select->fetch();
            if (
                $content === false
                || ($content->visibility === 'hidden' && $viewer->role !== 'admin'
                    && (int) $viewer->id !== (int) $content->author_id)
            ) {
                throw new ApiFailure('E0004');
            }
            if ((int) $viewer->id !== (int) $content->author_id && (! $visibility || $viewer->role !== 'admin')) {
                throw new ApiFailure('E0003');
            }
            $changes = $visibility
                ? $this->visibilityChanges($content, $viewer, $body->visibility)
                : ['title' => $body->title, 'body' => $body->body];

            return $this->saveChanges($db, 'articles', $content, $body->version, $changes);
        });
    }

    /** 從原始 Query 找明確管理範圍，讓角色門禁先於其他錯誤。 */
    private function managementRequested(Request $request): bool
    {
        foreach (explode('&', $request->server->get('QUERY_STRING', '')) as $pair) {
            [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
            if (urldecode($name) === 'filters[scope]' && urldecode($value) === 'management') {
                return true;
            }
        }

        return false;
    }

    /** 文章只回共同欄位與公開作者資料，恢復權依目前身分即時計算。 */
    private function articleResource(stdClass $article, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $article->author_id;

        return ['id' => (int) $article->id, 'title' => $article->title, 'body' => $article->body,
            'author' => ['id' => (int) $article->author_id, 'nickname' => $article->nickname],
            'version' => (int) $article->version, 'created_backend' => $article->created_backend,
            'created_at' => $this->displayTime($article->created_at),
            'updated_at' => $this->displayTime($article->updated_at),
            'visibility' => $article->visibility,
            'can_restore' => $article->visibility === 'hidden'
                && ($viewer?->role === 'admin' || ($own && $article->restore_control === 'author')),
        ];
    }
}
