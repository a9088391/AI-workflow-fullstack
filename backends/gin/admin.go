package main

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
)

// lockoutSettings 保存單筆共用設定，四欄必須在同一交易完整覆寫。
type lockoutSettings struct {
	Enabled          bool
	WindowMinutes    int
	FailureThreshold int
	LockSeconds      int
}

// settingsData 只輸出契約中的四欄，不暴露資料庫固定 ID。
func settingsData(v lockoutSettings) gin.H {
	return gin.H{"enabled": v.Enabled, "window_minutes": v.WindowMinutes, "failure_threshold": v.FailureThreshold, "lock_seconds": v.LockSeconds}
}

// settingsInput 保留 JSON 原始型態與最後同名鍵，區分語法、缺漏、型態及範圍錯誤。
func settingsInput(r *http.Request) (lockoutSettings, *apiError) {
	// 先限制本文大小與編碼，再區分 JSON 語法錯誤和合法非 object 的欄位錯誤。
	var result lockoutSettings
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20+1))
	if err != nil || len(raw) > 1<<20 || !utf8.Valid(raw) || !validSurrogates(raw) {
		return result, &apiError{code: "E0005"}
	}
	if len(raw) == 0 || strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])) != "application/json" {
		return result, fieldError("_body", "Request Body 必須為 JSON object")
	}
	if !json.Valid(raw) {
		return result, &apiError{code: "E0005"}
	}
	if len(bytes.TrimSpace(raw)) == 0 || bytes.TrimSpace(raw)[0] != '{' {
		return result, fieldError("_body", "Request Body 必須為 JSON object")
	}
	// 保留原始 JSON 型態，避免把字串、指數或小數悄悄轉為設定整數。
	var values map[string]json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil {
		return result, &apiError{code: "E0005"}
	}
	// 先判每欄的第一個契約錯誤，再彙整未知欄位；合法整數才轉為 Go int。
	limits := map[string]int{"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400}
	errorsByField := map[string][]string{}
	for _, name := range []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"} {
		value, ok := values[name]
		switch {
		case !ok:
			errorsByField[name] = []string{required}
		case bytes.Equal(value, []byte("null")):
			errorsByField[name] = []string{nullValue}
		case name == "enabled":
			if bytes.Equal(value, []byte("true")) {
				result.Enabled = true
			} else if !bytes.Equal(value, []byte("false")) {
				errorsByField[name] = []string{wrongType}
			}
		case !jsonIntegerPattern.Match(value):
			errorsByField[name] = []string{wrongType}
		default:
			n, err := strconv.ParseInt(string(value), 10, 64)
			if err != nil || n < 1 || n > int64(limits[name]) {
				errorsByField[name] = []string{invalid}
			} else {
				switch name {
				case "window_minutes":
					result.WindowMinutes = int(n)
				case "failure_threshold":
					result.FailureThreshold = int(n)
				case "lock_seconds":
					result.LockSeconds = int(n)
				}
			}
		}
	}
	for name := range values {
		if name != "enabled" && limits[name] == 0 {
			errorsByField[name] = []string{"不支援此欄位"}
		}
	}
	if len(errorsByField) > 0 {
		return result, &apiError{code: "E0001", fields: errorsByField}
	}
	return result, nil
}

