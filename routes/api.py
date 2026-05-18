from flask import Blueprint, Response, request, jsonify, stream_with_context
from datetime import datetime
import json
import queue
import time
import threading
import logging
import asyncio
import os
import subprocess

import shared
import config
from machine_controller import is_completion, is_busy_response, is_error_state_response

api_bp = Blueprint('api', __name__)
logger = logging.getLogger('api')


# ─── 還書進度追蹤（記憶體 stage tracker）─────────────────────────────────────
# 給前端 polling 用，取代固定秒數的階段切換。
# stage 對應：
#   pre_check    → 關門前確認狀態 / 預檢
#   closing      → close 指令執行中（投書口關閉）
#   closed       → close 完成，準備檢查書籍
#   checkin      → 連線圖書館系統還書
#   sorting      → put1 / put2 執行中（重新上磁 + 移箱）
#   done         → 全部完成
#   error        → 失敗（前端應停止輪詢並顯示錯誤）
_return_progress_lock = threading.Lock()
_return_progress = {
    "stage": "idle",
    "detail": "",
    "ts": 0.0,
    "target_bin": None,
}


def _set_return_stage(stage, detail="", target_bin=None):
    with _return_progress_lock:
        _return_progress["stage"] = stage
        _return_progress["detail"] = detail
        _return_progress["ts"] = time.time()
        if target_bin is not None:
            _return_progress["target_bin"] = target_bin
    logger.info(f"[return-stage] {stage}: {detail}")


def _reset_return_stage():
    _set_return_stage("idle", "")


# SIP2 09/10 checkin 成功時，AF/AG 欄位實務上常是「還書成功」這種無資訊量的回音，
# 也可能夾帶逾期罰款或預約到館等真正需要提示的訊息。直接把任何 AF/AG 都標成
# 「逾期提醒」會造成沒過期的書也跳出逾期警告，所以先過濾無資訊量回音，再依關鍵字
# 區分逾期與一般館方通知。
_SIP2_TRIVIAL_NOTICES = {'還書成功', '還書完成', '歸還成功', '歸還完成',
                         'check-in success', 'checkin success', 'success', 'ok'}
_SIP2_OVERDUE_KEYWORDS = ('逾期', '過期', '罰款', '滯納', '超過借閱', '欠款')

# 預約通知關鍵字。實務上 SIP2 server 回的 AF/AG 例如：
# "此資料有人預約，館藏狀態將變更為「預約待取」並寄發預約到館通知。"
_SIP2_RESERVATION_KEYWORDS = ('預約',)


def _extract_reservation_notice(*messages):
    """從 SIP2 checkin 回傳的 AF/AG 抽取預約通知文字；無則回傳 None。"""
    for raw in messages:
        if not raw:
            continue
        s = str(raw).strip()
        if not s:
            continue
        if any(k in s for k in _SIP2_RESERVATION_KEYWORDS):
            return s
    return None

# 圖書館系統拒絕還書時，若 AF/AG/error_message 含下列任一關鍵字，視為「已歸還」case。
# 實務上常見的情境是：第一次掃描→關門→checkin 成功，但回應在網路上掉了，前端誤判失敗
# 觸發重開門讓使用者再放一次；第二次 checkin 時 SIP2 server 回 "館藏資料異常請洽櫃台"
# 或 "已歸還" 之類的訊息。此時應提示使用者「本書已成功歸還，請取回書本」，而不是讓
# 櫃台以為「圖書館系統拒絕」造成的一般失敗。
_LIBRARY_ALREADY_RETURNED_KEYWORDS = ('館藏資料異常', '已歸還', '不在借出')


def _is_already_returned_reject(*messages):
    """判斷 SIP2 checkin 失敗訊息是否屬於『書已歸還』類別。"""
    for raw in messages:
        if not raw:
            continue
        s = str(raw)
        for kw in _LIBRARY_ALREADY_RETURNED_KEYWORDS:
            if kw in s:
                return True
    return False


def _is_machine_error_state_0(resp):
    """判斷機器控制器回應是否包含 'error state 0'（機器卡在錯誤狀態）。"""
    if resp is None:
        return False
    try:
        return "error state 0" in str(resp).lower()
    except Exception:
        return False


