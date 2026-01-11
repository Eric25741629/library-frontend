import asyncio
import re
import sys
from typing import Optional, Tuple

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


BASE_SEARCH_URL = "https://www.libwebpac.yuntech.edu.tw/search?searchField=ACN&searchInput={acn}"

# 頁面目前常見格式為：2025/09/08 或 已被外借/2026-02-26 等
# 我們只抽出日期，並轉成 YYYY-MM-DD 形式（例如 2026-02-26）
DATE_PATTERN = re.compile(r"(\d{4}[/-]\d{2}[/-]\d{2})")


def _extract_due_from_text(text: str) -> Optional[str]:
    """從一段文字裡解析出到期「日期」字串，格式為 YYYY-MM-DD。

    只回傳真正的日期，不回傳「已被外借／在架／借出」等狀態文字。
    若沒有偵測到日期，回傳 None。
    """
    if not text:
        return None

    m = DATE_PATTERN.search(text)
    if not m:
        return None

    # 將 2026/02/26 或 2026-02-26 正規化為 2026-02-26
    ymd = m.group(1)
    return ymd.replace("/", "-")


LOCATION_KEYS = ("館藏地/室", "館藏地", "位置", "館藏室")


def _extract_location_from_text(text: str) -> Optional[str]:
    """從一段文字裡解析出『館藏地/室』或『位置』的值。

    嘗試以「：/:", 或空白分隔在關鍵字之後的內容做擷取；
    若沒有偵測到關鍵字，回傳 None。
    """
    if not text:
        return None

    if not any(k in text for k in LOCATION_KEYS):
        return None

    # 形式一：館藏地/室：本館 3F 期刊室
    m = re.search(r"(?:館藏地(?:/室)?|位置)\s*[：:]?\s*(.+?)(?=\s{2,}|[\t\n\r]|到期|借出|在架|索書號|狀態|登錄號|$)", text)
    if m:
        val = m.group(1).strip()
        # 如果抓到的是表格表頭關鍵字，則排除
        if val in ["特藏/用途", "索書號", "狀態/到期日", "登錄號", "預約（人數）"]:
            return None
        return val or None

    # 形式二：館藏地/室 本館 3F 期刊室
    m2 = re.search(r"(?:館藏地(?:/室)?|位置)\s+(.+?)(?=\s{2,}|[\t\n\r]|到期|借出|在架|索書號|狀態|登錄號|$)", text)
    if m2:
        val = m2.group(1).strip()
        if val in ["特藏/用途", "索書號", "狀態/到期日", "登錄號", "預約（人數）"]:
            return None
        return val or None

    return None


