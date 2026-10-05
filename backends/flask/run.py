"""啟動 Flask WSGI 應用，供本機 Compose 入口使用。"""

from site_api.views import create_app

app = create_app()

if __name__ == "__main__":
    # 本機展示與既有 Python 後端一致，容器由 Compose 管理生命週期。
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
