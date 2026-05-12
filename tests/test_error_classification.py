"""
還書錯誤分類回歸測試。

背景：2026-05-12 之前 /api/return 的所有 SIP2 拒絕都回同一個 CHECKIN_FAILED code
和「圖書館系統拒絕本次還書」的訊息，櫃台無法分辨：
  - 書其實已被先前一次成功歸還了（第二次嘗試才回 "館藏資料異常"）
  - 圖書館系統因其他理由拒絕
  - 機器卡在 error state 0（cancel/reopen 後遺症），不應該繼續還書
  - SIP2 server 完全沒回應或沒連線

本檔覆蓋四個新分類 code 的判斷邏輯：
  LIBRARY_REJECT_ALREADY_RETURNED / LIBRARY_REJECT_OTHER /
  MACHINE_ERROR_STATE_0 / SIP2_DISCONNECTED
以及保留 BOOK_NOT_DETECTED、SERVICE_SUSPENDED 等舊 code 不受影響。
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import shared
import config
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
    """machine controller 替身。

    - get_status() 與 _send_command() 行為由 default_state / command_responses 控制
    - close_door() / reopen_door() / check_book_status() 預設成功
    - 把 STATE_OPENED 設成 3，與真實控制器一致
    """

    STATE_OPENED = 3

    def __init__(self, default_state='3', command_responses=None,
                 book_present=True):
        self.default_state = default_state
        self.command_responses = command_responses or {}
        self.book_present = book_present
        # 紀錄 reopen_door 是否被呼叫，方便個別測試斷言「失敗後是否重開門」
        self.reopen_door_called = False
        self.close_door_called = False

    def get_status(self):
        return self.default_state

    def _send_command(self, cmd):
        if cmd in self.command_responses:
            return self.command_responses[cmd]
        # 預設：state 查詢回 default_state；其他指令回 'ack'
        if cmd == 'state':
            return self.default_state
        return 'ack'

    def close_door(self):
        self.close_door_called = True
        return 'closed'

    def reopen_door(self):
        self.reopen_door_called = True
        return 'reopened'

    def check_book_status(self):
        return self.book_present

    def sort_book(self, bin_number=1):
        return f'put{bin_number} ack'


class _FakeSIP2:
    """sip2 client 替身：checkin_book 回固定 dict 或拋例外。"""

    def __init__(self, checkin_result):
        self._result = checkin_result

    def checkin_book(self, barcode):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def get_book_info(self, barcode):
        return {
            'barcode': barcode,
            'title': 'Test Book',
            'author': 'Test Author',
            'due_date': '',
        }


@contextmanager
def _patched_env(machine=None, sip2=None, library_checkin_enabled=True):
    """把 shared.machine / shared.sip2 / BOOK_CHECK_ENABLED / is_box_full
    與 config.LIBRARY_CHECKIN_ENABLED 都 patch 成測試專用的值。

    BOOK_CHECK_ENABLED 在 routes/api.py 內以 `shared.BOOK_CHECK_ENABLED`
    讀取，所以 patch shared 屬性即可。
    """
    old_machine = shared.machine
    old_sip2 = shared.sip2
    old_book_check = shared.BOOK_CHECK_ENABLED
    old_lib_checkin = config.LIBRARY_CHECKIN_ENABLED
    shared.machine = machine
    shared.sip2 = sip2
    shared.BOOK_CHECK_ENABLED = False  # 讓書箱書本偵測一律通過，聚焦在 checkin 行為
    config.LIBRARY_CHECKIN_ENABLED = library_checkin_enabled
    try:
        with patch.object(shared, 'is_box_full', return_value=False), \
             patch.object(shared, 'get_bin_counts', return_value={'1': 0, '2': 0}), \
             patch.object(config, 'reload_config'), \
             patch('routes.api.threading.Thread') as ThreadCls:
            # 讓 async_reopen 等背景 thread 變成同步呼叫，方便驗證 reopen_door
            def run_now(target=None, args=(), daemon=None):
                t = MagicMock()
                if target:
                    t.start = lambda: target(*args)
                else:
                    t.start = lambda: None
                return t
            ThreadCls.side_effect = run_now
            yield
    finally:
        shared.machine = old_machine
        shared.sip2 = old_sip2
        shared.BOOK_CHECK_ENABLED = old_book_check
        config.LIBRARY_CHECKIN_ENABLED = old_lib_checkin


def _post_return(client, book_id='B001'):
    return client.post('/api/return', json={'book_ids': [book_id]})


class TestLibraryRejectAlreadyReturned:
    """checkin 失敗且 AF/error 含「館藏資料異常」「已歸還」「不在借出」之一
    → LIBRARY_REJECT_ALREADY_RETURNED；後端會非同步重開門。"""

    @pytest.mark.parametrize('af,err', [
        ('館藏資料異常請洽櫃台', '館藏資料異常請洽櫃台'),
        ('此書已歸還', '此書已歸還'),
        (None, '不在借出狀態'),
        ('館藏資料異常', None),  # 只有 AF 也算
    ])
    def test_already_returned_keywords_map_to_classified_code(self, client, af, err):
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': False,
            'af_message': af,
            'ag_message': None,
            'error_message': err,
        })
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 400
        body = rv.get_json()
        assert body['success'] is False
        assert body['code'] == 'LIBRARY_REJECT_ALREADY_RETURNED'
        # 訊息必須提示「已歸還，請取回書本後洽櫃台」之類，不是舊的「圖書館系統拒絕」泛用語
        assert '已歸還' in body['message']
        # 後端必須重新開門讓使用者取回書本
        assert machine.reopen_door_called is True

    def test_failed_books_field_preserved(self, client):
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': False,
            'af_message': '館藏資料異常請洽櫃台',
            'ag_message': None,
            'error_message': '館藏資料異常請洽櫃台',
        })
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client, book_id='B777')
        body = rv.get_json()
        assert body['failed_books'] == ['B777']


class TestLibraryRejectOther:
    """checkin 失敗但 AF/AG/error 不含「已歸還」關鍵字 → LIBRARY_REJECT_OTHER；
    訊息應夾帶 SIP2 server 給的具體理由（error_message），方便櫃台判斷。"""

    def test_generic_rejection_uses_classified_code(self, client):
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': False,
            'af_message': '此書屬於不可由還書機處理',
            'ag_message': None,
            'error_message': '此書屬於不可由還書機處理',
        })
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 400
        body = rv.get_json()
        assert body['code'] == 'LIBRARY_REJECT_OTHER'
        # 應包含 SIP2 給的具體理由
        assert '不可由還書機處理' in body['message']
        assert machine.reopen_door_called is True

    def test_rejection_without_detail_falls_back_to_default_message(self, client):
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': False,
            'af_message': None,
            'ag_message': None,
            'error_message': None,
        })
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        body = rv.get_json()
        assert body['code'] == 'LIBRARY_REJECT_OTHER'
        # 沒有具體 detail 時，使用預設 fallback 訊息
        assert '圖書館系統拒絕本次還書' in body['message']


class TestMachineErrorState0:
    """機器回 'error state 0' 時直接擋下，回 MACHINE_ERROR_STATE_0；
    不可繼續關門 / checkin / sort（會把書吞掉但沒入帳）。"""

    def test_pre_close_state_returns_machine_error_state_0(self, client):
        # get_status() 回 'error state 0'
        machine = _FakeMachine(default_state='error state 0')
        sip2 = _FakeSIP2({'success': True})  # 不該被用到
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 503
        body = rv.get_json()
        assert body['code'] == 'MACHINE_ERROR_STATE_0'
        assert '機器發生錯誤' in body['message'] or '櫃台' in body['message']
        # 不能呼叫 close_door 或進入 SIP2 還書
        assert machine.close_door_called is False
        assert machine.reopen_door_called is False

    def test_homing_fallback_error_state_0_also_blocks(self, client):
        # 機器 state 既不是 OPENED 也不是 error state 0 → 走 fallback homing
        # homing 回 'error state 0' 時應該擋下
        machine = _FakeMachine(
            default_state='2',  # HOMED，不是 OPENED → 走 fallback homing 分支
            command_responses={'homing': 'error state 0'},
        )
        sip2 = _FakeSIP2({'success': True})
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 503
        body = rv.get_json()
        assert body['code'] == 'MACHINE_ERROR_STATE_0'
        assert machine.close_door_called is False

    def test_case_insensitive_match(self, client):
        # 真實機器有時用大寫 'Error State 0' 之類；判斷必須不分大小寫
        machine = _FakeMachine(default_state='ERROR STATE 0')
        sip2 = _FakeSIP2({'success': True})
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.get_json()['code'] == 'MACHINE_ERROR_STATE_0'


class TestSip2Disconnected:
    """shared.sip2 為 None 或 checkin_book 回 None/拋例外 → SIP2_DISCONNECTED，
    與一般 CHECKIN_FAILED 區分（後者代表 SIP2 server 明確回拒）。"""

    def test_sip2_is_none(self, client):
        machine = _FakeMachine()
        with _patched_env(machine=machine, sip2=None):
            rv = _post_return(client)
        assert rv.status_code == 400
        body = rv.get_json()
        assert body['code'] == 'SIP2_DISCONNECTED'
        assert '無法連線' in body['message'] or '稍後' in body['message']
        # 後端仍要重新開門讓使用者取回書本
        assert machine.reopen_door_called is True

    def test_checkin_returns_none(self, client):
        machine = _FakeMachine()
        sip2 = _FakeSIP2(None)
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        body = rv.get_json()
        assert body['code'] == 'SIP2_DISCONNECTED'
        assert machine.reopen_door_called is True

    def test_checkin_raises_exception(self, client):
        machine = _FakeMachine()
        sip2 = _FakeSIP2(ConnectionError('socket broken mid-checkin'))
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        body = rv.get_json()
        # 例外應被捕捉並轉成 SIP2_DISCONNECTED，不能 500
        assert rv.status_code == 400
        assert body['code'] == 'SIP2_DISCONNECTED'


class TestExistingCodesUnchanged:
    """舊 code 必須維持原行為，不被本次分類重構影響。"""

    def test_service_suspended_still_returned(self, client):
        machine = _FakeMachine()
        sip2 = _FakeSIP2({'success': True})
        with _patched_env(machine=machine, sip2=sip2):
            with patch.object(shared, 'is_box_full', return_value=True):
                rv = _post_return(client)
        assert rv.status_code == 503
        assert rv.get_json()['code'] == 'SERVICE_SUSPENDED'

    def test_successful_checkin_returns_success_with_no_error_code(self, client):
        # 確認成功路徑沒被本次重構搞壞：success=True，沒有 code，回 returned_books
        machine = _FakeMachine()
        sip2 = _FakeSIP2({
            'success': True,
            'af_message': None,
            'ag_message': None,
        })
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 200
        body = rv.get_json()
        assert body['success'] is True
        assert 'code' not in body or not body.get('code')
        assert len(body['data']) == 1


class TestClassificationHelpers:
    """直接單元測試分類 helper，確認關鍵字邏輯穩定。"""

    def test_is_already_returned_reject_keywords(self):
        from routes.api import _is_already_returned_reject
        assert _is_already_returned_reject('館藏資料異常') is True
        assert _is_already_returned_reject('此書已歸還') is True
        assert _is_already_returned_reject('不在借出狀態') is True
        # 多參數：任一含關鍵字就 True
        assert _is_already_returned_reject(None, '已歸還') is True
        # 全 None 或空字串 → False
        assert _is_already_returned_reject(None, None) is False
        assert _is_already_returned_reject('', '   ') is False
        # 不含關鍵字 → False
        assert _is_already_returned_reject('一般拒絕原因') is False

    def test_is_machine_error_state_0(self):
        from routes.api import _is_machine_error_state_0
        assert _is_machine_error_state_0('error state 0') is True
        assert _is_machine_error_state_0('Error State 0') is True
        assert _is_machine_error_state_0('hardware reports error state 0 now') is True
        assert _is_machine_error_state_0('error state 3') is False
        assert _is_machine_error_state_0('') is False
        assert _is_machine_error_state_0(None) is False
        # 數字 status (3) 不應被誤判
        assert _is_machine_error_state_0('3') is False