async def fetch_due_and_location(acn: str, debug: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """使用 Playwright 查詢登錄號，回傳 (到期日, 館藏地/室)。

    到期日格式為 YYYY-MM-DD；任一項若無法解析，回傳 None。
    """
    url = BASE_SEARCH_URL.format(acn=acn)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not debug)
        page = await browser.new_page()

        if debug:
            print(f"[DEBUG] GOTO: {url}")

        await page.goto(url, wait_until="networkidle", timeout=30000)

        try:
            await page.wait_for_timeout(3000)
            await page.wait_for_selector(f"text={acn}", timeout=15000)
        except PlaywrightTimeoutError:
            if debug:
                print("[DEBUG] Timeout waiting for ACN text.")

        due: Optional[str] = None
        loc: Optional[str] = None

        # === 核心策略：尋找包含 acn 的表格列(row) ===
        try:
            # 找到包含 ACN 文字的元素
            acn_elements = page.get_by_text(acn, exact=True)
            count = await acn_elements.count()
            
            for i in range(count):
                element = acn_elements.nth(i)
                # 向上尋找 tr 元素
                row = element.locator("xpath=ancestor::tr")
                if await row.count() > 0:
                    cells = row.locator("td")
                    cell_count = await cells.count()
                    
                    # 根據使用者提供的圖片結構：
                    # 第 1 欄 (index 0): 館藏地/室 (如：中文書庫區(五樓))
                    # 第 2 欄 (index 1): 特藏/用途 (如：圖書)
                    # 第 3 欄 (index 2): 索書號
                    # 第 4 欄 (index 3): 狀態/到期日 (如：已被外借/2026-02-26)
                    # 第 5 欄 (index 4): 登錄號 (ACN)
                    
                    if cell_count >= 5:
                        loc_text = (await cells.nth(0).inner_text()).strip()
                        due_text = (await cells.nth(3).inner_text()).strip()
                        
                        if debug:
                            print(f"[DEBUG] Found row with {acn}: loc={loc_text!r}, due={due_text!r}")
                            
                        due = _extract_due_from_text(due_text)
                        # 如果是「在架」，_extract_due_from_text 可能回傳 None，這裡直接保留原始文字或處理
                        if not due and "在架" in due_text:
                            due = "在架"
                            
                        loc = loc_text
                        
                        if due and loc:
                            await browser.close()
                            return due, loc

        except Exception as e:
            if debug:
                print(f"[DEBUG] Error in core strategy: {e}")

        # === 備用策略 A：以『到期日』標籤為中心 ===
        try:
            labels = page.get_by_text("到期日", exact=False)
            count = await labels.count()
        except Exception:
            count = 0
        if debug:
            print(f"[DEBUG] elements containing '到期日': {count}")

        for i in range(count):
            node = labels.nth(i)
            try:
                text = (await node.inner_text()).strip()
            except Exception:
                text = ""

            d = _extract_due_from_text(text)
            l = _extract_location_from_text(text)
            if d and not due:
                due = d
            if l and not loc:
                loc = l
            if due and loc:
                await browser.close()
                return due, loc

            # 往父層擴張，提高抓取機率
            parent = node
            for level in range(3):
                parent = parent.locator("xpath=..")
                try:
                    p_text = (await parent.inner_text()).strip()
                except Exception:
                    break

                if not due:
                    d = _extract_due_from_text(p_text)
                    if d:
                        due = d
                if not loc:
                    l = _extract_location_from_text(p_text)
                    if l:
                        loc = l
                if due and loc:
                    await browser.close()
                    return due, loc

        # === 解析策略 B：直接找『館藏地』等關鍵字元素 ===
        for key in LOCATION_KEYS:
            try:
                nodes = page.get_by_text(key, exact=False)
                cnt = await nodes.count()
            except Exception:
                cnt = 0
            if debug:
                print(f"[DEBUG] elements containing {key!r}: {cnt}")
            for i in range(cnt):
                node = nodes.nth(i)
                try:
                    t = (await node.inner_text()).strip()
                except Exception:
                    t = ""
                if not loc:
                    l = _extract_location_from_text(t)
                    if l:
                        loc = l
                if not due:
                    d = _extract_due_from_text(t)
                    if d:
                        due = d
                if due and loc:
                    await browser.close()
                    return due, loc

                # 擴張至父層
                parent = node
                for level in range(3):
                    parent = parent.locator("xpath=..")
                    try:
                        p_text = (await parent.inner_text()).strip()
                    except Exception:
                        break
                    if not loc:
                        l = _extract_location_from_text(p_text)
                        if l:
                            loc = l
                    if not due:
                        d = _extract_due_from_text(p_text)
                        if d:
                            due = d
                    if due and loc:
                        await browser.close()
                        return due, loc

        # === 解析策略 C：全文掃描 ===
        try:
            body_text = await page.inner_text("body")
        except Exception:
            body_text = ""
        if debug:
            print("[DEBUG] body text length:", len(body_text))

        if body_text:
            for raw in body_text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if ("到期" in line or "借出" in line or "在架" in line or "已被外借" in line) and not due:
                    d = _extract_due_from_text(line)
                    if d:
                        due = d
                if any(k in line for k in LOCATION_KEYS) and not loc:
                    l = _extract_location_from_text(line)
                    if l:
                        loc = l
                if due and loc:
                    await browser.close()
                    return due, loc

            # Fallback：取第一個日期作為到期日
            if not due:
                m = DATE_PATTERN.search(body_text)
                if m:
                    due = _extract_due_from_text(m.group(0))

            # Fallback：在全文中尋找館藏地關鍵字後的值
            if not loc:
                l = _extract_location_from_text(body_text)
                if l:
                    loc = l

        await browser.close()
        return due, loc
def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python libwebpac_playwright.py <登錄號或條碼>")
        print("例如: python libwebpac_playwright.py C261954")
        return 1

    acn = sys.argv[1].strip()
    if not acn:
        print("錯誤: 條碼不可為空")
        return 1

    # 若需要觀察實際畫面，把 debug 改成 True
    due, loc = asyncio.run(fetch_due_and_location(acn, debug=False))

    # 輸出兩個欄位：到期日期,館藏地/室（若解析不到則為空字串）
    print(f"{due or ''},{loc or ''}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
