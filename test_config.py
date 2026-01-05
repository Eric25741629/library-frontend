#!/usr/bin/env python3
"""
快速測試當前配置
驗證查詢功能與還書開關
"""
import config
import logging
from sip2_client import SIP2Client

# 簡化的日誌設定
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def test_current_config():
    print("=== 當前配置測試 ===")
    
    # 重新載入配置
    config.reload_config()
    
    print(f"圖書館主機: {config.LIBRARY_HOST}:{config.LIBRARY_PORT}")
    print(f"模擬模式: {'是' if config.LIBRARY_MOCK_ENABLED else '否'}")
    print(f"還書功能: {'啟用' if config.LIBRARY_CHECKIN_ENABLED else '停用'}")
    print(f"機構ID: {config.LIBRARY_INSTITUTION}")
    print()
    
    if config.LIBRARY_MOCK_ENABLED:
        print("ℹ️  當前為模擬模式，不會測試真實連接")
        return
    
    print("測試真實連接...")
    try:
        sip2 = SIP2Client(
            config.LIBRARY_HOST,
            config.LIBRARY_PORT,
            config.LIBRARY_USER if config.LIBRARY_LOGIN_ENABLED else "",
            config.LIBRARY_PASS if config.LIBRARY_LOGIN_ENABLED else "",
            institution_id=config.LIBRARY_INSTITUTION
        )
        
        if sip2.connect():
            print("✓ 連接成功")
            if sip2.login():
                print("✓ 登入成功")
                
                # 測試查詢功能
                print("測試查詢功能...")
                book_info = sip2.get_book_info("C261954")  # 使用之前的測試條碼
                if book_info:
                    print(f"✓ 查詢成功: {book_info['title']}")
                else:
                    print("✗ 查詢失敗")
                
                # 顯示還書功能狀態
                print(f"還書功能: {'會' if config.LIBRARY_CHECKIN_ENABLED else '不會'}真的執行到伺服器")
                
            else:
                print("✗ 登入失敗")
        else:
            print("✗ 連接失敗")
        
        sip2.close()
        
    except Exception as e:
        logger.error(f"測試錯誤: {e}")

if __name__ == "__main__":
    test_current_config()