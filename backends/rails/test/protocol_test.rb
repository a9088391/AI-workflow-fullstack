require "minitest/autorun"
require "rack/mock"
require_relative "../config/environment"

class ProtocolTest < Minitest::Test
  # 先由真實 Rails 路由確認共同錯誤及 CORS，不依賴可變資料庫內容。
  def test_unknown_api_has_safe_envelope_and_cors
    response = Rack::MockRequest.new(Rails.application).get("/api/missing", "HTTP_ORIGIN" => "http://localhost:8080")
    assert_equal 404, response.status
    body = JSON.parse(response.body)
    assert_equal "E0004", body.fetch("error_code")
    assert_match(/\A[0-9a-f-]{36}\z/, body.fetch("log_id"))
    assert_equal "http://localhost:8080", response["access-control-allow-origin"]
  end
end
