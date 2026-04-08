import socket
import datetime
import logging

class SIP2Client:
    def __init__(self, host, port, login_user, login_pass, institution_id='MAIN'):
        self.host = host
        self.port = port
        self.login_user = login_user
        self.login_pass = login_pass
        self.institution_id = institution_id
        self.sock = None
        self.logger = logging.getLogger(__name__)

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((self.host, self.port))
            self.logger.info(f"Connected to SIP2 server at {self.host}:{self.port}")
            return True
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            return False

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send_message(self, message):
        if not self.sock:
            if not self.connect():
                return None
        
        # Add CRC (Optional but recommended) - implementation simplified for now
        # Ideally, we should calculate CRC and append it. 
        # Format: <message><sequence><CRC><CR>
        
        # Simple send without CRC logic for now, just append CR
        message += '\r'
        
        try:
            self.logger.info(f"SIP2 SEND: {message.replace(chr(13), '<CR>')}")  
            self.sock.sendall(message.encode('utf-8'))
            response = self.sock.recv(1024).decode('utf-8')
            self.logger.info(f"SIP2 RECV: {response.replace(chr(13), '<CR>')}")  
            return response
        except Exception as e:
            self.logger.error(f"Socket error: {e}")
            self.close()
            return None

    def login(self):
        """
        93 Login
        - 如果 login_user / login_pass 都沒給，就發送和你手動一樣的版本：
        93CN|CO|CP<INST>|AY0
        - 如果有給帳號密碼，再用 9300CNuser|COpass| 的寫法 (CP 通常也需要，如果 Server 要求)
        """
        inst = self.institution_id or 'MAIN'
        
        try:
            # 1) 完全無帳號密碼：走「IP 白名單」模式
            if not self.login_user and not self.login_pass:
                self.logger.info("Attempting login without credentials (IP whitelist mode)") 
                msg = f"93CN|CO|CP{inst}|AY0"
                resp = self._send_message(msg)
                if resp and resp.startswith('94'):
                    self.logger.info("Login successful (IP whitelist mode)")
                    return True
                self.logger.error(f"Login failed (IP whitelist mode), response: {resp}")
                return False

            # 2) 有帳號密碼：走標準 93 格式
            # 注意：某些 SIP2 Server 即使有帳號密碼，也需要 CP 欄位 (Institution Id)
            self.logger.info(f"Attempting login with credentials for user: {self.login_user}")
            uid_algo = '0'
            pwd_algo = '0'
            pwd_field = f"CO{self.login_pass}|" if self.login_pass else "CO|"
            msg = f"93{uid_algo}{pwd_algo}CN{self.login_user}|{pwd_field}CP{inst}|"
            resp = self._send_message(msg)
            if resp and resp.startswith('94'):
                # 通常 resp[2] == '1' 代表 OK，你也可以再細判
                self.logger.info(f"Login successful with credentials, response: {resp[2] if len(resp) > 2 else 'N/A'}")
                return True
            self.logger.error(f"Login failed with credentials, response: {resp}")
            return False
        except Exception as e:
            self.logger.error(f"Login exception: {e}")
            return False

    def get_book_info(self, barcode):
        """17 Item Information"""
        try:
            # 17<Date><AO Institution Id><AB Item Identifier><AC Terminal Password>...
            now = datetime.datetime.now().strftime("%Y%m%d    %H%M%S")
            inst = self.institution_id or 'MAIN'
            msg = f"17{now}AO{inst}|AB{barcode}|"
            
            self.logger.info(f"Querying book info for barcode: {barcode}")
            resp = self._send_message(msg)
            if not resp: 
                self.logger.error("No response from SIP2 server")
                return None

            # Parse 18 Item Information Response
            # 18<Circulation Status><Security Marker>...
            # Fixed length fields first
            if not resp.startswith('18'): 
                self.logger.error(f"Unexpected response format: {resp[:20]}...")
                return None
            
            self.logger.info(f"Received item info response for {barcode}")
            
            # Simple parsing logic (SIP2 is positional + variable fields)
            # We need to extract Title (AJ), Author (AA), Due Date (AH), Patron Name (AE),
            # 以及附件相關欄位（AQ / AR）。
            data = {
                "barcode": barcode,
                "title": "Unknown Title",
                "author": "Unknown Author",
                "status": "Unknown",
                "due_date": None,
                "patron_name": None,
                "has_attachment": False,  # Default to False
                "attachment_desc": None,
                "attachment_ar": None,    # SIP2 AR 欄位原始內容（例如：附件未借出、1張光碟片）
                "error": False,
                "error_message": None,
            }
            
            # 解析回應的固定長度欄位
            if len(resp) >= 4:
                circulation_status = resp[2:4]
                # Circulation Status 代碼對照：
                # 01 = Other, 02 = On order, 03 = Available, 04 = Charged, 05 = Charged; not to be recalled, etc.
                status_map = {
                    '01': 'Other', '02': 'On order', '03': 'Available', 
                    '04': 'Charged', '05': 'Charged; not to be recalled',
                    '06': 'In process', '07': 'Recalled', '08': 'Waiting on hold shelf',
                    '09': 'Waiting to be re-shelved', '10': 'In transit between library locations',
                    '11': 'Claimed returned', '12': 'Lost', '13': 'Missing'
                }
                data['status'] = status_map.get(circulation_status, f'Unknown ({circulation_status})')
                self.logger.debug(f"Circulation status: {circulation_status} -> {data['status']}")
            
            parts = resp.split('|')
            for part in parts:
                if part.startswith('AA'): 
                    # AA 欄位包含書名
                    data['title'] = part[2:]
                    self.logger.debug(f"Found title: {data['title']}")
                elif part.startswith('AC'): 
                    # AC 通常是作者欄位
                    data['author'] = part[2:]
                    self.logger.debug(f"Found author: {data['author']}")
                elif part.startswith('AH'): 
                    data['due_date'] = part[2:].strip() # Due Date
                    self.logger.debug(f"Found due date: {data['due_date']}")
                elif part.startswith('AJ'): 
                    # AJ 在這個系統中是資料類型，不是書名
                    item_type = part[2:]
                    self.logger.debug(f"Found item type: {item_type}")
                elif part.startswith('AE'): 
                    # AE 在這個系統中是 ISBN
                    isbn = part[2:]
                    self.logger.debug(f"Found ISBN: {isbn}")
                elif part.startswith('CH'):
                    # CH 欄位可能包含索書號等資訊
                    ch_content = part[2:]
                    self.logger.debug(f"Found CH field: {ch_content}")
                elif part.startswith('AF') or part.startswith('AG'):
                    # AF/AG 在某些 SIP2 回應中有時為錯誤訊息，但也可能只是額外欄位 (例如再次回傳 barcode 或索書號)
                    # 只在明確包含錯誤關鍵字時，才視為錯誤；否則把內容保留為一般欄位
                    msg = part[2:].strip()
                    if msg:
                        low = msg.lower()
                        error_keywords = ['錯誤', 'error', 'invalid', 'not found', 'failed', 'not present', 'not available', '不存在']
                        is_error = any(kw in low for kw in error_keywords)
                        if is_error:
                            data['error'] = True
                            if not data.get('error_message'):
                                data['error_message'] = msg
                            else:
                                data['error_message'] += ('; ' + msg)
                            self.logger.debug(f"Found error/info field ({part[:2]}): {msg}")
                        else:
                            # 非錯誤訊息：保留到 af/ag 欄位以便日後使用
                            if part.startswith('AF'):
                                data['af'] = msg
                            else:
                                data['ag'] = msg
                            self.logger.debug(f"Found AF/AG non-error field ({part[:2]}): {msg}")
                elif part.startswith('AR'):
                    # AR 欄位：附件借閱狀態，例如："附件未借出"、"1張光碟片"
                    ar_val = part[2:].strip()
                    if ar_val:
                        data['attachment_ar'] = ar_val
                        self.logger.debug(f"Found attachment status (AR): {ar_val}")
                elif part.startswith('AQ'):
                    # AQ 欄位：附件說明，例如「1張光碟片」
                    attach = part[2:].strip()
                    if attach:
                        data['has_attachment'] = True
                        data['attachment_desc'] = attach
                        self.logger.debug(f"Found attachment info (AQ): {attach}")

            # If there was an error message from AF/AG, return an error-style dict
            if data.get('error'):
                return {
                    'error': True,
                    'message': data.get('error_message')
                }
            
            self.logger.info(f"Successfully parsed book info: {data['title']} by {data['author']}")
            return data
        except Exception as e:
            self.logger.error(f"Exception in get_book_info: {e}")
            return None

    def checkin_book(self, barcode):
        """09 Checkin"""
        # 09<No block><Date><Return Date><Current Location><Institution Id><Item Identifier><Terminal Password>...
        now = datetime.datetime.now().strftime("%Y%m%d    %H%M%S")
        inst = self.institution_id or 'MAIN'
        # AP (Current Location) 使用 config 指定的還書地點（例如 LB），若未設定則退回 inst
        current_loc = getattr(self, 'current_location', None) or inst
        msg = f"09N{now}{now}AP{current_loc}|AO{inst}|AB{barcode}|"
        
        resp = self._send_message(msg)
        if not resp: 
            self.logger.error(f"No response from SIP2 server for checkin: {barcode}")
            return {
                "success": False,
                "af_message": None,
                "ag_message": None,
                "error_message": "No response from SIP2 server"
            }

        # 10 Checkin Response
        # 格式：10<ok><resensitize><magnetic media><alert><date>...
        # ok 標誌在第 3 位（索引 2）：'1' = 成功，'0' = 失敗
        if not resp.startswith('10'):
            self.logger.error(f"Unexpected checkin response format: {resp[:20]}...")
            return {
                "success": False,
                "af_message": None,
                "ag_message": None,
                "error_message": "Unexpected checkin response format"
            }
        
        if len(resp) < 3:
            self.logger.error(f"Checkin response too short: {resp}")
            return {
                "success": False,
                "af_message": None,
                "ag_message": None,
                "error_message": "Checkin response too short"
            }
        
        ok_flag = resp[2]

        # 先抽取 AF / AG 訊息，無論成功或失敗都保留原始內容
        af_msg = None
        ag_msg = None
        try:
            parts = resp.split('|')
            for part in parts:
                if part.startswith('AF'):
                    msg = part[2:].strip()
                    if msg:
                        af_msg = msg
                elif part.startswith('AG'):
                    msg = part[2:].strip()
                    if msg:
                        ag_msg = msg
        except Exception as e:
            self.logger.debug(f"Parse AF/AG in checkin response failed: {e}")

        # 只要 09 回傳 10 且 ok_flag == '1'，一律視為成功，不再因 AF/AG 拒絕還書
        if ok_flag == '1':
            self.logger.info(f"Checkin successful for {barcode}")
            if af_msg:
                self.logger.info(f"Checkin AF message: {af_msg}")
            if ag_msg:
                self.logger.info(f"Checkin AG message: {ag_msg}")
            return {
                "success": True,
                "af_message": af_msg,
                "ag_message": ag_msg
            }

        # ok_flag != '1' 視為真正拒絕還書，這時才把 AF/AG 當成錯誤訊息
        error_msg = af_msg or ag_msg or "未知錯誤"
        self.logger.warning(f"Checkin failed for {barcode}: {error_msg}")
        return {
            "success": False,
            "af_message": af_msg,
            "ag_message": ag_msg,
            "error_message": error_msg
        }

    def health_check(self):
        # 用 99 SC Status 做健康檢查
        now = datetime.datetime.now().strftime("%Y%m%d    %H%M%S")
        inst = self.institution_id or 'MAIN'
        msg = f"99{now}AO{inst}|AY1"
        resp = self._send_message(msg)
        return bool(resp and resp.startswith('98'))

