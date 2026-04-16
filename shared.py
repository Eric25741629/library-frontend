import logging
from logging.handlers import TimedRotatingFileHandler
import json
import os
from pathlib import Path
import yaml
import sys
import sqlite3
import threading
import time
from datetime import datetime, timedelta

# 引入控制器
from machine_controller import MachineController
from sip2_client import SIP2Client, MockSIP2Client
import config as global_config

# --- 配置與常數 ---
CONFIG_FILE = Path('config.yml')
MACHINE_CONFIG_FILE = Path('machine_config.json')
BOX_DB_FILE = 'return_box.db'

# 設定基礎目錄
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get('LOG_DIR', str(BASE_DIR / 'logs'))).expanduser()
LOG_DIR.mkdir(exist_ok=True)
LOCALES_DIR = BASE_DIR / 'locales'

# 全域變數 (將在 init_shared 中初始化)
# 介面版本：修改前後端行為時請同步更新，供後台顯示
# 介面版本：修改前後端行為時請同步更新，供後台顯示
# 目前釋出版本
APP_VERSION = "0.9.4"

cfg = {}
MAX_RETURN_LIMIT = 20
BOOK_CHECK_ENABLED = True
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"
BARCODE_LOGIN_ENABLED = True
REQUIRE_ADMIN_LOGIN = False
DEFAULT_LANG = 'zh-TW'
LOCALES = {}

# 當管理端主動執行「關閉前端介面」時，持續抑制自動重啟流程，
# 直到前端再次啟動後主動清除。
MANUAL_FRONTEND_STOP_ACTIVE = False
_MANUAL_FRONTEND_STOP_LOCK = threading.Lock()

# 全域物件
machine = None
sip2 = None

# --- Logger 設定 ---
def make_timed_handler(name, backup_days=30):
    """建立分資料夾的 TimedRotatingFileHandler：
    logs/
      app/app.log (最新)
      app/history/app.log.2026-01-14 (舊檔)
    """
    # 建立子資料夾與 history 子資料夾
    sub_dir = LOG_DIR / name
    history_dir = sub_dir / 'history'
    sub_dir.mkdir(exist_ok=True)
    history_dir.mkdir(exist_ok=True)

    base_file = sub_dir / f'{name}.log'
    h = TimedRotatingFileHandler(base_file, when='midnight', backupCount=backup_days, encoding='utf-8')

    # 自訂 rotator：將輪轉出的檔案移到 history 子資料夾
    def _rotator(source, dest):
        try:
            src_path = Path(source)
            dst_path = history_dir / src_path.name
            src_path.replace(dst_path)
        except Exception:
            # 若移動失敗，至少維持預設行為
            try:
                Path(source).replace(dest)
            except Exception:
                pass

    h.rotator = _rotator

    # 格式：時間在前，方便用工具逆向排序時最新在上
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    return h

def setup_logging():
    global cfg
    backup_days = int(cfg.get('logs', {}).get('backup_days', 30))
    
    # 建立各個 logger
    loggers = ['app', 'machine', 'library', 'frontend']
    for name in loggers:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(make_timed_handler(name, backup_days))
    
    # Root logger
    logging.root.setLevel(logging.INFO)
    logging.root.addHandler(make_timed_handler('app', backup_days))
    
    # Console output
    console_h = logging.StreamHandler(sys.stdout)
    console_h.setLevel(logging.DEBUG)
    console_h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.root.addHandler(console_h)

logger = logging.getLogger('shared')

