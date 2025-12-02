import time
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from datetime import datetime, timedelta
import sqlite3
import os
import random
import logging
from logging.handlers import TimedRotatingFileHandler
import json
from pathlib import Path
import yaml
import sys

# 讀取 config.yml（若存在）
CONFIG_FILE = Path('config.yml')
cfg = {}
if CONFIG_FILE.exists():
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to load config.yml: {e}")

# 日誌保留天數（預設 30）
LOG_BACKUP_DAYS = int(cfg.get('logs', {}).get('backup_days', 30))

# 日誌：每日一檔，分四種類別並保留 LOG_BACKUP_DAYS（app / machine / library / frontend）
# 設定為相對於 app.py 的絕對路徑，避免因執行目錄不同導致 log 散落
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

LOG_BACKUP_DAYS = int(cfg.get('logs', {}).get('backup_days', 30))

def make_timed_handler(name):
    h = TimedRotatingFileHandler(LOG_DIR / f'{name}.log', when='midnight', backupCount=LOG_BACKUP_DAYS, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    return h

# General app log
app_handler = make_timed_handler('app')
app_logger = logging.getLogger('app')
app_logger.setLevel(logging.INFO)
app_logger.addHandler(app_handler)

# Machine-specific log
machine_handler = make_timed_handler('machine')
machine_logger = logging.getLogger('machine')
machine_logger.setLevel(logging.INFO)
machine_logger.addHandler(machine_handler)

# Library / SIP2 related log
library_handler = make_timed_handler('library')
library_logger = logging.getLogger('library')
library_logger.setLevel(logging.INFO)
library_logger.addHandler(library_handler)

# Frontend logs (collected from client via API)
frontend_handler = make_timed_handler('frontend')
frontend_logger = logging.getLogger('frontend')
frontend_logger.setLevel(logging.INFO)
frontend_logger.addHandler(frontend_handler)

# Ensure root also writes to general app log
logging.root.setLevel(logging.INFO)
logging.root.addHandler(app_handler)

# Also log to console/stdout for immediate visibility during development
console_h = logging.StreamHandler(sys.stdout)
console_h.setLevel(logging.DEBUG)
console_h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
logging.root.addHandler(console_h)

logger = logging.getLogger(__name__)

# 引入控制器
import threading
from machine_controller import MachineController
from sip2_client import SIP2Client, MockSIP2Client

# 設定 Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = cfg.get('secret_key', 'yuntech_library_secret_key')  # 設定密鑰（可由 config.yml 覆寫）
app.permanent_session_lifetime = timedelta(minutes=int(cfg.get('session_minutes', 5)))  # 設定 session 過期時間（分鐘）
# 從 config.yml 讀取主要設定
ADMIN_PASSWORD = cfg.get('admin_password', "admin")
BARCODE_LOGIN_ENABLED = cfg.get('barcode_login_enabled', True)
# 若設為 False，管理後台不需登入即可存取（暫時性需求）
REQUIRE_ADMIN_LOGIN = cfg.get('require_admin_login', False)
# 還書箱上限（可由管理介面變更）
MAX_RETURN_LIMIT = int(cfg.get('max_return_limit', 20))
# 是否啟用書籍檢測（可由管理介面變更）
BOOK_CHECK_ENABLED = cfg.get('book_check_enabled', True)

# 初始化機器：以 config.yml 的設定建立新的 MachineController 實例
machine_cfg = cfg.get('machine', {})
# 優先讀取 machine_config.json (使用者透過介面設定的)
MACHINE_CONFIG_FILE = Path('machine_config.json')
port = machine_cfg.get('port', '/dev/uno')
baudrate = machine_cfg.get('baudrate', 38400)

try:
    if MACHINE_CONFIG_FILE.exists():
        mc_cfg = json.loads(MACHINE_CONFIG_FILE.read_text(encoding='utf-8'))
        port = mc_cfg.get('port', port)
        baudrate = int(mc_cfg.get('baudrate', baudrate))
except Exception as e:
    logger.warning(f"Failed to read machine_config.json: {e}")

# 建立實例
machine = MachineController(port=port, baudrate=baudrate)

def init_machine_async():
    """背景初始化機器，避免阻塞 Server 啟動"""
    try:
        logger.info("Starting machine initialization in background...")
        machine.init_machine()
        logger.info("Machine initialization completed.")
    except Exception as e:
        logger.error(f"Machine initialization failed: {e}")

# Move initialization to background thread to avoid blocking server startup
init_thread = threading.Thread(target=init_machine_async, daemon=True)
init_thread.start()

# 初始化 SIP2 Client
# 從 config.py 讀取設定
import config as global_config

def init_sip2_client():
    """根據全域設定初始化 SIP2 Client"""
    global sip2
    # 這裡我們使用 config.py 內的變數
    # 注意：若未來希望完全不重啟 app 就能換 IP，需要讓 SIP2Client 支援動態重連，
    # 或者在這裡重新實例化。這裡採用「重新實例化」的方式。
    
    # 簡單判斷：如果 IP 是 127.0.0.1 或 localhost 且 port 是測試用，可能走 Mock?
    # 目前維持原本邏輯：預設 Mock，除非有明確設定要走真實連線。
    # 但使用者的需求是「動態加載」，所以我們應該預設使用真實 Client (或具備切換能力)
    # 這裡為了展示，我們先預設 "若 host 為 'mock' 則用 MockClient，否則用真 Client"
    
    host = global_config.LIBRARY_HOST
    port = global_config.LIBRARY_PORT
    
    # 若未啟用登入，傳空字串給 Client (Client 會改用無帳密 login 模式)
    if global_config.LIBRARY_LOGIN_ENABLED:
        user = global_config.LIBRARY_USER
        pwd = global_config.LIBRARY_PASS
    else:
        user = ""
        pwd = ""
        
    inst = global_config.LIBRARY_INSTITUTION
    
    mock_enabled = global_config.LIBRARY_MOCK_ENABLED
    
    logger.info(f"Initializing SIP2 Client: {host}:{port}, Inst={inst}, Login={global_config.LIBRARY_LOGIN_ENABLED}, Mock={mock_enabled}")
    
    if mock_enabled or host.lower() == 'mock':
        sip2 = MockSIP2Client(host, port, user, pwd, institution_id=inst)
    else:
        # 真實連線
        try:
             # 如果舊的有連線，先關閉
            if 'sip2' in globals() and sip2:
                try: sip2.close()
                except: pass
            
            sip2 = SIP2Client(host, port, user, pwd, institution_id=inst)
            # 嘗試連線 (非阻塞，失敗就之後由各個 API call 自己重連)
            # 但為了開機檢查，我們試著連一次
            if sip2.connect():
                sip2.login()
        except Exception as e:
            logger.error(f"Failed to init SIP2 client: {e}")
            # Fallback to a safe state or Mock if critical?
            # For now, we keep the object but it might be unconnected.
            pass

# 初次執行
sip2 = None
init_sip2_client()

# 本地還書箱資料庫 (只記錄還書箱內的物品)
BOX_DB_FILE = 'return_box.db'

def init_box_db():
    """初始化還書箱本地資料庫"""
    conn = sqlite3.connect(BOX_DB_FILE)
    c = conn.cursor()
    # 只需要記錄：ID, 書籍條碼, 書名, 圖片, 放入時間
    c.execute('''CREATE TABLE IF NOT EXISTS box_inventory
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  book_id TEXT,
                  title TEXT,
                  image_url TEXT,
                  return_time TEXT)''')
    conn.commit()
    conn.close()

init_box_db()

# 背景執行緒：檢查機器閒置狀態
def check_machine_idle():
    while True:
        try:
            machine.check_idle()
        except Exception as e:
            logger.error(f"Error checking machine idle: {e}")
        time.sleep(60) # 每分鐘檢查一次

idle_checker_thread = threading.Thread(target=check_machine_idle, daemon=True)
idle_checker_thread.start()

def get_box_db():
    conn = sqlite3.connect(BOX_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def is_box_full():
    # 優先使用實體感測器 (如果有)
    # 目前機器 API 沒有直接 "is_full" 指令，仍依賴資料庫計數
    # 實際應用可能需要結合 opbm1~4 感測器判斷
    conn = get_box_db()
    try:
        count = conn.execute('SELECT COUNT(*) FROM box_inventory').fetchone()[0]
    except:
        count = 0
    finally:
        conn.close()
    return count >= MAX_RETURN_LIMIT

@app.route('/')
def index():
    """渲染還書介面主頁"""
    if is_box_full():
        return render_template('return_book.html', service_suspended=True)
    return render_template('return_book.html', service_suspended=False)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """管理員登入"""
    if request.method == 'POST':
        data = request.json
        password = data.get('password')
        if password == ADMIN_PASSWORD:
            session.permanent = True
            session['logged_in'] = True
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "密碼錯誤"})
    return render_template('login.html', barcode_login_enabled=BARCODE_LOGIN_ENABLED)

