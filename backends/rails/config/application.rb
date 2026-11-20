require_relative "boot"
require "rails"
require "action_controller/railtie"
require_relative "../lib/site_api/boundary"

module ArticlesRails
  class Application < Rails::Application
    config.load_defaults 8.1
    config.api_only = true
    config.eager_load = true
    config.secret_key_base = SecureRandom.hex(64)
    config.hosts.clear
    config.force_ssl = false
    config.log_level = :warn
    config.logger = Logger.new($stdout)
    config.action_dispatch.show_exceptions = :none
    config.middleware.insert_before 0, SiteApi::Boundary
  end
end
