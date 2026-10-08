package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

const invalid = "欄位格式或範圍不正確"
const required = "此欄位為必填"
const nullValue = "此欄位不可為 null"
const wrongType = "欄位型態不正確"

var emailPattern = regexp.MustCompile(`^[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$`)
var passwordPattern = regexp.MustCompile(`^[A-Za-z0-9]{4,16}$`)
var decimalPattern = regexp.MustCompile(`^[0-9]+$`)
var jsonIntegerPattern = regexp.MustCompile(`^-?[0-9]+$`)

// apiError 保存固定錯誤碼及欄位細節，讓各端點共用相同回應。
type apiError struct {
	code   string
	fields map[string][]string
	retry  int
	status int
}

// fieldError 建立驗證錯誤，避免框架預設文案流入共同 API。
func fieldError(name, message string) *apiError {
	return &apiError{code: "E0001", fields: map[string][]string{name: {message}}}
}

// oneQuery 逐個解析原始參數，以保留重複及陣列鍵的驗證資訊。
func oneQuery(r *http.Request, allowed map[string]bool) (map[string]string, map[string][]string) {
	values, errors := map[string]string{}, map[string][]string{}
	if r.URL.RawQuery == "" {
		return values, errors
	}
	for _, pair := range strings.Split(r.URL.RawQuery, "&") {
		// 原始 Query 保留重複次數與陣列鍵，錯誤欄位改成 API15 的名稱。
		parts := strings.SplitN(pair, "=", 2)
		name, err := url.QueryUnescape(parts[0])
		if err != nil || !utf8.ValidString(name) {
			name = parts[0]
		}
		canonical := name
		for _, key := range []string{"page", "per_page", "filters[scope]", "filters[keyword]"} {
			if strings.HasPrefix(name, key+"[") {
				canonical = key
				break
			}
		}
		label := strings.ReplaceAll(strings.ReplaceAll(canonical, "filters[", "filters."), "]", "")
		_, seen := values[canonical]
		switch {
		case seen:
			errors[label] = []string{"此參數不可重複"}
		case canonical != name && allowed[canonical]:
			errors[label] = []string{wrongType}
		case !allowed[canonical]:
			errors[label] = []string{"不支援此欄位"}
		}
		raw := ""
		if len(parts) == 2 {
			raw = parts[1]
		}
		value, err := url.QueryUnescape(raw)
		if err != nil || !utf8.ValidString(value) {
			value = ""
			if allowed[canonical] && len(errors[label]) == 0 {
				errors[label] = []string{invalid}
			}
		}
		values[canonical] = value
	}
	return values, errors
}

// noQuery 拒絕未列在契約的參數，避免默默忽略呼叫端錯誤。
func noQuery(r *http.Request) *apiError {
	_, errors := oneQuery(r, map[string]bool{})
	if len(errors) > 0 {
		return &apiError{code: "E0001", fields: errors}
	}
	return nil
}

// noGetBody 先拒絕 GET 的非空本文，維持 API14 的錯誤順序。
func noGetBody(r *http.Request) *apiError {
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 1))
	if len(raw) > 0 {
		return fieldError("_body", "此 API 不接受 Request Body")
	}
	return nil
}

// urlID 只接受安全整數範圍內的 ASCII 十進位識別碼。
func urlID(value, name string) (int64, *apiError) {
	if !decimalPattern.MatchString(value) {
		return 0, fieldError(name, invalid)
	}
	digits := strings.TrimLeft(value, "0")
	if digits == "" || len(digits) > 16 || len(digits) == 16 && digits > "9007199254740991" {
		return 0, fieldError(name, invalid)
	}
	n, _ := strconv.ParseInt(digits, 10, 64)
	return n, nil
}

