"""
預約通知 (reservation_notice) 端對端測試。

背景：HyLib SIP2 server 在 09/10 checkin 回應時，會在 AF/AG 帶
「此資料有人預約，館藏狀態將變更為『預約待取』並寄發預約到館通知。」
歷史上前端只當 toast 顯示一次就丟掉，後台無法區分哪些書需特別處理。
本檔覆蓋：
  1. _extract_reservation_notice 關鍵字判斷
  2. init_box_db 會給舊 DB 補 reservation_notice 欄位
  3. /api/return 在 checkin 成功且 AF/AG 含「預約」時，
     會把訊息寫進 box_inventory.reservation_notice
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import shared
import config
from routes.api import api_bp, _extract_reservation_notice


# ---- helper 單元測試 -------------------------------------------------------

class TestExtractReservationNotice:
    def test_picks_up_typical_hyLib_message(self):
        text = '此資料有人預約，館藏狀態將變更為「預約待取」並寄發預約到館通知。'
        assert _extract_reservation_notice(text, None) == text

    def test_returns_first_match_when_af_has_it(self):
        af = '此資料有人預約，請洽櫃台。'
        ag = '還書成功'
        assert _extract_reservation_notice(af, ag) == af

    def test_falls_through_to_ag_if_af_blank(self):
        af = None
        ag = '此資料有人預約'
        assert _extract_reservation_notice(af, ag) == ag

    def test_returns_none_when_no_reservation_keyword(self):
        assert _extract_reservation_notice('還書成功', '逾期罰款 10 元') is None

    def test_handles_empty_inputs(self):
        assert _extract_reservation_notice(None, None) is None
        assert _extract_reservation_notice('', '') is None


# ---- schema migration -----------------------------------------------------

class TestSchemaMigration:
    def test_init_box_db_adds_reservation_notice_to_existing_tables(self, tmp_path):
        """模擬一個沒有 reservation_notice 欄位的舊 DB，呼叫 init_box_db 後
        兩張表都應有此欄位（NULLable TEXT）。"""
        db_file = tmp_path / "old_box.db"
        # 建立沒有 reservation_notice 的舊 schema
        conn = sqlite3.connect(db_file)
        conn.execute('''CREATE TABLE box_inventory
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         book_id TEXT, title TEXT, image_url TEXT,
                         return_time TEXT, target_bin INTEGER)''')
        conn.execute('''CREATE TABLE box_history
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         book_id TEXT, title TEXT, image_url TEXT,
                         return_time TEXT, clear_time TEXT, target_bin INTEGER)''')
        conn.commit()
        conn.close()

        old_db_file = shared.BOX_DB_FILE
        shared.BOX_DB_FILE = str(db_file)
        try:
            shared.init_box_db()
            conn = sqlite3.connect(db_file)
            inv_cols = [r[1] for r in conn.execute("PRAGMA table_info(box_inventory)").fetchall()]
            hist_cols = [r[1] for r in conn.execute("PRAGMA table_info(box_history)").fetchall()]
            conn.close()
        finally:
            shared.BOX_DB_FILE = old_db_file

        assert 'reservation_notice' in inv_cols
        assert 'reservation_notice' in hist_cols


# ---- end-to-end /api/return 寫入 box_inventory ------------------------------

class _FakeMachine:
    STATE_OPENED = 3

    def __init__(self):
        self.default_state = '3'
        self.reopen_door_called = False
        self.close_door_called = False
        self.sort_book_called_with = None

    def get_status(self):
        return self.default_state

    def _send_command(self, cmd):
        if cmd == 'state':
            return self.default_state
        return 'ack'

    def close_door(self):
        self.close_door_called = True
        return True

    def reopen_door(self):
        self.reopen_door_called = True
        return 'reopened'

    def check_book_status(self):
        return True

    def sort_book(self, bin_number=1):
        self.sort_book_called_with = bin_number
        return True


class _FakeSIP2:
    def __init__(self, checkin_result):
        self._result = checkin_result

    def checkin_book(self, barcode):
        return self._result

    def get_book_info(self, barcode):
        return {'barcode': barcode, 'title': '預約測試書', 'author': '測試作者', 'due_date': ''}


@contextmanager
def _patched_env_with_db(tmp_path, machine, sip2):
    db_file = tmp_path / "box.db"
    old_db_file = shared.BOX_DB_FILE
    shared.BOX_DB_FILE = str(db_file)
    shared.init_box_db()

    old_machine = shared.machine
    old_sip2 = shared.sip2
    old_book_check = shared.BOOK_CHECK_ENABLED
    old_lib_checkin = config.LIBRARY_CHECKIN_ENABLED
    shared.machine = machine
    shared.sip2 = sip2
    shared.BOOK_CHECK_ENABLED = False
    config.LIBRARY_CHECKIN_ENABLED = True
    try:
        with patch.object(shared, 'is_box_full', return_value=False), \
             patch.object(shared, 'get_bin_counts', return_value={'1': 0, '2': 0}), \
             patch.object(config, 'reload_config'), \
             patch('routes.api.threading.Thread') as ThreadCls:
            def run_now(target=None, args=(), daemon=None):
                t = MagicMock()
                t.start = lambda: target(*args) if target else None
                return t
            ThreadCls.side_effect = run_now
            yield db_file
    finally:
        shared.machine = old_machine
        shared.sip2 = old_sip2
        shared.BOOK_CHECK_ENABLED = old_book_check
        config.LIBRARY_CHECKIN_ENABLED = old_lib_checkin
        shared.BOX_DB_FILE = old_db_file


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(api_bp, url_prefix='/api')
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestReturnFlowPersistsReservation:
    def test_reservation_af_writes_to_box_inventory(self, client, tmp_path):
        msg = '此資料有人預約，館藏狀態將變更為「預約待取」並寄發預約到館通知。'
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': True,
            'af_message': msg,
            'ag_message': msg,
            'error_message': None,
        })
        with _patched_env_with_db(tmp_path, machine, sip2) as db_file:
            rv = client.post('/api/return', json={'book_ids': ['C295901']})
            assert rv.status_code == 200, rv.get_data(as_text=True)

            conn = sqlite3.connect(db_file)
            row = conn.execute(
                "SELECT book_id, reservation_notice FROM box_inventory WHERE book_id = ?",
                ('C295901',),
            ).fetchone()
            conn.close()

        assert row is not None, '預約書應已寫入 box_inventory'
        assert row[1] == msg, f'reservation_notice 欄位應記下館方原文，實際={row[1]!r}'

    def test_non_reservation_book_has_null_notice(self, client, tmp_path):
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': True,
            'af_message': '還書成功',
            'ag_message': None,
            'error_message': None,
        })
        with _patched_env_with_db(tmp_path, machine, sip2) as db_file:
            rv = client.post('/api/return', json={'book_ids': ['B001']})
            assert rv.status_code == 200, rv.get_data(as_text=True)

            conn = sqlite3.connect(db_file)
            row = conn.execute(
                "SELECT reservation_notice FROM box_inventory WHERE book_id = ?",
                ('B001',),
            ).fetchone()
            conn.close()

        assert row is not None
        assert row[0] is None, '一般還書不應留下 reservation_notice'
