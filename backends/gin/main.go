package main

import (
	"context"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
)

var messages = map[string]struct {
	status  int
	message string
}{
	"E0001": {422, "欄位資料不正確，請確認後再送出"},
	"E0002": {401, "登入已逾期，請重新登入"},
	"E0003": {403, "沒有權限使用此功能"},
	"E0004": {404, "找不到指定資料或 API"},
	"E0005": {400, "系統暫時無法處理，請稍後再試"},
	"E0101": {401, "帳號或密碼錯誤"},
	"E0102": {403, "此帳號已停用，請聯絡系統管理員"},
	"E0104": {500, "後台認證設定異常，請聯絡系統管理員"},
	"E0105": {429, "登入失敗次數過多，請稍後再試"},
	"E0201": {409, "內容已更新，請確認最新內容後再送出"},
	"E9999": {500, "系統暫時無法處理，請稍後再試"},
}

// server 持有唯一資料庫池與 Gin 專屬金鑰，避免請求間共用可變身分。
type server struct {
	db  *sql.DB
	key []byte
}

// respondError 使用共同錯誤 envelope，並以 log_id 串起此請求的紀錄。
func respondError(c *gin.Context, e *apiError) {
	detail := messages[e.code]
	if e.status != 0 {
		detail.status = e.status
	}
	body := gin.H{"success": false, "message": detail.message, "error_code": e.code, "log_id": c.GetString("log_id")}
	if e.fields != nil {
		body["errors"] = e.fields
	}
	if e.retry > 0 {
		c.Header("Retry-After", strconv.Itoa(e.retry))
	}
	c.JSON(detail.status, body)
}

// respondOK 只輸出已核准成功欄位，不帶錯誤或日誌識別。
func respondOK(c *gin.Context, status int, data any) {
	if data == nil {
		data = gin.H{}
	}
	c.JSON(status, gin.H{"success": true, "message": "OK", "data": data})
}

// failInternal 記錄內部失敗但不把 SQL 或金鑰細節回給客戶端。
func failInternal(c *gin.Context, err error) {
	log.Printf("ERROR log_id=%s err=%v", c.GetString("log_id"), err)
	respondError(c, &apiError{code: "E9999"})
}

// logMiddleware 指派每次請求的亂數 log_id 並統一 CORS 與進出記錄。
func logMiddleware() gin.HandlerFunc {
	// 每個請求使用獨立識別與回應標頭，讓並行請求不共用可變狀態。
	return func(c *gin.Context) {
		raw, err := randomBytes(16)
		if err != nil {
			c.AbortWithStatus(500)
			return
		}
		id := fmt.Sprintf("%x-%x-%x-%x-%x", raw[:4], raw[4:6], raw[6:8], raw[8:10], raw[10:])
		c.Set("log_id", id)
		if c.GetHeader("Origin") == env("CORS_ORIGIN", "http://localhost:8080") {
			c.Header("Access-Control-Allow-Origin", env("CORS_ORIGIN", "http://localhost:8080"))
			c.Header("Vary", "Origin")
			c.Header("Access-Control-Expose-Headers", "Retry-After")
		}
		// 預檢在業務路由前結束；一般請求完成後再記錄最終狀態。
		log.Printf("IN log_id=%s method=%s path=%s", id, c.Request.Method, c.Request.URL.Path)
		if c.Request.Method == http.MethodOptions {
			c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
			c.Header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
			c.Status(204)
			c.Abort()
			return
		}
		c.Next()
		if c.Writer.Status() >= 400 {
			log.Printf("EX log_id=%s status=%d", id, c.Writer.Status())
		}
		log.Printf("OUT log_id=%s status=%d", id, c.Writer.Status())
	}
}

