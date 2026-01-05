#!/usr/bin/env python3
"""
圖書館系統配置工具
用於設定和測試圖書館系統連接
"""
import yaml
import socket
from pathlib import Path

CONFIG_FILE = Path('config.yml')

def load_config():
    """載入當前配置"""
    if CONFIG_FILE.exists():
        try:
            return yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}
        except Exception as e:
            print(f"讀取配置錯誤: {e}")
            return {}
    return {}

def save_config(config):
    """儲存配置"""
    try:
        CONFIG_FILE.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True), encoding='utf-8')
        print("✓ 配置已儲存")
        return True
    except Exception as e:
        print(f"儲存配置錯誤: {e}")
        return False

def test_connection(host, port):
    """測試網路連接"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def main():
    print("圖書館系統配置工具")
    print("=" * 40)
    
    config = load_config()
    library_config = config.setdefault('library', {})
    
    # 顯示當前配置
    print(f"當前配置:")
    print(f"  主機: {library_config.get('host', '未設定')}")
    print(f"  端口: {library_config.get('port', '未設定')}")
    print(f"  模擬模式: {library_config.get('mock_enabled', True)}")
    print(f"  還書功能: {'啟用' if library_config.get('checkin_enabled', False) else '停用'}")
    print(f"  機構ID: {library_config.get('institution_id', 'MAIN')}")
    print()
    
    while True:
        print("選項:")
        print("1. 設定圖書館伺服器 IP 和端口")
        print("2. 切換模擬/真實模式")
        print("3. 切換還書功能開關")
        print("4. 設定機構 ID")
        print("5. 測試連接")
        print("6. 儲存並退出")
        print("7. 退出（不儲存）")
        
        choice = input("請選擇 (1-7): ").strip()
        
        if choice == '1':
            print("設定圖書館伺服器")
            print("常見配置:")
            print("  - 本地測試: 127.0.0.1")
            print("  - 校園網路: 192.168.x.x") 
            print("  - 圖書館伺服器: (請向IT部門確認)")
            
            host = input(f"請輸入 IP 地址 [{library_config.get('host', '192.168.1.100')}]: ").strip()
            if not host:
                host = library_config.get('host', '192.168.1.100')
                
            port_str = input(f"請輸入端口 [{library_config.get('port', 6001)}]: ").strip()
            if not port_str:
                port = library_config.get('port', 6001)
            else:
                try:
                    port = int(port_str)
                except ValueError:
                    print("無效的端口號")
                    continue
                    
            library_config['host'] = host
            library_config['port'] = port
            print(f"✓ 設定為 {host}:{port}")
            
        elif choice == '2':
            current_mock = library_config.get('mock_enabled', True)
            new_mock = not current_mock
            library_config['mock_enabled'] = new_mock
            mode = "模擬模式" if new_mock else "真實連接"
            print(f"✓ 切換為 {mode}")
            
        elif choice == '3':
            current_checkin = library_config.get('checkin_enabled', False)
            new_checkin = not current_checkin
            library_config['checkin_enabled'] = new_checkin
            status = "啟用" if new_checkin else "停用"
            print(f"✓ 還書功能已{status}")
            if new_checkin:
                print("  ⚠️  警告: 還書功能已啟用，確認還書時會真的歸還到圖書館系統")
            else:
                print("  ℹ️  資訊: 還書功能已停用，只會進行查詢但不會真的歸還")
            
        elif choice == '4':
            current_inst = library_config.get('institution_id', 'MAIN')
            new_inst = input(f"請輸入機構 ID [{current_inst}]: ").strip()
            if new_inst:
                library_config['institution_id'] = new_inst
                print(f"✓ 機構 ID 設定為 {new_inst}")
                
        elif choice == '5':
            host = library_config.get('host')
            port = library_config.get('port')
            if not host or not port:
                print("✗ 請先設定主機和端口")
                continue
                
            print(f"測試連接到 {host}:{port}...")
            if test_connection(host, port):
                print("✓ 連接成功")
            else:
                print("✗ 連接失敗")
                print("  可能原因:")
                print("  - IP 地址錯誤")
                print("  - 端口錯誤")
                print("  - 圖書館系統未啟動")
                print("  - 防火牆阻擋")
                
        elif choice == '6':
            if save_config(config):
                print("配置已儲存，程式將重新載入配置")
                break
                
        elif choice == '7':
            print("退出（未儲存變更）")
            break
            
        else:
            print("無效選項，請重新選擇")
        
        print()

if __name__ == "__main__":
    main()