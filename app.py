from flask import Flask
from datetime import timedelta
import logging

import shared
from routes.views import views_bp
from routes.api import api_bp
from routes.admin_api import admin_api_bp
from routes.hardware_api import hardware_api_bp

# 初始化共用組件（配置、日誌、資料庫、機器、SIP2）
shared.init_shared()

app = Flask(__name__)
app.secret_key = shared.cfg.get('secret_key', 'yuntech_library_secret_key')
app.permanent_session_lifetime = timedelta(minutes=int(shared.cfg.get('session_minutes', 5)))

# 註冊 Blueprints
app.register_blueprint(views_bp)
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(admin_api_bp, url_prefix='/api/admin')
app.register_blueprint(hardware_api_bp, url_prefix='/api/hardware')

# 啟動背景任務
shared.start_background_tasks()

logger = logging.getLogger(__name__)
logger.info("Application initialized.")

if __name__ == '__main__':
    # Start without the reloader to avoid duplicate processes and ensure console logs appear in this process.
    app.run(debug=False, use_reloader=False, port=5000)
