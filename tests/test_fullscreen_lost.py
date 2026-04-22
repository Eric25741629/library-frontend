"""
回歸測試：前端 /api/frontend/fullscreen_lost 必須不會重現 2026-04-21 18:21 的
重啟雪崩（連續觸發造成 Firefox 起不來）。

場景對應：使用者問「如果今天並非全螢幕時，會不會再次觸發 bug」。
Bug 的定義 = 在短時間內觸發多次 systemctl restart，讓 Firefox 被殺太快。
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import shared
from routes.api import api_bp


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(api_bp, url_prefix='/api')
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每個測試前後重置 api 模組的全域狀態，確保測試獨立。"""
    import routes.api as api_mod
    api_mod._FRONTEND_RESTART_IN_PROGRESS = False
    api_mod._FRONTEND_RESTART_LAST_TS = 0.0
    shared.clear_manual_frontend_stop()
    yield
    api_mod._FRONTEND_RESTART_IN_PROGRESS = False
    api_mod._FRONTEND_RESTART_LAST_TS = 0.0
    shared.clear_manual_frontend_stop()


@pytest.fixture
def mock_run():
    """攔截 subprocess.run，讓測試不會真的重啟 systemd。"""
    with patch('routes.api.subprocess.run') as m:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ''
        result.stderr = ''
        m.return_value = result
        yield m


def _wait_for_restart(mock_run, timeout=3.0):
    """等待背景 thread 真的呼叫了 subprocess.run（最多 timeout 秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock_run.call_count >= 1:
            return True
        time.sleep(0.05)
    return False


def _wait_restart_done(timeout=3.0):
    """等到 _FRONTEND_RESTART_IN_PROGRESS 回 False。"""
    import routes.api as api_mod
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not api_mod._FRONTEND_RESTART_IN_PROGRESS:
            return True
        time.sleep(0.05)
    return False


class TestSingleCall:
    def test_single_post_schedules_one_restart(self, client, mock_run):
        rv = client.post('/api/frontend/fullscreen_lost',
                         json={'trigger': 'fullscreenchange'})
        assert rv.status_code == 200
        assert rv.json['scheduled'] is True
        assert _wait_for_restart(mock_run), "背景 thread 沒有呼叫 subprocess.run"
        assert mock_run.call_count == 1

    def test_only_kiosk_browser_restarted_not_pyserver(self, client, mock_run):
        """pyserver 不應該被重啟 — 否則 Flask 自己斷線，且無必要。"""
        client.post('/api/frontend/fullscreen_lost', json={'trigger': 't'})
        assert _wait_for_restart(mock_run)
        args = mock_run.call_args[0][0]
        assert 'kiosk-browser.service' in args
        assert 'pyserver.service' not in args, (
            f"pyserver 不應被重啟，但實際執行了：{args}"
        )


class TestDebounce:
    def test_rapid_fire_10_posts_triggers_at_most_one_restart(self, client, mock_run):
        """這是 2026-04-21 bug 的核心回歸測試：快速連續觸發不可重啟多次。"""
        responses = [
            client.post('/api/frontend/fullscreen_lost', json={'trigger': f't{i}'})
            for i in range(10)
        ]
        # 第一個 schedule，其他應該被 in-progress lock / cooldown 擋掉。
        scheduled = [r for r in responses if r.json.get('scheduled')]
        skipped = [r for r in responses if r.json.get('skipped')]
        assert len(scheduled) == 1, f"預期只有一次 scheduled，實際 {len(scheduled)}"
        assert len(skipped) == 9

        assert _wait_for_restart(mock_run)
        assert _wait_restart_done()
        # 即便 thread 完成後，call count 仍只有 1。
        assert mock_run.call_count == 1

    def test_in_progress_marks_second_call_skipped(self, client, mock_run):
        """第一次進行中，第二次必須被 restart_in_progress 擋掉。"""
        # 讓 subprocess.run 卡住，模擬 systemctl 還在執行
        slow_event = threading.Event()
        mock_run.side_effect = lambda *a, **kw: (slow_event.wait(timeout=5)
                                                   or MagicMock(returncode=0,
                                                                stdout='', stderr=''))
        try:
            r1 = client.post('/api/frontend/fullscreen_lost', json={'trigger': 'a'})
            # 等 thread 真的進入 subprocess.run
            for _ in range(30):
                if mock_run.call_count >= 1:
                    break
                time.sleep(0.05)
            r2 = client.post('/api/frontend/fullscreen_lost', json={'trigger': 'b'})
            assert r1.json.get('scheduled') is True
            assert r2.json.get('skipped') is True
            assert r2.json.get('reason') == 'restart_in_progress'
        finally:
            slow_event.set()
            _wait_restart_done()


class TestCooldown:
    def test_second_call_within_cooldown_is_skipped(self, client, mock_run):
        """第一次重啟完成後 15 秒冷卻期內，第二次應被擋掉。"""
        r1 = client.post('/api/frontend/fullscreen_lost', json={'trigger': 'a'})
        assert r1.json.get('scheduled')
        assert _wait_for_restart(mock_run)
        assert _wait_restart_done()

        # 立刻第二次（<15s）
        r2 = client.post('/api/frontend/fullscreen_lost', json={'trigger': 'b'})
        assert r2.json.get('skipped') is True
        assert r2.json.get('reason') == 'cooldown'
        assert mock_run.call_count == 1

    def test_call_after_cooldown_expires_schedules_again(self, client, mock_run):
        """冷卻期過了，新的觸發應該重新排程。"""
        import routes.api as api_mod
        client.post('/api/frontend/fullscreen_lost', json={'trigger': 'a'})
        assert _wait_for_restart(mock_run)
        assert _wait_restart_done()

        # 把 last_ts 往前撥 16 秒，等於 cooldown 過了
        api_mod._FRONTEND_RESTART_LAST_TS = time.time() - 16

        r2 = client.post('/api/frontend/fullscreen_lost', json={'trigger': 'b'})
        assert r2.json.get('scheduled') is True
        # 等第二次 restart 發生
        deadline = time.time() + 3
        while time.time() < deadline and mock_run.call_count < 2:
            time.sleep(0.05)
        assert mock_run.call_count == 2


class TestManualStop:
    def test_manual_stop_active_suppresses_restart(self, client, mock_run):
        shared.mark_manual_frontend_stop()
        try:
            rv = client.post('/api/frontend/fullscreen_lost', json={'trigger': 't'})
            assert rv.json.get('skipped') is True
            assert rv.json.get('reason') == 'manual_stop_active'
        finally:
            shared.clear_manual_frontend_stop()
        # 給可能的背景 thread 時間（不應該有）
        time.sleep(0.3)
        assert mock_run.call_count == 0