# 條碼開頭代表 CD 光碟附件的前綴；V / VH / D 各自獨立分類，不混為一談。
# 比對順序很重要：較長的 VH 必須先比對，否則會被 V 吃掉，導致無法區分 VH 與單純的 V。
_DISC_MEDIA_PREFIXES = ('VH', 'V', 'D')


def _disc_media_kind(book_id):
    """回傳條碼匹配到的光碟附件前綴（'VH' / 'V' / 'D'）；不是光碟則回傳 None。"""
    if not book_id:
        return None
    s = str(book_id).strip().upper()
    for prefix in _DISC_MEDIA_PREFIXES:
        if s.startswith(prefix):
            return prefix
    return None


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
    """檢查系統狀態（包含圖書館與機器通訊）

    從 shared._state_cache 讀取，不再每次都打 serial / SIP2。背景 thread
    （shared._state_poll_thread）每秒更新一次 cache，所以這個 endpoint 永遠
    在 10 ms 內回應，且不會被還書中的指令拖累。
    """
    cached = shared.get_cached_status()
    return jsonify({
        "suspended": cached.get('suspended', False),
        "limit": cached.get('limit', shared.MAX_RETURN_LIMIT),
        "bin_counts": cached.get('bin_counts', {}),
        "library_ok": cached.get('library_ok', False),
        "machine_ok": cached.get('machine_ok', False),
        "machine_state": cached.get('machine_state', 'unknown'),
        "homing_in_progress": cached.get('homing_in_progress', False),
        "is_homed": cached.get('is_homed', False),
        "machine_debug": cached.get('machine_debug', {"ser_open": False, "type": None}),
        "last_updated": cached.get('last_updated', 0.0),
    })


@api_bp.route('/homepage/book_check')
def homepage_book_check():
    """首頁閒置期間偵測投書口是否有殘留書。

    用途：使用者剛開啟首頁時，若機器處於異常狀態（state≠homed/sleeping）
    或投書口偵測到書（前一輪 return 沒結束乾淨），前端應該擋下掃描流程
    並顯示「請聯絡管理員」。

    實作：
      - 不修改背景 poll thread（避免增加 serial 流量）
      - 前端 entry/idle 時手動 poll 本 endpoint（建議 10s 間隔）
      - bookok 是 query 指令會 block-acquire machine.lock；為避免拖到動作
        進行中的請求，這裡先 try-acquire 0.5s，搶不到就回 ok=null（不下結論）

    回傳：
      {
        "ok": bool|null,              # True=可掃描；False=異常需要管理員；null=未知（不下結論）
        "book_present": bool|null,    # True=偵測到書；null=未查詢或查詢失敗
        "machine_state": str,         # 'homed' / 'opened' / 'closed' / ...
        "reason": str|null,           # 'book_in_chute' / 'abnormal_state' / 'machine_busy' / 'query_failed'
      }
    """
    cached = shared.get_cached_status()
    machine_state = cached.get('machine_state', 'unknown')

    # 異常 state 不必查 bookok；直接判定 abnormal_state
    if machine_state not in ('homed', 'sleeping'):
        return jsonify({
            "ok": False,
            "book_present": None,
            "machine_state": machine_state,
            "reason": "abnormal_state",
        })

    machine = getattr(shared, 'machine', None)
    if machine is None or not hasattr(machine, 'check_book_status'):
        return jsonify({
            "ok": None,
            "book_present": None,
            "machine_state": machine_state,
            "reason": "machine_unavailable",
        })

    lock = getattr(machine, 'lock', None)
    if lock is not None and not lock.acquire(timeout=0.5):
        return jsonify({
            "ok": None,
            "book_present": None,
            "machine_state": machine_state,
            "reason": "machine_busy",
        })
    try:
        try:
            book_present = bool(machine.check_book_status())
        except Exception as e:
            logger.warning(f"homepage_book_check: bookok query failed: {e}")
            return jsonify({
                "ok": None,
                "book_present": None,
                "machine_state": machine_state,
                "reason": "query_failed",
            })
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass

    if book_present:
        return jsonify({
            "ok": False,
            "book_present": True,
            "machine_state": machine_state,
            "reason": "book_in_chute",
        })
    return jsonify({
        "ok": True,
        "book_present": False,
        "machine_state": machine_state,
        "reason": None,
    })


