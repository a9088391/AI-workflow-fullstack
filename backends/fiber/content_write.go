package main

import (
	"database/sql"
	"errors"
	"strconv"
	"strings"
	"time"
)

// writeIdentity 在同一交易依帳號、session 順序鎖定並重驗，避免撤銷先提交後仍寫入。
func (s *server) writeIdentity(c *requestContext) (*sql.Tx, *account, *apiError, error) {
	authorization := c.Request().Header.Get("Authorization")
	parts := splitBearer(authorization)
	if parts == "" {
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	claims, e := verifyToken(s.key, parts)
	if e != nil {
		return nil, nil, e, nil
	}
	ctx := c.Request().Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, nil, nil, err
	}
	// 先鎖帳號再鎖 session；交付呼叫者前的任何失敗都由這裡回滾。
	id, _ := strconv.ParseInt(claims.Sub, 10, 64)
	var a account
	err = tx.QueryRowContext(ctx, "SELECT id,nickname,role,status FROM accounts WHERE id=$1 FOR UPDATE", id).Scan(&a.ID, &a.Nickname, &a.Role, &a.Status)
	if errors.Is(err, sql.ErrNoRows) {
		tx.Rollback()
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	if err != nil {
		tx.Rollback()
		return nil, nil, nil, err
	}
	var accountID int64
	var source string
	var issued, expires time.Time
	var revoked sql.NullTime
	err = tx.QueryRowContext(ctx, "SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE", claims.JTI).Scan(&accountID, &source, &issued, &expires, &revoked)
	if errors.Is(err, sql.ErrNoRows) {
		tx.Rollback()
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	if err != nil {
		tx.Rollback()
		return nil, nil, nil, err
	}
	var now time.Time
	if err = tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
		tx.Rollback()
		return nil, nil, nil, err
	}
	// 停用身分先於撤銷回覆；claims、session 與資料庫時間須同時有效。
	if accountID != a.ID || source != backend || !issued.Equal(time.Unix(claims.Iat, 0)) || !expires.Equal(time.Unix(claims.Exp, 0)) || now.Unix() < claims.Nbf || now.Unix() >= claims.Exp {
		tx.Rollback()
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	if a.Status == "disabled" {
		tx.Rollback()
		return nil, nil, &apiError{code: "E0102"}, nil
	}
	if revoked.Valid {
		tx.Rollback()
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	// 成功後交易仍開啟，呼叫者負責提交並以 defer 回滾其餘離開路徑。
	return tx, &a, nil, nil
}

// splitBearer 僅接受一段 Bearer token，避免 HTTP 標頭的額外空白產生不同身分解讀。
func splitBearer(authorization string) string {
	parts := strings.Split(authorization, " ")
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] == "" {
		return ""
	}
	return parts[1]
}

// writeStart 統一受保護寫入的認證錯誤，成功時由呼叫者持有交易到提交。
func (s *server) writeStart(c *requestContext) (*sql.Tx, *account) {
	tx, viewer, e, err := s.writeIdentity(c)
	if err != nil {
		failInternal(c, err)
	} else if e != nil {
		respondError(c, e)
	}
	return tx, viewer
}

// writeDone 只在所有驗證與 SQL 寫入成功後提交，避免部分副作用外洩。
func writeDone(c *requestContext, tx *sql.Tx, status int) {
	if err := tx.Commit(); err != nil {
		failInternal(c, err)
		return
	}
	respondOK(c, status, nil)
}

// writeInput 先依序檢查 Query、Path、Body，遇錯即停止，避免提前查詢內容目標。
func writeInput(c *requestContext, pathName string, fields []string) (int64, map[string]string, bool) {
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return 0, nil, false
	}
	var id int64
	if pathName != "" {
		var e *apiError
		id, e = urlID(c.Param(pathName), pathName)
		if e != nil {
			respondError(c, e)
			return 0, nil, false
		}
	}
	body, e := bodyInput(c.Request(), fields, false)
	if e != nil {
		respondError(c, e)
		return 0, nil, false
	}
	return id, body, true
}

// createArticle 使用已鎖定的登入狀態寫入原文、作者與固定 Fiber 來源。
func (s *server) createArticle(c *requestContext) {
	tx, viewer := s.writeStart(c)
	if tx == nil {
		return
	}
	defer tx.Rollback()
	_, body, ok := writeInput(c, "", []string{"title", "body"})
	if !ok {
		return
	}
	// 單次資料庫時刻同時填入建立與更新時間，讓新文章的兩欄完全一致。
	_, err := tx.ExecContext(c.Request().Context(), "INSERT INTO articles (author_id,title,body,visibility,restore_control,version,created_backend,created_at,updated_at) SELECT $1,$2,$3,'visible',NULL,1,'fiber',t.now,t.now FROM (SELECT clock_timestamp() AS now) t", viewer.ID, body["title"], body["body"])
	if err != nil {
		failInternal(c, err)
		return
	}
	writeDone(c, tx, 201)
}

// createComment 鎖父文章後新增留言，避免下架與新增各讀到舊狀態。
func (s *server) createComment(c *requestContext) {
	// 先重驗登入並持有交易，格式失敗或權限拒絕都不留下部分新增。
	tx, viewer := s.writeStart(c)
	if tx == nil {
		return
	}
	defer tx.Rollback()
	id, body, ok := writeInput(c, "article_id", []string{"body"})
	if !ok {
		return
	}
	// 與文章下架鎖同一父列，再分辨不可讀的 404 與可預覽但禁止新增的 403。
	var authorID int64
	var visibility string
	err := tx.QueryRowContext(c.Request().Context(), "SELECT author_id,visibility FROM articles WHERE id=$1 FOR UPDATE", id).Scan(&authorID, &visibility)
	if errors.Is(err, sql.ErrNoRows) || err == nil && visibility == "hidden" && viewer.Role != "admin" && viewer.ID != authorID {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	if visibility == "hidden" {
		respondError(c, &apiError{code: "E0003"})
		return
	}
	_, err = tx.ExecContext(c.Request().Context(), "INSERT INTO comments (article_id,author_id,body,visibility,restore_control,version,created_backend,created_at,updated_at) SELECT $1,$2,$3,'visible',NULL,1,'fiber',t.now,t.now FROM (SELECT clock_timestamp() AS now) t", id, viewer.ID, body["body"])
	if err != nil {
		failInternal(c, err)
		return
	}
	writeDone(c, tx, 201)
}

// changeArticle 依鎖後的可讀性、操作權與版本次序決定編修或下架。
func (s *server) changeArticle(c *requestContext, visibility bool) {
	// 先認證並持有帳號與 session 鎖，之後的格式錯誤或拒絕統一回滾。
	tx, viewer := s.writeStart(c)
	if tx == nil {
		return
	}
	defer tx.Rollback()
	fields := []string{"title", "body", "version"}
	if visibility {
		fields = []string{"visibility", "version"}
	}
	id, body, ok := writeInput(c, "article_id", fields)
	if !ok {
		return
	}
	// 文章列鎖涵蓋可讀性、操作權、版本比對與更新，避免分開查核後被覆寫。
	var row content
	err := tx.QueryRowContext(c.Request().Context(), "SELECT author_id,title,body,visibility,restore_control,version FROM articles WHERE id=$1 FOR UPDATE", id).Scan(&row.AuthorID, &row.Title, &row.Body, &row.Visibility, &row.Restore, &row.Version)
	if errors.Is(err, sql.ErrNoRows) || err == nil && row.Visibility == "hidden" && viewer.Role != "admin" && viewer.ID != row.AuthorID {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	if viewer.ID != row.AuthorID && (!visibility || viewer.Role != "admin") {
		respondError(c, &apiError{code: "E0003"})
		return
	}
	if visibility {
		if e := restoreAllowed(row, viewer, body["visibility"]); e != nil {
			respondError(c, e)
			return
		}
	}
	// 權限通過後才裁決版本；同值的過期請求也不能當成成功。
	if strconv.FormatInt(row.Version, 10) != body["version"] {
		respondError(c, &apiError{code: "E0201"})
		return
	}
	// 恢復權變更也算實際更新；完全同值則不動版本與微秒時間。
	if visibility {
		control := nextRestore(row, viewer, body["visibility"])
		if row.Visibility != body["visibility"] || row.Restore.String != control {
			_, err = tx.ExecContext(c.Request().Context(), "UPDATE articles SET visibility=$1,restore_control=$2,version=version+1,updated_at=clock_timestamp() WHERE id=$3 AND version=$4", body["visibility"], nullableControl(control), id, row.Version)
		}
	} else if row.Title != body["title"] || row.Body != body["body"] {
		_, err = tx.ExecContext(c.Request().Context(), "UPDATE articles SET title=$1,body=$2,version=version+1,updated_at=clock_timestamp() WHERE id=$3 AND version=$4", body["title"], body["body"], id, row.Version)
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	writeDone(c, tx, 200)
}

// changeComment 先鎖父文章再鎖留言，維持與下架文章一致的交易順序。
func (s *server) changeComment(c *requestContext, visibility bool) {
	// 先完成登入與格式檢查，避免無效請求提前查詢留言是否存在。
	tx, viewer := s.writeStart(c)
	if tx == nil {
		return
	}
	defer tx.Rollback()
	fields := []string{"body", "version"}
	if visibility {
		fields = []string{"visibility", "version"}
	}
	id, body, ok := writeInput(c, "comment_id", fields)
	if !ok {
		return
	}
	// 先無鎖讀父 ID；文章鎖取得後再鎖留言並核對關聯。
	var parentID int64
	err := tx.QueryRowContext(c.Request().Context(), "SELECT article_id FROM comments WHERE id=$1", id).Scan(&parentID)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	var parentVisibility string
	err = tx.QueryRowContext(c.Request().Context(), "SELECT visibility FROM articles WHERE id=$1 FOR UPDATE", parentID).Scan(&parentVisibility)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	var row content
	err = tx.QueryRowContext(c.Request().Context(), "SELECT article_id,author_id,body,visibility,restore_control,version FROM comments WHERE id=$1 FOR UPDATE", id).Scan(&row.ArticleID, &row.AuthorID, &row.Body, &row.Visibility, &row.Restore, &row.Version)
	if errors.Is(err, sql.ErrNoRows) || err == nil && row.ArticleID != parentID {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	// 先隱藏不可讀目標，再判操作權；作者的留言例外不允許在父文下架時改內文。
	if viewer.Role != "admin" && viewer.ID != row.AuthorID && (row.Visibility == "hidden" || parentVisibility == "hidden") {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if viewer.ID != row.AuthorID && (!visibility || viewer.Role != "admin") || !visibility && parentVisibility != "visible" {
		respondError(c, &apiError{code: "E0003"})
		return
	}
	if visibility {
		if e := restoreAllowed(row, viewer, body["visibility"]); e != nil {
			respondError(c, e)
			return
		}
	}
	// 權限通過後才裁決版本；同值的過期請求也不能當成成功。
	if strconv.FormatInt(row.Version, 10) != body["version"] {
		respondError(c, &apiError{code: "E0201"})
		return
	}
	// 恢復權變更也算實際更新；完全同值則不動版本與微秒時間。
	if visibility {
		control := nextRestore(row, viewer, body["visibility"])
		if row.Visibility != body["visibility"] || row.Restore.String != control {
			_, err = tx.ExecContext(c.Request().Context(), "UPDATE comments SET visibility=$1,restore_control=$2,version=version+1,updated_at=clock_timestamp() WHERE id=$3 AND version=$4", body["visibility"], nullableControl(control), id, row.Version)
		}
	} else if row.Body != body["body"] {
		_, err = tx.ExecContext(c.Request().Context(), "UPDATE comments SET body=$1,version=version+1,updated_at=clock_timestamp() WHERE id=$2 AND version=$3", body["body"], id, row.Version)
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	writeDone(c, tx, 200)
}

// restoreAllowed 先阻擋無恢復權的作者，避免同值或過期版本繞過管理員下架。
func restoreAllowed(row content, viewer *account, desired string) *apiError {
	if desired == "visible" && row.Visibility == "hidden" && viewer.Role != "admin" && row.Restore.String != "author" {
		return &apiError{code: "E0003"}
	}
	return nil
}

// nextRestore 決定下架控制者；管理員接管他人內容屬實際版本變更。
func nextRestore(row content, viewer *account, desired string) string {
	if desired == "visible" {
		return ""
	}
	if viewer.Role == "admin" && viewer.ID != row.AuthorID {
		return "admin"
	}
	if row.Restore.Valid {
		return row.Restore.String
	}
	return "author"
}

// nullableControl 將公開狀態的空控制者寫為 SQL NULL，以符合資料表約束。
func nullableControl(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// updateArticle 僅允許作者編修內文，管理員也不能代改他人文字。
func (s *server) updateArticle(c *requestContext) { s.changeArticle(c, false) }

// articleVisibility 讓作者或管理員在同一列鎖內調整下架及恢復權。
func (s *server) articleVisibility(c *requestContext) { s.changeArticle(c, true) }

// updateComment 僅允許作者在父文章公開時編修自己的留言文字。
func (s *server) updateComment(c *requestContext) { s.changeComment(c, false) }

// commentVisibility 讓有資格者在父文章下架時仍可調整個別留言狀態。
func (s *server) commentVisibility(c *requestContext) { s.changeComment(c, true) }