// pageInput 收集列表 Query 錯誤；文章可用 scope，會員可用 keyword，其餘列表不接受篩選。
func pageInput(r *http.Request, filter string) (int64, int64, string, *apiError) {
	if e := noGetBody(r); e != nil {
		return 0, 0, "", e
	}
	allowed := map[string]bool{"page": true, "per_page": true}
	if filter != "" {
		allowed["filters["+filter+"]"] = true
	}
	values, errors := oneQuery(r, allowed)
	numbers := map[string]int64{}
	for _, name := range []string{"page", "per_page"} {
		value, ok := values[name]
		if !ok {
			errors[name] = []string{required}
		} else if len(errors[name]) == 0 {
			n, e := urlID(value, name)
			if e != nil || name == "per_page" && n > 100 {
				errors[name] = []string{invalid}
			} else {
				numbers[name] = n
			}
		}
	}
	if value, ok := values["filters[scope]"]; ok && len(errors["filters.scope"]) == 0 && value != "management" {
		errors["filters.scope"] = []string{invalid}
	}
	if value, ok := values["filters[keyword]"]; ok && len(errors["filters.keyword"]) == 0 {
		// 會員搜尋只去除 U+0020，保留其他 Unicode 與字面符號。
		value = strings.Trim(value, " ")
		if utf8.RuneCountInString(value) > 254 {
			errors["filters.keyword"] = []string{invalid}
		}
		values["filters[keyword]"] = value
	}
	if len(errors) > 0 {
		return 0, 0, "", &apiError{code: "E0001", fields: errors}
	}
	return numbers["page"], numbers["per_page"], values["filters["+filter+"]"], nil
}

// validSurrogates 拒絕未配對的 JSON UTF-16 escape，避免悄悄改成替代字元。
func validSurrogates(raw []byte) bool {
	for i := 0; i < len(raw); {
		if raw[i] != '\\' {
			i++
			continue
		}
		if i+1 >= len(raw) {
			return false
		}
		if raw[i+1] != 'u' {
			i += 2
			continue
		}
		if i+6 > len(raw) {
			return false
		}
		value, err := strconv.ParseUint(string(raw[i+2:i+6]), 16, 16)
		if err != nil {
			return false
		}
		if value >= 0xd800 && value <= 0xdbff {
			if i+12 > len(raw) || raw[i+6] != '\\' || raw[i+7] != 'u' {
				return false
			}
			low, err := strconv.ParseUint(string(raw[i+8:i+12]), 16, 16)
			if err != nil || low < 0xdc00 || low > 0xdfff {
				return false
			}
			i += 12
			continue
		}
		if value >= 0xdc00 && value <= 0xdfff {
			return false
		}
		i += 6
	}
	return true
}