@api_bp.route('/events')
def events():
    """Server-Sent Events endpoint — 狀態變化時主動推給前端。

    背景 polling thread（shared._state_poll_thread）偵測到 cache 有意義的變化
    時，會把新快照丟到每個訂閱者的 queue。本 handler 阻塞讀那個 queue 並轉送
    成 SSE 訊息。15 秒沒事件就送 keepalive 防止反向代理或瀏覽器收掉連線。

    用法：前端 `new EventSource('/api/events')`，監聽 message 事件解析 JSON。
    """
    def gen():
        q = shared.subscribe_state()
        try:
            # 連上來就先送一份目前的 cache，前端不需要等到下一次變化
            initial = shared.get_cached_status()
            yield f"data: {json.dumps(initial)}\n\n"

            while True:
                try:
                    snapshot = q.get(timeout=15)
                    yield f"data: {json.dumps(snapshot)}\n\n"
                except queue.Empty:
                    # SSE keepalive 註解行（以 ':' 開頭），不會觸發 onmessage
                    yield ": keepalive\n\n"
        except GeneratorExit:
            # 連線斷掉時 Flask 會丟這個 exception，正常路徑
            pass
        except Exception as e:
            logger.error(f"SSE generator error: {e}")
        finally:
            shared.unsubscribe_state(q)

    headers = {
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',   # 給 nginx：不要 buffer SSE
        'Connection': 'keep-alive',
    }
    return Response(stream_with_context(gen()),
                    mimetype='text/event-stream',
                    headers=headers)


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

    # V / VH / D 開頭的條碼一律視為 CD 光碟附件，本機不收，掃描階段就攔下，
    # 不必白白送 SIP2 查詢。沿用 ATTACHMENT_NOT_ACCEPTED code 讓前端可以重用既有 UI。
    disc_kind = _disc_media_kind(book_id)
    if disc_kind:
        logger.info(f"Rejecting disc-media barcode at scan time ({disc_kind}-prefix): {book_id}")
        return jsonify({
            "success": False,
            "code": "ATTACHMENT_NOT_ACCEPTED",
            "message": "本機不接受 CD 光碟還書，請洽櫃檯。"
        }), 400

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

@api_bp.route('/return_progress', methods=['GET'])
def get_return_progress():
    """提供前端輪詢還書流程目前 stage。

    回傳：
      { stage, detail, ts, target_bin, age_sec }

    stage 列表見 _return_progress 註解。
    """
    with _return_progress_lock:
        snap = dict(_return_progress)
    snap["age_sec"] = round(time.time() - snap["ts"], 2) if snap["ts"] else None
    return jsonify(snap)