# Mock Client for testing when actual server is unreachable
class MockSIP2Client:
    def __init__(self, host, port, login_user, login_pass, institution_id='MAIN'):
        self.logger = logging.getLogger(__name__)
        self.institution_id = institution_id
        self.dynamic_books = {}
        self.dynamic_counter = 1
    
    def connect(self): return True
    def close(self): pass
    def login(self): return True
    
    def get_book_info(self, barcode):
        if barcode == "error": return None
        # 模擬一個已過期的書籍
        if barcode == "overdue":
            return {
                "barcode": barcode,
                "title": f"逾期測試書籍-{barcode}",
                "author": "測試作者",
                "status": "Checked Out",
                "due_date": (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d"),
                "patron_name": "測試借閱者",
                "has_attachment": False
            }
        
        # 模擬有附件的書籍 (barcode 包含 'attach')
        if "attach" in barcode:
             return {
                "barcode": barcode,
                "title": f"SIP2含附件書籍-{barcode}",
                "author": "測試作者",
                "status": "Checked Out",
                "due_date": (datetime.datetime.now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d"),
                "patron_name": "測試借閱者",
                "has_attachment": True
            }

        # 檢查是否已經綁定過 (實現隨機條碼紀錄與綁定)
        if barcode in self.dynamic_books:
            return self.dynamic_books[barcode]

        # 產生新的測試書名 (循環 001 - 003)
        seq = ((self.dynamic_counter - 1) % 3) + 1
        title = f"還書機測試用-{seq:03d}"
        
        book_data = {
            "barcode": barcode,
            "title": title,
            "author": "測試作者",
            "status": "Checked Out",
            "due_date": (datetime.datetime.now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d"),
            "patron_name": "測試借閱者",
            "has_attachment": False
        }
        
        # 紀錄條碼與書名的綁定
        self.dynamic_books[barcode] = book_data
        self.dynamic_counter += 1

        return book_data

    def checkin_book(self, barcode):
        return {
            "success": True,
            "af_message": None,
            "ag_message": None
        }

    def health_check(self):
        return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = SIP2Client('140.125.', 6001, 'testuser', 'testpass')