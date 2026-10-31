<?php

namespace App;

use PDO;
use stdClass;
use Symfony\Component\HttpFoundation\JsonResponse;

/** 只共用文章與留言實際使用的恢復權與版本寫入規則，交易及鎖定仍由端點持有。 */
abstract class ContentController extends HttpController
{
    /** 恢復權由目前資料決定；管理員再次下架他人內容會接管控制權。 */
    protected function visibilityChanges(stdClass $content, stdClass $viewer, string $visibility): array
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
    protected function saveChanges(
        PDO $db,
        string $table,
        stdClass $content,
        int $version,
        array $changes
    ): JsonResponse {
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
            // table 與欄位由文章／留言端點固定傳入，禁止用戶端提供 SQL 識別字。
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
}