@api_bp.route('/return', methods=['POST'])
def return_book():
    """確認還書 API"""
    _set_return_stage("pre_check", "檢查還書箱狀態")
    if shared.is_box_full():
        _set_return_stage("error", "還書箱已滿")
        return jsonify({"success": False, "message": "還書箱已滿，暫停服務", "code": "SERVICE_SUSPENDED"}), 503

    data = request.json or {}
    books_payload = data.get('books') or []  # [{book_id, location, target_bin}]
    book_ids = data.get('book_ids', [])
    attachment_only = data.get('attachment_only', False)
    attachment_barcode = data.get('attachment_barcode')
    
    if 'book_id' in data:
        book_ids = [data['book_id']]
    
    if not book_ids:
        _set_return_stage("error", "未選擇任何書籍")
        return jsonify({"success": False, "message": "未選擇任何書籍"}), 400

    if attachment_only:
        if len(book_ids) != 1:
            _set_return_stage("error", "附件歸還僅支援單本")
            return jsonify({"success": False, "message": "附件歸還僅支援單本處理"}), 400
        if not attachment_barcode:
            _set_return_stage("error", "缺少附件條碼")
            return jsonify({"success": False, "message": "缺少附件條碼 (attachment_barcode)"}), 400
        if attachment_barcode.strip().upper() != book_ids[0].strip().upper():
            _set_return_stage("error", "附件條碼與書籍不符")
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
            _set_return_stage("error", f"分類箱 {selected_bin} 已滿")
            return jsonify({
                "success": False,
                "message": f"分類箱{selected_bin}已滿，暫停服務",
                "code": "BIN_FULL",
                "data": {"target_bin": selected_bin}
            }), 503
    
    # 硬體還書流程控制：進入 /api/return 前，前端應該已經呼叫過
    # wake_and_home + hardware/open，所以 state 必為 3 (OPENED)。
    # 若不是 state 3，過去的設計會「盲送 homing」想當 fallback，但 homing 後
    # state=2，接著 close 會回 error state 2 並失敗；流程仍然繼續到 checkin，
    # 結果是「SIP2 已歸還、box_inventory 已寫入，但書本根本沒進機器」的資料錯位。
    # 改成嚴格拒絕：state≠3 直接 MACHINE_NOT_OPEN，提示前端重新開門。
    try:
        status = machine.get_status()
        s_status = str(status).strip()

        if _is_machine_error_state_0(s_status):
            logger.error(f"Machine reported error state 0 on pre-close status check: {status}")
            _set_return_stage("error", "機器錯誤狀態，請洽櫃台")
            return jsonify({
                "success": False,
                "code": "MACHINE_ERROR_STATE_0",
                "message": "機器發生錯誤，請洽櫃台處理。"
            }), 503

        is_state_3 = False
        if s_status.isdigit() and int(s_status) == getattr(machine, 'STATE_OPENED', 3):
            is_state_3 = True
        elif "opened" in s_status.lower() or "error state 3" in s_status.lower():
            is_state_3 = True

        if not is_state_3:
            logger.error(
                f"/api/return rejected: machine state '{status}' is not OPENED (3). "
                f"Frontend must call /api/hardware/open before /api/return."
            )
            _set_return_stage("error", "投書口未開啟，請重新開始還書流程")
            return jsonify({
                "success": False,
                "code": "MACHINE_NOT_OPEN",
                "message": "投書口未開啟，請重新開始還書流程。",
                "machine_state": s_status,
            }), 409

        logger.info(f"Machine is in state {status} (OPENED). Proceeding to close.")

    except Exception as e:
        logger.error(f"Pre-close status check failed: {e}")
        _set_return_stage("error", "無法讀取機器狀態")
        return jsonify({
            "success": False,
            "code": "MACHINE_STATUS_UNKNOWN",
            "message": "無法讀取機器狀態，請洽櫃台。",
            "detail": str(e),
        }), 503

    # 關門前預檢
    if shared.BOOK_CHECK_ENABLED:
        logger.info("Pre-check: checking book status before closing door...")
        if not machine.check_book_status():
            logger.info("Pre-check failed: Book not detected. Aborting close.")
            _set_return_stage("error", "未偵測到書籍")
            return jsonify({
                "success": False,
                "message": "未偵測到書籍，請確認書籍已放入並靠右對齊",
                "code": "BOOK_NOT_DETECTED"
            }), 400
    else:
        logger.info("Book check disabled by config. Skipping pre-check.")

    # 關閉投書口
    _set_return_stage("closing", "投書口關閉中")
    close_ok = machine.close_door()
    if not close_ok:
        # close 失敗（hardware glitch、error state）→ 此時尚未做 SIP2 checkin，
        # 也未寫入 box_inventory，可以安全地讓使用者取書並回首頁。
        logger.error("close_door failed; reopening to let user retrieve book")
        _set_return_stage("error", "投書口關閉失敗，請取回書本")

        def async_reopen_after_close_fail():
            try:
                time.sleep(0.1)
                machine.reopen_door()
            except Exception as e:
                logger.error(f"Async reopen after close failure failed: {e}")

        threading.Thread(target=async_reopen_after_close_fail, daemon=True).start()

        return jsonify({
            "success": False,
            "code": "MACHINE_CLOSE_FAILED",
            "message": "投書口關閉失敗，已重新開啟，請取回書本並洽櫃台。",
        }), 500
    _set_return_stage("closed", "投書口已關閉，檢查書籍是否放妥")
    time.sleep(2)

    # 關門後複檢
    if shared.BOOK_CHECK_ENABLED:
        if not machine.check_book_status():
            logger.info("Post-check failed: Book not detected in return box, triggering async reopen.")
            _set_return_stage("error", "關門後未偵測到書籍，重新開門")
            
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
        # 還書失敗時用來分類錯誤原因；預設 CHECKIN_FAILED（舊行為）。
        # 之後會升級為更精確的 code（LIBRARY_REJECT_ALREADY_RETURNED / LIBRARY_REJECT_OTHER /
        # SIP2_DISCONNECTED），以便前端對不同失敗顯示不同訊息。
        failure_code = "CHECKIN_FAILED"
        failure_message = "圖書館系統拒絕本次還書，請取回書本並洽詢櫃台。"

        # sort_book 警告：SIP2 已 commit 後若 put1/put2 失敗，無法 reverse SIP2，
        # 仍回 success 但帶 warning 給前端 / 管理員追查。先初始化為 None。
        sort_warning = None

        _set_return_stage("checkin", "連線圖書館系統還書")
        for b_id in book_ids:
            checkin_success = True
            reservation_notice = None
            if not attachment_only:
                # 檢查是否啟用真實還書功能
                config.reload_config()  # 重新載入配置
                if config.LIBRARY_CHECKIN_ENABLED and shared.sip2:
                    logger.info(f"Executing real checkin for book: {b_id}")
                    try:
                        checkin_result = shared.sip2.checkin_book(b_id)
                    except Exception as ex:
                        # SIP2 socket 在還書中途斷線等；視為連線異常而非「圖書館拒絕」
                        logger.error(f"SIP2 checkin raised for {b_id}: {ex}")
                        checkin_result = None

                    # 新版 checkin_book 會回傳 dict，內含 success 與 AF/AG 訊息；
                    # 若為舊版 bool 也能相容處理；None 則代表 SIP2 沒有回應。
                    af_msg = None
                    ag_msg = None
                    error_msg = None
                    if checkin_result is None:
                        # 完全沒有回應 → 視為 SIP2 連線異常
                        checkin_success = False
                        failure_code = "SIP2_DISCONNECTED"
                        failure_message = "圖書館系統暫時無法連線，請稍後再試或洽櫃台。"
                    elif isinstance(checkin_result, dict):
                        checkin_success = bool(checkin_result.get('success'))
                        af_msg = checkin_result.get('af_message')
                        ag_msg = checkin_result.get('ag_message')
                        error_msg = checkin_result.get('error_message')
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
                        # 抽取預約通知（會寫進 box_inventory.reservation_notice 給後台顯示）
                        reservation_notice = _extract_reservation_notice(af_msg, ag_msg)
                        if reservation_notice:
                            logger.info(f"Book {b_id} has reservation: {reservation_notice}")
                    else:
                        logger.warning(f"Real checkin failed for book: {b_id} (af={af_msg!r} ag={ag_msg!r} err={error_msg!r})")
                        all_checkin_success = False
                        failed_books.append(b_id)
                        # 已是 SIP2_DISCONNECTED 的情況不再覆寫
                        if failure_code != "SIP2_DISCONNECTED":
                            if _is_already_returned_reject(af_msg, ag_msg, error_msg):
                                failure_code = "LIBRARY_REJECT_ALREADY_RETURNED"
                                failure_message = "本書系統紀錄已歸還，請取回書本後洽櫃台確認。"
                            else:
                                # AF/AG/error 都不是「已歸還」類別 → 一般圖書館拒絕
                                failure_code = "LIBRARY_REJECT_OTHER"
                                # 優先使用 SIP2 server 給的具體 error_message，沒有再 fallback
                                # 到 AF / AG / 預設訊息。
                                detail = error_msg or af_msg or ag_msg
                                if detail:
                                    failure_message = f"圖書館系統拒絕本次還書：{detail}，請取回書本並洽櫃台。"
                                else:
                                    failure_message = "圖書館系統拒絕本次還書，請取回書本並洽詢櫃台。"
                        break
                elif shared.sip2:
                    logger.info(f"Real checkin disabled - simulating checkin for book: {b_id}")
                    checkin_success = True  # 模擬成功
                else:
                    logger.warning(f"No SIP2 connection available for book: {b_id}")
                    checkin_success = False
                    all_checkin_success = False
                    failed_books.append(b_id)
                    failure_code = "SIP2_DISCONNECTED"
                    failure_message = "圖書館系統暫時無法連線，請稍後再試或洽櫃台。"
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

                cursor.execute(
                    'INSERT INTO box_inventory (book_id, title, image_url, return_time, target_bin, reservation_notice) VALUES (?, ?, ?, ?, ?, ?)',
                    (b_id, title, image_url, current_time, target_bin, reservation_notice),
                )

                returned_books.append({
                    "book_id": b_id,
                    "title": title,
                    "return_time": current_time,
                    "target_bin": target_bin,
                    "reservation_notice": reservation_notice,
                })
        # 若有任何一本書還書指令失敗，則：
        # 1) 回滾本次交易，不記錄到本地 box_inventory
        # 2) 重新開啟投書口，讓使用者取回書本
        # 3) 回傳錯誤訊息與分類 code，提醒使用者洽詢櫃台
        if not all_checkin_success:
            conn.rollback()
            _set_return_stage("error", failure_message)
            try:
                logger.info(f"Checkin failed for books {failed_books} (code={failure_code}); reopening door to let user retrieve items.")

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
                "message": failure_message,
                "code": failure_code,
                "failed_books": failed_books
            }), 400
        
        conn.commit()
        
        # 硬體分類：若已選定分櫃則使用，否則預設為館藏位置推導
        try:
            # 若本次沒有任何成功的還書紀錄，則不啟動分類動作，避免書未成功入帳就被送入箱內
            if not returned_books:
                logger.warning("No books were successfully checked in; skipping sort_book.")
                _set_return_stage("error", "沒有成功完成還書的書籍")
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
            bin_to_use = int(selected_bin or 1)
            _set_return_stage("sorting", f"重新上磁並送入分類箱 {bin_to_use}", target_bin=bin_to_use)
            sort_ok = machine.sort_book(bin_to_use)
            if not sort_ok:
                # sort 失敗時 SIP2 checkin 已經 commit，無法 reverse。
                # 書本物理上停在 state 4 (投書口關閉、書在裡面、未進分類箱)，
                # 此時 box_inventory 已寫入（DB 還是承認書在還書箱）但 target_bin
                # 不一定正確。讓流程繼續回傳 success，並在 response 帶上警告碼，
                # 由前端顯示「請洽櫃台」訊息，並由後台 log 給管理員追查。
                logger.error(
                    f"sort_book failed for bin {bin_to_use}; SIP2 already committed. "
                    f"Books={[b.get('book_id') for b in returned_books]}. "
                    f"Manual intervention required."
                )
                sort_warning = {
                    "code": "SORT_FAILED",
                    "message": f"書籍已歸還，但機器分類動作未確認，請洽櫃台確認分類箱 {bin_to_use}。",
                }
        except Exception as e:
            logger.error(f"sort_book raised exception: {e}", exc_info=True)
            sort_warning = {
                "code": "SORT_FAILED",
                "message": "書籍已歸還，但機器分類動作發生例外，請洽櫃台。",
            }

    except Exception as e:
        conn.rollback()
        logger.error(f"Return error: {e}")
        _set_return_stage("error", f"系統錯誤：{e}")
        return jsonify({"success": False, "message": "系統錯誤"}), 500
    finally:
        conn.close()

    _set_return_stage("done", f"完成歸還 {len(returned_books)} 本")

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
        "notice_type": selected_notice[1] if selected_notice else None,
        # sort_warning：SIP2 已歸還但機器分類失敗時帶警告碼，前端可彈窗提示「請洽櫃台」。
        "sort_warning": sort_warning,
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

        if is_completion("cancel", resp):
            return jsonify({
                "success": True,
                "message": "已取消還書",
                "response": str(resp)
            })
        if is_busy_response(resp):
            return jsonify({
                "success": False,
                "message": "機器忙碌中，取消未執行",
                "response": str(resp)
            }), 409
        if is_error_state_response(resp):
            return jsonify({
                "success": False,
                "message": f"取消失敗：{resp}",
                "response": str(resp)
            }), 400
        return jsonify({
            "success": False,
            "message": "取消指令未確認完成",
            "response": str(resp)
        }), 500
            
    except Exception as e:
        logger.error(f"Cancel return error: {e}")
        return jsonify({"success": False, "message": "取消操作失敗", "detail": str(e)}), 500
