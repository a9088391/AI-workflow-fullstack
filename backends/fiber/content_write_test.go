package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

// TestWriterAuthenticationFirst 確認新寫入路由在無憑證時先回 401，不揭露本文與目標格式。
func TestWriterAuthenticationFirst(t *testing.T) {
	app := newApp(&server{})
	for _, target := range []string{"/api/articles", "/api/articles/not-an-id", "/api/articles/not-an-id/visibility", "/api/articles/not-an-id/comments", "/api/comments/not-an-id", "/api/comments/not-an-id/visibility"} {
		// 故意提供錯誤 Path 與 Body；身份門禁必須先於任何欄位驗證。
		t.Run(target, func(t *testing.T) {
			method := http.MethodPut
			if target == "/api/articles" || strings.HasSuffix(target, "/comments") {
				method = http.MethodPost
			}
			request := httptest.NewRequest(method, target, strings.NewReader(`{bad`))
			request.Header.Set("Content-Type", "application/json")
			response, err := app.Test(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var payload map[string]any
			if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != 401 || payload["error_code"] != "E0002" {
				t.Fatalf("status=%d payload=%v", response.StatusCode, payload)
			}
		})
	}
}

// TestContentInput 驗證新作者操作的 JSON 整數、文字與未知欄位，不讓資料庫約束變成 500。
func TestContentInput(t *testing.T) {
	cases := []struct {
		name, raw string
		fields    []string
		errors    map[string][]string
	}{
		{"valid article", `{"title":" 標題 ","body":"一\r\n二","version":1}`, []string{"title", "body", "version"}, nil},
		{"version string", `{"body":"文","version":"1"}`, []string{"body", "version"}, map[string][]string{"version": {wrongType}}},
		{"version float", `{"body":"文","version":1.0}`, []string{"body", "version"}, map[string][]string{"version": {wrongType}}},
		{"version negative", `{"body":"文","version":-1}`, []string{"body", "version"}, map[string][]string{"version": {invalid}}},
		{"version overflow", `{"body":"文","version":9007199254740992}`, []string{"body", "version"}, map[string][]string{"version": {invalid}}},
		{"blank and NUL", `{"title":"　","body":"x\u0000","version":1,"author_id":2}`, []string{"title", "body", "version"}, map[string][]string{"title": {invalid}, "body": {invalid}, "author_id": {"不支援此欄位"}}},
		{"comment limit", `{"body":"` + strings.Repeat("文", 2001) + `"}`, []string{"body"}, map[string][]string{"body": {invalid}}},
		{"comment limit independent of field order", `{"version":1,"body":"` + strings.Repeat("文", 2001) + `"}`, []string{"version", "body"}, map[string][]string{"body": {invalid}}},
		{"article body remains longer", `{"body":"` + strings.Repeat("文", 2001) + `","version":1,"title":"標題"}`, []string{"body", "version", "title"}, nil},
	}
	for _, test := range cases {
		// 每組保留原始 JSON bytes，避免測試序列化器改寫整數與換行。
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest("POST", "/", strings.NewReader(test.raw))
			request.Header.Set("Content-Type", "application/json")
			values, e := bodyInput(request, test.fields, false)
			if test.errors == nil {
				if e != nil || values == nil {
					t.Fatalf("expected valid input: values=%v error=%v", values, e)
				}
			} else if e == nil || e.code != "E0001" || !reflect.DeepEqual(e.fields, test.errors) {
				t.Fatalf("got %#v; want %#v", e, test.errors)
			}
		})
	}
}

// TestRestoreControl 驗證管理員接管、作者不可繞過限制及重複下架的狀態選擇。
func TestRestoreControl(t *testing.T) {
	owner := &account{ID: 1, Role: "member"}
	admin := &account{ID: 2, Role: "admin"}
	hidden := content{AuthorID: 1, Visibility: "hidden"}
	hidden.Restore.Valid, hidden.Restore.String = true, "author"
	if nextRestore(hidden, admin, "hidden") != "admin" {
		t.Fatal("admin takeover lost")
	}
	hidden.Restore.String = "admin"
	if nextRestore(hidden, owner, "hidden") != "admin" || restoreAllowed(hidden, owner, "visible") == nil {
		t.Fatal("owner bypassed admin restore control")
	}
	if nextRestore(hidden, admin, "visible") != "" || restoreAllowed(hidden, admin, "visible") != nil {
		t.Fatal("admin cannot restore")
	}
}
