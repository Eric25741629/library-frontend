import time
import threading
import logging
import json
from pathlib import Path

# 嘗試匯入 serial，如果失敗則強制使用 mock mode
try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

# 可配置的機器設定檔（儲存 UART 設定）
MACHINE_CONFIG_FILE = Path('machine_config.json')

class MachineController:
    def __init__(self, port='/dev/ttyUSB0', baudrate=38400, mock_mode=False):
        self.port = port
        self.baudrate = baudrate
        self.mock_mode = mock_mode
        self.ser = None
        # Use dedicated 'machine' logger so machine logs go to machine.log
        self.logger = logging.getLogger('machine')
        # Threading lock to protect serial access across threads/requests
        self.lock = threading.Lock()
        
        # 如果沒有安裝 pyserial，強制進入 mock mode
        if not SERIAL_AVAILABLE:
            self.mock_mode = True
            self.logger.warning("pyserial not found, forcing MOCK mode")

        # 狀態常數
        self.STATE_NOT_INIT = 0
        self.STATE_POWER_ON_NOT_HOMED = 1
        self.STATE_HOMED = 2
        self.STATE_OPENED = 3
        self.STATE_CLOSED = 4 # 等待確認書籍
        
        # 閒置休眠設定
        self.last_action_time = time.time()
        self.MIN_COMMAND_INTERVAL = 2  # 最少兩秒才可發送下一個控制指令
        self.IDLE_TIMEOUT = 15 * 60 # 15 minutes
        self.is_sleeping = True # 假設初始為休眠，init時會喚醒

        if not self.mock_mode:
            try:
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=2
                )
                self.logger.info(f"Connected to machine on {self.port}")
            except Exception as e:
                self.logger.error(f"Failed to connect to machine: {e}")
                self.mock_mode = True # Fallback to mock mode
                self.logger.warning("Falling back to MOCK mode")

    def _send_command(self, cmd):
        """發送指令並等待回應 (ACK 或 完成訊息)

        - 在發送前確保與上一個控制指令的間隔至少為 self.MIN_COMMAND_INTERVAL（秒）。
        - 在發送與收到回應時寫入日誌（會由根 logger 的 TimedRotatingFileHandler 輸出，逐日輪替，保留 30 天）。
        """
        # 若距離上次控制指令不足，先等待剩餘時間
        # Use lock if available to prevent concurrent serial access
        lock = getattr(self, 'lock', None)
        if lock:
            lock.acquire()
        try:
            try:
                elapsed = time.time() - self.last_action_time
            except Exception:
                elapsed = getattr(self, 'MIN_COMMAND_INTERVAL', 2)
     
            if elapsed < getattr(self, 'MIN_COMMAND_INTERVAL', 2):
                wait = getattr(self, 'MIN_COMMAND_INTERVAL', 2) - elapsed
                self.logger.debug(f"Waiting {wait:.2f}s before sending command: {cmd}")
                time.sleep(wait)
     
            # 更新最後動作時間 (除了查詢狀態指令外)
            if cmd not in ["state"] and not cmd.startswith("op"):
                self.last_action_time = time.time()
                # 在 mock mode 下不需要 wake_up 邏輯，避免遞迴呼叫
                if not self.mock_mode and self.is_sleeping and cmd != "dep1":
                    self.wake_up()
     
            # 記錄要發送的指令
            self.logger.info(f"MachineCommand SEND: {cmd}")
     
            if self.mock_mode:
                self.logger.info(f"[MOCK] Sending command: {cmd}")
                time.sleep(0.5) # Simulate delay
                resp = self._mock_response(cmd)
                self.logger.info(f"MachineCommand RECV (mock): {cmd} -> {resp}")
                return resp
     
            if not self.ser or not self.ser.is_open:
                self.logger.error(f"MachineCommand ERROR: Serial not open for cmd={cmd}")
                return "Error: Serial not open"
     
            full_cmd = f"{cmd}\n" # 加上 0x0A (Line Feed)
            try:
                self.ser.write(full_cmd.encode())
            except Exception as e:
                self.logger.error(f"MachineCommand WRITE ERROR: {cmd} -> {e}")
                return f"Error: {e}"
            
            # 讀取回應
            response = ""
            start_time = time.time()
            
            while (time.time() - start_time) < 10: # 10秒 timeout
                try:
                    if self.ser.in_waiting:
                        line = self.ser.readline().decode().strip()
                        self.logger.debug(f"Received raw: {line}")
                        response = line # Keep updating the last response
                        low = line.lower()
                        
                        # 判斷指令是否完成（較寬鬆的比對）
                        if cmd == "dep1" and "device power on" in low:
                            break
                        if cmd == "open" and "open" in low and "opened" in low or "open" in low and "opened" in low:
                            # accept variations containing 'opened' or 'open'
                            if "opened" in low or "open" in low:
                                break
                        if cmd == "close" and "closed" in low:
                            break
                        if cmd.startswith("put") and low.startswith("been put"):
                            break
                        if cmd == "homing" and "homed" in low:
                            break
                        # 一般狀態查詢指令
                        if cmd in ["state", "opbm1", "opbm2", "opbm3", "opbm4", "opwd1", "opwd2", "opbhdn", "opbhup"]:
                            if line: break
                        if cmd == "bookok" and low.startswith("book is"):
                            break
                except Exception as e:
                    self.logger.error(f"MachineCommand READ ERROR for {cmd}: {e}")
                    break
                
                time.sleep(0.1)
            
            self.logger.info(f"MachineCommand RECV: {cmd} -> {response}")
            return response
        finally:
            if lock:
                try:
                    lock.release()
                except Exception:
                    pass

    def _mock_response(self, cmd):
        """模擬回應"""
        if cmd == "dep1": return "device power on"
        if cmd == "dep0": return "device power off"
        if cmd == "open": return "opened"
        if cmd == "reopen": return "opened"
        if cmd == "close": return "closed"
        if cmd == "put1": return "been put1"
        if cmd == "put2": return "been put2"
        if cmd == "bookok": return "book is ok"
        if cmd == "state": return "2" # Homed
        return "ack"

    def init_machine(self):
        """初始化機器: 啟動電源 (dep1) -> 回 Home (homing)"""
        resp = self._send_command("dep1")
        self.is_sleeping = False
        self.last_action_time = time.time()
        
        if "device power on" not in resp and "state" not in resp: # Assuming state might be returned on error
             self.logger.warning(f"Init power on response: {resp}")
        
        resp = self._send_command("homing")
        return "Homed" in resp or "ack" in resp

    def wake_up(self):
        """喚醒機器 (dep1)"""
        if self.is_sleeping:
            self.logger.info("Waking up machine...")
            self._send_command("dep1")
            self.is_sleeping = False

    def check_idle(self):
        """檢查是否閒置超時，若超時則進入休眠 (dep0)"""
        if not self.is_sleeping and (time.time() - self.last_action_time) > self.IDLE_TIMEOUT:
            self.logger.info("Machine idle timeout, entering sleep mode (dep0)")
            self._send_command("dep0")
            self.is_sleeping = True

    def open_door(self):
        """開啟投書口 (智慧判斷狀態)"""
        resp = self._send_command("open")
        
        # 若成功開啟
        if "opened" in resp:
            return True
            
        # 若已在開啟狀態 (State 3)，視為成功
        if "error state 3" in resp:
            return True
            
        # 若處於等待確認狀態 (State 4)，則應使用 reopen
        if "error state 4" in resp:
            self.logger.info("Machine in state 4, switching to 'reopen' command")
            return self.reopen_door()
            
        return False

    def reopen_door(self):
        """重新開啟投書口 (用於書籍未放妥時)"""
        resp = self._send_command("reopen")
        # 若已在開啟狀態 (State 3)，視為成功
        if "error state 3" in resp:
            return True
        return "opened" in resp

    def close_door(self):
        """關閉投書口"""
        resp = self._send_command("close")
        
        # 若成功關閉
        if "closed" in resp:
            return True
            
        # 若已在關閉狀態 (State 4)，視為成功
        if "error state 4" in resp:
            return True
            
        return False

    def check_book_status(self):
        """檢查書籍是否放妥 (bookok)"""
        resp = self._send_command("bookok")
        return "book is ok" in resp

    def sort_book(self, bin_number=1):
        """分類書籍 (put1 or put2)"""
        cmd = f"put{bin_number}"
        resp = self._send_command(cmd)
        return f"been {cmd}" in resp

    def get_status(self):
        """獲取機器狀態"""
        return self._send_command("state")
        
    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def set_uart(self, port, baudrate):
        """動態設定 UART 參數並嘗試重連序列埠（若非 mock mode）"""
        self.logger.info(f"Updating UART config -> port: {port}, baudrate: {baudrate}")
        self.port = port
        self.baudrate = baudrate

        # 如果是 mock mode，僅記錄並返回
        if self.mock_mode or not SERIAL_AVAILABLE:
            self.logger.warning("Mock mode or serial unavailable — skipping serial reopen")
            return False

        # 嘗試關閉既有連線，並用新參數重連
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
                timeout=2
            )
            self.logger.info(f"Reconnected to machine on {self.port} at {self.baudrate}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reconnect serial with new UART settings: {e}")
            return False

# Singleton instance
# 會嘗試從 MACHINE_CONFIG_FILE 讀取預設 port/baudrate
_default_port = '/dev/ttyUSB0'
_default_baud = 38400
try:
    if MACHINE_CONFIG_FILE.exists():
        cfg = json.loads(MACHINE_CONFIG_FILE.read_text(encoding='utf-8'))
        _default_port = cfg.get('port', _default_port)
        _default_baud = int(cfg.get('baudrate', _default_baud))
except Exception as e:
    logging.getLogger(__name__).warning(f"Failed to read machine config: {e}")

# 建立實例時預設不使用 mock（若系統上無 pyserial 或連線失敗，類別內會自動 fallback）
machine = MachineController(port=_default_port, baudrate=_default_baud, mock_mode=False)