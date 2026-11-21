require_relative "content_test"

# 僅於測試程序模擬固定設定列缺漏；不刪共享設定，不讓其他 live 請求受故障 fixture 影響。
module AdministrationMissingSettings
  # 保留實際連線／身分查詢，只在指定的外部 DB 讀取邊界回傳空集合。
  def exec(sql, ...)
    if Thread.current[:rails03_missing_settings] && sql == "SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1"
      return []
    end
    super
  end
end
PG::Connection.prepend(AdministrationMissingSettings)

class HttpTest
  # 三個設定競爭共用清理順序：先釋鎖、收齊背景請求，外層 fixture 才能安全還原設定。
  def with_administration_race_settings(workers, **settings)
    primary = nil
    with_lock_settings(**settings) do
      yield
    ensure
      primary = $!
      cleanup_errors = []
      # 釋放 owner 鎖後請求才能完成，不能先還原設定或先等待背景請求。
      begin
        owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
      rescue Exception => error
        cleanup_errors << error
      end
      # 一個背景請求失敗也要繼續收齊其餘請求，避免它們在 fixture 還原後遲到覆寫。
      workers.each do |worker|
        begin
          worker.join
        rescue Exception => error
          cleanup_errors << error
        end
      end
      # 保留 assertion／主要故障物件；所有清理錯誤另輸出完整診斷，不能吞掉失敗或互相掩蓋。
      cleanup_errors.each do |error|
        warn "STD-001 cleanup: #{error.full_message}"
      end
      primary ||= cleanup_errors.first
      raise primary if primary
    end
  rescue Exception => error
    # 原 helper 的還原若也失敗，原始主要錯誤仍是出口，還原錯誤必須另外可追查。
    if primary && !error.equal?(primary)
      warn "STD-001 fixture restore: #{error.full_message}"
      raise primary
    end
    raise
  end

  # 真正觀察兩請求重疊後故障，三種競爭的兩種順序都必須收妥請求再還原設定。
  def test_administration_settings_race_failure_restores_fixture
    methods = %i[test_administration_settings_concurrent_complete_updates
                 test_administration_settings_login_cross_backend_lock_order
                 test_administration_settings_disable_cross_backend_lock_order]
    original = @db.exec("SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1").first
    puts "STD-001 original settings=#{JSON.generate(original)}"
    problems = []
    # 第二／第四次成功觀察分別位於各方法的第一與第二種順序，不製造不存在的鎖。
    scenarios = methods.product([2, 4]) + [[methods.first, 2, true]]
    scenarios.each do |method, fail_after, cleanup_faults|
      probe = HttpTest.new(method.to_s)
      workers = []
      primary = RuntimeError.new("STD-001 injected after #{method} observation #{fail_after}")
      observed = nil
      begin
        probe.setup
        owner.exec("UPDATE login_lockout_settings SET enabled=true,window_minutes=7,failure_threshold=9,lock_seconds=13 WHERE id=1")
        expected = @db.exec("SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1").first
        puts "STD-001 before=#{JSON.generate(method: method, observation: fail_after, cleanup_faults: !!cleanup_faults, settings: expected)}"
        if cleanup_faults
          execute, execute_params = probe.owner.method(:exec), probe.owner.method(:exec_params)
          # 實際 ROLLBACK 已釋鎖後才注入清理錯誤，驗證診斷而不留下無法清理的假鎖。
          probe.owner.define_singleton_method(:exec) do |sql, *arguments|
            result = execute.call(sql, *arguments)
            raise "STD-001 rollback diagnostic" if sql == "ROLLBACK"
            result
          end
          # 設定真還原後才模擬回報失敗，確認 helper ensure 不會掩蓋原注入故障。
          probe.owner.define_singleton_method(:exec_params) do |sql, *arguments|
            result = execute_params.call(sql, *arguments)
            raise "STD-001 restore diagnostic" if sql.start_with?("UPDATE login_lockout_settings SET enabled=$1,")
            result
          end
        end
        wait = probe.method(:content_wait_for_lock)
        observations = 0
        # 原等待器仍先證明真實等待鏈，再拋出可辨識的主要失敗。
        probe.define_singleton_method(:content_wait_for_lock) do |*arguments|
          wait.call(*arguments)
          observations += 1
          raise primary if observations == fail_after
        end
        thread_new = Thread.method(:new)
        # 只記錄測試程序建立的背景請求，HTTP／PG 執行內容不替換。
        creator = lambda do |*arguments, &block|
          # Timeout 可能建立自己的常駐 timer thread；只收本檔定義的請求，不 join 標準函式庫生命週期。
          owned = File.expand_path(block.source_location.first) == File.expand_path(__FILE__)
          fail_worker = owned && cleanup_faults && workers.empty?
          # 背景錯誤在真 HTTP 完成後才注入，另一請求仍必須收妥且不可漏掉主要故障。
          worker = thread_new.call(*arguments) do
            result = block.call
            raise "STD-001 background diagnostic" if fail_worker
            result
          end
          worker.report_on_exception = false if owned
          workers << worker if owned
          worker
        end
        # 收集診斷供回歸斷言；HTTP／PG 真實操作仍照常完成。
        _, diagnostics = capture_io do
          begin
            Thread.stub(:new, creator) { probe.public_send(method) }
          rescue Exception => error
            # Minitest assertion 亦繼承 Exception；保留原物件才能辨識清理是否掩蓋主要失敗。
            observed = error
          end
        end
        warn diagnostics unless diagnostics.empty?
        actual = @db.exec("SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1").first
        result = { method: method, observation: fail_after, primary_preserved: observed.equal?(primary),
                   observed: observed&.class&.name, transaction_idle: probe.owner.transaction_status == PG::PQTRANS_IDLE,
                   workers_finished: !workers.empty? && workers.none?(&:alive?), settings_restored: expected == actual,
                   cleanup_diagnostics: !cleanup_faults || %w[rollback background restore].all? { |name| diagnostics.include?("STD-001 #{name} diagnostic") } }
        puts "STD-001 recovery=#{JSON.generate(result)}"
        problems << result unless result.values_at(:primary_preserved, :transaction_idle, :workers_finished, :settings_restored, :cleanup_diagnostics).all?
        # 每次失敗後立即再使用設定／登入公開入口，確認不是僅看資料列相同。
        recovery = probe.content_actor("cleanuprecovery", admin: true)
        content_success(call(:rails, "PUT", "/api/settings/login-lockout", token: recovery.fetch(:rails).fetch("access_token"), body: expected))
        assert_equal expected, call(:rails, "GET", "/api/settings/login-lockout", token: recovery.fetch(:rails).fetch("access_token"))[1].fetch("data")
        email = recovery.fetch(:rails).fetch("user").fetch("email")
        assert_equal email, login(:laravel, [email, "Demo1234"]).fetch("user").fetch("email")
      ensure
        # 紅燈版本也須安全釋鎖及收齊請求，才可還原隔離 fixture 並繼續下一個回歸。
        probe.owner.exec("ROLLBACK") if probe.instance_variable_get(:@owner) && probe.owner.transaction_status != PG::PQTRANS_IDLE
        # 回歸自身的保險清理也須收齊所有 worker，不能讓預期的背景故障阻擋 fixture 還原。
        workers.each do |worker|
          begin
            worker.join
          rescue Exception => error
            warn "STD-001 probe cleanup: #{error.full_message}"
          end
        end
        probe.teardown
        owner.exec_params("UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1", original.values)
      end
    end
    assert_empty problems, problems.inspect
  end

  # 固定設定列缺漏屬認證設定異常，不能回傳 200 data:null 冒充有效設定。
  def test_administration_missing_settings_fails_closed
    administrator = content_actor("missingadmin", admin: true)
    Thread.current[:rails03_missing_settings] = true
    result = rack_call("GET", "/api/settings/login-lockout", token: administrator.fetch(:rails).fetch("access_token"))
    error(result, 500, "E0104")
  ensure
    Thread.current[:rails03_missing_settings] = nil
  end

  # 內容先完成則停用等提交；停用先完成則另一後端不能沿用鎖前身分修改內容。
  def test_administration_status_content_cross_backend_commit_order
    first = second = nil
    administrator = content_actor("contentraceadmin", admin: true)
    # 每個方向各驗兩種提交順序，避免只覆蓋 Laravel 原有撤銷流程。
    [[:rails, :laravel], [:laravel, :rails]].each do |status_backend, writer_backend|
      # 相同公開編修入口只改同步點，分別證明先提交及鎖後拒絕。
      [:write_first, :status_first].each do |order|
        actor = content_actor("#{status_backend}#{order}")
        article = content_article(actor, writer_backend)
        id = actor.fetch(:rails).fetch("user").fetch("id")
        owner.exec("BEGIN")
        if order == :write_first
          owner.exec_params("SELECT id FROM articles WHERE id=$1 FOR UPDATE", [article.fetch("id")])
        else
          owner.exec_params("SELECT jti FROM login_sessions WHERE account_id=$1 ORDER BY jti FOR UPDATE", [id])
        end
        blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
        # 以文章鎖或目標session鎖分別控制先寫入／先停用，完整穿過兩後端真實HTTP入口。
        write = lambda do
          call(writer_backend, "PUT", "/api/articles/#{article.fetch('id')}", token: actor.fetch(writer_backend).fetch("access_token"),
               body: { title: "競爭完整標題", body: "競爭完整本文", version: 1 })
        end
        # 停用在獨立 HTTP 連線等待，不能與測試持鎖交易共用連線。
        disable = lambda do
          call(status_backend, "PUT", "/api/members/#{id}/status", token: administrator.fetch(status_backend).fetch("access_token"), body: { status: "disabled" })
        end
        # 先請求必須到達持鎖同步點，才讓另一請求形成真實重疊。
        first = Thread.new { order == :write_first ? write.call : disable.call }
        pid = content_waiting_pid(blocker)
        # 後請求在先請求持有的鎖上排隊，不能只由發送順序推論提交順序。
        second = Thread.new { order == :write_first ? disable.call : write.call }
        content_wait_for_lock(pid)
        owner.exec("COMMIT")
        disabled, written = order == :write_first ? [second.value, first.value] : [first.value, second.value]
        content_success(disabled)
        order == :write_first ? content_success(written) : error(written, 403, "E0102")
        row = call(:rails, "GET", "/api/articles/#{article.fetch('id')}", token: administrator.fetch(:rails).fetch("access_token"))[1].fetch("data")
        expected = order == :write_first ? ["競爭完整標題", "競爭完整本文", 2] : article.values_at("title", "body", "version")
        assert_equal expected, row.values_at("title", "body", "version")
        # 停用提交後，兩後端原有登入都必須立即拒絕。
        actor.each do |backend, identity|
          error(call(backend, "GET", "/api/me", token: identity.fetch("access_token")), 403, "E0102")
        end
      end
    end
  ensure
    owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
    first&.join
    second&.join
  end

  # 登入／登出與停用共享帳號鎖，確保停用之後沒有新有效登入或遲到成功登出。
  def test_administration_status_login_logout_cross_backend_commit_order
    first = second = nil
    administrator = content_actor("authraceadmin", admin: true)
    # 兩個停用來源分別面對另一後端的認證操作。
    [[:rails, :laravel], [:laravel, :rails]].each do |status_backend, auth_backend|
      # 登入與登出鎖點不同，各自驗證而不由一種結果外推。
      [:login, :logout].each do |operation|
        # 保留兩種先後的完整結果與恢復後再登入。
        [:auth_first, :status_first].each do |order|
          actor = content_actor("#{status_backend}#{operation}#{order}")
          identity = actor.fetch(auth_backend)
          id, email = identity.fetch("user").values_at("id", "email")
          before_failures = @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1", [id]).first.fetch("count")
          owner.exec("BEGIN")
          if order == :auth_first && operation == :login
            owner.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [id])
          else
            owner.exec_params("SELECT jti FROM login_sessions WHERE account_id=$1 ORDER BY jti FOR UPDATE", [id])
          end
          blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
          # 登入先時排帳號鎖；其他順序排session鎖，讓先請求持有帳號直到觀察第二請求等待。
          authenticate = lambda do
            operation == :login ? call(auth_backend, "POST", "/api/auth/login", body: { email: email, password: "Demo1234" }) :
              call(auth_backend, "POST", "/api/auth/logout", token: identity.fetch("access_token"))
          end
          # 真實管理端點在另一連線撤銷目標帳號全部登入。
          disable = lambda do
            call(status_backend, "PUT", "/api/members/#{id}/status", token: administrator.fetch(status_backend).fetch("access_token"), body: { status: "disabled" })
          end
          # 先請求到達可觀察鎖點後才安排相反操作。
          first = Thread.new { order == :auth_first ? authenticate.call : disable.call }
          pid = content_waiting_pid(blocker)
          # 等待鏈必須包含後請求，確保這次測試真的發生競爭。
          second = Thread.new { order == :auth_first ? disable.call : authenticate.call }
          content_wait_for_lock(pid)
          unlocked_at = owner.exec("SELECT clock_timestamp() AS now").first.fetch("now")
          owner.exec("COMMIT")
          disabled, authenticated = order == :auth_first ? [second.value, first.value] : [first.value, second.value]
          content_success(disabled)
          if order == :status_first
            error(authenticated, 403, "E0102")
          elsif operation == :logout
            content_success(authenticated)
          else
            assert_equal 200, authenticated.first
            fresh = authenticated[1].fetch("data").fetch("access_token")
            claims = JSON.parse(Base64.urlsafe_decode64(fresh.split(".")[1]))
            # live DB 解除帳號鎖後才簽發；精確等號另由固定時鐘公開Rack案例證明。
            assert_operator claims.fetch("iat"), :>=, Time.parse(unlocked_at).to_i
            error(call(auth_backend, "GET", "/api/me", token: fresh), 403, "E0102")
          end
          assert_equal before_failures, @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1", [id]).first.fetch("count")
          assert_equal 0, @db.exec_params("SELECT count(*) FROM login_sessions WHERE account_id=$1 AND revoked_at IS NULL", [id]).first.fetch("count")
          content_success(call(status_backend, "PUT", "/api/members/#{id}/status", token: administrator.fetch(status_backend).fetch("access_token"), body: { status: "active" }))
          # 恢復帳號只允許新登入，兩後端舊憑證均不得復活。
          actor.each do |backend, login_data|
            error(call(backend, "GET", "/api/me", token: login_data.fetch("access_token")), 401, "E0002")
          end
          error(call(auth_backend, "GET", "/api/me", token: fresh), 401, "E0002") if order == :auth_first && operation == :login
          assert_equal email, login(auth_backend, [email, "Demo1234"]).fetch("user").fetch("email")
        end
      end
    end
  ensure
    owner.exec("ROLLBACK") if @owner && owner.transaction_status != PG::PQTRANS_IDLE
    first&.join
    second&.join
  end

  # 放大視窗會重新納入同世代舊事件；數字修改不改既有到期，等號到期才換世代。
  def test_administration_window_expansion_and_expiry_with_setting_updates
    administrator, actor = content_actor("timeadmin", admin: true), content_actor("timeactor")
    token = administrator.fetch(:rails).fetch("access_token")
    id, email = actor.fetch(:rails).fetch("user").values_at("id", "email")
    settings = "/api/settings/login-lockout"
    time = Time.at(Time.now.to_i).utc
    values = { enabled: true, window_minutes: 1, failure_threshold: 3, lock_seconds: 10 }
    with_lock_settings(threshold: 3, seconds: 10) do
      content_success(call(:rails, "PUT", settings, token: token, body: values))
      epoch = @db.exec_params("SELECT failure_epoch FROM accounts WHERE id=$1", [id]).first.fetch("failure_epoch")
      # 由相同公開登入入口建立兩分鐘前事件；時鐘僅固定測試程序，DB/交易仍是真實。
      at_time(time - 120) do
        error(rack_call("POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 401, "E0101")
      end
      # 小視窗排除兩分鐘前事件，因此第二次錯誤尚未達三次門檻。
      at_time(time) do
        error(rack_call("POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 401, "E0101")
      end
      content_success(call(:laravel, "PUT", settings, token: administrator.fetch(:laravel).fetch("access_token"), body: values.merge(window_minutes: 5)))
      # 擴大視窗後重新納入舊事件，當次第三筆立即鎖定。
      at_time(time) do
        result = rack_call("POST", "/api/auth/login", body: { email: email, password: "Wrong1234" })
        error(result, 429, "E0105")
        assert_equal "10", result[2]["Retry-After"]
      end
      assert_equal time + 10, @db.exec_params("SELECT locked_until FROM accounts WHERE id=$1", [id]).first.fetch("locked_until")
      assert_equal 3, @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1 AND failure_epoch=$2", [id, epoch]).first.fetch("count")
      content_success(call(:rails, "PUT", settings, token: token, body: values.merge(window_minutes: 60, failure_threshold: 100, lock_seconds: 1)))
      # 數字修改不能縮短已鎖定到期，最後一秒仍回原倒數。
      at_time(time + 9) do
        result = rack_call("POST", "/api/auth/login", body: { email: email, password: "Demo1234" })
        error(result, 429, "E0105")
        assert_equal "1", result[2]["Retry-After"]
      end
      # 等於原到期時刻才切換世代，錯誤密碼成為新世代第一筆。
      at_time(time + 10) do
        error(rack_call("POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 401, "E0101")
      end
      assert_equal [epoch + 1, nil], @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first.values_at("failure_epoch", "locked_until")
      assert_equal 4, @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1", [id]).first.fetch("count")
    end
  end

  # 清計數／撤銷的 SQL 已執行後注入系統失敗，設定與帳號／session 必須一起回滾。
  def test_administration_atomic_rollback_and_management_reauthentication
    administrator, actor = content_actor("rollbackadmin", admin: true), content_actor("rollbackactor")
    token = administrator.fetch(:rails).fetch("access_token")
    id = actor.fetch(:rails).fetch("user").fetch("id")
    path = "/api/settings/login-lockout"
    with_lock_settings do
      owner.exec_params("UPDATE accounts SET locked_until=clock_timestamp()+interval '5 minutes' WHERE id=$1", [id])
      before = @db.exec("SELECT id,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
      settings_before = call(:rails, "GET", path, token: token)[1].fetch("data")
      # 此 hook 只作用此 Rack 請求的外部 DB 邊界，不替換 SQL 結果。
      Thread.current[:rails02_after_sql] = lambda do |sql|
        raise PG::ConnectionBad, "TEST_DO_NOT_EXPOSE" if sql.start_with?("UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL")
      end
      result = rack_call("PUT", path, token: token, body: settings_before.merge("enabled" => false))
      Thread.current[:rails02_after_sql] = nil
      error(result, 500, "E9999")
      assert_equal settings_before, call(:rails, "GET", path, token: token)[1].fetch("data")
      assert_equal before, @db.exec("SELECT id,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
      sessions = @db.exec_params("SELECT * FROM login_sessions WHERE account_id=$1 ORDER BY jti", [id]).to_a
      # 停用已更新帳號並撤銷 session 後仍可整體回滾，不能回傳半套成功。
      Thread.current[:rails02_after_sql] = lambda do |sql|
        raise PG::ConnectionBad, "TEST_DO_NOT_EXPOSE" if sql.start_with?("UPDATE login_sessions SET revoked_at=")
      end
      result = rack_call("PUT", "/api/members/#{id}/status", token: token, body: { status: "disabled" })
      Thread.current[:rails02_after_sql] = nil
      error(result, 500, "E9999")
      assert_equal before, @db.exec("SELECT id,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
      assert_equal sessions, @db.exec_params("SELECT * FROM login_sessions WHERE account_id=$1 ORDER BY jti", [id]).to_a
      assert_equal 200, call(:rails, "GET", "/api/me", token: actor.fetch(:rails).fetch("access_token")).first
      # 外層驗證後才撤銷或降角色，兩種管理寫入均必須鎖後重驗並拒絕所有變更。
      [[path, settings_before.merge("enabled" => false)], ["/api/members/#{id}/status", { status: "disabled" }]].each do |endpoint, body|
        # 撤銷與角色變更各自排在外層驗證之後，不能只測缺少 token。
        [:logout, :demote].each do |event|
          fresh = login(:rails, [administrator.fetch(:rails).fetch("user").fetch("email"), "Demo1234"]).fetch("access_token")
          # 正常登入本身會換管理者世代，故基準在取得新登入之後建立。
          reauthenticated_before = @db.exec("SELECT id,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
          # 外層讀取完成後才提交身分變更，確認交易重驗具有實際效果。
          callback = lambda do
            if event == :logout
              content_success(call(:rails, "POST", "/api/auth/logout", token: fresh))
            else
              owner.exec_params("UPDATE accounts SET role='member' WHERE id=$1", [administrator.fetch(:rails).fetch("user").fetch("id")])
            end
          end
          # 同一公開 Rack 入口必須完成所有正常門禁及交易流程。
          result = after_identity_read(callback) { rack_call("PUT", endpoint, token: fresh, body: body) }
          error(result, event == :logout ? 401 : 403, event == :logout ? "E0002" : "E0003")
          owner.exec_params("UPDATE accounts SET role='admin' WHERE id=$1", [administrator.fetch(:rails).fetch("user").fetch("id")])
          assert_equal settings_before, call(:rails, "GET", path, token: token)[1].fetch("data")
          assert_equal reauthenticated_before, @db.exec("SELECT id,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
        end
      end
    end
  ensure
    Thread.current[:rails02_after_sql] = nil
    owner.exec_params("UPDATE accounts SET role='admin' WHERE id=$1", [administrator.fetch(:rails).fetch("user").fetch("id")]) if administrator
  end

  # 兩後端設定真正排入同一行鎖，最後成功提交的是完整四欄，不會混合請求。
  def test_administration_settings_concurrent_complete_updates
    threads = []
    administrator = content_actor("raceadmin", admin: true)
    # 設定 fixture 在任何出口恢復原值，不影響同庫下一次執行。
    with_administration_race_settings(threads) do
      # 兩後端輪流成為最後提交者。
      [[:rails, :laravel], [:laravel, :rails]].each do |first, second|
        values = [{ enabled: true, window_minutes: 1, failure_threshold: 2, lock_seconds: 3 },
                  { enabled: true, window_minutes: 60, failure_threshold: 99, lock_seconds: 86_400 }]
        owner.exec("BEGIN")
        owner.exec("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE")
        blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
        # 先觀察第一請求的等待，再送第二請求；兩者都必須真的重疊才放鎖。
        [first, second].each_with_index do |backend, index|
          # 每個 PUT 使用獨立 HTTP 連線，讓設定列鎖真實序列化。
          threads << Thread.new do
            call(backend, "PUT", "/api/settings/login-lockout", token: administrator.fetch(backend).fetch("access_token"), body: values[index])
          end
          content_wait_for_lock(blocker, index + 1)
        end
        owner.exec("COMMIT")
        # 所有請求都須完整成功，不能只查最後設定而忽略其中一個失敗。
        threads.each { |thread| content_success(thread.value) }
        assert_equal values.last.transform_keys(&:to_s), call(:rails, "GET", "/api/settings/login-lockout", token: administrator.fetch(:rails).fetch("access_token"))[1].fetch("data")
      end
    end
  end

  # 設定先提交則登入採新設定；登入先判斷則失敗事件先保存，再由關閉原子清鎖換世代。
  def test_administration_settings_login_cross_backend_lock_order
    first = second = nil
    threads = []
    administrator = content_actor("loginraceadmin", admin: true)
    # 門檻設為一，使是否先計數可從當次 401／429 直接辨識。
    with_administration_race_settings(threads, threshold: 1, seconds: 300) do
      # 設定與登入各由兩個不同後端執行一次。
      [[:rails, :laravel], [:laravel, :rails]].each do |settings_backend, login_backend|
        # 關閉先與登入先必須產生各自核准的歷史事件數。
        [:settings_first, :login_first].each do |order|
          actor = content_actor("#{settings_backend}#{order}")
          identity = actor.fetch(:rails)
          id, email = identity.fetch("user").values_at("id", "email")
          owner.exec("UPDATE login_lockout_settings SET enabled=true,window_minutes=5,failure_threshold=1,lock_seconds=300 WHERE id=1")
          epoch = @db.exec_params("SELECT failure_epoch FROM accounts WHERE id=$1", [id]).first.fetch("failure_epoch")
          owner.exec("BEGIN")
          if order == :settings_first
            owner.exec("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE")
          else
            owner.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [id])
          end
          blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
          # 使用兩個真 HTTP 請求；同步點由 PostgreSQL 等待鏈決定，不依經過秒數推測。
          settings_request = lambda do
            call(settings_backend, "PUT", "/api/settings/login-lockout", token: administrator.fetch(settings_backend).fetch("access_token"),
                 body: { enabled: false, window_minutes: 5, failure_threshold: 1, lock_seconds: 300 })
          end
          # 合法格式錯誤密碼才有計數資格，用來區別新設定是否已提交。
          login_request = lambda do
            call(login_backend, "POST", "/api/auth/login", body: { email: email, password: "Wrong1234" })
          end
          # 先請求到達鎖點後才發送第二請求。
          threads << (first = Thread.new { order == :settings_first ? settings_request.call : login_request.call })
          pid = content_waiting_pid(blocker)
          # PostgreSQL 等待鏈證明請求確實重疊，不以 thread 啟動時間替代。
          threads << (second = Thread.new { order == :settings_first ? login_request.call : settings_request.call })
          content_wait_for_lock(pid)
          owner.exec("COMMIT")
          setting_result, login_result = order == :settings_first ? [first.value, second.value] : [second.value, first.value]
          content_success(setting_result)
          error(login_result, order == :settings_first ? 401 : 429, order == :settings_first ? "E0101" : "E0105")
          row = @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
          assert_equal epoch + 1, row.fetch("failure_epoch")
          assert_nil row.fetch("locked_until")
          assert_equal order == :settings_first ? 0 : 1, @db.exec_params("SELECT count(*) FROM login_failures WHERE account_id=$1", [id]).first.fetch("count")
          assert_equal 200, call(login_backend, "GET", "/api/me", token: actor.fetch(login_backend).fetch("access_token")).first
        end
      end
    end
  end

  # 關閉與停用在相反順序都安全完成；關閉不能恢復帳號或已撤銷登入。
  def test_administration_settings_disable_cross_backend_lock_order
    first = second = nil
    threads = []
    administrator = content_actor("disableraceadmin", admin: true)
    # 確保全域設定即使競爭測試失敗也會恢復。
    with_administration_race_settings(threads) do
      # 關閉及停用的來源輪替，驗證相容鎖順序並非單一框架巧合。
      [[:rails, :laravel], [:laravel, :rails]].each do |settings_backend, status_backend|
        # 相反順序都必須保留停用狀態並讓舊登入失效。
        [:settings_first, :status_first].each do |order|
          actor = content_actor("#{settings_backend}#{order}")
          id = actor.fetch(:rails).fetch("user").fetch("id")
          owner.exec("UPDATE login_lockout_settings SET enabled=true WHERE id=1")
          owner.exec_params("UPDATE accounts SET locked_until=clock_timestamp()+interval '5 minutes' WHERE id=$1", [id])
          epoch = @db.exec_params("SELECT failure_epoch FROM accounts WHERE id=$1", [id]).first.fetch("failure_epoch")
          owner.exec("BEGIN")
          owner.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [id])
          blocker = owner.exec("SELECT pg_backend_pid() AS pid").first.fetch("pid").to_i
          # 設定會由 settings→全帳號依序等待；停用只鎖 admin/target，皆不能反向取鎖。
          setting = lambda do
            call(settings_backend, "PUT", "/api/settings/login-lockout", token: administrator.fetch(settings_backend).fetch("access_token"),
                 body: { enabled: false, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 })
          end
          # 管理停用不取得設定鎖，只依共用帳號順序與設定協調。
          status = lambda do
            call(status_backend, "PUT", "/api/members/#{id}/status", token: administrator.fetch(status_backend).fetch("access_token"), body: { status: "disabled" })
          end
          # 先請求等待 owner 鎖，後請求必須被先請求的帳號鎖擋住。
          threads << (first = Thread.new { order == :settings_first ? setting.call : status.call })
          pid = content_waiting_pid(blocker)
          # 使用不同連線保留真實跨後端競爭。
          threads << (second = Thread.new { order == :settings_first ? status.call : setting.call })
          content_wait_for_lock(pid)
          owner.exec("COMMIT")
          content_success(first.value)
          content_success(second.value)
          assert_equal ["disabled", epoch + 1, nil], @db.exec_params("SELECT status,failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first.values_at("status", "failure_epoch", "locked_until")
          # 先確認停用當下優先顯示停用錯誤。
          actor.each do |backend, identity|
            error(call(backend, "GET", "/api/me", token: identity.fetch("access_token")), 403, "E0102")
          end
          content_success(call(status_backend, "PUT", "/api/members/#{id}/status", token: administrator.fetch(status_backend).fetch("access_token"), body: { status: "active" }))
          # 恢復後仍須拒絕兩份已撤銷的舊登入。
          actor.each do |backend, identity|
            error(call(backend, "GET", "/api/me", token: identity.fetch("access_token")), 401, "E0002")
          end
        end
      end
    end
  end

  # 以有限合成資料與固定同時刻驗完整 ID 分頁；量測真實 Rack SQL，再用同參數重播計畫。
  def test_administration_search_pagination_and_emitted_sql_plans
    administrator = content_actor("planadmin", admin: true)
    token = administrator.fetch(:rails).fetch("access_token")
    ids = []
    # 24 筆可區分首／深／超頁；來源輪替兩後端，停用會員仍在管理範圍。
    24.times do |index|
      actor = member(index.even? ? :rails : :laravel, "plan#{index}")
      identity = login(:rails, actor)
      id = identity.fetch("user").fetch("id")
      ids << id
      owner.exec_params("UPDATE accounts SET nickname=$1,created_at=to_timestamp(1000),status=$2 WHERE id=$3",
                        ["#{@namespace}A_B%\\É中", index.even? ? "active" : "disabled", id])
    end
    # 同關鍵字管理員不進會員集合；非 ASCII 大小寫及 Unicode 正規化皆不可混合。
    owner.exec_params("UPDATE accounts SET nickname=$1 WHERE id=$2", ["#{@namespace}A_B%\\É中", administrator.fetch(:rails).fetch("user").fetch("id")])
    expected = ids.reverse
    base = "/api/members?page=1&per_page=100&filters%5Bkeyword%5D="
    # 同一明確 ID 集合依 ASCII 摺疊、字面符號與 U+0020 trim 各自命中。
    ["#{@namespace}a_b", "#{@namespace}A_B%", "#{@namespace}A_B%\\", "#{@namespace}A_B%\\É", " #{@namespace}A_B%\\É中 "].each do |keyword|
      result = call(:rails, "GET", base + URI.encode_www_form_component(keyword), token: token)[1].fetch("data")
      assert_equal expected, result.fetch("list").map { |row| row.fetch("id") }
      assert_equal({ "page" => 1, "per_page" => 100, "total" => 24, "total_pages" => 1 }, result.fetch("pagination"))
    end
    # 非 ASCII 大小寫、正規化及萬用字元反例都應保持空集合；254 碼點仍是合法輸入。
    ["#{@namespace}A1B", "#{@namespace}A_B%\\é", "#{@namespace}A_B%\\E\u0301", "#{@namespace}Ａ", "😀" * 254, "\\u0000"].each do |keyword|
      data = call(:rails, "GET", base + URI.encode_www_form_component(keyword), token: token)[1].fetch("data")
      assert_equal [], data.fetch("list")
      assert_equal 0, data.fetch("pagination").fetch("total")
    end
    assert_equal [0], URI.decode_www_form_component("%00").codepoints
    error(call(:rails, "GET", base + "%00", token: token), 422, "E0001", { "filters.keyword" => ["欄位格式或範圍不正確"] })
    # 省略／空白不篩選，從同一固定資料狀態核對完整會員排序，避免靠筆數假設乾淨庫。
    all_ids = @db.exec("SELECT id FROM accounts WHERE role='member' ORDER BY created_at DESC,id DESC").map { |row| row.fetch("id") }
    ["", "&filters%5Bkeyword%5D=", "&filters%5Bkeyword%5D=%20%20"].each do |suffix|
      actual = []
      # 掃過有限測資的全部 API 頁面，以完整 ID 順序確認分頁沒有漏列或重複。
      (1..((all_ids.length + 99) / 100)).each do |page|
        data = call(:rails, "GET", "/api/members?page=#{page}&per_page=100#{suffix}", token: token)[1].fetch("data")
        assert_equal all_ids.length, data.fetch("pagination").fetch("total")
        actual.concat(data.fetch("list").map { |row| row.fetch("id") })
      end
      assert_equal all_ids, actual
    end
    # 短字與單獨符號會命中先前測資；以當下完整合成資料快照的 Ruby 字串語意獨立核對。
    snapshot = @db.exec("SELECT id,email,nickname FROM accounts WHERE role='member' ORDER BY created_at DESC,id DESC").to_a
    short_cases = ["a", "A", "a_", "中", "É", "é", "%", "_", "\\", "\u00a0", "\t"].map do |keyword|
      folded = keyword.tr("A-Z", "a-z")
      # Ruby 字串包含是獨立於 SQL 的預期計算，保留非 ASCII 碼點精確比對。
      matches = snapshot.select do |row|
        row.fetch("email").tr("A-Z", "a-z").include?(folded) || row.fetch("nickname").tr("A-Z", "a-z").include?(folded)
      end.map { |row| row.fetch("id") }
      actual = []
      # 每種短字都讀完全部命中頁，空集合也至少讀一次公開入口。
      (1..[(matches.length + 99) / 100, 1].max).each do |page|
        data = call(:rails, "GET", "/api/members?page=#{page}&per_page=100&filters%5Bkeyword%5D=#{URI.encode_www_form_component(keyword)}", token: token)[1].fetch("data")
        assert_equal matches.length, data.fetch("pagination").fetch("total")
        actual.concat(data.fetch("list").map { |row| row.fetch("id") })
      end
      assert_equal matches, actual
      [keyword, matches]
    end
    owner.exec("ANALYZE accounts")
    evidence = { project: @project, fixture: { ids: ids, tied_created_at: 1000, keyword: @namespace, members: 24, disabled: 12 }, endpoints: [] }
    # 不篩選、nickname、email OR、字面符號、Unicode及空集合分支均擷取實際 emitted SQL。
    cases = [[nil, all_ids], [@namespace, expected], ["#{@namespace}-plan1@EXAMPLE.TEST", [ids[1]]], ["#{@namespace}A_B%\\", expected], ["#{@namespace}A_B%\\É中", expected], ["#{@namespace}-missing", []]] + short_cases
    # 每種真實搜尋分支都量測同一公開 Rack 入口，計畫綁定其實際 SQL／參數。
    cases.each do |keyword, matches|
      # 首頁、深頁、邊界超頁及最大合法頁碼分別保留計畫與精確總數。
      [1, 4, 5, SiteApi::Input::MAX_ID].each do |page|
        query = "/api/members?page=#{page}&per_page=6"
        query += "&filters%5Bkeyword%5D=#{URI.encode_www_form_component(keyword)}" if keyword
        Thread.current[:rails02_sql] = []
        started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        response = rack_call("GET", query, token: token)
        elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
        statements = Thread.current[:rails02_sql]
        Thread.current[:rails02_sql] = nil
        assert_equal 200, response.first
        data = response[1].fetch("data")
        offset = (page - 1) * 6
        assert_equal matches.slice(offset, 6).to_a, data.fetch("list").map { |row| row.fetch("id") }
        assert_equal({ "page" => page, "per_page" => 6, "total" => matches.length, "total_pages" => (matches.length + 5) / 6 }, data.fetch("pagination"))
        # PG 兩次 type-map 查詢、SET、認證、count，加上有效頁才有的單次資料查詢。
        assert_equal offset < matches.length ? 6 : 5, statements.length
        # 只輸出業務搜尋 SQL；身分查詢的 jti 不放進計畫證據。
        queries = statements.select { |sql, _, _| sql.include?("FROM accounts WHERE role='member'") }
        # 使用同一綁定值重播已發出的查詢，不以手寫近似 SQL 冒充端點計畫。
        plans = queries.map do |sql, parameters, duration|
          { sql: sql, parameters: parameters, duration_seconds: duration,
            plan: @db.exec_params("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) #{sql}", parameters).first.fetch("QUERY PLAN") }
        end
        evidence[:endpoints] << { path: query, statement_count: statements.length, elapsed_seconds: elapsed, total: matches.length, queries: plans }
      end
    end
    evidence[:statistics] = @db.exec("SELECT relname,n_live_tup,last_analyze FROM pg_stat_user_tables WHERE relname='accounts'").to_a
    evidence[:row_count] = @db.exec("SELECT count(*) FROM accounts").first.fetch("count")
    File.write("/tmp/rails03-query-plans-#{@namespace}.json", JSON.pretty_generate(evidence))
  ensure
    Thread.current[:rails02_sql] = nil
  end

  # API17–22 逐一驗證認證／角色先行；HTTP 方法與 OPTIONS 先於身分及參數。
  def test_administration_authorization_methods_and_validation_order
    administrator, actor = content_actor("gateadmin", admin: true), content_actor("gateactor")
    admin, member = administrator.fetch(:rails), actor.fetch(:rails)
    id, aid = member.fetch("user").fetch("id"), admin.fetch("user").fetch("id")
    settings = "/api/settings/login-lockout"
    paths = [
      ["GET", "/api/members?page=bad&per_page=20", nil, "GET, OPTIONS"],
      ["GET", "/api/members/nope?x=1", nil, "GET, OPTIONS"],
      ["PUT", "/api/members/nope/status?x=1", "{", "PUT, OPTIONS"],
      ["GET", "/api/comments?page=bad&per_page=20", nil, "GET, OPTIONS"],
      ["GET", settings + "?x=1", nil, "GET, PUT, OPTIONS"],
      ["PUT", settings + "?x=1", "{", "GET, PUT, OPTIONS"],
      ["GET", "/api/articles?filters%5Bscope%5D=management&page=bad", nil, "GET, POST, OPTIONS"]
    ]
    # 各端點均測缺少、無效、一般會員與可識別停用登入，不能用單一端點代表全組。
    paths.each do |method, path, raw, allow|
      error(call(:rails, method, path, raw: raw), 401, "E0002")
      error(call(:rails, method, path, raw: raw, token: "bad"), 401, "E0002")
      error(call(:rails, method, path, raw: raw, token: member.fetch("access_token")), 403, "E0003")
      owner.exec_params("UPDATE accounts SET status='disabled' WHERE id=$1", [aid])
      begin
        error(call(:rails, method, path, raw: raw, token: admin.fetch("access_token")), 403, "E0102")
      ensure
        owner.exec_params("UPDATE accounts SET status='active' WHERE id=$1", [aid])
      end
      response = call(:rails, "OPTIONS", path, raw: "{")
      assert_equal 204, response.first
      assert_equal allow, response[2]["access-control-allow-methods"]
      response = call(:rails, "DELETE", path)
      error(response, 405, "E0005")
      assert_equal allow, response[2]["allow"]
    end
    # GET 非空 Body 優先於其餘 Query／ID 參數；管理員目標只有在參數合法後才 404。
    ["/api/members?page=1&per_page=20", "/api/members/#{id}", "/api/comments?page=1&per_page=20", settings].each do |path|
      separator = path.include?("?") ? "&" : "?"
      error(call(:rails, "GET", path + separator + "x=1", raw: "{", token: admin.fetch("access_token")), 422, "E0001", { "_body" => ["此 API 不接受 Request Body"] })
    end
    ["/api/members/#{aid}", "/api/members/9007199254740991"].each do |path|
      error(call(:rails, "GET", path, token: admin.fetch("access_token")), 404, "E0004")
      error(call(:rails, "PUT", path + "/status", token: admin.fetch("access_token"), body: { status: "disabled" }), 404, "E0004")
    end
    error(call(:rails, "GET", "/api/members/nope", token: admin.fetch("access_token")), 422, "E0001", { "member_id" => ["欄位格式或範圍不正確"] })
    error(call(:rails, "GET", "/api/members/#{id}?x=1&x=2", token: admin.fetch("access_token")), 422, "E0001", { "x" => ["此參數不可重複"] })
    assert_equal id, call(:rails, "GET", "/api/members/000#{id}", token: admin.fetch("access_token"))[1].fetch("data").fetch("id")
  end

  # 原共同設定案例逐欄檢查缺漏/null/type/range，並讀回原值證明拒絕沒有部分提交。
  def test_administration_body_and_query_contract
    administrator = content_actor("inputadmin", admin: true)
    actor = content_actor("inputmember")
    token = administrator.fetch(:rails).fetch("access_token")
    settings = "/api/settings/login-lockout"
    defaults = { "enabled" => true, "window_minutes" => 5, "failure_threshold" => 5, "lock_seconds" => 300 }
    # 所有合法邊界寫入在 ensure 復原，非法輸入不能改變原設定。
    with_lock_settings do
      status_path = "/api/members/#{actor.fetch(:rails).fetch('user').fetch('id')}/status"
      # 設定與會員狀態各自經完整解析流程，不把共用驗證器視為端點已驗。
      [[settings, defaults], [status_path, { "status" => "active" }]].each do |path, valid|
        # Body 解析與結構必須固定分類；重複 JSON key 採最後值，額外欄位不能忽略。
        error(call(:rails, "PUT", path, token: token), 422, "E0001", { "_body" => ["Request Body 必須為 JSON object"] })
        # 各種可解析的非 object 根結構都應以共同 _body 錯誤拒絕。
        ["[]", "null", "true", '"text"'].each do |raw|
          error(call(:rails, "PUT", path, token: token, raw: raw), 422, "E0001", { "_body" => ["Request Body 必須為 JSON object"] })
        end
        error(call(:rails, "PUT", path, token: token, raw: "{"), 400, "E0005")
        error(call(:rails, "PUT", path, token: token, body: valid.merge("version" => 1)), 422, "E0001", { "version" => ["不支援此欄位"] })
        error(call(:rails, "PUT", path + "?x=1&x=2", token: token, raw: "{"), 422, "E0001", { "x" => ["此參數不可重複"] })
        # 每個欄位分別測缺漏、null 及型態，不以代表欄位代替完整規格。
        valid.each_key do |field|
          # 移除單一鍵產生真缺漏，不能以 nil 代替必填案例。
          error(call(:rails, "PUT", path, token: token, body: valid.reject { |name, _| name == field }), 422, "E0001", { field => ["此欄位為必填"] })
          error(call(:rails, "PUT", path, token: token, body: valid.merge(field => nil)), 422, "E0001", { field => ["此欄位不可為 null"] })
          # JSON number、string、boolean、array/object 各自不接受框架自動轉型。
          bad_types = field == "enabled" ? [0, 1, "false", [], {}] : field == "status" ? [1, true, [], {}] : ["1", true, false, 1.0, [], {}]
          # 各型態的原始 JSON 值送到 HTTP，測試客戶端不先轉成字串或整數。
          bad_types.each do |value|
            error(call(:rails, "PUT", path, token: token, body: valid.merge(field => value)), 422, "E0001", { field => ["欄位型態不正確"] })
          end
        end
      end
      # 關閉狀態也必須逐欄拒絕數值越界。
      { "window_minutes" => 60, "failure_threshold" => 100, "lock_seconds" => 86_400 }.each do |field, maximum|
        # 同時涵蓋零、負值及第一個超上限值。
        [0, -1, maximum + 1].each do |value|
          error(call(:rails, "PUT", settings, token: token, body: defaults.merge("enabled" => false, field => value)), 422, "E0001", { field => ["欄位格式或範圍不正確"] })
        end
      end
      # 狀態是固定列舉，不做大小寫或空白正規化。
      ["ACTIVE", "", "disabled "].each do |value|
        error(call(:rails, "PUT", status_path, token: token, body: { status: value }), 422, "E0001", { "status" => ["欄位格式或範圍不正確"] })
      end
      assert_equal defaults, call(:rails, "GET", settings, token: token)[1].fetch("data")
      # 聚合獨立欄位錯誤，並保留必填/null/type/range 優先順序。
      error(call(:rails, "PUT", settings, token: token, body: { enabled: nil, failure_threshold: true, lock_seconds: 0, extra: 1 }), 422, "E0001",
            { "enabled" => ["此欄位不可為 null"], "window_minutes" => ["此欄位為必填"], "failure_threshold" => ["欄位型態不正確"], "lock_seconds" => ["欄位格式或範圍不正確"], "extra" => ["不支援此欄位"] })
      # 三個數值的最小／最大合法組合均由另一後端完整讀回。
      [1, 60].each do |window|
        values = defaults.merge("window_minutes" => window, "failure_threshold" => window == 1 ? 1 : 100, "lock_seconds" => window == 1 ? 1 : 86_400)
        content_success(call(:rails, "PUT", settings, token: token, body: values))
        assert_equal values, call(:laravel, "GET", settings, token: administrator.fetch(:laravel).fetch("access_token"))[1].fetch("data")
      end
    end
    # 同參數重複優先於非法值；巢狀keyword型態不可被URL parser吞掉。
    base = "/api/members?page=1&per_page=20"
    [["&page=x", "page", "此參數不可重複"], ["&x=1", "x", "不支援此欄位"], ["&filters%5Bkeyword%5D%5B%5D=x", "filters.keyword", "欄位型態不正確"], ["&filters%5Bkeyword%5D%5Bx%5D=y", "filters.keyword", "欄位型態不正確"], ["&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y", "filters.keyword", "此參數不可重複"], ["&filters%5Bkeyword%5D=" + "x" * 255, "filters.keyword", "欄位格式或範圍不正確"], ["&filters%5Bkeyword%5D=%FF", "filters.keyword", "欄位格式或範圍不正確"]].each do |suffix, field, message|
      error(call(:rails, "GET", base + suffix, token: token), 422, "E0001", { field => [message] })
    end
    error(call(:rails, "GET", "/api/members", token: token), 422, "E0001", { "page" => ["此欄位為必填"], "per_page" => ["此欄位為必填"] })
    error(call(:rails, "GET", "/api/comments?page=1&per_page=101", token: token), 422, "E0001", { "per_page" => ["欄位格式或範圍不正確"] })
  end

  # 明確 management 才含下架文章；管理留言包含下架父文與個別下架留言，DTO 不洩 Email。
  def test_administration_content_management_scope
    administrator, actor = content_actor("listadmin", admin: true), content_actor("listauthor")
    token = administrator.fetch(:rails).fetch("access_token")
    article = content_article(actor)
    first = content_comment(actor, article.fetch("id"))
    second = content_comment(actor, article.fetch("id"))
    content_success(call(:rails, "PUT", "/api/comments/#{second.fetch('id')}/visibility", token: token, body: { visibility: "hidden", version: 1 }))
    content_success(call(:rails, "PUT", "/api/articles/#{article.fetch('id')}/visibility", token: token, body: { visibility: "hidden", version: 1 }))
    error(call(:rails, "GET", "/api/articles/#{article.fetch('id')}"), 404, "E0004")
    path = "/api/articles?page=1&per_page=100"
    # 未指定 scope 的管理員仍使用公開列表；管理範圍才允許讀取所有 hidden。
    refute_includes call(:rails, "GET", path, token: token)[1].fetch("data").fetch("list").map { |row| row.fetch("id") }, article.fetch("id")
    # 兩後端管理入口必須顯示相同範圍與安全 DTO。
    [:rails, :laravel].each do |backend|
      admin = administrator.fetch(backend).fetch("access_token")
      managed = call(backend, "GET", "#{path}&filters%5Bscope%5D=management", token: admin)
      assert_equal 200, managed.first
      # 以剛建立 ID 定位，避免同庫舊測資讓列表總數假設失效。
      row = managed[1].fetch("data").fetch("list").find { |item| item.fetch("id") == article.fetch("id") }
      assert_equal ["hidden", true, 2], row.values_at("visibility", "can_restore", "version")
      content_dto(row, actor.fetch(:rails).fetch("user"))
      comments = call(backend, "GET", "/api/comments?page=1&per_page=100", token: admin)
      assert_equal 200, comments.first
      # 留言僅選本次父文集合，其他測資保留不刪除。
      rows = comments[1].fetch("data").fetch("list").select { |item| item.fetch("article_id") == article.fetch("id") }
      assert_equal [second.fetch("id"), first.fetch("id")], rows.map { |item| item.fetch("id") }
      # 每列都套用完整 DTO 形狀，防止只有第一列正確。
      rows.each do |item|
        content_dto(item, actor.fetch(:rails).fetch("user"), comment: true)
      end
    end
  end

  # 四欄設定由真實 API 原子提交；關閉只換會員世代及解鎖，不刪事件或撤銷有效登入。
  def test_administration_settings_shared_disable_and_noop
    administrator = content_actor("settingsadmin", admin: true)
    # 固定兩次錯誤可辨識跨後端共用計數，再復原原四欄設定。
    with_lock_settings(threshold: 2, seconds: 300) do
      # 關閉來源輪替兩後端，避免只驗既有 Laravel 行為。
      [:rails, :laravel].each do |backend|
        actor = content_actor("settings#{backend}")
        identity = actor.fetch(:rails)
        id = identity.fetch("user").fetch("id")
        email = identity.fetch("user").fetch("email")
        token = administrator.fetch(backend).fetch("access_token")
        path = "/api/settings/login-lockout"
        enabled = { enabled: true, window_minutes: 5, failure_threshold: 2, lock_seconds: 300 }
        content_success(call(backend, "PUT", path, token: token, body: enabled))
        error(call(:rails, "POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 401, "E0101")
        error(call(:laravel, "POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 429, "E0105")
        events = @db.exec_params("SELECT * FROM login_failures WHERE account_id=$1 ORDER BY id", [id]).to_a
        before = @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
        disabled = enabled.merge(enabled: false, window_minutes: 1, failure_threshold: 3, lock_seconds: 1)
        all_accounts = @db.exec("SELECT id,role,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
        all_failures = @db.exec("SELECT * FROM login_failures ORDER BY id").to_a
        all_sessions = @db.exec("SELECT * FROM login_sessions ORDER BY jti").to_a
        content_success(call(backend, "PUT", path, token: token, body: disabled))
        # 每位一般會員（含停用者）都只換一次世代並清鎖；管理員與全體登入／失敗歷史不變。
        expected_accounts = all_accounts.map do |row|
          row.fetch("role") == "member" ? row.merge("failure_epoch" => row.fetch("failure_epoch") + 1, "locked_until" => nil) : row
        end
        assert_equal expected_accounts, @db.exec("SELECT id,role,status,failure_epoch,locked_until FROM accounts ORDER BY id").to_a
        assert_equal all_failures, @db.exec("SELECT * FROM login_failures ORDER BY id").to_a
        assert_equal all_sessions, @db.exec("SELECT * FROM login_sessions ORDER BY jti").to_a
        # 另一後端讀回完整四欄；保留失敗歷史與有效登入，確認不是刪資料或全體登出。
        actor.each do |origin, login_data|
          assert_equal disabled.transform_keys(&:to_s), call(origin, "GET", path, token: administrator.fetch(origin).fetch("access_token"))[1].fetch("data")
          assert_equal 200, call(origin, "GET", "/api/me", token: login_data.fetch("access_token")).first
        end
        after = @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
        assert_equal before.fetch("failure_epoch") + 1, after.fetch("failure_epoch")
        assert_nil after.fetch("locked_until")
        assert_equal events, @db.exec_params("SELECT * FROM login_failures WHERE account_id=$1 ORDER BY id", [id]).to_a
        content_success(call(backend, "PUT", path, token: token, body: disabled.to_a.reverse.to_h))
        content_success(call(backend, "PUT", path, token: token, body: disabled.merge(lock_seconds: 2)))
        assert_equal after, @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
        error(call(:rails, "POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 401, "E0101")
        assert_equal events, @db.exec_params("SELECT * FROM login_failures WHERE account_id=$1 ORDER BY id", [id]).to_a
        content_success(call(backend, "PUT", path, token: token, body: enabled))
        error(call(:laravel, "POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 401, "E0101")
        error(call(:rails, "POST", "/api/auth/login", body: { email: email, password: "Wrong1234" }), 429, "E0105")
        locked = @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
        content_success(call(backend, "PUT", path, token: token, body: enabled.merge(window_minutes: 60, failure_threshold: 100, lock_seconds: 86_400)))
        assert_equal locked, @db.exec_params("SELECT failure_epoch,locked_until FROM accounts WHERE id=$1", [id]).first
      end
    end
  end

  # 停用撤銷所有後端登入；恢復不復活登入，也不改內容或未到期暫時鎖定。
  def test_administration_status_cross_backend_revocation_and_noop
    administrator = content_actor("statusadmin", admin: true)
    # 兩種管理來源都必須影響兩種登入來源，不能只撤銷 Rails session。
    [:rails, :laravel].each do |backend|
      actor = content_actor("status#{backend}")
      article = content_article(actor)
      id = actor.fetch(:rails).fetch("user").fetch("id")
      token = administrator.fetch(backend).fetch("access_token")
      path = "/api/members/#{id}/status"
      owner.exec_params("UPDATE accounts SET locked_until=clock_timestamp()+interval '5 minutes' WHERE id=$1", [id])
      before = @db.exec_params("SELECT * FROM accounts WHERE id=$1", [id]).first
      content_success(call(backend, "PUT", path, token: token, body: { status: "disabled" }))
      # 可識別的停用身分優先於 session 已撤銷；重複停用不改撤銷時間。
      revoked = @db.exec_params("SELECT jti,revoked_at FROM login_sessions WHERE account_id=$1 ORDER BY jti", [id]).to_a
      # 每個已知登入都必須撤銷，而非僅目前後端或其中一個 jti。
      assert revoked.all? { |row| row.fetch("revoked_at") }
      content_success(call(backend, "PUT", path, token: token, body: { status: "disabled" }))
      assert_equal revoked, @db.exec_params("SELECT jti,revoked_at FROM login_sessions WHERE account_id=$1 ORDER BY jti", [id]).to_a
      # 可識別停用身分在兩後端都優先回停用錯誤。
      actor.each do |origin, identity|
        error(call(origin, "GET", "/api/me", token: identity.fetch("access_token")), 403, "E0102")
      end
      content_success(call(backend, "PUT", path, token: token, body: { status: "active" }))
      # 恢復後舊登入仍無效，新登入仍受原未到期鎖定限制。
      actor.each do |origin, identity|
        error(call(origin, "GET", "/api/me", token: identity.fetch("access_token")), 401, "E0002")
        error(call(origin, "POST", "/api/auth/login", body: { email: identity.fetch("user").fetch("email"), password: "Demo1234" }), 429, "E0105")
      end
      assert_equal before, @db.exec_params("SELECT * FROM accounts WHERE id=$1", [id]).first
      assert_equal article, call(:rails, "GET", "/api/articles/#{article.fetch('id')}")[1].fetch("data")
    end
  end

  # 管理讀取先驗角色，再查一般會員；字面搜尋不把萬用符號或 Unicode 改寫。
  def test_administration_members_scope_search_and_detail
    administrator = content_actor("manager", admin: true)
    actor = content_actor("search")
    token = administrator.fetch(:rails).fetch("access_token")
    id = actor.fetch(:rails).fetch("user").fetch("id")
    nickname = "#{@namespace}A_B%\\É中"
    owner.exec_params("UPDATE accounts SET nickname=$1,status='disabled' WHERE id=$2", [nickname, id])
    # 同名管理員與相似會員都是反例，不能只驗證搜尋命中筆數。
    owner.exec_params("UPDATE accounts SET nickname=$1 WHERE id=$2", [nickname, administrator.fetch(:rails).fetch("user").fetch("id")])
    other = content_actor("other")
    owner.exec_params("UPDATE accounts SET nickname=$1 WHERE id=$2", ["#{@namespace}A1Bé", other.fetch(:rails).fetch("user").fetch("id")])
    path = "/api/members?page=1&per_page=100"
    # 暱稱／Email 各自命中同一精確 ID，管理員同名反例必須排除。
    ["#{@namespace}a_b", "#{@namespace}A_B%\\É中", actor.fetch(:rails).fetch("user").fetch("email").upcase].each do |keyword|
      result = call(:rails, "GET", "#{path}&filters%5Bkeyword%5D=#{URI.encode_www_form_component(keyword)}", token: token)
      assert_equal 200, result.first
      data = result[1].fetch("data")
      assert_equal [id], data.fetch("list").map { |row| row.fetch("id") }
      assert_equal 1, data.fetch("pagination").fetch("total")
      row = data.fetch("list").first
      assert_equal %w[id email nickname role status created_at].sort, row.keys.sort
      assert_equal "disabled", row.fetch("status")
      assert_equal row, call(:rails, "GET", "/api/members/#{id}", token: token)[1].fetch("data")
    end
    error(call(:rails, "GET", "/api/members/#{administrator.fetch(:rails).fetch('user').fetch('id')}", token: token), 404, "E0004")
    error(call(:rails, "GET", "/api/members/nope?x=1", token: other.fetch(:rails).fetch("access_token")), 403, "E0003")
    error(call(:rails, "GET", path), 401, "E0002")
    error(call(:rails, "GET", "#{path}&filters%5Bkeyword%5D=%00", token: token), 422, "E0001", { "filters.keyword" => ["欄位格式或範圍不正確"] })
  end
end
