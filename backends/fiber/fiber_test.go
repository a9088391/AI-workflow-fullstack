package main

import (
	"bufio"
	"bytes"
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
