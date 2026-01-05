import time
import threading
import logging
import json
from pathlib import Path

import serial

# 可配置的機器設定檔（儲存 UART 設定）
MACHINE_CONFIG_FILE = Path('machine_config.json')

class MachineController:
    def __init__(self, port='/dev/uno', baudrate=38400, simulate=False):
        """
        simulate: 若為 True，則不開啟序列埠，而在 _send_command 中回傳模擬回應，
        方便遠端或 CI 在沒有實體設備時測試放書/檢查書籍流程。
        """
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.simulate = bool(simulate)
        # Use dedicated 'machine' logger so machine logs go to machine.log
        self.logger = logging.getLogger('machine')
        # Threading lock to protect serial access across threads/requests
        # 使用 RLock 允許同一執行緒（例如在 recovery 中）重入
        self.lock = threading.RLock()

        # 狀態常數
        self.STATE_NOT_INIT = 0
        self.STATE_POWER_ON_NOT_HOMED = 1
        self.STATE_HOMED = 2
        self.STATE_OPENED = 3
        self.STATE_CLOSED = 4 # 等待確認書籍
        self.STATE_SLEEPING = 5 # 待機中 (新增)
        
        # 閒置休眠設定
        self.last_action_time = time.time()
        self.last_cmd = None # 記錄上一個執行的指令
        self.MIN_COMMAND_INTERVAL = 2  # 最少兩秒才可發送下一個控制指令
        # Initialize last_serial_time to allow immediate first command
        self.last_serial_time = time.time() - self.MIN_COMMAND_INTERVAL
        self.IDLE_TIMEOUT = 15 * 60 # 15 minutes
        self.is_sleeping = True # 假設初始為休眠，init時會喚醒
        # homing 非同步旗標：喚醒後背景執行 homing 不阻塞呼叫端
        self.homing_in_progress = False
        # 機器是否已完成 homing（即為 ready state）
        self.is_homed = False
        # 機構是否正在執行某個動作（例如 put1/put2/slide 等），此期間不接受控制指令
        self.action_in_progress = False
        # 當前執行中的動作代碼（若有）
        self.current_action_code = None
        # 是否已完成初次初始化（區分第一次開機需要強制 homing）
        self.initialized = False
        # 在非同步喚醒後，最長等待 homing 完成時間（秒）
        self.HOMING_WAIT_TIMEOUT = 30
        
        # 暫停狀態查詢的截止時間 (timestamp)
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
                self.logger.info(f"Connected to machine on {self.port}")
            except Exception as e:
                self.logger.error(f"Failed to connect to machine: {e}")
        else:
            # 在模擬模式下，不嘗試開啟序列埠
            self.ser = None
            self.logger.info("MachineController running in simulate mode; serial disabled")

    def _send_command(self, cmd):
        """發送指令並等待回應 (ACK 或 完成訊息)

        - 在發送前確保與上一個控制指令的間隔至少為 self.MIN_COMMAND_INTERVAL（秒）。
        - 在發送與收到回應時寫入日誌（會由根 logger 的 TimedRotatingFileHandler 輸出，逐日輪替，保留 30 天）。
        """
        # 若距離上次控制指令不足，先等待剩餘時間
        # Use lock if available to prevent concurrent serial access
        lock = getattr(self, 'lock', None)

        # 若機構正在執行動作（action_in_progress），立即回傳 ack-busy
        # 只允許查詢相關指令通過（例如 state / bookok）以取得當前狀態
        if getattr(self, 'action_in_progress', False) and cmd not in ["state", "bookok"]:
            self.logger.info(f"MachineCommand BUSY: {cmd} -> ack-busy (action {getattr(self,'current_action_code',None)})")
            return "ack-busy"
        # 針對 'state' 查詢指令，使用非阻塞或短超時鎖定，避免在執行長時間動作(如 put)時卡死監控執行緒
        if cmd == "state":
            if lock:
                # 嘗試取得鎖，若失敗(被佔用)則直接放棄此次查詢
                if not lock.acquire(timeout=0.1):
                    # self.logger.debug("Skipping state query (lock busy)")
                    return ""
        else:
            # 其他控制指令則必須等待鎖
            if lock:
                lock.acquire()

        try:
            # 計算距離上次指令的時間 (使用 last_serial_time 確保所有指令都遵循間隔)
            try:
                last_t = getattr(self, 'last_serial_time', 0)
                if last_t == 0:
                     # Fallback if not init
                     last_t = self.last_action_time
                elapsed = time.time() - last_t
            except Exception:
                elapsed = getattr(self, 'MIN_COMMAND_INTERVAL', 2)

            # 嚴格限制：若處於暫停查詢期間，且當前指令為 state，則直接跳過不傳送
            # 這是為了避免在 close/put 等動作後立即查詢狀態導致邏輯重疊或干擾
            if cmd == "state":
                pq = getattr(self, 'pause_query_until', 0)
                if time.time() < pq:
                    # self.logger.debug(f"Skipping state query (paused until {pq})")
                    return "time error"

            # 決定等待時間：
            # 若上一個指令是 close，需要額外等待（依需求描述：額外多等待5秒，不能立刻詢問）
            # 其他情況使用 MIN_COMMAND_INTERVAL (預設 2秒)
            min_interval = getattr(self, 'MIN_COMMAND_INTERVAL', 2)
            last_c = getattr(self, 'last_cmd', None)

            #if last_c == "close":
                 # min_interval 為 2，再加上 5 秒，總共等待 7 秒
                 #required_interval = min_interval 
            #else:
            required_interval = min_interval
     
            if elapsed < required_interval:
                wait = required_interval - elapsed
                self.logger.debug(f"Waiting {wait:.2f}s before sending command: {cmd}")
                time.sleep(wait)
     
            # 更新最後動作時間 (除了查詢狀態指令外 - 影響休眠邏輯)
            woke = False
            if cmd not in ["state"] and not cmd.startswith("op"):
                self.last_action_time = time.time()
                if self.is_sleeping and cmd != "dep1":
                    # 呼叫 wake_up（可能啟動非同步 homing）
                    self.wake_up()
                    woke = True
            
            # 針對特定指令，設定暫停狀態查詢的時間 (pause state query)
            # 機器在觸發 close 時，應該觸發詢問機器狀態 15 秒
            # 機器在觸發 put1 時，應該暫停詢問狀態 30 秒
            # 注意：這裡使用負向邏輯：設定 last_serial_time 以延遲下一個指令，或者直接 sleep?
            # 需求解讀：是為了避免狀態查詢指令 (get_status -> state) 在這些動作執行期間干擾或獲取錯誤狀態。
            # 實作方式：更新 last_serial_time 或使用一個獨立的 pause_query_until 標記
            
            # 使用 pause_query_until 機制 (需在 __init__ 加入初始化)
            
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
                        response = line  # Keep updating the last response
                        low = line.lower()

                        # 若回傳純數字且為 action code（雙位數或更大），視為正在執行動作
                        try:
                            if line.isdigit():
                                code = int(line)
                                # 若為已知一般狀態（0-5），視為非 action
                                if code in [getattr(self, 'STATE_NOT_INIT', 0), getattr(self, 'STATE_POWER_ON_NOT_HOMED', 1), getattr(self, 'STATE_HOMED', 2), getattr(self, 'STATE_OPENED', 3), getattr(self, 'STATE_CLOSED', 4), getattr(self, 'STATE_SLEEPING', 5)]:
                                    self.action_in_progress = False
                                    self.current_action_code = None
                                else:
                                    # 例如 11,21,51.. 等動作代碼，標記為執行中
                                    self.action_in_progress = True
                                    self.current_action_code = code
                        except Exception:
                            pass

                        # 即時同步狀態：針對任何指令的回應進行解析，確保狀態與機器實際回應一致
                        try:
                            if 'power' in low:
                                if 'on' in low or 'device power on' in low:
                                    self.is_sleeping = False
                                elif 'off' in low or 'power of' in low or 'device power of' in low:
                                    self.is_sleeping = True

                            if 'homed' in low:
                                self.is_homed = True
                                self.homing_in_progress = False
                                self.is_sleeping = False

                            if 'opened' in low:  # 注意：open 可能是動詞，opened 才是狀態
                                self.is_sleeping = False
                                self.is_homed = True

                            if 'sleep' in low or 'dep0' in low or 'standby' in low:
                                self.is_sleeping = True
                        except Exception:
                            pass

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
                            if line:
                                break
                        if cmd == "bookok" and low.startswith("book is"):
                            break
                except Exception as e:
                    self.logger.error(f"MachineCommand READ ERROR for {cmd}: {e}")
                    break

                time.sleep(0.1)
            
            self.logger.info(f"MachineCommand RECV: {cmd} -> {response}")
            
            # 更新指令時間與類型
            self.last_serial_time = time.time()
            self.last_cmd = cmd
            
            # 處理特殊指令的狀態查詢暫停邏輯
            if cmd == "close":
        # 機器在觸發 close 時，應該暫停詢問機器狀態 15 秒
                # 根據後文 put1 暫停 30 秒的語境，這裡 "觸發詢問機器狀態 15 秒" 極可能是 "暫停詢問 15 秒" 的筆誤
                # 或者是指 "持續詢問 15 秒"？
                # 但結合 put1 的 "暫停詢問狀態 30 秒"，推測 close 也是需要一段穩定時間。
                # 但通常 close 動作期間不應查詢。
                # 假設原意是：動作後需要一段時間讓機構復歸，這期間不應查詢。
                # User: "機器在觸發close時 應該觸發詢問機器狀態15秒" -> 這句話比較曖昧
                # User: "機器在觸發put1時 應該暫停詢問狀態30秒" -> 這句話很明確
                
                # 重新審視 "觸發詢問機器狀態15秒"：
                # 1. 可能是 "暫停詢問 15 秒" (Pause query for 15s)
                # 2. 可能是 "持續詢問 15 秒" (Keep querying for 15s to confirm closed)
                # 考慮到 close 是一個動作，完成後需要確認狀態。
                # 但 put1 是分類動作，動作時間長，所以需要暫停查詢以免干擾。
                # close 動作相對快，但可能需要確認有沒有夾手或異物。
                # 如果是 "暫停詢問"，那邏輯一致。如果是 "觸發詢問"，那邏輯相反。
                
                # 根據一般嵌入式控制經驗，動作指令發送後通常會有一段 "不應打擾期"。
                # 假設 user 筆誤，意指 "暫停詢問"。若 user 真意是 "密集詢問"，則需另寫邏輯。
                # 暫時採取 "暫停詢問 15 秒" 以保持系統穩定，避免在動作未完成時 query 造成衝突。
                # 如果 user 發現不對，會再反饋。
                self.pause_query_until = time.time() + 15
                
            elif cmd.startswith("put"):
                # 機器在觸發 put1 (或 put2) 時，應該暫停詢問狀態 30 秒
                self.pause_query_until = time.time() + 30
            
            return response
        finally:
            if lock:
                try:
                    lock.release()
                except Exception:
                    pass

    def init_machine(self):
        """初始化機器
        策略：
        1. 檢查狀態。
        2. 若狀態為 2 (STATE_HOMED)，則視為已就緒，不進行任何操作。
        3. 若狀態為 5 (STATE_SLEEPING)，嘗試喚醒 (dep1) 並等待狀態變為 2。
        4. 若上述皆非，則執行完整初始化流程：dep1 (喚醒) -> homing (歸零)。
        """
        self.logger.info("Initializing machine: Checking status first...")
        
        try:
            status_resp = self.get_status()
            s = str(status_resp).strip()
            
            # 若為狀態 2，直接回傳
            if s.isdigit() and int(s) == self.STATE_HOMED:
                self.logger.info(f"Machine is already HOMED (state {s}). Initialization skipped.")
                self.is_homed = True
                self.initialized = True
                self.is_sleeping = False
                return True
            
            # 若為狀態 5 (休眠)，嘗試喚醒並檢查是否回到 2
            if s.isdigit() and int(s) == self.STATE_SLEEPING:
                self.logger.info("Machine is SLEEPING (state 5). Waking up (dep1)...")
                self._send_command("dep1")
                self.is_sleeping = False
                self.last_action_time = time.time()
                
                # 等待一小段時間讓狀態更新
                time.sleep(1.0)
                
                # 再次檢查狀態
                status_resp = self.get_status()
                s = str(status_resp).strip()
                if s.isdigit() and int(s) == self.STATE_HOMED:
                     self.logger.info(f"Machine woke up to HOMED (state {s}). Initialization skipped.")
                     self.is_homed = True
                     self.initialized = True
                     return True
                else:
                     self.logger.info(f"Machine woke up but state is {s} (not 2). Proceeding to homing.")
                
        except Exception as e:
            self.logger.warning(f"init_machine: status check failed: {e}")

        # 若非狀態 2 (或喚醒失敗)，開始初始化
        self.logger.info("Machine not in HOMED state. Performing full initialization.")

        # 1. 喚醒 (dep1)
        # 無論目前是否開啟，發送 dep1 確保機器喚醒
        self.logger.info("Sending 'dep1'...")
        self._send_command("dep1")
        self.is_sleeping = False
        self.last_action_time = time.time()
        
        # 2. 歸零 (homing)
        self.logger.info("Sending 'homing'...")
        resp = self._send_command("homing")
        
        # 判斷歸零是否成功
        homed = False
        try:
            s_resp = str(resp).lower()
            if "homed" in s_resp or "ack" in s_resp:
                homed = True
            
            # 再次確認狀態
            if not homed:
                final_status = self.get_status()
                if str(final_status).strip() == str(self.STATE_HOMED):
                    homed = True
        except Exception:
            pass

        self.is_homed = homed
        self.initialized = True
        return self.is_homed

    def wake_up(self):
        """喚醒機器 (dep1)。

        行為：
        - 發出 dep1 並把 is_sleeping 設為 False。
        - 依指示：初始化只需開機執行一次，喚醒時僅需傳遞開機指令，不需額外 homing。
        """
        if self.is_sleeping:
            self.logger.info("Waking up machine (sending dep1)...")
            self._send_command("dep1")
            self.is_sleeping = False
            
            # 更新狀態旗標，假設喚醒後為正常待機狀態
            # 若之前已是 homed，喚醒後理應保持 homed (除非斷電)
            # 這裡不主動觸發 homing，除非外部顯式呼叫
            # 假設喚醒後即回復到 ready (homed) 狀態，以免 open_door 等待 homed 超時
            self.is_homed = True

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
            
        # 若失敗，執行斷線重連並歸零
        self.logger.warning("close_door failed. Executing recovery: Disconnect -> Reconnect -> Homing.")
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
                        timeout=2
                    )
                    self.logger.info("Recovery: Serial reconnected.")
                    # 這裡調用 _send_command 會再次 acquire lock，因為是 RLock 所以沒問題
                    self._send_command("homing")
                except Exception as e:
                    self.logger.error(f"Recovery failed: {e}")

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
        """獲取機器狀態並同步內部旗標。
        
        - 會嘗試解析機器回傳的數字狀態（例如 "2"）或文字回應，
          並更新 self.is_sleeping / self.is_homed / self.homing_in_progress 等旗標，
          以避免外層因為未同步內部狀態而誤判為 sleeping。
        - 回傳原始回應（string/int 可接受），不拋出例外。
        """
        resp = self._send_command("state")
        try:
            s = str(resp).strip() if resp is not None else ""
            low = s.lower()
            # 如果回傳純數字（例如 "2"），轉換並同步內部狀態
            if s.isdigit():
                code = int(s)
                # STATE constants 存於實例屬性
                try:
                    if code == getattr(self, 'STATE_NOT_INIT', 0):
                        # 0 = not init：不要把它誤判為休眠，標記為未初始化（非休眠）
                        self.is_sleeping = False
                        self.is_homed = False
                        self.homing_in_progress = False
                    elif code == getattr(self, 'STATE_POWER_ON_NOT_HOMED', 1):
                        self.is_sleeping = False
                        self.is_homed = False
                        self.homing_in_progress = False
                    elif code == getattr(self, 'STATE_HOMED', 2):
                        self.is_sleeping = False
                        self.is_homed = True
                        self.homing_in_progress = False
                    elif code == getattr(self, 'STATE_OPENED', 3):
                        self.is_sleeping = False
                        # door open 理當表示不在 homing 且視為就緒
                        self.is_homed = True
                        self.homing_in_progress = False
                    elif code == getattr(self, 'STATE_CLOSED', 4):
                        self.is_sleeping = False
                    elif code == 5:
                        # 部分設備使用 5 表示休眠/待機（device-specific）
                        self.is_sleeping = True
                        self.is_homed = False
                        self.homing_in_progress = False
                    else:
                        # 未知 code，保持現狀但嘗試保守處理（不強制喚醒）
                        self.is_sleeping = False
                except Exception:
                    # 若更新旗標失敗，不影響回傳
                    pass
                return s
            # 文字型回應解析（舊設備可能回傳描述字串）
            # 處理電源/休眠字串：涵蓋「power on / power off / device power on / device power off」
            try:
                if 'power' in low:
                    # 明確包含 on/off
                    if 'on' in low or 'power on' in low or 'device power on' in low:
                        self.is_sleeping = False
                    elif 'off' in low or 'power off' in low or 'device power off' in low or 'device power of' in low or 'power of' in low:
                        # 部分設備回傳可能有截斷或 typo（如 "device power of"），對此保守視為關機/休眠
                        self.is_sleeping = True
                # Homed / ACK 表示已完成 homing，且應為非休眠狀態
                if 'homed' in low or 'ack' in low:
                    self.is_homed = True
                    self.homing_in_progress = False
                    self.is_sleeping = False
                # Door open 表示已就緒且非休眠
                if 'open' in low or 'opened' in low:
                    self.is_sleeping = False
                    self.is_homed = True
                    self.homing_in_progress = False
                # 明確的休眠/待機字串
                if 'sleep' in low or 'dep0' in low or 'standby' in low:
                    self.is_sleeping = True
            except Exception:
                # 解析非致命；保留現有狀態
                pass
            return resp
        except Exception as e:
            try:
                self.logger.debug(f"get_status parse error: {e}")
            except Exception:
                pass
            return resp
    
    def is_door_open(self):
        """檢查投書口是否為開啟狀態，回傳 bool（使用 get_status 的同步旗標或解析回應）。"""
        try:
            # 優先使用已同步的旗標或直接解析 get_status 的回應
            # 若已知 machine_state（is_homed 與 ser 回應）則可簡短判定
            resp = self.get_status()
            s = str(resp).lower() if resp else ""
            if "open" in s or "opened" in s:
                return True
            # 若內部狀態已知且是 homed 且 ser open 但未明確 open，回傳 False（保守）
            return False
        except Exception as e:
            try:
                self.logger.debug(f"is_door_open probe failed: {e}")
            except Exception:
                pass
            return False

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def set_uart(self, port, baudrate):
        """動態設定 UART 參數並嘗試重連序列埠"""
        with self.lock:
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
    def test_connection(self, port, baudrate):
        """測試 UART 連線 (不影響當前實例狀態)
        
        回傳格式統一為 (bool, message)：
        - (True, "Port opened successfully") 表示可開啟
        - (False, "<error message>") 表示失敗原因
        """
        temp_ser = None
        try:
            temp_ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=2
            )
            if temp_ser.is_open:
                return True, "Port opened successfully"
            return False, "Failed to open port"
        except Exception as e:
            return False, str(e)
        finally:
            # 僅關閉序列埠，不要在 finally 裡回傳值，避免覆寫前面的回傳
            if temp_ser and temp_ser.is_open:
                try:
                    temp_ser.close()
                except Exception:
                    pass