// adminIdentity 先驗目前身分與角色，避免會員透過參數錯誤探測管理 API。
func (s *server) adminIdentity(c *gin.Context) (*account, *claims, bool) {
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

// memberData 限定管理員可見的會員欄位，避免輸出密碼及鎖定內部狀態。
func memberData(scanner interface{ Scan(...any) error }) (gin.H, error) {
	var id int64
	var email, nickname, role, status string
	var created time.Time
	err := scanner.Scan(&id, &email, &nickname, &role, &status, &created)
	return gin.H{"id": id, "email": email, "nickname": nickname, "role": role, "status": status, "created_at": businessTime(created)}, err
}

// members 在資料庫對一般會員做字面搜尋、精確計數與排序分頁。
func (s *server) members(c *gin.Context) {
	if _, _, ok := s.adminIdentity(c); !ok {
		return
	}
	page, perPage, keyword, e := pageInput(c.Request, "keyword")
	if e != nil {
		respondError(c, e)
		return
	}
	base := " FROM accounts WHERE role='member'"
	args := []any{}
	if keyword != "" {
		base += " AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0)"
		// 僅折疊 ASCII 大寫，保留其他 Unicode 碼點的精確比對。
		args = append(args, strings.Map(func(r rune) rune {
			if r >= 'A' && r <= 'Z' {
				return r + ('a' - 'A')
			}
			return r
		}, keyword))
	}
	// 計數與資料列共用相同範圍，排序及分頁留在資料庫而不全量載入。
	var total int64
	if err := s.db.QueryRowContext(c.Request.Context(), "SELECT count(*)"+base, args...).Scan(&total); err != nil {
		failInternal(c, fmt.Errorf("count members: %w", err))
		return
	}
	list := []gin.H{}
	offset, withinRange := safeOffset(page, perPage)
	if withinRange && offset < total {
		queryArgs := append(append([]any{}, args...), perPage, offset)
		limit := "$1 OFFSET $2"
		if keyword != "" {
			limit = "$2 OFFSET $3"
		}
		rows, err := s.db.QueryContext(c.Request.Context(), "SELECT id,email,nickname,role,status,created_at"+base+" ORDER BY created_at DESC,id DESC LIMIT "+limit, queryArgs...)
		if err != nil {
			failInternal(c, fmt.Errorf("list members: %w", err))
			return
		}
		// 掃描或迭代失敗都不得輸出部分成功列表，離開時仍關閉查詢資源。
		defer rows.Close()
		for rows.Next() {
			item, err := memberData(rows)
			if err != nil {
				failInternal(c, fmt.Errorf("scan member: %w", err))
				return
			}
			list = append(list, item)
		}
		if err := rows.Err(); err != nil {
			failInternal(c, fmt.Errorf("read members: %w", err))
			return
		}
	}
	respondOK(c, 200, gin.H{"list": list, "pagination": pagination(page, perPage, total)})
}

// member 只讀一般會員；管理員 ID 與不存在的 ID 共用 404 回應。
func (s *server) member(c *gin.Context) {
	if _, _, ok := s.adminIdentity(c); !ok {
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
	id, e := urlID(c.Param("member_id"), "member_id")
	if e != nil {
		respondError(c, e)
		return
	}
	data, err := memberData(s.db.QueryRowContext(c.Request.Context(), "SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 AND role='member'", id))
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0004"})
	} else if err != nil {
		failInternal(c, fmt.Errorf("read member: %w", err))
	} else {
		respondOK(c, 200, data)
	}
}

// lockedAdmin 在已啟動交易中依 ID 鎖帳號，再鎖 session 重驗管理員身分。
func lockedAdmin(c *gin.Context, tx *sql.Tx, viewer *account, token *claims, targetID int64, all bool) (*account, *apiError, error) {
	ctx := c.Request.Context()
	// 多帳號一律按 ID 加鎖；關閉鎖定時先鎖全部帳號，後續清除不會反向取得鎖。
	query := "SELECT id,role,status FROM accounts WHERE id IN ($1,$2) ORDER BY id FOR UPDATE"
	args := []any{viewer.ID, targetID}
	if all {
		query = "SELECT id,role,status FROM accounts ORDER BY id FOR UPDATE"
		args = nil
	} else if targetID == 0 {
		query = "SELECT id,role,status FROM accounts WHERE id=$1 FOR UPDATE"
		args = []any{viewer.ID}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, nil, fmt.Errorf("lock admin accounts: %w", err)
	}
	var current, target account
	for rows.Next() {
		var a account
		if err := rows.Scan(&a.ID, &a.Role, &a.Status); err != nil {
			rows.Close()
			return nil, nil, fmt.Errorf("scan locked account: %w", err)
		}
		if a.ID == viewer.ID {
			current = a
		}
		if a.ID == targetID {
			target = a
		}
	}
	// 先完成並關閉帳號查詢，才能在同一連線查核 session；列鎖維持到交易結束。
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, nil, fmt.Errorf("read locked accounts: %w", err)
	}
	// 帳號缺失無有效登入；其餘狀態要在 session 身分及到期核對後判定。
	if current.ID == 0 {
		return nil, &apiError{code: "E0002"}, nil
	}
	var accountID int64
	var backendID string
	var issued, expires time.Time
	var revoked sql.NullTime
	err = tx.QueryRowContext(ctx, "SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE", token.JTI).Scan(&accountID, &backendID, &issued, &expires, &revoked)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &apiError{code: "E0002"}, nil
	}
	if err != nil {
		return nil, nil, fmt.Errorf("lock admin session: %w", err)
	}
	// 精確登入身分與到期先於停用；可驗證且未到期時，停用才優先於撤銷。
	if accountID != current.ID || backendID != backend || !issued.Equal(time.Unix(token.Iat, 0)) || !expires.Equal(time.Unix(token.Exp, 0)) || time.Now().Unix() >= token.Exp {
		return nil, &apiError{code: "E0002"}, nil
	}
	if current.Status == "disabled" {
		return nil, &apiError{code: "E0102"}, nil
	}
	if current.Role != "admin" {
		return nil, &apiError{code: "E0003"}, nil
	}
	if revoked.Valid {
		return nil, &apiError{code: "E0002"}, nil
	}
	return &target, nil, nil
}

