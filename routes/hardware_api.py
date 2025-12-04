from flask import Blueprint, jsonify
import logging
import shared

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
    if shared.machine.close_door():
        return jsonify({"success": True, "message": "投書口已關閉"})
    return jsonify({"success": False, "message": "關閉失敗"}), 500

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