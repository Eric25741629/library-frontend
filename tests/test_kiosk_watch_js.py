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


class TestBadEventListenersMustNotReturn:
    """這些是 2026-04-21 之前觸發雪崩的事件。永遠不能放回來。"""

    def test_no_blur_listener(self, js):
        # blur 在條碼掃描器切換焦點時會觸發 — 不代表離開全螢幕。
        assert not re.search(r"addEventListener\(\s*['\"]blur['\"]", js), (
            "blur 事件會被條碼掃描器誤觸發，不可拿來判斷全螢幕")

    def test_no_focus_listener(self, js):
        assert not re.search(r"addEventListener\(\s*['\"]focus['\"]", js), (
            "focus 不代表全螢幕，不可當監控訊號")

    def test_no_visibility_visible_trigger(self, js):
        """visibilitychange 允許，但只能在 hidden 方向觸發檢查。"""
        # 找出所有 visibilityState 比較
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
