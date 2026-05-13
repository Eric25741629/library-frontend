import logging
from logging.handlers import TimedRotatingFileHandler
import json
import os
import queue
from pathlib import Path
import yaml
import sys
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import serial

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
APP_VERSION = "0.9.9 最終測試版"

cfg = {}
MAX_RETURN_LIMIT = 20
BOOK_CHECK_ENABLED = True
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"
BARCODE_LOGIN_ENABLED = True
REQUIRE_ADMIN_LOGIN = False
DEFAULT_LANG = 'zh-TW'
LOCALES = {}

# Watchdog 自動恢復旗標（預設 ON；可在 config.yml 的 watchdog 區塊關閉）
WATCHDOG_AUTO_RECOVER_MACHINE = True
WATCHDOG_AUTO_RECOVER_SIP2 = True

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
    global WATCHDOG_AUTO_RECOVER_MACHINE, WATCHDOG_AUTO_RECOVER_SIP2
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
    watchdog_cfg = cfg.get('watchdog', {}) if isinstance(cfg.get('watchdog', {}), dict) else {}
    WATCHDOG_AUTO_RECOVER_MACHINE = bool(watchdog_cfg.get('auto_recover_machine', True))
    WATCHDOG_AUTO_RECOVER_SIP2 = bool(watchdog_cfg.get('auto_recover_sip2', True))

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
    baudrate = machine_cfg.get('baudrate', 19200)
    
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


# ─── 狀態快取（B-2） ────────────────────────────────────────────────────────
#
# 原本 /api/status 每次呼叫都打 serial（machine.get_status，~200ms）+ SIP2
# health_check（~3ms），總共 ~0.3–2.1 秒。前端又每 15 秒輪詢，狀態變化最壞要
# 17 秒才出現在 UI 上。
#
# 改用「背景單一 thread 統一打 serial / SIP2 → 寫進 cache → /api/status 與
# /api/events SSE 都從 cache 讀」的架構：
#   - 不管前端有幾個 client 或多頻繁呼叫 /api/status，serial 流量都固定。
#   - /api/status 回應時間從 0.3–2.1s 變成 <10ms。
#   - 狀態真的改變時，背景 thread 透過 _state_subscribers 主動推給 SSE，
#     前端不必輪詢就能即時收到。
#   - SIP2 每 15 個 tick（15s）才檢查一次，避免拖累 SIP2 server。

_STATE_POLL_INTERVAL_SEC = 1.0
_SIP2_POLL_EVERY_N_TICKS = 15      # 每 15 秒檢查一次 SIP2 health

_state_cache = {
    'suspended': False,
    'limit': 0,
    'bin_counts': {},
    'library_ok': False,
    'machine_ok': False,
    'machine_state': 'unknown',
    'homing_in_progress': False,
    'is_homed': False,
    'machine_debug': {'ser_open': False, 'type': None},
    'last_updated': 0.0,
}
_state_cache_lock = threading.Lock()

# 訂閱者清單；每個訂閱者是一個 queue.Queue，背景 thread 偵測到狀態變化時 put
# 一份新狀態進去。SSE handler 從自己的 queue 取出來推給 client。
_state_subscribers = []
_state_subscribers_lock = threading.Lock()

# 標示狀態是否真的有改變（用於決定要不要 notify 訂閱者）
_STATE_CHANGE_FIELDS = (
    'suspended', 'library_ok', 'machine_ok', 'machine_state',
    'homing_in_progress', 'is_homed', 'bin_counts',
)


def get_cached_status():
    """非阻塞取得最新狀態快照（淺拷貝）。"""
    with _state_cache_lock:
        return dict(_state_cache)


def subscribe_state():
    """登記一個狀態變化訂閱者，回傳 Queue。SSE handler 從這個 Queue get。
    呼叫端結束時務必 unsubscribe_state(q) 釋放資源。"""
    q = queue.Queue(maxsize=16)
    with _state_subscribers_lock:
        _state_subscribers.append(q)
    return q


def unsubscribe_state(q):
    with _state_subscribers_lock:
        try:
            _state_subscribers.remove(q)
        except ValueError:
            pass


def _notify_subscribers(snapshot):
    with _state_subscribers_lock:
        targets = list(_state_subscribers)
    for q in targets:
        try:
            # 訂閱者來不及消費就丟新的（避免 SSE 卡死整個 poll thread）
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            q.put_nowait(snapshot)
        except Exception as e:
            logger.debug(f"notify subscriber failed: {e}")