// memberStatus 在同一交易確認目標、更新狀態並撤銷停用會員的所有舊 session。
func (s *server) memberStatus(c *gin.Context) {
	viewer, token, ok := s.adminIdentity(c)
	if !ok {
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	id, e := urlID(c.Param("member_id"), "member_id")
	if e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request, []string{"status"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request.Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, fmt.Errorf("begin member status: %w", err))
		return
	}
	// 任一錯誤出口都回滾整個狀態與副作用；只有 Commit 成功才送出成功。
	defer tx.Rollback()
	// 參數通過後鎖帳號與 session 重驗，再判定目標是否屬於可管理的一般會員。
	target, apiErr, err := lockedAdmin(c, tx, viewer, token, id, false)
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
	// 同值不撤銷；恢復也不清暫時鎖定或復活舊登入。
	if target.Status != body["status"] {
		if _, err = tx.ExecContext(ctx, "UPDATE accounts SET status=$1 WHERE id=$2", body["status"], id); err != nil {
			failInternal(c, fmt.Errorf("update member status: %w", err))
			return
		}
		if body["status"] == "disabled" {
			if _, err = tx.ExecContext(ctx, "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1 AND revoked_at IS NULL", id); err != nil {
				failInternal(c, fmt.Errorf("revoke member sessions: %w", err))
				return
			}
		}
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit member status: %w", err))
		return
	}
	respondOK(c, 200, nil)
}

