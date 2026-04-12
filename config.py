# config shim - 讀取 config.yml 並提供向後相容的變數
import yaml
from pathlib import Path

cfg_file = Path('config.yml')
_cfg = {}
if cfg_file.exists():
    try:
        _cfg = yaml.safe_load(cfg_file.read_text(encoding='utf-8')) or {}
    except Exception:
        _cfg = {}

# 預設值與對外變數 (向後相容)
VERSION = "0.9.4"
ADMIN_USERNAME = _cfg.get('admin_username', 'admin')
ADMIN_PASSWORD = _cfg.get('admin_password', 'admin')
BARCODE_LOGIN_ENABLED = bool(_cfg.get('barcode_login_enabled', True))
REQUIRE_ADMIN_LOGIN = bool(_cfg.get('require_admin_login', False))
MAX_RETURN_LIMIT = int(_cfg.get('max_return_limit', 20))

MACHINE_CONFIG = _cfg.get('machine', {'port': '/dev/uno', 'baudrate': 38400})
MACHINE_PORT = MACHINE_CONFIG.get('port', '/dev/uno')
MACHINE_BAUDRATE = int(MACHINE_CONFIG.get('baudrate', 38400))

LOG_BACKUP_DAYS = int((_cfg.get('logs') or {}).get('backup_days', 30))

LIBRARY_CONFIG = _cfg.get('library', {})
LIBRARY_MOCK_ENABLED = bool(LIBRARY_CONFIG.get('mock_enabled', False))
LIBRARY_HOST = LIBRARY_CONFIG.get('host', '192.168.1.100')
LIBRARY_PORT = int(LIBRARY_CONFIG.get('port', 6001))
LIBRARY_LOGIN_ENABLED = bool(LIBRARY_CONFIG.get('login_enabled', False))
LIBRARY_USER = LIBRARY_CONFIG.get('login_user', '')
LIBRARY_PASS = LIBRARY_CONFIG.get('login_pass', '')
LIBRARY_INSTITUTION = LIBRARY_CONFIG.get('institution_id', 'MAIN')
LIBRARY_CHECKIN_ENABLED = bool(LIBRARY_CONFIG.get('checkin_enabled', False))
LIBRARY_CURRENT_LOCATION = LIBRARY_CONFIG.get('current_location', LIBRARY_INSTITUTION)
ATTACHMENT_ACCEPTANCE_ENABLED = bool(LIBRARY_CONFIG.get('attachment_acceptance_enabled', True))

# 重新載入函式（如需在執行時重讀設定）
def reload_config():
    global _cfg, ADMIN_USERNAME, ADMIN_PASSWORD, BARCODE_LOGIN_ENABLED, REQUIRE_ADMIN_LOGIN, MAX_RETURN_LIMIT
    global MACHINE_CONFIG, MACHINE_PORT, MACHINE_BAUDRATE, LOG_BACKUP_DAYS
    global LIBRARY_CONFIG, LIBRARY_MOCK_ENABLED, LIBRARY_HOST, LIBRARY_PORT, LIBRARY_LOGIN_ENABLED, LIBRARY_USER, LIBRARY_PASS, LIBRARY_INSTITUTION, LIBRARY_CURRENT_LOCATION, ATTACHMENT_ACCEPTANCE_ENABLED
    
    if cfg_file.exists():
        try:
            _cfg = yaml.safe_load(cfg_file.read_text(encoding='utf-8')) or {}
        except Exception:
            _cfg = {}
    ADMIN_USERNAME = _cfg.get('admin_username', 'admin')
    ADMIN_PASSWORD = _cfg.get('admin_password', 'admin')
    BARCODE_LOGIN_ENABLED = bool(_cfg.get('barcode_login_enabled', True))
    REQUIRE_ADMIN_LOGIN = bool(_cfg.get('require_admin_login', False))
    MAX_RETURN_LIMIT = int(_cfg.get('max_return_limit', 20))
    
    MACHINE_CONFIG = _cfg.get('machine', {'port': '/dev/uno', 'baudrate': 38400})
    MACHINE_PORT = MACHINE_CONFIG.get('port', '/dev/uno')
    MACHINE_BAUDRATE = int(MACHINE_CONFIG.get('baudrate', 38400))
    
    LOG_BACKUP_DAYS = int((_cfg.get('logs') or {}).get('backup_days', 30))
    
    LIBRARY_CONFIG = _cfg.get('library', {})
    LIBRARY_MOCK_ENABLED = bool(LIBRARY_CONFIG.get('mock_enabled', False))
    LIBRARY_HOST = LIBRARY_CONFIG.get('host', '192.168.1.100')
    LIBRARY_PORT = int(LIBRARY_CONFIG.get('port', 6001))
    LIBRARY_LOGIN_ENABLED = bool(LIBRARY_CONFIG.get('login_enabled', False))
    LIBRARY_USER = LIBRARY_CONFIG.get('login_user', '')
    LIBRARY_PASS = LIBRARY_CONFIG.get('login_pass', '')
    LIBRARY_INSTITUTION = LIBRARY_CONFIG.get('institution_id', 'MAIN')
    LIBRARY_CHECKIN_ENABLED = bool(LIBRARY_CONFIG.get('checkin_enabled', False))
    LIBRARY_CURRENT_LOCATION = LIBRARY_CONFIG.get('current_location', LIBRARY_INSTITUTION)
    ATTACHMENT_ACCEPTANCE_ENABLED = bool(LIBRARY_CONFIG.get('attachment_acceptance_enabled', True))