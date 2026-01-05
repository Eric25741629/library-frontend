
import time
import asyncio
from playwright.async_api import async_playwright

async def check_book_status(book_id):
    # 設定網址：直接使用你提供的 bookDetail 結構
    # book_id 是網址中間那個數字 (例如 409184)
    url = f"https://www.libwebpac.yuntech.edu.tw/bookDetail/{book_id}"
    
    print(f"[*] 啟動瀏覽器查詢系統號: {book_id}")
    print(f"[*] 目標網址: {url}")

    async with async_playwright() as p:
        # 啟動 Chromium 瀏覽器 (無頭模式)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-dev-shm-usage"
            ]
        )
        
        try:
            # 創建新頁面並設定 User-Agent
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
            
            print("[*] 正在前往目標網址...")
            # 前往網頁
            await page.goto(url, wait_until="domcontentloaded")

            # 關鍵步驟：等待網頁上的「表格」載入完成
            print("[*] 正在等待資料載入...")
            
            try:
                # 等待表格行出現，最多等 10 秒
                await page.wait_for_selector("tr", timeout=10000)
                
                # 抓取所有表格內容
                rows = await page.query_selector_all("tr")
                
                print("\n" + "="*50)
                print(f"查詢結果 (系統號 {book_id})")
                print("="*50)
                
                found_data = False
                for row in rows:
                    text = await row.inner_text()
                    text = text.strip()
                    # 過濾掉空白行或標題
                    if not text:
                        continue
                        
                    # 這裡列印出每一行資料，你可以根據需要篩選
                    # 例如：只顯示包含 'C261954' 或 '到期日' 的行
                    print(f"[Row] {text}")
                    
                    if "到期日" in text or "在架" in text or "借出" in text:
                        found_data = True

                if not found_data:
                    print("[!] 警告：有抓到表格，但沒看到明顯的狀態關鍵字，請檢查輸出。")
                    
            except Exception as timeout_e:
                print(f"[!] 等待表格載入超時: {timeout_e}")
                print("[!] 嘗試抓取頁面所有文字...")
                
                # 備用方案：直接抓取整個頁面內容
                page_content = await page.inner_text("body")
                print(f"[Page Content] {page_content}")

        except Exception as e:
            print(f"[!] 發生錯誤: {e}")
            print("[!] 建議：請檢查網路連線或目標網址是否正確。")
        finally:
            await browser.close()
            print("="*50)

async def main():
    # 輸入網址上的那個數字 ID
    # 你的網址是 .../bookDetail/409184... 所以這裡是 409184
    input_id = input("請輸入書籍編號 (URL中的數字ID, 預設 409184): ")
    if not input_id:
        input_id = "C261954"
    
    await check_book_status(input_id)

if __name__ == "__main__":
    asyncio.run(main())