require_relative "authentication"

module SiteApi
  module Reading
    module_function

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
    def readable_article(db, id, viewer)
      row = db.exec_params("SELECT articles.*, accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.id=$1", [id]).first
      if !row || row["visibility"] == "hidden" && (!viewer || viewer["role"] != "admin" && viewer["id"] != row["author_id"])
        raise Error.new("E0004")
      end
      row
    end

    # 計數與資料採相同條件、固定排序及作者 JOIN；超頁不作無界讀取或改寫頁碼。
    def page(db, viewer, page, per_page, article_id: nil)
      table = article_id ? "comments" : "articles"
      parameters = article_id ? [article_id] : []
      where = article_id ? "WHERE comments.article_id=$1" : "WHERE articles.visibility='visible'"
      where += " AND comments.visibility='visible'" if article_id && (!viewer || viewer["role"] != "admin")
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
          content_dto(row, viewer, comment: !article_id.nil?)
        end,
        pagination: { page: page, per_page: per_page, total: total, total_pages: (total + per_page - 1) / per_page }
      }
    end
  end
end
