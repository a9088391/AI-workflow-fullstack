require_relative "reading"

module SiteApi
  module Writing
    module_function

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