@app.route('/logout')
def logout():
    """管理員登出"""
    session.pop('logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin')
def admin():
    """渲染管理員後台"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return redirect(url_for('login'))
    # Pass current config to template for pre-filling forms
    current_config = {
        'max_return_limit': MAX_RETURN_LIMIT
    }
    return render_template('admin.html', config=current_config)

@app.route('/api/status')
def get_status():
    """檢查系統狀態（包含圖書館與機器通訊），並回傳可讀的機器狀態字串與 homing 標記。"""
    suspended = is_box_full()
    limit = MAX_RETURN_LIMIT

    # 檢查圖書館通訊（以 get_book_info 做簡單測試）
    library_ok = False
    try:
        try:
            test_info = sip2.get_book_info('ping_test')
        except Exception:
            test_info = None
        library_ok = test_info is not None
    except Exception as e:
        logger.error(f"status check library error: {e}")
        library_ok = False

    # 檢查機器通訊
    machine_ok = False
    machine_debug = {"ser_open": False, "type": None}
    machine_state = "unknown"
    homing_in_progress = False
    is_homed = False

    try:
        machine_debug["type"] = type(machine).__name__ if machine is not None else None
        ser = getattr(machine, 'ser', None)
        ser_open = bool(ser and getattr(ser, 'is_open', False))
        machine_ok = ser_open
        machine_debug["ser_open"] = ser_open

        # 主動更新機器狀態：呼叫 get_status 以觸發硬體查詢並更新內部 flag
        # (避免因初始預設值 is_sleeping=True 導致誤判而未查詢硬體)
        last_resp = ""
        if machine and hasattr(machine, 'get_status'):
            try:
                last_resp = machine.get_status()
            except Exception as e:
                logger.debug(f"status probe get_status failed: {e}")

        # 讀取更新後的機器內部狀態標記
        try:
            homing_in_progress = bool(getattr(machine, 'homing_in_progress', False))
            is_homed = bool(getattr(machine, 'is_homed', False))
            is_sleeping = bool(getattr(machine, 'is_sleeping', False))
        except Exception:
            homing_in_progress = False
            is_homed = False
            is_sleeping = False

        # 決定可讀狀態優先順序
        if machine is None:
            machine_state = "unavailable"
        elif is_sleeping:
            machine_state = "sleeping"
        elif homing_in_progress:
            machine_state = "homing"
        elif is_homed:
            machine_state = "homed"
        else:
            # 若無明確 flag，嘗試根據剛剛的 resp 解析 (fallback)
            machine_state = "ready"
            try:
                low = str(last_resp).lower()
                if "open" in low or "opened" in low:
                    machine_state = "opened"
                elif "closed" in low:
                    machine_state = "closed"
                elif "power on" in low or "dep1" in low:
                    machine_state = "power_on_not_homed"
                elif "homed" in low or "ack" in low:
                    machine_state = "homed"
            except Exception:
                pass
    except Exception as e:
        logger.error(f"status check machine error: {e}")
        machine_ok = False
        machine_state = "error"

    return jsonify({
        "suspended": suspended,
        "limit": limit,
        "library_ok": library_ok,
        "machine_ok": machine_ok,
        "machine_state": machine_state,
        "homing_in_progress": homing_in_progress,
        "is_homed": is_homed,
        "machine_debug": machine_debug
    })

@app.route('/api/scan', methods=['POST'])
def scan_book():
    """掃描書籍 API"""
    if is_box_full():
        return jsonify({"success": False, "message": "還書箱已滿，暫停服務", "code": "SERVICE_SUSPENDED"}), 503

    data = request.json
    book_id = data.get('book_id')

    if not book_id:
        return jsonify({"success": False, "message": "請輸入書籍編號"}), 400

    # 1. 查詢 SIP2 圖書館系統
    book_info = sip2.get_book_info(book_id)

    if not book_info:
        return jsonify({"success": False, "message": f"找不到書籍編號: {book_id}"}), 404

    # 2. 檢查圖書館系統中的狀態 (SIP2 回傳的狀態判斷需視實際回應調整)
    # Mock SIP2 Client 會回傳 status='Checked Out' or 'Available'
    
    # 圖片暫時使用假圖，因為 SIP2 通常不包含圖片 URL
    image_url = f"https://picsum.photos/seed/{book_id}/100/150"

    # 3. 檢查是否逾期
    # SIP2 日期格式通常為 YYYYMMDD 或 YYYY-MM-DD (視實作而定)
    # 這裡我們嘗試解析並比對
    is_overdue = False
    due_date_str = book_info.get('due_date')
    if due_date_str:
        try:
            # 嘗試解析多種格式
            due_date = None
            for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
                try:
                    due_date = datetime.strptime(due_date_str.strip(), fmt)
                    break
                except ValueError:
                    continue
            
            if due_date:
                # 比較日期 (只比對日期部分)
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                if due_date < today:
                    is_overdue = True
        except Exception as e:
            logger.warning(f"Date parse error: {e}")

    if is_overdue:
        return jsonify({
            "success": False,
            "code": "OVERDUE",
            "message": f"書籍「{book_info['title']}」已逾期 ({due_date_str})，請至人工櫃台歸還。",
            "data": {
                 "title": book_info['title'],
                 "due_date": due_date_str,
                 "patron_name": book_info.get('patron_name', 'Unknown')
            }
        }), 400

    # 4. 回傳資訊供前端確認
    return jsonify({
        "success": True,
        "message": "掃描成功",
        "data": {
            "book_id": book_info['barcode'],
            "title": book_info['title'],
            "author": book_info['author'],
            "image_url": image_url,
            "due_date": due_date_str,
            "patron_name": book_info.get('patron_name'),
            "has_attachment": book_info.get('has_attachment', False)
        }
    })

@app.route('/api/return', methods=['POST'])
def return_book():
    """確認還書 API"""
    if is_box_full():
        return jsonify({"success": False, "message": "還書箱已滿，暫停服務", "code": "SERVICE_SUSPENDED"}), 503

    data = request.json or {}
    book_ids = data.get('book_ids', [])
    # 支援附件歸還標記 (attachment_only)
    attachment_only = data.get('attachment_only', False)
    # 可選：前端掃描的附件條碼，用於後端再次校對
    attachment_barcode = data.get('attachment_barcode')
    
    if 'book_id' in data:
        book_ids = [data['book_id']]
    
    if not book_ids:
        return jsonify({"success": False, "message": "未選擇任何書籍"}), 400

    # 若為附件歸還，強制為單本流程並要求提供附件條碼以供後端驗證
    if attachment_only:
        if len(book_ids) != 1:
            return jsonify({"success": False, "message": "附件歸還僅支援單本處理"}), 400
        if not attachment_barcode:
            return jsonify({"success": False, "message": "缺少附件條碼 (attachment_barcode)"}), 400
        # 簡單比對：附件條碼需與書籍條碼相符（大小寫不敏感）
        if attachment_barcode.strip().upper() != book_ids[0].strip().upper():
            return jsonify({"success": False, "message": "附件條碼與書籍不符，請重新掃描"}), 400

    returned_books = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 硬體還書流程控制
    # 1. 狀態檢查：若為狀態 3 (OPENED) 則直接執行；否則觸發 Homing
    try:
        status = machine.get_status()
        is_state_3 = False
        
        # 判斷是否為狀態 3 (數值或字串判斷)
        s_status = str(status).strip()
        if s_status.isdigit() and int(s_status) == getattr(machine, 'STATE_OPENED', 3):
            is_state_3 = True
        elif "opened" in s_status.lower() or "error state 3" in s_status.lower():
            is_state_3 = True
            
        if is_state_3:
            logger.info(f"Machine is in state {status} (OPENED). Proceeding to close directly.")
        else:
            logger.warning(f"Machine state is '{status}' (Not OPENED). Triggering HOMING as safety fallback.")
            # 非狀態 3，執行 Homing
            resp = machine._send_command("homing")
            logger.info(f"Fallback homing response: {resp}")
            
    except Exception as e:
        logger.error(f"Pre-close status check failed: {e}. Defaulting to HOMING.")
        try:
             machine._send_command("homing")
        except:
             pass

    # 2. 關門前預檢 (Pre-check)
    # 若啟用檢測且在關門前就偵測不到書籍，直接提示使用者，省去「關門->重開」的時間
    if BOOK_CHECK_ENABLED:
        logger.info("Pre-check: checking book status before closing door...")
        if not machine.check_book_status():
            logger.info("Pre-check failed: Book not detected. Aborting close.")
            return jsonify({
                "success": False,
                "message": "未偵測到書籍，請確認書籍已放入並靠右對齊",
                "code": "BOOK_NOT_DETECTED"
            }), 400
    else:
        logger.info("Book check disabled by config. Skipping pre-check.")

    # 3. 關閉投書口（前端應已開門並使用者放入）
    machine.close_door()
    time.sleep(1) # 等待門關閉

    # 4. 關門後複檢 (Post-check)
    if BOOK_CHECK_ENABLED:
        if not machine.check_book_status():
            # 如果關門後書沒放好（可能滑落或預檢誤判），先回傳錯誤給前端，再背景執行重開門
            logger.info("Post-check failed: Book not detected in return box, triggering async reopen.")
            
            def async_reopen():
                try:
                    # 稍微延遲一點點，讓前端有時間先處理 UI，避免瞬間搶佔資源（可選）
                    time.sleep(0.1)
                    machine.reopen_door()
                except Exception as e:
                    logger.error(f"Async reopen failed: {e}")

            threading.Thread(target=async_reopen, daemon=True).start()

            return jsonify({
                "success": False,
                "message": "未偵測到書籍，請確認書籍已放入並靠右對齊",
                "code": "BOOK_NOT_DETECTED"
            }), 400
    else:
        logger.info("Book check disabled by config. Skipping post-check.")
            try:
                # 稍微延遲一點點，讓前端有時間先處理 UI，避免瞬間搶佔資源（可選）
                time.sleep(0.1)
                machine.reopen_door()
            except Exception as e:
                logger.error(f"Async reopen failed: {e}")

        threading.Thread(target=async_reopen, daemon=True).start()

        return jsonify({
            "success": False,
            "message": "未偵測到書籍，請確認書籍已放入並靠右對齊",
            "code": "BOOK_NOT_DETECTED"
        }), 400

    # 開始處理每一本書 (目前邏輯假設一次一本或批次一起處理)
    # 若是批次，硬體上可能是一次處理一本，這裡簡化為全部成功才算
    
    conn = get_box_db()
    cursor = conn.cursor()

    try:
        for b_id in book_ids:
            # SIP2 Checkin
            # 若僅是歸還附件，不需再次 SIP2 Checkin（或視系統需求而定）
            # 假設附件歸還也需要 Checkin 或僅記錄
            checkin_success = True
            if not attachment_only:
                 checkin_success = sip2.checkin_book(b_id)

            if checkin_success:
                # 取得書籍資訊 (為了記錄)
                book_info = sip2.get_book_info(b_id)
                title = book_info['title'] if book_info else 'Unknown'
                if attachment_only:
                    title += " (附件)"
                
                image_url = f"https://picsum.photos/seed/{b_id}/100/150"

                # 寫入本地資料庫
                cursor.execute('INSERT INTO box_inventory (book_id, title, image_url, return_time) VALUES (?, ?, ?, ?)',
                               (b_id, title, image_url, current_time))
                
                returned_books.append({
                    "book_id": b_id,
                    "title": title,
                    "return_time": current_time
                })
        
        conn.commit()
        
        # 硬體分類 (預設放入箱子 1)
        machine.sort_book(1)
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Return error: {e}")
        return jsonify({"success": False, "message": "系統錯誤"}), 500
    finally:
        conn.close()

    return jsonify({
        "success": True,
        "message": f"成功歸還 {len(returned_books)} 本書籍",
        "data": returned_books
    })

@app.route('/api/admin/logs', methods=['GET'])
def get_logs():
    """獲取還書箱內的書籍清單（box_inventory）與今日統計數據"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_box_db()
    data = {"logs": [], "history_logs": [], "today_total": 0}
    
    try:
        # 1. 獲取還書箱內的書籍（未清空前）
        logs = conn.execute('SELECT * FROM box_inventory ORDER BY id DESC').fetchall()
        data["logs"] = [dict(row) for row in logs]

        # 1.5 獲取歷史紀錄 (若表存在)
        table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='box_history'").fetchone()
        if table_check:
             history = conn.execute('SELECT * FROM box_history ORDER BY id DESC LIMIT 50').fetchall() # 限制 50 筆避免過多
             data["history_logs"] = [dict(row) for row in history]
        
        # 2. 獲取今日還書總數（包含 box_inventory 和 box_history 中今日的紀錄）
        # 取得 box_inventory 中今日的數量
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # 檢查 box_history 表是否存在
        table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='box_history'").fetchone()
        
        count_inventory = conn.execute("SELECT COUNT(*) FROM box_inventory WHERE return_time LIKE ?", (f"{today_str}%",)).fetchone()[0]
        
        count_history = 0
        if table_check:
             count_history = conn.execute("SELECT COUNT(*) FROM box_history WHERE return_time LIKE ?", (f"{today_str}%",)).fetchone()[0]
             
        data["today_total"] = count_inventory + count_history
        
    except Exception as e:
        logger.error(f"get_logs error: {e}")
    finally:
        conn.close()
    return jsonify(data)

