package main

import (
	"database/sql"
	"errors"
)

// myArticles 只列目前登入者自己的文章，包含下架內容供作者恢復。
func (s *server) myArticles(c *requestContext) {
	viewer, _, ok := s.identity(c, true)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request(), "")
	if e != nil {
		respondError(c, e)
		return
	}
	base := " FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.author_id=$1"
	var total int64
	if err := s.db.QueryRowContext(c.Request().Context(), "SELECT count(*)"+base, viewer.ID).Scan(&total); err != nil {
		failInternal(c, err)
		return
	}
	// 精確計數與排序都在 SQL；合法巨大頁碼直接回空頁，避免 OFFSET 溢位。
	list := []map[string]any{}
	offset, valid := safeOffset(page, perPage)
	if valid && offset < total {
		rows, err := s.db.QueryContext(c.Request().Context(), "SELECT "+articleColumns+base+" ORDER BY articles.created_at DESC,articles.id DESC LIMIT $2 OFFSET $3", viewer.ID, perPage, offset)
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
	respondOK(c, 200, map[string]any{"list": list, "pagination": pagination(page, perPage, total)})
}

// myComments 只列登入者自己的留言；不讀取父文章內容或授予其讀取權。
func (s *server) myComments(c *requestContext) {
	viewer, _, ok := s.identity(c, true)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request(), "")
	if e != nil {
		respondError(c, e)
		return
	}
	base := " FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.author_id=$1"
	var total int64
	if err := s.db.QueryRowContext(c.Request().Context(), "SELECT count(*)"+base, viewer.ID).Scan(&total); err != nil {
		failInternal(c, err)
		return
	}
	// 父文章下架不改個人列表範圍，且每頁只執行一次列表查詢。
	list := []map[string]any{}
	offset, valid := safeOffset(page, perPage)
	if valid && offset < total {
		rows, err := s.db.QueryContext(c.Request().Context(), "SELECT "+commentColumns+base+" ORDER BY comments.created_at DESC,comments.id DESC LIMIT $2 OFFSET $3", viewer.ID, perPage, offset)
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
	respondOK(c, 200, map[string]any{"list": list, "pagination": pagination(page, perPage, total)})
}

// commentDetail 讓作者獨立讀自己的留言，即使父文章已下架也不洩漏其內文。
func (s *server) commentDetail(c *requestContext) {
	viewer, _, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noGetBody(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	id, e := urlID(c.Param("comment_id"), "comment_id")
	if e != nil {
		respondError(c, e)
		return
	}
	var row content
	var parentVisibility string
	err := s.db.QueryRowContext(c.Request().Context(), "SELECT "+commentColumns+",articles.visibility FROM comments JOIN accounts ON accounts.id=comments.author_id JOIN articles ON articles.id=comments.article_id WHERE comments.id=$1", id).Scan(&row.ID, &row.AuthorID, &row.ArticleID, &row.Body, &row.Visibility, &row.Restore, &row.Version, &row.Backend, &row.Created, &row.Updated, &row.Nickname, &parentVisibility)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	// 非作者的可見留言先分辨隱藏，再回詳情權限不足。
	if viewer.Role != "admin" && viewer.ID != row.AuthorID {
		if row.Visibility == "hidden" || parentVisibility == "hidden" {
			respondError(c, &apiError{code: "E0004"})
		} else {
			respondError(c, &apiError{code: "E0003"})
		}
		return
	}
	respondOK(c, 200, contentData(row, viewer, false))
}
