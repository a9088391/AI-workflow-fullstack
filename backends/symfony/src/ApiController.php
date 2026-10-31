<?php

namespace App;

use JsonException;
use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/** 編排內容、會員管理與鎖定設定；帳號端點另由 AccountController 負責。 */
class ApiController extends HttpController
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

    /** 父文章先判定可讀；預覽下架文章只列 visible 留言，管理員可見個別下架留言。 */
    public function comments(Request $request, string $article_id): JsonResponse
    {
        // 父文章可讀資格與留言可見性在同一讀取流程中決定。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            $identity = $this->authenticate($request, $db, true);
            $viewer = $identity[0] ?? null;
            $input = $this->listQuery($request, false);
            $id = $this->id($article_id, 'article_id');
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

    /** 以目前登入者建立公開文章；來源及時間由伺服器指定，不能由 Body 冒填。 */
    public function createArticle(Request $request): JsonResponse
    {
        // 認證鎖保留到寫入提交，與停用及撤銷登入取得一致先後。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $body = $this->contentBody($request, ['title' => 200, 'body' => 20000]);
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

    /** 登入者只在目前公開的父文章發表留言；父列鎖防止與下架交錯。 */
    public function createComment(Request $request, string $article_id): JsonResponse
    {
        // 認證與父文章在同一交易依固定順序鎖定，新增不能越過已提交下架。
        return $this->database->transaction(function (PDO $db) use ($request, $article_id): JsonResponse {
            [$viewer] = $this->authenticate($request, $db);
            $this->noQuery($request);
            $id = $this->id($article_id, 'article_id');
            $body = $this->contentBody($request, ['body' => 2000]);
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

    /** 管理員以資料庫完成會員字面搜尋、精確計數與穩定分頁，避免載入全部帳號。 */
    public function members(Request $request): JsonResponse
    {
        // 角色門禁先於 Query；先精確計數，有該頁資料才查列表，查詢次數不隨結果筆數增加。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $input = $this->listQuery($request, false, true);
            $keyword = $input['keyword'];
            $filter = " WHERE accounts.role = 'member'";
            $parameters = [];
            if ($keyword !== null && $keyword !== '') {
                // translate 只折疊 ASCII；strpos 將 %, _ 與反斜線視為字面內容。
                $filter .= " AND (strpos(translate(accounts.nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    . "'abcdefghijklmnopqrstuvwxyz'), :nickname) > 0 OR strpos(translate(accounts.email, "
                    . "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), :email) > 0)";
                $folded = strtr($keyword, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
                $parameters = ['nickname' => $folded, 'email' => $folded];
            }
            $count = $db->prepare('SELECT count(*) FROM accounts' . $filter);
            $count->execute($parameters);
            $total = (int) $count->fetchColumn();
            $rows = $this->pageRows(
                $db,
                'SELECT accounts.id, accounts.email, accounts.nickname, accounts.role, '
                . 'accounts.status, accounts.created_at FROM accounts'
                . $filter . ' ORDER BY accounts.created_at DESC, accounts.id DESC',
                $input,
                $total,
                $parameters,
            );
            $list = [];
            foreach ($rows as $row) {
                $list[] = $this->account($row);
            }

            return $this->success(['list' => $list, 'pagination' => $this->pagination($input, $total)]);
        });
    }

    /** 管理員只能讀一般會員；管理員 ID 與不存在的 ID 都以 404 隱藏。 */
    public function member(Request $request, string $member_id): JsonResponse
    {
        // 保留認證鎖直到回應形成；先驗角色，再處理參數及只含一般會員的目標。
        return $this->database->transaction(function (PDO $db) use ($request, $member_id): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $this->noGetBody($request);
            $this->noQuery($request);
            $id = $this->id($member_id, 'member_id');
            $query = $db->prepare(
                "SELECT id, email, nickname, role, status, created_at FROM accounts WHERE id = :id AND role = 'member'"
            );
            $query->execute(['id' => $id]);
            $member = $query->fetch();
            if ($member === false) {
                throw new ApiFailure('E0004');
            }

            return $this->success($this->account($member));
        });
    }

    /** 管理員停用一般會員時同步撤銷舊登入；同值與恢復不重做副作用。 */
    public function memberStatus(Request $request, string $member_id): JsonResponse
    {
        // 先短交易驗角色，讓無權者無法藉 Path、Body 或目標資料探測會員。
        $viewer = $this->database->transaction(function (PDO $db) use ($request): stdClass {
            $account = $this->authenticate($request, $db)[0];
            if ($account->role !== 'admin') {
                throw new ApiFailure('E0003');
            }

            return $account;
        });
        $this->noQuery($request);
        $id = $this->id($member_id, 'member_id');
        $body = $this->contentBody($request, ['status' => 'status']);

        // 帳號按 ID 鎖定，再重驗管理員 session；停用與撤銷不可分開提交。
        return $this->database->transaction(function (PDO $db) use ($request, $viewer, $id, $body): JsonResponse {
            $lock = $db->prepare(
                'SELECT id, role, status FROM accounts WHERE id IN (:viewer, :target) ORDER BY id FOR UPDATE'
            );
            $lock->execute(['viewer' => $viewer->id, 'target' => $id]);
            $accounts = [];
            foreach ($lock->fetchAll() as $account) {
                $accounts[(int) $account->id] = $account;
            }
            $current = $this->authenticate($request, $db)[0];
            if ($current->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $target = $accounts[$id] ?? null;
            if ($target === null || $target->role !== 'member') {
                throw new ApiFailure('E0004');
            }
            if ($target->status === $body->status) {
                return $this->success([]);
            }
            $update = $db->prepare('UPDATE accounts SET status = :status WHERE id = :id');
            $update->execute(['status' => $body->status, 'id' => $id]);
            if ($body->status === 'disabled') {
                $revoke = $db->prepare(
                    'UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE account_id = :id AND '
                    . 'revoked_at IS NULL'
                );
                $revoke->execute(['id' => $id]);
            }

            return $this->success([]);
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

    /** 管理員讀取共用鎖定設定，只公開四個核准欄位。 */
    public function loginLockoutSettings(Request $request): JsonResponse
    {
        // 先驗目前管理員身分，再回傳共用設定，缺少設定不能偽造預設值。
        return $this->database->transaction(function (PDO $db) use ($request): JsonResponse {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $this->noGetBody($request);
            $this->noQuery($request);
            $settings = $db->query('SELECT * FROM login_lockout_settings WHERE id = 1')->fetch();
            if ($settings === false) {
                throw new ApiFailure('E0104');
            }

            return $this->success($this->settingsValues($settings));
        });
    }

    /** 完整覆寫鎖定設定；只有真正關閉才在同一交易解除會員鎖定並換計數世代。 */
    public function updateLoginLockoutSettings(Request $request): JsonResponse
    {
        // 先驗角色及輸入；稍後設定列鎖可能等待，故寫入交易內仍須重新驗證登入。
        $this->database->transaction(function (PDO $db) use ($request): void {
            $viewer = $this->authenticate($request, $db)[0];
            if ($viewer->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
        });
        $this->noQuery($request);
        $body = $this->settingsBody($request);

        // 設定列先取鎖再重驗目前身分；設定與必要的清鎖副作用必須一起提交。
        return $this->database->transaction(function (PDO $db) use ($request, $body): JsonResponse {
            $settings = $db->query('SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE')->fetch();
            if ($settings === false) {
                throw new ApiFailure('E0104');
            }
            $disabling = (bool) $settings->enabled && ! $body->enabled;
            if ($disabling) {
                // 設定→帳號→session 的共同鎖序與登入一致，避免清鎖與登入計數交錯。
                $db->query('SELECT id FROM accounts ORDER BY id FOR UPDATE')->fetchAll();
            }
            $current = $this->authenticate($request, $db)[0];
            if ($current->role !== 'admin') {
                throw new ApiFailure('E0003');
            }
            $values = $this->settingsValues($settings);
            $next = array_replace($values, (array) $body);
            if ($values === $next) {
                return $this->success([]);
            }
            $update = $db->prepare(
                'UPDATE login_lockout_settings SET enabled = :enabled, window_minutes = :window, '
                . 'failure_threshold = :threshold, lock_seconds = :seconds WHERE id = 1'
            );
            $update->bindValue(':enabled', $body->enabled, PDO::PARAM_BOOL);
            $update->bindValue(':window', $body->window_minutes, PDO::PARAM_INT);
            $update->bindValue(':threshold', $body->failure_threshold, PDO::PARAM_INT);
            $update->bindValue(':seconds', $body->lock_seconds, PDO::PARAM_INT);
            $update->execute();
            if ($disabling) {
                $db->exec(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 WHERE role = 'member'"
                );
            }

            return $this->success([]);
        });
    }

    /** 將 PostgreSQL 回傳的字串整數與布林值轉為共同 JSON 型態。 */
    private function settingsValues(stdClass $settings): array
    {
        return ['enabled' => (bool) $settings->enabled, 'window_minutes' => (int) $settings->window_minutes,
            'failure_threshold' => (int) $settings->failure_threshold, 'lock_seconds' => (int) $settings->lock_seconds];
    }

    /** 驗證設定四欄的原始 JSON 型態與範圍，維持已核准的逐欄錯誤順序。 */
    private function settingsBody(Request $request): stdClass
    {
        $raw = $request->getContent();
        if (
            $raw === ''
            || ! str_starts_with(strtolower($request->headers->get('Content-Type', '')), 'application/json')
        ) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiFailure('E0005');
        }
        if (! is_object($body)) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        $limits = ['enabled' => null, 'window_minutes' => 60, 'failure_threshold' => 100, 'lock_seconds' => 86400];
        $errors = [];
        foreach ($limits as $name => $max) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif ($max === null ? ! is_bool($body->$name) : ! is_int($body->$name)) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif ($max !== null && ($body->$name < 1 || $body->$name > $max)) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $limits)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }
        $this->rejectErrors($errors);

        return $body;
    }

    /** 共同 Body 驗證保留欄位順序及逐欄第一個錯誤，嚴格檢查版本整數。 */
    private function contentBody(Request $request, array $fields): stdClass
    {
        $raw = $request->getContent();
        if (
            $raw === ''
            || ! str_starts_with(strtolower($request->headers->get('Content-Type', '')), 'application/json')
        ) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        try {
            $body = json_decode($raw, false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiFailure('E0005');
        }
        if (! is_object($body)) {
            throw new ApiFailure('E0001', ['_body' => ['Request Body 必須為 JSON object']]);
        }
        // 必填、null、型態、格式順序固定；未知與唯讀欄位最後加入明細。
        $errors = [];
        foreach ($fields as $name => $rule) {
            if (! property_exists($body, $name)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif ($body->$name === null) {
                $errors[$name] = ['此欄位不可為 null'];
            } elseif (
                ($rule === 'version' && ! is_int($body->$name))
                || ($rule !== 'version' && ! is_string($body->$name))
            ) {
                $errors[$name] = ['欄位型態不正確'];
            } elseif (
                ($rule === 'version' && ($body->$name < 1 || $body->$name > 9007199254740991))
                || ($rule === 'visibility' && ! in_array($body->$name, ['visible', 'hidden'], true))
                || ($rule === 'status' && ! in_array($body->$name, ['active', 'disabled'], true))
                || (is_int($rule) && ! Rules::validContent($body->$name, $rule))
            ) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        foreach ($body as $name => $_) {
            if (! array_key_exists($name, $fields)) {
                $errors[$name] = ['不支援此欄位'];
            }
        }
        $this->rejectErrors($errors);

        return $body;
    }

    /** 恢復權由目前資料決定；管理員再次下架他人內容會接管控制權。 */
    private function visibilityChanges(stdClass $content, stdClass $viewer, string $visibility): array
    {
        $own = (int) $viewer->id === (int) $content->author_id;
        if (
            $visibility === 'visible' && $content->visibility === 'hidden'
            && $viewer->role !== 'admin' && $content->restore_control !== 'author'
        ) {
            throw new ApiFailure('E0003');
        }
        // 作者重複下架不能移除管理員限制，管理員操作自己內容仍按作者規則。
        $control = $visibility === 'visible' ? null : ($content->restore_control ?? 'author');
        if ($visibility === 'hidden' && $viewer->role === 'admin' && ! $own) {
            $control = 'admin';
        }

        return ['visibility' => $visibility, 'restore_control' => $control];
    }

    /** 列鎖中先驗版本，再以版本條件更新真正變動欄位；同值保持時間與版本。 */
    private function saveChanges(PDO $db, string $table, stdClass $content, int $version, array $changes): JsonResponse
    {
        if ((int) $content->version !== $version) {
            throw new ApiFailure('E0201');
        }
        $changed = false;
        foreach ($changes as $name => $value) {
            if ($content->$name !== $value) {
                $changed = true;
                break;
            }
        }
        if ($changed) {
            // table 與欄位只由本類別固定傳入；不可讓用戶端資料成為 SQL 識別字。
            $assignments = [];
            foreach ($changes as $name => $_) {
                $assignments[] = $name . ' = :' . $name;
            }
            $sql = 'UPDATE ' . $table . ' SET ' . implode(', ', $assignments)
                . ', version = version + 1, updated_at = clock_timestamp() WHERE id = :id AND version = :version';
            $update = $db->prepare($sql);
            $update->execute([...$changes, 'id' => $content->id, 'version' => $version]);
            if ($update->rowCount() !== 1) {
                throw new ApiFailure('E0201');
            }
        }

        return $this->success([]);
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

    /** 逐一解碼原始 Query，驗證重複、陣列、必填分頁及限定的篩選欄位。 */
    private function listQuery(Request $request, bool $scopeAllowed, bool $keywordAllowed = false): array
    {
        $this->noGetBody($request);
        $values = [];
        $errors = [];
        $allowed = [
            'page',
            'per_page',
            ...($scopeAllowed ? ['filters[scope]'] : []),
            ...($keywordAllowed ? ['filters[keyword]'] : []),
        ];
        $raw = $request->server->get('QUERY_STRING', '');
        if ($raw !== '') {
            foreach (explode('&', $raw) as $pair) {
                [$name, $value] = array_pad(explode('=', $pair, 2), 2, '');
                $name = urldecode($name);
                $name = mb_check_encoding($name, 'UTF-8') ? $name : rawurlencode($name);
                $arrayValue = preg_match('/\A(page|per_page|filters\[(?:scope|keyword)\])\[/', $name, $parts) === 1;
                $canonical = $arrayValue ? $parts[1] : $name;
                $errorName = preg_replace('/\Afilters\[([^\]]+)\]\z/', 'filters.$1', $canonical);
                if (array_key_exists($canonical, $values)) {
                    $errors[$errorName] = ['此參數不可重複'];
                } elseif ($arrayValue && in_array($canonical, $allowed, true)) {
                    $errors[$errorName] = ['欄位型態不正確'];
                } elseif (! in_array($canonical, $allowed, true)) {
                    $errors[$errorName] = ['不支援此欄位'];
                }
                $values[$canonical] = urldecode($value);
            }
        }
        foreach (['page', 'per_page'] as $name) {
            if (! array_key_exists($name, $values)) {
                $errors[$name] = ['此欄位為必填'];
            } elseif (
                ! isset($errors[$name])
                && (($number = $this->positiveInteger($values[$name])) === null
                    || ($name === 'per_page' && $number > 100))
            ) {
                $errors[$name] = ['欄位格式或範圍不正確'];
            }
        }
        if (
            $scopeAllowed && isset($values['filters[scope]'])
            && ! isset($errors['filters.scope']) && $values['filters[scope]'] !== 'management'
        ) {
            $errors['filters.scope'] = ['欄位格式或範圍不正確'];
        }
        if ($keywordAllowed && isset($values['filters[keyword]']) && ! isset($errors['filters.keyword'])) {
            // 關鍵字只去除 ASCII 空格；無效 UTF-8 與 NUL 不可送入 PostgreSQL text。
            $keyword = trim($values['filters[keyword]'], ' ');
            if (
                ! mb_check_encoding($keyword, 'UTF-8')
                || str_contains($keyword, "\0") || mb_strlen($keyword, 'UTF-8') > 254
            ) {
                $errors['filters.keyword'] = ['欄位格式或範圍不正確'];
            }
        }
        $this->rejectErrors($errors);

        return ['page' => (int) $values['page'], 'per_page' => (int) $values['per_page'],
            'scope' => $values['filters[scope]'] ?? null,
            'keyword' => isset($values['filters[keyword]']) ? trim($values['filters[keyword]'], ' ') : null];
    }

    /** URL 整數接受前導零，但值須落在 JavaScript 安全整數範圍。 */
    private function positiveInteger(string $value): ?int
    {
        if (preg_match('/\A[0-9]+\z/D', $value) !== 1) {
            return null;
        }
        $digits = ltrim($value, '0');
        if (
            $digits === '' || strlen($digits) > 16
            || (strlen($digits) === 16 && strcmp($digits, '9007199254740991') > 0)
        ) {
            return null;
        }

        return (int) $digits;
    }

    /** 將 Path ID 的格式錯誤固定回傳對應欄位，不查詢不存在的資源。 */
    private function id(string $value, string $field): int
    {
        $id = $this->positiveInteger($value);
        if ($id === null) {
            throw new ApiFailure('E0001', [$field => ['欄位格式或範圍不正確']]);
        }

        return $id;
    }

    /** 精確 count 後使用穩定排序；巨大合法頁碼直接回空頁，避免 offset 溢位。 */
    private function pageRows(PDO $db, string $sql, array $input, int $total, array $parameters = []): array
    {
        $offset = intdiv(PHP_INT_MAX, $input['per_page']) < $input['page'] - 1
            ? null
            : ($input['page'] - 1) * $input['per_page'];
        if ($offset === null || $offset >= $total) {
            return [];
        }
        $query = $db->prepare($sql . ' LIMIT :limit OFFSET :offset');
        foreach ($parameters as $name => $value) {
            $query->bindValue(':' . $name, $value, is_int($value) ? PDO::PARAM_INT : PDO::PARAM_STR);
        }
        $query->bindValue(':limit', $input['per_page'], PDO::PARAM_INT);
        $query->bindValue(':offset', $offset, PDO::PARAM_INT);
        $query->execute();

        return $query->fetchAll();
    }

    /** 分頁保留請求頁碼與範圍內精確總數，空集合的總頁數為零。 */
    private function pagination(array $input, int $total): array
    {
        return [
            'page' => $input['page'],
            'per_page' => $input['per_page'],
            'total' => $total,
            'total_pages' => intdiv($total, $input['per_page']) + (int) ($total % $input['per_page'] !== 0),
        ];
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
