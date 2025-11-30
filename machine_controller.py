import time
import threading
import logging
import json
from pathlib import Path

import serial

# 可配置的機器設定檔（儲存 UART 設定）
MACHINE_CONFIG_FILE = Path('machine_config.json')

class MachineController:
    def __init__(self, port='/dev/uno', baudrate=38400):
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        # Use dedicated 'machine' logger so machine logs go to machine.log
        self.logger = logging.getLogger('machine')
        # Threading lock to protect serial access across threads/requests
        self.lock = threading.Lock()

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
        # homing 非同步旗標：喚醒後背景執行 homing 不阻塞呼叫端
        self.homing_in_progress = False
        # 機器是否已完成 homing（即為 ready state）
        self.is_homed = False
        # 在非同步喚醒後，最長等待 homing 完成時間（秒）
        self.HOMING_WAIT_TIMEOUT = 30

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
            woke = False
            if cmd not in ["state"] and not cmd.startswith("op"):
                self.last_action_time = time.time()
                if self.is_sleeping and cmd != "dep1":
                    # 呼叫 wake_up（可能啟動非同步 homing）
                    self.wake_up()
                    woke = True
      
            # 如果剛喚醒且非同步 homing 還未完成，等待 homing (以避免立即下 open 等動作導致錯誤)
            if woke and cmd != "dep1":
                wait_deadline = time.time() + getattr(self, 'HOMING_WAIT_TIMEOUT', 30)
                self.logger.info(f"Waiting for homing to complete before sending '{cmd}' (deadline in {self.HOMING_WAIT_TIMEOUT}s)...")
                while time.time() < wait_deadline:
                    if not getattr(self, 'homing_in_progress', False) and getattr(self, 'is_homed', False):
                        break
                    time.sleep(0.1)
                else:
                    # timeout
                    self.logger.warning(f"Homing did not finish within {self.HOMING_WAIT_TIMEOUT}s; proceeding may fail.")
                    return "Error: Homing timeout"
      
            # 記錄要發送的指令
            self.logger.info(f"MachineCommand SEND: {cmd}")
     
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
            
            # 針對機械動作指令延長等待時間
            timeout = 20 if cmd in ["open", "close", "reopen"] else 10
    
            while (time.time() - start_time) < timeout:
                try:
                    if self.ser.in_waiting:
                        line = self.ser.readline().decode().strip()
                        self.logger.debug(f"Received raw: {line}")
                        response = line # Keep updating the last response
                        low = line.lower()
                        
                        # 判斷指令是否完成（較寬鬆的比對）
                        if cmd == "dep1" and "device power on" in low:
                            break
                        if cmd == "open" and ("opened" in low or "open" in low):
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

    def init_machine(self):
        """初始化機器: 啟動電源 (dep1) -> 回 Home (homing)"""
        resp = self._send_command("dep1")
        self.is_sleeping = False
        self.last_action_time = time.time()
        
        if "device power on" not in resp and "state" not in resp: # Assuming state might be returned on error
             self.logger.warning(f"Init power on response: {resp}")
        
        resp = self._send_command("homing")
        homed = False
        try:
            homed = ("homed" in resp.lower()) or ("ack" in resp.lower())
        except Exception:
            homed = False
        self.is_homed = bool(homed)
        return homed

    def wake_up(self):
        """喚醒機器 (dep1)，並於背景非同步執行 homing 以回到 Home（避免阻塞使用者介面）。"""
        if self.is_sleeping:
            self.logger.info("Waking up machine...")
            self._send_command("dep1")
            self.is_sleeping = False

            # 若尚未在執行 homing，啟動背景執行緒執行 homing
            if not getattr(self, 'homing_in_progress', False):
                def do_homing():
                    try:
                        self.homing_in_progress = True
                        self.logger.info("Starting asynchronous homing...")
                        resp = self._send_command("homing")
                        self.logger.info(f"Asynchronous homing completed: {resp}")
                        try:
                            ok = ("homed" in resp.lower()) or ("ack" in resp.lower())
                        except Exception:
                            ok = False
                        self.is_homed = bool(ok)
                    except Exception as e:
                        self.logger.warning(f"Homing after wake failed: {e}")
                        self.is_homed = False
                    finally:
                        self.homing_in_progress = False

                t = threading.Thread(target=do_homing, daemon=True)
                t.start()

    def check_idle(self):
        """檢查是否閒置超時，若超時則先嘗試回原點（homing）再進入休眠 (dep0)。

        為避免機器在非標準狀態（例如 state 4）下直接進入休眠造成錯誤：
        - 若尚未 homed，會同步執行 homing（短暫等待），更新 is_homed。
        - homing 若失敗或超時，仍會記錄 warning，然後嘗試送 dep0（避免長時間佔用）。
        """
        if not self.is_sleeping and (time.time() - self.last_action_time) > self.IDLE_TIMEOUT:
            # 先嘗試回原點，僅在需要時執行
            try:
                self.logger.info("Idle timeout: attempting homing before entering sleep")
                if not getattr(self, 'is_homed', False) and not getattr(self, 'homing_in_progress', False):
                    # 同步執行 homing，避免在非標準狀態直接進入 dep0
                    self.homing_in_progress = True
                    resp = self._send_command("homing")
                    self.logger.info(f"Homing before sleep response: {resp}")
                    try:
                        ok = ("homed" in str(resp).lower()) or ("ack" in str(resp).lower())
                    except Exception:
                        ok = False
                    self.is_homed = bool(ok)
            except Exception as e:
                self.logger.warning(f"Homing before sleep failed: {e}")
            finally:
                # 無論 homing 成敗，清除 homing flag
                self.homing_in_progress = False

            # 再進入休眠
            self.logger.info("Machine idle timeout, entering sleep mode (dep0)")
            self._send_command("dep0")
            self.is_sleeping = True
            # 進入休眠後，homed 狀態失效
            self.is_homed = False

    def open_door(self):
        """開啟投書口 (智慧判斷狀態)
        - 若機器處於睡眠，先喚醒並等待 homing 完成（會以 HOMING_WAIT_TIMEOUT 為上限）。
        - 若 homing 未完成則會等待；超時則回傳 False，避免送出 open 導致機器錯誤。
        """
        # 若在睡眠，先喚醒（會觸發非同步 homing）
        if self.is_sleeping:
            self.logger.info("open_door requested: machine sleeping, calling wake_up() first")
            self.wake_up()
        
        # 等待 homing 完成（若有在進行或尚未 homed）
        wait_deadline = time.time() + getattr(self, 'HOMING_WAIT_TIMEOUT', 30)
        if getattr(self, 'homing_in_progress', False) or not getattr(self, 'is_homed', False):
            self.logger.info(f"Waiting up to {self.HOMING_WAIT_TIMEOUT}s for homing to complete before open")
            while time.time() < wait_deadline:
                if not getattr(self, 'homing_in_progress', False) and getattr(self, 'is_homed', False):
                    break
                time.sleep(0.1)
            else:
                self.logger.warning("open_door aborted: homing did not complete in time")
                return False
        
        # 目前已喚醒且 homed，安全發送 open 指令
        resp = self._send_command("open")
        
        # 若成功開啟 (收到 opened 為主，若超時但有收到 ack 也視為成功)
        if "opened" in resp or "ack" in resp:
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
        
        # 若成功關閉 (收到 closed 為主，若超時但有收到 ack 也視為成功)
        if "closed" in resp or "ack" in resp:
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
        """動態設定 UART 參數並嘗試重連序列埠"""
        self.logger.info(f"Updating UART config -> port: {port}, baudrate: {baudrate}")
        self.port = port
        self.baudrate = baudrate

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
