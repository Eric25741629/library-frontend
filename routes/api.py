from flask import Blueprint, request, jsonify
from datetime import datetime
import time
import threading
import logging

import shared

api_bp = Blueprint('api', __name__)
logger = logging.getLogger('api')

@api_bp.route('/status')
def get_status():
    """檢查系統狀態（包含圖書館與機器通訊）"""
    suspended = shared.is_box_full()
    limit = shared.MAX_RETURN_LIMIT

    # 檢查圖書館通訊
    library_ok = False
    try:
        if shared.sip2:
            try:
                test_info = shared.sip2.get_book_info('ping_test')
            except Exception:
                test_info = None
            library_ok = test_info is not None
        else:
            library_ok = False
    except Exception as e:
        logger.error(f"status check library error: {e}")
        library_ok = False

    # 檢查機器通訊
    machine_ok = False
    machine_debug = {"ser_open": False, "type": None}
    machine_state = "unknown"
    homing_in_progress = False
    is_homed = False
    
    machine = shared.machine

    try:
        machine_debug["type"] = type(machine).__name__ if machine is not None else None
        ser = getattr(machine, 'ser', None)
        ser_open = bool(ser and getattr(ser, 'is_open', False))
        machine_ok = ser_open
        machine_debug["ser_open"] = ser_open

        # 主動更新機器狀態
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

@api_bp.route('/scan', methods=['POST'])
def scan_book():
    """掃描書籍 API"""
    if shared.is_box_full():
        return jsonify({"success": False, "message": "還書箱已滿，暫停服務", "code": "SERVICE_SUSPENDED"}), 503

    data = request.json
    book_id = data.get('book_id')

    if not book_id:
        return jsonify({"success": False, "message": "請輸入書籍編號"}), 400

    # 1. 查詢 SIP2 圖書館系統
    if not shared.sip2:
        return jsonify({"success": False, "message": "圖書館系統未連接"}), 500
        
    book_info = shared.sip2.get_book_info(book_id)

    if not book_info:
        return jsonify({"success": False, "message": f"找不到書籍編號: {book_id}"}), 404

    image_url = f"https://picsum.photos/seed/{book_id}/100/150"

    # 3. 檢查是否逾期
    is_overdue = False
    due_date_str = book_info.get('due_date')
    if due_date_str:
        try:
            due_date = None
            for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
                try:
                    due_date = datetime.strptime(due_date_str.strip(), fmt)
                    break
                except ValueError:
                    continue
            
            if due_date:
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

@api_bp.route('/return', methods=['POST'])
def return_book():
    """確認還書 API"""
    if shared.is_box_full():
        return jsonify({"success": False, "message": "還書箱已滿，暫停服務", "code": "SERVICE_SUSPENDED"}), 503

    data = request.json or {}
    book_ids = data.get('book_ids', [])
    attachment_only = data.get('attachment_only', False)
    attachment_barcode = data.get('attachment_barcode')
    
    if 'book_id' in data:
        book_ids = [data['book_id']]
    
    if not book_ids:
        return jsonify({"success": False, "message": "未選擇任何書籍"}), 400

    if attachment_only:
        if len(book_ids) != 1:
            return jsonify({"success": False, "message": "附件歸還僅支援單本處理"}), 400
        if not attachment_barcode:
            return jsonify({"success": False, "message": "缺少附件條碼 (attachment_barcode)"}), 400
        if attachment_barcode.strip().upper() != book_ids[0].strip().upper():
            return jsonify({"success": False, "message": "附件條碼與書籍不符，請重新掃描"}), 400

    returned_books = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    machine = shared.machine
    
    # 硬體還書流程控制
    try:
        status = machine.get_status()
        is_state_3 = False
        
        s_status = str(status).strip()
        if s_status.isdigit() and int(s_status) == getattr(machine, 'STATE_OPENED', 3):
            is_state_3 = True
        elif "opened" in s_status.lower() or "error state 3" in s_status.lower():
            is_state_3 = True
            
        if is_state_3:
            logger.info(f"Machine is in state {status} (OPENED). Proceeding to close directly.")
        else:
            logger.warning(f"Machine state is '{status}' (Not OPENED). Triggering HOMING as safety fallback.")
            resp = machine._send_command("homing")
            logger.info(f"Fallback homing response: {resp}")
            
    except Exception as e:
        logger.error(f"Pre-close status check failed: {e}. Defaulting to HOMING.")
        try:
             machine._send_command("homing")
        except:
             pass

    # 關門前預檢
    if shared.BOOK_CHECK_ENABLED:
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

    # 關閉投書口
    machine.close_door()
    time.sleep(2)

    # 關門後複檢
    if shared.BOOK_CHECK_ENABLED:
        if not machine.check_book_status():
            logger.info("Post-check failed: Book not detected in return box, triggering async reopen.")
            
            def async_reopen():
                try:
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

    # 開始處理
    conn = shared.get_box_db()
    cursor = conn.cursor()

    try:
        for b_id in book_ids:
            checkin_success = True
            if not attachment_only:
                 if shared.sip2:
                    checkin_success = shared.sip2.checkin_book(b_id)
                 else:
                    checkin_success = False # No SIP2 connection

            if checkin_success:
                book_info = shared.sip2.get_book_info(b_id) if shared.sip2 else None
                title = book_info['title'] if book_info else 'Unknown'
                if attachment_only:
                    title += " (附件)"
                
                image_url = f"https://picsum.photos/seed/{b_id}/100/150"

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
