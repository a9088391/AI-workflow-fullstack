require_relative "http_test"
require "timeout"

# 僅測試程序觀察真實 SQL 與鎖同步點，不在伺服器安裝測試端點或改寫查詢結果。
module ContentSqlObservation
  # 連線設定／交易指令亦計入實際 statement 數，避免只數資料 SELECT 而低估成本。
  def exec(sql, ...)
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    result = super
    Thread.current[:rails02_sql]&.push([sql, [], Process.clock_gettime(Process::CLOCK_MONOTONIC) - started])
    result
  end

  # 保留 SQL／綁定參數及耗時，讓公開 Rack 列表的實際查詢可重播 EXPLAIN。
  def exec_params(sql, parameters, ...)
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    result = super
    Thread.current[:rails02_sql]&.push([sql, parameters, Process.clock_gettime(Process::CLOCK_MONOTONIC) - started])
    hook = Thread.current[:rails02_after_sql]
    hook&.call(sql)
    result
  end
end
PG::Connection.prepend(ContentSqlObservation)

class HttpTest
  # 一般寫入必須回精確空 object，不能以省略資料或空陣列替代共同契約。
  def content_success(result, status = 200)
    assert_equal [status, { "success" => true, "message" => "OK", "data" => {} }], result.first(2)
  end

  # 從公開登入建立同帳號的跨後端身分，角色 fixture 只在本票隔離庫調整。
  def content_actor(suffix, admin: false)
    actor = member(:rails, suffix)
    owner.exec_params("UPDATE accounts SET role='admin' WHERE email=$1", [actor.first]) if admin
    { rails: login(:rails, actor), laravel: login(:laravel, actor) }
  end

  # 以公開建立與我的列表取得新文章 ID，不猜 sequence，也不以 SQL 插入互通資料。
  def content_article(actor, backend = :rails)
    token = actor.fetch(backend).fetch("access_token")
    content_success(call(backend, "POST", "/api/articles", token: token, body: { title: "#{@namespace}😀", body: " 保留\r\n\r\n<text>\n " }), 201)
    call(backend, "GET", "/api/me/articles?page=1&per_page=100", token: token)[1].fetch("data").fetch("list").first
  end

  # 留言同樣以 API 建立與本人列表識別，保留建立來源的證據鏈。
  def content_comment(actor, article_id, backend = :rails)
    token = actor.fetch(backend).fetch("access_token")
    content_success(call(backend, "POST", "/api/articles/#{article_id}/comments", token: token, body: { body: " 留言😀\r\n\r\n " }), 201)
    call(backend, "GET", "/api/me/comments?page=1&per_page=100", token: token)[1].fetch("data").fetch("list").first
  end

  # 檢查完整 DTO 欄位與型態，防止 JOIN 的內部欄位或父文章文字外洩。
  def content_dto(row, author, comment: false)
    expected = %w[id body author version created_backend created_at updated_at visibility can_restore] + (comment ? %w[article_id] : %w[title])
    assert_equal expected.sort, row.keys.sort
    assert_kind_of Integer, row.fetch("id")
    assert_kind_of Integer, row.fetch("version")
    assert_equal({ "id" => author.fetch("id"), "nickname" => author.fetch("nickname") }, row.fetch("author"))
    assert_kind_of String, row.fetch("body")
    assert_kind_of String, row.fetch("created_backend")
    assert_includes %w[visible hidden], row.fetch("visibility")
    assert_includes [true, false], row.fetch("can_restore")
    # 每個時間都是共同台北秒級格式，而非 Rails 預設 ISO8601。
    %w[created_at updated_at].each do |field|
      assert_match(/\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\z/, row.fetch(field))
    end
    assert_kind_of(comment ? Integer : String, row.fetch(comment ? "article_id" : "title"))
  end

  # 對照 article_http/comment_http/lifecycle_http：兩方向跨後端讀、編修、下架、恢復不改建立來源。
  def test_content_cross_backend_round_trips_and_dto
    actor = content_actor("cross")
    # 每個建立來源都由另一後端合法寫回，原後端再讀回相同資料。
    [:rails, :laravel].each do |origin|
      editor = origin == :rails ? :laravel : :rails
      original = content_article(actor, origin)
      comment = content_comment(actor, original.fetch("id"), origin)
      # 文章與留言都必須保留完整 DTO 及實際原始換行。
      [["articles", original], ["comments", comment]].each do |table, row|
        path = "/api/#{table}/#{row.fetch('id')}"
        token = actor.fetch(editor).fetch("access_token")
        read = call(editor, "GET", path, token: token)[1].fetch("data")
        assert_equal row, read
        content_dto(read, actor.fetch(:rails).fetch("user"), comment: table == "comments")
        assert_equal origin.to_s, read.fetch("created_backend")
        assert_equal read.fetch("created_at"), read.fetch("updated_at")
        body = { body: "更新\r\n😀", version: 1 }
        body[:title] = "更新標題" if table == "articles"
        content_success(call(editor, "PUT", path, token: token, body: body))
        content_success(call(editor, "PUT", "#{path}/visibility", token: token, body: { version: 2, visibility: "hidden" }))
        hidden = call(origin, "GET", path, token: actor.fetch(origin).fetch("access_token"))[1].fetch("data")
        assert_equal ["hidden", true, 3, origin.to_s], hidden.values_at("visibility", "can_restore", "version", "created_backend")
        # 隱藏文章期間不編留言本文；恢復後再處理下一筆，讓兩種內容各自走完整週期。
        content_success(call(editor, "PUT", "#{path}/visibility", token: token, body: { version: 3, visibility: "visible" }))
        restored = call(origin, "GET", path, token: actor.fetch(origin).fetch("access_token"))[1].fetch("data")
        assert_equal ["visible", false, 4, origin.to_s, "更新\r\n😀"], restored.values_at("visibility", "can_restore", "version", "created_backend", "body")
        assert_equal row.fetch("created_at"), restored.fetch("created_at")
      end
    end
  end

  # 對照生命週期共用案例，確認權限先於版本、版本先於同值及 admin 恢復控制。
  def test_content_permissions_noop_versions_and_admin_control
    actor, administrator, stranger = content_actor("author"), content_actor("admin", admin: true), content_actor("stranger")
    article = content_article(actor)
    comment = content_comment(actor, article.fetch("id"))
    # 同一權限／版本規則必須同時適用文章與留言。
    [["articles", article], ["comments", comment]].each do |table, row|
      path = "/api/#{table}/#{row.fetch('id')}"
      # 分別取三種角色的真實憑證，讓同一內容依序驗證不同操作資格。
      token, admin, other = [actor, administrator, stranger].map do |identity|
        identity.fetch(:rails).fetch("access_token")
      end
      same = { body: row.fetch("body"), version: 1 }
      same[:title] = row.fetch("title") if table == "articles"
      before = @db.exec_params("SELECT * FROM #{table} WHERE id=$1", [row.fetch("id")]).first
      content_success(call(:rails, "PUT", path, token: token, body: same))
      content_success(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 1, visibility: "visible" }))
      assert_equal before, @db.exec_params("SELECT * FROM #{table} WHERE id=$1", [row.fetch("id")]).first
      error(call(:rails, "PUT", path, token: admin, body: same.merge(version: 999)), 403, "E0003")
      error(call(:rails, "PUT", path, token: other, body: same), 403, "E0003")
      content_success(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 1, visibility: "hidden" }))
      error(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 1, visibility: "hidden" }), 409, "E0201")
      error(call(:rails, "PUT", path, token: other, body: same), 404, "E0004")
      # 修改下架內容保留 hidden 與 author 控制，不偷偷恢復。
      content_success(call(:rails, "PUT", path, token: token, body: same.merge(body: "隱藏編修", version: 2)))
      hidden = call(:rails, "GET", path, token: token)[1].fetch("data")
      assert_equal ["hidden", true, 3], hidden.values_at("visibility", "can_restore", "version")
      content_success(call(:rails, "PUT", "#{path}/visibility", token: admin, body: { version: 3, visibility: "hidden" }))
      error(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 1, visibility: "visible" }), 403, "E0003")
      controlled = @db.exec_params("SELECT * FROM #{table} WHERE id=$1", [row.fetch("id")]).first
      content_success(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 4, visibility: "hidden" }))
      assert_equal controlled, @db.exec_params("SELECT * FROM #{table} WHERE id=$1", [row.fetch("id")]).first
      assert_equal false, call(:rails, "GET", path, token: token)[1].fetch("data").fetch("can_restore")
      assert_equal true, call(:rails, "GET", path, token: admin)[1].fetch("data").fetch("can_restore")
      content_success(call(:rails, "PUT", "#{path}/visibility", token: admin, body: { version: 4, visibility: "visible" }))
      assert_equal ["visible", nil, 5], @db.exec_params("SELECT * FROM #{table} WHERE id=$1", [row.fetch("id")]).first.values_at("visibility", "restore_control", "version")
      error(call(:rails, "PUT", path, token: token, body: same.merge(body: "隱藏編修", version: 3)), 409, "E0201")
    end
    own = content_article(administrator)
    content_success(call(:rails, "PUT", "/api/articles/#{own.fetch('id')}/visibility", token: administrator.fetch(:rails).fetch("access_token"), body: { version: 1, visibility: "hidden" }))
    assert_equal "author", @db.exec_params("SELECT restore_control FROM articles WHERE id=$1", [own.fetch("id")]).first.fetch("restore_control")
  end

  # 我的留言與詳情獨立於父文，父文 hidden 仍阻擋新增／改本文，visibility 可依權限操作。
  def test_content_hidden_parent_and_own_content_boundaries
    parent_author, commenter, administrator = content_actor("parent"), content_actor("commenter"), content_actor("admin", admin: true)
    article = content_article(parent_author)
    id = article.fetch("id")
    comment = content_comment(commenter, id)
    # 各角色各用自己的登入，避免把父文預覽權與留言作者資格混用。
    token, parent, admin = [commenter, parent_author, administrator].map do |actor|
      actor.fetch(:rails).fetch("access_token")
    end
    path = "/api/comments/#{comment.fetch('id')}"
    error(call(:rails, "GET", path), 401, "E0002")
    error(call(:rails, "GET", path, token: parent), 403, "E0003")
    content_success(call(:rails, "PUT", "/api/articles/#{id}/visibility", token: parent, body: { version: 1, visibility: "hidden" }))
    error(call(:rails, "GET", "/api/articles/#{id}", token: token), 404, "E0004")
    assert_equal comment, call(:rails, "GET", path, token: token)[1].fetch("data")
    mine = call(:rails, "GET", "/api/me/comments?page=1&per_page=20", token: token)[1].fetch("data")
    assert_equal [comment], mine.fetch("list")
    refute_includes JSON.generate(mine), article.fetch("title")
    error(call(:rails, "PUT", path, token: token, body: { body: "不能修改", version: 999 }), 403, "E0003")
    # 無父文預覽權的人得到 404；作者與管理員預覽者得到 403。
    [[token, 404, "E0004"], [parent, 403, "E0003"], [admin, 403, "E0003"]].each do |credential, status, code|
      error(call(:rails, "POST", "/api/articles/#{id}/comments", token: credential, body: { body: "不得新增" }), status, code)
    end
    content_success(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 1, visibility: "hidden" }))
    error(call(:rails, "GET", path, token: parent), 404, "E0004")
    content_success(call(:rails, "PUT", "#{path}/visibility", token: token, body: { version: 2, visibility: "visible" }))
    assert_equal 1, @db.exec_params("SELECT count(*) FROM comments WHERE article_id=$1", [id]).first.fetch("count")
    assert_equal comment.fetch("body"), call(:rails, "GET", path, token: admin)[1].fetch("data").fetch("body")
    own_hidden = call(:rails, "GET", "/api/me/articles?page=1&per_page=20", token: parent)[1].fetch("data").fetch("list")
    # 只投影識別碼與狀態，精確核對本人列表仍包含下架父文。
    assert_equal [[id, "hidden"]], (own_hidden.map do |row|
      row.values_at("id", "visibility")
    end)
  end

  # 共用輸入案例逐欄重播所有寫入用途，並以持久化前後資料確認拒絕沒有副作用。
  def test_content_input_contract_and_validation_order
    actor = content_actor("input")
    token = actor.fetch(:rails).fetch("access_token")
    article = content_article(actor)
    id = article.fetch("id")
    comment = content_comment(actor, id)
    cid = comment.fetch("id")
    operations = [
      ["POST", "/api/articles", { title: "標題", body: "文章" }],
      ["PUT", "/api/articles/#{id}", { title: "標題", body: "文章", version: 1 }],
      ["PUT", "/api/articles/#{id}/visibility", { visibility: "visible", version: 1 }],
      ["POST", "/api/articles/#{id}/comments", { body: "留言" }],
      ["PUT", "/api/comments/#{cid}", { body: "留言", version: 1 }],
      ["PUT", "/api/comments/#{cid}/visibility", { visibility: "visible", version: 1 }]
    ]
    before = @db.exec_params("SELECT (SELECT count(*) FROM articles WHERE author_id=$1) AS a,(SELECT count(*) FROM comments WHERE author_id=$1) AS c", [actor.fetch(:rails).fetch("user").fetch("id")]).first
    # 每個端點都先認證，再 Query／Path／Body，不讓 malformed JSON 遮蔽 401。
    operations.each do |method, path, valid|
      error(call(:rails, method, "#{path}?unknown=1", raw: "{"), 401, "E0002")
      error(call(:rails, method, path, token: token, raw: "{"), 400, "E0005")
      error(call(:rails, method, path, token: token, raw: "[]"), 422, "E0001", { "_body" => ["Request Body 必須為 JSON object"] })
      # 缺漏、null、型態與唯讀欄位均不能被框架忽略或轉型。
      valid.each_key do |field|
        # 每次只移除目標欄位，才能分辨缺漏與其他欄位錯誤。
        missing = valid.reject do |name, _|
          name == field
        end
        error(call(:rails, method, path, token: token, body: missing), 422, "E0001", { field.to_s => [SiteApi::Input::REQUIRED] })
        error(call(:rails, method, path, token: token, body: valid.merge(field => nil)), 422, "E0001", { field.to_s => ["此欄位不可為 null"] })
        error(call(:rails, method, path, token: token, body: valid.merge(field => [])), 422, "E0001", { field.to_s => [SiteApi::Input::TYPE] })
      end
      # 逐一送入唯讀或未知欄位，確認每種用途都拒絕額外資料。
      %w[id author author_id article_id created_backend created_at updated_at can_restore extra].each do |field|
        error(call(:rails, method, path, token: token, body: valid.merge(field => "forbidden")), 422, "E0001", { field => ["不支援此欄位"] })
      end
      error(call(:rails, method, "#{path}?x=1&x=2", token: token, body: valid), 422, "E0001", { "x" => ["此參數不可重複"] })
      if valid.key?(:visibility)
        # 狀態只接受精確列舉；同時違規的欄位須完整回報，不能只留下第一項錯誤。
        ["HIDDEN", ""].each do |visibility|
          error(call(:rails, method, path, token: token, body: valid.merge(visibility: visibility)), 422, "E0001", { "visibility" => [SiteApi::Input::INVALID] })
        end
        error(call(:rails, method, path, token: token, body: { visibility: nil, version: "1" }), 422, "E0001",
              { "visibility" => ["此欄位不可為 null"], "version" => [SiteApi::Input::TYPE] })
      end
      next unless valid.key?(:version)
      # JSON 數字的型態與範圍分開驗證，max 是合法輸入，僅因舊版本回 409。
      [true, false, "1", 1.0, {}].each do |version|
        error(call(:rails, method, path, token: token, body: valid.merge(version: version)), 422, "E0001", { "version" => [SiteApi::Input::TYPE] })
      end
      # 合法整數型態仍須拒絕範圍外版本，不能與型態錯誤混為一談。
      [0, -1, SiteApi::Input::MAX_ID + 1].each do |version|
        error(call(:rails, method, path, token: token, body: valid.merge(version: version)), 422, "E0001", { "version" => [SiteApi::Input::INVALID] })
      end
      error(call(:rails, method, path, token: token, body: valid.merge(version: SiteApi::Input::MAX_ID)), 409, "E0201")
    end
    assert_equal before, @db.exec_params("SELECT (SELECT count(*) FROM articles WHERE author_id=$1) AS a,(SELECT count(*) FROM comments WHERE author_id=$1) AS c", [actor.fetch(:rails).fetch("user").fetch("id")]).first
    assert_equal article, call(:rails, "GET", "/api/articles/#{id}", token: token)[1].fetch("data")
    assert_equal comment, call(:rails, "GET", "/api/comments/#{cid}", token: token)[1].fetch("data")
    # URL 允許前導零但不接受 JSON 數字語法；合法不存在的最大 ID 與 1 都走可讀性判斷。
    assert_equal article, call(:rails, "GET", "/api/articles/000#{id}")[1].fetch("data")
    ["0", "+1", "-1", "1.0", "1e0", "9007199254740992"].each do |invalid|
      error(call(:rails, "PUT", "/api/articles/#{invalid}", token: token, body: { title: "t", body: "b", version: 1 }), 422, "E0001", { "article_id" => [SiteApi::Input::INVALID] })
      error(call(:rails, "PUT", "/api/comments/#{invalid}", token: token, body: { body: "b", version: 1 }), 422, "E0001", { "comment_id" => [SiteApi::Input::INVALID] })
    end
    error(call(:rails, "PUT", "/api/articles/#{SiteApi::Input::MAX_ID}", token: token, body: { title: "t", body: "b", version: 1 }), 404, "E0004")
    error(call(:rails, "PUT", "/api/comments/#{SiteApi::Input::MAX_ID}", token: token, body: { body: "b", version: 1 }), 404, "E0004")
    # 兩種自己的列表與詳情皆先登入，之後才拒絕 GET Body 與未知 Query。
    ["/api/me/articles?page=1&per_page=20", "/api/me/comments?page=1&per_page=20", "/api/comments/#{cid}"].each do |path|
      error(call(:rails, "GET", path, raw: "{"), 401, "E0002")
      error(call(:rails, "GET", path, token: token, raw: "{}"), 422, "E0001", { "_body" => ["此 API 不接受 Request Body"] })
    end
  end

  # 碼點長度與固定空白集合不依 Ruby trim，原樣換行、NUL 與 Unicode 邊界皆由 HTTP 驗證。
  def test_content_unicode_limits_blank_set_and_raw_preservation
    actor = content_actor("unicode")
    token = actor.fetch(:rails).fetch("access_token")
    article = content_article(actor)
    parent = "/api/articles/#{article.fetch('id')}/comments"
    cases = [["/api/articles", :title, 200, { title: "t", body: "b" }],
             ["/api/articles", :body, 20_000, { title: "t", body: "b" }],
             [parent, :body, 2000, { body: "b" }]]
    blanks = ([*0x09..0x0d, 0x20, 0x85, 0xa0, 0x1680, *0x2000..0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000]).pack("U*")
    # 先確認測試輸入是實際碼點，避免把反斜線字串當成空白／NUL 測試。
    assert_equal 25, blanks.codepoints.length
    # 各文字欄位依自己的上限測試，避免共用驗證誤套文章或留言長度。
    cases.each do |path, field, maximum, valid|
      # 一碼點與最大長度皆須成功，並先確認 JSON 沒改寫測試值。
      [1, maximum].each do |length|
        value = "😀" * length
        assert_equal length, JSON.parse(JSON.generate(valid.merge(field => value))).fetch(field.to_s).codepoints.length
        content_success(call(:rails, "POST", path, token: token, body: valid.merge(field => value)), 201)
      end
      # 空值、超長、全空白與實際 NUL 均須在寫入前拒絕。
      ["", "😀" * (maximum + 1), blanks, "x\0y"].each do |value|
        error(call(:rails, "POST", path, token: token, body: valid.merge(field => value)), 422, "E0001", { field.to_s => [SiteApi::Input::INVALID] })
      end
      # 集合內每個字元單獨也須拒絕，集合外零寬空白依契約仍是合法文字。
      blanks.each_char do |blank|
        error(call(:rails, "POST", path, token: token, body: valid.merge(field => blank)), 422, "E0001", { field.to_s => [SiteApi::Input::INVALID] })
      end
      content_success(call(:rails, "POST", path, token: token, body: valid.merge(field => "\u200b")), 201)
    end
    value = " \r\n😀e\u0301\r\n<markdown>\n "
    raw = JSON.generate(title: value, body: value)
    assert_equal value.codepoints, JSON.parse(raw).fetch("body").codepoints
    content_success(call(:rails, "POST", "/api/articles", token: token, raw: raw), 201)
    row = call(:rails, "GET", "/api/me/articles?page=1&per_page=1", token: token)[1].fetch("data").fetch("list").first
    assert_equal value, row.fetch("title")
    assert_equal value, row.fetch("body")
  end

  # 觀察真實 PostgreSQL blocker，不以睡眠推測請求已到達預期鎖點。
  def content_wait_for_lock(blocker, count = 1)
    # 同步點最多等八秒，未觀察到真實重疊便失敗，避免無限等候。
    Timeout.timeout(8) do
      # 每輪重新讀取等待鏈，讓條件由實際鎖關係而非經過時間決定。
      loop do
        # 持鎖交易會保留活動快照；每次重新觀察，才能看到後來建立的 HTTP 連線。
        owner.exec("SELECT pg_stat_clear_snapshot()")
        # 同列的後續請求可能等待前一請求的 tuple lock；沿等待鏈計數並去重，才不會漏掉已重疊的請求。
        waiting = owner.exec_params(<<~SQL, [blocker]).first.fetch("count").to_i
          WITH RECURSIVE waiting(pid) AS (
            SELECT pid FROM pg_stat_activity WHERE $1::integer=ANY(pg_blocking_pids(pid))
            UNION
            SELECT activity.pid FROM pg_stat_activity activity
            JOIN waiting ON waiting.pid=ANY(pg_blocking_pids(activity.pid))
          )
          SELECT count(*) FROM waiting
        SQL
        return if waiting >= count
        Thread.pass
      end
    end
  end

  # 回傳真正在指定 blocker 上等待的應用連線，供下一個公開操作確認相反順序的重疊。
  def content_waiting_pid(blocker)
    content_wait_for_lock(blocker)
    owner.exec_params("SELECT pid FROM pg_stat_activity WHERE $1::integer=ANY(pg_blocking_pids(pid)) ORDER BY pid LIMIT 1", [blocker]).first.fetch("pid").to_i
  end

  # 同版本的 HTTP 寫入受控重疊，必須恰一成功、一衝突，完整資料只能來自成功請求。
  def test_content_concurrent_same_version_with_rails_and_laravel
    threads = nil
    actor = content_actor("versionrace")
    # 同後端與跨後端都以同一帳號鎖排隊，不能錯把不同請求先後啟動當成已重疊。
    [[:rails, :rails], [:rails, :laravel]].each do |backends|
      article = content_article(actor)
      comment = content_comment(actor, article.fetch("id"))
      # 文章與留言分別重播相同版本競爭，避免只驗其中一條寫入路徑。
      [["articles", article], ["comments", comment]].each do |table, row|
        owner.exec("BEGIN")
        owner.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [actor.fetch(:rails).fetch("user").fetch("id")])
        blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
        # 保存每個送出者與結果位置，才能從實際勝者核對完整持久化資料。
        threads = backends.each_with_index.map do |backend, index|
          # 請求各只送一次，排隊後才釋放鎖，所有錯誤從 Thread#value 傳回測試。
          Thread.new do
            body = { body: "#{backend}-#{index}", version: 1 }
            body[:title] = "#{backend}-#{index}" if table == "articles"
            call(backend, "PUT", "/api/#{table}/#{row.fetch('id')}", token: actor.fetch(backend).fetch("access_token"), body: body)
          end
        end
        begin
          content_wait_for_lock(blocker, 2)
        ensure
          owner.exec("COMMIT")
        end
        # 收齊所有請求結果並傳回背景例外，不把執行緒失敗當成版本衝突。
        results = threads.map do |thread|
          thread.value
        end
        # 只比較狀態集合，不預設哪個後端先取得鎖。
        assert_equal [200, 409], results.map { |result| result.first }.sort
        # 精確檢查實際失敗方的共同錯誤格式，避免僅狀態相同的假陽性。
        failure = results.find do |result|
          result.first == 409
        end
        error(failure, 409, "E0201")
        # 由成功結果定位原始請求內容，確保資料沒有混合兩次更新。
        winner = results.index do |result|
          result.first == 200
        end
        saved = @db.exec_params("SELECT * FROM #{table} WHERE id=$1", [row.fetch("id")]).first
        assert_equal [2, "#{backends[winner]}-#{winner}", "rails"], saved.values_at("version", "body", "created_backend")
        assert_equal saved.fetch("body"), saved.fetch("title") if table == "articles"
      end
    end
  ensure
    owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
    # 即使同步點失敗仍收齊已送出的請求，不重送未知結果的寫入。
    threads&.each do |thread|
      thread.join
    end
  end

  # 認證外層完成後撤銷／停用先提交，鎖內重驗須拒絕，且內容沒有部分變更。
  def test_content_revocation_before_lock_and_rollback_on_failure
    administrator = content_actor("disableadmin", admin: true)
    # 每種撤銷來源都先提交，再由受保護寫入鎖內重驗拒絕。
    %i[logout revoke disable].each do |operation|
      actor = content_actor(operation.to_s)
      article = content_article(actor)
      comment = content_comment(actor, article.fetch("id"))
      token = actor.fetch(:rails).fetch("access_token")
      paths = [["POST", "/api/articles", { title: "新", body: "新" }],
               ["PUT", "/api/articles/#{article.fetch('id')}", { title: "新", body: "新", version: 1 }],
               ["PUT", "/api/comments/#{comment.fetch('id')}", { body: "新", version: 1 }]]
      # 每個寫入各用新的登入；logout/revoke 只撤銷自己的 session，disable 復原不復活 token。
      paths.each do |method, path, body|
        fresh = login(:rails, [actor.fetch(:rails).fetch("user").fetch("email"), "Demo1234"]).fetch("access_token")
        before = @db.exec_params("SELECT (SELECT count(*) FROM articles WHERE author_id=$1) AS a,(SELECT count(*) FROM comments WHERE author_id=$1) AS c", [article.fetch("author").fetch("id")]).first
        # 外層身分讀取完成後、寫入取鎖前提交撤銷，讓鎖內重驗必須看見新狀態。
        callback = lambda do
          case operation
          when :logout
            content_success(call(:rails, "POST", "/api/auth/logout", token: fresh))
          when :revoke
            claims = JSON.parse(Base64.urlsafe_decode64(fresh.split(".")[1]))
            owner.exec_params("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE jti=$1", [claims.fetch("jti")])
          when :disable
            content_success(call(:laravel, "PUT", "/api/members/#{article.fetch('author').fetch('id')}/status", token: administrator.fetch(:laravel).fetch("access_token"), body: { status: "disabled" }))
          end
        end
        # 從公開 Rack 入口觸發上述同步點，驗證不能沿用撤銷前的認證結果。
        raced = after_identity_read(callback) do
          rack_call(method, path, token: fresh, body: body)
        end
        error(raced, operation == :disable ? 403 : 401, operation == :disable ? "E0102" : "E0002")
        assert_equal before, @db.exec_params("SELECT (SELECT count(*) FROM articles WHERE author_id=$1) AS a,(SELECT count(*) FROM comments WHERE author_id=$1) AS c", [article.fetch("author").fetch("id")]).first
        assert_equal [1, article.fetch("body")], @db.exec_params("SELECT version,body FROM articles WHERE id=$1", [article.fetch("id")]).first.values_at("version", "body")
        assert_equal [1, comment.fetch("body")], @db.exec_params("SELECT version,body FROM comments WHERE id=$1", [comment.fetch("id")]).first.values_at("version", "body")
        if operation == :disable
          content_success(call(:laravel, "PUT", "/api/members/#{article.fetch('author').fetch('id')}/status", token: administrator.fetch(:laravel).fetch("access_token"), body: { status: "active" }))
        end
        error(call(:rails, "GET", "/api/me", token: fresh), 401, "E0002")
      end
    end
    actor = content_actor("rollback")
    article = content_article(actor)
    before = @db.exec_params("SELECT * FROM articles WHERE id=$1", [article.fetch("id")]).first
    # UPDATE 真正執行後拋外部故障，公開出口須回 500 並由同交易回滾。
    hook = lambda do |sql|
      raise PG::ConnectionBad, "TEST_DO_NOT_EXPOSE" if sql.start_with?("UPDATE articles SET")
    end
    Thread.current[:rails02_after_sql] = hook
    result = rack_call("PUT", "/api/articles/#{article.fetch('id')}", token: actor.fetch(:rails).fetch("access_token"), body: { title: "未提交", body: "未提交", version: 1 })
    error(result, 500, "E9999")
    assert_equal before, @db.exec_params("SELECT * FROM articles WHERE id=$1", [article.fetch("id")]).first
  ensure
    Thread.current[:rails02_after_sql] = nil
  end

  # 父文章下架先提交時，等待同一文章鎖的留言建立／編修須讀新狀態並拒絕。
  def test_content_parent_lock_checks_committed_state
    thread = nil
    actor = content_actor("parentrace")
    # 建立與編修都必須等待同一父文鎖後才判斷狀態。
    [:create, :edit].each do |operation|
      article = content_article(actor)
      comment = content_comment(actor, article.fetch("id"))
      owner.exec("BEGIN")
      owner.exec_params("SELECT id FROM articles WHERE id=$1 FOR UPDATE", [article.fetch("id")])
      blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
      # 公開留言寫入在 owner 的父文鎖上等待，待下架提交後才繼續。
      thread = Thread.new do
        if operation == :create
          call(:rails, "POST", "/api/articles/#{article.fetch('id')}/comments", token: actor.fetch(:rails).fetch("access_token"), body: { body: "不得新增" })
        else
          call(:rails, "PUT", "/api/comments/#{comment.fetch('id')}", token: actor.fetch(:rails).fetch("access_token"), body: { body: "不得編修", version: 1 })
        end
      end
      begin
        content_wait_for_lock(blocker)
        owner.exec_params("UPDATE articles SET visibility='hidden',restore_control='author',version=version+1,updated_at=clock_timestamp() WHERE id=$1", [article.fetch("id")])
      ensure
        owner.exec("COMMIT")
      end
      error(thread.value, 403, "E0003")
      assert_equal 1, @db.exec_params("SELECT count(*) FROM comments WHERE article_id=$1", [article.fetch("id")]).first.fetch("count")
      assert_equal [comment.fetch("body"), 1], @db.exec_params("SELECT body,version FROM comments WHERE id=$1", [comment.fetch("id")]).first.values_at("body", "version")
    end
  ensure
    owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
    thread&.join
  end

  # 寫入先取得帳號鎖時，登出／停用只能等完整內容提交；後續舊 token 必須失效。
  def test_content_write_commits_before_waiting_logout_or_disable
    writer = revoker = nil
    administrator = content_actor("commitadmin", admin: true)
    # 登出與跨後端停用都須等待先取帳號鎖的寫入提交。
    %i[logout disable].each do |revocation|
      # 三種內容操作各自驗證完整提交，不以其中一種結果代表全部。
      %i[article_edit comment_create comment_edit].each do |operation|
        actor = content_actor("#{revocation}#{operation}")
        article = content_article(actor)
        comment = content_comment(actor, article.fetch("id"))
        token = actor.fetch(:rails).fetch("access_token")
        owner.exec("BEGIN")
        owner.exec_params("SELECT id FROM articles WHERE id=$1 FOR UPDATE", [article.fetch("id")])
        blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
        # 先送寫入使其持有帳號鎖、等待 owner 的文章鎖，再安排撤銷排隊。
        writer = Thread.new do
          case operation
          when :article_edit
            call(:rails, "PUT", "/api/articles/#{article.fetch('id')}", token: token, body: { title: "完整提交", body: "完整提交", version: 1 })
          when :comment_create
            call(:rails, "POST", "/api/articles/#{article.fetch('id')}/comments", token: token, body: { body: "完整提交" })
          when :comment_edit
            call(:rails, "PUT", "/api/comments/#{comment.fetch('id')}", token: token, body: { body: "完整提交", version: 1 })
          end
        end
        begin
          writer_pid = content_waiting_pid(blocker)
          # 已確認寫入正在等文章鎖後才送撤銷，並觀察撤銷受寫入帳號鎖阻擋。
          revoker = Thread.new do
            if revocation == :logout
              call(:rails, "POST", "/api/auth/logout", token: token)
            else
              call(:laravel, "PUT", "/api/members/#{article.fetch('author').fetch('id')}/status", token: administrator.fetch(:laravel).fetch("access_token"), body: { status: "disabled" })
            end
          end
          content_wait_for_lock(writer_pid)
        ensure
          owner.exec("COMMIT")
        end
        content_success(writer.value, operation == :comment_create ? 201 : 200)
        content_success(revoker.value)
        error(call(:rails, "GET", "/api/me", token: token), revocation == :disable ? 403 : 401, revocation == :disable ? "E0102" : "E0002")
        if operation == :article_edit
          assert_equal ["完整提交", "完整提交", 2], @db.exec_params("SELECT title,body,version FROM articles WHERE id=$1", [article.fetch("id")]).first.values_at("title", "body", "version")
        elsif operation == :comment_edit
          assert_equal ["完整提交", 2], @db.exec_params("SELECT body,version FROM comments WHERE id=$1", [comment.fetch("id")]).first.values_at("body", "version")
        else
          assert_equal 2, @db.exec_params("SELECT count(*) FROM comments WHERE article_id=$1", [article.fetch("id")]).first.fetch("count")
          assert_equal 1, @db.exec_params("SELECT count(*) FROM comments WHERE article_id=$1 AND body='完整提交' AND version=1", [article.fetch("id")]).first.fetch("count")
        end
      end
    end
  ensure
    owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
    writer&.join
    revoker&.join
  end

  # 留言先於另一後端下架文章取得文章鎖時，可先完整提交，之後隨父文章對外隱藏。
  def test_content_comment_commits_before_cross_backend_parent_hide
    writer = hider = nil
    actor, administrator = content_actor("beforehide"), content_actor("hideadmin", admin: true)
    # 兩種留言本文寫入都須能先提交，再隨父文下架而對外隱藏。
    [:create, :edit].each do |operation|
      article = content_article(actor)
      comment = content_comment(actor, article.fetch("id"))
      owner.exec("BEGIN")
      owner.exec_params("SELECT id FROM articles WHERE id=$1 FOR UPDATE", [article.fetch("id")])
      blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
      # 先讓留言寫入排入文章鎖等待鏈，確立它早於後續下架請求。
      writer = Thread.new do
        if operation == :create
          call(:rails, "POST", "/api/articles/#{article.fetch('id')}/comments", token: actor.fetch(:rails).fetch("access_token"), body: { body: "先提交" })
        else
          call(:rails, "PUT", "/api/comments/#{comment.fetch('id')}", token: actor.fetch(:rails).fetch("access_token"), body: { body: "先提交", version: 1 })
        end
      end
      begin
        writer_pid = content_waiting_pid(blocker)
        # 留言已排隊後才由 Laravel 下架，觀察下架等待留言而非猜測請求順序。
        hider = Thread.new do
          call(:laravel, "PUT", "/api/articles/#{article.fetch('id')}/visibility", token: administrator.fetch(:laravel).fetch("access_token"), body: { visibility: "hidden", version: 1 })
        end
        content_wait_for_lock(writer_pid)
      ensure
        owner.exec("COMMIT")
      end
      content_success(writer.value, operation == :create ? 201 : 200)
      content_success(hider.value)
      error(call(:rails, "GET", "/api/articles/#{article.fetch('id')}/comments?page=1&per_page=100"), 404, "E0004")
      rows = call(:rails, "GET", "/api/me/comments?page=1&per_page=100", token: actor.fetch(:rails).fetch("access_token"))[1].fetch("data").fetch("list")
      # 只計本次父文的成功本文，排除同作者其他案例留下的留言。
      assert_equal 1, (rows.count do |row|
        row.fetch("article_id") == article.fetch("id") && row.fetch("body") == "先提交"
      end)
    end
  ensure
    owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
    writer&.join
    hider&.join
  end

  # 精確個人集合、相同建立時間的 ID 次序與深頁，以實際公開入口 SQL／計畫證明沒有 N+1。
  def test_content_own_pagination_and_endpoint_sql_plans
    actor, other = content_actor("plans"), content_actor("plansother")
    token = actor.fetch(:rails).fetch("access_token")
    articles, comments = [], []
    # 24 筆每類的有限合成分布，足以區分分頁、深頁、隱藏內容與其他作者混入。
    24.times do
      article = content_article(actor)
      articles << article.fetch("id")
      comments << content_comment(actor, article.fetch("id")).fetch("id")
    end
    content_article(other)
    # 同時固定建立時間驗證次排序；不修改內容或來源，fixture 不清除任何資料。
    owner.exec_params("UPDATE articles SET created_at=to_timestamp(1000) WHERE author_id=$1", [actor.fetch(:rails).fetch("user").fetch("id")])
    owner.exec_params("UPDATE comments SET created_at=to_timestamp(1000) WHERE author_id=$1", [actor.fetch(:rails).fetch("user").fetch("id")])
    content_success(call(:rails, "PUT", "/api/articles/#{articles.first}/visibility", token: token, body: { version: 1, visibility: "hidden" }))
    content_success(call(:rails, "PUT", "/api/comments/#{comments.first}/visibility", token: token, body: { version: 1, visibility: "hidden" }))
    owner.exec("ANALYZE articles, comments, accounts")
    evidence = { project: @project, fixture: { articles: 24, comments: 24, tied_created_at: 1000, hidden_each: 1 }, endpoints: [] }
    # 文章與留言各自量測，避免共用查詢掩蓋其中一類列表的額外成本。
    [["articles", articles], ["comments", comments]].each do |table, ids|
      # 全集合來自真實建立 ID；跨後端必須讀回相同順序及精確 total。
      expected = ids.sort.reverse
      first = call(:rails, "GET", "/api/me/#{table}?page=1&per_page=100", token: token)[1].fetch("data")
      # 以 API 建立的 ID 核對完整順序，不能只比較筆數。
      assert_equal expected, first.fetch("list").map { |row| row.fetch("id") }
      assert_equal first, call(:laravel, "GET", "/api/me/#{table}?page=1&per_page=100", token: actor.fetch(:laravel).fetch("access_token"))[1].fetch("data")
      # 首頁、深頁與合法超頁分別量測，確認頁碼和精確總數不被改寫。
      [1, 4, 5].each do |page|
        path = "/api/me/#{table}?page=#{page}&per_page=6"
        Thread.current[:rails02_sql] = []
        started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        response = rack_call("GET", path, token: token)
        elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
        statements = Thread.current[:rails02_sql]
        Thread.current[:rails02_sql] = nil
        assert_equal 200, response.first
        data = response[1].fetch("data")
        # 比較指定頁的確切 ID 集合，避免只比較筆數而漏掉錯誤分頁。
        assert_equal expected.slice((page - 1) * 6, 6).to_a, data.fetch("list").map { |row| row.fetch("id") }
        assert_equal({ "page" => page, "per_page" => 6, "total" => 24, "total_pages" => 4 }, data.fetch("pagination"))
        # 每請求初始化兩個 PG type map 各查 pg_type 一次，加上 SET、認證及實際內容查詢。
        assert_equal page == 5 ? 5 : 6, statements.length
        # 只挑實際發出的內容查詢供計畫重播，認證憑證不進入證據。
        content_queries = statements.select do |sql, _parameters, _duration|
          sql.include?("FROM #{table} JOIN accounts")
        end
        assert_equal page == 5 ? 1 : 2, content_queries.length
        # 使用同 SQL 與綁定值量測真實計畫，避免以等價手寫 SQL 代替端點證據。
        plans = content_queries.map do |sql, parameters, duration|
          plan = @db.exec_params("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) #{sql}", parameters).first.fetch("QUERY PLAN")
          { sql: sql, parameters: parameters, statement_seconds: duration, plan: plan }
        end
        # 身分 SQL 與總數都可查，但 session jti 不進入持久化證據。
        evidence[:endpoints] << { path: path, elapsed_seconds: elapsed, statement_count: statements.length,
                                  emitted_sql: statements.map(&:first), content_queries: plans }
      end
    end
    empty = call(:rails, "GET", "/api/me/comments?page=2&per_page=100", token: other.fetch(:rails).fetch("access_token"))[1].fetch("data")
    assert_equal [], empty.fetch("list")
    assert_equal({ "page" => 2, "per_page" => 100, "total" => 0, "total_pages" => 0 }, empty.fetch("pagination"))
    evidence[:statistics] = @db.exec("SELECT relname,n_live_tup,last_analyze FROM pg_stat_user_tables WHERE relname IN ('articles','comments','accounts') ORDER BY relname").to_a
    evidence[:row_counts] = @db.exec("SELECT (SELECT count(*) FROM articles) AS articles,(SELECT count(*) FROM comments) AS comments,(SELECT count(*) FROM accounts) AS accounts").first
    File.write("/tmp/rails02-query-plans-#{@namespace}.json", JSON.pretty_generate(evidence))
  ensure
    Thread.current[:rails02_sql] = nil
  end
end
