require "json"
require "uri"
require_relative "error"

module SiteApi
  class Input
    INVALID = "欄位格式或範圍不正確".freeze
    TYPE = "欄位型態不正確".freeze
    REQUIRED = "此欄位為必填".freeze
    MAX_ID = 9_007_199_254_740_991
    EMAIL = /\A[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\z/
    PASSWORD = /\A[A-Za-z0-9]{4,16}\z/
    BLANK = /\A[\u0009-\u000D\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]*\z/

    # 直接使用 Rack 的原始位元組，不接觸 Rails 可能合併、轉型的 params。
    def initialize(request)
      @request = request
    end

    # 解碼百分比位元組後才判斷 UTF-8；不把非法位元組換成合法 U+FFFD。
    def decode(value)
      # 百分比替換只產生單一原始位元組，不做 Unicode 正規化。
      value.b.tr("+", " ").gsub(/%([0-9A-Fa-f]{2})/) do
        [Regexp.last_match(1).to_i(16)].pack("C")
      end.force_encoding(Encoding::UTF_8)
    end

    # 保留參數身分及重複性；顯示標籤碰撞不能偽造真正重複或掩蓋既有錯誤。
    def query(allowed = [])
      values, errors, seen = {}, {}, {}
      # 空 Query 沒有參數；非空 Query 中的空項仍是未知參數。
      @request.query_string.b.split("&", -1).each do |pair|
        next if @request.query_string.empty?
        encoded_name, encoded_value = pair.split("=", 2)
        encoded_name ||= ""
        identity = decode(encoded_name).b
        name = identity.dup.force_encoding(Encoding::UTF_8)
        unless name.valid_encoding?
          # 非法 UTF-8 以解碼後 bytes 產生唯一 RFC3986 標籤；不同 URL 拼法仍保留相同身分。
          name = URI::DEFAULT_PARSER.escape(identity, /[^A-Za-z0-9._~-]/)
        end
        array = /\A(page|per_page|filters\[scope\])\[/.match(name)
        canonical = array && allowed.include?(array[1]) ? array[1] : name
        identity = canonical.b if canonical != name
        label = canonical.sub(/\Afilters\[([^\]]+)\]\z/, 'filters.\1')
        if seen[identity]
          errors[label] = ["此參數不可重複"]
        elsif array && allowed.include?(canonical)
          errors[label] ||= [TYPE]
        elsif !allowed.include?(canonical)
          errors[label] ||= ["不支援此欄位"]
        end
        seen[identity] = true
        value = decode(encoded_value || "")
        unless value.valid_encoding?
          errors[label] ||= [INVALID] if allowed.include?(canonical)
          value = ""
        end
        values[canonical] = value
      end
      [values, errors]
    end

    # 精確 management 值才觸發角色門禁；重複／其他值仍交後續 Query 驗證。
    def management?
      # 先做原始對照以維持認證／角色優先，不先拒絕其餘參數。
      @request.query_string.b.split("&").any? do |pair|
        name, value = pair.split("=", 2)
        decode(name || "") == "filters[scope]" && decode(value || "") == "management"
      end
    end

    # 無 Query 的操作也拒絕未知與重複欄位，不讓框架默默忽略。
    def no_query
      _, errors = query
      raise Error.new("E0001", errors) unless errors.empty?
    end

    # GET 只看原始非空 Body，優先於 Content-Type 或 JSON 解析。
    def no_body
      raise Error.new("E0001", { "_body" => ["此 API 不接受 Request Body"] }) unless @request.raw_post.to_s.empty?
    end

    # URL 數字只接受 ASCII 十進位，先縮去零再比長度，避免超長整數轉換。
    def integer(value, field)
      digits = value.sub(/\A0+/, "")
      unless /\A[0-9]+\z/.match?(value) && !digits.empty? && digits.length <= 16 &&
             (digits.length < 16 || digits <= "9007199254740991")
        raise Error.new("E0001", { field => [INVALID] })
      end
      digits.to_i
    end

    # 列表同時彙整必填、重複及範圍錯誤，通過後才回傳可用整數。
    def pagination(scope: false)
      no_body
      values, errors = query(scope ? %w[page per_page filters[scope]] : %w[page per_page])
      numbers = {}
      # 同參數的重複／型態錯誤先保留，不能被值驗證覆蓋。
      %w[page per_page].each do |field|
        if !values.key?(field)
          errors[field] = [REQUIRED]
        elsif !errors.key?(field)
          begin
            number = integer(values[field], field)
            number > 100 && field == "per_page" ? errors[field] = [INVALID] : numbers[field] = number
          rescue Error
            errors[field] = [INVALID]
          end
        end
      end
      if scope && values.key?("filters[scope]") && values["filters[scope]"] != "management"
        errors["filters.scope"] ||= [INVALID]
      end
      raise Error.new("E0001", errors) unless errors.empty?
      [numbers.fetch("page"), numbers.fetch("per_page")]
    end

    # JSON 重複鍵依共同既有語意採最後值；嚴格 UTF-8、深度與標準數字不能被框架放寬。
    def body(fields, empty: false)
      raw = @request.raw_post.to_s.dup.force_encoding(Encoding::UTF_8)
      return {} if empty && raw.empty?
      media = @request.get_header("CONTENT_TYPE").to_s.split(";", 2).first.to_s.strip.downcase
      if raw.empty? || media != "application/json"
        raise Error.new("E0001", { "_body" => ["Request Body 必須為 JSON object"] })
      end
      begin
        raise JSON::ParserError unless raw.valid_encoding?
        parsed = JSON.parse(raw, max_nesting: 511, allow_nan: false, allow_duplicate_key: true)
      rescue JSON::ParserError, JSON::NestingError, EncodingError
        raise Error.new("E0005")
      end
      unless parsed.is_a?(Hash)
        raise Error.new("E0001", { "_body" => ["Request Body 必須為 JSON object"] })
      end
      errors = {}
      # 必填／null／型態先行；各欄錯誤獨立彙整。
      fields.each do |field|
        if !parsed.key?(field)
          errors[field] = [REQUIRED]
        elsif parsed[field].nil?
          errors[field] = ["此欄位不可為 null"]
        elsif !parsed[field].is_a?(String)
          errors[field] = [TYPE]
        end
      end
      # 未知鍵保留原名稱，不轉換或忽略唯讀欄位。
      parsed.each_key do |field|
        errors[field] = ["不支援此欄位"] unless fields.include?(field)
      end
      if fields.include?("email") && !errors.key?("email")
        email = parsed["email"] = parsed["email"].sub(/\A +/, "").sub(/ +\z/, "").tr("A-Z", "a-z")
        errors["email"] = [INVALID] unless email.length <= 254 && EMAIL.match?(email) && email.split("@", 2).first.length <= 64
      end
      if fields.include?("password") && !errors.key?("password") && !PASSWORD.match?(parsed["password"])
        errors["password"] = [INVALID]
      end
      if fields.include?("nickname") && !errors.key?("nickname")
        nickname = parsed["nickname"]
        errors["nickname"] = [INVALID] unless nickname.length.between?(1, 30) && !nickname.include?("\0") && !BLANK.match?(nickname)
      end
      raise Error.new("E0001", errors) unless errors.empty?
      parsed
    end
  end
end
