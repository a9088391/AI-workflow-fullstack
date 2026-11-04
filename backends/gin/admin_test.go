package main

import (
	"net/http/httptest"
	"strings"
	"testing"
)

// TestAdminInput 固定管理搜尋及設定寫入最易與其他後端分歧的原始輸入語意。
func TestAdminInput(t *testing.T) {
	// 文章管理範圍須精確相符；關鍵字的 U+0020 trim 不可套到 scope。
	for _, tc := range []struct{ raw, wantScope, wantError string }{
		{"management", "management", ""},
		{"%20management%20", "", invalid},
	} {
		r := httptest.NewRequest("GET", "/?page=1&per_page=20&filters%5Bscope%5D="+tc.raw, nil)
		_, _, scope, e := pageInput(r, "scope")
		if tc.wantError == "" && (e != nil || scope != tc.wantScope) || tc.wantError != "" && (e == nil || e.fields["filters.scope"][0] != tc.wantError) {
			t.Fatalf("scope %q: want %q / %q, got %q / %v", tc.raw, tc.wantScope, tc.wantError, scope, e)
		}
	}
	// 關鍵字只裁 U+0020，陣列與重複欄位必須回精確的欄位錯誤。
	for _, tc := range []struct{ query, wantKeyword, field, message string }{
		{"page=1&per_page=20&filters%5Bkeyword%5D=+A%25_%5C+", "A%_\\", "", ""},
		{"page=1&per_page=20&filters%5Bkeyword%5D=" + strings.Repeat("a", 254), strings.Repeat("a", 254), "", ""},
		{"page=1&per_page=20&filters%5Bkeyword%5D=%00", "", "filters.keyword", invalid},
		{"page=1&per_page=20&filters%5Bkeyword%5D%5B%5D=x", "", "filters.keyword", wrongType},
		{"page=1&per_page=20&filters%5Bkeyword%5D=a&filters%5Bkeyword%5D=b", "", "filters.keyword", "此參數不可重複"},
		{"page=1&per_page=20&filters%5Bkeyword%5D=" + strings.Repeat("a", 255), "", "filters.keyword", invalid},
	} {
		r := httptest.NewRequest("GET", "/?"+tc.query, nil)
		_, _, keyword, e := pageInput(r, "keyword")
		if tc.field == "" && (e != nil || keyword != tc.wantKeyword) {
			t.Fatalf("query %q: keyword=%q error=%v", tc.query, keyword, e)
		}
		if tc.field != "" && (e == nil || e.fields[tc.field][0] != tc.message) {
			t.Fatalf("query %q: expected %s=%q, got %v", tc.query, tc.field, tc.message, e)
		}
	}
	// JSON 最後同名鍵決定型態；超大整數屬範圍錯誤，小數與指數屬型態錯誤。
	for _, tc := range []struct{ raw, field, message string }{
		{`{"enabled":true,"enabled":false,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}`, "", ""},
		{`{"enabled":true,"window_minutes":5,"failure_threshold":5,"lock_seconds":300,"extra":1}`, "extra", "不支援此欄位"},
		{`{"enabled":true,"window_minutes":5,"failure_threshold":5}`, "lock_seconds", required},
		{`{"enabled":true,"window_minutes":9007199254740992,"failure_threshold":5,"lock_seconds":300}`, "window_minutes", invalid},
		{`{"enabled":true,"window_minutes":1e0,"failure_threshold":5,"lock_seconds":300}`, "window_minutes", wrongType},
		{`{"enabled":null,"window_minutes":5,"failure_threshold":5,"lock_seconds":300}`, "enabled", nullValue},
	} {
		r := httptest.NewRequest("PUT", "/", strings.NewReader(tc.raw))
		r.Header.Set("Content-Type", "application/json")
		values, e := settingsInput(r)
		if tc.field == "" && (e != nil || values.Enabled) {
			t.Fatalf("body %q: values=%+v error=%v", tc.raw, values, e)
		}
		if tc.field != "" && (e == nil || e.fields[tc.field][0] != tc.message) {
			t.Fatalf("body %q: expected %s=%q, got %v", tc.raw, tc.field, tc.message, e)
		}
	}
	// 狀態欄位沿用既有 Body 解析，但只能接受契約中的兩個狀態。
	for _, tc := range []struct{ value, want string }{{"active", ""}, {"disabled", ""}, {"other", invalid}} {
		r := httptest.NewRequest("PUT", "/", strings.NewReader(`{"status":"`+tc.value+`"}`))
		r.Header.Set("Content-Type", "application/json")
		_, e := bodyInput(r, []string{"status"}, false)
		if tc.want == "" && e != nil || tc.want != "" && (e == nil || e.fields["status"][0] != tc.want) {
			t.Fatalf("status %q: expected %q, got %v", tc.value, tc.want, e)
		}
	}
}
