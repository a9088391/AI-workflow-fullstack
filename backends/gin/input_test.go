package main

import (
	"net/http/httptest"
	"reflect"
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
