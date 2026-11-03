package main

import (
	"net/http/httptest"
	"reflect"
	"testing"
)

// TestQueryErrors 保留原始 Query 比對完整錯誤欄位，防止顯示名稱碰撞改變參數身分。
func TestQueryErrors(t *testing.T) {
	cases := []struct {
		name, query, filter string
		list                bool
		want                map[string][]string
	}{
		{"unknown filter", "filters%5Bunknown%5D=x", "", false, map[string][]string{"filters.unknown": {"不支援此欄位"}}},
		{"public unknown scope filter shape", "page=1&per_page=20&filters%5Bunexpected%5D=1", "scope", true, map[string][]string{"filters.unexpected": {"不支援此欄位"}}},
		{"public unknown keyword filter shape", "page=1&per_page=20&filters%5Bunexpected%5D=1", "keyword", true, map[string][]string{"filters.unexpected": {"不支援此欄位"}}},
		{"unknown filter dot identity distinct", "filters%5Bunknown%5D=x&filters.unknown=y", "", false, map[string][]string{"filters.unknown": {"不支援此欄位"}}},
		{"unknown brackets", "odd%5Bx%5D=x&oddx=y", "", false, map[string][]string{"odd[x]": {"不支援此欄位"}, "oddx": {"不支援此欄位"}}},
		{"unknown nested arrays", "filters%5Bunknown%5D%5Bx%5D=x&filters%5Bunknown%5D%5By%5D=y", "", false, map[string][]string{"filters[unknown][x]": {"不支援此欄位"}, "filters[unknown][y]": {"不支援此欄位"}}},
		{"unsupported scope arrays", "filters%5Bscope%5D%5Bx%5D=x&filters%5Bscope%5D%5By%5D=y", "", false, map[string][]string{"filters[scope][x]": {"不支援此欄位"}, "filters[scope][y]": {"不支援此欄位"}}},
		{"unsupported page arrays", "page%5Bx%5D=x&page%5By%5D=y", "", false, map[string][]string{"page[x]": {"不支援此欄位"}, "page[y]": {"不支援此欄位"}}},
		{"unknown true duplicate", "filters%5Bunknown%5D=x&filters%5Bunknown%5D=y", "", false, map[string][]string{"filters.unknown": {"此參數不可重複"}}},
		{"invalid UTF8 and literal percent", "%FF=x&%25FF=y", "", false, map[string][]string{"%FF": {"不支援此欄位"}}},
		{"literal percent and invalid UTF8", "%25FF=x&%FF=y", "", false, map[string][]string{"%FF": {"不支援此欄位"}}},
		{"invalid UTF8 escape case duplicate", "%FF=x&%ff=y", "", false, map[string][]string{"%FF": {"此參數不可重複"}}},
		{"invalid UTF8 exact duplicate", "%ff=x&%ff=y", "", false, map[string][]string{"%FF": {"此參數不可重複"}}},
		{"invalid UTF8 duplicate after collision", "%FF=x&%25FF=y&%ff=z", "", false, map[string][]string{"%FF": {"此參數不可重複"}}},
		{"literal percent duplicate after collision", "%25FF=x&%FF=y&%25FF=z", "", false, map[string][]string{"%FF": {"此參數不可重複"}}},
		{"replacement character distinct", "%FF=x&%EF%BF%BD=y", "", false, map[string][]string{"%FF": {"不支援此欄位"}, "�": {"不支援此欄位"}}},
		{"ASCII case remains distinct", "x%FF=x&X%ff=y", "", false, map[string][]string{"x%FF": {"不支援此欄位"}, "X%FF": {"不支援此欄位"}}},
		{"encoded ordinary duplicate", "other=x&%6Fther=y", "", false, map[string][]string{"other": {"此參數不可重複"}}},
		{"scope literal dot collision first", "page=1&per_page=20&filters.scope=x&filters%5Bscope%5D=bad", "scope", true, map[string][]string{"filters.scope": {"不支援此欄位"}}},
		{"scope literal dot collision last", "page=1&per_page=20&filters%5Bscope%5D=management&filters.scope=x", "scope", true, map[string][]string{"filters.scope": {"不支援此欄位"}}},
		{"scope array before dot", "page=1&per_page=20&filters%5Bscope%5D%5Bx%5D=x&filters.scope=y", "scope", true, map[string][]string{"filters.scope": {wrongType}}},
		{"dot before scope array", "page=1&per_page=20&filters.scope=y&filters%5Bscope%5D%5Bx%5D=x", "scope", true, map[string][]string{"filters.scope": {"不支援此欄位"}}},
		{"scope scalar array duplicate", "page=1&per_page=20&filters%5Bscope%5D=management&filters%5Bscope%5D%5Bx%5D=x", "scope", true, map[string][]string{"filters.scope": {"此參數不可重複"}}},
		{"page scalar array duplicate", "page=1&page%5Bx%5D=x&per_page=20", "", true, map[string][]string{"page": {"此參數不可重複"}}},
		{"page array scalar duplicate", "page%5Bx%5D=x&page=1&per_page=20", "", true, map[string][]string{"page": {"此參數不可重複"}}},
		{"page array type", "page%5Bx%5D=x&per_page=20", "", true, map[string][]string{"page": {wrongType}}},
		{"keyword scalar array duplicate", "page=1&per_page=20&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D%5By%5D=y", "keyword", true, map[string][]string{"filters.keyword": {"此參數不可重複"}}},
		{"keyword array before dot", "page=1&per_page=20&filters%5Bkeyword%5D%5Bx%5D=x&filters.keyword=y", "keyword", true, map[string][]string{"filters.keyword": {wrongType}}},
		{"dot before keyword array", "page=1&per_page=20&filters.keyword=y&filters%5Bkeyword%5D%5Bx%5D=x", "keyword", true, map[string][]string{"filters.keyword": {"不支援此欄位"}}},
		{"keyword unknown nested scope", "page=1&per_page=20&filters%5Bscope%5D%5Bx%5D=x&filters%5Bscope%5D%5By%5D=y", "keyword", true, map[string][]string{"filters[scope][x]": {"不支援此欄位"}, "filters[scope][y]": {"不支援此欄位"}}},
		{"independent errors aggregate", "page=bad&per_page=101&filters%5Bunknown%5D=x", "", true, map[string][]string{"page": {invalid}, "per_page": {invalid}, "filters.unknown": {"不支援此欄位"}}},
		{"duplicate wins over invalid value", "page=bad&page=0&per_page=20", "", true, map[string][]string{"page": {"此參數不可重複"}}},
		{"invalid UTF8 value", "page=%FF&per_page=20", "", true, map[string][]string{"page": {invalid}}},
	}
	for _, test := range cases {
		// Request 保留百分比編碼與順序，避免測試端先解碼而掩蓋原始鍵差異。
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/?"+test.query, nil)
			var e *apiError
			if test.list {
				_, _, _, e = pageInput(r, test.filter)
			} else {
				e = noQuery(r)
			}
			if e == nil || e.code != "E0001" || !reflect.DeepEqual(e.fields, test.want) {
				t.Fatalf("query=%q error=%+v, want E0001 fields=%v", test.query, e, test.want)
			}
		})
	}
}

