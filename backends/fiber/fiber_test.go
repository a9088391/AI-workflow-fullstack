package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gofiber/fiber/v3"
)

// TestRawInput 驗證原始 Query 與 JSON 語意，不讓 Fiber 預設解析改變共同錯誤。
func TestRawInput(t *testing.T) {
	// 每組只指定一個可觀察差異；正確欄位需與錯誤一起回覆。
	cases := []struct {
		name, target, body string
		expected           map[string][]string
		code               string
	}{
		{"duplicate query", "/?page=1&page=2&per_page=20", "", map[string][]string{"page": {"此參數不可重複"}}, "E0001"},
		{"array query", "/?page[]=1&per_page=20", "", map[string][]string{"page": {"欄位型態不正確"}}, "E0001"},
		{"unknown query", "/?page=1&per_page=20&unexpected=1", "", map[string][]string{"unexpected": {"不支援此欄位"}}, "E0001"},
		{"last duplicate JSON", "/", `{"email":"bad","email":" A@Example.TEST ","password":"abcd","nickname":"合法"}`, nil, ""},
		{"short password", "/", `{"email":"a@example.test","password":"abc","nickname":"合法"}`, map[string][]string{"password": {invalid}}, "E0001"},
		{"non ASCII password", "/", `{"email":"a@example.test","password":"ａｂｃｄ","nickname":"合法"}`, map[string][]string{"password": {invalid}}, "E0001"},
		{"JSON root array", "/", `[]`, map[string][]string{"_body": {"Request Body 必須為 JSON object"}}, "E0001"},
		{"null and type", "/", `{"email":null,"password":1234,"nickname":"壞\u0000名","extra":true}`, map[string][]string{"email": {nullValue}, "password": {wrongType}, "nickname": {invalid}, "extra": {"不支援此欄位"}}, "E0001"},
		{"malformed JSON", "/", `{"email":`, nil, "E0005"},
		{"lone surrogate", "/", `{"email":"a@example.test","password":"abcd","nickname":"\ud800"}`, nil, "E0005"},
	}
	for _, test := range cases {
		// 子測試保留原始序列化文字，避免客戶端幫忙正規化特殊輸入。
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, test.target, strings.NewReader(test.body))
			request.Header.Set("Content-Type", "application/json")
			var result *apiError
			if test.body == "" {
				_, _, _, result = pageInput(request, "")
			} else {
				values, failure := bodyInput(request, []string{"email", "password", "nickname"}, false)
				result = failure
				if test.name == "last duplicate JSON" && (result != nil || values["email"] != "a@example.test") {
					t.Fatalf("last JSON value: values=%v error=%v", values, result)
				}
			}
			if test.code == "" {
				if result != nil {
					t.Fatalf("unexpected error: %v", result)
				}
				return
			}
			if result == nil || result.code != test.code || !reflect.DeepEqual(result.fields, test.expected) {
				t.Fatalf("got %#v; want code %s fields %#v", result, test.code, test.expected)
			}
		})
	}
}