def _collect_machine_state():
    """從 machine controller 與本地計算出機器相關欄位。不會打 SIP2。

    回傳的 dict 內含一個 '_raw_state_resp' 私有欄位（不寫進 _state_cache，
    僅給 poll thread 判斷『serial 是否空回應』用，watchdog counter 使用）。
    """
    out = {
        'machine_ok': False,
        'machine_state': 'unavailable',
        'homing_in_progress': False,
        'is_homed': False,
        'machine_debug': {'ser_open': False, 'type': None},
        '_raw_state_resp': None,
    }
    if machine is None:
        return out
    try:
        out['machine_debug']['type'] = type(machine).__name__
        ser = getattr(machine, 'ser', None)
        ser_open = bool(ser and getattr(ser, 'is_open', False))
        out['machine_ok'] = ser_open
        out['machine_debug']['ser_open'] = ser_open

        # 主動更新機器狀態（這裡會發 'state' 指令到 serial）
        last_resp = ''
        if hasattr(machine, 'get_status'):
            try:
                last_resp = machine.get_status()
            except Exception as e:
                logger.debug(f"poll get_status failed: {e}")
        out['_raw_state_resp'] = last_resp

        homing_flag = bool(getattr(machine, 'homing_in_progress', False))
        is_homed = bool(getattr(machine, 'is_homed', False))
        is_sleeping = bool(getattr(machine, 'is_sleeping', False))
        out['homing_in_progress'] = homing_flag
        out['is_homed'] = is_homed

        # 決定可讀狀態：規格 state 0-5 是單一事實來源，優先用 state digit 推；
        # 用旗標當 fallback（例如 state 查詢期間正在 homing）。
        # 舊版用 is_homed 旗標時把 state 2/3/4 都壓成 'homed'，UI 看不出投書口開關。
        state_name_by_code = {
            0: 'not_init',
            1: 'power_on_not_homed',
            2: 'homed',
            3: 'opened',
            4: 'closed',
            5: 'sleeping',
        }
        s_raw = str(last_resp).strip() if last_resp is not None else ''
        # 'busy' = _send_command 拿不到 lock；別把它變成 _raw_state_resp 餵給 watchdog，
        # 也別讓下面 fallback 把 machine_state 倒退到 'ready'。沿用上次 cache 的值。
        if s_raw == 'busy':
            out['_raw_state_resp'] = 'busy'
            with _state_cache_lock:
                out['machine_state'] = _state_cache.get('machine_state', 'ready')
            return out
        if homing_flag:
            out['machine_state'] = 'homing'
        elif s_raw.isdigit() and int(s_raw) in state_name_by_code:
            out['machine_state'] = state_name_by_code[int(s_raw)]
        elif is_sleeping:
            out['machine_state'] = 'sleeping'
        elif is_homed:
            out['machine_state'] = 'homed'
        else:
            out['machine_state'] = 'ready'
            try:
                low = s_raw.lower()
                if 'opened' in low:
                    out['machine_state'] = 'opened'
                elif 'closed' in low:
                    out['machine_state'] = 'closed'
                elif 'power on' in low:
                    out['machine_state'] = 'homed' if 'not homed' not in low else 'power_on_not_homed'
                elif 'homed' in low and 'not homed' not in low:
                    out['machine_state'] = 'homed'
            except Exception:
                pass
    except Exception as e:
        logger.error(f"_collect_machine_state error: {e}")
        out['machine_state'] = 'error'
    return out


def _collect_sip2_state():
    """檢查 SIP2 連線。打一次 99 health_check。"""
    if not sip2:
        return {'library_ok': False}
    try:
        return {'library_ok': bool(sip2.health_check())}
    except Exception:
        return {'library_ok': False}


# ─── Watchdog（自動恢復） ─────────────────────────────────────────────────────
#
# 觀察 _state_poll_thread 累積的兩個故障計數器，連續超過閾值且未在冷卻窗口
# 內，就觸發對應的恢復動作。
#
# 故障模式：
#   1. /dev/uno serial 連續回空字串 → 機器卡住，需要關閉/重開 serial + dep1 + homing
#   2. SIP2 health_check 連續失敗 → 圖書館伺服器連線掉，需要 reconnect + login
#
# 為避免抖動造成連續重啟，每種恢復動作各有自己獨立的 12 小時冷卻窗口；冷卻
# 期間若閾值持續被觸發，只記錄一次 WARNING，不再嘗試恢復。

