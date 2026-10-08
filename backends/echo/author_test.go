package main

import (
	"database/sql"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestContentBodyInput 固定跨框架易分歧的 JSON 整數型態、文字長度與空白規則。
func TestContentBodyInput(t *testing.T) {
	cases := []struct {
		name, raw, field, want string
		fields                 []string
	}{
		{"article body may exceed comment limit", `{"title":"標題","body":"` + strings.Repeat("a", 2001) + `"}`, "", "", []string{"title", "body"}},
		{"comment body limit", `{"body":"` + strings.Repeat("a", 2001) + `"}`, "body", invalid, []string{"body"}},
		{"unicode whitespace", `{"body":"\u3000\u00a0"}`, "body", invalid, []string{"body"}},
		{"NUL", `{"body":"a\u0000b"}`, "body", invalid, []string{"body"}},
		{"version string", `{"body":"a","version":"1"}`, "version", wrongType, []string{"body", "version"}},
		{"version exponent", `{"body":"a","version":1e0}`, "version", wrongType, []string{"body", "version"}},
		{"version negative", `{"body":"a","version":-1}`, "version", invalid, []string{"body", "version"}},
		{"version zero", `{"body":"a","version":0}`, "version", invalid, []string{"body", "version"}},
		{"version overflow", `{"body":"a","version":9007199254740992}`, "version", invalid, []string{"body", "version"}},
		{"version maximum and CRLF", `{"body":"a\r\nb","version":9007199254740991}`, "", "", []string{"body", "version"}},
	}
	for _, test := range cases {
		// 每個原始 JSON 保留數字詞法及 Unicode escape，避免測試客戶端替換輸入。
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest("PUT", "/", strings.NewReader(test.raw))
			r.Header.Set("Content-Type", "application/json")
			values, e := bodyInput(r, test.fields, false)
			if test.field == "" {
				if e != nil || values == nil {
					t.Fatalf("expected valid body, got values=%v error=%v", values, e)
				}
				return
			}
			if e == nil || e.code != "E0001" || len(e.fields[test.field]) != 1 || e.fields[test.field][0] != test.want {
				t.Fatalf("expected %s=%q, got values=%v error=%v", test.field, test.want, values, e)
			}
		})
	}
}

// TestContentAccessAndRestoreControl 固定隱藏 404、可讀 403 與管理員接管後的恢復權。
func TestContentAccessAndRestoreControl(t *testing.T) {
	member := &account{ID: 1, Role: "member"}
	other := &account{ID: 2, Role: "member"}
	admin := &account{ID: 3, Role: "admin"}
	row := content{AuthorID: 1, Visibility: "hidden", Restore: sql.NullString{String: "author", Valid: true}}
	// 隱藏資料先阻擋其他會員；管理員能讀，卻不能替他人改寫內文。
	if e := contentAccess(row, "visible", other, true, false); e == nil || e.code != "E0004" {
		t.Fatalf("hidden article must be 404, got %v", e)
	}
	if e := contentAccess(row, "visible", admin, true, false); e == nil || e.code != "E0003" {
		t.Fatalf("admin editing other's article must be 403, got %v", e)
	}
	if e := contentAccess(row, "hidden", member, false, false); e == nil || e.code != "E0003" {
		t.Fatalf("editing comment below hidden article must be 403, got %v", e)
	}
	if e := contentAccess(row, "hidden", member, false, true); e != nil {
		t.Fatalf("own comment visibility remains operable, got %v", e)
	}
	// 接管是實際恢復權變更；作者再次下架不能把 admin 權限改回 author。
	control, e := visibilityControl(row, admin, "hidden")
	if e != nil || control != "admin" {
		t.Fatalf("expected admin takeover, got %q %v", control, e)
	}
	row.Restore.String = control
	control, e = visibilityControl(row, member, "hidden")
	if e != nil || control != "admin" {
		t.Fatalf("author must preserve admin control, got %q %v", control, e)
	}
	if _, e = visibilityControl(row, member, "visible"); e == nil || e.code != "E0003" {
		t.Fatalf("author cannot restore admin-hidden content, got %v", e)
	}
}
