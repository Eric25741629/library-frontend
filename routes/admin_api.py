from flask import Blueprint, request, jsonify, session
from datetime import datetime
import json
import yaml
import logging
import os
import subprocess
import time
from pathlib import Path

import shared
import config as global_config
from machine_controller import is_completion, is_busy_response, is_error_state_response

admin_api_bp = Blueprint('admin_api', __name__)
logger = logging.getLogger('admin_api')

# 上傳圖片儲存目錄（位於 static/uploads）
STATIC_DIR = Path(__file__).resolve().parents[1] / 'static'
UPLOAD_DIR = STATIC_DIR / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)

@admin_api_bp.before_request
def check_login():
    if shared.REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return jsonify({"error": "Unauthorized", "success": False, "message": "未登入"}), 401

@admin_api_bp.route('/logs', methods=['GET'])
def get_logs():
    """獲取還書箱內的書籍清單（box_inventory）與今日統計數據"""
    conn = shared.get_box_db()
    data = {"logs": [], "history_logs": [], "today_total": 0, "bin_counts": shared.get_bin_counts()}
    
    try:
        logs = conn.execute('SELECT * FROM box_inventory ORDER BY id DESC').fetchall()
        data["logs"] = [dict(row) for row in logs]

        table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='box_history'").fetchone()
        if table_check:
             history = conn.execute('SELECT * FROM box_history ORDER BY id DESC LIMIT 50').fetchall()
             data["history_logs"] = [dict(row) for row in history]
        
        today_str = datetime.now().strftime("%Y-%m-%d")
        
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