_WATCHDOG_POLL_INTERVAL_SEC = 5.0
# poll thread 每 1s 一次 machine state，連續 3 次空回應 ≈ 3 秒
_WATCHDOG_MACHINE_EMPTY_THRESHOLD = 3
# poll thread 每 15s 一次 SIP2 health，連續 4 次失敗 ≈ 60 秒
_WATCHDOG_SIP2_FAIL_THRESHOLD = 4
_WATCHDOG_COOLDOWN_SEC = 12 * 3600  # 12 小時
# 啟動寬限期：machine init 期間 state 查詢自然會回空字串（init 自己持鎖在跑
# dep1+homing），此時不該被 watchdog 視為「卡住」並重複觸發。經實測 init 約
# 24 秒完成，保守給 60 秒寬限。
_WATCHDOG_STARTUP_GRACE_SEC = 60.0

_watchdog_lock = threading.Lock()
_consecutive_empty_state = 0
_consecutive_sip2_fail = 0
_watchdog_machine_cooldown_until = 0.0
_watchdog_sip2_cooldown_until = 0.0
# 記錄上次 log 過「冷卻中跳過」的閾值類型，避免每 5 秒重複噴 log
_watchdog_machine_cooldown_logged = False
_watchdog_sip2_cooldown_logged = False
# 程序啟動時間；watchdog 在啟動寬限期內不觸發 recovery
_watchdog_start_time = time.time()


def _update_machine_empty_counter(raw_resp):
    """每次 poll thread 取得 machine state 後呼叫。raw_resp 為 None 或空字串
    視為「空回應」，連續計數；任何非空回應重置為 0。

    例外：'busy' 是 _send_command 對 state 指令拿不到 lock 時的回傳值
    （代表正有別的動作 open/close/cancel/put 持鎖，硬體不一定卡），
    這次 tick 不該計入也不該重置；否則 12 秒 open 期間會累計 ~12 個 empty，
    動作一結束 watchdog 就會送 dep1+homing 把成果回滾。"""
    global _consecutive_empty_state
    if isinstance(raw_resp, str) and raw_resp.strip() == 'busy':
        return
    is_empty = raw_resp is None or (isinstance(raw_resp, str) and raw_resp.strip() == '')
    with _watchdog_lock:
        if is_empty:
            _consecutive_empty_state += 1
        else:
            _consecutive_empty_state = 0


def _update_sip2_fail_counter(library_ok):
    """每次 poll thread 跑 SIP2 health_check 後呼叫。library_ok=False 累加；
    True 重置為 0。單位是「15 秒間隔」，不是秒。"""
    global _consecutive_sip2_fail
    with _watchdog_lock:
        if library_ok:
            _consecutive_sip2_fail = 0
        else:
            _consecutive_sip2_fail += 1


def _watchdog_reset_counters():
    """測試 / 手動恢復後可呼叫，把計數器歸零。"""
    global _consecutive_empty_state, _consecutive_sip2_fail
    with _watchdog_lock:
        _consecutive_empty_state = 0
        _consecutive_sip2_fail = 0


