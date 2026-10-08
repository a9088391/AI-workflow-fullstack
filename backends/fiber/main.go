package main

import (
	"context"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/recover"
	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
)

var messages = map[string]struct {
	status  int
	message string
}{
	"E0001": {422, "欄位資料不正確，請確認後再送出"}, "E0002": {401, "登入已逾期，請重新登入"}, "E0003": {403, "沒有權限使用此功能"}, "E0004": {404, "找不到指定資料或 API"}, "E0005": {400, "系統暫時無法處理，請稍後再試"}, "E0101": {401, "帳號或密碼錯誤"}, "E0102": {403, "此帳號已停用，請聯絡系統管理員"}, "E0104": {500, "後台認證設定異常，請聯絡系統管理員"}, "E0105": {429, "登入失敗次數過多，請稍後再試"}, "E9999": {500, "系統暫時無法處理，請稍後再試"},
	"E0201": {409, "內容已更新，請確認最新內容後再送出"},
}

// server 持有唯一資料庫池與 Fiber 專屬金鑰，避免請求間共用可變身分。
type server struct {
	db  *sql.DB
	key []byte
}

// respondError 使用共同錯誤 envelope，並以 log_id 串起此請求的紀錄。
func respondError(c *requestContext, e *apiError) {
	detail := messages[e.code]
	if e.status != 0 {
		detail.status = e.status
	}
	body := map[string]any{"success": false, "message": detail.message, "error_code": e.code, "log_id": c.Get("log_id").(string)}
	if e.fields != nil {
		body["errors"] = e.fields
	}
	if e.retry > 0 {
		c.c.Set("Retry-After", strconv.Itoa(e.retry))
	}
	c.JSON(detail.status, body)
}

// respondOK 只輸出已核准成功欄位，不帶錯誤或日誌識別。
func respondOK(c *requestContext, status int, data any) {
	if data == nil {
		data = map[string]any{}
	}
	c.JSON(status, map[string]any{"success": true, "message": "OK", "data": data})
}

// failInternal 記錄內部失敗但不把 SQL 或金鑰細節回給客戶端。
func failInternal(c *requestContext, err error) {
	log.Printf("ERROR log_id=%s err=%v", c.Get("log_id").(string), err)
	respondError(c, &apiError{code: "E9999"})
}

// register 驗證全部輸入後使用唯一索引裁決跨後端並行註冊。
func (s *server) register(c *requestContext) {
	// 公開註冊忽略舊 JWT，先完成 Query 與 Body 驗證才接觸資料庫。
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request(), []string{"email", "password", "nickname"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	var exists bool
	err := s.db.QueryRowContext(c.Request().Context(), "SELECT EXISTS(SELECT 1 FROM accounts WHERE email=$1)", body["email"]).Scan(&exists)
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
	_, err = s.db.ExecContext(c.Request().Context(), "INSERT INTO accounts (email,password_hash,nickname,role,status) VALUES ($1,$2,$3,'member','active')", body["email"], hash, body["nickname"])
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
func (s *server) login(c *requestContext) {
	// 格式錯誤不計入鎖定，所以先完整驗證帳密。
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	body, e := bodyInput(c.Request(), []string{"email", "password"}, false)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request().Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, err)
		return
	}
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
		// 雜湊格式異常需可追查，但日誌不得包含密碼或 PHC 內容。
		log.Printf("ERROR log_id=%s invalid password hash", c.Get("log_id").(string))
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
	_, err = tx.ExecContext(ctx, "INSERT INTO login_sessions (jti,account_id,backend_id,issued_at,expires_at) VALUES ($1,$2,'fiber',to_timestamp($3),to_timestamp($4))", jti, a.ID, issued, issued+duration)
	if err != nil {
		failInternal(c, err)
		return
	}
	if err = tx.Commit(); err != nil {
		failInternal(c, err)
		return
	}
	respondOK(c, 200, map[string]any{"access_token": token, "token_type": "Bearer", "expires_at": businessTime(time.Unix(issued+duration, 0)), "user": accountData(a)})
}

