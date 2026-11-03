package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

// writeTx 依帳號、session 順序加鎖重驗，避免外層認證後被停用或撤銷仍寫入。
func (s *server) writeTx(c *gin.Context, viewer *account, token *claims) (*sql.Tx, *account, bool) {
	ctx := c.Request.Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, fmt.Errorf("begin protected write: %w", err))
		return nil, nil, false
	}
	// 外層認證後仍可能等待他人停用或登出；帳號鎖先於 session，鎖後重新裁決。
	var current account
	err = tx.QueryRowContext(ctx, "SELECT id,role,status FROM accounts WHERE id=$1 FOR UPDATE", viewer.ID).Scan(&current.ID, &current.Role, &current.Status)
	if errors.Is(err, sql.ErrNoRows) {
		tx.Rollback()
		respondError(c, &apiError{code: "E0002"})
		return nil, nil, false
	}
	if err != nil {
		tx.Rollback()
		failInternal(c, fmt.Errorf("lock writer account: %w", err))
		return nil, nil, false
	}
	var accountID int64
	var backendID string
	var issued, expires time.Time
	var revoked sql.NullTime
	err = tx.QueryRowContext(ctx, "SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE", token.JTI).Scan(&accountID, &backendID, &issued, &expires, &revoked)
	if errors.Is(err, sql.ErrNoRows) {
		tx.Rollback()
		respondError(c, &apiError{code: "E0002"})
		return nil, nil, false
	}
	if err != nil {
		tx.Rollback()
		failInternal(c, fmt.Errorf("lock writer session: %w", err))
		return nil, nil, false
	}
	// 先核對 JWT 與紀錄的身分、精確時間及當下到期界線；停用只優先於撤銷。
	if accountID != current.ID || backendID != backend || !issued.Equal(time.Unix(token.Iat, 0)) || !expires.Equal(time.Unix(token.Exp, 0)) {
		tx.Rollback()
		respondError(c, &apiError{code: "E0002"})
		return nil, nil, false
	}
	if time.Now().Unix() >= token.Exp {
		tx.Rollback()
		respondError(c, &apiError{code: "E0002"})
		return nil, nil, false
	}
	if current.Status == "disabled" {
		tx.Rollback()
		respondError(c, &apiError{code: "E0102"})
		return nil, nil, false
	}
	if revoked.Valid {
		tx.Rollback()
		respondError(c, &apiError{code: "E0002"})
		return nil, nil, false
	}
	// 失敗分支已自行回滾；成功時把交易交給 handler，負責 Commit 或 defer Rollback。
	return tx, &current, true
}

// createArticle 在認證鎖內寫入作者與實際建立後端，讓建立與更新時間相同。
func (s *server) createArticle(c *gin.Context) {
	// 先驗登入，再驗 Query 與 Body，無效輸入不得進入寫入交易。
	viewer, token, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request, []string{"title", "body"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	tx, current, ok := s.writeTx(c, viewer, token)
	if !ok {
		return
	}
	defer tx.Rollback()
	// 時鐘於取得認證鎖後取一次；來源及作者均不接受用戶端輸入。
	_, err := tx.ExecContext(c.Request.Context(), "WITH t AS (SELECT clock_timestamp() AS value) INSERT INTO articles (author_id,title,body,visibility,restore_control,version,created_backend,created_at,updated_at) SELECT $1,$2,$3,'visible',NULL,1,'gin',t.value,t.value FROM t", current.ID, body["title"], body["body"])
	if err != nil {
		failInternal(c, fmt.Errorf("create article: %w", err))
		return
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit article: %w", err))
		return
	}
	respondOK(c, 201, nil)
}

// myArticles 只列登入者自己的文章，包含下架內容但不擴大其他人的讀取權。
func (s *server) myArticles(c *gin.Context) {
	viewer, _, ok := s.identity(c, true)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request, "")
	if e != nil {
		respondError(c, e)
		return
	}
	s.ownedList(c, viewer, page, perPage, true)
}

// myComments 只列登入者自己的留言，不因父文章下架而失去自己的詳情入口。
func (s *server) myComments(c *gin.Context) {
	viewer, _, ok := s.identity(c, true)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request, "")
	if e != nil {
		respondError(c, e)
		return
	}
	s.ownedList(c, viewer, page, perPage, false)
}

