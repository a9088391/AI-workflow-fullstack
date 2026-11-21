require_relative "reading"

module SiteApi
  module Writing
    module_function

    # 管理角色在參數前驗證；回傳 claims 供交易內鎖後重新查核登入。
    def administrator(request, db)
      account, claims = Authentication.identity(request, db)
      raise Error.new("E0003") unless account["role"] == "admin"
      [account, claims]
    end

    # 多帳號依 ID 取鎖後才鎖登入；實際停用才撤銷全部後端 session，恢復不改鎖定。
    def member_status(request, db, input, raw_id)
      account, claims = administrator(request, db)
      input.no_query
      id = input.integer(raw_id, "member_id")
      body = input.body(%w[status])
      # 參數通過才進入寫入交易；雙帳號固定取鎖順序避免交錯管理造成死鎖。
      db.transaction do
        db.exec_params("SELECT id FROM accounts WHERE id IN ($1,$2) ORDER BY id FOR UPDATE", [account.fetch("id"), id])
        db.exec_params("SELECT jti FROM login_sessions WHERE jti=$1 FOR UPDATE", [claims.fetch("jti")])
        administrator(request, db)
        # 管理員目標與不存在同為 404；外層認證不能取代此處目前資料判定。
        target = db.exec_params("SELECT role,status FROM accounts WHERE id=$1", [id]).first
        raise Error.new("E0004") unless target && target["role"] == "member"
        next if target["status"] == body.fetch("status")
        db.exec_params("UPDATE accounts SET status=$1 WHERE id=$2", [body.fetch("status"), id])
        if body.fetch("status") == "disabled"
          db.exec_params("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1 AND revoked_at IS NULL", [id])
        end
      end
      {}
    end

    # 設定列先序列化登入／管理更新；四欄及關閉副作用共用同一交易，系統失敗全部回滾。
    def lockout_settings(request, db, input)
      account, claims = administrator(request, db)
      input.no_query
      body = input.body(%w[enabled window_minutes failure_threshold lock_seconds])
      # 設定與登入共用第一把鎖，避免依過期設定累計或清除錯誤世代。
      db.transaction do
        settings = db.exec("SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1 FOR UPDATE").first
        raise Error.new("E0104") unless settings
        closing = settings.fetch("enabled") && !body.fetch("enabled")
        # 關閉需鎖全部帳號；其他更新只鎖管理者，均先帳號後登入並於鎖後重驗角色。
        if closing
          db.exec("SELECT id FROM accounts ORDER BY id FOR UPDATE")
        else
          db.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [account.fetch("id")])
        end
        db.exec_params("SELECT jti FROM login_sessions WHERE jti=$1 FOR UPDATE", [claims.fetch("jti")])
        administrator(request, db)
        next if settings == body
        db.exec_params("UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1",
                       body.values_at("enabled", "window_minutes", "failure_threshold", "lock_seconds"))
        # 只有 true→false 才換世代及清鎖；不刪歷史、不啟用停用帳號，也不撤銷任何登入。
        db.exec("UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE role='member'") if closing
      end
      {}
    end

    # 先認證與驗參數，交易內依帳號→session→文章→留言鎖定並重驗，避免撤銷後仍寫入。
    def execute(request, db, input, table:, action:, raw_id: nil)
      account, claims = Authentication.identity(request, db)
      input.no_query
      field = table == "comments" && action != :create ? "comment_id" : "article_id"
      id = input.integer(raw_id, field) if raw_id
      fields = if action == :visibility
                 %w[visibility version]
               else
                 (table == "articles" ? %w[title body] : %w[body]) + (action == :edit ? %w[version] : [])
               end
      body = input.body(fields, comment: table == "comments")
      # 外層讀取只用於鎖定身分；權限一律使用鎖後的目前帳號，所有鎖保留至提交。
      db.transaction do
        db.exec_params("SELECT id FROM accounts WHERE id=$1 FOR UPDATE", [account.fetch("id")])
        db.exec_params("SELECT jti FROM login_sessions WHERE jti=$1 FOR UPDATE", [claims.fetch("jti")])
        viewer, = Authentication.identity(request, db)
        if action == :create
          create(db, viewer, table, body, id)
        else
          row = table == "articles" ? Reading.readable_article(db, id, viewer, lock: true) : Reading.readable_comment(db, id, viewer, lock: true)
          mutate(db, viewer, table, row, body, action)
        end
      end
      {}
    end

    # 父文章公開檢查與留言 INSERT 共用文章鎖；建立時間和來源只由伺服器填入。
    def create(db, viewer, table, body, article_id)
      if table == "comments"
        parent = Reading.readable_article(db, article_id, viewer, lock: true)
        raise Error.new("E0003") unless parent["visibility"] == "visible"
      end
      now = db.exec("SELECT clock_timestamp() AS now").first.fetch("now")
      if table == "articles"
        db.exec_params("INSERT INTO articles(author_id,title,body,visibility,created_backend,created_at,updated_at) VALUES($1,$2,$3,'visible','rails',$4,$4)",
                       [viewer.fetch("id"), body.fetch("title"), body.fetch("body"), now])
      else
        db.exec_params("INSERT INTO comments(author_id,article_id,body,visibility,created_backend,created_at,updated_at) VALUES($1,$2,$3,'visible','rails',$4,$4)",
                       [viewer.fetch("id"), article_id, body.fetch("body"), now])
      end
    end

    # 可讀性已在取鎖時確認；先操作權再版本，同值也不能繞過過期衝突。
    def mutate(db, viewer, table, row, body, action)
      author = viewer["id"] == row["author_id"]
      raise Error.new("E0003") unless author || action == :visibility && viewer["role"] == "admin"
      if table == "comments" && action == :edit && row["article_visibility"] != "visible"
        raise Error.new("E0003")
      end
      changes = if action == :visibility
                  visibility_changes(row, viewer, body.fetch("visibility"))
                else
                  body.slice(*(table == "articles" ? %w[title body] : %w[body]))
                end
      raise Error.new("E0201") unless row["version"] == body.fetch("version")
      # 剔除未改變欄位，讓同值操作不觸發版本與時間更新。
      changes.reject! do |field, value|
        row[field] == value
      end
      return if changes.empty?
      parameters = changes.values
      # 每個白名單欄位對應同序綁定位置；作者／來源不在白名單，資料值也不插入 SQL。
      assignments = changes.keys.each_with_index.map do |field, index|
        "#{field}=$#{index + 1}"
      end
      index = parameters.length
      result = db.exec_params("UPDATE #{table} SET #{assignments.join(',')},version=version+1,updated_at=clock_timestamp() WHERE id=$#{index + 1} AND version=$#{index + 2} RETURNING id",
                              [*parameters, row.fetch("id"), body.fetch("version")])
      raise Error.new("E0201") if result.ntuples.zero?
    end

    # 管理員可接管他人下架內容；作者重複下架不能解除管理控制，自己的管理員仍依作者規則。
    def visibility_changes(row, viewer, visibility)
      if visibility == "visible"
        if row["visibility"] == "hidden" && row["restore_control"] == "admin" && viewer["role"] != "admin"
          raise Error.new("E0003")
        end
        { "visibility" => visibility, "restore_control" => nil }
      else
        control = if viewer["role"] == "admin" && viewer["id"] != row["author_id"]
                    "admin"
                  elsif row["visibility"] == "hidden"
                    row.fetch("restore_control")
                  else
                    "author"
                  end
        { "visibility" => visibility, "restore_control" => control }
      end
    end
  end
end