// identity 對保護及條件式公開端點套用同一 JWT/session 查核順序。
func (s *server) identity(c *requestContext, required bool) (*account, *claims, bool) {
	// 公開讀取若送出空 Authorization 也屬於失效憑證，不能降成訪客。
	if _, supplied := c.Request().Header["Authorization"]; supplied {
		required = true
	}
	a, claims, e, err := authenticate(c.Request().Context(), s.db, s.key, c.Request().Header.Get("Authorization"), required)
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
func (s *server) logout(c *requestContext) {
	// 先認證再檢查 Body，符合保護端點的錯誤優先順序。
	a, claims, ok := s.identity(c, true)
	if !ok {
		return
	}
	if e := noQuery(c.Request()); e != nil {
		respondError(c, e)
		return
	}
	_, e := bodyInput(c.Request(), []string{}, true)
	if e != nil {
		respondError(c, e)
		return
	}
	ctx := c.Request().Context()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		failInternal(c, err)
		return
	}
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
func (s *server) me(c *requestContext) {
	a, _, ok := s.identity(c, true)
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
	respondOK(c, 200, accountData(*a))
}

// health 同時確認金鑰已載入及全部業務表可讀，避免假就緒。
func (s *server) health(c *requestContext) {
	var n int
	err := s.db.QueryRowContext(c.Request().Context(), "SELECT 1 FROM accounts,login_sessions,login_failures,login_lockout_settings,articles,comments LIMIT 1").Scan(&n)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		c.String(503, "unavailable")
		return
	}
	c.String(200, "OK")
}

// newApp 將共同 API 掛在 Fiber 3 路由，保留明確的方法與錯誤格式。
func newApp(s *server) *fiber.App {
	// 傳輸層有界預讀；處理器限量解析本文並保留認證優先順序。
	app := fiber.New(fiber.Config{
		BodyLimit:                    1<<20 + 1,
		StreamRequestBody:            true,
		DisablePreParseMultipartForm: true,
		ReadTimeout:                  15 * time.Second,
		CaseSensitive:                true,
		StrictRouting:                true,
		DisableHeadAutoRegister:      true,
		ErrorHandler:                 fiberError,
	})
	app.Use(logMiddleware, recover.New())
	app.Post("/api/auth/register", asHandler(s.register))
	app.Post("/api/auth/login", asHandler(s.login))
	app.Post("/api/auth/logout", asHandler(s.logout))
	app.Get("/api/me", asHandler(s.me))
	app.Get("/api/me/articles", asHandler(s.myArticles))
	app.Get("/api/me/comments", asHandler(s.myComments))
	app.Get("/api/articles", asHandler(s.articles))
	app.Post("/api/articles", asHandler(s.createArticle))
	app.Get("/api/articles/:article_id", asHandler(s.article))
	app.Put("/api/articles/:article_id", asHandler(s.updateArticle))
	app.Put("/api/articles/:article_id/visibility", asHandler(s.articleVisibility))
	app.Get("/api/articles/:article_id/comments", asHandler(s.comments))
	app.Post("/api/articles/:article_id/comments", asHandler(s.createComment))
	app.Get("/api/comments/:comment_id", asHandler(s.commentDetail))
	app.Put("/api/comments/:comment_id", asHandler(s.updateComment))
	app.Put("/api/comments/:comment_id/visibility", asHandler(s.commentVisibility))
	app.Get("/up", asHandler(s.health))
	// 既有路徑使用其他方法時回 405；未知路徑交由共同 404 處理。
	for _, path := range []string{"/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/me", "/api/me/articles", "/api/me/comments", "/api/articles", "/api/articles/:article_id", "/api/articles/:article_id/visibility", "/api/articles/:article_id/comments", "/api/comments/:comment_id", "/api/comments/:comment_id/visibility", "/up"} {
		// 只攔同一資源路徑的錯誤方法，不替未實作操作提供假成功。
		app.All(path, func(c fiber.Ctx) error {
			respondError(&requestContext{c: c}, &apiError{code: "E0005", status: 405})
			return nil
		})
	}
	return app
}

// run 載入持久金鑰及共用資料庫，成功後才監聽固定服務埠。
func run() error {
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
	if err := db.PingContext(ctx); err != nil {
		return err
	}
	return newApp(&server{db, key}).Listen(":8000")
}

// env 只對非秘密連線欄位使用既有容器預設值。
func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// main 將資料庫或金鑰啟動失敗留在容器日誌並退出。
func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