// ownedList 沿用公開列表的排序、分頁與資源形狀，只將範圍限定為作者本人。
func (s *server) ownedList(c *gin.Context, viewer *account, page, perPage int64, article bool) {
	table, columns := "comments", commentColumns
	reader := readCommentRow
	if article {
		table, columns, reader = "articles", articleColumns, readArticleRow
	}
	// 計數與取頁使用相同作者條件；沒有快照分頁承諾，也不逐筆追加查詢。
	base := " FROM " + table + " JOIN accounts ON accounts.id=" + table + ".author_id WHERE " + table + ".author_id=$1"
	ctx := c.Request.Context()
	var total int64
	if err := s.db.QueryRowContext(ctx, "SELECT count(*)"+base, viewer.ID).Scan(&total); err != nil {
		failInternal(c, fmt.Errorf("count owned content: %w", err))
		return
	}
	list := []gin.H{}
	offset, inRange := safeOffset(page, perPage)
	if inRange && offset < total {
		rows, err := s.db.QueryContext(ctx, "SELECT "+columns+base+" ORDER BY "+table+".created_at DESC,"+table+".id DESC LIMIT $2 OFFSET $3", viewer.ID, perPage, offset)
		if err != nil {
			failInternal(c, fmt.Errorf("list owned content: %w", err))
			return
		}
		// 即使中途掃描失敗仍釋放查詢資源；迭代錯誤不能回成不完整成功列表。
		defer rows.Close()
		for rows.Next() {
			row, err := reader(rows)
			if err != nil {
				failInternal(c, fmt.Errorf("scan owned content: %w", err))
				return
			}
			list = append(list, contentData(row, viewer, article))
		}
		if err := rows.Err(); err != nil {
			failInternal(c, fmt.Errorf("iterate owned content: %w", err))
			return
		}
	}
	respondOK(c, 200, gin.H{"list": list, "pagination": pagination(page, perPage, total)})
}

// createComment 鎖住公開父文章後新增留言，使文章下架與留言建立不能交錯通過。
func (s *server) createComment(c *gin.Context) {
	// 先驗登入，再驗 Query／Path／Body，無效輸入不得進入寫入交易。
	viewer, token, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	id, e := urlID(c.Param("article_id"), "article_id")
	if e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request, []string{"body"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	tx, current, ok := s.writeTx(c, viewer, token)
	if !ok {
		return
	}
	defer tx.Rollback()
	// 父列與文章下架使用相同鎖；作者或管理員能預覽下架文，但不能新增留言。
	var authorID int64
	var state string
	err := tx.QueryRowContext(c.Request.Context(), "SELECT author_id,visibility FROM articles WHERE id=$1 FOR UPDATE", id).Scan(&authorID, &state)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("lock comment parent: %w", err))
		return
	}
	if state == "hidden" {
		if current.Role == "admin" || current.ID == authorID {
			respondError(c, &apiError{code: "E0003"})
		} else {
			respondError(c, &apiError{code: "E0004"})
		}
		return
	}
	_, err = tx.ExecContext(c.Request.Context(), "WITH t AS (SELECT clock_timestamp() AS value) INSERT INTO comments (article_id,author_id,body,visibility,restore_control,version,created_backend,created_at,updated_at) SELECT $1,$2,$3,'visible',NULL,1,'gin',t.value,t.value FROM t", id, current.ID, body["body"])
	if err != nil {
		failInternal(c, fmt.Errorf("create comment: %w", err))
		return
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit comment: %w", err))
		return
	}
	respondOK(c, 201, nil)
}

// commentDetail 讓作者及管理員獨立讀取留言，其他會員先判隱藏再判詳情權限。
func (s *server) commentDetail(c *gin.Context) {
	viewer, _, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noGetBody(c.Request); e != nil {
		respondError(c, e)
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	id, e := urlID(c.Param("comment_id"), "comment_id")
	if e != nil {
		respondError(c, e)
		return
	}
	// 一次讀取留言與父文章狀態，其他會員須先過隱藏 404 才判作者詳情 403。
	var row content
	var parentState string
	query := "SELECT " + commentColumns + ",articles.visibility FROM comments JOIN accounts ON accounts.id=comments.author_id JOIN articles ON articles.id=comments.article_id WHERE comments.id=$1"
	err := s.db.QueryRowContext(c.Request.Context(), query, id).Scan(&row.ID, &row.AuthorID, &row.ArticleID, &row.Body, &row.Visibility, &row.Restore, &row.Version, &row.Backend, &row.Created, &row.Updated, &row.Nickname, &parentState)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("read comment detail: %w", err))
		return
	}
	if viewer.Role != "admin" && viewer.ID != row.AuthorID {
		if row.Visibility == "hidden" || parentState == "hidden" {
			respondError(c, &apiError{code: "E0004"})
		} else {
			respondError(c, &apiError{code: "E0003"})
		}
		return
	}
	respondOK(c, 200, contentData(row, viewer, false))
}

