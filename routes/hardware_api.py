from flask import Blueprint, jsonify
import logging
import shared
import time
import threading

hardware_api_bp = Blueprint('hardware_api', __name__)
logger = logging.getLogger('hardware_api')

@hardware_api_bp.route('/open', methods=['POST'])
def open_hardware_door():
    """單獨開啟投書口 API"""
    if shared.machine.open_door():
        return jsonify({"success": True, "message": "投書口已開啟"})
    return jsonify({"success": False, "message": "開啟失敗"}), 500

@hardware_api_bp.route('/close', methods=['POST'])
def close_hardware_door():
    """單獨關閉投書口 API"""
    try:
        # 使用 cancel 指令替代直接 close，因為在故障/還書流程中 cancel 更安全
        resp = shared.machine._send_command('cancel')
        ok = False
        try:
            s = str(resp).lower()
            if 'ack' in s or 'cancel' in s or 'canceled' in s or 'cancelled' in s:
                ok = True
        except Exception:
            pass

        if ok:
            return jsonify({"success": True, "message": "已發送 cancel 指令 (視為關門/中止)", "response": resp})
        else:
            # 若沒有明確 ack，也回傳成功但附上原始回應以供除錯
            return jsonify({"success": True, "message": "已發送 cancel 指令 (無明確 ACK)", "response": resp})
    except Exception as e:
        logger.error(f"close_hardware_door error: {e}")
        return jsonify({"success": False, "message": "關閉失敗", "detail": str(e)}), 500

@hardware_api_bp.route('/wake', methods=['POST'])
def wake_hardware():
    """喚醒機器"""
    try:
        if hasattr(shared.machine, 'wake_up'):
            shared.machine.wake_up()
            return jsonify({"success": True, "message": "已向機器發出喚醒指令"})
        else:
            return jsonify({"success": False, "message": "機器不支援喚醒操作"}), 501
    except Exception as e:
        logger.error(f"wake_hardware error: {e}")
        return jsonify({"success": False, "message": "喚醒失敗"}), 500

@hardware_api_bp.route('/wake_and_home', methods=['POST'])
def wake_and_home():
    """初始化/回原點操作：僅在必要時或使用者手動要求時執行 homing。"""
    machine = shared.machine
    try:
        # 0. 預檢：若機器已在原點 (State 2)，則不需重複執行 Homing
        try:
            status_resp = machine.get_status()
            s = str(status_resp).strip()
            # 檢查是否為狀態 2 (HOMED)
            if s.isdigit() and int(s) == getattr(machine, 'STATE_HOMED', 2):
                 logger.info("wake_and_home: Machine is already HOMED (state 2). Skipping hardware action.")
                 # 確保內部狀態同步
                 machine.is_homed = True
                 machine.homing_in_progress = False
                 machine.is_sleeping = False
                 return jsonify({"success": True, "message": "機器已在原點 (Skipped)", "response": status_resp})
        except Exception as e:
            logger.warning(f"wake_and_home pre-check failed: {e}")

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
        
        if hasattr(machine, 'is_homed'):
            machine.is_homed = ok

        if ok:
            return jsonify({"success": True, "message": "已執行回原點 (初始化)", "response": resp})
        else:
            logger.warning(f"wake_and_home: homing response inconclusive, resp={resp}")
            return jsonify({"success": True, "message": "回原點指令已發送", "response": resp})

    except Exception as e:
        logger.error(f"wake_and_home error: {e}")
        return jsonify({"success": False, "message": "操作失敗", "detail": str(e)}), 500


@hardware_api_bp.route('/reset', methods=['POST'])
def reset_hardware():
    """重置序列：用於書本卡住時的快速重置（reopen -> close -> homing）。"""
    machine = shared.machine
    logger.info("reset_hardware: Reset sequence triggered")
    responses = {}
    lock = getattr(machine, 'lock', None)
    acquired = False
    try:
        if lock:
            acquired = lock.acquire(timeout=5)
        # 標記為 action 中，避免其他指令干擾
        try:
            machine.action_in_progress = True
            machine.current_action_code = 99
        except Exception:
            pass

        # 0) 嘗試 cancel（如果是還書流程造成的卡住，先送 cancel 可能比強制 close 更安全）
        cancel_ok = False
        try:
            r = machine._send_command('cancel')
            responses['cancel'] = r
            try:
                s = str(r).lower()
                if any(k in s for k in ('ack', 'cancel', 'cancelled', 'canceled')):
                    cancel_ok = True
                    responses['cancel_ok'] = True
            except Exception:
                pass
        except Exception as e:
            responses['cancel_error'] = str(e)

        # 如果 cancel 成功（回傳 ack 或取消字樣），則視為已中止還書流程，不需再做機械重置
        if cancel_ok:
            try:
                machine.action_in_progress = False
                machine.current_action_code = None
            except Exception:
                pass
            return jsonify({"success": True, "message": "Cancel succeeded; mechanical reset skipped", "responses": responses})

        # 1) 嘗試 reopen（若門已卡，可嘗試重新開啟）
        try:
            r = machine._send_command('reopen')
            responses['reopen'] = r
        except Exception as e:
            responses['reopen_error'] = str(e)
        time.sleep(0.5)

        # 2) 嘗試 close
        try:
            r = machine._send_command('close')
            responses['close'] = r
        except Exception as e:
            responses['close_error'] = str(e)
        time.sleep(0.5)

        # 3) 執行 homing
        try:
            r = machine._send_command('homing')
            responses['homing'] = r
            # 解析 homing 成功與否
            ok = False
            try:
                s = str(r).lower()
                if 'homed' in s or 'ack' in s:
                    ok = True
            except Exception:
                ok = False
            machine.is_homed = ok
        except Exception as e:
            responses['homing_error'] = str(e)

        # 清除 action flag
        try:
            machine.action_in_progress = False
            machine.current_action_code = None
        except Exception:
            pass

        return jsonify({"success": True, "message": "Reset sequence executed", "responses": responses})
    except Exception as e:
        logger.error(f"reset_hardware error: {e}")
        try:
            machine.action_in_progress = False
            machine.current_action_code = None
        except Exception:
            pass
        return jsonify({"success": False, "message": "Reset failed", "detail": str(e)}), 500
    finally:
        if lock and acquired:
            try:
                lock.release()
            except Exception:
                pass