// adminComments 限管理員列出所有留言，避免下架內容在管理流程中遺漏。
func (s *server) adminComments(c *gin.Context) {
	viewer, _, ok := s.adminIdentity(c)
	if !ok {
		return
	}
	page, perPage, _, e := pageInput(c.Request, "")
	if e != nil {
		respondError(c, e)
		return
	}
	base := " FROM comments JOIN accounts ON accounts.id=comments.author_id"
	// 計數與資料列共用相同範圍，排序及分頁留在資料庫而不全量載入。
	var total int64
	if err := s.db.QueryRowContext(c.Request.Context(), "SELECT count(*)"+base).Scan(&total); err != nil {
		failInternal(c, fmt.Errorf("count admin comments: %w", err))
		return
	}
	list := []gin.H{}
	offset, withinRange := safeOffset(page, perPage)
	if withinRange && offset < total {
		rows, err := s.db.QueryContext(c.Request.Context(), "SELECT "+commentColumns+base+" ORDER BY comments.created_at DESC,comments.id DESC LIMIT $1 OFFSET $2", perPage, offset)
		if err != nil {
			failInternal(c, fmt.Errorf("list admin comments: %w", err))
			return
		}
		// 掃描或迭代失敗都不得輸出部分成功列表，離開時仍關閉查詢資源。
		defer rows.Close()
		for rows.Next() {
			row, err := readCommentRow(rows)
			if err != nil {
				failInternal(c, fmt.Errorf("scan admin comment: %w", err))
				return
			}
			list = append(list, contentData(row, viewer, false))
		}
		if err := rows.Err(); err != nil {
			failInternal(c, fmt.Errorf("read admin comments: %w", err))
			return
		}
	}
	respondOK(c, 200, gin.H{"list": list, "pagination": pagination(page, perPage, total)})
}

// loginLockoutSettings 讓管理員讀取目前提交的完整設定，避免回傳資料庫內部欄位。
func (s *server) loginLockoutSettings(c *gin.Context) {
	if _, _, ok := s.adminIdentity(c); !ok {
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
	var v lockoutSettings
	err := s.db.QueryRowContext(c.Request.Context(), "SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1").Scan(&v.Enabled, &v.WindowMinutes, &v.FailureThreshold, &v.LockSeconds)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0104"})
	} else if err != nil {
		failInternal(c, fmt.Errorf("read lockout settings: %w", err))
	} else {
		respondOK(c, 200, settingsData(v))
	}
}

// updateLoginLockoutSettings 以設定列鎖序列化完整覆寫；真正關閉才原子清除會員鎖定。
func (s *server) updateLoginLockoutSettings(c *gin.Context) {
	viewer, token, ok := s.adminIdentity(c)
	if !ok {
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	next, e := settingsInput(c.Request)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request.Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, fmt.Errorf("begin lockout settings: %w", err))
		return
	}
	// 任一錯誤出口都回滾整個狀態與副作用；只有 Commit 成功才送出成功。
	defer tx.Rollback()
	// 設定列先於帳號及 session，加鎖順序與登入一致以序列化完整設定。
	var old lockoutSettings
	err = tx.QueryRowContext(ctx, "SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1 FOR UPDATE").Scan(&old.Enabled, &old.WindowMinutes, &old.FailureThreshold, &old.LockSeconds)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0104"})
		return
	}
	if err != nil {
		failInternal(c, fmt.Errorf("lock lockout settings: %w", err))
		return
	}
	// 真正關閉才需鎖全部帳號；數值或同值更新只重驗目前管理員。
	disabling := old.Enabled && !next.Enabled
	if _, apiErr, err := lockedAdmin(c, tx, viewer, token, 0, disabling); err != nil {
		failInternal(c, err)
		return
	} else if apiErr != nil {
		respondError(c, apiErr)
		return
	}
	// 相同完整值沒有副作用；數值變動也不調整現存的 locked_until。
	if old != next {
		_, err = tx.ExecContext(ctx, "UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1", next.Enabled, next.WindowMinutes, next.FailureThreshold, next.LockSeconds)
		if err != nil {
			failInternal(c, fmt.Errorf("update lockout settings: %w", err))
			return
		}
		if disabling {
			_, err = tx.ExecContext(ctx, "UPDATE accounts SET locked_until=NULL,failure_epoch=failure_epoch+1 WHERE role='member'")
			if err != nil {
				failInternal(c, fmt.Errorf("clear member lockouts: %w", err))
				return
			}
		}
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, fmt.Errorf("commit lockout settings: %w", err))
		return
	}
	respondOK(c, 200, nil)
}
