<?php

namespace App;

use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排公開、作者與管理範圍的留言讀寫，保留父文章檢查與留言寫入的共同交易。 */
class CommentController extends ContentController
{
    /** 父文章先判定可讀；預覽下架文章只列 visible 留言，管理員可見個別下架留言。 */
    public function comments(Request $request, string $article_id): JsonResponse
    {
        // 父文章可讀資格與留言可見性在同一讀取流程中決定。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            $identity = $this->authenticate($request, $db, true);
            $viewer = $identity[0] ?? null;
            $input = $this->listQuery($request, false);
            $id = $this->id($article_id, 'article_id');
            // 父文章的預覽資格只開放讀取此列表，不放寬個別下架留言的可見性。
            $parent = $db->prepare('SELECT author_id, visibility FROM articles WHERE id = :id');
            $parent->execute(['id' => $id]);
            $article = $parent->fetch();
            if (
                $article === false
                || ($article->visibility === 'hidden' && $viewer?->role !== 'admin'
                    && (int) ($viewer?->id ?? 0) !== (int) $article->author_id)
            ) {
                throw new ApiFailure('E0004');
            }

            // 計數與列表沿用相同可見範圍及排序，作者預覽也不包含個別下架留言。
            $where = ' WHERE comments.article_id = :id'
                . ($viewer?->role === 'admin' ? '' : " AND comments.visibility = 'visible'");
            $from = ' FROM comments JOIN accounts ON accounts.id = comments.author_id' . $where;
            $count = $db->prepare('SELECT count(*)' . $from);
            $count->execute(['id' => $id]);
            $total = (int) $count->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT comments.id, comments.author_id, comments.article_id, comments.body, '
                . 'comments.visibility, comments.restore_control, comments.version, '
                . 'comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname'
                . $from . ' ORDER BY comments.created_at DESC, comments.id DESC',
                $input,
                $total,
                ['id' => $id],
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->commentResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 個人留言列表不依父文章可見性篩除自己的留言，也不回父文章內容。 */
    public function myComments(Request $request): JsonResponse
    {
        // 查詢只依登入作者限制；個別下架與父文章下架仍能供作者查核。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $input = $this->listQuery($request, false);
            $from = ' FROM comments JOIN accounts ON accounts.id = comments.author_id WHERE '
                . 'comments.author_id = :author';
            $count = $db->prepare('SELECT count(*)' . $from);
            $count->execute(['author' => $viewer->id]);
            $total = (int) $count->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT comments.id, comments.author_id, comments.article_id, comments.body, '
                . 'comments.visibility, comments.restore_control, comments.version, '
                . 'comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname'
                . $from . ' ORDER BY comments.created_at DESC, comments.id DESC',
                $input,
                $total,
                ['author' => (int) $viewer->id],
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->commentResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 登入者只在目前公開的父文章發表留言；父列鎖防止與下架交錯。 */
    public function createComment(Request $request, string $article_id): JsonResponse
    {
        // 認證與父文章在同一交易依固定順序鎖定，新增不能越過已提交下架。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $id = $this->id($article_id, 'article_id');
            $body = $this->contentBody($request, ['body' => 2000]);
            // 下架與新增競爭同一父列鎖，等待後重讀目前狀態才決定可讀及發表權限。
            $select = $db->prepare('SELECT author_id, visibility FROM articles WHERE id = :id FOR UPDATE');
            $select->execute(['id' => $id]);
            $article = $select->fetch();
            if (
                $article === false
                || ($article->visibility === 'hidden' && $viewer->role !== 'admin'
                    && (int) $viewer->id !== (int) $article->author_id)
            ) {
                throw new ApiFailure('E0004');
            }
            if ($article->visibility === 'hidden') {
                throw new ApiFailure('E0003');
            }
            // 父文章保持鎖定直到新增提交，作者與來源固定由伺服器填入。
            $now = $this->sqlTime($this->now($db));
            $insert = $db->prepare(
                "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, "
                . "version, created_backend, created_at, updated_at) VALUES (:article, :author, :body, "
                . "'visible', NULL, 1, 'symfony', :created, :updated)"
            );
            $insert->execute([
                'article' => $id,
                'author' => $viewer->id,
                'body' => $body->body,
                'created' => $now,
                'updated' => $now,
            ]);

            return $this->success([], 201);
        });
    }