// register 驗證全部輸入後使用唯一索引裁決跨後端並行註冊。
func (s *server) register(c *gin.Context) {
	// 公開註冊忽略舊 JWT，先完成 Query 與 Body 驗證才接觸資料庫。
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request, []string{"email", "password", "nickname"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	// 先回報已註冊帳號；實際 INSERT 仍須由唯一索引處理並行競爭。
	var exists bool
	err := s.db.QueryRowContext(c.Request.Context(), "SELECT EXISTS(SELECT 1 FROM accounts WHERE email=$1)", body["email"]).Scan(&exists)
	if err != nil {
		failInternal(c, err)
		return
	}
	if exists {
		respondError(c, fieldError("email", "此 Email 已註冊"))
		return
	}
	hash, err := passwordHash(body["password"])
	if err != nil {
		failInternal(c, err)
		return
	}
	// 唯一索引仍裁決其他後端同時註冊；只將該衝突轉成已核准文案。
	_, err = s.db.ExecContext(c.Request.Context(), "INSERT INTO accounts (email,password_hash,nickname,role,status) VALUES ($1,$2,$3,'member','active')", body["email"], hash, body["nickname"])
	if err != nil {
		var databaseError *pgconn.PgError
		if errors.As(err, &databaseError) && databaseError.Code == "23505" {
			respondError(c, fieldError("email", "此 Email 已註冊"))
		} else {
			failInternal(c, err)
		}
		return
	}
	respondOK(c, 201, nil)
}

// lockRetry 在提交後取新的資料庫時間，將剩餘秒數向上取整。
func (s *server) lockRetry(ctx context.Context, until time.Time) (int, error) {
	var now time.Time
	if err := s.db.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
		return 0, err
	}
	seconds := int((until.Sub(now) + time.Second - time.Nanosecond) / time.Second)
	if seconds < 1 {
		seconds = 1
	}
	return seconds, nil
}

// login 序列化設定及帳號列；失敗事件先提交再回 401 或 429。
func (s *server) login(c *gin.Context) {
	// 格式錯誤不計入鎖定，所以先完整驗證帳密。
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request, []string{"email", "password"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request.Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, err)
		return
	}
	// 任一未提交出口都回滾；成功 Commit 後的 Rollback 不會撤回結果。
	defer tx.Rollback()
	// 設定列先於帳號列加鎖，讓管理設定變更與跨後端登入使用同一序列。
	var enabled bool
	var window, threshold, lockSeconds int
	err = tx.QueryRowContext(ctx, "SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1 FOR UPDATE").Scan(&enabled, &window, &threshold, &lockSeconds)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0104"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	var a account
	err = tx.QueryRowContext(ctx, "SELECT id,email,password_hash,nickname,role,status,failure_epoch,locked_until,created_at FROM accounts WHERE email=$1 FOR UPDATE", body["email"]).Scan(&a.ID, &a.Email, &a.Hash, &a.Nickname, &a.Role, &a.Status, &a.Epoch, &a.Locked, &a.Created)
	if errors.Is(err, sql.ErrNoRows) {
		respondError(c, &apiError{code: "E0101"})
		return
	}
	if err != nil {
		failInternal(c, err)
		return
	}
	// 等待設定及帳號鎖結束後才取實際時間，避免把等待時間排除於鎖定判定。
	var now time.Time
	if err = tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
		failInternal(c, err)
		return
	}
	if a.Status == "disabled" {
		respondError(c, &apiError{code: "E0102"})
		return
	}
	// 鎖定到期只變更計數世代；現存未到期鎖不驗密碼也不延長。
	if a.Role == "member" && a.Locked.Valid {
		if enabled && now.Before(a.Locked.Time) {
			if err = tx.Commit(); err != nil {
				failInternal(c, err)
				return
			}
			retry, err := s.lockRetry(ctx, a.Locked.Time)
			if err != nil {
				failInternal(c, err)
				return
			}
			respondError(c, &apiError{code: "E0105", retry: retry})
			return
		}
		a.Epoch++
		_, err = tx.ExecContext(ctx, "UPDATE accounts SET failure_epoch=$1,locked_until=NULL WHERE id=$2", a.Epoch, a.ID)
		if err != nil {
			failInternal(c, err)
			return
		}
	}
	matches, apiErr := verifyPassword(body["password"], a.Hash)
	if apiErr != nil {
		respondError(c, apiErr)
		return
	}
	if !matches {
		// 只有啟用鎖定的一般會員才新增事件；先提交再送出業務錯誤。
		if enabled && a.Role == "member" {
			_, err = tx.ExecContext(ctx, "INSERT INTO login_failures (account_id,failure_epoch,occurred_at) VALUES ($1,$2,$3)", a.ID, a.Epoch, now)
			if err != nil {
				failInternal(c, err)
				return
			}
			// 視窗左開右閉並包含本次事件，只計目前世代而不刪除歷史。
			var count int
			err = tx.QueryRowContext(ctx, "SELECT count(*) FROM login_failures WHERE account_id=$1 AND failure_epoch=$2 AND occurred_at>$3 AND occurred_at<=$4", a.ID, a.Epoch, now.Add(-time.Duration(window)*time.Minute), now).Scan(&count)
			if err != nil {
				failInternal(c, err)
				return
			}
			if count >= threshold {
				until := now.Add(time.Duration(lockSeconds) * time.Second)
				_, err = tx.ExecContext(ctx, "UPDATE accounts SET locked_until=$1 WHERE id=$2", until, a.ID)
				if err != nil {
					failInternal(c, err)
					return
				}
				if err = tx.Commit(); err != nil {
					failInternal(c, err)
					return
				}
				retry, err := s.lockRetry(ctx, until)
				if err != nil {
					failInternal(c, err)
					return
				}
				respondError(c, &apiError{code: "E0105", retry: retry})
				return
			}
		}
		if err = tx.Commit(); err != nil {
			failInternal(c, err)
			return
		}
		respondError(c, &apiError{code: "E0101"})
		return
	}
	issued := now.Unix()
	// JWT 與 session 使用同一資料庫時刻；重設計數與寫入 session 同交易。
	random, err := randomBytes(16)
	if err != nil {
		failInternal(c, err)
		return
	}
	jti := base64.RawURLEncoding.EncodeToString(random)
	token, err := issueToken(s.key, a.ID, jti, issued)
	if err != nil {
		failInternal(c, err)
		return
	}
	_, err = tx.ExecContext(ctx, "UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE id=$1", a.ID)
	if err != nil {
		failInternal(c, err)
		return
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO login_sessions (jti,account_id,backend_id,issued_at,expires_at) VALUES ($1,$2,'gin',to_timestamp($3),to_timestamp($4))", jti, a.ID, issued, issued+duration)
	if err != nil {
		failInternal(c, err)
		return
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, err)
		return
	}
	respondOK(c, 200, gin.H{"access_token": token, "token_type": "Bearer", "expires_at": businessTime(time.Unix(issued+duration, 0)), "user": accountData(a)})
}

