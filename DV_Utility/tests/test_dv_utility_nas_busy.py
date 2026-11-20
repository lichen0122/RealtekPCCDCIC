"""RT NAS 連線時 UI 不得看起來卡住: 登入驗證在背景 thread, 連線期間顯示 loading。

背景: 連 NAS + 網域登入一次要 2~5 秒。原本登入窗按 OK 後在 GUI thread 同步連線 (視窗凍住),
之後背景再連兩次也沒有任何提示, 使用者以為程式當掉。
"""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import dv_utility  # noqa: E402
import rtnas  # noqa: E402

SFTP_URL = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/Tool/v.json'


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(qapp, until, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not until():
        qapp.processEvents()
        time.sleep(0.01)
    return until()


# ── 登入驗證 (pure) ───────────────────────────────────────────────────────────

class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_check_nas_login_outcomes(monkeypatch):
    monkeypatch.setattr(dv_utility.rtnas, 'connect', lambda *a, **k: _Ctx())
    assert dv_utility.check_nas_login('h', 22, 'u', 'p') is None

    def _auth(*a, **k):
        raise rtnas.AuthError('rejected')
    monkeypatch.setattr(dv_utility.rtnas, 'connect', _auth)
    assert dv_utility.check_nas_login('h', 22, 'u', 'p') == 'auth'

    def _net(*a, **k):
        raise rtnas.RtnasError('cannot reach h:22')
    monkeypatch.setattr(dv_utility.rtnas, 'connect', _net)
    assert 'cannot reach' in dv_utility.check_nas_login('h', 22, 'u', 'p')


# ── 登入視窗: 驗證期間顯示 loading, 不凍住 ───────────────────────────────────────

def _dialog(qapp, outcome, gate):
    def check(host, port, user, password):
        gate.wait(3)
        return outcome
    dlg = dv_utility.NasLoginDialog(None, 'h', 22, 'rtdomain\\u', check=check)
    dlg.pw_edit.setText('pw')
    return dlg


def test_login_dialog_shows_busy_while_checking_then_accepts(qapp):
    gate = threading.Event()
    dlg = _dialog(qapp, None, gate)
    dlg.submit()
    assert dlg.busy_bar.isVisibleTo(dlg)            # loading 動畫
    assert dlg.busy_bar.maximum() == 0               # 不定進度 (轉動)
    assert not dlg.ok_button.isEnabled() and not dlg.pw_edit.isEnabled()
    assert '正在連線' in dlg.status_label.text()
    gate.set()
    assert _pump(qapp, lambda: dlg.result() == dv_utility.QDialog.DialogCode.Accepted)
    assert (dlg.username, dlg.password) == ('rtdomain\\u', 'pw')


def test_login_dialog_wrong_password_stays_open_with_error(qapp):
    gate = threading.Event()
    gate.set()
    dlg = _dialog(qapp, 'auth', gate)
    dlg.submit()
    assert _pump(qapp, lambda: dlg.ok_button.isEnabled())
    assert dlg.result() != dv_utility.QDialog.DialogCode.Accepted
    assert not dlg.busy_bar.isVisibleTo(dlg)
    assert '密碼錯誤' in dlg.status_label.text()
    assert dlg.pw_edit.text() == ''                  # 清掉讓使用者重輸


def test_login_dialog_network_error_rejects_with_message(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(dv_utility.QMessageBox, 'warning', lambda *a, **k: shown.append(a[-1]))
    gate = threading.Event()
    gate.set()
    dlg = _dialog(qapp, 'cannot reach h:22', gate)
    dlg.submit()
    assert _pump(qapp, lambda: dlg.result() == dv_utility.QDialog.DialogCode.Rejected and shown)
    assert 'cannot reach' in shown[0]


def test_login_dialog_needs_a_password(qapp):
    dlg = dv_utility.NasLoginDialog(None, 'h', 22, 'u', check=lambda *a: pytest.fail('no check'))
    dlg.submit()
    assert '請輸入密碼' in dlg.status_label.text()


# ── 主畫面: 背景連 NAS 時顯示 loading ─────────────────────────────────────────

class _Progress:
    def __init__(self):
        self.range = (0, 100)
        self.value = 0

    def setRange(self, lo, hi):
        self.range = (lo, hi)

    def maximum(self):
        return self.range[1]

    def setValue(self, v):
        self.value = v


def test_busy_slot_switches_progress_to_indeterminate_and_back():
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.progress = _Progress()
    inst._slot_set_busy(True)
    assert inst.progress.range == (0, 0)
    inst._slot_update_progress(50, 100)              # 開始下載 -> 回到百分比
    assert inst.progress.range == (0, 100) and inst.progress.value == 50
    inst._slot_set_busy(True)
    inst._slot_set_busy(False)
    assert inst.progress.range == (0, 100)


def _worker_inst(target):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    inst.target = target
    events = []
    inst.signals.update_status.connect(lambda s: events.append(('status', s)))
    inst.signals.set_busy.connect(lambda b: events.append(('busy', b)))

    def _stop():
        events.append(('fetch',))
        raise RuntimeError('stop here')
    inst.get_newest_version = _stop
    return inst, events


def test_check_for_update_shows_busy_before_connecting_to_nas():
    inst, events = _worker_inst(SFTP_URL)
    inst.check_for_update()
    i = events.index(('fetch',))
    assert ('busy', True) in events[:i]
    assert any(e[0] == 'status' and '正在連線公司 RT NAS' in e[1] for e in events[:i])


def test_check_for_update_https_tool_has_no_nas_busy():
    inst, events = _worker_inst('https://x/v.json')
    inst.check_for_update()
    assert ('busy', True) not in events
    assert not any(e[0] == 'status' and 'RT NAS' in e[1] for e in events)


def test_fetch_to_file_shows_busy_before_nas_connect(monkeypatch, tmp_path):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    inst.nas_store = object()
    events = []
    inst.signals.set_busy.connect(lambda b: events.append(('busy', b)))

    def _connect(url, store=None, **kw):
        events.append(('connect',))
        raise rtnas.RtnasError('stop here')
    monkeypatch.setattr(dv_utility.rtnas, 'connect_url', _connect)
    with pytest.raises(rtnas.RtnasError):
        inst._fetch_to_file(SFTP_URL.replace('v.json', 'app.zip'), str(tmp_path / 'z'), 'app.zip')
    assert events[:2] == [('busy', True), ('connect',)]