@app.route('/api/admin/clear_logs', methods=['POST'])
def clear_logs():
    """(向後相容) 清空還書箱 — 保留原有路由，行為為移至歷史紀錄並清空箱內計數"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    conn = get_box_db()
    try:
        # 建立 box_history 如果不存在
        conn.execute('''CREATE TABLE IF NOT EXISTS box_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      book_id TEXT,
                      title TEXT,
                      image_url TEXT,
                      return_time TEXT,
                      clear_time TEXT)''')
                      
        # 將 box_inventory 資料搬移到 box_history
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute('''INSERT INTO box_history (book_id, title, image_url, return_time, clear_time)
                        SELECT book_id, title, image_url, return_time, ? FROM box_inventory''', (current_time,))
        
        # 清空 box_inventory
        conn.execute('DELETE FROM box_inventory')
        conn.commit()
    except Exception as e:
        print(f"Clear logs error: {e}")
        return jsonify({"success": False, "message": "清空失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "還書箱已清空，服務恢復"})


@app.route('/api/admin/clear_box', methods=['POST'])
def clear_box():
    """清空還書箱內容物 (管理員取走實體書籍) - 僅重置箱內書本計數，不刪除歷史紀錄"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    conn = get_box_db()
    try:
        # 修改：不再刪除 box_inventory，而是應該有一個欄位或標記表示已取出
        # 但依目前簡單設計 box_inventory 即代表箱內物品。
        # 為了滿足「不刪除紀錄」但又要「清空計數 0/20」，我們需要將這些記錄標記為 'archived' 或移至歷史表
        
        # 方案 A: 增加 status 欄位 (需要修改 schema，比較麻煩)
        # 方案 B: 將資料搬移到 box_history 表 (如果沒有就建一個)，然後清空 box_inventory
        
        # 1. 建立 box_history 如果不存在
        conn.execute('''CREATE TABLE IF NOT EXISTS box_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      book_id TEXT,
                      title TEXT,
                      image_url TEXT,
                      return_time TEXT,
                      clear_time TEXT)''')
                      
        # 2. 將 box_inventory 資料搬移到 box_history
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute('''INSERT INTO box_history (book_id, title, image_url, return_time, clear_time)
                        SELECT book_id, title, image_url, return_time, ? FROM box_inventory''', (current_time,))
        
        # 3. 清空 box_inventory (這樣計數就會變回 0)
        conn.execute('DELETE FROM box_inventory')
        
        conn.commit()
    except Exception as e:
        print(f"Clear box error: {e}")
        return jsonify({"success": False, "message": "清空失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "還書箱內容物已清空，服務恢復"})


@app.route('/api/admin/clear_history', methods=['POST'])
def clear_history():
    """清空歷史還書紀錄（僅清除歷史紀錄，不影響箱內目前的記錄）"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    conn = get_box_db()
    try:
        # 確保 box_history 存在（如不存在則建立）
        conn.execute('''CREATE TABLE IF NOT EXISTS box_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      book_id TEXT,
                      title TEXT,
                      image_url TEXT,
                      return_time TEXT,
                      clear_time TEXT)''')

        # 清空歷史紀錄（不刪除 box_inventory，保留箱內資料）
        conn.execute('DELETE FROM box_history')
        conn.commit()
    except Exception as e:
        print(f"Clear history error: {e}")
        return jsonify({"success": False, "message": "清空失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "歷史紀錄已清空"})


@app.route('/api/admin/get_machine_params', methods=['GET'])
def get_machine_params():
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401
    return jsonify({
        "success": True,
        "limit": MAX_RETURN_LIMIT,
        "book_check_enabled": BOOK_CHECK_ENABLED
    })

@app.route('/api/admin/set_machine_params', methods=['POST'])
def set_machine_params():
    """設定還書箱參數（上限、是否檢查書籍...）"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    data = request.json or {}
    try:
        new_limit = int(data.get('limit'))
        new_check = bool(data.get('book_check_enabled'))
    except Exception:
        return jsonify({"success": False, "message": "參數錯誤"}), 400

    if new_limit < 1 or new_limit > 100:
        return jsonify({"success": False, "message": "限制值必須介於 1 到 100"}), 400

    try:
        # 1. 讀取現有 config.yml
        current_conf = {}
        if CONFIG_FILE.exists():
            current_conf = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
        
        # 2. 更新 config
        current_conf['max_return_limit'] = new_limit
        current_conf['book_check_enabled'] = new_check
        
        # 3. 寫回 config.yml
        CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')
        
        # 4. Reload config
        global_config.reload_config()
        global MAX_RETURN_LIMIT
        global BOOK_CHECK_ENABLED
        MAX_RETURN_LIMIT = new_limit
        BOOK_CHECK_ENABLED = new_check
        
        return jsonify({
            "success": True,
            "message": f"參數已更新 (上限: {MAX_RETURN_LIMIT}, 書籍檢測: {BOOK_CHECK_ENABLED})",
            "limit": MAX_RETURN_LIMIT,
            "book_check_enabled": BOOK_CHECK_ENABLED
        })
        
    except Exception as e:
        logger.error(f"set_machine_params error: {e}")
        return jsonify({"success": False, "message": "設定儲存失敗"}), 500

@app.route('/api/hardware/open', methods=['POST'])
def open_hardware_door():
    """單獨開啟投書口 API (供前端流程控制)"""
    if machine.open_door():
        return jsonify({"success": True, "message": "投書口已開啟"})
    return jsonify({"success": False, "message": "開啟失敗"}), 500

@app.route('/api/hardware/close', methods=['POST'])
def close_hardware_door():
    """單獨關閉投書口 API (供前端流程控制)"""
    if machine.close_door():
        return jsonify({"success": True, "message": "投書口已關閉"})
    return jsonify({"success": False, "message": "關閉失敗"}), 500

@app.route('/api/hardware/wake', methods=['POST'])
def wake_hardware():
    """喚醒機器並使其進入待命狀態（供前端點擊喚醒）"""
    try:
        # 呼叫 machine 的 wake_up 方法（若存在）
        if hasattr(machine, 'wake_up'):
            machine.wake_up()
            return jsonify({"success": True, "message": "已向機器發出喚醒指令"})
        else:
            return jsonify({"success": False, "message": "機器不支援喚醒操作"}), 501
    except Exception as e:
        logger.error(f"wake_hardware error: {e}")
        return jsonify({"success": False, "message": "喚醒失敗"}), 500

@app.route('/api/hardware/wake_and_home', methods=['POST'])
def wake_and_home():
    """初始化/回原點操作：僅在必要時或使用者手動要求時執行 homing。
    
    依據新邏輯：
    - 若此路由被觸發，代表前端認為需要「重置」或「回原點」。
    - 這裡會發送 homing 指令，作為手動初始化的手段。
    - 喚醒動作 (wake_up) 只負責 dep1，這裡則負責 homing。
    """
    try:
        # 1. 確保喚醒
        if hasattr(machine, 'wake_up'):
            machine.wake_up()
        else:
            machine._send_command("dep1")

        # 2. 發送 homing 指令 (手動初始化)
        try:
            resp = machine._send_command("homing")
        except Exception as e:
            logger.warning(f"wake_and_home: homing command failed: {e}")
            return jsonify({"success": False, "message": "homing 指令發送失敗", "detail": str(e)}), 500

        ok = False
        try:
            ok = ("homed" in str(resp).lower()) or ("ack" in str(resp).lower())
        except Exception:
            ok = False
        
        # 更新內部狀態
        if hasattr(machine, 'is_homed'):
            machine.is_homed = ok

        if ok:
            return jsonify({"success": True, "message": "已執行回原點 (初始化)", "response": resp})
        else:
            # 有些機器 homing 回傳可能不包含 homed 字樣，若沒報錯也視為執行完畢
            logger.warning(f"wake_and_home: homing response inconclusive, resp={resp}")
            return jsonify({"success": True, "message": "回原點指令已發送", "response": resp})

    except Exception as e:
        logger.error(f"wake_and_home error: {e}")
        return jsonify({"success": False, "message": "操作失敗", "detail": str(e)}), 500

# 移除 get_books 因為改用 SIP2

# Removed inline app.run here so all routes defined before server start.
# The server will be started at the bottom of the file (after all routes) to avoid 404 for routes added later.
@app.route('/api/admin/send_command', methods=['POST'])
def admin_send_command():
    """管理員發送單一機器指令供驗證（僅供測試，需管理員登入）"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    data = request.json or {}
    cmd = data.get('cmd')
    if not cmd:
        return jsonify({"success": False, "message": "參數缺失：cmd"}), 400

    try:
        # 直接呼叫 machine._send_command 以進行單指令驗證（受管理員權限保護）
        resp = machine._send_command(cmd)
        return jsonify({"success": True, "cmd": cmd, "response": resp})
    except Exception as e:
        logger.error(f"admin_send_command error: {e}")
        return jsonify({"success": False, "message": f"執行失敗: {e}"}), 500

# Admin UART APIs: 取得與設定機器 UART 參數
@app.route('/api/admin/get_uart', methods=['GET'])
def get_uart():
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401
    try:
        from machine_controller import MACHINE_CONFIG_FILE
        # 優先讀取設定檔，若不存在則回傳當前 machine 參數
        if MACHINE_CONFIG_FILE.exists():
            cfg = json.loads(MACHINE_CONFIG_FILE.read_text(encoding='utf-8'))
            return jsonify({"success": True, "uart": cfg})
        return jsonify({"success": True, "uart": {"port": machine.port, "baudrate": machine.baudrate}})
    except Exception as e:
        logger.error(f"get_uart error: {e}")
        return jsonify({"success": False, "message": "讀取設定失敗"}), 500


@app.route('/api/admin/test_uart', methods=['POST'])
def test_uart():
    """測試機器 UART 連線 (不儲存)"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401
    
    data = request.json or {}
    port = data.get('port')
    baud = data.get('baudrate')
    
    if not port or baud is None:
        return jsonify({"success": False, "message": "參數缺失"}), 400
        
    try:
        baud = int(baud)
        success, msg = machine.test_connection(port, baud)
        if success:
             return jsonify({"success": True, "message": f"測試連線成功 ({port}, {baud})"})
        else:
             return jsonify({"success": False, "message": f"測試連線失敗: {msg}"})
    except Exception as e:
        logger.error(f"test_uart error: {e}")
        return jsonify({"success": False, "message": f"測試例外錯誤: {e}"}), 500

@app.route('/api/admin/set_uart', methods=['POST'])
def set_uart():
    """設定機器 UART（會寫入 machine_config.json 與 config.yml 並嘗試重新連線）"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    data = request.json or {}
    port = data.get('port')
    baud = data.get('baudrate')

    if not port or baud is None:
        return jsonify({"success": False, "message": "參數缺失：port 與 baudrate 必須提供"}), 400

    try:
        baud = int(baud)
    except Exception:
        return jsonify({"success": False, "message": "baudrate 必須為整數"}), 400

    try:
        uart_cfg = {"port": port, "baudrate": baud}
        
        # 1. 寫入 machine_config.json (保持向後相容)
        Path('machine_config.json').write_text(json.dumps(uart_cfg), encoding='utf-8')
        
        # 2. 同步寫入 config.yml
        current_conf = {}
        if CONFIG_FILE.exists():
            current_conf = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
        current_conf['machine'] = uart_cfg
        CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')
        
        # 3. Reload config
        global_config.reload_config()

        # 4. 嘗試立即套用到 machine 物件
        applied = False
        try:
            applied = machine.set_uart(port, baud)
        except Exception as e:
            logger.error(f"apply UART failed: {e}")
            
        logger.info(f"Admin action: set_uart -> {uart_cfg}, applied={applied}")
        return jsonify({"success": True, "applied": applied, "uart": uart_cfg})
    except Exception as e:
        logger.error(f"set_uart error: {e}")
        return jsonify({"success": False, "message": "設定失敗"}), 500

@app.route('/api/admin/get_library_config', methods=['GET'])
def get_library_config():
    """取得圖書館連線設定"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401
    
    # 重新讀取 config 以確保最新
    global_config.reload_config()
    
    return jsonify({
        "success": True,
        "config": {
            "mock_enabled": global_config.LIBRARY_MOCK_ENABLED,
            "host": global_config.LIBRARY_HOST,
            "port": global_config.LIBRARY_PORT,
            "login_enabled": global_config.LIBRARY_LOGIN_ENABLED,
            "login_user": global_config.LIBRARY_USER,
            # 密碼不回傳明碼，或回傳遮罩
            "login_pass": "********" if global_config.LIBRARY_PASS else "",
            "institution_id": global_config.LIBRARY_INSTITUTION
        }
    })

@app.route('/api/admin/set_library_config', methods=['POST'])
def set_library_config():
    """設定圖書館連線設定 (寫入 config.yml 並重載)"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401

    data = request.json or {}
    mock_enabled = data.get('mock_enabled', False)
    host = data.get('host')
    port = data.get('port')
    login_enabled = data.get('login_enabled', False)
    user = data.get('login_user')
    pwd = data.get('login_pass')
    inst = data.get('institution_id')

    if not host or not port:
         return jsonify({"success": False, "message": "Host 與 Port 為必填"}), 400

    if not inst:
         return jsonify({"success": False, "message": "機構代碼 (AO) 為必填"}), 400

    try:
        # 1. 讀取現有 config.yml
        current_conf = {}
        if CONFIG_FILE.exists():
            current_conf = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
        
        # 2. 更新 library 區塊
        lib_conf = current_conf.get('library', {})
        lib_conf['mock_enabled'] = bool(mock_enabled)
        lib_conf['host'] = host
        lib_conf['port'] = int(port)
        lib_conf['login_enabled'] = bool(login_enabled)
        lib_conf['login_user'] = user or ""
        # 如果密碼欄位是遮罩 (********) 且沒變更，就不更新密碼
        if pwd and pwd != "********":
            lib_conf['login_pass'] = pwd
        elif pwd == "": # 如果使用者刻意清空
             lib_conf['login_pass'] = ""
             
        lib_conf['institution_id'] = inst or "MAIN"
        
        current_conf['library'] = lib_conf
        
        # 3. 寫回 config.yml
        CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')
        
        # 4. Reload config in memory
        global_config.reload_config()
        
        # 5. Re-init SIP2 Client
        init_sip2_client()
        
        return jsonify({"success": True, "message": "圖書館設定已更新並嘗試重新連線"})
        
    except Exception as e:
        logger.error(f"set_library_config error: {e}")
        return jsonify({"success": False, "message": f"設定失敗: {e}"}), 500

@app.route('/api/admin/test_library_conn', methods=['POST'])
def test_library_conn():
    """測試圖書館連線 (使用當前送來的設定，不儲存)"""
    if REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"success": False, "message": "未登入"}), 401
        
    data = request.json or {}
    mock_enabled = data.get('mock_enabled', False)
    host = data.get('host')
    port = data.get('port')
    login_enabled = data.get('login_enabled', False)
    user = data.get('login_user')
    pwd = data.get('login_pass')
    inst = data.get('institution_id')
    
    # 決定是否使用帳密
    if not login_enabled:
        user = ""
        pwd = ""
    else:
        # 處理遮罩密碼：如果密碼是 ********，則使用目前的設定值
        if pwd == "********":
            pwd = global_config.LIBRARY_PASS

    if not host or not port:
         return jsonify({"success": False, "message": "Host 與 Port 為必填"}), 400
         
    try:
        if mock_enabled or host.lower() == 'mock':
             return jsonify({"success": True, "message": "Mock 連線測試成功 (強制 Mock 模式)"})
             
        test_client = SIP2Client(host, int(port), user, pwd, institution_id=inst)
        if test_client.connect():
            login_ok = test_client.login()
            test_client.close()
            
            if login_ok:
                return jsonify({"success": True, "message": f"連線與登入成功 ({host}:{port})"})
            else:
                return jsonify({"success": False, "message": f"連線成功但登入失敗 (Check User/Pass/AO)"})
        else:
             return jsonify({"success": False, "message": f"無法建立連線 ({host}:{port})"})
             
    except Exception as e:
        return jsonify({"success": False, "message": f"測試例外: {e}"}), 500

if __name__ == '__main__':
    # Start without the reloader to avoid duplicate processes and ensure console logs appear in this process.
    app.run(debug=False, use_reloader=False, port=5000)