// TestQueryErrorIdentity 經公開路由區分參數身分與錯誤標籤，避免編碼碰撞誤報重複。
func TestQueryErrorIdentity(t *testing.T) {
	cases := []struct {
		query  string
		fields map[string][]string
	}{
		{"odd%5Bx%5D=1", map[string][]string{"odd[x]": {"不支援此欄位"}}},
		{"odd%5Bx%5D=1&odd%5Bx%5D=2", map[string][]string{"odd[x]": {"此參數不可重複"}}},
		{"filters%5Bunexpected%5D=1", map[string][]string{"filters.unexpected": {"不支援此欄位"}}},
		{"filters%5Bunexpected%5D%5Bdeep%5D=1", map[string][]string{"filters[unexpected][deep]": {"不支援此欄位"}}},
		{"%FF=x&%25FF=y", map[string][]string{"%FF": {"不支援此欄位"}}},
		{"%25FF=x&%FF=y", map[string][]string{"%FF": {"不支援此欄位"}}},
		{"%FF=x&%ff=y", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%25FF=x&%25FF=y", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%FF=x&%FF=y&%25FF=z", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%FF=x&%25FF=y&%FF=z", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%25FF=x&%FF=y&%FF=z", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%25FF=x&%25FF=y&%FF=z", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%25FF=x&%FF=y&%25FF=z", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%FF=x&%25FF=y&%25FF=z", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%FF=x&%25FF=y&%ff=z&%25FF=w", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%25FF=x&%FF=y&%25FF=z&%ff=w", map[string][]string{"%FF": {"此參數不可重複"}}},
		{"%ff=x&%25ff=y", map[string][]string{"%FF": {"不支援此欄位"}, "%ff": {"不支援此欄位"}}},
		{"%FF=x&%EF%BF%BD=y", map[string][]string{"%FF": {"不支援此欄位"}, "\uFFFD": {"不支援此欄位"}}},
		{"%E6%9C%AA%E7%9F%A5=x", map[string][]string{"未知": {"不支援此欄位"}}},
		{"page%5B%FF%5D=x", map[string][]string{"page%5B%FF%5D": {"不支援此欄位"}}},
		{"filters%5B%FF%5D=x", map[string][]string{"filters%5B%FF%5D": {"不支援此欄位"}}},
		{"filters%5Bunexpected%5D=x&filters.unexpected=y", map[string][]string{"filters.unexpected": {"不支援此欄位"}}},
		{"filters%5Bunexpected%5D=x&filters%5Bunexpected%5D=y&filters.unexpected=z", map[string][]string{"filters.unexpected": {"此參數不可重複"}}},
	}
	app := newApp(&server{})
	for _, test := range cases {
		// 兩個公開寫入入口都應先拒絕 Query，不接觸資料庫或被損壞 JSON 改變優先序。
		for _, path := range []string{"/api/auth/register", "/api/auth/login"} {
			// 直接保留 RawQuery 百分比拼法，才能區分非法位元組與字面百分比。
			t.Run(path+"?"+test.query, func(t *testing.T) {
				r := httptest.NewRequest(http.MethodPost, path+"?"+test.query, strings.NewReader("{"))
				r.Header.Set("Content-Type", "application/json")
				response, err := app.Test(r)
				if err != nil {
					t.Fatal(err)
				}
				defer response.Body.Close()
				var body struct {
					Code   string              `json:"error_code"`
					Fields map[string][]string `json:"errors"`
				}
				if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
					t.Fatal(err)
				}
				if response.StatusCode != 422 || body.Code != "E0001" || !reflect.DeepEqual(body.Fields, test.fields) {
					t.Fatalf("status=%d body=%+v; want fields=%v", response.StatusCode, body, test.fields)
				}
			})
		}
	}
}

// TestListQueryPriority 驗證支援欄位才收攏陣列，且共用顯示名稱不會掩蓋較優先錯誤。
func TestListQueryPriority(t *testing.T) {
	cases := []struct {
		query  string
		fields map[string][]string
	}{
		{"page[]=1&per_page=20", map[string][]string{"page": {wrongType}}},
		{"page[0]=1&page[1]=2&per_page=20", map[string][]string{"page": {"此參數不可重複"}}},
		{"page=1&page[0]=2&per_page=20", map[string][]string{"page": {"此參數不可重複"}}},
		{"page=bad&per_page=101", map[string][]string{"page": {invalid}, "per_page": {invalid}}},
		{"page=1&per_page=20&filters[scope][]=bad", map[string][]string{"filters.scope": {wrongType}}},
		{"page=1&per_page=20&filters[scope]=bad&filters[scope][]=bad", map[string][]string{"filters.scope": {"此參數不可重複"}}},
		{"page=1&per_page=20&filters[scope][]=bad&filters[scope]=bad", map[string][]string{"filters.scope": {"此參數不可重複"}}},
		{"page=1&per_page=20&filters[scope][]=bad&filters.scope=x", map[string][]string{"filters.scope": {wrongType}}},
		{"page=1&per_page=20&filters.scope=x&filters[scope][]=bad", map[string][]string{"filters.scope": {wrongType}}},
		{"page=1&per_page=20&filters[scope]=bad&filters.scope=x", map[string][]string{"filters.scope": {"不支援此欄位"}}},
		{"page=1&per_page=20&filters.scope=x&filters[scope]=bad", map[string][]string{"filters.scope": {"不支援此欄位"}}},
		{"page=1&per_page=20&filters[scope][]=bad&filters.scope=x&filters.scope=y", map[string][]string{"filters.scope": {"此參數不可重複"}}},
		{"page=1&per_page=20&filters[unexpected][deep]=x", map[string][]string{"filters[unexpected][deep]": {"不支援此欄位"}}},
		{"page=1&per_page=20&filters[keyword][]=x", map[string][]string{"filters[keyword][]": {"不支援此欄位"}}},
		{"page=1&per_page=20&page%5B%FF%5D=x", map[string][]string{"page%5B%FF%5D": {"不支援此欄位"}}},
	}
	app := newApp(&server{})
	for _, test := range cases {
		// 每筆均在 Query 驗證停止，不需資料庫，也不能被解析器靜默取第一或最後值。
		t.Run(test.query, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, "/api/articles?"+test.query, nil)
			response, err := app.Test(r)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var body struct {
				Code   string              `json:"error_code"`
				Fields map[string][]string `json:"errors"`
			}
			if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != 422 || body.Code != "E0001" || !reflect.DeepEqual(body.Fields, test.fields) {
				t.Fatalf("status=%d body=%+v; want fields=%v", response.StatusCode, body, test.fields)
			}
		})
	}
}

