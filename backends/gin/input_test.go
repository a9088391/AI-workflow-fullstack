package main

import (
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

// TestQueryIdentityAndLabels 區分參數身分與錯誤顯示名稱，避免不同輸入被誤判重複或覆寫首個錯誤。
func TestQueryIdentityAndLabels(t *testing.T) {
	// 已支援欄位的陣列仍映射同一 scalar；未知陣列鍵則保留原身分與名稱。
	cases := []struct {
		name, query string
		allowed     map[string]bool
		want        map[string][]string
	}{
		{"invalid bytes and literal escape", "%FF=x&%25FF=y", nil, map[string][]string{"%FF": {"不支援此欄位"}}},
		{"valid replacement character", "%EF%BF%BD=x", nil, map[string][]string{"�": {"不支援此欄位"}}},
		{"distinct unknown array keys", "page%5B0%5D=x&page%5B1%5D=y", nil, map[string][]string{"page[0]": {"不支援此欄位"}, "page[1]": {"不支援此欄位"}}},
		{"true invalid byte duplicate", "%FF=x&%ff=y&%25FF=z", nil, map[string][]string{"%FF": {"此參數不可重複"}}},
		{"invalid bytes preserve ASCII name", "page%5b%ff%5d=x", nil, map[string][]string{"page%5B%FF%5D": {"不支援此欄位"}}},
		{"literal escape preserves case", "%ff=x&%25ff=y", nil, map[string][]string{"%FF": {"不支援此欄位"}, "%ff": {"不支援此欄位"}}},
		{"first error survives label collision", "filters%5Bscope%5D%5B%5D=x&filters.scope=y", map[string]bool{"filters[scope]": true}, map[string][]string{"filters.scope": {wrongType}}},
		{"duplicate survives label collision", "filters%5Bscope%5D=management&filters%5Bscope%5D=management&filters.scope=y", map[string]bool{"filters[scope]": true}, map[string][]string{"filters.scope": {"此參數不可重複"}}},
		{"distinct allowed array keys", "page%5B0%5D=x&page%5B1%5D=y", map[string]bool{"page": true}, map[string][]string{"page": {"此參數不可重複"}}},
		{"true allowed duplicate", "page=1&%70age=2", map[string]bool{"page": true}, map[string][]string{"page": {"此參數不可重複"}}},
	}
	for _, tc := range cases {
		// 保留原始百分比編碼，讓案例真正經過 Query 解碼而非預先正規化。
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/?"+tc.query, nil)
			_, got := oneQuery(r, tc.allowed)
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("query %q: want %v, got %v", tc.query, tc.want, got)
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
