package main

import (
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// memberRow 只讀管理會員回應所需欄位，避免洩漏密碼與鎖定資料。
func memberRow(scanner interface{ Scan(...any) error }) (account, error) {
	var a account
	err := scanner.Scan(&a.ID, &a.Email, &a.Nickname, &a.Role, &a.Status, &a.Created)
	return a, err
}

// asciiFold 只折疊 ASCII 大寫字母，其他 Unicode 字元保持精確比對。
func asciiFold(value string) string {
	// 逐字元只轉換 ASCII 大寫，讓搜尋詞與資料庫 translate 採相同規則。
	return strings.Map(func(r rune) rune {
		// PostgreSQL translate 使用同一對 ASCII 字母表。
		if r >= 'A' && r <= 'Z' {
			return r + ('a' - 'A')
		}
		return r
	}, value)
}

// adminViewer 先驗證目前登入與角色，管理端點不先解析輸入或查目標。
func (s *server) adminViewer(c *requestContext) (*account, *claims, bool) {
	viewer, token, ok := s.identity(c, true)
	if !ok {
		return nil, nil, false
	}
	if viewer.Role != "admin" {
		respondError(c, &apiError{code: "E0003"})
		return nil, nil, false
	}
	return viewer, token, true
}

// members 在資料庫完成字面搜尋、精確計數與穩定排序分頁。
func (s *server) members(c *requestContext) {
	if _, _, ok := s.adminViewer(c); !ok {
		return
	}
	page, perPage, keyword, e := pageInput(c.Request(), "keyword")
	if e != nil {
		respondError(c, e)
		return
	}
	base := " FROM accounts WHERE role='member'"
	// 固定欄位與字面包含條件同時用於 count／列表，只有 ASCII 大寫參與折疊。
	args := []any{}
	if keyword != "" {
		base += " AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0)"
		args = append(args, asciiFold(keyword))
	}
	var total int64
	if err := s.db.QueryRowContext(c.Request().Context(), "SELECT count(*)"+base, args...).Scan(&total); err != nil {
		failInternal(c, fmt.Errorf("count members: %w", err))
		return
	}
	list := []map[string]any{}
	offset, valid := safeOffset(page, perPage)
	if valid && offset < total {
		// 參數位置依是否搜尋調整；每頁只作一次 JOIN-free 查詢，沒有逐筆讀取。
		limitArg := len(args) + 1
		query := fmt.Sprintf("SELECT id,email,nickname,role,status,created_at%s ORDER BY created_at DESC,id DESC LIMIT $%d OFFSET $%d", base, limitArg, limitArg+1)
		rows, err := s.db.QueryContext(c.Request().Context(), query, append(args, perPage, offset)...)
		if err != nil {
			failInternal(c, fmt.Errorf("list members: %w", err))
			return
		}
		defer rows.Close()
		for rows.Next() {
			a, err := memberRow(rows)
			if err != nil {
				failInternal(c, fmt.Errorf("scan member: %w", err))
				return
			}
			list = append(list, accountData(a))
		}
		if err := rows.Err(); err != nil {
			failInternal(c, fmt.Errorf("iterate members: %w", err))
			return
		}
	}
	respondOK(c, 200, map[string]any{"list": list, "pagination": pagination(page, perPage, total)})
}

// memberDetail 僅向管理員回傳一般會員；管理員 ID 與不存在 ID 同為 404。
func (s *server) memberDetail(c *requestContext) {
	if _, _, ok := s.adminViewer(c); !ok {
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
	id, e := urlID(c.Param("member_id"), "member_id")
	if e != nil {
		respondError(c, e)
		return
	}
	a, err := memberRow(s.db.QueryRowContext(c.Request().Context(), "SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 AND role='member'", id))
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("read member: %w", err))
		return
	}
	respondOK(c, 200, accountData(a))
}