def _watchdog_recover_machine():
    """關閉並重開 serial，重送 dep1 + homing。整段在 machine.lock 內執行。

    若 config.yml 把 watchdog.auto_recover_machine 設為 false，只 log 不執行。"""
    n = _consecutive_empty_state
    logger.warning(f"watchdog: machine stuck ({n} empty states), triggering reset")
    if not WATCHDOG_AUTO_RECOVER_MACHINE:
        logger.warning("watchdog: machine recovery (auto-recover disabled, skipping)")
        return
    if machine is None:
        logger.warning("watchdog: machine recovery skipped (machine controller not initialized)")
        return

    try:
        # machine.lock 是 RLock，安全可重入
        with machine.lock:
            # 1) 關閉現有 serial
            ser = getattr(machine, 'ser', None)
            try:
                if ser is not None and getattr(ser, 'is_open', False):
                    ser.close()
                    logger.warning("watchdog: machine recovery - serial closed")
            except Exception as e:
                logger.warning(f"watchdog: machine recovery - serial close failed: {e}")

            # 2) 重新開啟 serial
            try:
                if not getattr(machine, 'simulate', False):
                    machine.ser = serial.Serial(
                        port=machine.port,
                        baudrate=machine.baudrate,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=2,
                    )
                    logger.warning(
                        f"watchdog: machine recovery - serial reopened on {machine.port}"
                    )
            except Exception as e:
                logger.error(f"watchdog: machine recovery - serial reopen failed: {e}")
                return

            # 3) 送 dep1 (喚醒)
            try:
                machine._send_command("dep1")
                logger.warning("watchdog: machine recovery - dep1 sent")
            except Exception as e:
                logger.error(f"watchdog: machine recovery - dep1 failed: {e}")

            # 4) 送 homing
            try:
                machine._send_command("homing")
                logger.warning("watchdog: machine recovery - homing sent")
            except Exception as e:
                logger.error(f"watchdog: machine recovery - homing failed: {e}")
    except Exception as e:
        logger.error(f"watchdog: machine recovery unexpected error: {e}")
        return

    logger.warning("watchdog: machine recovery completed")


def _watchdog_recover_sip2():
    """嘗試取得 sip2 IO lock（5 秒 timeout），拿到後呼叫 init_sip2_client 重連。

    若拿不到 lock（表示有別的請求正在傳輸中），跳過這次觸發，下個 5 秒 tick
    若計數器仍超標會再試。"""
    n = _consecutive_sip2_fail
    logger.warning(f"watchdog: SIP2 disconnected ~{n*_SIP2_POLL_EVERY_N_TICKS}s, triggering reconnect")
    if not WATCHDOG_AUTO_RECOVER_SIP2:
        logger.warning("watchdog: SIP2 recovery (auto-recover disabled, skipping)")
        return

    io_lock = getattr(sip2, '_io_lock', None) if sip2 is not None else None
    if io_lock is None:
        logger.warning("watchdog: SIP2 recovery - no io_lock available, calling init_sip2_client directly")
        try:
            init_sip2_client()
            logger.warning("watchdog: SIP2 recovery completed")
        except Exception as e:
            logger.error(f"watchdog: SIP2 recovery failed: {e}")
        return

    acquired = io_lock.acquire(timeout=5)
    if not acquired:
        logger.warning("watchdog: SIP2 recovery - could not acquire io_lock in 5s, will retry on next tick")
        return
    try:
        try:
            init_sip2_client()
            logger.warning("watchdog: SIP2 recovery completed")
        except Exception as e:
            logger.error(f"watchdog: SIP2 recovery failed: {e}")
    finally:
        try:
            io_lock.release()
        except Exception:
            pass


def _watchdog_tick(now=None):
    """單次 watchdog 評估邏輯：檢查計數器、冷卻，必要時呼叫恢復函式。

    抽成獨立函式以便測試可直接同步呼叫（不必啟動 thread）。"""
    global _watchdog_machine_cooldown_until, _watchdog_sip2_cooldown_until
    global _watchdog_machine_cooldown_logged, _watchdog_sip2_cooldown_logged
    if now is None:
        now = time.time()

    # 啟動寬限期：pyserver 剛起來時 machine init 還在背景執行，state 查詢
    # 自然會回空字串幾秒；此時不該被 watchdog 視為「卡住」而重複觸發 dep1+homing
    # （init 本來就會做這件事）。實測 init 約 24 秒完成，保守給 60 秒。
    if now - _watchdog_start_time < _WATCHDOG_STARTUP_GRACE_SEC:
        return

    with _watchdog_lock:
        empty_state = _consecutive_empty_state
        sip2_fail = _consecutive_sip2_fail
        machine_cd_until = _watchdog_machine_cooldown_until
        sip2_cd_until = _watchdog_sip2_cooldown_until

    # === Machine ===
    if empty_state >= _WATCHDOG_MACHINE_EMPTY_THRESHOLD:
        if now < machine_cd_until:
            if not _watchdog_machine_cooldown_logged:
                hours_remaining = max(0.0, (machine_cd_until - now) / 3600.0)
                logger.warning(
                    f"watchdog: machine threshold breached but in cooldown "
                    f"({hours_remaining:.1f} hours remaining)"
                )
                _watchdog_machine_cooldown_logged = True
        else:
            try:
                _watchdog_recover_machine()
            finally:
                with _watchdog_lock:
                    _watchdog_machine_cooldown_until = now + _WATCHDOG_COOLDOWN_SEC
                _watchdog_machine_cooldown_logged = False
    else:
        # 閾值已未達標 → 允許下次冷卻過後再 log 一次
        _watchdog_machine_cooldown_logged = False

    # === SIP2 ===
    if sip2_fail >= _WATCHDOG_SIP2_FAIL_THRESHOLD:
        if now < sip2_cd_until:
            if not _watchdog_sip2_cooldown_logged:
                hours_remaining = max(0.0, (sip2_cd_until - now) / 3600.0)
                logger.warning(
                    f"watchdog: sip2 threshold breached but in cooldown "
                    f"({hours_remaining:.1f} hours remaining)"
                )
                _watchdog_sip2_cooldown_logged = True
        else:
            try:
                _watchdog_recover_sip2()
            finally:
                with _watchdog_lock:
                    _watchdog_sip2_cooldown_until = now + _WATCHDOG_COOLDOWN_SEC
                _watchdog_sip2_cooldown_logged = False
    else:
        _watchdog_sip2_cooldown_logged = False