// bodyInput 限制大小並逐鍵解析 JSON，同名鍵依最後值驗證以維持跨後端一致。
func bodyInput(r *http.Request, fields []string, optionalEmpty bool) (map[string]string, *apiError) {
	// 先限制大小、UTF-8 與 JSON 結構；解析失敗不可留下部分欄位。
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20+1))
	if err != nil {
		return nil, &apiError{code: "E0005"}
	}
	if len(raw) > 1<<20 {
		return nil, &apiError{code: "E0005"}
	}
	if !utf8.Valid(raw) || !validSurrogates(raw) {
		return nil, &apiError{code: "E0005"}
	}
	if optionalEmpty && len(raw) == 0 {
		return map[string]string{}, nil
	}
	if len(raw) == 0 || strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])) != "application/json" {
		return nil, fieldError("_body", "Request Body 必須為 JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil {
		return nil, &apiError{code: "E0005"}
	}
	if token != json.Delim('{') {
		if json.Valid(raw) {
			return nil, fieldError("_body", "Request Body 必須為 JSON object")
		}
		return nil, &apiError{code: "E0005"}
	}
	allowed := map[string]bool{}
	for _, field := range fields {
		allowed[field] = true
	}
	values := map[string]string{}
	present := map[string]bool{}
	errors := map[string][]string{}
	for decoder.More() {
		// JSON object 同名鍵沿用其他後端的最後值，且只以最後值決定欄位錯誤。
		keyToken, err := decoder.Token()
		if err != nil {
			return nil, &apiError{code: "E0005"}
		}
		key := keyToken.(string)
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, &apiError{code: "E0005"}
		}
		present[key] = true
		delete(errors, key)
		delete(values, key)
		if !allowed[key] {
			errors[key] = []string{"不支援此欄位"}
			continue
		}
		if bytes.Equal(value, []byte("null")) {
			errors[key] = []string{nullValue}
			continue
		}
		if key == "enabled" {
			// 設定開關必須保留 JSON boolean 型態，不能接受字串。
			if bytes.Equal(value, []byte("true")) || bytes.Equal(value, []byte("false")) {
				values[key] = string(value)
			} else {
				errors[key] = []string{wrongType}
			}
			continue
		}
		if key == "version" || key == "window_minutes" || key == "failure_threshold" || key == "lock_seconds" {
			// JSON 整數保留詞法；超大整數是範圍錯誤，小數與字串是型態錯誤。
			if !jsonIntegerPattern.Match(value) {
				errors[key] = []string{wrongType}
			} else {
				values[key] = string(value)
			}
			continue
		}
		if len(value) == 0 || value[0] != '"' {
			errors[key] = []string{wrongType}
			continue
		}
		var stringValue string
		if err := json.Unmarshal(value, &stringValue); err != nil {
			return nil, &apiError{code: "E0005"}
		}
		values[key] = stringValue
	}
	if _, err = decoder.Token(); err != nil {
		return nil, &apiError{code: "E0005"}
	}
	if _, err = decoder.Token(); err != io.EOF {
		return nil, &apiError{code: "E0005"}
	}
	for _, field := range fields {
		if !present[field] {
			errors[field] = []string{required}
		}
	}
	for _, field := range fields {
		if len(errors[field]) > 0 {
			continue
		}
		// 格式驗證對所有型態正確欄位執行，彙整成一次 422 回應。
		value := values[field]
		switch field {
		case "email":
			value = strings.Trim(value, " ")
			parts := strings.Split(value, "@")
			// 先以原字元驗證 ASCII 格式，避免 Unicode 小寫把非 ASCII 地址變成合法帳號。
			if len(value) > 254 || len(parts) != 2 || len(parts[0]) > 64 || !emailPattern.MatchString(value) {
				errors[field] = []string{invalid}
			} else {
				values[field] = strings.ToLower(value)
			}
		case "password":
			if !passwordPattern.MatchString(value) {
				errors[field] = []string{invalid}
			}
		case "nickname":
			if utf8.RuneCountInString(value) < 1 || utf8.RuneCountInString(value) > 30 || strings.ContainsRune(value, 0) || allSpace(value) {
				errors[field] = []string{invalid}
			}
		case "title", "body":
			// 有標題的是文章；留言內文有較短上限，兩者都保留原始文字。
			limit := 2000
			if field == "title" {
				limit = 200
			} else {
				for _, candidate := range fields {
					if candidate == "title" {
						limit = 20000
						break
					}
				}
			}
			length := utf8.RuneCountInString(value)
			if length < 1 || length > limit || strings.ContainsRune(value, 0) || allSpace(value) {
				errors[field] = []string{invalid}
			}
		case "visibility":
			if value != "visible" && value != "hidden" {
				errors[field] = []string{invalid}
			}
		case "version":
			if _, e := urlID(value, field); e != nil {
				errors[field] = []string{invalid}
			}
		case "status":
			if value != "active" && value != "disabled" {
				errors[field] = []string{invalid}
			}
		case "window_minutes", "failure_threshold", "lock_seconds":
			limit := int64(86400)
			if field == "window_minutes" {
				limit = 60
			} else if field == "failure_threshold" {
				limit = 100
			}
			n, err := strconv.ParseInt(value, 10, 64)
			if err != nil || n < 1 || n > limit {
				errors[field] = []string{invalid}
			}
		}
	}
	if len(errors) > 0 {
		return nil, &apiError{code: "E0001", fields: errors}
	}
	return values, nil
}

// allSpace 比對已核准的 Unicode 空白集合，避免把其他文字裁掉。
func allSpace(value string) bool {
	for _, r := range value {
		if !(r >= 9 && r <= 13 || r == 0x20 || r == 0x85 || r == 0xa0 || r == 0x1680 || r >= 0x2000 && r <= 0x200a || r == 0x2028 || r == 0x2029 || r == 0x202f || r == 0x205f || r == 0x3000) {
			return false
		}
	}
	return true
}
