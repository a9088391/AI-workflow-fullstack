package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

// TestManagementQueryInput 固定字面搜尋的裁切、Unicode 長度及陣列／重複參數錯誤。
func TestManagementQueryInput(t *testing.T) {
	cases := []struct {
		name, query, wantKeyword, wantField, wantMessage string
	}{
		{"literal symbols", "page=1&per_page=20&filters%5Bkeyword%5D=+%25_%5C%C9%99+", "%_\\ə", "", ""},
		{"short keyword", "page=1&per_page=20&filters%5Bkeyword%5D=A", "A", "", ""},
		{"spaces only", "page=1&per_page=20&filters%5Bkeyword%5D=++", "", "", ""},
		{"tab retained", "page=1&per_page=20&filters%5Bkeyword%5D=+%09+", "\t", "", ""},
		{"NUL rejected", "page=1&per_page=20&filters%5Bkeyword%5D=%00", "", "filters.keyword", invalid},
		{"unicode too long", "page=1&per_page=20&filters%5Bkeyword%5D=" + strings.Repeat("%E4%B8%AD", 255), "", "filters.keyword", invalid},
		{"keyword array", "page=1&per_page=20&filters%5Bkeyword%5D%5B0%5D=x", "", "filters.keyword", wrongType},
		{"keyword duplicate", "page=1&per_page=20&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y", "", "filters.keyword", "此參數不可重複"},
	}
	for _, test := range cases {
		// 原始 Query 保留 URL 編碼與重複鍵，避免測試端先替服務解析掉差異。
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/api/members?"+test.query, nil)
			_, _, keyword, e := pageInput(r, "keyword")
			if test.wantField == "" {
				if e != nil || keyword != test.wantKeyword {
					t.Fatalf("keyword=%q error=%v, want %q", keyword, e, test.wantKeyword)
				}
				return
			}
			if e == nil || len(e.fields[test.wantField]) != 1 || e.fields[test.wantField][0] != test.wantMessage {
				t.Fatalf("error=%v, want %s=%q", e, test.wantField, test.wantMessage)
			}
		})
	}
	if asciiFold("AZ中Éİ%_\\") != "az中Éİ%_\\" {
		t.Fatal("ASCII folding changed non-ASCII or literal symbols")
	}
}

// TestAdministrationBodyInput 固定狀態與設定的 JSON 型態、範圍及多錯欄彙整。
func TestAdministrationBodyInput(t *testing.T) {
	cases := []struct {
		name, raw, field, want string
		fields                 []string
	}{
		{"status valid", `{"status":"disabled"}`, "", "", []string{"status"}},
		{"status invalid", `{"status":"pending"}`, "status", invalid, []string{"status"}},
		{"settings valid", `{"enabled":false,"window_minutes":60,"failure_threshold":100,"lock_seconds":86400}`, "", "", []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"boolean string", `{"enabled":"false","window_minutes":5,"failure_threshold":5,"lock_seconds":300}`, "enabled", wrongType, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"numeric string", `{"enabled":true,"window_minutes":"5","failure_threshold":5,"lock_seconds":300}`, "window_minutes", wrongType, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"numeric exponent", `{"enabled":true,"window_minutes":1e0,"failure_threshold":5,"lock_seconds":300}`, "window_minutes", wrongType, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"numeric overflow", `{"enabled":true,"window_minutes":999999999999999999999999,"failure_threshold":5,"lock_seconds":300}`, "window_minutes", invalid, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"numeric range", `{"enabled":true,"window_minutes":0,"failure_threshold":5,"lock_seconds":300}`, "window_minutes", invalid, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"explicit null", `{"enabled":null,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}`, "enabled", nullValue, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"missing enabled", `{"window_minutes":5,"failure_threshold":5,"lock_seconds":300}`, "enabled", required, []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
		{"unknown settings field", `{"enabled":true,"window_minutes":5,"failure_threshold":5,"lock_seconds":300,"extra":1}`, "extra", "不支援此欄位", []string{"enabled", "window_minutes", "failure_threshold", "lock_seconds"}},
	}
	for _, test := range cases {
		// 直接送原始 JSON，才能證明整數詞法不會被測試客戶端轉型。
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest("PUT", "/", strings.NewReader(test.raw))
			r.Header.Set("Content-Type", "application/json")
			_, e := bodyInput(r, test.fields, false)
			if test.field == "" {
				if e != nil {
					t.Fatalf("expected valid body, got %v", e)
				}
				return
			}
			if e == nil || len(e.fields[test.field]) != 1 || e.fields[test.field][0] != test.want {
				t.Fatalf("error=%v, want %s=%q", e, test.field, test.want)
			}
		})
	}
}

