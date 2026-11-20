module SiteApi
  class Error < StandardError
    MESSAGES = {
      "E0001" => [422, "欄位資料不正確，請確認後再送出"],
      "E0002" => [401, "登入已逾期，請重新登入"],
      "E0003" => [403, "沒有權限使用此功能"],
      "E0004" => [404, "找不到指定資料或 API"],
      "E0005" => [400, "系統暫時無法處理，請稍後再試"],
      "E0101" => [401, "帳號或密碼錯誤"],
      "E0102" => [403, "此帳號已停用，請聯絡系統管理員"],
      "E0104" => [500, "後台認證設定異常，請聯絡系統管理員"],
      "E0105" => [429, "登入失敗次數過多，請稍後再試"],
      "E9999" => [500, "系統暫時無法處理，請稍後再試"]
    }.freeze
    attr_reader :code, :errors, :retry_until, :status

    # 業務錯誤只帶安全欄位；鎖定期限留到提交後再計算 Retry-After。
    def initialize(code, errors = nil, retry_until: nil, status: nil)
      super(code)
      @code, @errors, @retry_until = code, errors, retry_until
      @status = status || MESSAGES.fetch(code).first
    end

    # 共同 envelope 排除框架診斷與敏感資料，驗證錯誤才帶 errors。
    def payload(log_id)
      body = { success: false, message: MESSAGES.fetch(code).last, error_code: code, log_id: log_id }
      body[:errors] = errors if errors
      body
    end
  end
end
