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
            self.sock.sendall(message.encode('utf-8'))
            response = self.sock.recv(1024).decode('utf-8')
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
        
        # 1) 完全無帳號密碼：走「IP 白名單」模式
        if not self.login_user and not self.login_pass:
            msg = f"93CN|CO|CP{inst}|AY0"
            resp = self._send_message(msg)
            if resp and resp.startswith('94'):
                return True
            return False

        # 2) 有帳號密碼：走標準 93 格式
        # 注意：某些 SIP2 Server 即使有帳號密碼，也需要 CP 欄位 (Institution Id)
        uid_algo = '0'
        pwd_algo = '0'
        pwd_field = f"CO{self.login_pass}|" if self.login_pass else "CO|"
        msg = f"93{uid_algo}{pwd_algo}CN{self.login_user}|{pwd_field}CP{inst}|"
        resp = self._send_message(msg)
        if resp and resp.startswith('94'):
            # 通常 resp[2] == '1' 代表 OK，你也可以再細判
            return True
        return False

    def get_book_info(self, barcode):
        """17 Item Information"""
        # 17<Date><AO Institution Id><AB Item Identifier><AC Terminal Password>...
        now = datetime.datetime.now().strftime("%Y%m%d    %H%M%S")
        inst = self.institution_id or 'MAIN'
        msg = f"17{now}AO{inst}|AB{barcode}|"
        
        resp = self._send_message(msg)
        if not resp: return None

        # Parse 18 Item Information Response
        # 18<Circulation Status><Security Marker>...
        # Fixed length fields first
        if not resp.startswith('18'): return None
        
        # Simple parsing logic (SIP2 is positional + variable fields)
        # We need to extract Title (AJ), Author (AA), Due Date (AH), Patron Name (AE)
        data = {
            "barcode": barcode,
            "title": "Unknown Title",
            "author": "Unknown Author",
            "status": "Unknown",
            "due_date": None,
            "patron_name": None,
            "has_attachment": False # Default to False
        }
        
        parts = resp.split('|')
        for part in parts:
            if part.startswith('AJ'): data['title'] = part[2:]
            elif part.startswith('AA'): data['author'] = part[2:]
            elif part.startswith('AH'): data['due_date'] = part[2:].strip() # Due Date
            elif part.startswith('AE'): data['patron_name'] = part[2:] # Patron Name
            # Example field for attachment - adjust based on actual SIP2 field (e.g. BQ or CK)
            # elif part.startswith('BQ'): data['has_attachment'] = 'attachment' in part[2:].lower()
        
        return data

    def checkin_book(self, barcode):
        """09 Checkin"""
        # 09<No block><Date><Return Date><Current Location><Institution Id><Item Identifier><Terminal Password>...
        now = datetime.datetime.now().strftime("%Y%m%d    %H%M%S")
        inst = self.institution_id or 'MAIN'
        # AP (Current Location) 通常也是 Institution Id 或特定 Location Id，這裡暫時使用空或 inst
        msg = f"09N{now}{now}AP{inst}|AO{inst}|AB{barcode}|"
        
        resp = self._send_message(msg)
        if not resp: return False

        # 10 Checkin Response
        # 10<Ok><Resensitize>...
        if resp.startswith('101'): # 1 is Ok
            return True
        return False

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
        return True

    def health_check(self):
        return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = SIP2Client('140.125.', 6001, 'testuser', 'testpass')