import logging
from logging.handlers import TimedRotatingFileHandler
import json
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
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# 全域變數 (將在 init_shared 中初始化)
cfg = {}
MAX_RETURN_LIMIT = 20
BOOK_CHECK_ENABLED = True
ADMIN_PASSWORD = "admin"
BARCODE_LOGIN_ENABLED = True
REQUIRE_ADMIN_LOGIN = False

# 全域物件
machine = None
sip2 = None

# --- Logger 設定 ---
def make_timed_handler(name, backup_days=30):
    h = TimedRotatingFileHandler(LOG_DIR / f'{name}.log', when='midnight', backupCount=backup_days, encoding='utf-8')
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
                  return_time TEXT)''')
    
    # 建立 box_history 如果不存在 (用於歸檔)
    c.execute('''CREATE TABLE IF NOT EXISTS box_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  book_id TEXT,
                  title TEXT,
                  image_url TEXT,
                  return_time TEXT,
                  clear_time TEXT)''')
                  
    conn.commit()
    conn.close()

def get_box_db():
    conn = sqlite3.connect(BOX_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def is_box_full():
    conn = get_box_db()
    try:
        count = conn.execute('SELECT COUNT(*) FROM box_inventory').fetchone()[0]
    except:
        count = 0
    finally:
        conn.close()
    return count >= MAX_RETURN_LIMIT

# --- 初始化函式 ---
def load_config():
    global cfg, MAX_RETURN_LIMIT, BOOK_CHECK_ENABLED, ADMIN_PASSWORD, BARCODE_LOGIN_ENABLED, REQUIRE_ADMIN_LOGIN
    if CONFIG_FILE.exists():
        try:
            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
        except Exception as e:
            print(f"Failed to load config.yml: {e}")
            cfg = {}
    
    MAX_RETURN_LIMIT = int(cfg.get('max_return_limit', 20))
    BOOK_CHECK_ENABLED = cfg.get('book_check_enabled', True)
    ADMIN_PASSWORD = cfg.get('admin_password', "admin")
    BARCODE_LOGIN_ENABLED = cfg.get('barcode_login_enabled', True)
    REQUIRE_ADMIN_LOGIN = cfg.get('require_admin_login', False)

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
        sip2 = MockSIP2Client(host, port, user, pwd, institution_id=inst)
    else:
        try:
            if sip2:
                try: sip2.close()
                except: pass
            
            sip2 = SIP2Client(host, port, user, pwd, institution_id=inst)
            # 嘗試連線
            if sip2.connect():
                sip2.login()
        except Exception as e:
            logger.error(f"Failed to init SIP2 client: {e}")

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
    setup_logging()
    init_machine_controller()
    init_sip2_client()
    init_box_db()