// TestRequestBridgeLifetime 經實際 Fiber handler 讀回輸入，確認請求期限與結束取消不污染下一筆。
func TestRequestBridgeLifetime(t *testing.T) {
	app := newApp(&server{})
	contexts := make(chan context.Context, 2)
	// 此同步讀回端點只存在測試，確保橋接不先解析 Query 或改寫本文、重複標頭及 Path 參數。
	app.Post("/bridge/:id", asHandler(func(c *requestContext) {
		r := c.Request()
		contexts <- r.Context()
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		deadline, ok := r.Context().Deadline()
		c.JSON(200, map[string]any{"method": r.Method, "path": r.URL.Path, "query": r.URL.RawQuery,
			"body": string(raw), "header": r.Header.Values("X-Bridge"), "id": c.Param("id"),
			"bounded": ok && time.Until(deadline) > 0 && time.Until(deadline) <= 15*time.Second, "active": r.Context().Err() == nil})
	}))
	for i := 0; i < 2; i++ {
		// 相同輸入連續兩次，分別確認新 context 有效、離開 handler 後已取消。
		r := httptest.NewRequest(http.MethodPost, "/bridge/0001?%FF=x&%25FF=y", strings.NewReader("原文\r\n下一行"))
		r.Header.Add("X-Bridge", "first")
		r.Header.Add("X-Bridge", "second")
		response, err := app.Test(r)
		if err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		err = json.NewDecoder(response.Body).Decode(&body)
		response.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		want := map[string]any{"method": "POST", "path": "/bridge/0001", "query": "%FF=x&%25FF=y",
			"body": "原文\r\n下一行", "header": []any{"first", "second"}, "id": "0001", "bounded": true, "active": true}
		if response.StatusCode != 200 || !reflect.DeepEqual(body, want) {
			t.Fatalf("bridge status=%d body=%v", response.StatusCode, body)
		}
		if ctx := <-contexts; ctx.Err() != context.Canceled {
			t.Fatalf("handler context after response: %v", ctx.Err())
		}
	}
}

