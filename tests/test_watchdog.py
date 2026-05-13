"""
Watchdog 自動恢復回歸測試。

對應 2026-05-12 早上 09:20-09:34 的事件：
  1. /dev/uno serial 連續空回應，導致機器看似卡死，當天靠人工 4 次重啟才修好。
  2. SIP2 連線掉線數分鐘，現有 reconnect-on-next-call 太慢。

Watchdog 設計：
  - 觀察 _state_poll_thread 累積的 `_consecutive_empty_state` /
    `_consecutive_sip2_fail` 計數器。
  - 連續超過閾值且不在冷卻窗口內，呼叫對應的恢復函式。
  - 每種恢復動作各有獨立的 12 小時冷卻。
  - 冷卻期間第一次達標只 log 一次，不重複噴。
  - `watchdog.auto_recover_*: false` 時只 log，不執行恢復動作。
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

import shared


# ─── Fixtures ─────────────────────────────────────────────────────────────────

class _FakeSerial:
    """模擬 pyserial.Serial 物件。"""
    def __init__(self, is_open=True):
        self.is_open = is_open
        self.closed_count = 0

    def close(self):
        self.is_open = False
        self.closed_count += 1


class _FakeMachine:
    """模擬 MachineController，供 watchdog 恢復流程使用。"""
    def __init__(self, status_response=""):
        self.lock = threading.RLock()
        self.port = '/dev/uno'
        self.baudrate = 9600
        self.simulate = True  # 不嘗試真的開 serial
        self.ser = _FakeSerial(is_open=True)
        self._status_response = status_response
        self.commands_sent = []
        self.homing_in_progress = False
        self.is_homed = False
        self.is_sleeping = False

    def get_status(self):
        return self._status_response

    def _send_command(self, cmd):
        self.commands_sent.append(cmd)
        return "ack"


class _FakeSip2:
    """模擬 SIP2Client。health_check_result 控制回傳。"""
    def __init__(self, health_check_result=False):
        self._io_lock = threading.Lock()
        self.health_check_result = health_check_result
        self.health_check_calls = 0
        self.close_calls = 0

    def health_check(self):
        self.health_check_calls += 1
        return self.health_check_result

    def close(self):
        self.close_calls += 1


@pytest.fixture
def fake_machine():
    return _FakeMachine(status_response="")


@pytest.fixture
def fake_sip2():
    return _FakeSip2(health_check_result=False)


@pytest.fixture(autouse=True)
def _reset_watchdog_state():
    """每個測試前後重置 shared 模組裡的 watchdog 全域狀態。"""
    shared._watchdog_reset_counters()
    shared._watchdog_machine_cooldown_until = 0.0
    shared._watchdog_sip2_cooldown_until = 0.0
    shared._watchdog_machine_cooldown_logged = False
    shared._watchdog_sip2_cooldown_logged = False
    shared.WATCHDOG_AUTO_RECOVER_MACHINE = True
    shared.WATCHDOG_AUTO_RECOVER_SIP2 = True
    # 把啟動寬限期挪到很久以前，否則 _watchdog_tick 會因為「啟動 60s 內」直接 return
    _orig_start_time = shared._watchdog_start_time
    shared._watchdog_start_time = 0.0
    yield
    shared._watchdog_reset_counters()
    shared._watchdog_machine_cooldown_until = 0.0
    shared._watchdog_sip2_cooldown_until = 0.0
    shared._watchdog_machine_cooldown_logged = False
    shared._watchdog_sip2_cooldown_logged = False
    shared.WATCHDOG_AUTO_RECOVER_MACHINE = True
    shared._watchdog_start_time = _orig_start_time
    shared.WATCHDOG_AUTO_RECOVER_SIP2 = True


# ─── Counter tests ────────────────────────────────────────────────────────────

class TestCounters:
    def test_empty_machine_response_increments(self):
        shared._update_machine_empty_counter("")
        shared._update_machine_empty_counter(None)
        shared._update_machine_empty_counter("  ")  # 空白也算
        assert shared._consecutive_empty_state == 3

    def test_non_empty_response_resets_machine_counter(self):
        shared._update_machine_empty_counter("")
        shared._update_machine_empty_counter("")
        assert shared._consecutive_empty_state == 2
        shared._update_machine_empty_counter("homed")
        assert shared._consecutive_empty_state == 0

    def test_busy_response_neither_increments_nor_resets(self):
        # 'busy' = _send_command 在 state 指令拿不到 lock 時的回傳值；
        # watchdog 不該把它當卡死（會在長動作結束瞬間誤觸發 recovery），
        # 也不該把它當「正常回應」清掉真正卡住的累計。
        shared._update_machine_empty_counter("")
        shared._update_machine_empty_counter("")
        assert shared._consecutive_empty_state == 2
        shared._update_machine_empty_counter("busy")
        shared._update_machine_empty_counter("busy")
        shared._update_machine_empty_counter("busy")
        assert shared._consecutive_empty_state == 2  # 不動
        shared._update_machine_empty_counter("homed")
        assert shared._consecutive_empty_state == 0

    def test_sip2_fail_increments_then_resets(self):
        shared._update_sip2_fail_counter(False)
        shared._update_sip2_fail_counter(False)
        shared._update_sip2_fail_counter(False)
        assert shared._consecutive_sip2_fail == 3
        shared._update_sip2_fail_counter(True)
        assert shared._consecutive_sip2_fail == 0


# ─── _watchdog_tick: startup grace ────────────────────────────────────────────

class TestStartupGrace:
    """啟動寬限期間 watchdog 不該觸發 recovery，避免 machine init 本身的
    state-empty 過渡期被誤判為卡住。"""

    def test_within_grace_does_not_trigger(self, fake_machine, fake_sip2):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, '_watchdog_recover_machine') as mock_m, \
             patch.object(shared, '_watchdog_recover_sip2') as mock_s:
            # 模擬程序剛啟動：start_time = now - 10s（小於 60s 寬限期）
            now = 12345.0
            shared._watchdog_start_time = now - 10.0
            shared._consecutive_empty_state = 99  # 超過閾值
            shared._consecutive_sip2_fail = 99
            shared._watchdog_tick(now=now)
            mock_m.assert_not_called()
            mock_s.assert_not_called()

    def test_after_grace_triggers(self, fake_machine):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine') as mock_rec:
            # 程序已啟動 120 秒，遠超 60s 寬限期
            now = 12345.0
            shared._watchdog_start_time = now - 120.0
            shared._consecutive_empty_state = 3
            shared._watchdog_tick(now=now)
            mock_rec.assert_called_once()


# ─── _watchdog_tick: machine recovery ─────────────────────────────────────────

class TestMachineRecovery:
    def test_below_threshold_no_recovery(self, fake_machine):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine') as mock_rec:
            shared._consecutive_empty_state = 2  # 未達 3
            shared._watchdog_tick(now=1000.0)
            mock_rec.assert_not_called()

    def test_threshold_breached_triggers_recovery(self, fake_machine):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine') as mock_rec:
            shared._consecutive_empty_state = 3
            shared._watchdog_tick(now=1000.0)
            mock_rec.assert_called_once()

    def test_recovery_sets_cooldown(self, fake_machine):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine') as mock_rec:
            shared._consecutive_empty_state = 5
            shared._watchdog_tick(now=1000.0)
            assert shared._watchdog_machine_cooldown_until == 1000.0 + shared._WATCHDOG_COOLDOWN_SEC
            mock_rec.assert_called_once()

    def test_recovery_blocked_within_cooldown(self, fake_machine, caplog):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine') as mock_rec:
            shared._consecutive_empty_state = 5
            shared._watchdog_machine_cooldown_until = 1000.0 + shared._WATCHDOG_COOLDOWN_SEC
            with caplog.at_level(logging.WARNING, logger='shared'):
                shared._watchdog_tick(now=1000.0 + 60)  # 一分鐘後
            mock_rec.assert_not_called()
            # 必須 log 一次冷卻訊息
            assert any('threshold breached but in cooldown' in r.message
                       for r in caplog.records), \
                "缺少冷卻中跳過的 WARNING log"

    def test_cooldown_log_only_once(self, fake_machine, caplog):
        """冷卻期間連續多次 tick，只 log 一次跳過訊息（避免每 5s 噴）。"""
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine'):
            shared._consecutive_empty_state = 5
            shared._watchdog_machine_cooldown_until = 1000.0 + shared._WATCHDOG_COOLDOWN_SEC
            with caplog.at_level(logging.WARNING, logger='shared'):
                shared._watchdog_tick(now=1000.0 + 60)
                shared._watchdog_tick(now=1000.0 + 65)
                shared._watchdog_tick(now=1000.0 + 70)
            cooldown_logs = [r for r in caplog.records
                             if 'threshold breached but in cooldown' in r.message
                             and 'machine' in r.message]
            assert len(cooldown_logs) == 1, \
                f"冷卻中 log 應該只有 1 次，實際 {len(cooldown_logs)}"

    def test_recovery_runs_again_after_cooldown_expires(self, fake_machine):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, '_watchdog_recover_machine') as mock_rec:
            shared._consecutive_empty_state = 5

            # 第一次：觸發
            shared._watchdog_tick(now=1000.0)
            assert mock_rec.call_count == 1

            # 1 小時後（仍在 12h 冷卻內）：跳過
            shared._watchdog_tick(now=1000.0 + 3600)
            assert mock_rec.call_count == 1

            # 12 小時又 1 秒後：冷卻過了，再次觸發
            shared._watchdog_tick(now=1000.0 + shared._WATCHDOG_COOLDOWN_SEC + 1)
            assert mock_rec.call_count == 2

    def test_auto_recover_disabled_logs_but_skips(self, fake_machine, caplog):
        """config 關閉 watchdog.auto_recover_machine 時，必須 log 但不執行恢復。"""
        shared.WATCHDOG_AUTO_RECOVER_MACHINE = False
        with patch.object(shared, 'machine', fake_machine):
            shared._consecutive_empty_state = 5
            with caplog.at_level(logging.WARNING, logger='shared'):
                shared._watchdog_recover_machine()
            # 沒送任何指令
            assert fake_machine.commands_sent == []
            # serial 沒被關掉
            assert fake_machine.ser.closed_count == 0
            # 但有 log 出 disabled 訊息
            assert any('auto-recover disabled' in r.message and 'machine' in r.message
                       for r in caplog.records), \
                "缺少 'auto-recover disabled' WARNING"

    def test_recovery_closes_and_resends_dep1_homing(self, fake_machine):
        """真的執行恢復時，應該 close serial，並送出 dep1 / homing。

        simulate=True 時不會試圖重開 serial.Serial（測試裡無法開真的）。"""
        with patch.object(shared, 'machine', fake_machine):
            shared._consecutive_empty_state = 5
            shared._watchdog_recover_machine()
        # serial 應該被關過一次
        assert fake_machine.ser.closed_count == 1
        # 應該送出 dep1 + homing
        assert 'dep1' in fake_machine.commands_sent
        assert 'homing' in fake_machine.commands_sent
        # 順序：dep1 在 homing 之前
        assert (fake_machine.commands_sent.index('dep1')
                < fake_machine.commands_sent.index('homing'))


# ─── _watchdog_tick: sip2 recovery ────────────────────────────────────────────

class TestSip2Recovery:
    def test_below_threshold_no_recovery(self, fake_sip2):
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, '_watchdog_recover_sip2') as mock_rec:
            shared._consecutive_sip2_fail = 3  # 未達 4
            shared._watchdog_tick(now=1000.0)
            mock_rec.assert_not_called()

    def test_threshold_breached_triggers_recovery(self, fake_sip2):
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, '_watchdog_recover_sip2') as mock_rec:
            shared._consecutive_sip2_fail = 4
            shared._watchdog_tick(now=1000.0)
            mock_rec.assert_called_once()

    def test_recovery_calls_init_sip2_client(self, fake_sip2):
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, 'init_sip2_client') as mock_init:
            shared._consecutive_sip2_fail = 5
            shared._watchdog_recover_sip2()
            mock_init.assert_called_once()

    def test_recovery_skips_when_io_lock_busy(self, fake_sip2):
        """如果 sip2._io_lock 拿不到（5s timeout），跳過本次觸發。"""
        # 用 Mock 取代真實 lock，模擬 acquire 永遠失敗（拿不到）
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False
        fake_sip2._io_lock = fake_lock
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, 'init_sip2_client') as mock_init:
            shared._consecutive_sip2_fail = 5
            shared._watchdog_recover_sip2()
        # 拿 lock 失敗，不會呼叫 init
        mock_init.assert_not_called()
        # 確認 acquire 有被嘗試（且有給 timeout=5）
        fake_lock.acquire.assert_called_once_with(timeout=5)
        # 沒拿到 lock 就不該 release
        fake_lock.release.assert_not_called()

    def test_recovery_blocked_within_cooldown(self, fake_sip2, caplog):
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, '_watchdog_recover_sip2') as mock_rec:
            shared._consecutive_sip2_fail = 5
            shared._watchdog_sip2_cooldown_until = 1000.0 + shared._WATCHDOG_COOLDOWN_SEC
            with caplog.at_level(logging.WARNING, logger='shared'):
                shared._watchdog_tick(now=1000.0 + 60)
            mock_rec.assert_not_called()
            assert any('threshold breached but in cooldown' in r.message
                       and 'sip2' in r.message
                       for r in caplog.records), \
                "缺少 sip2 冷卻 WARNING"

    def test_recovery_runs_again_after_cooldown_expires(self, fake_sip2):
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, '_watchdog_recover_sip2') as mock_rec:
            shared._consecutive_sip2_fail = 5
            shared._watchdog_tick(now=1000.0)
            assert mock_rec.call_count == 1
            shared._watchdog_tick(now=1000.0 + shared._WATCHDOG_COOLDOWN_SEC + 1)
            assert mock_rec.call_count == 2

    def test_auto_recover_disabled_logs_but_skips(self, fake_sip2, caplog):
        shared.WATCHDOG_AUTO_RECOVER_SIP2 = False
        with patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, 'init_sip2_client') as mock_init:
            shared._consecutive_sip2_fail = 5
            with caplog.at_level(logging.WARNING, logger='shared'):
                shared._watchdog_recover_sip2()
            mock_init.assert_not_called()
            assert any('auto-recover disabled' in r.message and 'SIP2' in r.message
                       for r in caplog.records), \
                "缺少 SIP2 'auto-recover disabled' WARNING"


# ─── Cooldowns are independent ────────────────────────────────────────────────

class TestIndependentCooldowns:
    def test_machine_cooldown_does_not_block_sip2(self, fake_machine, fake_sip2):
        with patch.object(shared, 'machine', fake_machine), \
             patch.object(shared, 'sip2', fake_sip2), \
             patch.object(shared, '_watchdog_recover_machine') as mock_m, \
             patch.object(shared, '_watchdog_recover_sip2') as mock_s:
            # machine 計數超標 + machine 冷卻中；sip2 也超標但冷卻是 0
            shared._consecutive_empty_state = 5
            shared._consecutive_sip2_fail = 5
            shared._watchdog_machine_cooldown_until = 1000.0 + shared._WATCHDOG_COOLDOWN_SEC
            shared._watchdog_sip2_cooldown_until = 0.0
            shared._watchdog_tick(now=1000.0 + 60)
            mock_m.assert_not_called()
            mock_s.assert_called_once()


# ─── _collect_machine_state exposes raw response ──────────────────────────────

class TestCollectMachineStateRawField:
    """確保 _collect_machine_state 回傳的 dict 帶有 _raw_state_resp，
    供 _state_poll_thread → _update_machine_empty_counter 使用。"""

    def test_raw_state_resp_present_with_empty_response(self, fake_machine):
        fake_machine._status_response = ""
        with patch.object(shared, 'machine', fake_machine):
            result = shared._collect_machine_state()
        assert '_raw_state_resp' in result
        assert result['_raw_state_resp'] == ""

    def test_raw_state_resp_present_with_real_response(self, fake_machine):
        fake_machine._status_response = "2"
        with patch.object(shared, 'machine', fake_machine):
            result = shared._collect_machine_state()
        assert result['_raw_state_resp'] == "2"

    def test_raw_state_resp_when_machine_is_none(self):
        with patch.object(shared, 'machine', None):
            result = shared._collect_machine_state()
        assert result['_raw_state_resp'] is None