// TestPageQueryValues 確認錯誤身分修正保留合法分頁、篩選與原始搜尋文字的處理。
func TestPageQueryValues(t *testing.T) {
	cases := []struct {
		name, query, filter, want string
		page, perPage             int64
	}{
		{"leading zeros", "page=0002&per_page=0100", "", "", 2, 100},
		{"maximum page", "page=9007199254740991&per_page=1", "", "", 9007199254740991, 1},
		{"management scope", "page=1&per_page=20&filters%5Bscope%5D=management", "scope", "management", 1, 20},
		{"literal keyword", "page=1&per_page=20&filters%5Bkeyword%5D=+%25_%5C%E4%B8%AD+", "keyword", "%_\\中", 1, 20},
		{"replacement character keyword", "page=1&per_page=20&filters%5Bkeyword%5D=%EF%BF%BD", "keyword", "�", 1, 20},
	}
	for _, test := range cases {
		// 由端點的解析接點檢查回傳值，不依賴 oneQuery 的內部 map 排列。
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/?"+test.query, nil)
			page, perPage, value, e := pageInput(r, test.filter)
			if e != nil || page != test.page || perPage != test.perPage || value != test.want {
				t.Fatalf("got (%d,%d,%q,%v), want (%d,%d,%q,nil)", page, perPage, value, e, test.page, test.perPage, test.want)
			}
		})
	}
}