    /** 作者及管理員可獨立讀留言；其他會員先按個別與父文章可見性判 404。 */
    public function comment(Request $request, string $comment_id): JsonResponse
    {
        // 只查父文章狀態，不讀其內文，避免作者專用詳情擴張父文章讀取權。
        return $this->database->transaction(function (PDO $db) use ($request, $comment_id): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noGetBody($request);
            $this->noQuery($request);
            $id = $this->id($comment_id, 'comment_id');
            // 只讀父文章可見性供拒絕順序判定，不把父文章內文帶入作者專用詳情。
            $select = $db->prepare(
                'SELECT comments.id, comments.author_id, comments.article_id, comments.body, '
                . 'comments.visibility, comments.restore_control, comments.version, '
                . 'comments.created_backend, comments.created_at, comments.updated_at, '
                . 'accounts.nickname, articles.visibility AS article_visibility FROM comments JOIN '
                . 'accounts ON accounts.id = comments.author_id JOIN articles ON articles.id = '
                . 'comments.article_id WHERE comments.id = :id'
            );
            $select->execute(['id' => $id]);
            $comment = $select->fetch();
            // 其他會員先依隱藏狀態回 404，公開資料才以作者專用端點權限回 403。
            if (
                $comment === false
                || ($viewer->role !== 'admin' && (int) $viewer->id !== (int) $comment->author_id
                    && ($comment->visibility === 'hidden' || $comment->article_visibility === 'hidden'))
            ) {
                throw new ApiFailure('E0004');
            }
            if ($viewer->role !== 'admin' && (int) $viewer->id !== (int) $comment->author_id) {
                throw new ApiFailure('E0003');
            }

            return $this->success($this->commentResource($comment, $viewer));
        });
    }

    /** 作者可修改自己的留言，但父文章下架時任何作者都不得改內文。 */
    public function updateComment(Request $request, string $comment_id): JsonResponse
    {
        return $this->mutateComment($request, $comment_id, false);
    }

    /** 作者或管理員可調整個別留言下架狀態，仍受恢復控制權約束。 */
    public function commentVisibility(Request $request, string $comment_id): JsonResponse
    {
        return $this->mutateComment($request, $comment_id, true);
    }

    /** 先探父 ID，再依文章→留言鎖序重讀，原子判定可讀、權限及版本。 */
    private function mutateComment(Request $request, string $comment_id, bool $visibility): JsonResponse
    {
        // 父列鎖同時序列化文章下架與留言寫入；探查結果不授權，鎖後必須重驗關聯。
        return $this->database->transaction(function (PDO $db) use ($request, $comment_id, $visibility): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $id = $this->id($comment_id, 'comment_id');
            $body = $this->contentBody(
                $request,
                $visibility
                    ? ['visibility' => 'visibility', 'version' => 'version']
                    : ['body' => 2000, 'version' => 'version'],
            );
            // 探查只取得父 ID；文章→留言依序鎖定後，還須重新確認兩者關聯。
            $lookup = $db->prepare('SELECT article_id FROM comments WHERE id = :id');
            $lookup->execute(['id' => $id]);
            $parentId = $lookup->fetchColumn();
            $article = false;
            $content = false;
            if ($parentId !== false) {
                $parent = $db->prepare('SELECT id, visibility FROM articles WHERE id = :id FOR UPDATE');
                $parent->execute(['id' => $parentId]);
                $article = $parent->fetch();
                if ($article !== false) {
                    $select = $db->prepare('SELECT * FROM comments WHERE id = :id FOR UPDATE');
                    $select->execute(['id' => $id]);
                    $content = $select->fetch();
                }
            }
            // 先裁決隱藏與操作權限；父文章下架時禁止改內文，但保留既有可見性管理。
            if (
                $content === false || (int) $content->article_id !== (int) $article->id
                || ($viewer->role !== 'admin' && (int) $viewer->id !== (int) $content->author_id
                    && ($content->visibility === 'hidden' || $article->visibility === 'hidden'))
            ) {
                throw new ApiFailure('E0004');
            }
            if (
                ((int) $viewer->id !== (int) $content->author_id && (! $visibility || $viewer->role !== 'admin'))
                || (! $visibility && $article->visibility !== 'visible')
            ) {
                throw new ApiFailure('E0003');
            }
            $changes = $visibility
                ? $this->visibilityChanges($content, $viewer, $body->visibility)
                : ['body' => $body->body];

            return $this->saveChanges($db, 'comments', $content, $body->version, $changes);
        });
    }

    /** 管理員讀取全部留言，包括留言本身或父文章已下架的資料。 */
    public function adminComments(Request $request): JsonResponse
    {
        // 管理員資格先於 Query；同一交易查核身分並計數分頁，避免略過門禁。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $input = $this->listQuery($request, false);
            $from = ' FROM comments JOIN accounts ON accounts.id = comments.author_id';
            $total = (int) $db->query('SELECT count(*)' . $from)->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT comments.id, comments.author_id, comments.article_id, comments.body, '
                . 'comments.visibility, comments.restore_control, comments.version, '
                . 'comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname'
                . $from . ' ORDER BY comments.created_at DESC, comments.id DESC',
                $input,
                $total,
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->commentResource($row, $viewer);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 留言只回共同欄位與 article_id，不用父文章內容擴張可讀範圍。 */
    private function commentResource(stdClass $comment, ?stdClass $viewer): array
    {
        $own = $viewer !== null && (int) $viewer->id === (int) $comment->author_id;

        return ['id' => (int) $comment->id, 'body' => $comment->body,
            'author' => ['id' => (int) $comment->author_id, 'nickname' => $comment->nickname],
            'article_id' => (int) $comment->article_id, 'version' => (int) $comment->version,
            'created_backend' => $comment->created_backend,
            'created_at' => $this->displayTime($comment->created_at),
            'updated_at' => $this->displayTime($comment->updated_at),
            'visibility' => $comment->visibility,
            'can_restore' => $comment->visibility === 'hidden'
                && ($viewer?->role === 'admin' || ($own && $comment->restore_control === 'author')),
        ];
    }
}
