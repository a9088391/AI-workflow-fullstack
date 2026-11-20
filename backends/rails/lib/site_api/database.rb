require "pg"

module SiteApi
  module Database
    module_function

    # 每個請求持有短連線；同一用途內共用交易，離開時釋放且不留未提交狀態。
    def open
      db = PG.connect(host: ENV.fetch("DB_HOST", "db"), port: ENV.fetch("DB_PORT", "5432"),
                      dbname: ENV.fetch("DB_DATABASE", "articles"), user: ENV.fetch("DB_USERNAME", "articles_app"),
                      password: ENV.fetch("DB_PASSWORD", ""), connect_timeout: 5)
      db.type_map_for_results = PG::BasicTypeMapForResults.new(db)
      db.type_map_for_queries = PG::BasicTypeMapForQueries.new(db)
      db.exec("SET TIME ZONE 'Asia/Taipei'")
      yield db
    ensure
      db&.close
    end
  end
end
