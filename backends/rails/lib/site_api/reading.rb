require_relative "authentication"

module SiteApi
  module Reading
    module_function

    # 一般會員列表與詳情共用本人 DTO，管理員帳號不屬於管理目標。
    def member(db, id)
      row = db.exec_params("SELECT id,email,nickname,role,status,created_at FROM accounts WHERE role='member' AND id=$1", [id]).first
      raise Error.new("E0004") unless row
      Authentication.account_dto(row)
    end

    # count 與分頁使用完全相同條件；strpos 按字面包含，translate 只摺疊 ASCII。
    def members(db, page, per_page, keyword)
      parameters = keyword.empty? ? [] : [keyword.tr("A-Z", "a-z")]
      source = "FROM accounts WHERE role='member'"
      unless keyword.empty?
        source += " AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') COLLATE \"C\",$1)>0 " \
                  "OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') COLLATE \"C\",$1)>0)"
      end
      total = db.exec_params("SELECT count(*) #{source}", parameters).first.fetch("count")
      offset, rows = (page - 1) * per_page, []
      if offset < total
        index = parameters.length
        rows = db.exec_params("SELECT id,email,nickname,role,status,created_at #{source} ORDER BY created_at DESC,id DESC LIMIT $#{index + 1} OFFSET $#{index + 2}",
                              [*parameters, per_page, offset]).to_a
      end
      {
        # 投影核准欄位，不把失敗世代、密碼或登入資料洩漏至管理列表。
        list: rows.map do |row|
          Authentication.account_dto(row)
        end,
        pagination: { page: page, per_page: per_page, total: total, total_pages: (total + per_page - 1) / per_page }
      }
    end

    # 對外內容 DTO 僅含公開作者資訊；恢復旗標依目前身分動態判定。
    def content_dto(row, viewer, comment: false)
      restore = row["visibility"] == "hidden" && viewer &&
                (viewer["role"] == "admin" || viewer["id"] == row["author_id"] && row["restore_control"] == "author")
      result = row.slice("id", "body", "version", "created_backend", "visibility").merge(
        "author" => { "id" => row.fetch("author_id"), "nickname" => row.fetch("nickname") },
        "created_at" => Authentication.business_time(row.fetch("created_at")),
        "updated_at" => Authentication.business_time(row.fetch("updated_at")), "can_restore" => !!restore
      )
      result[comment ? "article_id" : "title"] = row.fetch(comment ? "article_id" : "title")
      result
    end

    # 父文章／詳情只讓作者或管理員預覽 hidden，其餘與不存在相同回 404。
    def readable_article(db, id, viewer, lock: false)
      sql = "SELECT articles.*, accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.id=$1"
      row = db.exec_params(sql + (lock ? " FOR UPDATE OF articles" : ""), [id]).first
      if !row || row["visibility"] == "hidden" && (!viewer || viewer["role"] != "admin" && viewer["id"] != row["author_id"])
        raise Error.new("E0004")
      end
      row
    end

    # 作者與管理員可獨立讀留言；其他人先隱藏不可讀資料，再回作者專用詳情的 403。
    def readable_comment(db, id, viewer, lock: false)
      # 探索關聯不先鎖留言；父 ID 不可由應用帳號修改，仍在鎖後再次比對。
      parent = if lock
                 db.exec_params("SELECT id FROM articles WHERE id=(SELECT article_id FROM comments WHERE id=$1) FOR UPDATE", [id]).first
               end
      sql = "SELECT comments.*, accounts.nickname, articles.visibility AS article_visibility " \
            "FROM comments JOIN accounts ON accounts.id=comments.author_id JOIN articles ON articles.id=comments.article_id WHERE comments.id=$1"
      row = db.exec_params(sql + (lock ? " FOR UPDATE OF comments" : ""), [id]).first
      raise Error.new("E0004") if !row || lock && (!parent || row["article_id"] != parent["id"])
      if viewer["role"] != "admin" && viewer["id"] != row["author_id"]
        raise Error.new("E0004") if row["visibility"] == "hidden" || row["article_visibility"] == "hidden"
        raise Error.new("E0003") unless lock
      end
      row
    end

    # 計數與資料採相同條件、固定排序及作者 JOIN；超頁不作無界讀取或改寫頁碼。
    def page(db, viewer, page, per_page, article_id: nil, mine: nil, management: nil)
      # management 只由已驗角色的 controller 固定傳入，與作者／公開範圍分開選取條件。
      table = management || mine || (article_id ? "comments" : "articles")
      parameters = mine ? [viewer.fetch("id")] : article_id ? [article_id] : []
      where = if management
                ""
              elsif mine
                "WHERE #{table}.author_id=$1"
              elsif article_id
                "WHERE comments.article_id=$1"
              else
                "WHERE articles.visibility='visible'"
              end
      where += " AND comments.visibility='visible'" if !mine && article_id && (!viewer || viewer["role"] != "admin")
      source = "FROM #{table} JOIN accounts ON accounts.id=#{table}.author_id #{where}"
      total = db.exec_params("SELECT count(*) #{source}", parameters).first.fetch("count")
      offset = (page - 1) * per_page
      rows = []
      if offset < total
        index = parameters.length
        rows = db.exec_params("SELECT #{table}.*, accounts.nickname #{source} ORDER BY #{table}.created_at DESC, #{table}.id DESC LIMIT $#{index + 1} OFFSET $#{index + 2}",
                              [*parameters, per_page, offset]).to_a
      end
      {
        # 列表與詳情共用同形 DTO，不因 JOIN 讀到內部欄位而直接公開。
        list: rows.map do |row|
          content_dto(row, viewer, comment: table == "comments")
        end,
        pagination: { page: page, per_page: per_page, total: total, total_pages: (total + per_page - 1) / per_page }
      }
    end
  end
end
