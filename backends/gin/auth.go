package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/argon2"
)

const backend = "gin"
const issuer = "ai-workflow-fullstack:gin"
const audience = "ai-workflow-fullstack-api:gin"
const duration = 28800

var phcPattern = regexp.MustCompile(`^\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+/]{22})\$([A-Za-z0-9+/]{43})$`)
var tokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
var taipei = time.FixedZone("Asia/Taipei", 8*3600)

// account 保留資料庫的目前角色與鎖定狀態，JWT 不持有這些授權欄位。
type account struct {
	ID                                  int64
	Email, Hash, Nickname, Role, Status string
	Epoch                               int64
	Locked                              sql.NullTime
	Created                             time.Time
}

// claims 僅保存本後端固定格式的短期身分與登入紀錄鍵。
type claims struct {
	Iss string `json:"iss"`
	Aud string `json:"aud"`
	Sub string `json:"sub"`
	JTI string `json:"jti"`
	Iat int64  `json:"iat"`
	Nbf int64  `json:"nbf"`
	Exp int64  `json:"exp"`
}

// businessTime 將 timestamptz 轉成台北秒級格式，維持共同回應契約。
func businessTime(t time.Time) string { return t.In(taipei).Format("2006-01-02 15:04:05") }

// accountData 限定可回傳的帳號欄位，避免洩漏密碼與鎖定內部值。
func accountData(a account) map[string]any {
	return map[string]any{"id": a.ID, "email": a.Email, "nickname": a.Nickname, "role": a.Role, "status": a.Status, "created_at": businessTime(a.Created)}
}

// randomBytes 使用作業系統安全亂數，供密碼鹽與每次登入 jti 使用。
func randomBytes(n int) ([]byte, error) { b := make([]byte, n); _, err := rand.Read(b); return b, err }

// passwordHash 寫入標準 Argon2id PHC，讓所有後端可驗證同一密碼。
func passwordHash(password string) (string, error) {
	salt, err := randomBytes(16)
	if err != nil {
		return "", err
	}
	hash := argon2.IDKey([]byte(password), salt, 2, 19456, 1, 32)
	return "$argon2id$v=19$m=19456,t=2,p=1$" + base64.RawStdEncoding.EncodeToString(salt) + "$" + base64.RawStdEncoding.EncodeToString(hash), nil
}

// verifyPassword 先限制 PHC 版本與成本，再以固定成本驗證跨後端雜湊。
func verifyPassword(password, encoded string) (bool, *apiError) {
	match := phcPattern.FindStringSubmatch(encoded)
	if match == nil {
		return false, &apiError{code: "E0104"}
	}
	salt, err := base64.RawStdEncoding.DecodeString(match[1])
	if err != nil || len(salt) != 16 || base64.RawStdEncoding.EncodeToString(salt) != match[1] {
		return false, &apiError{code: "E0104"}
	}
	expected, err := base64.RawStdEncoding.DecodeString(match[2])
	if err != nil || len(expected) != 32 || base64.RawStdEncoding.EncodeToString(expected) != match[2] {
		return false, &apiError{code: "E0104"}
	}
	actual := argon2.IDKey([]byte(password), salt, 2, 19456, 1, 32)
	return hmac.Equal(actual, expected), nil
}

// loadKey 要求既有 32-byte 秘密金鑰，缺失時服務無法進入就緒狀態。
func loadKey() ([]byte, error) {
	raw, err := os.ReadFile("/run/secrets/gin-jwt.key")
	if err != nil {
		return nil, fmt.Errorf("read Gin JWT key: %w", err)
	}
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil || len(key) != 32 {
		return nil, errors.New("Gin JWT key must be base64-encoded 32 bytes")
	}
	return key, nil
}

// issueToken 固定 HS256、issuer、audience 與八小時整數秒 claims。
func issueToken(key []byte, id int64, jti string, issued int64) (string, error) {
	head := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	payload, err := json.Marshal(claims{issuer, audience, strconv.FormatInt(id, 10), jti, issued, issued, issued + duration})
	if err != nil {
		return "", err
	}
	unsigned := head + "." + base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(unsigned))
	return unsigned + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil)), nil
}

// uniqueObject 拒絕 JWT 的重複鍵，避免不同解析器對同一簽章得出不同身分。
func uniqueObject(raw []byte) (map[string]json.RawMessage, bool) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return nil, false
	}
	object := make(map[string]json.RawMessage)
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return nil, false
		}
		name := key.(string)
		if _, exists := object[name]; exists {
			return nil, false
		}
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return nil, false
		}
		object[name] = value
	}
	end, err := decoder.Token()
	if err != nil || end != json.Delim('}') {
		return nil, false
	}
	_, err = decoder.Token()
	return object, err == io.EOF
}

