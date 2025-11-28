import socket
import datetime
import logging

class SIP2Client:
    def __init__(self, host, port, login_user, login_pass):
        self.host = host
        self.port = port
        self.login_user = login_user
        self.login_pass = login_pass
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
        """93 Login"""
        # 如果沒有提供 Login User，則跳過登入步驟
        # 某些圖書館系統 (如 Koha) 允許直接查詢而不需先送 93 Login
        if not self.login_user:
            self.logger.info("No login user provided, skipping Login command.")
            return True

        # 93<UID algorithm><PWD algorithm><Login User ID><Login Password>
        # Algorithm 0 = No encryption
        uid_algo = '0'
        pwd_algo = '0'
        
        # 根據 SIP2 標準，如果沒有密碼，CO 欄位可以留空或不傳，但有些系統要求傳送空值
        # 這裡處理 password 為 None 或空字串的情況
        pwd_field = f"CO{self.login_pass}|" if self.login_pass else "CO|"
        
        msg = f"93{uid_algo}{pwd_algo}CN{self.login_user}|{pwd_field}"
        resp = self._send_message(msg)
        if resp and resp.startswith('941'): # 94 is Login Response, 1 is Ok
            return True
        return False

    def get_book_info(self, barcode):
        """17 Item Information"""
        # 17<Date><AO Institution Id><AB Item Identifier><AC Terminal Password>...
        now = datetime.datetime.now().strftime("%Y%m%d    %H%M%S")
        msg = f"17{now}AO|AB{barcode}|"
        
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
        msg = f"09N{now}{now}AP|AO|AB{barcode}|" # Simplified
        
        resp = self._send_message(msg)
        if not resp: return False

        # 10 Checkin Response
        # 10<Ok><Resensitize>...
        if resp.startswith('101'): # 1 is Ok
            return True
        return False

# Mock Client for testing when actual server is unreachable
class MockSIP2Client:
    def __init__(self, host, port, login_user, login_pass):
        self.logger = logging.getLogger(__name__)
    
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

        return {
            "barcode": barcode,
            "title": f"SIP2測試書籍-{barcode}",
            "author": "測試作者",
            "status": "Checked Out",
            "due_date": (datetime.datetime.now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d"),
            "patron_name": "測試借閱者",
            "has_attachment": False
        }

    def checkin_book(self, barcode):
        return True
