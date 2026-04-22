"""
前端 kiosk_watch.js 合約測試：用靜態分析確保未來不會把造成
2026-04-21 18:21 雪崩的壞 pattern 放回來。

為什麼不用瀏覽器跑 JS：
- 開發機沒有 node.js，headless browser 在此專案屬過度工程。
- 我們要防的是「程式碼裡不該出現特定 pattern」— 這純文字檢查就夠。
- 行為正確性由 backend pytest 覆蓋（收到 POST 後不會雪崩）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

JS_PATH = Path(__file__).resolve().parent.parent / 'static' / 'kiosk_watch.js'


@pytest.fixture(scope='module')
def js() -> str:
    return JS_PATH.read_text(encoding='utf-8')


class TestEndpointContract:
    def test_posts_to_correct_endpoint(self, js):
        assert "'/api/frontend/fullscreen_lost'" in js, (
            "前端必須 POST 到 /api/frontend/fullscreen_lost")

    def test_uses_post_method(self, js):
        assert re.search(r"method:\s*'POST'", js), "必須用 POST 方法"


class TestDuplicateSuppression:
    def test_has_notified_guard(self, js):
        """notified 旗標必須存在，防止同一次離開全螢幕發出多次請求。"""
        assert re.search(r'let\s+notified\s*=\s*false', js), (
            "notified 旗標不見了 — 會讓同一次事件觸發多個 fetch")

    def test_notify_checks_flag_before_fetch(self, js):
        """notify() 函式內必須先檢查 notified 才呼叫 fetch。"""
        m = re.search(r'async function notify\([^)]*\)\s*\{(.*?)^\s*\}',
                      js, re.DOTALL | re.MULTILINE)
        assert m, "找不到 notify 函式定義"
        body = m.group(1)
        # guard 要在 fetch 之前
        guard_pos = body.find('if (notified)')
        fetch_pos = body.find('fetch(')
        assert guard_pos != -1, "notify() 裡沒有 notified 守衛"
        assert guard_pos < fetch_pos, "notified 守衛必須在 fetch 之前"


class TestFocusTracking:
    """
    Kiosk 威脅模型：焦點離開瀏覽器 = 有人可以操作上層視窗（file manager、
    USB popup 等），必須觸發重啟。但要避開 4/21 雪崩路徑 — 所以要用
    JS 的 blur event + document.hasFocus() 二次確認 + 啟動緩衝，
    而不是舊版的 X11 xdotool getactivewindow 連發輪詢。
    """

    def test_has_blur_listener(self, js):
        assert re.search(r"addEventListener\(\s*['\"]blur['\"]", js), (
            "kiosk 必須監聽 window.blur 才能偵測焦點離開瀏覽器")

    def test_blur_handler_uses_hasFocus_confirmation(self, js):
        """單一 blur 事件不可直接觸發重啟 — 必須延遲後用 document.hasFocus() 二次確認。"""
        assert 'document.hasFocus()' in js, (
            "必須用 document.hasFocus() 二次確認，避免瞬間 blur 誤判")

    def test_blur_handler_has_confirmation_delay(self, js):
        """blur → 二次確認之間要有延遲（容忍短暫焦點抖動），至少 1.5 秒。"""
        # blur listener 內 setTimeout 的延遲參數可以是字面數字或常數名
        m = re.search(
            r"addEventListener\(\s*['\"]blur['\"].*?setTimeout\([^,]+,\s*([A-Za-z_][\w]*|\d+)\s*\)",
            js, re.DOTALL)
        assert m, "blur 監聽內必須有 setTimeout 做延遲二次確認"
        raw = m.group(1)
        if raw.isdigit():
            delay = int(raw)
        else:
            # 若是常數名稱（如 BLUR_CONFIRM_MS），從宣告處解析數值
            const_m = re.search(
                rf"(?:const|let|var)\s+{re.escape(raw)}\s*=\s*(\d+)", js)
            assert const_m, f"找不到常數 {raw} 的宣告"
            delay = int(const_m.group(1))
        assert delay >= 1500, (
            f"blur 確認延遲 {delay}ms 太短，可能誤判條碼掃描器等瞬間焦點切換")

    def test_startup_grace_period_exists(self, js):
        """Firefox 剛啟動的前若干秒可能尚未拿到焦點，必須有啟動緩衝期。"""
        m = re.search(r"STARTUP_GRACE_MS\s*=\s*(\d+)", js)
        assert m, "必須定義 STARTUP_GRACE_MS 啟動緩衝期"
        grace = int(m.group(1))
        assert grace >= 10000, (
            f"啟動緩衝 {grace}ms 太短，Firefox 進 fullscreen + grab focus 需要時間")

    def test_no_visibility_visible_trigger(self, js):
        """visibilitychange 允許，但只能在 hidden 方向觸發檢查。"""
        matches = re.findall(r"visibilityState\s*===?\s*['\"](\w+)['\"]", js)
        if matches:
            assert 'visible' not in matches, (
                "不可在 visibilityState === 'visible' 時觸發 fullscreen 檢查")


class TestTimingGuards:
    """啟動期時序 — 避免 Firefox 還沒進入全螢幕就被誤判。"""

    def test_initial_load_delay_at_least_5s(self, js):
        """頁面剛載入時 Firefox --kiosk 可能還在 transition，給至少 5 秒緩衝。"""
        m = re.search(r"scheduleCheck\(\s*['\"]initial-load['\"]\s*,\s*(\d+)\s*\)", js)
        assert m, "沒找到 initial-load 觸發"
        delay = int(m.group(1))
        assert delay >= 5000, f"initial-load 延遲 {delay}ms 太短，可能誤觸發"

    def test_resize_delay_at_least_2s(self, js):
        """kiosk 啟動時 window 尺寸會跳動，resize 要給緩衝。"""
        # 找 addEventListener('resize', ...) 那行，抓裡面 scheduleCheck 的第二個引數
        m = re.search(
            r"addEventListener\(\s*['\"]resize['\"].*?scheduleCheck\([^,]+,\s*(\d+)",
            js, re.DOTALL)
        assert m, "沒找到 resize → scheduleCheck 的呼叫"
        delay = int(m.group(1))
        assert delay >= 2000, f"resize 延遲 {delay}ms 太短"


class TestFullscreenDetection:
    def test_checks_document_fullscreen_element(self, js):
        assert 'document.fullscreenElement' in js

    def test_has_viewport_fallback(self, js):
        """Firefox --kiosk 不一定設 fullscreenElement — 尺寸 fallback 必須存在。"""
        assert 'window.innerWidth' in js
        assert 'window.innerHeight' in js
        assert 'screen.width' in js or 'screen.availWidth' in js
