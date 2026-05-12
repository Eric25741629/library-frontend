from flask import Blueprint, request, jsonify
from datetime import datetime
import time
import threading
import logging
import asyncio
import os
import subprocess

import shared
import config

api_bp = Blueprint('api', __name__)
logger = logging.getLogger('api')


# SIP2 09/10 checkin 成功時，AF/AG 欄位實務上常是「還書成功」這種無資訊量的回音，
# 也可能夾帶逾期罰款或預約到館等真正需要提示的訊息。直接把任何 AF/AG 都標成
# 「逾期提醒」會造成沒過期的書也跳出逾期警告，所以先過濾無資訊量回音，再依關鍵字
# 區分逾期與一般館方通知。
_SIP2_TRIVIAL_NOTICES = {'還書成功', '還書完成', '歸還成功', '歸還完成',
                         'check-in success', 'checkin success', 'success', 'ok'}
_SIP2_OVERDUE_KEYWORDS = ('逾期', '過期', '罰款', '滯納', '超過借閱', '欠款')


def _classify_sip2_notice(text):
    """將 SIP2 AF/AG 文字分類；回傳 (text, 'overdue'|'notice') 或 None（無資訊量）。"""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    stripped = s.replace('。', '').replace('.', '').strip().lower()
    if stripped in _SIP2_TRIVIAL_NOTICES:
        return None
    if any(k in s for k in _SIP2_OVERDUE_KEYWORDS):
        return (s, 'overdue')
    return (s, 'notice')

_FRONTEND_RESTART_LOCK = threading.Lock()
_FRONTEND_RESTART_IN_PROGRESS = False
_FRONTEND_RESTART_LAST_TS = 0.0


def _build_user_systemd_env():
    """Build environment required for systemctl --user from Flask service context."""
    uid = os.getuid()
    env = os.environ.copy()
    xdg_runtime = env.get('XDG_RUNTIME_DIR') or f'/run/user/{uid}'
    env['XDG_RUNTIME_DIR'] = xdg_runtime
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={xdg_runtime}/bus')
    return env


_INTRUDER_WINDOW_PROCS = (
    # Touching these names terminates known GNOME/Ubuntu surface-level
    # intruders that might have stolen focus from Firefox. This is a
    # defense-in-depth layer; the primary defense is install_gnome_lockdown
    # (disabling the keybindings that open them in the first place).
    'gnome-control-center',
    'gnome-calculator',
    'gnome-screenshot',
    'gnome-system-monitor',
    'gnome-terminal',
    'gnome-disks',
    'nautilus',
    'evince',
    'eog',
    'file-roller',
    'gedit',
    'xterm',
)


def _kill_intruder_windows():
    # 單次 pkill 用 ERE 交替 (a|b|c) 比串行 12 次快一個數量級；最壞情況從 36s 降到 3s。
    pattern = '|'.join(_INTRUDER_WINDOW_PROCS)
    try:
        subprocess.run(['pkill', '-u', 'kiosk', '-f', pattern],
                       timeout=3, check=False)
    except Exception as e:
        logger.debug(f"pkill intruder windows failed: {e}")


def _restart_frontend_services(trigger: str):
    global _FRONTEND_RESTART_IN_PROGRESS
    try:
        # Give current HTTP response a short window to flush before restarting.
        time.sleep(0.3)
        # Defense in depth: kill any intruder windows that stole focus before
        # we restart Firefox, so Firefox returns to the top of the stack.
        _kill_intruder_windows()
        env = _build_user_systemd_env()
        result = subprocess.run(
            ['systemctl', '--user', 'restart', 'kiosk-browser.service'],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or '').strip()
            logger.error(f"frontend auto-restart failed (trigger={trigger}): {err}")
        else:
            logger.warning(f"frontend auto-restart triggered (trigger={trigger})")
    except Exception as e:
        logger.error(f"frontend auto-restart exception (trigger={trigger}): {e}")
    finally:
        with _FRONTEND_RESTART_LOCK:
            _FRONTEND_RESTART_IN_PROGRESS = False

def _compute_target_bin(location: str, book_id: str = None) -> int:
    """根據條碼編號決定分類箱：
    - 條碼以 C 或 G 開頭 → put1 (=1)
    - 其他 → put2 (=2)
    """
    try:
        # 優先使用條碼編號前綴判斷
        if book_id:
            b_id = str(book_id).strip().upper()
            if b_id.startswith('C') or b_id.startswith('G'):
                return 1
            else:
                return 2
        
        # 如果沒有條碼，回退到館藏位置判斷（中文館藏使用 put1）
        text = (location or '').strip()
        return 1 if ('中文' in text) else 2
    except Exception:
        return 2

