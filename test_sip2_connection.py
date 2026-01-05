#!/usr/bin/env python3
"""
SIP2 連接測試腳本
用於測試與圖書館系統的實際連接
"""
import logging
import sys
from sip2_client import SIP2Client
import config

# 設定詳細的日誌
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('sip2_test.log', encoding='utf-8')
    ]
)

logger = logging.getLogger(__name__)

def test_sip2_connection():
    """測試 SIP2 連接和基本功能"""
    logger.info("=== SIP2 連接測試開始 ===")
    
    # 從配置文件讀取設定
    config.reload_config()
    
    host = config.LIBRARY_HOST
    port = config.LIBRARY_PORT
    user = config.LIBRARY_USER if config.LIBRARY_LOGIN_ENABLED else ""
    password = config.LIBRARY_PASS if config.LIBRARY_LOGIN_ENABLED else ""
    institution = config.LIBRARY_INSTITUTION
    
    logger.info(f"連接參數: {host}:{port}, Institution: {institution}")
    logger.info(f"登入模式: {'帳號密碼' if config.LIBRARY_LOGIN_ENABLED else 'IP白名單'}")
    
    try:
        # 創建 SIP2 客戶端
        sip2 = SIP2Client(host, port, user, password, institution_id=institution)
        
        # 1. 測試連接
        logger.info("1. 測試連接...")
        if sip2.connect():
            logger.info("✓ 連接成功")
        else:
            logger.error("✗ 連接失敗")
            return False
            
        # 2. 測試登入
        logger.info("2. 測試登入...")
        if sip2.login():
            logger.info("✓ 登入成功")
        else:
            logger.error("✗ 登入失敗")
            return False
            
        # 3. 測試健康檢查
        logger.info("3. 測試健康檢查...")
        if sip2.health_check():
            logger.info("✓ 健康檢查通過")
        else:
            logger.warning("⚠ 健康檢查失敗（可能是正常的）")
            
        # 4. 測試書籍查詢
        logger.info("4. 測試書籍查詢...")
        test_barcodes = [
            "C261954",  # 測試條碼
            "ping_test",  # 狀態檢查用
            "sample001"   # 範例條碼
        ]
        
        for barcode in test_barcodes:
            logger.info(f"  查詢條碼: {barcode}")
            book_info = sip2.get_book_info(barcode)
            if book_info:
                logger.info(f"  ✓ 書名: {book_info.get('title', 'N/A')}")
                logger.info(f"  ✓ 作者: {book_info.get('author', 'N/A')}")
                logger.info(f"  ✓ 狀態: {book_info.get('status', 'N/A')}")
                logger.info(f"  ✓ 到期日: {book_info.get('due_date', 'N/A')}")
                break  # 找到一個有效回應就停止
            else:
                logger.info(f"  ✗ 查詢失敗或無此書")
                
        # 關閉連接
        sip2.close()
        logger.info("=== 測試完成 ===")
        return True
        
    except Exception as e:
        logger.error(f"測試過程中發生錯誤: {e}")
        return False

def test_network_connectivity():
    """測試網路連通性"""
    import socket
    
    logger.info("=== 網路連通性測試 ===")
    host = config.LIBRARY_HOST
    port = config.LIBRARY_PORT
    
    try:
        # 測試 TCP 連接
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result == 0:
            logger.info(f"✓ 可以連接到 {host}:{port}")
            return True
        else:
            logger.error(f"✗ 無法連接到 {host}:{port}")
            return False
            
    except Exception as e:
        logger.error(f"網路測試錯誤: {e}")
        return False

if __name__ == "__main__":
    print("圖書館系統 SIP2 連接測試")
    print("=" * 40)
    
    # 先測試網路連通性
    if test_network_connectivity():
        # 再測試 SIP2 協議
        test_sip2_connection()
    else:
        print("網路連接失敗，請檢查 IP 地址和端口設定")
        
    print("\n詳細日誌請查看: sip2_test.log")