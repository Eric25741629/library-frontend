import time
import threading
import logging
import json
from pathlib import Path

import serial

MACHINE_CONFIG_FILE = Path('machine_config.json')


# 規格 2025-12-07：純查詢類指令（立即回單行結果，不會有後續完成訊息）
QUERY_COMMANDS = {
    "state", "act", "act2", "bookok",
    "opbm1", "opbm2", "opbm3", "opbm4",
    "opwd1", "opwd2", "opbhdn", "opbhup",
    "readskip", "help",
}

# 動作類指令：先回 ack（或 ack-busy / error state），動作完成後再回完成訊息
ACTION_COMPLETION_PATTERNS = {
    "dep1": ["device power on"],          # device power on / device power on, not homed
    "dep0": ["device power of"],          # device power of (規格拼字)
    "homing": ["homed"],                  # Homed
    "open": ["opened"],
    "reopen": ["opened"],
    "close": ["closed"],
    "put1": ["been put1"],
    "put2": ["been put2"],
    # cancel 兩階段已實測（2026-05-13 /dev/uno）：state 3 下送 cancel →
    # 0.22s ack → 12.16s canceled → state 2。timeout 必須足夠覆蓋實測 ~12s。
    "cancel": ["canceled", "cancelled"],
    "reset": ["reseted"],
    "bmto1": ["moved to pos1"],
    "bmto2": ["moved to pos2"],
    "bmto3": ["moved to pos3"],
    "bmto4": ["moved to pos4"],
    "skip": ["skip book check"],
    "resume": ["resume book check"],
}

# 每個動作指令容許完成訊息的最大等待秒數
ACTION_TIMEOUTS = {
    "dep1": 8, "dep0": 8,
    "homing": 40,
    "open": 20, "close": 20, "reopen": 20,
    "put1": 120, "put2": 120,
    # cancel：實測 state 3 下 ack→canceled 約 12s（2026-05-13 /dev/uno），給 2x headroom
    "cancel": 30, "reset": 40,
    "bmto1": 30, "bmto2": 30, "bmto3": 30, "bmto4": 30,
    "skip": 3, "resume": 3,
}

# Phase 1: 等 ack / ack-busy / error state 的最長時間
PHASE1_ACK_TIMEOUT = 2.0


def is_busy_response(resp):
    """回應是否表示機構忙碌中（指令未執行）。"""
    if resp is None:
        return False
    s = str(resp).strip().lower()
    return s == "ack-busy" or s.startswith("ack-busy")


def is_error_state_response(resp):
    """回應是否為 'error state X'（指令在錯誤狀態下被拒絕）。"""
    if resp is None:
        return False
    return str(resp).strip().lower().startswith("error state")


def is_completion(cmd, resp):
    """檢查 resp 是否為 cmd 對應的完成訊息。

    這是給上層 caller 用的標準判斷，**不要再用 `"ack" in resp` 的字串比對**。
    """
    if resp is None:
        return False
    s = str(resp).strip().lower()
    if not s or s == "ack" or is_busy_response(s) or is_error_state_response(s):
        return False
    patterns = ACTION_COMPLETION_PATTERNS.get(cmd, [])
    for p in patterns:
        if p.lower() in s:
            return True
    return False


