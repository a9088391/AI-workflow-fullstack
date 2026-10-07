package main

import (
	"database/sql"
	"errors"

	"github.com/gin-gonic/gin"
)

const articleColumns = "articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname"
const commentColumns = "comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname"

// content 保存文章或留言的共同讀取欄位，還原權限於回應時重算。
type content struct {
	ID, AuthorID, ArticleID, Version           int64
	Title, Body, Visibility, Backend, Nickname string
	Restore                                    sql.NullString
	Created, Updated                           sql.NullTime
}

// contentData 只輸出公開作者與目前使用者可恢復的內容欄位。
func contentData(row content, viewer *account, article bool) gin.H {
	canRestore := row.Visibility == "hidden" && viewer != nil && (viewer.Role == "admin" || viewer.ID == row.AuthorID && row.Restore.String == "author")
	result := gin.H{"id": row.ID, "author": gin.H{"id": row.AuthorID, "nickname": row.Nickname}, "body": row.Body, "version": row.Version, "created_backend": row.Backend, "created_at": businessTime(row.Created.Time), "updated_at": businessTime(row.Updated.Time), "visibility": row.Visibility, "can_restore": canRestore}
	if article {
		result["title"] = row.Title
	} else {
		result["article_id"] = row.ArticleID
	}
	return result
}

// readArticleRow 依固定欄位順序取單篇文章，避免 SQL 欄位流入 API。
func readArticleRow(scanner interface{ Scan(...any) error }) (content, error) {
	var row content
	err := scanner.Scan(&row.ID, &row.AuthorID, &row.Title, &row.Body, &row.Visibility, &row.Restore, &row.Version, &row.Backend, &row.Created, &row.Updated, &row.Nickname)
	return row, err
}

// readCommentRow 依固定欄位順序取單則留言與其父文章 ID。
func readCommentRow(scanner interface{ Scan(...any) error }) (content, error) {
	var row content
	err := scanner.Scan(&row.ID, &row.AuthorID, &row.ArticleID, &row.Body, &row.Visibility, &row.Restore, &row.Version, &row.Backend, &row.Created, &row.Updated, &row.Nickname)
	return row, err
}

// pagination 保留請求頁碼及精確總數，超頁也不改成最後一頁。
func pagination(page, perPage, total int64) gin.H {
	return gin.H{"page": page, "per_page": perPage, "total": total, "total_pages": (total + perPage - 1) / perPage}
}

// managementRequested 在其他格式錯誤之前辨識明確管理範圍，先檢查角色。
func managementRequested(c *gin.Context) bool {
	for _, value := range c.Request.URL.Query()["filters[scope]"] {
		if value == "management" {
			return true
		}
	}
	return false
}

// articles 預設只列公開文章；管理範圍須明確指定且通過管理員門禁。
func (s *server) articles(c *gin.Context) {
	// 明確管理範圍先過角色門禁；預設列表即使管理員也只看公開內容。
	management := managementRequested(c)
	viewer, _, ok := s.identity(c, management)
	if !ok {
		return
	}
	if management && viewer.Role != "admin" {
		respondError(c, &apiError{code: "E0003"})
		return
	}
	page, perPage, scope, e := pageInput(c.Request, "scope")
	if e != nil {
		respondError(c, e)
		return
	}
	where := "WHERE articles.visibility='visible'"
	if scope == "management" {
		where = ""
	}
	base := " FROM articles JOIN accounts ON accounts.id=articles.author_id " + where
	// 計數與列表套相同 SQL 條件，資料庫完成排序分頁且每筆不再追加查詢。
	var total int64
	err := s.db.QueryRowContext(c.Request.Context(), "SELECT count(*)"+base).Scan(&total)
	if err != nil {
		failInternal(c, err)
		return
	}
	list := []gin.H{}
	offset, withinRange := safeOffset(page, perPage)
	if withinRange && offset < total {
		rows, err := s.db.QueryContext(c.Request.Context(), "SELECT "+articleColumns+base+" ORDER BY articles.created_at DESC,articles.id DESC LIMIT $1 OFFSET $2", perPage, offset)
		if err != nil {
			failInternal(c, err)
			return
		}
		defer rows.Close()
		for rows.Next() {
			row, err := readArticleRow(rows)
			if err != nil {
				failInternal(c, err)
				return
			}
			list = append(list, contentData(row, viewer, true))
		}
		if err = rows.Err(); err != nil {
			failInternal(c, err)
			return
		}
	}
	respondOK(c, 200, gin.H{"list": list, "pagination": pagination(page, perPage, total)})
}

// article 對訪客隱藏已下架文章，作者及管理員仍可閱讀。
func (s *server) article(c *gin.Context) {
	// 攜帶憑證先驗證，失效身分不可默默降為訪客。
	viewer, _, ok := s.identity(c, false)
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
	id, e := urlID(c.Param("article_id"), "article_id")
	if e != nil {
		respondError(c, e)
		return
	}
	row, err := readArticleRow(s.db.QueryRowContext(c.Request.Context(), "SELECT "+articleColumns+" FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.id=$1", id))
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	if row.Visibility == "hidden" && (viewer == nil || viewer.Role != "admin" && viewer.ID != row.AuthorID) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	respondOK(c, 200, contentData(row, viewer, true))
}

// comments 先驗父文章可讀性，再依管理員身分篩選個別隱藏留言。
func (s *server) comments(c *gin.Context) {
	// 父文章的可讀性先於個別留言篩選，避免下架文章被訪客探知。
	viewer, _, ok := s.identity(c, false)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request, "")
	if e != nil {
		respondError(c, e)
		return
	}
	id, e := urlID(c.Param("article_id"), "article_id")
	if e != nil {
		respondError(c, e)
		return
	}
	var authorID int64
	var visibility string
	err := s.db.QueryRowContext(c.Request.Context(), "SELECT author_id,visibility FROM articles WHERE id=$1", id).Scan(&authorID, &visibility)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	if visibility == "hidden" && (viewer == nil || viewer.Role != "admin" && viewer.ID != authorID) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	where := " WHERE comments.article_id=$1"
	if viewer == nil || viewer.Role != "admin" {
		where += " AND comments.visibility='visible'"
	}
	base := " FROM comments JOIN accounts ON accounts.id=comments.author_id" + where
	// 計數與列表都依同一筆父文章及可見性條件，由 SQL 排序分頁。
	var total int64
	err = s.db.QueryRowContext(c.Request.Context(), "SELECT count(*)"+base, id).Scan(&total)
	if err != nil {
		failInternal(c, err)
		return
	}
	list := []gin.H{}
	offset, withinRange := safeOffset(page, perPage)
	if withinRange && offset < total {
		rows, err := s.db.QueryContext(c.Request.Context(), "SELECT "+commentColumns+base+" ORDER BY comments.created_at DESC,comments.id DESC LIMIT $2 OFFSET $3", id, perPage, offset)
		if err != nil {
			failInternal(c, err)
			return
		}
		defer rows.Close()
		for rows.Next() {
			row, err := readCommentRow(rows)
			if err != nil {
				failInternal(c, err)
				return
			}
			list = append(list, contentData(row, viewer, false))
		}
		if err = rows.Err(); err != nil {
			failInternal(c, err)
			return
		}
	}
	respondOK(c, 200, gin.H{"list": list, "pagination": pagination(page, perPage, total)})
}

// safeOffset 避免極端合法頁碼造成 int64 溢位。
func safeOffset(page, perPage int64) (int64, bool) {
	if page-1 > 9007199254740991/perPage {
		return 0, false
	}
	return (page - 1) * perPage, true
}