// recheckAdmin 在帳號鎖之後查核 session，防止驗證與寫入之間被撤銷。
func recheckAdmin(c *requestContext, tx *sql.Tx, current account, token *claims) (*apiError, error) {
	// 呼叫者已鎖目前帳號；接著鎖 session 並在等鎖後取實際時間，維持共同鎖序。
	ctx := c.Request().Context()
	var accountID int64
	var backendID string
	var issued, expires time.Time
	var revoked sql.NullTime
	err := tx.QueryRowContext(ctx, "SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE", token.JTI).Scan(&accountID, &backendID, &issued, &expires, &revoked)
	if errors.Is(err, sql.ErrNoRows) {
		return &apiError{code: "E0002"}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("lock admin session: %w", err)
	}
	var now time.Time
	if err := tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
		return nil, fmt.Errorf("read admin clock: %w", err)
	}
	if accountID != current.ID || backendID != backend || !issued.Equal(time.Unix(token.Iat, 0)) || !expires.Equal(time.Unix(token.Exp, 0)) || now.Unix() < token.Nbf || now.Unix() >= token.Exp {
		return &apiError{code: "E0002"}, nil
	}
	// 登入紀錄身分可驗證後，停用優先於撤銷，最後才判管理角色。
	if current.Status == "disabled" {
		return &apiError{code: "E0102"}, nil
	}
	if revoked.Valid {
		return &apiError{code: "E0002"}, nil
	}
	if current.Role != "admin" {
		return &apiError{code: "E0003"}, nil
	}
	return nil, nil
}

// memberStatus 將會員狀態與停用時的所有 session 撤銷放進同一交易。
func (s *server) memberStatus(c *requestContext) {
	viewer, token, ok := s.adminViewer(c)
	if !ok {
		return
	}
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	id, e := urlID(c.Param("member_id"), "member_id")
	if e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request(), []string{"status"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request().Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, fmt.Errorf("begin member status: %w", err))
		return
	}
	defer tx.Rollback()
	// 多筆帳號按 ID 鎖住，然後才查 session；與 Laravel 停用及設定關閉同序。
	rows, err := tx.QueryContext(ctx, "SELECT id,role,status FROM accounts WHERE id IN ($1,$2) ORDER BY id FOR UPDATE", viewer.ID, id)
	if err != nil {
		failInternal(c, fmt.Errorf("lock member accounts: %w", err))
		return
	}
	var current, target account
	// 完整收完鎖定結果再查 session，避免同一連線仍有未結束的 rows。
	for rows.Next() {
		var a account
		if err := rows.Scan(&a.ID, &a.Role, &a.Status); err != nil {
			rows.Close()
			failInternal(c, fmt.Errorf("scan locked account: %w", err))
			return
		}
		if a.ID == viewer.ID {
			current = a
		}
		if a.ID == id {
			target = a
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		failInternal(c, fmt.Errorf("iterate locked accounts: %w", err))
		return
	}
	if current.ID == 0 {
		respondError(c, &apiError{code: "E0002"})
		return
	}
	// 寫入前重驗管理員，外層認證不能取代取得鎖後的目前狀態。
	apiErr, err := recheckAdmin(c, tx, current, token)
	if err != nil {
		failInternal(c, err)
		return
	}
	if apiErr != nil {
		respondError(c, apiErr)
		return
	}
	if target.ID == 0 || target.Role != "member" {
		respondError(c, &apiError{code: "E0004"})
		return
	}
	// 狀態同值時不寫入，也不再次撤銷既有登入。
	if target.Status == body["status"] {
		respondOK(c, 200, nil)
		return
	}
	if _, err := tx.ExecContext(ctx, "UPDATE accounts SET status=$1 WHERE id=$2", body["status"], id); err != nil {
		failInternal(c, fmt.Errorf("set member status: %w", err))
		return
	}
	if body["status"] == "disabled" {
		if _, err := tx.ExecContext(ctx, "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1 AND revoked_at IS NULL", id); err != nil {
			failInternal(c, fmt.Errorf("revoke member sessions: %w", err))
			return
		}
	}
	if err := tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit member status: %w", err))
		return
	}
	respondOK(c, 200, nil)
}

// managementComments 向管理員列出全部留言，包含個別或父文章下架內容。
func (s *server) managementComments(c *requestContext) {
	// 角色先於格式，計數與完整留言列表都由 SQL 完成，不因父文章下架縮小範圍。
	viewer, _, ok := s.adminViewer(c)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request(), "")
	if e != nil {
		respondError(c, e)
		return
	}
	base := " FROM comments JOIN accounts ON accounts.id=comments.author_id"
	var total int64
	if err := s.db.QueryRowContext(c.Request().Context(), "SELECT count(*)"+base).Scan(&total); err != nil {
		failInternal(c, fmt.Errorf("count management comments: %w", err))
		return
	}
	list := []map[string]any{}
	offset, valid := safeOffset(page, perPage)
	if valid && offset < total {
		rows, err := s.db.QueryContext(c.Request().Context(), "SELECT "+commentColumns+base+" ORDER BY comments.created_at DESC,comments.id DESC LIMIT $1 OFFSET $2", perPage, offset)
		if err != nil {
			failInternal(c, fmt.Errorf("list management comments: %w", err))
			return
		}
		defer rows.Close()
		for rows.Next() {
			row, err := readCommentRow(rows)
			if err != nil {
				failInternal(c, fmt.Errorf("scan management comment: %w", err))
				return
			}
			list = append(list, contentData(row, viewer, false))
		}
		if err := rows.Err(); err != nil {
			failInternal(c, fmt.Errorf("iterate management comments: %w", err))
			return
		}
	}
	respondOK(c, 200, map[string]any{"list": list, "pagination": pagination(page, perPage, total)})
}