// verifyToken 先驗固定演算法簽章再檢查 claims，避免非正規身分與過期憑證通過。
func verifyToken(key []byte, token string) (claims, *apiError) {
	// 固定大小上限與正規 base64url，先驗 HMAC 才解析任何 claims。
	var c claims
	if len(token) > 4096 {
		return c, &apiError{code: "E0002"}
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return c, &apiError{code: "E0002"}
	}
	for _, part := range parts {
		if !tokenPattern.MatchString(part) {
			return c, &apiError{code: "E0002"}
		}
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || len(signature) != sha256.Size || base64.RawURLEncoding.EncodeToString(signature) != parts[2] {
		return c, &apiError{code: "E0002"}
	}
	unsigned := parts[0] + "." + parts[1]
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(unsigned))
	if !hmac.Equal(mac.Sum(nil), signature) {
		return c, &apiError{code: "E0002"}
	}
	header, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || base64.RawURLEncoding.EncodeToString(header) != parts[0] {
		return c, &apiError{code: "E0002"}
	}
	headerObject, valid := uniqueObject(header)
	if !valid || len(headerObject) != 2 || string(headerObject["alg"]) != `"HS256"` || string(headerObject["typ"]) != `"JWT"` {
		return c, &apiError{code: "E0002"}
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || base64.RawURLEncoding.EncodeToString(payload) != parts[1] {
		return c, &apiError{code: "E0002"}
	}
	// RawMessage 先保留型態；反序列化後要求精確欄位集合，拒絕 null、陣列與浮點 NumericDate。
	object, valid := uniqueObject(payload)
	// 固定欄位、型態與時間關係，不容許 JWT 搭載額外授權資訊。
	if !valid || len(object) != 7 {
		return c, &apiError{code: "E0002"}
	}
	required := []string{"iss", "aud", "sub", "jti", "iat", "nbf", "exp"}
	for _, name := range required {
		if _, ok := object[name]; !ok {
			return c, &apiError{code: "E0002"}
		}
	}
	if json.Unmarshal(payload, &c) != nil {
		return c, &apiError{code: "E0002"}
	}
	for _, name := range []string{"iat", "nbf", "exp"} {
		raw := string(object[name])
		if !decimalPattern.MatchString(raw) {
			return c, &apiError{code: "E0002"}
		}
	}
	for _, name := range []string{"iss", "aud", "sub", "jti"} {
		if len(object[name]) == 0 || object[name][0] != '"' {
			return c, &apiError{code: "E0002"}
		}
	}
	id, e := urlID(c.Sub, "sub")
	jti, e2 := base64.RawURLEncoding.DecodeString(c.JTI)
	// JWT sub 須為無前導零的唯一表示；URL ID 仍可按原契約接受前導零。
	if e != nil || id < 1 || c.Sub != strconv.FormatInt(id, 10) || e2 != nil || len(jti) != 16 || base64.RawURLEncoding.EncodeToString(jti) != c.JTI || c.Iss != issuer || c.Aud != audience || c.Iat < 0 || c.Nbf != c.Iat || c.Exp != c.Iat+duration || time.Now().Unix() < c.Iat || time.Now().Unix() >= c.Exp {
		return c, &apiError{code: "E0002"}
	}
	return c, nil
}

// authenticate 以共用資料庫確認帳號與 session 現況，停用優先於撤銷。
func authenticate(ctx context.Context, db *sql.DB, key []byte, authorization string, required bool) (*account, *claims, *apiError, error) {
	// 沒帶憑證的公開讀取是訪客；其他情況先完成簽章與 claims 驗證。
	if authorization == "" && !required {
		return nil, nil, nil, nil
	}
	parts := strings.Split(authorization, " ")
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] == "" {
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	c, e := verifyToken(key, parts[1])
	if e != nil {
		return nil, nil, e, nil
	}
	id, _ := strconv.ParseInt(c.Sub, 10, 64)
	var a account
	err := db.QueryRowContext(ctx, "SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1", id).Scan(&a.ID, &a.Email, &a.Nickname, &a.Role, &a.Status, &a.Created)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	if err != nil {
		return nil, nil, nil, err
	}
	var accountID int64
	var backendID string
	var issued, expires time.Time
	var revoked sql.NullTime
	err = db.QueryRowContext(ctx, "SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1", c.JTI).Scan(&accountID, &backendID, &issued, &expires, &revoked)
	// session 的帳號、來源及精確時刻須與 JWT 一致，停用狀態先於撤銷回覆。
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	if err != nil {
		return nil, nil, nil, err
	}
	if accountID != a.ID || backendID != backend || !issued.Equal(time.Unix(c.Iat, 0)) || !expires.Equal(time.Unix(c.Exp, 0)) {
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	if a.Status == "disabled" {
		return nil, nil, &apiError{code: "E0102"}, nil
	}
	if revoked.Valid {
		return nil, nil, &apiError{code: "E0002"}, nil
	}
	return &a, &c, nil, nil
}
