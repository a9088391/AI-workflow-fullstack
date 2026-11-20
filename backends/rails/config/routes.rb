# 統一入口保留原始 Body／Query；不讓 Rails params 的轉型改寫共同驗證順序。
Rails.application.routes.draw do
  match "*path", to: "api#dispatch_request", via: :all, format: false
  root to: "api#dispatch_request", via: :all
end