// lockoutData 固定公開設定的四個欄位與 JSON 型態。
func lockoutData(enabled bool, window, threshold, seconds int) map[string]any {
	return map[string]any{"enabled": enabled, "window_minutes": window, "failure_threshold": threshold, "lock_seconds": seconds}
}

// lockoutSettings 只向管理員回傳共用設定，不洩漏帳號鎖定資料。
func (s *server) lockoutSettings(c *requestContext) {
	if _, _, ok := s.adminViewer(c); !ok {
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
	var enabled bool
	var window, threshold, seconds int
	err := s.db.QueryRowContext(c.Request().Context(), "SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1").Scan(&enabled, &window, &threshold, &seconds)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0104"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("read lockout settings: %w", err))
		return
	}
	respondOK(c, 200, lockoutData(enabled, window, threshold, seconds))
}

// updateLockoutSettings 原子覆寫四欄，僅啟用轉關閉時清除會員鎖定與計數世代。
func (s *server) updateLockoutSettings(c *requestContext) {
	// 先驗管理角色與全部四欄；即使關閉功能也必須保留合法的三個數值。
	viewer, token, ok := s.adminViewer(c)
	if !ok {
		return
	}
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request(), []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	enabled := body["enabled"] == "true"
	window, _ := strconv.Atoi(body["window_minutes"])
	threshold, _ := strconv.Atoi(body["failure_threshold"])
	seconds, _ := strconv.Atoi(body["lock_seconds"])
	ctx := c.Request().Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, fmt.Errorf("begin lockout update: %w", err))
		return
	}
	defer tx.Rollback()
	// 設定列先於帳號列；關閉時先依 ID 鎖所有帳號，再重驗管理員 session。
	var previous bool
	var oldWindow, oldThreshold, oldSeconds int
	err = tx.QueryRowContext(ctx, "SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1 FOR UPDATE").Scan(&previous, &oldWindow, &oldThreshold, &oldSeconds)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0104"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("lock lockout settings: %w", err))
		return
	}
	disabling := previous && !enabled
	if disabling {
		rows, err := tx.QueryContext(ctx, "SELECT id FROM accounts ORDER BY id FOR UPDATE")
		if err != nil {
			failInternal(c, fmt.Errorf("lock accounts for lockout: %w", err))
			return
		}
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				failInternal(c, fmt.Errorf("scan lockout account: %w", err))
				return
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			failInternal(c, fmt.Errorf("iterate lockout accounts: %w", err))
			return
		}
	}
	var current account
	err = tx.QueryRowContext(ctx, "SELECT id,role,status FROM accounts WHERE id=$1 FOR UPDATE", viewer.ID).Scan(&current.ID, &current.Role, &current.Status)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0002"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("lock admin account: %w", err))
		return
	}
	apiErr, err := recheckAdmin(c, tx, current, token)
	if err != nil {
		failInternal(c, err)
		return
	}
	if apiErr != nil {
		respondError(c, apiErr)
		return
	}
	// 四欄皆同值時直接成功，避免無意義地更新同一筆設定。
	if previous == enabled && oldWindow == window && oldThreshold == threshold && oldSeconds == seconds {
		respondOK(c, 200, nil)
		return
	}
	if _, err := tx.ExecContext(ctx, "UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1", enabled, window, threshold, seconds); err != nil {
		failInternal(c, fmt.Errorf("update lockout settings: %w", err))
		return
	}
	if disabling {
		// 世代遞增令舊失敗事件不再計數；保留事件及文章、留言資料。
		if _, err := tx.ExecContext(ctx, "UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE role='member'"); err != nil {
			failInternal(c, fmt.Errorf("clear member lockout: %w", err))
			return
		}
	}
	if err := tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit lockout settings: %w", err))
		return
	}
	respondOK(c, 200, nil)
}
