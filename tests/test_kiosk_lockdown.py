"""
Kiosk 鎖定回歸測試：確保逃脫路徑不會因誤改 code 而打開。

威脅模型：自助還書機。使用者只能操作 Firefox 裡的頁面，不能開
其他視窗、切換 app、關 Firefox、開 GNOME 設定等。

兩道防線：
1. install_gnome_lockdown — install 時 gsettings 清空危險快捷鍵
2. _kill_intruder_windows — Firefox 重啟時 pkill 掉可能冒出來的視窗
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / 'install_user_services.sh'


@pytest.fixture(scope='module')
def install_script() -> str:
    return INSTALL_SH.read_text(encoding='utf-8')


class TestGnomeLockdownInInstallScript:
    """install_user_services.sh 必須包含 GNOME 快捷鍵鎖定。"""

    def test_lockdown_function_defined(self, install_script):
        assert 'install_gnome_lockdown()' in install_script, (
            "必須定義 install_gnome_lockdown 函式")

    def test_lockdown_called_from_install_flow(self, install_script):
        """主安裝流程必須呼叫 install_gnome_lockdown。"""
        # 找 install_systemd_services 函式內容
        m = re.search(r'install_systemd_services\(\)\s*\{(.*?)^\}',
                      install_script, re.DOTALL | re.MULTILINE)
        assert m, "找不到 install_systemd_services 函式"
        body = m.group(1)
        assert 'install_gnome_lockdown' in body, (
            "install_systemd_services 必須呼叫 install_gnome_lockdown，"
            "否則每次重裝會漏掉鎖定步驟")

    def test_overlay_key_cleared(self, install_script):
        """Super 鍵不可再開 Activities。"""
        assert re.search(
            r"gsettings\s+set\s+org\.gnome\.mutter\s+overlay-key\s+''",
            install_script), "必須清空 org.gnome.mutter overlay-key（Super 鍵）"

    @pytest.mark.parametrize('keybinding', [
        'switch-applications',        # Alt+Tab
        'panel-run-dialog',           # Alt+F2
        'close',                      # Alt+F4 — 關 Firefox 最嚴重
        'activate-window-menu',       # Alt+Space
        'begin-move',                 # Alt+F7
        'begin-resize',               # Alt+F8
    ])
    def test_critical_wm_keybinding_disabled(self, install_script, keybinding):
        """關鍵 WM 快捷鍵必須列在鎖定清單裡。"""
        assert keybinding in install_script, (
            f"關鍵快捷鍵 {keybinding} 未被禁用 — 使用者可能藉此逃出 kiosk")

    @pytest.mark.parametrize('keybinding', [
        'toggle-application-view',    # Super+A
        'toggle-overview',            # Super+S
        'switch-to-application-1',    # Super+1
    ])
    def test_critical_shell_keybinding_disabled(self, install_script, keybinding):
        assert keybinding in install_script, (
            f"關鍵 Shell 快捷鍵 {keybinding} 未被禁用")

    @pytest.mark.parametrize('keybinding', [
        'control-center',             # 設定快捷鍵
        'calculator',
        'logout',
    ])
    def test_critical_media_keybinding_disabled(self, install_script, keybinding):
        assert keybinding in install_script, (
            f"關鍵 media-keys 快捷鍵 {keybinding} 未被禁用")


class TestIntruderWindowKiller:
    """後端重啟邏輯必須殺掉已知入侵視窗。"""

    def test_kill_function_exists(self):
        from routes.api import _kill_intruder_windows  # noqa
        from routes.api import _INTRUDER_WINDOW_PROCS
        assert len(_INTRUDER_WINDOW_PROCS) > 0

    @pytest.mark.parametrize('proc', [
        'gnome-control-center',  # 設定 — 最主要威脅
        'nautilus',              # 檔案總管
        'gnome-calculator',
        'gnome-terminal',
        'xterm',
    ])
    def test_critical_intruder_in_kill_list(self, proc):
        from routes.api import _INTRUDER_WINDOW_PROCS
        assert proc in _INTRUDER_WINDOW_PROCS, (
            f"{proc} 應列入 kill 清單 — 使用者可能藉此操作系統")

    def test_restart_calls_kill_before_systemctl(self):
        """確保 _restart_frontend_services 會先 pkill 再 restart，
        避免 systemctl 先跑但入侵視窗還在前景。"""
        import routes.api as api_mod
        calls = []

        def fake_kill():
            calls.append('kill')

        def fake_run(*args, **kwargs):
            calls.append(('run', args[0]))
            from unittest.mock import MagicMock
            r = MagicMock()
            r.returncode = 0
            r.stdout = ''
            r.stderr = ''
            return r

        with patch.object(api_mod, '_kill_intruder_windows', fake_kill), \
             patch.object(api_mod.subprocess, 'run', fake_run), \
             patch.object(api_mod.time, 'sleep', lambda *_: None):
            api_mod._FRONTEND_RESTART_IN_PROGRESS = True
            try:
                api_mod._restart_frontend_services('test')
            finally:
                api_mod._FRONTEND_RESTART_IN_PROGRESS = False

        assert calls[0] == 'kill', (
            f"必須先 pkill 再呼叫 systemctl，實際順序：{calls}")
        assert any(isinstance(c, tuple) and 'systemctl' in c[1] for c in calls[1:]), (
            "pkill 後必須接 systemctl restart")