// editArticle 將文章內文修改交由共同交易流程，以同一順序裁決權限與版本。
func (s *server) editArticle(c *gin.Context) { s.mutateContent(c, true, false) }

// setArticleVisibility 將文章下架與恢復交由共同交易流程，保留恢復控制權。
func (s *server) setArticleVisibility(c *gin.Context) { s.mutateContent(c, true, true) }

// editComment 將留言修改交由共同交易流程，並在交易內重驗父文章公開。
func (s *server) editComment(c *gin.Context) { s.mutateContent(c, false, false) }

// setCommentVisibility 將留言下架與恢復交由共同交易流程，不將父文章狀態改寫。
func (s *server) setCommentVisibility(c *gin.Context) { s.mutateContent(c, false, true) }

// mutateContent 共用四種版本寫入的驗證與鎖順序，避免同值或競爭繞過權限。
func (s *server) mutateContent(c *gin.Context, article, visibility bool) {
	// 先驗登入，再驗 Query／Path／Body，無效輸入不得進入寫入交易。
	viewer, token, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	name := "comment_id"
	if article {
		name = "article_id"
	}
	id, e := urlID(c.Param(name), name)
	if e != nil {
		respondError(c, e)
		return
	}
	fields := []string{"body", "version"}
	if visibility {
		fields = []string{"visibility", "version"}
	} else if article {
		fields = []string{"title", "body", "version"}
	}
	body, e := bodyInput(c.Request, fields, false)
	if e != nil {
		respondError(c, e)
		return
	}
	version, _ := strconv.ParseInt(body["version"], 10, 64)
	tx, current, ok := s.writeTx(c, viewer, token)
	if !ok {
		return
	}
	defer tx.Rollback()
	ctx := c.Request.Context()
	var row content
	var lockErr error
	parentState := "visible"
	if article {
		row, lockErr = lockedArticle(ctx, tx, id)
	} else {
		// 先探索父 ID，再鎖文章及留言；探索不持有留言鎖，也不授予內容讀取權。
		var parentID int64
		err := tx.QueryRowContext(ctx, "SELECT article_id FROM comments WHERE id=$1", id).Scan(&parentID)
		if errors.Is(err, sql.ErrNoRows) {
			respondError(c, &apiError{code: "E0004"})
			return
		}
		if err != nil {
			failInternal(c, fmt.Errorf("find comment parent: %w", err))
			return
		}
		err = tx.QueryRowContext(ctx, "SELECT visibility FROM articles WHERE id=$1 FOR UPDATE", parentID).Scan(&parentState)
		if errors.Is(err, sql.ErrNoRows) {
			respondError(c, &apiError{code: "E0004"})
			return
		}
		if err != nil {
			failInternal(c, fmt.Errorf("lock comment parent: %w", err))
			return
		}
		row, lockErr = lockedComment(ctx, tx, id)
		if lockErr == nil && row.ArticleID != parentID {
			lockErr = sql.ErrNoRows
		}
	}
	if errors.Is(lockErr, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if lockErr != nil {
		failInternal(c, fmt.Errorf("lock content: %w", lockErr))
		return
	}
	// 可讀性先於操作權；父文章下架時，其他會員不能據留言推知其存在。
	if e := contentAccess(row, parentState, current, article, visibility); e != nil {
		respondError(c, e)
		return
	}
	control := row.Restore.String
	if visibility {
		control, e = visibilityControl(row, current, body["visibility"])
		if e != nil {
			respondError(c, e)
			return
		}
	}
	// 權限與恢復資格都通過後才比版本；過期的同值請求仍必須回 409。
	if row.Version != version {
		respondError(c, &apiError{code: "E0201"})
		return
	}
	// 正確版本而資料相同時不執行 UPDATE，因此 version 和 updated_at 都不變。
	changed := false
	if visibility {
		changed = row.Visibility != body["visibility"] || control != row.Restore.String
	} else if article {
		changed = row.Title != body["title"] || row.Body != body["body"]
	} else {
		changed = row.Body != body["body"]
	}
	if changed {
		if err := saveContent(ctx, tx, row, body, article, visibility, control); err != nil {
			failInternal(c, err)
			return
		}
	}
	if err := tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit content change: %w", err))
		return
	}
	respondOK(c, 200, nil)
}