// TestLargeBodyBoundary 驗證大型本文仍依端點驗證順序回覆，且日誌記錄實際狀態。
func TestLargeBodyBoundary(t *testing.T) {
	// 分別跨過原本的 1 MiB 與 4 MiB 傳輸門檻，避免只把錯誤推到較大的本文。
	cases := []struct {
		name, method, target, authorization string
		size                                int
		status                              int
		code                                string
	}{
		{"me 2 MiB", "GET", "/api/me", "", 2 << 20, 401, "E0002"},
		{"me 5 MiB", "GET", "/api/me", "", 5 << 20, 401, "E0002"},
		{"articles 2 MiB", "GET", "/api/articles?page=1&per_page=20", "", 2 << 20, 422, "E0001"},
		{"articles 5 MiB", "GET", "/api/articles?page=1&per_page=20", "", 5 << 20, 422, "E0001"},
		{"articles invalid JWT 5 MiB", "GET", "/api/articles?page=1&per_page=20", "Bearer invalid", 5 << 20, 401, "E0002"},
		{"logout 2 MiB", "POST", "/api/auth/logout", "", 2 << 20, 401, "E0002"},
		{"logout 5 MiB", "POST", "/api/auth/logout", "", 5 << 20, 401, "E0002"},
		{"login 5 MiB", "POST", "/api/auth/login", "", 5 << 20, 400, "E0005"},
		{"wrong method 5 MiB", "PATCH", "/api/me", "", 5 << 20, 405, "E0005"},
		{"unknown route 5 MiB", "GET", "/not-an-api", "", 5 << 20, 404, "E0004"},
	}
	app := newApp(&server{})
	for _, test := range cases {
		// 每組只保留自己的日誌，逐筆比對 response log_id 與 IN／EX／OUT。
		t.Run(test.name, func(t *testing.T) {
			var logs bytes.Buffer
			oldWriter := log.Writer()
			log.SetOutput(&logs)
			defer log.SetOutput(oldWriter)
			request := httptest.NewRequest(test.method, test.target, strings.NewReader(strings.Repeat("x", test.size)))
			request.Header.Set("Origin", "http://localhost:8080")
			if test.authorization != "" {
				request.Header.Set("Authorization", test.authorization)
			}
			response, err := app.Test(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var body map[string]any
			if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != test.status || body["error_code"] != test.code {
				t.Fatalf("status=%d body=%v", response.StatusCode, body)
			}
			if test.code == "E0001" && !reflect.DeepEqual(body["errors"], map[string]any{"_body": []any{"此 API 不接受 Request Body"}}) {
				t.Fatalf("GET body errors=%v", body["errors"])
			}
			if response.Header.Get("Access-Control-Allow-Origin") != "http://localhost:8080" {
				t.Fatalf("CORS headers=%v", response.Header)
			}
			id, ok := body["log_id"].(string)
			if !ok || id == "" {
				t.Fatalf("missing log_id: %v", body)
			}
			for _, want := range []string{"IN log_id=" + id + " ", "EX log_id=" + id + " status=" + strconv.Itoa(test.status), "OUT log_id=" + id + " status=" + strconv.Itoa(test.status)} {
				if strings.Count(logs.String(), want) != 1 {
					t.Fatalf("expected one %q in log %q", want, logs.String())
				}
			}
		})
	}
}

// TestStreamedBodyConnection 驗證大型與 chunked 本文送完才回應，並可安全重用連線。
func TestStreamedBodyConnection(t *testing.T) {
	var logs bytes.Buffer
	oldWriter := log.Writer()
	log.SetOutput(&logs)
	defer log.SetOutput(oldWriter)
	app := newApp(&server{})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	// 只為這個測試啟動暫時 listener，清理時等待 Fiber 停止。
	go func() { done <- app.Listener(listener, fiber.ListenConfig{DisableStartupMessage: true}) }()
	// 失敗路徑也要關閉連線及伺服器，避免背景處理器留到下一個測試。
	defer func() {
		if err := app.ShutdownWithTimeout(time.Second); err != nil {
			t.Errorf("shutdown: %v", err)
		}
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Error("listener did not stop")
		}
	}()
	conn, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(8 * time.Second)); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(conn)
	// 分段送完 5 MiB 後才讀回應；提前關閉會讓真實客戶端遇到 BrokenPipe。
	if _, err := fmt.Fprintf(conn, "GET /api/me HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost:8080\r\nContent-Length: %d\r\n\r\n", 5<<20); err != nil {
		t.Fatal(err)
	}
	chunk := strings.Repeat("x", 32<<10)
	for sent := 0; sent < 5<<20; sent += len(chunk) {
		if _, err := io.WriteString(conn, chunk); err != nil {
			t.Fatalf("upload stopped at %d bytes: %v", sent, err)
		}
	}
	response, err := http.ReadResponse(reader, nil)
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != 401 || response.Close || body["error_code"] != "E0002" || response.Header.Get("Access-Control-Allow-Origin") != "http://localhost:8080" {
		t.Fatalf("large GET status=%d close=%v body=%v headers=%v", response.StatusCode, response.Close, body, response.Header)
	}
	if _, err := io.Copy(io.Discard, response.Body); err != nil {
		t.Fatal(err)
	}
	if err := response.Body.Close(); err != nil {
		t.Fatal(err)
	}
	// 同一條連線下一個路由仍須正確解析，證明上一筆本文已讀到 EOF。
	if _, err := io.WriteString(conn, "GET /not-an-api HTTP/1.1\r\nHost: localhost\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	next, err := http.ReadResponse(reader, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer next.Body.Close()
	if next.StatusCode != 404 {
		t.Fatalf("reused connection status=%d", next.StatusCode)
	}
	if _, err := io.Copy(io.Discard, next.Body); err != nil {
		t.Fatal(err)
	}
	// chunked 本文也要讀到結束區塊，否則下一筆請求會被當成殘餘 chunk。
	stream, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	if err := stream.SetDeadline(time.Now().Add(8 * time.Second)); err != nil {
		t.Fatal(err)
	}
	streamReader := bufio.NewReader(stream)
	if _, err := io.WriteString(stream, "GET /api/articles?page=1&per_page=20 HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost:8080\r\nTransfer-Encoding: chunked\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 64; i++ {
		if _, err := fmt.Fprintf(stream, "%x\r\n%s\r\n", len(chunk), chunk); err != nil {
			t.Fatalf("chunk %d: %v", i, err)
		}
	}
	if _, err := io.WriteString(stream, "0\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	streamResponse, err := http.ReadResponse(streamReader, nil)
	if err != nil {
		t.Fatal(err)
	}
	var streamBody map[string]any
	if err := json.NewDecoder(streamResponse.Body).Decode(&streamBody); err != nil {
		t.Fatal(err)
	}
	if streamResponse.StatusCode != 422 || streamResponse.Close || streamBody["error_code"] != "E0001" || streamResponse.Header.Get("Access-Control-Allow-Origin") != "http://localhost:8080" {
		t.Fatalf("chunked status=%d close=%v body=%v headers=%v", streamResponse.StatusCode, streamResponse.Close, streamBody, streamResponse.Header)
	}
	if _, err := io.Copy(io.Discard, streamResponse.Body); err != nil {
		t.Fatal(err)
	}
	if err := streamResponse.Body.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := io.WriteString(stream, "GET /api/me HTTP/1.1\r\nHost: localhost\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	streamNext, err := http.ReadResponse(streamReader, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer streamNext.Body.Close()
	if streamNext.StatusCode != 401 {
		t.Fatalf("chunked reused connection status=%d", streamNext.StatusCode)
	}
	// 損壞的 chunk 結構屬於傳輸錯誤，需回 400/E0005 並關閉連線。
	bad, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer bad.Close()
	if err := bad.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := io.WriteString(bad, "GET /api/articles?page=1&per_page=20 HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost:8080\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\nZ\r\n"); err != nil {
		t.Fatal(err)
	}
	badResponse, err := http.ReadResponse(bufio.NewReader(bad), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer badResponse.Body.Close()
	var badBody map[string]any
	if err := json.NewDecoder(badResponse.Body).Decode(&badBody); err != nil {
		t.Fatal(err)
	}
	if badResponse.StatusCode != 400 || !badResponse.Close || badBody["error_code"] != "E0005" || badResponse.Header.Get("Access-Control-Allow-Origin") != "http://localhost:8080" {
		t.Fatalf("transport error status=%d body=%v headers=%v", badResponse.StatusCode, badBody, badResponse.Header)
	}
	id, ok := badBody["log_id"].(string)
	if !ok || id == "" {
		t.Fatalf("transport error missing log_id: %v", badBody)
	}
	for _, want := range []string{"IN log_id=" + id + " ", "EX log_id=" + id + " status=400", "OUT log_id=" + id + " status=400"} {
		if strings.Count(logs.String(), want) != 1 {
			t.Fatalf("expected one %q in log %q", want, logs.String())
		}
	}
}

// TestAuthPrimitives 固定 Argon2id 與本後端 JWT 的重要驗證邊界。
func TestAuthPrimitives(t *testing.T) {
	// 正確密碼、錯誤密碼與毀損 PHC 必須可區分，毀損不算登入失敗。
	hash, err := passwordHash("abcd")
	if err != nil {
		t.Fatal(err)
	}
	if ok, failure := verifyPassword("abcd", hash); !ok || failure != nil {
		t.Fatalf("valid PHC: %v %v", ok, failure)
	}
	if ok, failure := verifyPassword("abce", hash); ok || failure != nil {
		t.Fatalf("wrong password: %v %v", ok, failure)
	}
	if _, failure := verifyPassword("abcd", "invalid"); failure == nil || failure.code != "E0104" {
		t.Fatalf("corrupt PHC: %v", failure)
	}
	// 固定測試金鑰只驗本後端簽章與八小時 claims，不使用執行環境秘密。
	key := []byte("12345678901234567890123456789012")
	token, err := issueToken(key, 1, "AAAAAAAAAAAAAAAAAAAAAA", time.Now().Unix())
	if err != nil {
		t.Fatal(err)
	}
	if claim, failure := verifyToken(key, token); failure != nil || claim.Iss != issuer || claim.Exp-claim.Iat != duration {
		t.Fatalf("own token: %v %v", claim, failure)
	}
	if _, failure := verifyToken([]byte("another-1234567890123456789012345"), token); failure == nil || failure.code != "E0002" {
		t.Fatalf("other key: %v", failure)
	}
}

// TestFiberHTTPBoundary 驗證 Fiber 路由、CORS 與 API envelope 的可觀察回應。
func TestFiberHTTPBoundary(t *testing.T) {
	app := newApp(&server{})
	cases := []struct {
		method, target, body, origin string
		status                       int
		code                         string
	}{
		{"GET", "/api/me", "", "", 401, "E0002"},
		{"GET", "/api/articles?page=1&per_page=20", "", "", 401, "E0002"},
		{"POST", "/api/auth/register?bad=1", `{}`, "", 422, "E0001"},
		{"POST", "/api/auth/register?bad=1&bad=2", `{}`, "", 422, "E0001"},
		{"POST", "/api/auth/register?page[]=1", `{}`, "", 422, "E0001"},
		{"POST", "/api/auth/register", `{"email":`, "", 400, "E0005"},
		{"PATCH", "/api/me", "", "", 405, "E0005"},
		{"GET", "/API/me", "", "", 404, "E0004"},
		{"GET", "/api/me/", "", "", 404, "E0004"},
		{"GET", "/not-an-api", "", "", 404, "E0004"},
		{"OPTIONS", "/api/auth/login", "", "http://localhost:8080", 204, ""},
	}
	for _, test := range cases {
		// 只對不需資料庫的邊界送請求，其他交易由隔離 HTTP 契約測試覆蓋。
		t.Run(test.method+" "+test.target, func(t *testing.T) {
			request := httptest.NewRequest(test.method, test.target, strings.NewReader(test.body))
			if test.body != "" {
				request.Header.Set("Content-Type", "application/json")
			}
			if test.origin != "" {
				request.Header.Set("Origin", test.origin)
			}
			if test.target == "/api/articles?page=1&per_page=20" {
				request.Header.Set("Authorization", "Bearer invalid")
			}
			response, err := app.Test(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			if response.StatusCode != test.status {
				raw, _ := io.ReadAll(response.Body)
				t.Fatalf("status %d body %s", response.StatusCode, raw)
			}
			if test.code == "" {
				if response.Header.Get("Access-Control-Allow-Origin") != test.origin || response.Header.Get("Access-Control-Expose-Headers") != "Retry-After" {
					t.Fatalf("CORS headers: %v", response.Header)
				}
				return
			}
			var body map[string]any
			if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if body["error_code"] != test.code || body["success"] != false || body["message"] != messages[test.code].message || body["log_id"] == "" {
				t.Fatalf("envelope: %#v", body)
			}
		})
	}
}
