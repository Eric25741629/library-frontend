"""
/api/homepage/book_check 回歸測試。

首頁閒置時偵測投書口殘留書 + 機器異常狀態。三種異常情境：
  - state ≠ homed/sleeping  → ok=False, reason=abnormal_state（不查 bookok）
  - state=homed + bookok 偵測到書 → ok=False, reason=book_in_chute
  - state=homed + lock 搶不到 → ok=null, reason=machine_busy（不下結論）
"""
from __future__ import annotations

import threading
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


class _FakeMachine:
    """最小化的 machine 替身，只覆蓋 endpoint 真正用到的介面。"""

    def __init__(self, book_present=False, lock=None, check_raises=None):
        self.book_present = book_present
        self.lock = lock if lock is not None else threading.RLock()
        self._check_raises = check_raises

    def check_book_status(self):
        if self._check_raises is not None:
            raise self._check_raises
        return self.book_present


def _mock_status(state):
    return {'machine_state': state}


class TestHomepageBookCheck:
    def test_homed_and_empty_chute_ok(self, client):
        machine = _FakeMachine(book_present=False)
        with patch.object(shared, 'machine', machine), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('homed')):
            rv = client.get('/api/homepage/book_check')
        assert rv.status_code == 200
        body = rv.get_json()
        assert body['ok'] is True
        assert body['book_present'] is False
        assert body['machine_state'] == 'homed'
        assert body['reason'] is None

    def test_sleeping_and_empty_chute_ok(self, client):
        machine = _FakeMachine(book_present=False)
        with patch.object(shared, 'machine', machine), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('sleeping')):
            rv = client.get('/api/homepage/book_check')
        body = rv.get_json()
        assert body['ok'] is True
        assert body['reason'] is None

    def test_book_in_chute_flagged(self, client):
        machine = _FakeMachine(book_present=True)
        with patch.object(shared, 'machine', machine), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('homed')):
            rv = client.get('/api/homepage/book_check')
        body = rv.get_json()
        assert body['ok'] is False
        assert body['book_present'] is True
        assert body['reason'] == 'book_in_chute'
        assert body['machine_state'] == 'homed'

    def test_abnormal_state_opened_skips_bookok(self, client):
        # 異常 state 不該再去打 serial 查 bookok（短路）
        machine = MagicMock()
        machine.check_book_status = MagicMock(side_effect=AssertionError(
            "check_book_status should not be called when state is abnormal"
        ))
        with patch.object(shared, 'machine', machine), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('opened')):
            rv = client.get('/api/homepage/book_check')
        body = rv.get_json()
        assert body['ok'] is False
        assert body['book_present'] is None
        assert body['reason'] == 'abnormal_state'
        assert body['machine_state'] == 'opened'
        machine.check_book_status.assert_not_called()

    def test_abnormal_state_closed_flagged(self, client):
        # state 4 (closed) = 前一輪 return 沒結束乾淨
        machine = _FakeMachine(book_present=False)
        with patch.object(shared, 'machine', machine), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('closed')):
            rv = client.get('/api/homepage/book_check')
        body = rv.get_json()
        assert body['ok'] is False
        assert body['reason'] == 'abnormal_state'

    def test_lock_busy_returns_null_no_conclusion(self, client):
        # 用獨立 thread 持有 lock 模擬「動作進行中」；endpoint 0.5s timeout
        # 搶不到鎖時應該回 machine_busy 而不去打 serial。
        lock = threading.RLock()
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            lock.acquire()
            acquired.set()
            release.wait(timeout=5)
            lock.release()

        t = threading.Thread(target=holder)
        t.start()
        acquired.wait(timeout=2)

        # bookok 被打到就 fail — 拿不到鎖根本不該呼叫
        machine = _FakeMachine(lock=lock, check_raises=AssertionError(
            "check_book_status should not be called when lock is busy"
        ))
        try:
            with patch.object(shared, 'machine', machine), \
                 patch.object(shared, 'get_cached_status', return_value=_mock_status('homed')):
                rv = client.get('/api/homepage/book_check')
            body = rv.get_json()
            assert body['ok'] is None
            assert body['book_present'] is None
            assert body['reason'] == 'machine_busy'
            assert body['machine_state'] == 'homed'
        finally:
            release.set()
            t.join(timeout=2)

    def test_check_book_status_exception_returns_query_failed(self, client):
        machine = _FakeMachine(check_raises=RuntimeError("serial died"))
        with patch.object(shared, 'machine', machine), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('homed')):
            rv = client.get('/api/homepage/book_check')
        body = rv.get_json()
        assert body['ok'] is None
        assert body['book_present'] is None
        assert body['reason'] == 'query_failed'

    def test_machine_none_returns_unavailable(self, client):
        with patch.object(shared, 'machine', None), \
             patch.object(shared, 'get_cached_status', return_value=_mock_status('homed')):
            rv = client.get('/api/homepage/book_check')
        body = rv.get_json()
        assert body['ok'] is None
        assert body['reason'] == 'machine_unavailable'