// TestAdministrationRoutePriority 驗證六個管理路由與文章管理範圍先認證，再處理格式或目標。
func TestAdministrationRoutePriority(t *testing.T) {
	app := newApp(&server{})
	cases := []struct {
		method, target, body string
	}{
		{"GET", "/api/members?page=bad", ""},
		{"GET", "/api/members/no-id?unknown=1", "x"},
		{"PUT", "/api/members/no-id/status", `{"status":"invalid"}`},
		{"GET", "/api/comments?page=bad", ""},
		{"GET", "/api/settings/login-lockout?unknown=1", "x"},
		{"PUT", "/api/settings/login-lockout", `{"enabled":"false"}`},
		{"GET", "/api/articles?page=bad&filters%5Bscope%5D=management", ""},
	}
	for _, test := range cases {
		// 使用無效憑證，避免資料庫依賴並確定錯誤格式不搶在身分查核之前。
		t.Run(test.method+" "+test.target, func(t *testing.T) {
			r := httptest.NewRequest(test.method, test.target, strings.NewReader(test.body))
			r.Header.Set("Authorization", "Bearer invalid")
			if test.body != "" {
				r.Header.Set("Content-Type", "application/json")
			}
			response, err := app.Test(r)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var result map[string]any
			if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != http.StatusUnauthorized || result["error_code"] != "E0002" {
				t.Fatalf("status=%d body=%v", response.StatusCode, result)
			}
		})
	}
}

// TestMemberKeywordValidation 區分實際 NUL 與字面跳脫文字，並固定長度、錯誤優先順序及跨欄位彙整。
func TestMemberKeywordValidation(t *testing.T) {
	const query = "page=1&per_page=20&filters%5Bkeyword%5D="
	cases := []struct {
		name, raw, wantKeyword string
		want                   map[string][]string
	}{
		{"NUL only", query + "%00", "", map[string][]string{"filters.keyword": {invalid}}},
		{"NUL start", query + "%00abc", "", map[string][]string{"filters.keyword": {invalid}}},
		{"NUL middle", query + "ab%00c", "", map[string][]string{"filters.keyword": {invalid}}},
		{"NUL end", query + "abc%00", "", map[string][]string{"filters.keyword": {invalid}}},
		{"NUL with spaces", query + "+%00+", "", map[string][]string{"filters.keyword": {invalid}}},
		{"literal escape", query + "%5Cu0000", `\u0000`, nil},
		{"254 non-BMP with spaces", query + "++" + strings.Repeat("%F0%9F%98%80", 254) + "++", strings.Repeat("😀", 254), nil},
		{"255 non-BMP with spaces", query + "++" + strings.Repeat("%F0%9F%98%80", 255) + "++", "", map[string][]string{"filters.keyword": {invalid}}},
		{"spaces only", query + "++", "", nil},
		{"tab retained", query + "+%09+", "\t", nil},
		{"invalid page and NUL", "page=bad&per_page=20&filters%5Bkeyword%5D=%00", "", map[string][]string{"page": {invalid}, "filters.keyword": {invalid}}},
		{"missing page and NUL", "per_page=20&filters%5Bkeyword%5D=%00", "", map[string][]string{"page": {required}, "filters.keyword": {invalid}}},
		{"NUL first duplicate", query + "%00&filters%5Bkeyword%5D=x", "", map[string][]string{"filters.keyword": {"此參數不可重複"}}},
		{"NUL last duplicate", query + "x&filters%5Bkeyword%5D=%00", "", map[string][]string{"filters.keyword": {"此參數不可重複"}}},
		{"NUL array", "page=1&per_page=20&filters%5Bkeyword%5D%5B0%5D=%00", "", map[string][]string{"filters.keyword": {wrongType}}},
	}
	for _, tc := range cases {
		// 保留原始百分比編碼，讓解碼、優先順序與值驗證都走端點使用的接點。
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/api/members?"+tc.raw, nil)
			page, perPage, keyword, e := pageInput(r, "keyword")
			if tc.want == nil {
				if e != nil || page != 1 || perPage != 20 || keyword != tc.wantKeyword {
					t.Fatalf("got (%d,%d,%q,%v), want (1,20,%q,nil)", page, perPage, keyword, e, tc.wantKeyword)
				}
			} else if e == nil || e.code != "E0001" || !reflect.DeepEqual(e.fields, tc.want) {
				t.Fatalf("query=%q error=%+v, want E0001 fields=%v", tc.raw, e, tc.want)
			}
		})
	}
	// GET 本文先於 Query 拒絕，不能因新增 NUL 驗證而多回關鍵字錯誤。
	r := httptest.NewRequest("GET", "/api/members?"+query+"%00", strings.NewReader("x"))
	_, _, _, e := pageInput(r, "keyword")
	if e == nil || e.code != "E0001" || !reflect.DeepEqual(e.fields, map[string][]string{"_body": {"此 API 不接受 Request Body"}}) {
		t.Fatalf("GET body error=%+v, want only _body error", e)
	}
}