// contentAccess 依可讀性、操作權順序判定，防止對隱藏內容洩漏存在與版本。
func contentAccess(row content, parentState string, viewer *account, article, visibility bool) *apiError {
	if viewer.Role != "admin" && viewer.ID != row.AuthorID && (row.Visibility == "hidden" || parentState == "hidden") {
		return &apiError{code: "E0004"}
	}
	if (viewer.ID != row.AuthorID && (!visibility || viewer.Role != "admin")) || (!article && !visibility && parentState == "hidden") {
		return &apiError{code: "E0003"}
	}
	return nil
}

// visibilityControl 計算恢復控制者；管理員處理別人的下架會接管，作者不能解除限制。
func visibilityControl(row content, viewer *account, target string) (string, *apiError) {
	control := row.Restore.String
	if target == "visible" && row.Visibility == "hidden" && viewer.Role != "admin" && control != "author" {
		return "", &apiError{code: "E0003"}
	}
	if target == "visible" {
		return "", nil
	}
	if viewer.Role == "admin" && viewer.ID != row.AuthorID {
		return "admin", nil
	}
	if control == "" {
		return "author", nil
	}
	return control, nil
}

// lockedArticle 鎖定單篇文章並讀取目前版本，不額外鎖住作者帳號以保持共同順序。
func lockedArticle(ctx context.Context, tx *sql.Tx, id int64) (content, error) {
	row, err := readArticleRow(tx.QueryRowContext(ctx, "SELECT "+articleColumns+" FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.id=$1 FOR UPDATE OF articles", id))
	return row, err
}

// lockedComment 由已鎖父文章的流程呼叫，只鎖留言本身以維持文章→留言順序。
func lockedComment(ctx context.Context, tx *sql.Tx, id int64) (content, error) {
	row, err := readCommentRow(tx.QueryRowContext(ctx, "SELECT "+commentColumns+" FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.id=$1 FOR UPDATE OF comments", id))
	return row, err
}

// saveContent 只更新允許的內容欄位，並以版本條件保護跨後端寫入。
func saveContent(ctx context.Context, tx *sql.Tx, row content, body map[string]string, article, visibility bool, control string) error {
	// 僅寫入這次操作允許的欄位；建立來源及其他欄位保持原值。
	var result sql.Result
	var err error
	if visibility {
		var restore any
		if control != "" {
			restore = control
		}
		if article {
			result, err = tx.ExecContext(ctx, "UPDATE articles SET visibility=$1,restore_control=$2,version=version+1,updated_at=clock_timestamp() WHERE id=$3 AND version=$4", body["visibility"], restore, row.ID, row.Version)
		} else {
			result, err = tx.ExecContext(ctx, "UPDATE comments SET visibility=$1,restore_control=$2,version=version+1,updated_at=clock_timestamp() WHERE id=$3 AND version=$4", body["visibility"], restore, row.ID, row.Version)
		}
	} else if article {
		result, err = tx.ExecContext(ctx, "UPDATE articles SET title=$1,body=$2,version=version+1,updated_at=clock_timestamp() WHERE id=$3 AND version=$4", body["title"], body["body"], row.ID, row.Version)
	} else {
		result, err = tx.ExecContext(ctx, "UPDATE comments SET body=$1,version=version+1,updated_at=clock_timestamp() WHERE id=$2 AND version=$3", body["body"], row.ID, row.Version)
	}
	if err != nil {
		return fmt.Errorf("update content: %w", err)
	}
	// 已持有內容鎖且帶版本條件，更新筆數異常須失敗，不能回報假成功。
	n, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("check updated content: %w", err)
	}
	if n != 1 {
		return fmt.Errorf("locked content version changed before update: id=%d", row.ID)
	}
	return nil
}