// identity 對保護及條件式公開端點套用同一 JWT/session 查核順序。
func (s *server) identity(c *gin.Context, required bool) (*account, *claims, bool) {
	// 公開讀取若送出空 Authorization 也屬於失效憑證，不能降成訪客。
	if _, supplied := c.Request.Header["Authorization"]; supplied {
		required = true
	}
	a, claims, e, err := authenticate(c.Request.Context(), s.db, s.key, c.GetHeader("Authorization"), required)
	if err != nil {
		failInternal(c, err)
		return nil, nil, false
	}
	if e != nil {
		respondError(c, e)
		return nil, nil, false
	}
	return a, claims, true
}

// logout 鎖住帳號與登入紀錄後重驗，只撤銷這個 jti。
func (s *server) logout(c *gin.Context) {
	// 先認證再檢查 Body，符合保護端點的錯誤優先順序。
	a, claims, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noQuery(c.Request); e != nil {
		respondError(c, e)
		return
	}
	_, e := bodyInput(c.Request, []string{}, true)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request.Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, err)
		return
	}
	// 任一未提交出口都回滾；成功 Commit 後的 Rollback 不會撤回結果。
	defer tx.Rollback()
	// 與停用、重設密碼共用帳號→session 鎖順序；鎖後再次檢查現況。
	var status string
	err = tx.QueryRowContext(ctx, "SELECT status FROM accounts WHERE id=$1 FOR UPDATE", a.ID).Scan(&status)
	if err != nil {
		failInternal(c, err)
		return
	}
	var revoked sql.NullTime
	err = tx.QueryRowContext(ctx, "SELECT revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE", claims.JTI).Scan(&revoked)
	if err != nil {
		failInternal(c, err)
		return
	}
	if status == "disabled" {
		respondError(c, &apiError{code: "E0102"})
		return
	}
	if revoked.Valid || time.Now().Unix() >= claims.Exp {
		respondError(c, &apiError{code: "E0002"})
		return
	}
	_, err = tx.ExecContext(ctx, "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE jti=$1", claims.JTI)
	if err != nil {
		failInternal(c, err)
		return
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, err)
		return
	}
	respondOK(c, 200, nil)
}

