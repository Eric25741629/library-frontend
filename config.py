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
ADMIN_PASSWORD = _cfg.get('admin_password', 'admin')
BARCODE_LOGIN_ENABLED = bool(_cfg.get('barcode_login_enabled', True))
REQUIRE_ADMIN_LOGIN = bool(_cfg.get('require_admin_login', False))
MAX_RETURN_LIMIT = int(_cfg.get('max_return_limit', 20))

MACHINE_CONFIG = _cfg.get('machine', {'port': '/dev/ttyUSB0', 'baudrate': 38400, 'mock_mode': True})
MACHINE_PORT = MACHINE_CONFIG.get('port', '/dev/ttyUSB0')
MACHINE_BAUDRATE = int(MACHINE_CONFIG.get('baudrate', 38400))
MACHINE_MOCK_MODE = bool(MACHINE_CONFIG.get('mock_mode', True))

LOG_BACKUP_DAYS = int((_cfg.get('logs') or {}).get('backup_days', 30))

# 重新載入函式（如需在執行時重讀設定）
def reload_config():
    global _cfg, ADMIN_PASSWORD, BARCODE_LOGIN_ENABLED, REQUIRE_ADMIN_LOGIN, MAX_RETURN_LIMIT
    global MACHINE_CONFIG, MACHINE_PORT, MACHINE_BAUDRATE, MACHINE_MOCK_MODE, LOG_BACKUP_DAYS
    if cfg_file.exists():
        try:
            _cfg = yaml.safe_load(cfg_file.read_text(encoding='utf-8')) or {}
        except Exception:
            _cfg = {}
    ADMIN_PASSWORD = _cfg.get('admin_password', 'admin')
    BARCODE_LOGIN_ENABLED = bool(_cfg.get('barcode_login_enabled', True))
    REQUIRE_ADMIN_LOGIN = bool(_cfg.get('require_admin_login', False))
    MAX_RETURN_LIMIT = int(_cfg.get('max_return_limit', 20))
    MACHINE_CONFIG = _cfg.get('machine', {'port': '/dev/ttyUSB0', 'baudrate': 38400, 'mock_mode': True})
    MACHINE_PORT = MACHINE_CONFIG.get('port', '/dev/ttyUSB0')
    MACHINE_BAUDRATE = int(MACHINE_CONFIG.get('baudrate', 38400))
    MACHINE_MOCK_MODE = bool(MACHINE_CONFIG.get('mock_mode', True))
    LOG_BACKUP_DAYS = int((_cfg.get('logs') or {}).get('backup_days', 30))