class MachineController:
    # 暴露為 class 屬性，方便外部 import
    QUERY_COMMANDS = QUERY_COMMANDS
    ACTION_COMPLETION_PATTERNS = ACTION_COMPLETION_PATTERNS
    ACTION_TIMEOUTS = ACTION_TIMEOUTS

    def __init__(self, port='/dev/uno', baudrate=19200, simulate=False):
        """
        simulate: 若為 True，則不開啟序列埠，方便遠端或 CI 在沒有實體設備時測試。

        baudrate 預設 19200，與規格 2025-12-07 一致。
        """
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.simulate = bool(simulate)
        self.logger = logging.getLogger('machine')
        # RLock 允許同一執行緒重入
        self.lock = threading.RLock()

        # 狀態常數（規格 §3）
        self.STATE_NOT_INIT = 0
        self.STATE_POWER_ON_NOT_HOMED = 1
        self.STATE_HOMED = 2
        self.STATE_OPENED = 3
        self.STATE_CLOSED = 4
        self.STATE_SLEEPING = 5

        self.last_action_time = time.time()
        self.last_cmd = None
        self.MIN_COMMAND_INTERVAL = 2
        self.last_serial_time = time.time() - self.MIN_COMMAND_INTERVAL
        self.IDLE_TIMEOUT = 15 * 60
        self.is_sleeping = True
        self.homing_in_progress = False
        self.is_homed = False
        self.action_in_progress = False
        self.current_action_code = None
        self.initialized = False
        self.HOMING_WAIT_TIMEOUT = 30

        self.pause_query_until = 0

        if not self.simulate:
            try:
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=2
                )
                self.logger.info(f"Connected to machine on {self.port} @ {self.baudrate}")
            except Exception as e:
                self.logger.error(f"Failed to connect to machine: {e}")
        else:
            self.ser = None
            self.logger.info("MachineController running in simulate mode; serial disabled")

    # ─────────────────────────────────────────────────────────────────
    # 核心：兩階段協定的 _send_command
    # ─────────────────────────────────────────────────────────────────

    def _send_command(self, cmd):
        """依規格 2025-12-07 的兩階段協定發送指令。

        Phase 1（短 timeout PHASE1_ACK_TIMEOUT）：等待
          - "ack"          → 進入 Phase 2 等完成
          - "ack-busy"     → 機構忙碌，回傳 "ack-busy"
          - "error state X"→ 狀態錯誤，回傳該字串
          - 完成訊息       → 部分快指令可能跳過 ack，直接給完成訊息

        Phase 2（依指令各自的 ACTION_TIMEOUTS）：等待完成訊息
          - 完成訊息       → 回傳該字串
          - "error state X"→ 回傳該字串（例如機構在動作途中改變狀態）
          - timeout        → 回傳 "ack-no-completion"

        查詢類指令（QUERY_COMMANDS）：只讀單行非空回應後立即回傳。

        若處於 simulate 模式或序列埠未開啟，會回傳描述性錯誤字串。
        """
        lock = getattr(self, 'lock', None)

        # 內部 action_in_progress 旗標：仍在執行動作時，僅放行查詢指令
        if getattr(self, 'action_in_progress', False) and cmd not in QUERY_COMMANDS:
            self.logger.info(
                f"MachineCommand BUSY (internal flag): {cmd} -> ack-busy "
                f"(action={getattr(self,'current_action_code',None)})"
            )
            return "ack-busy"

        # 鎖：state 使用非阻塞 0.1s timeout 避免拖住監控執行緒。
        # 拿不到鎖代表正有其他動作 (open/close/cancel/…) 持鎖；硬體不一定卡，
        # 只是這次不該打擾。回傳 "busy" 而非空字串，讓 watchdog 的「空回應」
        # 計數器能區分「機器卡死」與「動作進行中」。
        if cmd == "state":
            if lock and not lock.acquire(timeout=0.1):
                return "busy"
        else:
            if lock:
                lock.acquire()

        try:
            # state 查詢若在 pause_query_until 期間直接跳過
            if cmd == "state":
                pq = getattr(self, 'pause_query_until', 0)
                if time.time() < pq:
                    return "time error"

            # 指令間隔保護（state 例外，狀態查詢不該被卡）
            if cmd != "state":
                try:
                    elapsed = time.time() - getattr(self, 'last_serial_time', 0)
                except Exception:
                    elapsed = self.MIN_COMMAND_INTERVAL
                if elapsed < self.MIN_COMMAND_INTERVAL:
                    wait = self.MIN_COMMAND_INTERVAL - elapsed
                    self.logger.debug(f"Waiting {wait:.2f}s before sending: {cmd}")
                    time.sleep(wait)

            # 控制指令觸發喚醒
            woke = False
            if cmd not in QUERY_COMMANDS:
                self.last_action_time = time.time()
                if self.is_sleeping and cmd != "dep1":
                    self.wake_up()
                    woke = True

            # 喚醒後，若有非同步 homing 仍在進行，等它完成才送下一個動作
            if woke and cmd != "dep1":
                wait_deadline = time.time() + self.HOMING_WAIT_TIMEOUT
                self.logger.info(
                    f"Waiting for homing before '{cmd}' (deadline {self.HOMING_WAIT_TIMEOUT}s)"
                )
                while time.time() < wait_deadline:
                    if not self.homing_in_progress and self.is_homed:
                        break
                    time.sleep(0.1)
                else:
                    self.logger.warning(
                        f"Homing did not finish within {self.HOMING_WAIT_TIMEOUT}s"
                    )
                    return "Error: Homing timeout"

            self.logger.info(f"MachineCommand SEND: {cmd}")

            if not self.ser or not getattr(self.ser, 'is_open', False):
                self.logger.error(f"MachineCommand ERROR: Serial not open for cmd={cmd}")
                return "Error: Serial not open"

            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass

            try:
                self.ser.write(f"{cmd}\n".encode())
            except Exception as e:
                self.logger.error(f"MachineCommand WRITE ERROR: {cmd} -> {e}")
                return f"Error: {e}"

            # 讀取
            if cmd in QUERY_COMMANDS:
                result = self._read_query_response(cmd)
            else:
                result = self._read_action_response(cmd)

            self.logger.info(f"MachineCommand RECV: {cmd} -> {result!r}")

            self.last_serial_time = time.time()
            self.last_cmd = cmd

            # 部分指令完成後，仍需短暫暫停狀態查詢，避免狀態查詢與機構穩定態錯位
            if cmd == "close":
                self.pause_query_until = time.time() + 5

            return result

        finally:
            if lock:
                try:
                    lock.release()
                except Exception:
                    pass

    def _read_query_response(self, cmd):
        """查詢指令：讀第一行非空回應後回傳。timeout 3 秒。"""
        timeout = 3
        start = time.time()
        while time.time() - start < timeout:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors='ignore').strip()
                    if not line:
                        continue
                    # 過濾掉裝置自身回送的指令 echo
                    if line.lower() == cmd.lower():
                        continue
                    self._apply_response_side_effects(line)
                    return line
            except Exception as e:
                self.logger.error(f"Query read error for {cmd}: {e}")
                return f"Error: {e}"
            time.sleep(0.05)
        return ""

    def _read_action_response(self, cmd):
        """動作指令：先等 ack/ack-busy/error state，再等完成訊息。"""
        completion_patterns = ACTION_COMPLETION_PATTERNS.get(cmd, [])
        action_timeout = ACTION_TIMEOUTS.get(cmd, 30)

        # ── Phase 1：等 ack / ack-busy / error state（或快指令直接給完成）
        phase1_deadline = time.time() + PHASE1_ACK_TIMEOUT
        got_ack = False
        last_line = ""

        while time.time() < phase1_deadline:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors='ignore').strip()
                    if not line:
                        continue
                    if line.lower() == cmd.lower():
                        continue
                    last_line = line
                    low = line.lower()
                    self.logger.debug(f"Phase1 RX: {line}")
                    self._apply_response_side_effects(line)

                    if "ack-busy" in low:
                        return "ack-busy"
                    if low.startswith("error state"):
                        return line
                    # 快指令可能直接給完成訊息
                    if self._matches_completion(low, completion_patterns):
                        return line
                    if low == "ack" or low.endswith(" ack"):
                        got_ack = True
                        break
                    # 其他行視為雜訊繼續讀
            except Exception as e:
                self.logger.error(f"Phase1 read error for {cmd}: {e}")
                return f"Error: {e}"
            time.sleep(0.05)

        if not got_ack:
            self.logger.warning(
                f"Phase1 timeout for {cmd}; last_line={last_line!r}"
            )
            # 若 Phase 1 整段沒收到任何東西，回 timeout；有收到非預期內容則保留
            return last_line or "timeout"

        # ── Phase 2：等完成訊息
        self.action_in_progress = True
        try:
            phase2_deadline = time.time() + action_timeout
            while time.time() < phase2_deadline:
                try:
                    if self.ser.in_waiting:
                        line = self.ser.readline().decode(errors='ignore').strip()
                        if not line:
                            continue
                        if line.lower() == cmd.lower():
                            continue
                        low = line.lower()
                        self.logger.debug(f"Phase2 RX: {line}")
                        self._apply_response_side_effects(line)

                        if self._matches_completion(low, completion_patterns):
                            return line
                        if low.startswith("error state"):
                            return line
                        # 動作代碼/雜訊忽略
                except Exception as e:
                    self.logger.error(f"Phase2 read error for {cmd}: {e}")
                    return f"Error: {e}"
                time.sleep(0.1)

            self.logger.warning(
                f"Phase2 timeout for {cmd} after ack (action_timeout={action_timeout}s)"
            )
            return "ack-no-completion"
        finally:
            # 完成或超時都清除 action 旗標
            self.action_in_progress = False
            self.current_action_code = None

    @staticmethod
    def _matches_completion(low_line, patterns):
        for p in patterns:
            if p.lower() in low_line:
                return True
        return False

    def _apply_response_side_effects(self, line):
        """解析回應字串並同步內部旗標。純更新函式，不拋出例外。"""
        if not line:
            return
        try:
            low = line.lower()

            # 純數字 state code
            if line.isdigit():
                code = int(line)
                if code == self.STATE_NOT_INIT:
                    self.is_sleeping = False
                    self.is_homed = False
                    self.homing_in_progress = False
                elif code == self.STATE_POWER_ON_NOT_HOMED:
                    self.is_sleeping = False
                    self.is_homed = False
                    self.homing_in_progress = False
                elif code == self.STATE_HOMED:
                    self.is_sleeping = False
                    self.is_homed = True
                    self.homing_in_progress = False
                    # 回到 homed 表示動作已結束
                    self.action_in_progress = False
                    self.current_action_code = None
                elif code == self.STATE_OPENED:
                    self.is_sleeping = False
                    self.is_homed = True
                    self.homing_in_progress = False
                elif code == self.STATE_CLOSED:
                    self.is_sleeping = False
                    self.is_homed = True
                elif code == self.STATE_SLEEPING:
                    self.is_sleeping = True
                    self.is_homed = False
                    self.homing_in_progress = False
                return

            # 文字回應
            if "device power on" in low:
                self.is_sleeping = False
            elif "device power of" in low or "device power off" in low:
                self.is_sleeping = True
                self.is_homed = False

            # 注意：dep1 在 state 0→1 的回應 "device power on, not homed" 也含 "homed"
            # 子字串，必須先排除「not homed」才能把它判定為已 homed。
            if "homed" in low and "not homed" not in low:
                self.is_homed = True
                self.homing_in_progress = False
                self.is_sleeping = False

            if "opened" in low:
                self.is_sleeping = False
                self.is_homed = True
                self.homing_in_progress = False

            if "closed" in low:
                self.is_sleeping = False

            if low.startswith("been put"):
                # put 完成 → 機構回到 homed
                self.action_in_progress = False
                self.current_action_code = None

            if "canceled" in low or "cancelled" in low:
                self.action_in_progress = False
                self.current_action_code = None

            if "reseted" in low:
                # reset 完成 → state 0
                self.is_homed = False
                self.is_sleeping = False
                self.action_in_progress = False
                self.current_action_code = None
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # 高階 API（給 routes 使用）
    # ─────────────────────────────────────────────────────────────────

    def init_machine(self):
        """初始化機器：若已在 STATE_HOMED 直接返回，否則執行 dep1 → homing。"""
        self.logger.info("Initializing machine: Checking status first...")

        try:
            status_resp = self.get_status()
            s = str(status_resp).strip()

            if s.isdigit() and int(s) == self.STATE_HOMED:
                self.logger.info(f"Machine already HOMED (state {s}). Init skipped.")
                self.is_homed = True
                self.initialized = True
                self.is_sleeping = False
                return True

            if s.isdigit() and int(s) == self.STATE_SLEEPING:
                self.logger.info("Machine SLEEPING. Waking (dep1)...")
                dep1_resp = self._send_command("dep1")
                self.is_sleeping = False
                # dep1 從 state 5 → state 2 (已 homed)；若回 "device power on" 即可
                if is_completion("dep1", dep1_resp):
                    # 確認 state 是否真為 2
                    time.sleep(0.5)
                    check = self.get_status()
                    if str(check).strip().isdigit() and int(str(check).strip()) == self.STATE_HOMED:
                        self.is_homed = True
                        self.initialized = True
                        return True
        except Exception as e:
            self.logger.warning(f"init_machine status check failed: {e}")

        self.logger.info("Performing full initialization (dep1 + homing).")
        dep1_resp = self._send_command("dep1")
        self.is_sleeping = False
        self.last_action_time = time.time()
        if is_busy_response(dep1_resp):
            self.logger.warning(f"dep1 returned busy: {dep1_resp}")

        homing_resp = self._send_command("homing")
        homed = is_completion("homing", homing_resp)
        if not homed:
            # 退而求其次：查 state
            try:
                final = self.get_status()
                if str(final).strip() == str(self.STATE_HOMED):
                    homed = True
            except Exception:
                pass

        self.is_homed = homed
        self.initialized = True
        return homed

    def wake_up(self):
        """喚醒：發 dep1 並等到完成訊息才回傳。

        依規格 §8 dep1：
        - state 5 → state 2 (Ready, homed)         回 "device power on"
        - state 0 → state 1 (Power on, not homed)  回 "device power on, not homed"

        正常路徑只會走 state 5→2；落到 state 1 時 caller 之後沒辦法直接送動作指令
        （_send_command 在 woke 後會等 is_homed=True 才放行）。為了讓 wake_up 的
        post-condition「回 True 即可直接送下一個動作」成立，這裡若發現是 state 1
        就同步補一次 homing。
        """
        if not self.is_sleeping:
            return True
        self.logger.info("Waking up machine (dep1)...")
        resp = self._send_command("dep1")
        if not is_completion("dep1", resp):
            self.logger.warning(f"wake_up: dep1 did not complete: {resp!r}")
            return False
        self.is_sleeping = False
        low = str(resp).lower()
        # 規格的「device power on, not homed」對應 state 1；單純「device power on」對應 state 2
        if "not homed" in low:
            self.logger.info("wake_up: woke to state 1 (not homed), performing homing")
            self._homing_sync()
        else:
            self.is_homed = True
        self.logger.info(f"wake_up done: is_homed={self.is_homed}")
        return self.is_homed

    def check_idle(self):
        """檢查閒置超時 → 依當前 state 決定 cancel / homing / 直接 dep0。

        規格 §8：dep0 從 state 2 → state 5（也支援 state 1 → 0）。其他 state 下 dep0
        會被機構拒絕，需要先把機構帶回合法狀態。

        策略：
        - state 2：直接 dep0（最常見，跳過 homing 節省 10-20s）
        - state 3：先 cancel（→ state 2）再 dep0
        - state 4：cancel 規格上不允許，改 homing（→ state 2）再 dep0
        - 其他/未知：homing 再 dep0
        """
        if self.is_sleeping or (time.time() - self.last_action_time) <= self.IDLE_TIMEOUT:
            return

        try:
            status = self.get_status()
            s = str(status).strip()
            code = int(s) if s.isdigit() else None
        except Exception as e:
            self.logger.warning(f"check_idle: state query failed: {e}")
            code = None

        if code == self.STATE_HOMED:
            self.logger.info("Idle timeout: state 2 (homed), going to dep0 directly")
        elif code == self.STATE_OPENED:
            self.logger.info("Idle timeout: state 3 (opened), sending cancel before dep0")
            resp = self._send_command("cancel")
            if not is_completion("cancel", resp):
                self.logger.warning(
                    f"check_idle cancel did not complete: {resp!r}, falling back to homing"
                )
                self._homing_sync()
        elif code == self.STATE_CLOSED:
            self.logger.info("Idle timeout: state 4 (closed), homing before dep0")
            self._homing_sync()
        else:
            self.logger.info(
                f"Idle timeout: state {code} (unexpected), homing before dep0"
            )
            self._homing_sync()

        self.logger.info("Sending dep0 (sleep)")
        resp = self._send_command("dep0")
        if is_completion("dep0", resp):
            self.is_sleeping = True
            self.is_homed = False
        else:
            self.logger.warning(f"dep0 did not complete: {resp!r}")

    def _homing_sync(self):
        """同步執行 homing 並更新 is_homed / homing_in_progress 旗標。"""
        self.homing_in_progress = True
        try:
            resp = self._send_command("homing")
            self.is_homed = is_completion("homing", resp)
        finally:
            self.homing_in_progress = False

    def open_door(self):
        """開啟投書口；只認 `opened` 完成訊息。"""
        if self.is_sleeping:
            self.logger.info("open_door: machine sleeping, waking first")
            self.wake_up()

        # 等 homing 完成
        if self.homing_in_progress or not self.is_homed:
            wait_deadline = time.time() + self.HOMING_WAIT_TIMEOUT
            self.logger.info(f"Waiting up to {self.HOMING_WAIT_TIMEOUT}s for homing")
            while time.time() < wait_deadline:
                if not self.homing_in_progress and self.is_homed:
                    break
                time.sleep(0.1)
            else:
                self.logger.warning("open_door aborted: homing did not complete in time")
                return False

        resp = self._send_command("open")

        if is_completion("open", resp):
            return True
        if is_busy_response(resp):
            self.logger.warning(f"open_door got ack-busy: {resp}")
            return False
        if is_error_state_response(resp):
            # state 4 → 應改 reopen；state 3 → 已開
            low = str(resp).lower()
            if "state 4" in low:
                self.logger.info("Machine in state 4, switching to reopen")
                return self.reopen_door()
            if "state 3" in low:
                return True
            self.logger.warning(f"open_door got error state: {resp}")
            return False
        self.logger.warning(f"open_door inconclusive resp: {resp!r}")
        return False

    def reopen_door(self):
        """重新開啟投書口 (書未放妥時)。只認 `opened`。"""
        resp = self._send_command("reopen")
        if is_completion("reopen", resp):
            return True
        if is_error_state_response(resp) and "state 3" in str(resp).lower():
            return True
        return False

    def close_door(self):
        """關閉投書口；只認 `closed`。"""
        resp = self._send_command("close")

        if is_completion("close", resp):
            return True
        if is_error_state_response(resp) and "state 4" in str(resp).lower():
            return True
        if is_busy_response(resp):
            self.logger.warning(f"close_door got ack-busy: {resp}")
            return False

        self.logger.warning(f"close_door failed: {resp!r}. Trying recovery.")
        if not self.simulate:
            with self.lock:
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
                except Exception:
                    pass
                try:
                    time.sleep(1)
                    self.ser = serial.Serial(
                        port=self.port,
                        baudrate=self.baudrate,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=2,
                    )
                    self.logger.info("Recovery: Serial reconnected.")
                    self._send_command("homing")
                except Exception as e:
                    self.logger.error(f"Recovery failed: {e}")
        return False

    def check_book_status(self):
        """檢查書籍是否放妥 (bookok)。"""
        resp = self._send_command("bookok")
        return "book is ok" in str(resp).lower()

    def sort_book(self, bin_number=1):
        """分類書籍 (put1 / put2)；只認 `been put{n}` 完成訊息。"""
        cmd = f"put{int(bin_number)}"
        resp = self._send_command(cmd)
        return is_completion(cmd, resp)

    def get_status(self):
        """讀 state 並同步內部旗標。"""
        return self._send_command("state")

    def is_door_open(self):
        """投書口是否開啟。"""
        try:
            resp = self.get_status()
            s = str(resp).strip()
            if s.isdigit():
                return int(s) == self.STATE_OPENED
            low = s.lower()
            return "opened" in low or "open" in low
        except Exception:
            return False

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def set_uart(self, port, baudrate):
        with self.lock:
            self.logger.info(f"Updating UART -> port={port}, baudrate={baudrate}")
            self.port = port
            self.baudrate = baudrate
            try:
                if self.ser and self.ser.is_open:
                    try:
                        self.ser.close()
                    except Exception as e:
                        self.logger.debug(f"Error closing serial: {e}")
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=2,
                )
                self.logger.info(f"Reconnected to {self.port} @ {self.baudrate}")
                return True
            except Exception as e:
                self.logger.error(f"Failed to reconnect: {e}")
                return False

    def test_connection(self, port, baudrate):
        temp_ser = None
        try:
            temp_ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=2,
            )
            if temp_ser.is_open:
                return True, "Port opened successfully"
            return False, "Failed to open port"
        except Exception as e:
            return False, str(e)
        finally:
            if temp_ser and temp_ser.is_open:
                try:
                    temp_ser.close()
                except Exception:
                    pass