def _watchdog_thread():
    """背景 thread：每 5 秒檢查一次計數器，必要時觸發恢復。"""
    while True:
        try:
            _watchdog_tick()
        except Exception as e:
            logger.error(f"_watchdog_thread error: {e}")
        time.sleep(_WATCHDOG_POLL_INTERVAL_SEC)


def _state_poll_thread():
    """背景 thread，每秒打 serial 拿狀態，每 15 秒打 SIP2 拿連線狀態。
    狀態變化時呼叫 _notify_subscribers 把新快照推給訂閱者。"""
    tick = 0
    while True:
        try:
            tick += 1
            machine_part = _collect_machine_state()
            # 抽出 watchdog 用的 raw response，並從寫入 cache 的字典裡移除
            raw_state_resp = machine_part.pop('_raw_state_resp', None)
            _update_machine_empty_counter(raw_state_resp)

            # SIP2 健康檢查節流：tick=1 立即跑一次，之後每 15 tick 一次
            sip2_polled = False
            if tick == 1 or tick % _SIP2_POLL_EVERY_N_TICKS == 0:
                sip2_part = _collect_sip2_state()
                sip2_polled = True
            else:
                with _state_cache_lock:
                    sip2_part = {'library_ok': _state_cache.get('library_ok', False)}

            if sip2_polled:
                _update_sip2_fail_counter(bool(sip2_part.get('library_ok')))

            # 本地計算欄位（檔案 / sqlite，便宜）
            try:
                bin_counts = get_bin_counts()
            except Exception:
                bin_counts = {}
            try:
                suspended = is_box_full()
            except Exception:
                suspended = False

            new_snapshot = {
                **machine_part,
                **sip2_part,
                'bin_counts': bin_counts,
                'suspended': suspended,
                'limit': MAX_RETURN_LIMIT,
                'last_updated': time.time(),
            }

            # 寫入 cache，判斷有沒有「有意義的」變化
            changed = False
            with _state_cache_lock:
                for f in _STATE_CHANGE_FIELDS:
                    if _state_cache.get(f) != new_snapshot.get(f):
                        changed = True
                        break
                _state_cache.update(new_snapshot)
                snapshot_copy = dict(_state_cache)

            if changed:
                _notify_subscribers(snapshot_copy)
        except Exception as e:
            logger.error(f"_state_poll_thread error: {e}")
        time.sleep(_STATE_POLL_INTERVAL_SEC)


def start_background_tasks():
    # 1. 初始化機器
    threading.Thread(target=init_machine_async, daemon=True).start()
    # 2. 閒置檢查
    threading.Thread(target=check_machine_idle, daemon=True).start()
    # 3. 狀態快取 polling（serial / SIP2 每秒同步進 cache，狀態變化推 SSE 訂閱者）
    threading.Thread(target=_state_poll_thread, daemon=True).start()
    # 4. Watchdog：每 5 秒檢查 poll thread 累積的計數器，連續故障時觸發恢復
    threading.Thread(target=_watchdog_thread, daemon=True).start()

# --- 主初始化入口 ---
def init_shared():
    load_config()
    load_locales()
    setup_logging()
    init_machine_controller()
    init_sip2_client()
    init_box_db()