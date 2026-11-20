require "json"
require "securerandom"
require "logger"
require_relative "error"

module SiteApi
  class Boundary
    ORIGIN = "http://localhost:8080".freeze
    LOGGER = Logger.new($stdout)

    # 保留 Rails Rack 入口，讓路由、解析與 DB 例外都有共同安全出口。
    def initialize(app)
      @app = app
    end

    # 每次請求只記識別碼／方法／狀態，不記 URL、本文、JWT 或例外診斷。
    def call(env)
      id = env["site_api.log_id"] = SecureRandom.uuid
      LOGGER.info("IN log_id=#{id} method=#{env['REQUEST_METHOD']}")
      begin
        status, headers, body = @app.call(env)
      rescue Error => error
        status, headers, body = error_response(error, id)
      rescue StandardError
        status, headers, body = error_response(Error.new("E9999"), id)
      end
      headers["allow"] = env["site_api.allow"] if status == 405 && env["site_api.allow"]
      # 來源只精確允許共同前端；不反射陌生 Origin，也不允許 cookie 憑證。
      if env["HTTP_ORIGIN"] == ORIGIN
        headers["access-control-allow-origin"] = ORIGIN
        headers["vary"] = "Origin"
        headers["access-control-expose-headers"] = "Retry-After"
      end
      LOGGER.warn("EX log_id=#{id} status=#{status}") if status >= 400
      LOGGER.info("OUT log_id=#{id} status=#{status}")
      [status, headers, body]
    end

    # 失敗只輸出共同訊息；登入提交後用實際 DB 時間取得剩餘秒數。
    def error_response(error, id)
      headers = { "content-type" => "application/json; charset=utf-8" }
      if error.retry_until
        require_relative "database"
        # 新連線取提交後時刻；查核失敗交回安全系統錯誤，不假稱鎖定時間可用。
        now = Database.open { |db| db.exec("SELECT clock_timestamp() AS now").first.fetch("now") }
        headers["retry-after"] = [(error.retry_until - now).ceil, 1].max.to_s
      end
      [error.status, headers, [JSON.generate(error.payload(id))]]
    rescue StandardError
      fallback = Error.new("E9999")
      [500, { "content-type" => "application/json; charset=utf-8" }, [JSON.generate(fallback.payload(id))]]
    end
  end
end