@admin_api_bp.route('/clear_logs', methods=['POST'])
def clear_logs():
    """(向後相容) 清空還書箱"""
    conn = shared.get_box_db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS box_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      book_id TEXT,
                      title TEXT,
                      image_url TEXT,
                      return_time TEXT,
                      clear_time TEXT,
                      target_bin INTEGER)''')
                      
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute('''INSERT INTO box_history (book_id, title, image_url, return_time, clear_time, target_bin, reservation_notice, reservation_acknowledged_at)
                SELECT book_id, title, image_url, return_time, ?, target_bin, reservation_notice, reservation_acknowledged_at FROM box_inventory''', (current_time,))
        
        conn.execute('DELETE FROM box_inventory')
        conn.commit()
    except Exception as e:
        logger.error(f"Clear logs error: {e}")
        return jsonify({"success": False, "message": "清空失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "還書箱已清空，服務恢復"})

@admin_api_bp.route('/clear_box', methods=['POST'])
def clear_box():
    """清空還書箱內容物"""
    conn = shared.get_box_db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS box_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      book_id TEXT,
                      title TEXT,
                      image_url TEXT,
                      return_time TEXT,
                      clear_time TEXT,
                      target_bin INTEGER)''')
                      
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute('''INSERT INTO box_history (book_id, title, image_url, return_time, clear_time, target_bin, reservation_notice, reservation_acknowledged_at)
                SELECT book_id, title, image_url, return_time, ?, target_bin, reservation_notice, reservation_acknowledged_at FROM box_inventory''', (current_time,))
        
        conn.execute('DELETE FROM box_inventory')
        conn.commit()
    except Exception as e:
        logger.error(f"Clear box error: {e}")
        return jsonify({"success": False, "message": "清空失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "還書箱內容物已清空，服務恢復"})

@admin_api_bp.route('/clear_history', methods=['POST'])
def clear_history():
    """清空歷史還書紀錄"""
    conn = shared.get_box_db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS box_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      book_id TEXT,
                      title TEXT,
                      image_url TEXT,
                      return_time TEXT,
                      clear_time TEXT,
                      target_bin INTEGER)''')
        conn.execute('DELETE FROM box_history')
        conn.commit()
    except Exception as e:
        logger.error(f"Clear history error: {e}")
        return jsonify({"success": False, "message": "清空失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "歷史紀錄已清空"})


@admin_api_bp.route('/acknowledge_reservation', methods=['POST'])
def acknowledge_reservation():
    """標記指定書籍的預約通知已被管理員看過。
    後續 fetchLogs 仍會帶回 reservation_notice（badge 仍顯示），但 modal 不再彈。
    傳入 {"book_ids": ["C295901", ...]}；空陣列代表 ack 目前所有預約書。"""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('book_ids')
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = shared.get_box_db()
    try:
        if isinstance(raw_ids, list) and raw_ids:
            placeholders = ','.join('?' for _ in raw_ids)
            sql = (
                f"UPDATE box_inventory SET reservation_acknowledged_at = ? "
                f"WHERE reservation_notice IS NOT NULL "
                f"AND reservation_acknowledged_at IS NULL "
                f"AND book_id IN ({placeholders})"
            )
            conn.execute(sql, [now, *raw_ids])
        else:
            conn.execute(
                "UPDATE box_inventory SET reservation_acknowledged_at = ? "
                "WHERE reservation_notice IS NOT NULL "
                "AND reservation_acknowledged_at IS NULL",
                (now,),
            )
        conn.commit()
        acked = conn.execute(
            "SELECT COUNT(*) FROM box_inventory "
            "WHERE reservation_acknowledged_at = ?",
            (now,),
        ).fetchone()[0]
    except Exception as e:
        logger.error(f"acknowledge_reservation error: {e}")
        return jsonify({"success": False, "message": "更新失敗"}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "acknowledged": acked})


@admin_api_bp.route('/stop_frontend', methods=['POST'])
def stop_frontend():
    """允許管理員關閉前端 kiosk 瀏覽器服務。"""
    # 人工關閉前端時，持續抑制「非全螢幕 -> 自動重啟」機制。
    shared.mark_manual_frontend_stop()

    uid = os.getuid()
    env = os.environ.copy()
    xdg_runtime = env.get('XDG_RUNTIME_DIR') or f'/run/user/{uid}'
    env['XDG_RUNTIME_DIR'] = xdg_runtime
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={xdg_runtime}/bus')

    try:
        result = subprocess.run(
            ['systemctl', '--user', 'stop', 'kiosk-browser.service'],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    except Exception as e:
        logger.error(f"stop_frontend exec error: {e}")
        return jsonify({"success": False, "message": f"關閉前端失敗: {e}"}), 500

    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip()
        logger.error(f"stop_frontend failed: {err}")
        return jsonify({"success": False, "message": f"關閉前端失敗: {err}"}), 500

    logger.info("Admin action: kiosk-browser.service stopped")
    return jsonify({"success": True, "message": "前端介面已關閉"})

@admin_api_bp.route('/get_machine_params', methods=['GET'])
def get_machine_params():
    return jsonify({
        "success": True,
        "limit": shared.MAX_RETURN_LIMIT,
        "book_check_enabled": shared.BOOK_CHECK_ENABLED
    })

@admin_api_bp.route('/get_admin_auth', methods=['GET'])
def get_admin_auth():
    return jsonify({
        "success": True,
        "require_admin_login": bool(getattr(shared, 'REQUIRE_ADMIN_LOGIN', False)),
        "admin_username": getattr(shared, 'ADMIN_USERNAME', 'admin')
    })

@admin_api_bp.route('/set_admin_auth', methods=['POST'])
def set_admin_auth():
    """設定後台登入保護（是否啟用、帳號、密碼）"""
    data = request.json or {}
    require_login = bool(data.get('require_admin_login', False))
    username = (data.get('admin_username') or '').strip()
    password = data.get('admin_password')

    if not username:
        return jsonify({"success": False, "message": "管理員帳號不可為空"}), 400

    # 密碼可留空，表示不變更
    update_password = password is not None and str(password) != ''

    try:
        current_conf = {}
        if shared.CONFIG_FILE.exists():
            current_conf = yaml.safe_load(shared.CONFIG_FILE.read_text(encoding='utf-8')) or {}

        current_conf['require_admin_login'] = require_login
        current_conf['admin_username'] = username
        if update_password:
            current_conf['admin_password'] = str(password)

        shared.CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')

        global_config.reload_config()
        shared.REQUIRE_ADMIN_LOGIN = require_login
        shared.ADMIN_USERNAME = username
        if update_password:
            shared.ADMIN_PASSWORD = str(password)

        return jsonify({
            "success": True,
            "message": "後台登入設定已更新",
            "require_admin_login": shared.REQUIRE_ADMIN_LOGIN,
            "admin_username": shared.ADMIN_USERNAME
        })
    except Exception as e:
        logger.error(f"set_admin_auth error: {e}")
        return jsonify({"success": False, "message": "儲存後台登入設定失敗"}), 500

@admin_api_bp.route('/set_machine_params', methods=['POST'])
def set_machine_params():
    """設定還書箱參數"""
    data = request.json or {}
    try:
        new_limit = int(data.get('limit'))
        new_check = bool(data.get('book_check_enabled'))
    except Exception:
        return jsonify({"success": False, "message": "參數錯誤"}), 400

    if new_limit < 1 or new_limit > 100:
        return jsonify({"success": False, "message": "限制值必須介於 1 到 100"}), 400

    try:
        current_conf = {}
        if shared.CONFIG_FILE.exists():
            current_conf = yaml.safe_load(shared.CONFIG_FILE.read_text(encoding='utf-8')) or {}
        
        current_conf['max_return_limit'] = new_limit
        current_conf['book_check_enabled'] = new_check
        
        shared.CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')
        
        # Reload global config
        global_config.reload_config()
        shared.MAX_RETURN_LIMIT = new_limit
        shared.BOOK_CHECK_ENABLED = new_check
        
        return jsonify({
            "success": True,
            "message": f"參數已更新 (上限: {shared.MAX_RETURN_LIMIT}, 書籍檢測: {shared.BOOK_CHECK_ENABLED})",
            "limit": shared.MAX_RETURN_LIMIT,
            "book_check_enabled": shared.BOOK_CHECK_ENABLED
        })
        
    except Exception as e:
        logger.error(f"set_machine_params error: {e}")
        return jsonify({"success": False, "message": "設定儲存失敗"}), 500

@admin_api_bp.route('/send_command', methods=['POST'])
def admin_send_command():
    """管理員發送單一機器指令"""
    data = request.json or {}
    cmd = data.get('cmd')
    if not cmd:
        return jsonify({"success": False, "message": "參數缺失：cmd"}), 400

    try:
        resp = shared.machine._send_command(cmd)
        return jsonify({"success": True, "cmd": cmd, "response": resp})
    except Exception as e:
        logger.error(f"admin_send_command error: {e}")
        return jsonify({"success": False, "message": f"執行失敗: {e}"}), 500

@admin_api_bp.route('/get_uart', methods=['GET'])
def get_uart():
    try:
        if shared.MACHINE_CONFIG_FILE.exists():
            cfg = json.loads(shared.MACHINE_CONFIG_FILE.read_text(encoding='utf-8'))
            return jsonify({"success": True, "uart": cfg})
        return jsonify({"success": True, "uart": {"port": shared.machine.port, "baudrate": shared.machine.baudrate}})
    except Exception as e:
        logger.error(f"get_uart error: {e}")
        return jsonify({"success": False, "message": "讀取設定失敗"}), 500

@admin_api_bp.route('/test_uart', methods=['POST'])
def test_uart():
    """測試機器 UART 連線 (不儲存)"""
    data = request.json or {}
    port = data.get('port')
    baud = data.get('baudrate')
    
    if not port or baud is None:
        return jsonify({"success": False, "message": "參數缺失"}), 400
        
    try:
        baud = int(baud)
        success, msg = shared.machine.test_connection(port, baud)
        if success:
             return jsonify({"success": True, "message": f"測試連線成功 ({port}, {baud})"})
        else:
             return jsonify({"success": False, "message": f"測試連線失敗: {msg}"})
    except Exception as e:
        logger.error(f"test_uart error: {e}")
        return jsonify({"success": False, "message": f"測試例外錯誤: {e}"}), 500

@admin_api_bp.route('/disconnect_uart', methods=['POST'])
def disconnect_uart():
    """斷開機器 UART 連線"""
    try:
        if shared.machine.ser and shared.machine.ser.is_open:
            shared.machine.ser.close()
            logger.info("Admin action: Disconnected UART.")
            return jsonify({"success": True, "message": "已斷開 UART 連線"})
        else:
            return jsonify({"success": True, "message": "UART 連線早已斷開"})
    except Exception as e:
        logger.error(f"disconnect_uart error: {e}")
        return jsonify({"success": False, "message": f"斷線失敗: {e}"}), 500

@admin_api_bp.route('/reconnect_uart', methods=['POST'])
def reconnect_uart():
    """重新連接機器 UART 並執行初始化流程 (Wake up -> Homing)"""
    try:
        # 讀取目前設定 (以確保使用最新設定)
        port = shared.machine.port
        baud = shared.machine.baudrate
        
        # 1. 強制斷線 (如果還連著)
        if shared.machine.ser and shared.machine.ser.is_open:
            try:
                shared.machine.ser.close()
            except Exception:
                pass
        
        # 2. 銷毀並重新建立 MachineController 實例
        # 這是最徹底的重置方式，確保所有狀態（包括線程鎖、內部旗標、計時器等）都回到初始狀態
        try:
            old_machine = shared.machine
            # 嘗試釋放舊資源
            if old_machine:
                try:
                    if old_machine.ser and old_machine.ser.is_open:
                         old_machine.ser.close()
                except: pass
            
            # 重新初始化 MachineController
            shared.init_machine_controller()
            logger.info("MachineController re-initialized.")
            
            # 3. 觸發初始化流程 (init_machine)
            # 由於這是全新的實例，直接呼叫 init_machine 即可
            # 為了避免阻塞 API 回應，我們在這裡同步執行一次初始化，或選擇性地丟到背景
            # 根據使用者需求「觸發一開始執行的流程」，我們這裡同步執行 init_machine
            
            init_result = shared.machine.init_machine()
            
            if init_result:
                 return jsonify({"success": True, "message": f"已完全重啟機器控制器並初始化完成 ({port}, {baud})"})
            else:
                 return jsonify({"success": True, "message": f"已重啟控制器，但初始化流程可能未完全成功 ({port}, {baud})"})

        except Exception as e:
            logger.error(f"Failed to recreate MachineController: {e}")
            return jsonify({"success": False, "message": f"控制器重啟失敗: {e}"})
    except Exception as e:
        logger.error(f"reconnect_uart error: {e}")
        return jsonify({"success": False, "message": f"重連失敗: {e}"}), 500

@admin_api_bp.route('/set_uart', methods=['POST'])
def set_uart():
    """設定機器 UART"""
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
        
        shared.MACHINE_CONFIG_FILE.write_text(json.dumps(uart_cfg), encoding='utf-8')
        
        current_conf = {}
        if shared.CONFIG_FILE.exists():
            current_conf = yaml.safe_load(shared.CONFIG_FILE.read_text(encoding='utf-8')) or {}
        current_conf['machine'] = uart_cfg
        shared.CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')
        
        global_config.reload_config()

        applied = False
        try:
            applied = shared.machine.set_uart(port, baud)
        except Exception as e:
            logger.error(f"apply UART failed: {e}")
            
        logger.info(f"Admin action: set_uart -> {uart_cfg}, applied={applied}")
        return jsonify({"success": True, "applied": applied, "uart": uart_cfg})
    except Exception as e:
        logger.error(f"set_uart error: {e}")
        return jsonify({"success": False, "message": "設定失敗"}), 500

@admin_api_bp.route('/get_library_config', methods=['GET'])
def get_library_config():
    global_config.reload_config()
    
    return jsonify({
        "success": True,
        "config": {
            "mock_enabled": global_config.LIBRARY_MOCK_ENABLED,
            "host": global_config.LIBRARY_HOST,
            "port": global_config.LIBRARY_PORT,
            "login_enabled": global_config.LIBRARY_LOGIN_ENABLED,
            "login_user": global_config.LIBRARY_USER,
            "login_pass": "********" if global_config.LIBRARY_PASS else "",
            "institution_id": global_config.LIBRARY_INSTITUTION,
            "checkin_enabled": global_config.LIBRARY_CHECKIN_ENABLED,
            "attachment_acceptance_enabled": global_config.ATTACHMENT_ACCEPTANCE_ENABLED
        }
    })

@admin_api_bp.route('/set_library_config', methods=['POST'])
def set_library_config():
    """設定圖書館連線設定"""
    data = request.json or {}
    mock_enabled = data.get('mock_enabled', False)
    host = data.get('host')
    port = data.get('port')
    login_enabled = data.get('login_enabled', False)
    checkin_enabled = data.get('checkin_enabled', False)
    attachment_acceptance_enabled = data.get('attachment_acceptance_enabled', True)
    user = data.get('login_user')
    pwd = data.get('login_pass')
    inst = data.get('institution_id')

    if not host or not port:
         return jsonify({"success": False, "message": "Host 與 Port 為必填"}), 400

    if not inst:
         return jsonify({"success": False, "message": "機構代碼 (AO) 為必填"}), 400

    try:
        current_conf = {}
        if shared.CONFIG_FILE.exists():
            current_conf = yaml.safe_load(shared.CONFIG_FILE.read_text(encoding='utf-8')) or {}
        
        lib_conf = current_conf.get('library', {})
        lib_conf['mock_enabled'] = bool(mock_enabled)
        lib_conf['host'] = host
        lib_conf['port'] = int(port)
        lib_conf['login_enabled'] = bool(login_enabled)
        lib_conf['login_user'] = user or ""
        if pwd and pwd != "********":
            lib_conf['login_pass'] = pwd
        elif pwd == "": 
             lib_conf['login_pass'] = ""
             
        lib_conf['institution_id'] = inst or "MAIN"
        lib_conf['checkin_enabled'] = bool(checkin_enabled)
        lib_conf['attachment_acceptance_enabled'] = bool(attachment_acceptance_enabled)
        
        current_conf['library'] = lib_conf
        
        shared.CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')
        
        global_config.reload_config()
        
        shared.init_sip2_client()
        
        return jsonify({"success": True, "message": "圖書館設定已更新並嘗試重新連線"})
        
    except Exception as e:
        logger.error(f"set_library_config error: {e}")
        return jsonify({"success": False, "message": f"設定失敗: {e}"}), 500


@admin_api_bp.route('/get_front_images', methods=['GET'])
def get_front_images():
    """取得前台掃描/放書示意圖的目前設定路徑"""
    try:
        if shared.CONFIG_FILE.exists():
            current_conf = yaml.safe_load(shared.CONFIG_FILE.read_text(encoding='utf-8')) or {}
        else:
            current_conf = {}

        front_images = current_conf.get('front_images', {}) or {}
        return jsonify({
            "success": True,
            "images": {
                "scan_guide": front_images.get('scan_guide', ''),
                "place_book_guide": front_images.get('place_book_guide', ''),
                "scan_guide_en": front_images.get('scan_guide_en', ''),
                "place_book_guide_en": front_images.get('place_book_guide_en', ''),
            }
        })
    except Exception as e:
        logger.error(f"get_front_images error: {e}")
        return jsonify({"success": False, "message": "讀取設定失敗"}), 500


@admin_api_bp.route('/upload_front_images', methods=['POST'])
def upload_front_images():
    """上傳/更新前台掃描區與放書示意圖

    接受 multipart/form-data：
      - scan_guide: 檔案 (選填)
      - place_book_guide: 檔案 (選填)
    儲存於 static/uploads，並在 config.yml 的 front_images 中寫入相對路徑
    （例如 uploads/scan_guide.png），前端再透過 url_for('static', filename=...) 存取。
    """
    try:
        # 讀取現有設定
        if shared.CONFIG_FILE.exists():
            current_conf = yaml.safe_load(shared.CONFIG_FILE.read_text(encoding='utf-8')) or {}
        else:
            current_conf = {}

        front_images = current_conf.get('front_images', {}) or {}

        def save_image(field_name, default_basename):
            file = request.files.get(field_name)
            if not file or not file.filename:
                return None

            # 只允許基本安全的副檔名（加入 .svg 支援）
            suffix = Path(file.filename).suffix.lower()
            if suffix not in {'.png', '.jpg', '.jpeg', '.gif', '.svg'}:
                raise ValueError(f"不支援的檔案格式: {suffix}")

            safe_name = f"{default_basename}{suffix}"
            dest_path = UPLOAD_DIR / safe_name
            file.save(dest_path)

            # 回傳給 config 使用 static 下的相對路徑
            return f"uploads/{safe_name}"

        for field in ('scan_guide', 'place_book_guide', 'scan_guide_en', 'place_book_guide_en'):
            try:
                new_path = save_image(field, field)
                if new_path:
                    front_images[field] = new_path
            except ValueError as ve:
                return jsonify({"success": False, "message": str(ve)}), 400

        current_conf['front_images'] = front_images
        shared.CONFIG_FILE.write_text(yaml.dump(current_conf, allow_unicode=True), encoding='utf-8')

        # 重新載入全域設定，確保 shared.cfg 內也能看到
        global_config.reload_config()
        shared.load_config()

        return jsonify({
            "success": True,
            "message": "前台圖片已更新",
            "images": front_images,
        })
    except Exception as e:
        logger.error(f"upload_front_images error: {e}")
        return jsonify({"success": False, "message": "圖片上傳或儲存失敗"}), 500

@admin_api_bp.route('/test_library_conn', methods=['POST'])
def test_library_conn():
    """測試圖書館連線"""
    data = request.json or {}
    mock_enabled = data.get('mock_enabled', False)
    host = data.get('host')
    port = data.get('port')
    login_enabled = data.get('login_enabled', False)
    user = data.get('login_user')
    pwd = data.get('login_pass')
    inst = data.get('institution_id')
    
    if not login_enabled:
        user = ""
        pwd = ""
    else:
        if pwd == "********":
            pwd = global_config.LIBRARY_PASS

    if not host or not port:
         return jsonify({"success": False, "message": "Host 與 Port 為必填"}), 400
         
    try:
        if mock_enabled or str(host).lower() == 'mock':
             return jsonify({"success": True, "message": "Mock 連線測試成功 (強制 Mock 模式)"})

        # Import SIP2Client specifically for testing to avoid shared instance modification
        from sip2_client import SIP2Client
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


# ─── 緊急恢復端點 (Admin Recovery) ─────────────────────────────────────────────
#
# 當機器卡在錯誤狀態（例如 "error state 0" 因 serial glitch）時，現場操作員
# 過去必須 ssh 進機台執行 `systemctl --user restart pyserver.service` 才能
# 恢復。以下四個端點讓管理員可以在後台 UI 直接點按執行修復動作，不必下指令列。
#
# 重要原則：
#   1. 全部為 admin 專屬，沿用 admin_api_bp.before_request 的 session 檢查。
#   2. 全部為「人工觸發」，不會被自動執行（自動恢復屬於另一支 agent 的任務）。
#   3. 整個 request 最長 35 秒，超時直接回傳訊息要求人工檢查，避免 UI 永遠
#      在等。
#   4. 所有狀況都會 log 至 INFO，方便事後對照當天的時間軸。

_RECOVERY_OVERALL_TIMEOUT_SEC = 35


def _send_with_deadline(cmd, deadline):
    """在剩餘時間內呼叫 machine._send_command。

    回傳 (resp, timed_out)：若已超過 deadline 則回傳 (None, True)，
    呼叫端應在此情況下立即結束流程。
    """
    if time.time() >= deadline:
        return None, True
    resp = shared.machine._send_command(cmd)
    return resp, False


@admin_api_bp.route('/machine/reset', methods=['POST'])
def machine_reset():
    """完整重置機器：關閉序列埠 -> 重新開啟 -> dep1 -> homing。

    使用情境：序列埠卡死、機器狀態 unknown / error / 0 時可由管理員一鍵
    重置。會佔用 shared.machine.lock (RLock)，避免與正在執行中的還書動作
    搶序列埠。
    """
    logger.info("admin recovery triggered: machine/reset")
    start_ts = time.time()
    deadline = start_ts + _RECOVERY_OVERALL_TIMEOUT_SEC
    steps = []

    try:
        if shared.machine is None:
            return jsonify({
                "success": False,
                "message": "機器控制器尚未初始化",
                "steps": steps,
            })

        with shared.machine.lock:
            # 1. 關閉現有序列埠
            try:
                if shared.machine.ser and shared.machine.ser.is_open:
                    shared.machine.ser.close()
                    steps.append({"step": "close_serial", "ok": True})
                else:
                    steps.append({"step": "close_serial", "ok": True, "note": "already closed"})
            except Exception as e:
                logger.error(f"machine/reset close_serial error: {e}")
                steps.append({"step": "close_serial", "ok": False, "error": str(e)})

            if time.time() > deadline:
                return jsonify({
                    "success": False,
                    "message": "重置流程耗時超過 35 秒，請手動檢查",
                    "steps": steps,
                })

            # 2. 重新開啟序列埠（沿用 MachineController.__init__ 的參數）
            import serial as _serial
            try:
                shared.machine.ser = _serial.Serial(
                    port=shared.machine.port,
                    baudrate=shared.machine.baudrate,
                    bytesize=_serial.EIGHTBITS,
                    parity=_serial.PARITY_NONE,
                    stopbits=_serial.STOPBITS_ONE,
                    timeout=2,
                )
                steps.append({"step": "reopen_serial", "ok": True,
                              "port": shared.machine.port,
                              "baudrate": shared.machine.baudrate})
            except Exception as e:
                logger.error(f"machine/reset reopen_serial error: {e}")
                steps.append({"step": "reopen_serial", "ok": False, "error": str(e)})
                return jsonify({
                    "success": False,
                    "message": f"重新開啟序列埠失敗: {e}",
                    "steps": steps,
                })

            if time.time() > deadline:
                return jsonify({
                    "success": False,
                    "message": "重置流程耗時超過 35 秒，請手動檢查",
                    "steps": steps,
                })

            # 3. dep1：等待 "power on"，最多 10 秒
            dep_deadline = min(time.time() + 10, deadline)
            dep_resp, timed_out = _send_with_deadline("dep1", dep_deadline)
            if timed_out:
                return jsonify({
                    "success": False,
                    "message": "重置流程耗時超過 35 秒，請手動檢查",
                    "steps": steps,
                })
            dep_ok = is_completion("dep1", dep_resp)
            steps.append({"step": "dep1", "ok": dep_ok, "response": dep_resp})

            if time.time() > deadline:
                return jsonify({
                    "success": False,
                    "message": "重置流程耗時超過 35 秒，請手動檢查",
                    "steps": steps,
                })

            # 4. homing：等待 "Homed" 完成訊息（兩階段協定，ack 不算完成），最多 30 秒
            home_deadline = min(time.time() + 30, deadline)
            home_resp, timed_out = _send_with_deadline("homing", home_deadline)
            if timed_out:
                return jsonify({
                    "success": False,
                    "message": "重置流程耗時超過 35 秒，請手動檢查",
                    "steps": steps,
                })
            home_ok = is_completion("homing", home_resp)
            steps.append({"step": "homing", "ok": home_ok, "response": home_resp})

            overall_ok = dep_ok and home_ok
            return jsonify({
                "success": overall_ok,
                "message": "機器重置完成" if overall_ok else "重置流程已執行，但部分步驟未取得預期回應",
                "steps": steps,
            })

    except Exception as e:
        logger.error(f"machine/reset error: {e}")
        return jsonify({"success": False, "message": str(e), "steps": steps})


@admin_api_bp.route('/machine/force_homing', methods=['POST'])
def machine_force_homing():
    """直接執行 homing，給機器一個明確的「回原點」指令。

    與 machine/reset 的差別：不會關 / 開序列埠，只送 homing 指令。
    """
    logger.info("admin recovery triggered: machine/force_homing")
    start_ts = time.time()
    deadline = start_ts + _RECOVERY_OVERALL_TIMEOUT_SEC

    try:
        if shared.machine is None:
            return jsonify({"success": False, "message": "機器控制器尚未初始化"})

        # _send_command 已內含 lock，不必再外層加鎖（lock 是 RLock，重入也安全，
        # 但這裡保持單純）
        resp = shared.machine._send_command("homing")
        if time.time() > deadline:
            return jsonify({
                "success": False,
                "message": "Homing 流程耗時超過 35 秒，請手動檢查",
                "response": resp,
            })
        ok = is_completion("homing", resp)
        return jsonify({
            "success": ok,
            "message": "Homing 完成" if ok else f"Homing 回應未確認: {resp}",
            "response": resp,
        })

    except Exception as e:
        logger.error(f"machine/force_homing error: {e}")
        return jsonify({"success": False, "message": str(e)})


@admin_api_bp.route('/sip2/reconnect', methods=['POST'])
def sip2_reconnect():
    """強制重新初始化 SIP2 連線。

    使用 shared.sip2._io_lock（plain Lock，不能重入）防止在 in-flight 還書中
    斷線；若拿不到 lock 也只是讓 init 繼續執行，不阻擋。
    """
    logger.info("admin recovery triggered: sip2/reconnect")
    start_ts = time.time()
    deadline = start_ts + _RECOVERY_OVERALL_TIMEOUT_SEC

    try:
        io_lock = None
        try:
            io_lock = getattr(shared.sip2, '_io_lock', None) if shared.sip2 is not None else None
        except Exception:
            io_lock = None

        held = False
        if io_lock is not None:
            try:
                held = io_lock.acquire(timeout=5)
            except Exception:
                held = False

        try:
            shared.init_sip2_client()
        finally:
            if held and io_lock is not None:
                try:
                    io_lock.release()
                except Exception:
                    pass

        if time.time() > deadline:
            return jsonify({
                "success": False,
                "message": "SIP2 重連耗時超過 35 秒，請手動檢查",
            })

        # 等待背景 poller 拿到新的 client 狀態
        time.sleep(1.5)

        if time.time() > deadline:
            return jsonify({
                "success": False,
                "message": "SIP2 重連耗時超過 35 秒，請手動檢查",
            })

        library_ok = False
        try:
            cached = shared.get_cached_status() or {}
            library_ok = bool(cached.get('library_ok', False))
        except Exception:
            library_ok = False

        return jsonify({
            "success": True,
            "message": "SIP2 已重新連線" + ("，library_ok=True" if library_ok else "，但 library_ok 仍為 False，請確認 SIP2 Server 狀態"),
            "library_ok": library_ok,
        })

    except Exception as e:
        logger.error(f"sip2/reconnect error: {e}")
        return jsonify({"success": False, "message": str(e)})


@admin_api_bp.route('/machine/clear_error', methods=['POST'])
def machine_clear_error():
    """清除「error state 0」這類卡死狀態：先送 cancel，再送 homing。

    若機器卡在執行中的動作，cancel 會讓它放棄；接著 homing 讓它回到 ready。
    """
    logger.info("admin recovery triggered: machine/clear_error")
    start_ts = time.time()
    deadline = start_ts + _RECOVERY_OVERALL_TIMEOUT_SEC

    try:
        if shared.machine is None:
            return jsonify({"success": False, "message": "機器控制器尚未初始化"})

        cancel_resp = shared.machine._send_command("cancel")
        if time.time() > deadline:
            return jsonify({
                "success": False,
                "message": "Clear error 耗時超過 35 秒，請手動檢查",
                "responses": {"cancel": cancel_resp, "homing": None},
            })

        homing_resp = shared.machine._send_command("homing")
        if time.time() > deadline:
            return jsonify({
                "success": False,
                "message": "Clear error 耗時超過 35 秒，請手動檢查",
                "responses": {"cancel": cancel_resp, "homing": homing_resp},
            })

        ok = is_completion("homing", homing_resp)
        return jsonify({
            "success": ok,
            "message": "錯誤狀態已清除" if ok else "已送出 cancel + homing，但 homing 回應未確認",
            "responses": {"cancel": cancel_resp, "homing": homing_resp},
        })

    except Exception as e:
        logger.error(f"machine/clear_error error: {e}")
        return jsonify({"success": False, "message": str(e)})