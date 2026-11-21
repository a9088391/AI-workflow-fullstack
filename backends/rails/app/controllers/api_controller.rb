require_relative "../../lib/site_api/writing"

class ApiController < ActionController::Metal
  # 不透過 params；固定路徑／方法先判定，所有錯誤交最外層共同邊界。
  def dispatch_request
    # Rack 保留 percent bytes；只解碼一次，避免 %2531 被錯認成 1。
    path = URI::DEFAULT_PARSER.unescape(request.path.b)
    route = case path
            when "/up" then :health
            when "/api/auth/register" then :register
            when "/api/auth/login" then :login
            when "/api/auth/logout" then :logout
            when "/api/me" then :me
            when "/api/me/articles" then :mine_articles
            when "/api/me/comments" then :mine_comments
            when "/api/articles" then :articles
            when %r{\A/api/articles/([^/]+)/visibility\z} then :article_visibility
            when %r{\A/api/articles/([^/]+)/comments\z} then :comments
            when %r{\A/api/articles/([^/]+)\z} then :article
            when %r{\A/api/comments/([^/]+)/visibility\z} then :comment_visibility
            when %r{\A/api/comments/([^/]+)\z} then :comment
            end
    raise SiteApi::Error.new("E0004") unless route
    methods = case route
              when :register, :login, :logout then %w[POST]
              when :articles, :comments then %w[GET POST]
              when :article, :comment then %w[GET PUT]
              when :article_visibility, :comment_visibility then %w[PUT]
              else %w[GET]
              end
    allow = [*methods, "OPTIONS"].join(", ")
    if request.request_method == "OPTIONS"
      self.status = 204
      headers["access-control-allow-methods"] = allow
      headers["access-control-allow-headers"] = "Authorization, Content-Type, Accept"
      self.response_body = ""
      return
    end
    unless methods.include?(request.request_method)
      # 例外回應由最外層重建，將 Allow 留在請求脈絡，避免隨 controller response 丟失。
      request.env["site_api.allow"] = allow
      raise SiteApi::Error.new("E0005", status: 405)
    end
    input = SiteApi::Input.new(request)
    # 每個用途共用同一連線，受保護寫入自行建立交易；讀取不持有寫鎖。
    data = SiteApi::Database.open do |db|
      execute(route, db, input, path)
    end
    if route == :health
      self.status = 200
      self.content_type = "text/plain"
      self.response_body = "OK"
      return
    end
    self.status = request.request_method == "POST" && %i[register articles comments].include?(route) ? 201 : 200
    self.content_type = "application/json; charset=utf-8"
    self.response_body = JSON.generate(success: true, message: "OK", data: data)
  end

  # 認證／角色→本文／Query→ID→可讀性的固定順序，不受 Rails 自動解析影響。
  def execute(route, db, input, path)
    # 寫入不經讀取分支，避免先解析 Body 或用訪客範圍查目標而破壞認證優先。
    if %w[POST PUT].include?(request.request_method) && %i[articles article comments comment article_visibility comment_visibility].include?(route)
      table = %i[comment comment_visibility comments].include?(route) ? "comments" : "articles"
      action = request.request_method == "POST" ? :create : %i[article_visibility comment_visibility].include?(route) ? :visibility : :edit
      return SiteApi::Writing.execute(request, db, input, table: table, action: action, raw_id: path.split("/")[3])
    end
    case route
    when :health
      SiteApi::Authentication.key
      # 健康檢查只讀既有應用權限可見的業務表；SQL 版本由 Compose 的集中入口門禁保證。
      db.exec("SELECT 1 FROM accounts, login_sessions, login_failures, login_lockout_settings, articles, comments LIMIT 0")
      {}
    when :register, :login
      input.no_query
      body = input.body(route == :register ? %w[email password nickname] : %w[email password])
      route == :register ? SiteApi::Authentication.register(db, body) : SiteApi::Authentication.login(db, body)
    when :logout
      SiteApi::Authentication.logout(request, db, input)
    when :me
      account, = SiteApi::Authentication.identity(request, db)
      input.no_body
      input.no_query
      SiteApi::Authentication.account_dto(account)
    when :mine_articles, :mine_comments
      viewer, = SiteApi::Authentication.identity(request, db)
      page, per_page = input.pagination
      SiteApi::Reading.page(db, viewer, page, per_page, mine: route == :mine_articles ? "articles" : "comments")
    when :comment
      viewer, = SiteApi::Authentication.identity(request, db)
      input.no_body
      input.no_query
      id = input.integer(path.split("/")[3], "comment_id")
      SiteApi::Reading.content_dto(SiteApi::Reading.readable_comment(db, id, viewer), viewer, comment: true)
    when :articles
      management = input.management?
      identity = SiteApi::Authentication.identity(request, db, required: management)
      viewer = identity&.first
      raise SiteApi::Error.new("E0003") if management && viewer["role"] != "admin"
      page, per_page = input.pagination(scope: true)
      # 管理用途留待 Ticket 03；此票不以公開列表冒充管理資料或放寬範圍。
      raise SiteApi::Error.new("E0004") if management
      SiteApi::Reading.page(db, viewer, page, per_page)
    when :article, :comments
      identity = SiteApi::Authentication.identity(request, db, required: false)
      viewer = identity&.first
      if route == :comments
        page, per_page = input.pagination
      else
        input.no_body
        input.no_query
      end
      id = input.integer(path.split("/")[3], "article_id")
      article = SiteApi::Reading.readable_article(db, id, viewer)
      route == :comments ? SiteApi::Reading.page(db, viewer, page, per_page, article_id: id) : SiteApi::Reading.content_dto(article, viewer)
    end
  end
end