# --- 資料庫函式 ---
def init_box_db():
    """初始化還書箱本地資料庫"""
    conn = sqlite3.connect(BOX_DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS box_inventory
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  book_id TEXT,
                  title TEXT,
                  image_url TEXT,
                  return_time TEXT,
                  target_bin INTEGER)''')
    
    # 建立 box_history 如果不存在 (用於歸檔)
    c.execute('''CREATE TABLE IF NOT EXISTS box_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  book_id TEXT,
                  title TEXT,
                  image_url TEXT,
                  return_time TEXT,
                  clear_time TEXT,
                  target_bin INTEGER)''')
    # Migration: add target_bin if missing (old DB)
    try:
        cols = [row[1] for row in c.execute("PRAGMA table_info('box_inventory')").fetchall()]
        if 'target_bin' not in cols:
            c.execute('ALTER TABLE box_inventory ADD COLUMN target_bin INTEGER')
        cols_h = [row[1] for row in c.execute("PRAGMA table_info('box_history')").fetchall()]
        if 'target_bin' not in cols_h:
            c.execute('ALTER TABLE box_history ADD COLUMN target_bin INTEGER')
        # Backfill target_bin for legacy rows based on book_id prefix
        c.execute("""
            UPDATE box_inventory
            SET target_bin = CASE
                WHEN target_bin IS NULL AND upper(substr(book_id,1,1)) IN ('C','G') THEN 1
                WHEN target_bin IS NULL THEN 2
                ELSE target_bin
            END
        """)
        c.execute("""
            UPDATE box_history
            SET target_bin = CASE
                WHEN target_bin IS NULL AND upper(substr(book_id,1,1)) IN ('C','G') THEN 1
                WHEN target_bin IS NULL THEN 2
                ELSE target_bin
            END
        """)
    except Exception as e:
        logger.warning(f"DB migration for target_bin failed: {e}")
                  
    conn.commit()
    conn.close()

def get_box_db():
    conn = sqlite3.connect(BOX_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def is_box_full():
    conn = get_box_db()
    try:
        rows = conn.execute('SELECT target_bin, COUNT(*) FROM box_inventory GROUP BY target_bin').fetchall()
        counts = {int(r[0]): r[1] for r in rows if r[0] is not None}
        bin1 = counts.get(1, 0)
        bin2 = counts.get(2, 0)
    except Exception:
        bin1 = 0
        bin2 = 0
    finally:
        conn.close()
    return bin1 >= MAX_RETURN_LIMIT or bin2 >= MAX_RETURN_LIMIT

def get_bin_counts():
    """取得箱內分櫃數量"""
    conn = get_box_db()
    try:
        rows = conn.execute('SELECT target_bin, COUNT(*) FROM box_inventory GROUP BY target_bin').fetchall()
        counts = {int(r[0]): r[1] for r in rows if r[0] is not None}
        return {
            "1": counts.get(1, 0),
            "2": counts.get(2, 0)
        }
    except Exception:
        return {"1": 0, "2": 0}
    finally:
        conn.close()


def mark_manual_frontend_stop():
    """標記前端為人工關閉，持續忽略全螢幕退出自動重啟。"""
    global MANUAL_FRONTEND_STOP_ACTIVE
    with _MANUAL_FRONTEND_STOP_LOCK:
        MANUAL_FRONTEND_STOP_ACTIVE = True


def clear_manual_frontend_stop():
    """前端重新啟動後，解除人工關閉保護狀態。"""
    global MANUAL_FRONTEND_STOP_ACTIVE
    with _MANUAL_FRONTEND_STOP_LOCK:
        MANUAL_FRONTEND_STOP_ACTIVE = False


def is_manual_frontend_stop_active() -> bool:
    """目前是否處於人工關閉前端的保護狀態。"""
    with _MANUAL_FRONTEND_STOP_LOCK:
        return bool(MANUAL_FRONTEND_STOP_ACTIVE)

# --- 初始化函式 ---
def load_config():
    global cfg, MAX_RETURN_LIMIT, BOOK_CHECK_ENABLED, ADMIN_USERNAME, ADMIN_PASSWORD, BARCODE_LOGIN_ENABLED, REQUIRE_ADMIN_LOGIN, DEFAULT_LANG
    if CONFIG_FILE.exists():
        try:
            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
        except Exception as e:
            print(f"Failed to load config.yml: {e}")
            cfg = {}
    
    MAX_RETURN_LIMIT = int(cfg.get('max_return_limit', 20))
    BOOK_CHECK_ENABLED = cfg.get('book_check_enabled', True)
    ADMIN_USERNAME = cfg.get('admin_username', "admin")
    ADMIN_PASSWORD = cfg.get('admin_password', "admin")
    BARCODE_LOGIN_ENABLED = cfg.get('barcode_login_enabled', True)
    REQUIRE_ADMIN_LOGIN = cfg.get('require_admin_login', False)
    DEFAULT_LANG = cfg.get('default_lang', 'zh-TW')

def load_locales():
    global LOCALES
    LOCALES = {}
    try:
        if not LOCALES_DIR.exists():
            return
        for file in LOCALES_DIR.glob('*.json'):
            try:
                data = json.loads(file.read_text(encoding='utf-8'))
                LOCALES[file.stem] = data if isinstance(data, dict) else {}
            except Exception as e:
                logger.warning(f"Failed to load locale {file.name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to load locales: {e}")

def init_machine_controller():
    global machine
    machine_cfg = cfg.get('machine', {})
    port = machine_cfg.get('port', '/dev/uno')
    baudrate = machine_cfg.get('baudrate', 38400)
    
    # 優先讀取 machine_config.json
    try:
        if MACHINE_CONFIG_FILE.exists():
            mc_cfg = json.loads(MACHINE_CONFIG_FILE.read_text(encoding='utf-8'))
            port = mc_cfg.get('port', port)
            baudrate = int(mc_cfg.get('baudrate', baudrate))
    except Exception as e:
        logger.warning(f"Failed to read machine_config.json: {e}")

    machine = MachineController(port=port, baudrate=baudrate)

def init_sip2_client():
    global sip2
    # 從 global_config (config.py) 讀取設定
    # 確保使用最新配置
    global_config.reload_config()
    
    host = global_config.LIBRARY_HOST
    port = global_config.LIBRARY_PORT
    
    if global_config.LIBRARY_LOGIN_ENABLED:
        user = global_config.LIBRARY_USER
        pwd = global_config.LIBRARY_PASS
    else:
        user = ""
        pwd = ""
        
    inst = global_config.LIBRARY_INSTITUTION
    mock_enabled = global_config.LIBRARY_MOCK_ENABLED
    
    logger.info(f"Initializing SIP2 Client: {host}:{port}, Inst={inst}, Mock={mock_enabled}")
    
    if mock_enabled or str(host).lower() == 'mock':
        logger.info("Using MockSIP2Client")
        sip2 = MockSIP2Client(host, port, user, pwd, institution_id=inst)
    else:
        try:
            if sip2:
                try: sip2.close()
                except: pass
            
            logger.info("Creating real SIP2Client")
            sip2 = SIP2Client(host, port, user, pwd, institution_id=inst)
            # 嘗試連線
            if sip2.connect():
                logger.info("SIP2 connection successful")
                if sip2.login():
                    logger.info("SIP2 login successful")
                else:
                    logger.warning("SIP2 login failed")
            else:
                logger.error("SIP2 connection failed")
        except Exception as e:
            logger.error(f"Failed to init SIP2 client: {e}")
            sip2 = None

# --- 背景任務 ---
def init_machine_async():
    """背景初始化機器"""
    try:
        logger.info("Starting machine initialization in background...")
        machine.init_machine()
        logger.info("Machine initialization completed.")
    except Exception as e:
        logger.error(f"Machine initialization failed: {e}")

def check_machine_idle():
    """定期檢查機器閒置"""
    while True:
        try:
            if machine:
                machine.check_idle()
        except Exception as e:
            logger.error(f"Error checking machine idle: {e}")
        time.sleep(60)

def start_background_tasks():
    # 1. 初始化機器
    threading.Thread(target=init_machine_async, daemon=True).start()
    # 2. 閒置檢查
    threading.Thread(target=check_machine_idle, daemon=True).start()

# --- 主初始化入口 ---
def init_shared():
    load_config()
    load_locales()
    setup_logging()
    init_machine_controller()
    init_sip2_client()
    init_box_db()