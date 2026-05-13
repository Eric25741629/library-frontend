from flask import Blueprint, jsonify
import logging
import shared
import time
import threading
from machine_controller import is_completion, is_busy_response, is_error_state_response

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
    """單獨關閉投書口 API（透過 cancel 中止還書流程）"""
    try:
        resp = shared.machine._send_command('cancel')
        if is_completion("cancel", resp):
            return jsonify({"success": True, "message": "已取消還書並回到 ready", "response": resp})
        if is_busy_response(resp):
            return jsonify({"success": False, "message": "機器忙碌中，請稍後再試", "response": resp}), 409
        if is_error_state_response(resp):
            return jsonify({"success": False, "message": f"取消失敗：{resp}", "response": resp}), 400
        return jsonify({"success": False, "message": "取消指令未確認完成", "response": resp}), 500
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
    """初始化/回原點：若機器已在 state 2 則跳過 homing。"""
    machine = shared.machine
    try:
        try:
            status_resp = machine.get_status()
            s = str(status_resp).strip()
            if s.isdigit() and int(s) == getattr(machine, 'STATE_HOMED', 2):
                logger.info("wake_and_home: already HOMED, skipping")
                machine.is_homed = True
                machine.homing_in_progress = False
                machine.is_sleeping = False
                return jsonify({"success": True, "message": "機器已在原點 (Skipped)", "response": status_resp})
        except Exception as e:
            logger.warning(f"wake_and_home pre-check failed: {e}")

        if hasattr(machine, 'wake_up'):
            machine.wake_up()
        else:
            machine._send_command("dep1")

        try:
            resp = machine._send_command("homing")
        except Exception as e:
            logger.warning(f"wake_and_home: homing command failed: {e}")
            return jsonify({"success": False, "message": "homing 指令發送失敗", "detail": str(e)}), 500

        ok = is_completion("homing", resp)
        if hasattr(machine, 'is_homed'):
            machine.is_homed = ok

        if ok:
            return jsonify({"success": True, "message": "已執行回原點 (初始化)", "response": resp})
        if is_busy_response(resp):
            return jsonify({"success": False, "message": "機器忙碌中，homing 未執行", "response": resp}), 409
        logger.warning(f"wake_and_home: homing response inconclusive, resp={resp}")
        return jsonify({"success": False, "message": "回原點未完成", "response": resp}), 500

    except Exception as e:
        logger.error(f"wake_and_home error: {e}")
        return jsonify({"success": False, "message": "操作失敗", "detail": str(e)}), 500


@hardware_api_bp.route('/reset', methods=['POST'])
def reset_hardware():
    """重置序列：cancel → (若失敗) reopen → close → homing。"""
    machine = shared.machine
    logger.info("reset_hardware: Reset sequence triggered")
    responses = {}
    lock = getattr(machine, 'lock', None)
    acquired = False
    try:
        if lock:
            acquired = lock.acquire(timeout=5)
        try:
            machine.action_in_progress = True
            machine.current_action_code = 99
        except Exception:
            pass

        # 0) cancel：若成功直接返回
        cancel_ok = False
        try:
            r = machine._send_command('cancel')
            responses['cancel'] = r
            cancel_ok = is_completion("cancel", r)
            responses['cancel_ok'] = cancel_ok
        except Exception as e:
            responses['cancel_error'] = str(e)

        if cancel_ok:
            try:
                machine.action_in_progress = False
                machine.current_action_code = None
            except Exception:
                pass
            return jsonify({"success": True, "message": "Cancel succeeded; mechanical reset skipped", "responses": responses})

        # 1) reopen
        try:
            r = machine._send_command('reopen')
            responses['reopen'] = r
            responses['reopen_ok'] = is_completion("reopen", r)
        except Exception as e:
            responses['reopen_error'] = str(e)
        time.sleep(0.5)

        # 2) close
        try:
            r = machine._send_command('close')
            responses['close'] = r
            responses['close_ok'] = is_completion("close", r)
        except Exception as e:
            responses['close_error'] = str(e)
        time.sleep(0.5)

        # 3) homing
        homing_ok = False
        try:
            r = machine._send_command('homing')
            responses['homing'] = r
            homing_ok = is_completion("homing", r)
            responses['homing_ok'] = homing_ok
            machine.is_homed = homing_ok
        except Exception as e:
            responses['homing_error'] = str(e)

        try:
            machine.action_in_progress = False
            machine.current_action_code = None
        except Exception:
            pass

        return jsonify({
            "success": bool(homing_ok),
            "message": "Reset sequence executed" if homing_ok else "Reset 完成但 homing 未確認",
            "responses": responses,
        })
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
