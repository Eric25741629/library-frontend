"""
sort_book put 完成確認（階段 1）測試。

對應 2026-05-25 19:10 事件：put1 在 120s action_timeout 內沒回 `been put1`，
舊 sort_book 直接判失敗報「人工介入」，但機構其實 ~16 分鐘後才完成（been put1）。

階段 1 行為：
  - put 立即完成 → True。
  - put 回 'ack-no-completion'（ack 過、完成逾時）→ 進確認窗，期間若觀察到
    `been put{n}` → True（只是慢）。
  - 確認窗內始終沒看到 been put{n} → False（真‧人工介入）。
  - 完成判定只認 been put{n}，不認 state==HOMED（避免 homing 偽陽性）。
"""
from __future__ import annotations

import time

import machine_controller
from machine_controller import MachineController


def _make_machine():
    # simulate=True 不開序列埠；確認窗參數調小讓測試快。
    return MachineController(
        simulate=True,
        put_confirm_interval=0.01,
        put_confirm_deadline=0.3,
    )


def test_sort_book_immediate_completion():
    m = _make_machine()
    m._send_command = lambda cmd: "been put1"
    assert m.sort_book(1) is True


def test_sort_book_hard_failure_does_not_confirm():
    """error state / 未 ack 等非逾時失敗，直接 False，不進確認窗。"""
    m = _make_machine()
    calls = []
    m._send_command = lambda cmd: (calls.append(cmd), "error state 4")[1]
    m.get_status = lambda: calls.append("state")  # 不該被呼叫
    assert m.sort_book(1) is False
    assert "state" not in calls  # 證明沒進確認窗


def test_sort_book_late_completion_confirmed():
    """逾時後在確認窗內觀察到 been put1 → True。"""
    m = _make_machine()
    m._send_command = lambda cmd: "ack-no-completion"

    # 模擬：第一次 confirm-poll 的 get_status 觸發韌體推 been put1，
    # 由 side-effect handler 記進 _put_done_at（這裡直接呼叫 handler 模擬讀到該行）。
    state = {"polls": 0}

    def fake_get_status():
        state["polls"] += 1
        if state["polls"] == 1:
            m._apply_response_side_effects("been put1")
        return "4"

    m.get_status = fake_get_status
    assert m.sort_book(1) is True
    assert m._put_done_at.get(1) is not None


def test_sort_book_never_completes_returns_false():
    """確認窗內始終沒 been put → False。"""
    m = _make_machine()
    m._send_command = lambda cmd: "ack-no-completion"
    m.get_status = lambda: "4"  # 永遠停在 closed，從不完成
    t0 = time.time()
    assert m.sort_book(1) is False
    # 應確實等滿確認窗才放棄（約 deadline）。
    assert time.time() - t0 >= m.PUT_CONFIRM_DEADLINE - 0.05


def test_state_homed_alone_is_not_completion():
    """只回到 HOMED(2) 不算完成（避免 homing 偽陽性）—— 必須有 been put{n}。"""
    m = _make_machine()
    m._send_command = lambda cmd: "ack-no-completion"
    m.get_status = lambda: "2"  # 機器回 homed，但沒推 been put
    assert m.sort_book(1) is False


def test_been_put_side_effect_records_bin():
    """_apply_response_side_effects 看到 been put2 應記 bin=2。"""
    m = _make_machine()
    assert m._put_done_at.get(2) is None
    m._apply_response_side_effects("been put2")
    assert m._put_done_at.get(2) is not None


def test_confirm_ignores_stale_been_put():
    """確認窗只認 since 之後的 been put；舊的不算。"""
    m = _make_machine()
    # 預先放一個「過去」的完成記錄。
    m._put_done_at[1] = time.time() - 100
    m._send_command = lambda cmd: "ack-no-completion"
    m.get_status = lambda: "4"
    assert m.sort_book(1) is False  # 舊紀錄早於 since，不算數
