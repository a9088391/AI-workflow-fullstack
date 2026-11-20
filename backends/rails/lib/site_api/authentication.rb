require "argon2id"
require "base64"
require "openssl"
require "securerandom"
require_relative "database"
require_relative "input"

module SiteApi
  module Authentication
    PHC = /\A\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+\/]{22})\$([A-Za-z0-9+\/]{43})\z/
    ISSUER = "ai-workflow-fullstack:rails".freeze
    AUDIENCE = "ai-workflow-fullstack-api:rails".freeze

    module_function

    # 固定以台北時區的秒級字串公開業務時間，不改寫儲存時間點。
    def business_time(time)
      time.getlocal("+08:00").strftime("%Y-%m-%d %H:%M:%S")
    end

    # 本人 DTO 只公開核准帳號欄位，內部雜湊、計數與鎖定不進入回應。
    def account_dto(account)
      account.slice("id", "email", "nickname", "role", "status").merge("created_at" => business_time(account.fetch("created_at")))
    end

    # 只讀獨立持久化金鑰；缺漏或非正規 Base64 都明確失敗，不自動換新。
    def key
      encoded = File.binread("/run/secrets/rails-jwt.key").sub(/\n+\z/, "")
      raw = Base64.strict_decode64(encoded)
      raise Error.new("E0104") unless raw.bytesize == 32 && Base64.strict_encode64(raw) == encoded
      raw
    rescue SystemCallError, ArgumentError
      raise Error.new("E0104")
    end

    # 無 padding 的正規 Base64url 用於 JWT 各段及安全隨機 jti。
    def encode(raw)
      Base64.urlsafe_encode64(raw, padding: false)
    end

    # 嚴格重編碼核對，拒絕 padding、非 URL 字元或非正規尾端 bits。
    def decode(text)
      raise Error.new("E0002") unless text.is_a?(String) && /\A[A-Za-z0-9_-]+\z/.match?(text)
      raw = Base64.urlsafe_decode64(text)
      raise Error.new("E0002") unless encode(raw) == text
      raw
    rescue ArgumentError
      raise Error.new("E0002")
    end

    # 簽發固定本後端、八小時 claims；角色與密碼不寫入 JWT。
    def issue(account_id, jti, issued)
      header = encode(JSON.generate(alg: "HS256", typ: "JWT"))
      claims = { iss: ISSUER, aud: AUDIENCE, sub: account_id.to_s, jti: jti,
                 iat: issued, nbf: issued, exp: issued + 28_800 }
      signed = "#{header}.#{encode(JSON.generate(claims))}"
      "#{signed}.#{encode(OpenSSL::HMAC.digest('SHA256', key, signed))}"
    end

    # 金鑰與演算法固定由伺服器決定；先驗簽章才解析身分，拒絕二義 JSON。
    def verify(token)
      own_key = key
      parts = token.split(".", -1)
      raise Error.new("E0002") unless parts.length == 3
      signature = decode(parts[2])
      expected = OpenSSL::HMAC.digest("SHA256", own_key, "#{parts[0]}.#{parts[1]}")
      raise Error.new("E0002") unless signature.bytesize == 32 && OpenSSL.fixed_length_secure_compare(expected, signature)
      begin
        header_text, claims_text = decode(parts[0]).force_encoding("UTF-8"), decode(parts[1]).force_encoding("UTF-8")
        raise JSON::ParserError unless header_text.valid_encoding? && claims_text.valid_encoding?
        header = JSON.parse(header_text, max_nesting: 511, allow_duplicate_key: false)
        claims = JSON.parse(claims_text, max_nesting: 511, allow_duplicate_key: false)
      rescue JSON::ParserError, JSON::NestingError, EncodingError
        raise Error.new("E0002")
      end
      raise Error.new("E0002") unless header == { "alg" => "HS256", "typ" => "JWT" } && claims.is_a?(Hash)
      subject, issued = claims["sub"], claims["iat"]
      unless claims["iss"] == ISSUER && claims["aud"] == AUDIENCE &&
             subject.is_a?(String) && /\A[1-9][0-9]{0,15}\z/.match?(subject) && subject.to_i <= Input::MAX_ID &&
             claims["jti"].is_a?(String) && decode(claims["jti"]).bytesize == 16 &&
             issued.is_a?(Integer) && issued >= 0 && claims["nbf"].is_a?(Integer) && claims["nbf"] == issued &&
             claims["exp"].is_a?(Integer) && claims["exp"] == issued + 28_800 &&
             Time.now.to_i >= issued && Time.now.to_i < claims["exp"]
        raise Error.new("E0002")
      end
      claims
    end

    # 可選登入只有完全省略 Authorization 才視為訪客，錯誤憑證不能降成訪客。
    def identity(request, db, required: true)
      authorization = request.get_header("HTTP_AUTHORIZATION")
      return nil if authorization.nil? && !required
      match = /\ABearer ([^\s]+)\z/i.match(authorization.to_s)
      raise Error.new("E0002") unless match
      claims = verify(match[1])
      # 單一 statement 快照同時讀帳號與其 session，避免並行停用提交後混合新舊狀態。
      row = db.exec_params(
        "SELECT accounts.id, accounts.email, accounts.nickname, accounts.role, accounts.status, accounts.created_at, " \
        "login_sessions.backend_id AS session_backend_id, login_sessions.issued_at AS session_issued_at, " \
        "login_sessions.expires_at AS session_expires_at, login_sessions.revoked_at AS session_revoked_at " \
        "FROM accounts JOIN login_sessions ON login_sessions.account_id=accounts.id " \
        "WHERE accounts.id=$1 AND login_sessions.jti=$2", [claims["sub"], claims["jti"]]
      ).first
      unless row && row["session_backend_id"] == "rails" &&
             row["session_issued_at"] == Time.at(claims["iat"]) && row["session_expires_at"] == Time.at(claims["exp"])
        raise Error.new("E0002")
      end
      # 身分可驗證後才判停用，優先於同一登入已撤銷；復原帳號不復活舊 token。
      raise Error.new("E0102") if row["status"] == "disabled"
      raise Error.new("E0002") if row["session_revoked_at"]
      [row.slice("id", "email", "nickname", "role", "status", "created_at"), claims]
    end

    # 只接受共用 PHC 的固定成本與正規 Base64；損壞不能算作密碼錯誤。
    def password_matches?(password, encoded)
      match = PHC.match(encoded.to_s)
      raise Error.new("E0104") unless match
      # 先限制大小與尾端 bits，避免不支援成本流入雜湊函式。
      [16, 32].each_with_index do |length, index|
        text = match[index + 1]
        raw = Base64.strict_decode64(text + "=" * ((4 - text.length % 4) % 4))
        raise Error.new("E0104") unless raw.bytesize == length && Base64.strict_encode64(raw).delete_suffix("=").delete_suffix("=") == text
      end
      Argon2id::Password.new(encoded).is_password?(password)
    rescue Argon2id::Error, ArgumentError
      raise Error.new("E0104")
    end

    # DB 唯一約束裁決並行重複註冊，不以應用預查代替資料完整性。
    def register(db, body)
      if db.exec_params("SELECT id FROM accounts WHERE email=$1", [body.fetch("email")]).first
        raise Error.new("E0001", { "email" => ["此 Email 已註冊"] })
      end
      begin
        encoded = Argon2id::Password.create(body.fetch("password"), m_cost: 19_456, t_cost: 2,
                                          parallelism: 1, salt_len: 16, output_len: 32).to_s
      rescue Argon2id::Error
        raise Error.new("E0104")
      end
      db.exec_params("INSERT INTO accounts(email,password_hash,nickname,role,status) VALUES($1,$2,$3,'member','active')",
                     [body.fetch("email"), encoded, body.fetch("nickname")])
      {}
    rescue PG::UniqueViolation => error
      raise unless error.result.error_field(PG::Result::PG_DIAG_CONSTRAINT_NAME) == "accounts_email_key"
      raise Error.new("E0001", { "email" => ["此 Email 已註冊"] })
    end

    # 設定列→帳號列鎖後取一次實際 DB 時間；業務失敗當作結果，先提交才拋出。
    def login(db, body)
      # 集中鎖定共用設定，保留其他後端同樣的失敗計數與更新順序。
      result = db.transaction do
        login_locked(db, body)
      end
      raise result if result.is_a?(Error)
      result
    end

    # 鎖後判定保留計數的業務錯誤；系統或雜湊錯誤則拋例外讓交易回滾。
    def login_locked(db, body)
      settings = db.exec("SELECT * FROM login_lockout_settings WHERE id=1 FOR UPDATE").first
      unless settings && [true, false].include?(settings["enabled"]) &&
             settings["window_minutes"].is_a?(Integer) && settings["window_minutes"].between?(1, 60) &&
             settings["failure_threshold"].is_a?(Integer) && settings["failure_threshold"].between?(1, 100) &&
             settings["lock_seconds"].is_a?(Integer) && settings["lock_seconds"].between?(1, 86_400)
        raise Error.new("E0104")
      end
      account = db.exec_params("SELECT * FROM accounts WHERE email=$1 FOR UPDATE", [body.fetch("email")]).first
      now = db.exec("SELECT clock_timestamp() AS now").first.fetch("now")
      return Error.new("E0101") unless account
      return Error.new("E0102") if account["status"] == "disabled"
      if account["role"] == "member" && account["locked_until"]
        return Error.new("E0105", retry_until: account["locked_until"]) if settings["enabled"] && now < account["locked_until"]
        # 到期或功能關閉即換世代；這個改變即使隨後密碼錯誤也要提交。
        account["failure_epoch"] += 1
        db.exec_params("UPDATE accounts SET failure_epoch=$1, locked_until=NULL WHERE id=$2", [account["failure_epoch"], account["id"]])
      end
      unless password_matches?(body.fetch("password"), account.fetch("password_hash"))
        if settings["enabled"] && account["role"] == "member"
          db.exec_params("INSERT INTO login_failures(account_id,failure_epoch,occurred_at) VALUES($1,$2,$3)",
                         [account["id"], account["failure_epoch"], now])
          count = db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1 AND failure_epoch=$2 AND occurred_at>$3 AND occurred_at<=$4",
                                 [account["id"], account["failure_epoch"], now - settings["window_minutes"] * 60, now]).first.fetch("count")
          if count >= settings["failure_threshold"]
            until_time = now + settings["lock_seconds"]
            db.exec_params("UPDATE accounts SET locked_until=$1 WHERE id=$2", [until_time, account["id"]])
            return Error.new("E0105", retry_until: until_time)
          end
        end
        return Error.new("E0101")
      end
      # 金鑰或 session 寫入失敗會回滾成功重設，不能回傳未提交的登入。
      issued = now.to_i
      jti = encode(SecureRandom.random_bytes(16))
      token = issue(account.fetch("id"), jti, issued)
      db.exec_params("UPDATE accounts SET failure_epoch=failure_epoch+1, locked_until=NULL WHERE id=$1", [account["id"]])
      db.exec_params("INSERT INTO login_sessions(jti,account_id,backend_id,issued_at,expires_at) VALUES($1,$2,'rails',to_timestamp($3),to_timestamp($4))",
                     [jti, account["id"], issued, issued + 28_800])
      { access_token: token, token_type: "Bearer", expires_at: business_time(Time.at(issued + 28_800)), user: account_dto(account) }
    end

    # 登出在帳號→session 鎖後重驗登入，只撤銷本次 jti，其他登入不受影響。
    def logout(request, db, input)
      # 端點認證優先於 Query／Body，驗完參數才取得寫入鎖。
      db.transaction do
        account, claims = identity(request, db)
        input.no_query
        input.body([], empty: true)
        db.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [account["id"]])
        db.exec_params("SELECT jti FROM login_sessions WHERE jti=$1 FOR UPDATE", [claims["jti"]])
        identity(request, db)
        db.exec_params("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE jti=$1", [claims["jti"]])
      end
      {}
    end
  end
end
