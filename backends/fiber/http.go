package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"time"

	"github.com/gofiber/fiber/v3"
)

// requestContext 只在同步處理器內保留 Fiber context 與原始 HTTP 輸入。
type requestContext struct {
	c       fiber.Ctx
	request *http.Request
}

// Request 將 Fiber 原始 Query、Body 和標頭交給共同驗證器，避免框架預解析改變契約。
func (c *requestContext) Request() *http.Request {
	if c.request == nil {
		header := http.Header{}
		// 標頭逐筆保留，讓 Authorization 與 JSON 型態檢查使用實際請求值。
		c.c.Request().Header.VisitAll(func(key, value []byte) {
			header.Add(string(key), string(value))
		})
		// 大型本文由 fasthttp 串流提供；BodyRaw 會把剩餘內容全載入記憶體。
		var body io.Reader = c.c.Request().BodyStream()
		if body == nil {
			body = bytes.NewReader(c.c.BodyRaw())
		}
		request := &http.Request{
			Method: c.c.Method(),
			URL:    &url.URL{Path: c.c.Path(), RawQuery: string(c.c.Request().URI().QueryString())},
			Header: header,
			Body:   io.NopCloser(body),
		}
		// Fiber context 會重用；只在目前處理器內使用並讓資料庫呼叫有明確時限。
		ctx, cancel := context.WithTimeout(c.c.Context(), 15*time.Second)
		c.c.Locals("request_cancel", cancel)
		c.request = request.WithContext(ctx)
	}
	return c.request
}

// Param 讀取 Fiber 路由參數，保留 URL ID 驗證給共同驗證器。
func (c *requestContext) Param(name string) string { return c.c.Params(name) }

// Get 讀取本次請求的日誌識別，不跨請求保存身分。
func (c *requestContext) Get(name string) any { return c.c.Locals(name) }

// JSON 只輸出處理器指定的共同狀態與資料形狀。
func (c *requestContext) JSON(status int, value any) { _ = c.c.Status(status).JSON(value) }

// String 供健康檢查回覆最小文字狀態。
func (c *requestContext) String(status int, value string) { _ = c.c.Status(status).SendString(value) }

// asHandler 在同步邊界清理請求 context，避免資料庫時限資源延續到下一個請求。
func asHandler(handle func(*requestContext)) fiber.Handler {
	// Fiber context 僅在此 callback 有效，處理結束就取消資料庫時限。
	return func(c fiber.Ctx) error {
		request := &requestContext{c: c}
		// 清理可能已建立的請求 context；無資料庫操作時不需建立。
		defer func() {
			if cancel, ok := c.Locals("request_cancel").(context.CancelFunc); ok {
				cancel()
			}
		}()
		handle(request)
		return nil
	}
}

// setCORS 只對核准來源設定跨來源標頭；路由前錯誤仍保有 Origin 時也適用。
func setCORS(c fiber.Ctx) {
	origin := env("CORS_ORIGIN", "http://localhost:8080")
	if c.Get("Origin") == origin {
		c.Set("Access-Control-Allow-Origin", origin)
		c.Set("Vary", "Origin")
		c.Set("Access-Control-Expose-Headers", "Retry-After")
	}
}

// finishLog 在回應狀態已確定後，以同一個 log_id 記錄錯誤及完成狀態。
func finishLog(c fiber.Ctx) {
	id := c.Locals("log_id").(string)
	status := c.Response().StatusCode()
	if status >= 400 {
		log.Printf("EX log_id=%s status=%d", id, status)
	}
	log.Printf("OUT log_id=%s status=%d", id, status)
}

// logMiddleware 配置追查 ID、CORS 與 OPTIONS，回應前讀盡串流並記錄實際狀態。
func logMiddleware(c fiber.Ctx) error {
	raw, err := randomBytes(16)
	if err != nil {
		return c.SendStatus(500)
	}
	id := fmt.Sprintf("%x-%x-%x-%x-%x", raw[:4], raw[4:6], raw[6:8], raw[8:10], raw[10:])
	c.Locals("log_id", id)
	setCORS(c)
	log.Printf("IN log_id=%s method=%s path=%s", id, c.Method(), c.Path())
	if c.Method() == http.MethodOptions {
		c.Set("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
		c.Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
		err = c.SendStatus(204)
	} else {
		err = c.Next()
	}
	// 處理器已決定驗證結果；送出前讀盡本文，避免客戶端仍在上傳時被關閉或污染下一筆請求。
	if stream := c.Request().BodyStream(); stream != nil {
		if _, readErr := io.Copy(io.Discard, stream); readErr != nil {
			// HTTP 本文不完整時不保留連線；不把不可信的解析內容寫進日誌。
			c.Response().Header.SetConnectionClose()
			log.Printf("ERROR log_id=%s request body read failed", id)
			return fiberError(c, fiber.ErrBadRequest)
		}
	}
	if err != nil {
		return fiberError(c, err)
	}
	// fasthttp 的讀取錯誤會先走 use middleware，再由 Fiber 呼叫 ErrorHandler。
	if !c.Matched() && c.Response().StatusCode() == 200 {
		return nil
	}
	finishLog(c)
	return nil
}

// fiberError 將路由與 HTTP 失敗轉成共同 envelope，不向客戶端洩漏框架訊息。
func fiberError(c fiber.Ctx, err error) error {
	status := fiber.StatusInternalServerError
	if failure, ok := err.(*fiber.Error); ok {
		status = failure.Code
	}
	code := "E9999"
	if status == 404 {
		code = "E0004"
	} else if status < 500 {
		code = "E0005"
	}
	if c.Locals("log_id") == nil {
		// 不支援的方法等錯誤可能未進入 middleware，仍補上可追查的 IN。
		raw, randomErr := randomBytes(16)
		if randomErr != nil {
			return c.SendStatus(500)
		}
		id := fmt.Sprintf("%x-%x-%x-%x-%x", raw[:4], raw[4:6], raw[6:8], raw[8:10], raw[10:])
		c.Locals("log_id", id)
		log.Printf("IN log_id=%s method=%s path=%s", id, c.Method(), c.Path())
	}
	setCORS(c)
	if status >= 500 {
		log.Printf("ERROR log_id=%s internal handler failure", c.Locals("log_id"))
	}
	respondError(&requestContext{c: c}, &apiError{code: code, status: status})
	finishLog(c)
	return nil
}
