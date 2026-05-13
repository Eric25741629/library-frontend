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
                 book_present=True, close_door_result=True, sort_book_result=True):
        self.default_state = default_state
        self.command_responses = command_responses or {}
        self.book_present = book_present
        self._close_door_result = close_door_result
        self._sort_book_result = sort_book_result
        # 紀錄 reopen_door 是否被呼叫，方便個別測試斷言「失敗後是否重開門」
        self.reopen_door_called = False
        self.close_door_called = False
        self.sort_book_called_with = None

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
        return self._close_door_result

    def reopen_door(self):
        self.reopen_door_called = True
        return 'reopened'

    def check_book_status(self):
        return self.book_present

    def sort_book(self, bin_number=1):
        self.sort_book_called_with = bin_number
        return self._sort_book_result


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

    def test_non_opened_state_rejected_strictly(self, client):
        # 新行為（規格 2025-12-07 流程）：state 非 3 (OPENED) 直接拒絕，
        # 不再走「盲送 homing fallback 後繼續 close」這條會吞書本的路徑。
        # 取代舊版 test_homing_fallback_error_state_0_also_blocks。
        machine = _FakeMachine(default_state='2')  # HOMED 但門沒開
        sip2 = _FakeSIP2({'success': True})  # 不該被用到
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 409
        body = rv.get_json()
        assert body['code'] == 'MACHINE_NOT_OPEN'
        # 嚴格拒絕：不該關門、不該呼叫 SIP2 checkin
        assert machine.close_door_called is False
        assert machine.reopen_door_called is False

    def test_case_insensitive_match(self, client):
        # 真實機器有時用大寫 'Error State 0' 之類；判斷必須不分大小寫
        machine = _FakeMachine(default_state='ERROR STATE 0')
        sip2 = _FakeSIP2({'success': True})
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.get_json()['code'] == 'MACHINE_ERROR_STATE_0'


class TestCloseDoorFailure:
    """close_door() 回 False 時不能繼續往下 checkin / sort，
    必須回 MACHINE_CLOSE_FAILED 並重開門讓使用者取回書本。"""

    def test_close_failed_blocks_checkin_and_reopens(self, client):
        machine = _FakeMachine(default_state='3', close_door_result=False)
        sip2 = _FakeSIP2({'success': True})  # 不該被用到
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 500
        body = rv.get_json()
        assert body['code'] == 'MACHINE_CLOSE_FAILED'
        # close 失敗 → 應該重開門讓使用者取書、且 sort_book 不該被呼叫
        assert machine.close_door_called is True
        assert machine.reopen_door_called is True
        assert machine.sort_book_called_with is None


class TestSortBookFailure:
    """sort_book() 回 False 時，SIP2 已 commit、box_inventory 已寫入，
    流程仍回 success=True 但 response 帶 sort_warning 給前端 / 管理員追查。"""

    def test_sort_failure_returns_success_with_warning(self, client):
        machine = _FakeMachine(default_state='3', sort_book_result=False)
        sip2 = _FakeSIP2({'success': True})
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 200
        body = rv.get_json()
        # 還書本身仍標 success（SIP2 已 commit 無法 reverse）
        assert body['success'] is True
        # 但帶上 sort_warning 警告碼
        assert body.get('sort_warning') is not None
        assert body['sort_warning']['code'] == 'SORT_FAILED'
        assert '櫃台' in body['sort_warning']['message']
        # sort_book 確實被呼叫過
        assert machine.sort_book_called_with is not None

    def test_sort_success_has_no_warning(self, client):
        machine = _FakeMachine(default_state='3', sort_book_result=True)
        sip2 = _FakeSIP2({'success': True})
        with _patched_env(machine=machine, sip2=sip2):
            rv = _post_return(client)
        assert rv.status_code == 200
        body = rv.get_json()
        assert body['success'] is True
        assert body.get('sort_warning') is None


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
