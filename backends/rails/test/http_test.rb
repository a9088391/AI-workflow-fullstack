require "minitest/autorun"
require "minitest/mock"
require "net/http"
require "json"
require "securerandom"
require "pg"
require "base64"
require "openssl"
require "rack/mock"
require File.expand_path("config/environment", Dir.pwd)

# 只在測試程序安排時鐘與查詢同步點；PG SQL／鎖／提交均使用真實隔離資料庫。
module TestDatabaseClock
  # 完整公開 Rack 請求可固定邊界時刻，避免依睡眠或模糊的鄰近時間推論等號。
  def exec(sql, ...)
    clock = Thread.current[:rails01_clock]
    return [{ "now" => clock }] if clock && sql == "SELECT clock_timestamp() AS now"
    super
  end

  # 在真正身分查詢完成後觸發一次外部提交，精確安排並行停用，不替換查詢結果。
  def exec_params(sql, ...)
    result = super
    hook = Thread.current[:rails01_after_identity_read]
    if hook && sql.start_with?("SELECT ") && sql.include?("FROM accounts") && !sql.include?("FOR UPDATE")
      Thread.current[:rails01_after_identity_read] = nil
      hook.call
    end
    result
  end
end
PG::Connection.prepend(TestDatabaseClock)

class HttpTest < Minitest::Test
  # 首次寫入前核對實際資料庫識別及兩個服務；外部執行者另須 inspect Compose 掛載／port。
  def setup
    @project = ENV.fetch("RAILS01_TEST_PROJECT")
    raise "isolated project required" unless /\Aisolated-rails01-[a-z0-9-]+\z/.match?(@project)
    @db = PG.connect(host: ENV.fetch("DB_HOST"), dbname: "articles", user: ENV.fetch("DB_USERNAME"), password: ENV.fetch("DB_PASSWORD"))
    @db.type_map_for_results = PG::BasicTypeMapForResults.new(@db)
    @db.type_map_for_queries = PG::BasicTypeMapForQueries.new(@db)
    raise "database target mismatch" unless @db.exec("SHOW cluster_name").first.fetch("cluster_name") == @project
    raise "application user required" unless @db.exec("SELECT current_user").first.fetch("current_user") == "articles_app"
    status, up = call(:rails, "GET", "/up")
    assert_equal 200, status
    assert_equal "OK", up
    @namespace = "r#{SecureRandom.hex(7)}"
  end

  # 測試只釋放自己的連線；保留隔離資料供 Main 讀回，不刪除或重設資料卷。
  def teardown
    @owner&.close
    @db&.close
  end

  # 原始 HTTP 保留輸入 bytes 與 GET Body，不經表單／JSON client 自動正規化。
  def call(backend, method, path, body: nil, raw: nil, token: nil, headers: {})
    base = backend == :rails ? "http://127.0.0.1:8000" : "http://laravel:8000"
    uri = URI("#{base}#{path}")
    request = Net::HTTPGenericRequest.new(method, !body.nil? || !raw.nil?, true, uri.request_uri)
    request["Accept"] = "application/json"
    request["Content-Type"] = "application/json" if body || raw
    request["Authorization"] = "Bearer #{token}" if token
    # 測試可傳 malformed Authorization 或精確媒體型態，但不記錄憑證。
    headers.each do |name, value|
      request[name] = value
    end
    request.body = raw.nil? ? JSON.generate(body) : raw if body || raw
    # 每個呼叫完成才回傳結果，不對未知結果的非冪等寫入自動重試。
    response = Net::HTTP.start(uri.host, uri.port, open_timeout: 5, read_timeout: 10) do |http|
      http.request(request)
    end
    data = path == "/up" ? response.body : response.body.to_s.empty? ? {} : JSON.parse(response.body)
    [response.code.to_i, data, response]
  end

  # 比較逐字錯誤碼／訊息與安全形狀，不讓僅 HTTP status 相同掩蓋契約差異。
  def error(response, status, code, errors = nil)
    actual_status, body = response
    messages = { "E0001" => "欄位資料不正確，請確認後再送出", "E0002" => "登入已逾期，請重新登入",
                 "E0003" => "沒有權限使用此功能", "E0004" => "找不到指定資料或 API", "E0005" => "系統暫時無法處理，請稍後再試",
                 "E0101" => "帳號或密碼錯誤", "E0102" => "此帳號已停用，請聯絡系統管理員",
                 "E0104" => "後台認證設定異常，請聯絡系統管理員", "E0105" => "登入失敗次數過多，請稍後再試", "E9999" => "系統暫時無法處理，請稍後再試" }
    assert_equal status, actual_status
    assert_equal false, body.fetch("success")
    assert_equal code, body.fetch("error_code")
    assert_equal messages.fetch(code), body.fetch("message")
    assert_match(/\A[0-9a-f-]{36}\z/, body.fetch("log_id"))
    errors ? assert_equal(errors, body["errors"]) : assert_nil(body["errors"])
    assert_equal errors ? %w[error_code errors log_id message success] : %w[error_code log_id message success], body.keys.sort
  end

  # 帳號由實際 API 建立；有效 fixture 密碼符合本機 demo 英數長度限制。
  def member(backend = :rails, suffix = "member", password = "Demo1234")
    email = "#{@namespace}-#{suffix}@example.test"
    result = call(backend, "POST", "/api/auth/register", body: { email: email, password: password, nickname: "測試\r\n😀" })
    assert_equal [201, { "success" => true, "message" => "OK", "data" => {} }], result.first(2)
    [email, password]
  end

  # 以實際登入取得憑證，不透過私有簽發函式偽造一般成功案例。
  def login(backend, actor)
    status, body = call(backend, "POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }, headers: { "Authorization" => "old invalid token" })
    assert_equal 200, status
    body.fetch("data")
  end

  # owner 只用於指定隔離 fixture 的故障與時間安排，不作跨後端互通的資料建立證據。
  def owner
    @owner ||= PG.connect(host: ENV.fetch("DB_HOST"), dbname: "articles", user: "articles_owner", password: ENV.fetch("DB_OWNER_PASSWORD"))
  end

  # 保留完整設定並於 ensure 復原，避免同一隔離目標重跑時繼承測試設定。
  def with_lock_settings(threshold: 5, seconds: 300)
    old = owner.exec("SELECT * FROM login_lockout_settings WHERE id=1").first
    owner.exec_params("UPDATE login_lockout_settings SET enabled=true, window_minutes=5, failure_threshold=$1, lock_seconds=$2 WHERE id=1", [threshold, seconds])
    yield
  ensure
    if old
      owner.exec_params("UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1",
                        old.values_at("enabled", "window_minutes", "failure_threshold", "lock_seconds"))
    end
  end

  # 測試時鐘只作用於當次 Rack 請求，完成或失敗都恢復，不改 live Puma 的時間。
  def at_time(time)
    previous = Thread.current[:rails01_clock]
    # 同步 JWT 與取鎖後 DB 時刻，允許直接證明等號及持久化效果。
    Time.stub(:now, time) do
      Thread.current[:rails01_clock] = time
      yield
    end
  ensure
    Thread.current[:rails01_clock] = previous
  end

  # 和 live HTTP 使用相同 Rails route/controller，但只在此程序受控時鐘下呼叫。
  def rack_call(method, path, body: nil, token: nil)
    env = {}
    env["HTTP_AUTHORIZATION"] = "Bearer #{token}" if token
    if body
      env["CONTENT_TYPE"] = "application/json"
      env[:input] = JSON.generate(body)
    end
    response = Rack::MockRequest.new(Rails.application).request(method, path, env)
    [response.status, JSON.parse(response.body), response]
  end

  # 同步點只涵蓋當次公開 Rack 請求，確認實際觸發並在任何出口還原。
  def after_identity_read(callback)
    previous = Thread.current[:rails01_after_identity_read]
    Thread.current[:rails01_after_identity_read] = callback
    result = yield
    assert_nil Thread.current[:rails01_after_identity_read], "identity query synchronization was not reached"
    result
  ensure
    Thread.current[:rails01_after_identity_read] = previous
  end

  # 跨向驗密碼、內容、八小時與登出讀回，證明互通也維持後端 JWT 邊界。
  def test_cross_backend_accounts_reading_and_logout
    actor = member(:laravel)
    laravel = login(:laravel, actor)
    rails = login(:rails, actor)
    token = rails.fetch("access_token")
    assert_equal 201, call(:laravel, "POST", "/api/articles", token: laravel.fetch("access_token"), body: { title: @namespace, body: "原文\r\n中文😀" }).first
    status, mine = call(:laravel, "GET", "/api/me/articles?page=1&per_page=100", token: laravel.fetch("access_token"))
    assert_equal 200, status
    # 只選自己剛建立的文章，不依序號連續性或其他測試資料排序猜 ID。
    article = mine.fetch("data").fetch("list").find do |row|
      row["title"] == @namespace
    end
    id = article.fetch("id")
    assert_equal 200, call(:rails, "GET", "/api/articles/%#{id.to_s.getbyte(0).to_s(16)}#{id.to_s[1..]}").first
    error(call(:rails, "GET", "/api/articles/%2531"), 422, "E0001", { "article_id" => ["欄位格式或範圍不正確"] })
    error(call(:rails, "GET", "/api/articles/+1"), 422, "E0001", { "article_id" => ["欄位格式或範圍不正確"] })
    assert_equal 201, call(:laravel, "POST", "/api/articles/#{id}/comments", token: laravel.fetch("access_token"), body: { body: "原留言\n😀" }).first
    status, detail = call(:rails, "GET", "/api/articles/#{id}", token: token)
    assert_equal 200, status
    assert_equal article, detail.fetch("data")
    assert_equal %w[id nickname], detail.fetch("data").fetch("author").keys.sort
    status, comments = call(:rails, "GET", "/api/articles/#{id}/comments?page=1&per_page=20")
    assert_equal 200, status
    # 只投影留言本文，以完整集合證明沒有額外可見留言。
    assert_equal ["原留言\n😀"], comments.fetch("data").fetch("list").map { |row| row.fetch("body") }
    error(call(:rails, "GET", "/api/me", token: laravel.fetch("access_token")), 401, "E0002")
    error(call(:laravel, "GET", "/api/me", token: token), 401, "E0002")
    rails_actor = member(:rails, "reverse", "1234")
    assert_equal rails_actor[0], login(:laravel, rails_actor).fetch("user").fetch("email")
    second = login(:rails, actor).fetch("access_token")
    payload = JSON.parse(Base64.urlsafe_decode64(token.split(".")[1]))
    assert_equal 28_800, payload.fetch("exp") - payload.fetch("iat")
    assert_equal Time.at(payload.fetch("exp")).getlocal("+08:00").strftime("%Y-%m-%d %H:%M:%S"), rails.fetch("expires_at")
    assert_equal [200, { "success" => true, "message" => "OK", "data" => {} }], call(:rails, "POST", "/api/auth/logout", token: token).first(2)
    error(call(:rails, "GET", "/api/me", token: token), 401, "E0002")
    assert_equal 200, call(:rails, "GET", "/api/me", token: second).first
  end

  # 聚合型態、正規化與密碼邊界，不以框架原生文案作 oracle。
  def test_registration_validation_and_unicode
    error(call(:rails, "POST", "/api/auth/register", body: { email: nil, password: true, nickname: "\0", role: "admin" }), 422, "E0001",
          { "email" => ["此欄位不可為 null"], "password" => ["欄位型態不正確"], "nickname" => ["欄位格式或範圍不正確"], "role" => ["不支援此欄位"] })
    # 每個非法密碼都在公開登入入口拒絕，不會建立失敗事件。
    ["abc", "a" * 17, "12 4", "abc!", "中文1234", "abcd\n"].each do |password|
      error(call(:rails, "POST", "/api/auth/login", body: { email: "#{@namespace}@example.test", password: password }), 422, "E0001", { "password" => ["欄位格式或範圍不正確"] })
    end
    # 合法長度與純英／純數字都要經另一後端驗證 PHC。
    ["ABCD", "a" * 16, "1234"].each_with_index do |password, index|
      actor = member(:rails, "valid#{index}", password)
      assert_equal actor[0], login(:laravel, actor).fetch("user").fetch("email")
    end
    actor = member
    email = "  #{actor[0].upcase}  "
    assert_equal actor[0], login(:rails, [email, actor[1]]).fetch("user").fetch("email")
    error(call(:rails, "POST", "/api/auth/register", body: { email: email, password: actor[1], nickname: "A" }), 422, "E0001", { "email" => ["此 Email 已註冊"] })
  end

  # 解析必須看原始 bytes；重複 Body 最後值、畸形 JSON、UTF-8、媒體型態各自保留分類。
  def test_raw_request_and_query_contract
    error(call(:rails, "POST", "/api/auth/login", raw: "{"), 400, "E0005")
    error(call(:rails, "POST", "/api/auth/login", raw: "\xFF".b), 400, "E0005")
    error(call(:rails, "POST", "/api/auth/login", raw: "[]"), 422, "E0001", { "_body" => ["Request Body 必須為 JSON object"] })
    error(call(:rails, "POST", "/api/auth/login", raw: "{}", headers: { "Content-Type" => "application/jsonx" }), 422, "E0001", { "_body" => ["Request Body 必須為 JSON object"] })
    actor = member
    raw = JSON.generate(email: actor[0], password: "Wrong123").sub(/\}\z/, ',"password":"Demo1234"}')
    assert_equal 200, call(:rails, "POST", "/api/auth/login", raw: raw).first
    error(call(:rails, "GET", "/api/articles?page=1&page=2&per_page=20"), 422, "E0001", { "page" => ["此參數不可重複"] })
    error(call(:rails, "GET", "/api/articles?page[]=1&per_page=20"), 422, "E0001", { "page" => ["欄位型態不正確"] })
    error(call(:rails, "GET", "/api/articles?page=%FF&per_page=20"), 422, "E0001", { "page" => ["欄位格式或範圍不正確"] })
    error(call(:rails, "GET", "/api/articles?page=1&per_page=20&x%FF=1&x%25FF=2"), 422, "E0001", { "x%FF" => ["不支援此欄位"] })
    # 空片段是未知空名稱，仍須保留有效 page 與 per_page 的值。
    ["page=1&per_page=20&", "&page=1&per_page=20", "page=1&&per_page=20"].each do |query|
      error(call(:rails, "GET", "/api/articles?#{query}"), 422, "E0001", { "" => ["不支援此欄位"] })
    end
    error(call(:rails, "GET", "/api/articles?page=1&per_page=20", raw: "{"), 422, "E0001", { "_body" => ["此 API 不接受 Request Body"] })
    error(call(:rails, "GET", "/api/articles/0?x=1", headers: { "Authorization" => "invalid" }), 401, "E0002")
    error(call(:rails, "GET", "/api/articles/0?x=1"), 422, "E0001", { "x" => ["不支援此欄位"] })
    error(call(:rails, "GET", "/api/articles/0"), 422, "E0001", { "article_id" => ["欄位格式或範圍不正確"] })
    error(call(:rails, "GET", "/api/articles?page=1&per_page=20&filters[scope]=management"), 401, "E0002")
    token = login(:rails, actor).fetch("access_token")
    error(call(:rails, "GET", "/api/articles?filters[scope]=management", token: token), 403, "E0003")
    error(call(:rails, "GET", "/api/articles?page=1&per_page=20&filters[scope]=other"), 422, "E0001", { "filters.scope" => ["欄位格式或範圍不正確"] })
    status, page = call(:rails, "GET", "/api/articles?page=9007199254740991&per_page=100")
    assert_equal 200, status
    assert_equal [], page.fetch("data").fetch("list")
    assert_equal 9_007_199_254_740_991, page.fetch("data").fetch("pagination").fetch("page")
  end

  # 共用失敗計數必須跨後端累計且在 401／429 後保留，既有登入仍可讀取。
  def test_lockout_commits_and_recovers
    # 同一帳號以兩個後端交錯密碼錯誤；格式錯誤不應影響門檻。
    with_lock_settings(threshold: 3, seconds: 2) do
      actor = member
      existing = login(:rails, actor).fetch("access_token")
      error(call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: "abc" }), 422, "E0001", { "password" => ["欄位格式或範圍不正確"] })
      error(call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: "Wrong123" }), 401, "E0101")
      error(call(:laravel, "POST", "/api/auth/login", body: { email: actor[0], password: "Wrong123" }), 401, "E0101")
      locked = call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: "Wrong123" }, headers: { "Origin" => "http://localhost:8080" })
      error(locked, 429, "E0105")
      assert_includes ["1", "2"], locked[2]["Retry-After"]
      assert_equal "Retry-After", locked[2]["Access-Control-Expose-Headers"]
      error(call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }), 429, "E0105")
      assert_equal 200, call(:rails, "GET", "/api/me", token: existing).first
      # 用隔離 fixture 將鎖設為已到期，公開登入確認清鎖／換世代及下一次失敗重新計數。
      owner.exec_params("UPDATE accounts SET locked_until=clock_timestamp()-interval '1 microsecond' WHERE email=$1", [actor[0]])
      login(:rails, actor)
      error(call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: "Wrong123" }), 401, "E0101")
    end
  end

  # 故障雜湊與停用／撤銷順序不能變成普通密碼錯誤或暴露儲存欄位。
  def test_corrupted_hash_and_revocation_precedence
    actor = member
    data = login(:rails, actor)
    token = data.fetch("access_token")
    id = data.fetch("user").fetch("id")
    original = @db.exec_params("SELECT password_hash,failure_epoch FROM accounts WHERE id=$1", [id]).first
    # 損壞格式、不同成本、padding 與非正規尾端 bits 均不能當作普通錯誤密碼。
    ["broken", original.fetch("password_hash").sub("m=19456", "m=19457"),
     # 到期清鎖發生在驗雜湊前；系統錯誤必須回滾那個清鎖／世代改變。
     original.fetch("password_hash") + "=", original.fetch("password_hash")[0...-1] + "B"].each do |encoded|
      owner.exec_params("UPDATE accounts SET password_hash=$1,locked_until=to_timestamp(1) WHERE id=$2", [encoded, id])
      error(call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }), 500, "E0104")
      persisted = @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
      assert_equal original.fetch("failure_epoch"), persisted.fetch("failure_epoch")
      assert_equal Time.at(1), persisted.fetch("locked_until")
    end
    assert_equal 0, @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1", [id]).first.fetch("count")
    owner.exec_params("UPDATE accounts SET status='disabled' WHERE id=$1", [id])
    owner.exec_params("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1", [id])
    error(call(:rails, "GET", "/api/me", token: token), 403, "E0102")
    error(call(:rails, "POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }), 403, "E0102")
    owner.exec_params("UPDATE accounts SET status='active' WHERE id=$1", [id])
    error(call(:rails, "GET", "/api/me", token: token), 401, "E0002")
  end

  # 公開讀取不洩漏下架資料；作者預覽父文也不能讀到個別下架留言。
  def test_visibility_and_exact_comments_pagination
    actor = member(:laravel)
    laravel = login(:laravel, actor).fetch("access_token")
    rails = login(:rails, actor).fetch("access_token")
    stranger = login(:rails, member(:rails, "stranger")).fetch("access_token")
    assert_equal 201, call(:laravel, "POST", "/api/articles", token: laravel, body: { title: @namespace, body: "parent" }).first
    id = call(:laravel, "GET", "/api/me/articles?page=1&per_page=100", token: laravel)[1].fetch("data").fetch("list").first.fetch("id")
    # 固定三則資料，才能完整核對分頁集合、排序與真實總數。
    3.times do |index|
      assert_equal 201, call(:laravel, "POST", "/api/articles/#{id}/comments", token: laravel, body: { body: "comment#{index}" }).first
    end
    original = call(:rails, "GET", "/api/articles/#{id}/comments?page=1&per_page=100")[1].fetch("data").fetch("list")
    comment = original.first
    assert_equal 200, call(:laravel, "PUT", "/api/comments/#{comment.fetch('id')}/visibility", token: laravel, body: { version: 1, visibility: "hidden" }).first
    first = call(:rails, "GET", "/api/articles/#{id}/comments?page=1&per_page=1")[1].fetch("data")
    second = call(:rails, "GET", "/api/articles/#{id}/comments?page=2&per_page=1")[1].fetch("data")
    # 兩頁分別只有一則；完整 ID 集合須和下架前清單去除下架項目一致。
    assert_equal original.drop(1).map { |row| row.fetch("id") }, [first.fetch("list").first.fetch("id"), second.fetch("list").first.fetch("id")]
    assert_equal({ "page" => 1, "per_page" => 1, "total" => 2, "total_pages" => 2 }, first.fetch("pagination"))
    assert_equal 200, call(:laravel, "PUT", "/api/articles/#{id}/visibility", token: laravel, body: { version: 1, visibility: "hidden" }).first
    error(call(:rails, "GET", "/api/articles/#{id}"), 404, "E0004")
    error(call(:rails, "GET", "/api/articles/#{id}", token: stranger), 404, "E0004")
    assert_equal true, call(:rails, "GET", "/api/articles/#{id}", token: rails)[1].fetch("data").fetch("can_restore")
    assert_equal 2, call(:rails, "GET", "/api/articles/#{id}/comments?page=1&per_page=20", token: rails)[1].fetch("data").fetch("pagination").fetch("total")
    # 目前角色由 DB 決定；同一有效身分升成 admin 後，可預覽隱藏內容與全部留言。
    stranger_claims = JSON.parse(Base64.urlsafe_decode64(stranger.split(".")[1]))
    owner.exec_params("UPDATE accounts SET role='admin' WHERE id=$1", [stranger_claims.fetch("sub")])
    admin_comments = call(:rails, "GET", "/api/articles/#{id}/comments?page=1&per_page=20", token: stranger)[1].fetch("data")
    assert_equal 3, admin_comments.fetch("pagination").fetch("total")
    assert_equal true, call(:rails, "GET", "/api/articles/#{id}", token: stranger)[1].fetch("data").fetch("can_restore")
    error(call(:rails, "GET", "/api/articles?page=1&per_page=20&filters[scope]=management", token: stranger), 404, "E0004")
    error(call(:rails, "GET", "/api/articles?filters[scope]=management", token: stranger), 422, "E0001", { "page" => ["此欄位為必填"], "per_page" => ["此欄位為必填"] })
  end

  # CORS 同時驗成功／錯誤與 preflight，陌生 origin 不得取得瀏覽器讀取權。
  def test_cors_methods_and_safe_errors
    allowed = call(:rails, "OPTIONS", "/api/auth/login", headers: { "Origin" => "http://localhost:8080", "Access-Control-Request-Method" => "POST", "Access-Control-Request-Headers" => "authorization,content-type" })
    assert_equal 204, allowed.first
    assert_equal "http://localhost:8080", allowed[2]["Access-Control-Allow-Origin"]
    assert_equal "POST, OPTIONS", allowed[2]["Access-Control-Allow-Methods"]
    denied = call(:rails, "GET", "/api/missing", headers: { "Origin" => "http://localhost:8080.evil" })
    error(denied, 404, "E0004")
    assert_nil denied[2]["Access-Control-Allow-Origin"]
    wrong_method = call(:rails, "DELETE", "/api/auth/login")
    error(wrong_method, 405, "E0005")
    assert_equal "POST, OPTIONS", wrong_method[2]["Allow"]
  end

  # 同一 Email 的跨後端競爭只准一份帳號，錯誤計數也不能因競爭而遺失。
  def test_concurrent_cross_backend_registration_and_failures
    email = "#{@namespace}@example.test"
    # 不用重試重送，兩個原始註冊請求各自保留結果。
    threads = [:rails, :laravel].map do |backend|
      # HTTP 各用自己的連線，不跨執行緒共用 PG client。
      Thread.new do
        call(backend, "POST", "/api/auth/register", body: { email: email, password: "Demo1234", nickname: "競爭" })
      end
    end
    # join.value 會傳回例外，避免背景失敗被誤認為競爭通過。
    results = threads.map do |thread|
      thread.value
    end
    # 比較全部狀態，必須恰好一個成功建立。
    assert_equal [201, 422], results.map { |result| result.first }.sort
    # 找到實際失敗方，核對共同錯誤而不假設哪個後端取得唯一鍵。
    duplicate = results.find { |result| result.first == 422 }
    error(duplicate, 422, "E0001", { "email" => ["此 Email 已註冊"] })
    # 正確登入先開始新世代，並行失敗才有可精確核對的起點。
    with_lock_settings(threshold: 4) do
      login(:rails, [email, "Demo1234"])
      # 用共享設定列序列化各後端的計數判定，不因 HTTP 並行漏計。
      threads = [:rails, :laravel, :rails, :laravel].map do |backend|
        # 每個參與者只提交一次合法格式錯誤密碼。
        Thread.new { call(backend, "POST", "/api/auth/login", body: { email: email, password: "Wrong123" }) }
      end
      # 等所有參與者完成才核對提交後狀態。
      failures = threads.map do |thread|
        thread.value
      end
      # 四個結果必須只有門檻當次為 429，不能漏計或提前鎖定。
      assert_equal [401, 401, 401, 429], failures.map { |result| result.first }.sort
      assert_equal 4, @db.exec_params("SELECT count(*) FROM login_failures JOIN accounts ON accounts.id=account_id WHERE email=$1", [email]).first.fetch("count")
      error(call(:rails, "POST", "/api/auth/login", body: { email: email, password: "Demo1234" }), 429, "E0105")
    end
  end

  # 受控 DB 時鐘只證明精確邊界；live HTTP 鎖定／復原案例仍由前面的測試提供。
  def test_exact_failure_window_and_expiry_boundaries
    # 五個既有事件中只應納入左界內與等於 t 的兩筆；第四次才鎖，才能識別錯誤邊界。
    with_lock_settings(threshold: 4, seconds: 300) do
      actor = member
      data = login(:rails, actor)
      id = data.fetch("user").fetch("id")
      epoch = @db.exec_params("SELECT failure_epoch FROM accounts WHERE id=$1", [id]).first.fetch("failure_epoch")
      time = Time.at(Time.now.to_i).utc
      # t-N 本身與未來事件必須排除；舊事件保留原始時間，不刪除來通過計數。
      [-301, -300, -299, 0, 1].each do |offset|
        owner.exec_params("INSERT INTO login_failures(account_id,failure_epoch,occurred_at) VALUES($1,$2,$3)", [id, epoch, (time + offset).iso8601(6)])
      end
      # 本次為視窗內第三筆；誤計左界或未來事件都會提早回 429。
      at_time(time) do
        error(rack_call("POST", "/api/auth/login", body: { email: actor[0], password: "Wrong123" }), 401, "E0101")
        assert_nil @db.exec_params("SELECT locked_until FROM accounts WHERE id=$1", [id]).first.fetch("locked_until")
        # 同時刻第四筆必須達門檻；漏計等於 t 的事件就無法鎖定。
        result = rack_call("POST", "/api/auth/login", body: { email: actor[0], password: "Wrong123" })
        error(result, 429, "E0105")
        assert_equal "300", result[2]["Retry-After"]
      end
      assert_equal time + 300, @db.exec_params("SELECT locked_until FROM accounts WHERE id=$1", [id]).first.fetch("locked_until")
      assert_equal 7, @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1", [id]).first.fetch("count")
      # 到期前一秒仍鎖住，密碼正確也不能延長或建立登入。
      at_time(time + 299) do
        error(rack_call("POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }), 429, "E0105")
      end
      # 等於 locked_until 必須清鎖並允許登入，不能多鎖一秒。
      at_time(time + 300) do
        assert_equal 200, rack_call("POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }).first
      end
      account = @db.exec_params("SELECT locked_until,failure_epoch FROM accounts WHERE id=$1", [id]).first
      assert_nil account.fetch("locked_until")
      assert_equal epoch + 2, account.fetch("failure_epoch")
    end
  end

  # 八小時等號經公開 Rack 驗證，簽章與登入紀錄仍使用真正登入建立的資料。
  def test_exact_jwt_boundaries_and_invalid_claims
    data = login(:rails, member)
    token = data.fetch("access_token")
    claims = JSON.parse(Base64.urlsafe_decode64(token.split(".")[1]))
    # nbf 等號與 exp 前一秒可用，驗證不會新建／延長任何登入。
    [claims.fetch("iat"), claims.fetch("exp") - 1].each do |now|
      # 該次公開 Rack 呼叫只改外部時間來源，仍讀真實登入紀錄。
      at_time(Time.at(now)) { assert_equal 200, rack_call("GET", "/api/me", token: token).first }
    end
    # nbf 之前及 exp 等號拒絕，不加入任何到期寬限。
    [claims.fetch("iat") - 1, claims.fetch("exp")].each do |now|
      # 認證應在讀取本人資料前拒絕不在有效區間的身分。
      at_time(Time.at(now)) { error(rack_call("GET", "/api/me", token: token), 401, "E0002") }
    end
    own_key = Base64.strict_decode64(File.binread("/run/secrets/rails-jwt.key").strip)
    # 缺少 jti 和其他錯誤各自產生一份簽章有效的憑證，排除只測簽章失敗的假陽性。
    missing_jti = claims.reject { |field, _| field == "jti" }
    variants = [claims.merge("aud" => ["ai-workflow-fullstack-api:rails"]), claims.merge("sub" => "01"),
                claims.merge("iat" => claims.fetch("iat").to_f), claims.merge("nbf" => claims.fetch("iat") + 1),
                claims.merge("exp" => claims.fetch("exp") + 1), missing_jti]
    # 自己的簽章仍不能使錯誤 claims 通過；這些結果不得被 DB session 掩蓋。
    variants.each do |invalid|
      parts = [Base64.urlsafe_encode64('{"alg":"HS256","typ":"JWT"}', padding: false), Base64.urlsafe_encode64(JSON.generate(invalid), padding: false)]
      signed = parts.join(".")
      forged = "#{signed}.#{Base64.urlsafe_encode64(OpenSSL::HMAC.digest('SHA256', own_key, signed), padding: false)}"
      error(call(:rails, "GET", "/api/me", token: forged), 401, "E0002")
    end
    # 錯誤演算法或重複 header 即使簽章正確仍必須拒絕。
    ['{"alg":"none","typ":"JWT"}', '{"alg":"HS256","alg":"HS256","typ":"JWT"}'].each do |header|
      signed = "#{Base64.urlsafe_encode64(header, padding: false)}.#{token.split('.')[1]}"
      forged = "#{signed}.#{Base64.urlsafe_encode64(OpenSSL::HMAC.digest('SHA256', own_key, signed), padding: false)}"
      error(call(:rails, "GET", "/api/me", token: forged), 401, "E0002")
    end
  end

  # 外部金鑰／連線故障經完整 Rack 入口安全失敗，並驗證登入交易沒有部分提交。
  def test_key_and_database_failure_do_not_commit_or_leak
    actor = member
    data = login(:rails, actor)
    token, id = data.fetch("access_token"), data.fetch("user").fetch("id")
    before = @db.exec_params("SELECT failure_epoch FROM accounts WHERE id=$1", [id]).first
    sessions = @db.exec_params("SELECT count(*) FROM login_sessions WHERE account_id=$1", [id]).first.fetch("count")
    original_read = File.method(:binread)
    # 只替換外部金鑰讀取，不改生產檔案；其餘檔案讀取保持原樣。
    invalid_key = lambda do |path, *args|
      path == "/run/secrets/rails-jwt.key" ? "invalid-key\n" : original_read.call(path, *args)
    end
    # 格式正確帳密與可驗證 token 都要回認證設定錯誤，而非誤稱過期。
    File.stub(:binread, invalid_key) do
      error(rack_call("POST", "/api/auth/login", body: { email: actor[0], password: actor[1] }), 500, "E0104")
      error(rack_call("GET", "/api/me", token: token), 500, "E0104")
    end
    assert_equal before, @db.exec_params("SELECT failure_epoch FROM accounts WHERE id=$1", [id]).first
    assert_equal sessions, @db.exec_params("SELECT count(*) FROM login_sessions WHERE account_id=$1", [id]).first.fetch("count")
    # 只有連線系統邊界失敗；錯誤字串刻意含敏感標記，確保共同出口不會序列化它。
    unavailable = lambda do |**_options|
      raise PG::ConnectionBad, "DO_NOT_EXPOSE_DB_CREDENTIAL"
    end
    # 無法查核共用狀態是系統錯誤，不得回 JWT 無效或宣稱登入成功。
    PG.stub(:connect, unavailable) do
      error(rack_call("GET", "/api/me", token: token), 500, "E9999")
    end
    assert_equal 200, call(:rails, "GET", "/api/me", token: token).first
  end

  # 非法 UTF-8 名稱的不同 URL 拼法須共用安全標籤，同時保留真正參數身分。
  def test_cod001_canonical_invalid_query_labels
    cases = {
      "%78%FF=1" => { "x%FF" => ["不支援此欄位"] },
      "%78%FF=1&x%FF=2" => { "x%FF" => ["此參數不可重複"] },
      "x%ff=1&%78%fF=2" => { "x%FF" => ["此參數不可重複"] },
      "%78%2F%FF=1" => { "x%2F%FF" => ["不支援此欄位"] },
      "%78%7E%FF=1" => { "x~%FF" => ["不支援此欄位"] },
      "%E4%B8%AD=1" => { "中" => ["不支援此欄位"] },
      "%78%FF=1&x%25FF=2" => { "x%FF" => ["不支援此欄位"] },
      "%78%FF=1&x%FF=2&x%25FF=3" => { "x%FF" => ["此參數不可重複"] }
    }
    # GET 列表及 POST 無 Query 用途都須輸出相同精確錯誤，Laravel 只作已核對契約的比較方。
    [:laravel, :rails].each do |backend|
      # 每個案例保留原始 percent bytes，不透過 Query builder 重新編碼。
      cases.each do |query, expected|
        error(call(backend, "GET", "/api/articles?page=1&per_page=20&#{query}"), 422, "E0001", expected)
        error(call(backend, "POST", "/api/auth/login?#{query}", body: { email: "#{@namespace}@example.test", password: "Demo1234" }), 422, "E0001", expected)
      end
    end
  end

  # 帳號與 session 必須來自一致快照；停用提交不能混成舊 active／新 revoked 的 401。
  def test_cod002_coherent_auth_snapshot_during_member_disable
    administrator = member(:laravel, "admin")
    owner.exec_params("UPDATE accounts SET role='admin' WHERE email=$1", [administrator[0]])
    admin_token = login(:laravel, administrator).fetch("access_token")
    # 讀取可採停用前快照回 200；登出須在寫鎖後重驗，回已提交的停用錯誤。
    ["GET", "POST"].each_with_index do |method, index|
      actor = member(:rails, "race#{index}")
      data = login(:rails, actor)
      token, id = data.fetch("access_token"), data.fetch("user").fetch("id")
      # 身分 SELECT 已真正完成後，以另一服務的公開 API 完成停用及撤銷交易。
      disable = lambda do
        assert_equal 200, call(:laravel, "PUT", "/api/members/#{id}/status", token: admin_token, body: { status: "disabled" }).first
      end
      # 同步點不依睡眠；舊版在第一次 account 讀取後、新版在完整 JOIN 快照後提交停用。
      raced = after_identity_read(disable) do
        rack_call(method, method == "GET" ? "/api/me" : "/api/auth/logout", token: token)
      end
      error(call(:rails, "GET", "/api/me", token: token), 403, "E0102")
      persisted = @db.exec_params("SELECT accounts.status,login_sessions.revoked_at FROM accounts JOIN login_sessions ON account_id=accounts.id WHERE accounts.id=$1", [id]).first
      assert_equal "disabled", persisted.fetch("status")
      refute_nil persisted.fetch("revoked_at")
      assert_equal 200, call(:laravel, "PUT", "/api/members/#{id}/status", token: admin_token, body: { status: "active" }).first
      error(call(:rails, "GET", "/api/me", token: token), 401, "E0002")
      if method == "GET"
        assert_equal 200, raced.first
        assert_equal data.fetch("user"), raced[1].fetch("data")
      else
        error(raced, 403, "E0102")
      end
    end
  end

  # JOIN 仍須先確認登入身分；帳號停用不能遮蔽錯誤關聯、期限或來源的 401。
  def test_cod002_invalid_session_identity_precedes_disabled_state
    actor = member(:rails, "identity")
    data = login(:rails, actor)
    other = login(:rails, member(:rails, "other"))
    token, id = data.fetch("access_token"), data.fetch("user").fetch("id")
    claims = JSON.parse(Base64.urlsafe_decode64(token.split(".")[1]))
    other_claims = JSON.parse(Base64.urlsafe_decode64(other.fetch("access_token").split(".")[1]))
    # 隔離 fixture 以相同原子提交安排停用與撤銷，不建立生產端點或改變 grants。
    owner.transaction do
      owner.exec_params("UPDATE accounts SET status='disabled' WHERE id=$1", [id])
      owner.exec_params("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1", [id])
    end
    own_key = Base64.strict_decode64(File.binread("/run/secrets/rails-jwt.key").strip)
    invalid = [claims.merge("jti" => other_claims.fetch("jti")),
               claims.merge("jti" => Base64.urlsafe_encode64(SecureRandom.random_bytes(16), padding: false)),
               claims.merge("iat" => claims.fetch("iat") - 1, "nbf" => claims.fetch("nbf") - 1, "exp" => claims.fetch("exp") - 1)]
    # 有效簽章及時間關係仍不能把其他帳號、缺漏或不符 DB 期限的 session 當成可驗身分。
    invalid.each do |variant|
      signed = "#{token.split('.')[0]}.#{Base64.urlsafe_encode64(JSON.generate(variant), padding: false)}"
      forged = "#{signed}.#{Base64.urlsafe_encode64(OpenSSL::HMAC.digest('SHA256', own_key, signed), padding: false)}"
      error(call(:rails, "GET", "/api/me", token: forged), 401, "E0002")
    end
    owner.exec_params("UPDATE login_sessions SET backend_id='laravel' WHERE jti=$1", [claims.fetch("jti")])
    error(call(:rails, "GET", "/api/me", token: token), 401, "E0002")
    owner.exec_params("UPDATE login_sessions SET backend_id='rails' WHERE jti=$1", [claims.fetch("jti")])
    error(call(:rails, "GET", "/api/me", token: token), 403, "E0102")
    owner.exec_params("UPDATE accounts SET status='active' WHERE id=$1", [id])
    error(call(:rails, "GET", "/api/me", token: token), 401, "E0002")
  end
end