@api_bp.route('/status')
def get_status():
    """檢查系統狀態（包含圖書館與機器通訊）"""
    suspended = shared.is_box_full()
    limit = shared.MAX_RETURN_LIMIT
    bin_counts = shared.get_bin_counts()

    # 檢查圖書館通訊
    # 改用 SIP2 99 SC Status 健康檢查，而不是一直送 17 Item Information
    library_ok = False
    try:
        if shared.sip2:
            try:
                library_ok = bool(shared.sip2.health_check())
            except Exception:
                library_ok = False
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
        "bin_counts": bin_counts,
        "library_ok": library_ok,
        "machine_ok": machine_ok,
        "machine_state": machine_state,
        "homing_in_progress": homing_in_progress,
        "is_homed": is_homed,
        "machine_debug": machine_debug
    })


@api_bp.route('/frontend/fullscreen_lost', methods=['POST'])
def frontend_fullscreen_lost():
    """Receive fullscreen-loss signal from kiosk frontend and restart services if needed."""
    global _FRONTEND_RESTART_LAST_TS, _FRONTEND_RESTART_IN_PROGRESS

    if shared.is_manual_frontend_stop_active():
        return jsonify({
            "success": True,
            "skipped": True,
            "reason": "manual_stop_active",
            "message": "已偵測為人工關閉，略過自動重啟"
        })

    payload = request.json or {}
    trigger = str(payload.get('trigger') or 'unknown').strip()[:64]
    now = time.time()

    with _FRONTEND_RESTART_LOCK:
        # Debounce noisy browser events (resize/visibility/fullscreenchange may fire in burst).
        if _FRONTEND_RESTART_IN_PROGRESS:
            return jsonify({
                "success": True,
                "skipped": True,
                "reason": "restart_in_progress",
                "message": "重啟程序進行中"
            })

        if (now - _FRONTEND_RESTART_LAST_TS) < 15:
            return jsonify({
                "success": True,
                "skipped": True,
                "reason": "cooldown",
                "message": "重啟冷卻中"
            })

        _FRONTEND_RESTART_LAST_TS = now
        _FRONTEND_RESTART_IN_PROGRESS = True

    threading.Thread(target=_restart_frontend_services, args=(trigger,), daemon=True).start()

    return jsonify({
        "success": True,
        "scheduled": True,
        "message": "已通知後端重啟服務",
        "trigger": trigger
    })