// me 回傳資料庫目前帳號，不建立新 session 或延長到期。
func (s *server) me(c *gin.Context) {
	a, _, ok := s.identity(c, true)
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
	respondOK(c, 200, accountData(*a))
}

// health 同時確認金鑰已載入及全部業務表可讀，避免假就緒。
func (s *server) health(c *gin.Context) {
	var n int
	// LIMIT 0 驗證全部表及讀取權限，避免健康檢查掃描多表笛卡兒積。
	err := s.db.QueryRowContext(c.Request.Context(), "SELECT 1 FROM accounts,login_sessions,login_failures,login_lockout_settings,articles,comments LIMIT 0").Scan(&n)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		c.String(503, "unavailable")
		return
	}
	c.String(200, "OK")
}

// run 建立固定路由，保持 Gin 預設 404 與 HTTP 方法錯誤可受共同回應控制。
func run() error {
	// 先驗持久金鑰及資料庫連線，失敗時讓容器退出而非提供假就緒服務。
	key, err := loadKey()
	if err != nil {
		return err
	}
	dsn := fmt.Sprintf("host=%s port=%s dbname=%s user=%s password=%s sslmode=disable timezone=Asia/Taipei", env("DB_HOST", "db"), env("DB_PORT", "5432"), env("DB_DATABASE", "articles"), env("DB_USERNAME", "articles_app"), os.Getenv("DB_PASSWORD"))
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(20)
	db.SetConnMaxLifetime(time.Hour)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err = db.PingContext(ctx); err != nil {
		return err
	}
	s := &server{db, key}
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	// 未列出的尾斜線路徑應走共同 404 回應，不由 Gin 轉成 HTML 301。
	router.RedirectTrailingSlash = false
	// 未預期 panic 仍輸出安全的共通 envelope，方便由 log_id 追查。
	router.Use(logMiddleware(), gin.CustomRecovery(func(c *gin.Context, _ any) {
		log.Printf("ERROR log_id=%s panic", c.GetString("log_id"))
		respondError(c, &apiError{code: "E9999"})
		c.Abort()
	}))
	// 路由直接綁定各責任檔案的 handler，所有框架保持相同方法與路徑。
	router.POST("/api/auth/register", s.register)
	router.POST("/api/auth/login", s.login)
	router.POST("/api/auth/logout", s.logout)
	router.GET("/api/me", s.me)
	router.GET("/api/articles", s.articles)
	router.POST("/api/articles", s.createArticle)
	router.GET("/api/articles/:article_id", s.article)
	router.PUT("/api/articles/:article_id", s.editArticle)
	router.PUT("/api/articles/:article_id/visibility", s.setArticleVisibility)
	router.GET("/api/articles/:article_id/comments", s.comments)
	router.POST("/api/articles/:article_id/comments", s.createComment)
	router.GET("/api/me/articles", s.myArticles)
	router.GET("/api/me/comments", s.myComments)
	router.GET("/api/comments/:comment_id", s.commentDetail)
	router.PUT("/api/comments/:comment_id", s.editComment)
	router.PUT("/api/comments/:comment_id/visibility", s.setCommentVisibility)
	router.GET("/api/members", s.members)
	router.GET("/api/members/:member_id", s.member)
	router.PUT("/api/members/:member_id/status", s.memberStatus)
	router.GET("/api/comments", s.adminComments)
	router.GET("/api/settings/login-lockout", s.loginLockoutSettings)
	router.PUT("/api/settings/login-lockout", s.updateLoginLockoutSettings)
	router.GET("/up", s.health)
	// 未知路徑也使用可追查的共同錯誤 envelope。
	router.NoRoute(func(c *gin.Context) { respondError(c, &apiError{code: "E0004"}) })
	// 已知路徑但方法不符時保留 HTTP 405。
	router.NoMethod(func(c *gin.Context) { respondError(c, &apiError{code: "E0005", status: 405}) })
	router.HandleMethodNotAllowed = true
	// Run 返回後由 defer 關閉資料庫池；啟動失敗交由 main 記錄並退出。
	return router.Run(":8000")
}

// env 僅對非秘密連線欄位給既有容器預設值。
func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// main 讓啟動失敗進入容器日誌並退出，避免金鑰或資料庫失效仍就緒。
func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
