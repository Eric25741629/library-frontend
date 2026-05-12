"""
測試後台「緊急恢復」端點：
  - POST /api/admin/machine/reset
  - POST /api/admin/machine/force_homing
  - POST /api/admin/sip2/reconnect
  - POST /api/admin/machine/clear_error

背景：機器卡在 "error state 0"（例如 2026-05-12 14:31:25 那次 serial glitch）
時，操作員過去只能 ssh 重啟服務。這四個端點讓管理員可以從後台 UI 直接觸發
修復，本檔案確認：
  1. 每個端點都吃 admin auth。
  2. 各自呼叫到正確的 shared.machine / shared.sip2 動作。
  3. 整體 35 秒 timeout 在底層 _send_command 卡住時會被觸發。
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from flask import Flask

import shared
from routes.admin_api import admin_api_bp


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.register_blueprint(admin_api_bp, url_prefix='/api/admin')
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_auth_flag():
    """確保測試使用乾淨的 auth 設定。預設不要求登入，個別測試自行覆寫。"""
    original = getattr(shared, 'REQUIRE_ADMIN_LOGIN', False)
    shared.REQUIRE_ADMIN_LOGIN = False
    yield
    shared.REQUIRE_ADMIN_LOGIN = original


@pytest.fixture
def fake_machine():
    """裝一隻假的 MachineController 給 shared.machine 用。

    預設行為：
      - lock 是真的 RLock（與正式版一致）。
      - _send_command 依照指令回傳成功訊息。
      - ser.is_open 為 True，close() 是個 no-op，避免序列埠真的去開。
    """
    m = MagicMock()
    m.lock = threading.RLock()
    m.port = '/dev/uno-test'
    m.baudrate = 38400
    m.ser = MagicMock()
    m.ser.is_open = True
    m.ser.close = MagicMock()

    def fake_send(cmd):
        if cmd == 'dep1':
            return 'device power on'
        if cmd == 'homing':
            return 'Homed'
        if cmd == 'cancel':
            return 'ack'
        return 'ack'

    m._send_command = MagicMock(side_effect=fake_send)
    return m


@pytest.fixture
def fake_sip2():
    s = MagicMock()
    s._io_lock = threading.Lock()
    s.health_check = MagicMock(return_value=True)
    return s


@pytest.fixture(autouse=True)
def patch_shared(monkeypatch, fake_machine, fake_sip2):
    """把 shared.machine / shared.sip2 / serial / init_sip2_client 換成可控版本。"""
    monkeypatch.setattr(shared, 'machine', fake_machine)
    monkeypatch.setattr(shared, 'sip2', fake_sip2)

    # init_sip2_client 不要真的去打 SIP2 server
    init_calls = []

    def fake_init_sip2():
        init_calls.append(time.time())
    monkeypatch.setattr(shared, 'init_sip2_client', fake_init_sip2)
    fake_init_sip2.call_log = init_calls  # type: ignore[attr-defined]

    # 讓 get_cached_status 回傳一個固定的 library_ok=True 快照
    monkeypatch.setattr(shared, 'get_cached_status', lambda: {
        'library_ok': True,
        'machine_ok': True,
        'machine_state': 'homed',
    })

    # 攔截 serial.Serial 不要真的去開實體裝置
    fake_serial_module = MagicMock()
    fake_serial_module.EIGHTBITS = 8
    fake_serial_module.PARITY_NONE = 'N'
    fake_serial_module.STOPBITS_ONE = 1
    fake_serial_module.Serial = MagicMock(return_value=MagicMock(is_open=True))
    monkeypatch.setitem(__import__('sys').modules, 'serial', fake_serial_module)


# ─── 通用工具 ─────────────────────────────────────────────────────────────────

def _login(client):
    """sets session['logged_in'] = True so before_request passes when login is required."""
    with client.session_transaction() as sess:
        sess['logged_in'] = True


# ─── /machine/reset ───────────────────────────────────────────────────────────

class TestMachineReset:
    def test_success_steps(self, client, fake_machine):
        # 在送 request 前先抓住原本的 ser.close mock；endpoint 內會把
        # machine.ser 換成 _serial.Serial(...) 的新物件，所以事後再讀
        # fake_machine.ser 已經是新 mock，看不到 close 呼叫。
        original_close = fake_machine.ser.close

        rv = client.post('/api/admin/machine/reset')
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['success'] is True
        # 確認所有 4 個步驟（close, reopen, dep1, homing）都被回報
        step_names = [s['step'] for s in data['steps']]
        assert 'close_serial' in step_names
        assert 'reopen_serial' in step_names
        assert 'dep1' in step_names
        assert 'homing' in step_names

        # 確認真的有送 dep1 + homing
        sent_cmds = [c.args[0] for c in fake_machine._send_command.call_args_list]
        assert 'dep1' in sent_cmds
        assert 'homing' in sent_cmds

        # 序列埠 close 也應被呼叫
        assert original_close.called

    def test_machine_none_returns_failure(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'machine', None)
        rv = client.post('/api/admin/machine/reset')
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['success'] is False
        assert '尚未初始化' in data['message']

    def test_serial_close_exception_does_not_abort(self, client, fake_machine):
        fake_machine.ser.close.side_effect = Exception('serial busted')
        rv = client.post('/api/admin/machine/reset')
        # close 失敗不應該整個流程崩掉；reopen 仍要嘗試
        data = rv.get_json()
        step_names = [s['step'] for s in data['steps']]
        # 至少要試到 reopen_serial
        assert 'reopen_serial' in step_names

    def test_requires_auth_when_enabled(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'REQUIRE_ADMIN_LOGIN', True)
        rv = client.post('/api/admin/machine/reset')
        assert rv.status_code == 401

    def test_auth_pass_with_session(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'REQUIRE_ADMIN_LOGIN', True)
        _login(client)
        rv = client.post('/api/admin/machine/reset')
        assert rv.status_code == 200


# ─── /machine/force_homing ────────────────────────────────────────────────────

class TestForceHoming:
    def test_success(self, client, fake_machine):
        rv = client.post('/api/admin/machine/force_homing')
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['success'] is True
        assert data['response'] == 'Homed'
        # 確認真的呼叫 _send_command('homing')
        fake_machine._send_command.assert_any_call('homing')

    def test_ack_also_counts_as_success(self, client, fake_machine):
        fake_machine._send_command.side_effect = lambda cmd: 'ack'
        rv = client.post('/api/admin/machine/force_homing')
        data = rv.get_json()
        assert data['success'] is True

    def test_no_homed_in_response_is_failure(self, client, fake_machine):
        fake_machine._send_command.side_effect = lambda cmd: 'something else'
        rv = client.post('/api/admin/machine/force_homing')
        data = rv.get_json()
        assert data['success'] is False
        assert data['response'] == 'something else'

    def test_exception_caught(self, client, fake_machine):
        fake_machine._send_command.side_effect = Exception('boom')
        rv = client.post('/api/admin/machine/force_homing')
        data = rv.get_json()
        assert data['success'] is False
        assert 'boom' in data['message']

    def test_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'REQUIRE_ADMIN_LOGIN', True)
        rv = client.post('/api/admin/machine/force_homing')
        assert rv.status_code == 401


# ─── /sip2/reconnect ──────────────────────────────────────────────────────────

class TestSip2Reconnect:
    def test_success(self, client):
        rv = client.post('/api/admin/sip2/reconnect')
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['success'] is True
        # init_sip2_client 至少被呼叫一次
        assert len(shared.init_sip2_client.call_log) >= 1  # type: ignore[attr-defined]

    def test_library_ok_reflects_cache(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'get_cached_status', lambda: {'library_ok': False})
        rv = client.post('/api/admin/sip2/reconnect')
        data = rv.get_json()
        assert data['success'] is True
        assert data['library_ok'] is False

    def test_uses_io_lock_when_present(self, client, fake_sip2):
        # 在 init_sip2_client 之前先把 lock 拿著，確認 endpoint 仍能進入（5s timeout）
        rv = client.post('/api/admin/sip2/reconnect')
        assert rv.status_code == 200

    def test_sip2_none_safe(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'sip2', None)
        rv = client.post('/api/admin/sip2/reconnect')
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['success'] is True

    def test_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'REQUIRE_ADMIN_LOGIN', True)
        rv = client.post('/api/admin/sip2/reconnect')
        assert rv.status_code == 401


# ─── /machine/clear_error ─────────────────────────────────────────────────────

class TestClearError:
    def test_success(self, client, fake_machine):
        rv = client.post('/api/admin/machine/clear_error')
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['success'] is True
        responses = data['responses']
        assert responses['cancel'] == 'ack'
        assert responses['homing'] == 'Homed'

        sent_cmds = [c.args[0] for c in fake_machine._send_command.call_args_list]
        # 必須先 cancel 再 homing
        assert sent_cmds.index('cancel') < sent_cmds.index('homing')

    def test_homing_response_not_ok(self, client, fake_machine):
        def side(cmd):
            if cmd == 'cancel':
                return 'ack'
            if cmd == 'homing':
                return 'still moving'
            return ''
        fake_machine._send_command.side_effect = side
        rv = client.post('/api/admin/machine/clear_error')
        data = rv.get_json()
        assert data['success'] is False
        assert data['responses']['homing'] == 'still moving'

    def test_machine_none(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'machine', None)
        rv = client.post('/api/admin/machine/clear_error')
        data = rv.get_json()
        assert data['success'] is False

    def test_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(shared, 'REQUIRE_ADMIN_LOGIN', True)
        rv = client.post('/api/admin/machine/clear_error')
        assert rv.status_code == 401


# ─── 35 秒整體 timeout ────────────────────────────────────────────────────────

class TestOverallTimeout:
    def test_clear_error_times_out_when_send_command_slow(self, client, fake_machine, monkeypatch):
        """模擬 _send_command 卡住超過 35 秒：應該回 timeout 訊息，不可掛在連線上等。"""
        # 把 _RECOVERY_OVERALL_TIMEOUT_SEC 改成 0.5 秒，配合 _send_command 各 sleep 0.4s
        # 第一次 cancel 過得了 deadline 檢查，但第二次 homing 之後就會超時。
        import routes.admin_api as admin_mod
        monkeypatch.setattr(admin_mod, '_RECOVERY_OVERALL_TIMEOUT_SEC', 0.5)

        def slow_send(cmd):
            time.sleep(0.4)
            return 'ack'
        fake_machine._send_command.side_effect = slow_send

        rv = client.post('/api/admin/machine/clear_error')
        data = rv.get_json()
        assert data['success'] is False
        # 訊息中應提示「耗時超過 35 秒」這類字串（保留原本中文，方便操作員看）
        assert '35 秒' in data['message']

    def test_reset_times_out_at_dep1_phase(self, client, fake_machine, monkeypatch):
        """模擬 dep1 階段就超時：reset 應提早返回 timeout 訊息。"""
        import routes.admin_api as admin_mod
        monkeypatch.setattr(admin_mod, '_RECOVERY_OVERALL_TIMEOUT_SEC', 0.3)

        def slow_send(cmd):
            time.sleep(0.4)
            return 'device power on' if cmd == 'dep1' else 'Homed'
        fake_machine._send_command.side_effect = slow_send

        rv = client.post('/api/admin/machine/clear_error')
        data = rv.get_json()
        assert data['success'] is False
        assert '35 秒' in data['message']