@api_bp.route('/frontend/session_started', methods=['POST'])
def frontend_session_started():
    """Clear manual-stop suppression when kiosk frontend is intentionally started again."""
    if shared.is_manual_frontend_stop_active():
        shared.clear_manual_frontend_stop()
        logger.info("frontend manual-stop suppression cleared by new session")
    return jsonify({"success": True})

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
        logger.error("SIP2 client is not initialized")
        return jsonify({"success": False, "message": "圖書館系統未連接"}), 500
        
    logger.info(f"Querying book with ID: {book_id}")
    book_info = shared.sip2.get_book_info(book_id)

    if not book_info:
        logger.warning(f"No book found for ID: {book_id}")
        return jsonify({"success": False, "message": f"找不到書籍編號: {book_id}"}), 404

    # If SIP2 client returned an error dict (e.g., AF/AG message), handle it
    if isinstance(book_info, dict) and book_info.get('error'):
        msg = book_info.get('message') or '查詢失敗'
        logger.warning(f"SIP2 reported error for {book_id}: {msg}")
        return jsonify({"success": False, "message": msg}), 400

    logger.info(f"Found book: {book_info.get('title', 'Unknown')} by {book_info.get('author', 'Unknown')}")

    # 2. 只解析 AH 欄位的狀態文字（例如："已被外借/2026-02-26"、"仍在館內"），不再使用到期日判斷逾期
    ah_field = (book_info.get('due_date') or '').strip()
    if '在館內' in ah_field or '仍在館內' in ah_field:
        logger.warning(f"Book {book_id} is already in library (AH: {ah_field})")
        return jsonify({
            "success": False,
            "code": "ALREADY_IN_LIBRARY",
            "message": f"書籍「{book_info['title']}」已經在館內了，無需還書。",
            "data": {
                "title": book_info['title'],
                "author": book_info.get('author', 'Unknown'),
                "status": ah_field
            }
        }), 400

    image_url = f"https://picsum.photos/seed/{book_id}/100/150"

    # 3. 不再使用爬蟲查詢到期日與館藏位置；僅使用 SIP2 回傳的資訊
    location = None

    # 4. 不再阻擋逾期書籍，改由人工櫃台處理罰款與逾期流程

    # 5. 依 AR/AQ 判斷附件狀態
    # AR 三種典型狀態：
    #   - 空字串            → 無附件
    #   - "附件未借出"     → 有設定附件，但目前沒有借附件，本機允許還書
    #   - 其他非空文字       → 代表附件目前有借出，本機不處理，請至櫃台
    attachment_ar = (book_info.get('attachment_ar') or '').strip()
    attachment_desc = book_info.get('attachment_desc')
    has_attachment_flag = bool(book_info.get('has_attachment'))

    # attachment_configured: 系統判定「有附件設定」
    # attachment_borrowed: 目前附件為「已借出」
    attachment_configured = False
    attachment_borrowed = False

    block_for_attachment = False
    block_reason = None

    if attachment_ar:
        attachment_configured = True
        if attachment_ar == '附件未借出':
            logger.info(f"Book {book_id} has un-borrowed attachment (AR='附件未借出'); allowed for kiosk.")
        else:
            # 任何非空且非「附件未借出」的 AR，視為有附件需人工處理
            block_for_attachment = True
            block_reason = f"AR={attachment_ar}"
            attachment_borrowed = True
    elif has_attachment_flag:
        # AR 未提供時，退回舊的 AQ 判斷
        attachment_configured = True
        block_for_attachment = True
        block_reason = attachment_desc or '該書含附件'

    if block_for_attachment and not config.ATTACHMENT_ACCEPTANCE_ENABLED:
        logger.warning(f"Book {book_id} has attachment that kiosk will not handle ({block_reason})")
        return jsonify({
            "success": False,
            "code": "ATTACHMENT_NOT_ACCEPTED",
            "message": "本機不處理含附件之書籍，請攜書及附件至人工櫃台辦理。",
            "data": {
                "title": book_info['title'],
                "author": book_info.get('author', 'Unknown'),
                "attachment_desc": attachment_desc,
                "attachment_ar": attachment_ar
            }
        }), 400

    # 4. 成功回傳，並附上館藏位置與分櫃建議（不再對外顯示到期日）
    target_bin = _compute_target_bin(location, book_id)
    return jsonify({
        "success": True,
        "message": "掃描成功",
        "data": {
            "book_id": book_info['barcode'],
            "title": book_info['title'],
            "author": book_info['author'],
            "image_url": image_url,
            "patron_name": book_info.get('patron_name'),
            "has_attachment": attachment_configured,
            "attachment_borrowed": attachment_borrowed,
            "attachment_desc": book_info.get('attachment_desc'),
            "attachment_ar": book_info.get('attachment_ar'),
            "location": location,
            "target_bin": target_bin
        }
    })


@api_bp.route('/check_due', methods=['POST'])
def check_due():
    """目前已停用逾期查詢與爬蟲功能，固定回應未啟用。"""
    return jsonify({
        "success": False,
        "message": "本機目前未啟用逾期查詢功能，請洽櫃台。",
        "disabled": True
    }), 503

@api_bp.route('/return', methods=['POST'])
def return_book():
    """確認還書 API"""
    if shared.is_box_full():
        return jsonify({"success": False, "message": "還書箱已滿，暫停服務", "code": "SERVICE_SUSPENDED"}), 503

    data = request.json or {}
    books_payload = data.get('books') or []  # [{book_id, location, target_bin}]
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
    # 蒐集還書成功但包含 AF/AG 訊息；每筆為 (text, kind)，kind ∈ {'overdue','notice'}
    notice_messages = []
    # 選擇分櫃（預設 1；若有提供 target_bin 則使用）
    selected_bin = None
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    machine = shared.machine

    # 先嘗試由前端資料或條碼推算分櫃，以便做滿箱檢查
    try:
        if books_payload and isinstance(books_payload, list):
            first = books_payload[0] if books_payload else None
            if isinstance(first, dict):
                b_id = first.get('book_id')
                location = first.get('location')
                selected_bin = int(first.get('target_bin') or _compute_target_bin(location, b_id))
        if selected_bin is None and book_ids:
            selected_bin = int(_compute_target_bin(None, book_ids[0]))
    except Exception:
        selected_bin = None

    if selected_bin is not None:
        bin_counts = shared.get_bin_counts()
        if bin_counts.get(str(selected_bin), 0) >= shared.MAX_RETURN_LIMIT:
            return jsonify({
                "success": False,
                "message": f"分類箱{selected_bin}已滿，暫停服務",
                "code": "BIN_FULL",
                "data": {"target_bin": selected_bin}
            }), 503
    
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
        # 若前端提供 books，則以 books 中的第一本為準（流程限制一次一本）
        if books_payload and isinstance(books_payload, list):
            try:
                first = books_payload[0]
                b_id = first.get('book_id')
                location = first.get('location')
                selected_bin = int(first.get('target_bin') or _compute_target_bin(location, b_id))
                book_ids = [b_id]
            except Exception:
                pass

        # 若有多本書，建立一個方便查詢的 map（以 book_id 對應前端傳來的完整資訊）
        books_map = {}
        try:
            for item in books_payload:
                if not isinstance(item, dict):
                    continue
                key = (item.get('book_id') or item.get('barcode'))
                if key:
                    books_map[str(key).strip()] = item
        except Exception:
            books_map = {}

        # 紀錄是否有還書失敗；若失敗則不做分類並重新開門，讓使用者取回書本洽詢櫃台
        all_checkin_success = True
        failed_books = []

        for b_id in book_ids:
            checkin_success = True
            if not attachment_only:
                # 檢查是否啟用真實還書功能
                config.reload_config()  # 重新載入配置
                if config.LIBRARY_CHECKIN_ENABLED and shared.sip2:
                    logger.info(f"Executing real checkin for book: {b_id}")
                    checkin_result = shared.sip2.checkin_book(b_id)

                    # 新版 checkin_book 會回傳 dict，內含 success 與 AF/AG 訊息；
                    # 若為舊版 bool 也能相容處理。
                    af_msg = None
                    ag_msg = None
                    if isinstance(checkin_result, dict):
                        checkin_success = bool(checkin_result.get('success'))
                        af_msg = checkin_result.get('af_message')
                        ag_msg = checkin_result.get('ag_message')
                    else:
                        checkin_success = bool(checkin_result)

                    if checkin_success:
                        logger.info(f"Real checkin successful for book: {b_id}")
                        # 09/10 成功，但 AF/AG 可能夾帶逾期說明或其他館方提醒。
                        # 先去重（AF/AG 在館方系統常為同一句），再分類；純成功回音會被丟掉。
                        seen = set()
                        for raw in (af_msg, ag_msg):
                            if not raw:
                                continue
                            key = str(raw).strip()
                            if key in seen:
                                continue
                            seen.add(key)
                            classified = _classify_sip2_notice(raw)
                            if classified:
                                notice_messages.append(classified)
                    else:
                        logger.warning(f"Real checkin failed for book: {b_id}")
                        all_checkin_success = False
                        failed_books.append(b_id)
                        break
                elif shared.sip2:
                    logger.info(f"Real checkin disabled - simulating checkin for book: {b_id}")
                    checkin_success = True  # 模擬成功
                else:
                    logger.warning(f"No SIP2 connection available for book: {b_id}")
                    checkin_success = False
                    all_checkin_success = False
                    failed_books.append(b_id)
                    break

            if checkin_success:
                # 優先使用前端在掃描步驟取得的書籍資料，避免在還書階段再次送出 17 Item Information
                raw_key = str(b_id).strip() if b_id is not None else None
                book_source = books_map.get(raw_key, {}) if raw_key else {}
                
                # 如果從 books_map 找不到，嘗試不同的 key 格式或從 SIP2 重新查詢
                if not book_source or not book_source.get('title'):
                    logger.warning(f"Book {b_id} not found in books_map, trying SIP2 query")
                    try:
                        if shared.sip2:
                            book_info_fallback = shared.sip2.get_book_info(b_id)
                            if book_info_fallback and not book_info_fallback.get('error'):
                                book_source = book_info_fallback
                    except Exception as e:
                        logger.error(f"Fallback book info query failed for {b_id}: {e}")

                title = book_source.get('title') or 'Unknown'
                if attachment_only:
                    title += " (附件)"

                # 前端若已提供 image_url 則沿用，否則使用預設隨機圖
                image_url = book_source.get('image_url') or f"https://picsum.photos/seed/{b_id}/100/150"

                # 分櫃資訊
                location = book_source.get('location')
                target_bin = None
                try:
                    target_bin = int(book_source.get('target_bin') or selected_bin or _compute_target_bin(location, b_id))
                except Exception:
                    target_bin = selected_bin or _compute_target_bin(location, b_id)

                cursor.execute('INSERT INTO box_inventory (book_id, title, image_url, return_time, target_bin) VALUES (?, ?, ?, ?, ?)',
                               (b_id, title, image_url, current_time, target_bin))

                returned_books.append({
                    "book_id": b_id,
                    "title": title,
                    "return_time": current_time,
                    "target_bin": target_bin
                })
        # 若有任何一本書還書指令失敗，則：
        # 1) 回滾本次交易，不記錄到本地 box_inventory
        # 2) 重新開啟投書口，讓使用者取回書本
        # 3) 回傳錯誤訊息，提醒使用者洽詢櫃台
        if not all_checkin_success:
            conn.rollback()
            try:
                logger.info(f"Checkin failed for books {failed_books}; reopening door to let user retrieve items.")

                def async_reopen_after_checkin_error():
                    try:
                        time.sleep(0.1)
                        machine.reopen_door()
                    except Exception as e:
                        logger.error(f"Reopen door after checkin error failed: {e}")

                threading.Thread(target=async_reopen_after_checkin_error, daemon=True).start()
            except Exception as e:
                logger.error(f"Failed to schedule reopen after checkin error: {e}")

            return jsonify({
                "success": False,
                "message": "圖書館系統拒絕本次還書，請取回書本並洽詢櫃台。",
                "code": "CHECKIN_FAILED",
                "failed_books": failed_books
            }), 400
        
        conn.commit()
        
        # 硬體分類：若已選定分櫃則使用，否則預設為館藏位置推導
        try:
            # 若本次沒有任何成功的還書紀錄，則不啟動分類動作，避免書未成功入帳就被送入箱內
            if not returned_books:
                logger.warning("No books were successfully checked in; skipping sort_book.")
                return jsonify({
                    "success": False,
                    "message": "沒有成功完成還書的書籍，請確認狀態或洽詢櫃台。",
                    "data": []
                }), 400

            if selected_bin is None:
                # 嘗試以最後一本的條碼編號和館藏位置決定
                last_book = returned_books[-1] if returned_books else {}
                last_book_id = last_book.get('book_id')
                last_loc = last_book.get('location')
                selected_bin = _compute_target_bin(last_loc, last_book_id)
            machine.sort_book(int(selected_bin or 1))
        except Exception as e:
            logger.error(f"sort_book failed: {e}")
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Return error: {e}")
        return jsonify({"success": False, "message": "系統錯誤"}), 500
    finally:
        conn.close()

    # 選出最後一筆訊息給前端顯示；optimistic 優先選 overdue 類，再退回一般 notice。
    selected_notice = None
    if notice_messages:
        overdue_only = [m for m in notice_messages if m[1] == 'overdue']
        selected_notice = (overdue_only or notice_messages)[-1]

    return jsonify({
        "success": True,
        "message": f"成功歸還 {len(returned_books)} 本書籍",
        "data": returned_books,
        # 若有 AF/AG 提示訊息，傳回前端顯示（不影響還書成功與否）
        # overdue_message 為了向後相容保留；notice_type ∈ {'overdue','notice'}
        "overdue_message": selected_notice[0] if selected_notice else None,
        "notice_type": selected_notice[1] if selected_notice else None
    })

@api_bp.route('/cancel', methods=['POST'])
def cancel_return():
    """取消還書 API - 直接發送 cancel 指令，不執行 home"""
    try:
        machine = shared.machine
        if not machine:
            return jsonify({"success": False, "message": "機器未連接"}), 500
            
        # 發送取消指令
        resp = machine._send_command("cancel")
        logger.info(f"Cancel command sent, response: {resp}")
        
        # 檢查回應是否包含 ack
        success = "ack" in str(resp).lower()
        
        if success:
            return jsonify({
                "success": True, 
                "message": "已發送取消指令",
                "response": str(resp)
            })
        else:
            return jsonify({
                "success": True, 
                "message": "取消指令已發送",
                "response": str(resp)
            })
            
    except Exception as e:
        logger.error(f"Cancel return error: {e}")
        return jsonify({"success": False, "message": "取消操作失敗", "detail": str(e)}), 500
