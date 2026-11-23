"""DV_Utility asks for the RT NAS login when the update check cannot read the NAS."""

import dv_utility
import rtnas
import update_check


class Store:
    def __init__(self, creds=None):
        self.creds = creds
        self.cleared = False

    def load(self):
        return self.creds

    def clear(self):
        self.cleared = True
        self.creds = None


def _inst(monkeypatch, store, raise_auth=True):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    inst.version = 'v20261123'
    inst.nas_store = store
    emitted = []
    inst.signals.update_needs_login.connect(emitted.append)

    def check(current, store=None):
        if raise_auth:
            raise rtnas.AuthError('no saved RT NAS credentials')
        return None

    monkeypatch.setattr(update_check, 'check_for_update', check)
    return inst, emitted


def test_no_saved_login_asks_for_login(monkeypatch):
    inst, emitted = _inst(monkeypatch, Store(None))
    inst._check_update()
    assert emitted == [False]


def test_rejected_password_asks_again(monkeypatch):
    inst, emitted = _inst(monkeypatch, Store(('rtdomain\\u', 'old')))
    inst._check_update()
    assert emitted == [True]


def test_slot_prompts_with_update_url_and_rechecks(monkeypatch):
    store = Store(('rtdomain\\u', 'old'))
    inst, _ = _inst(monkeypatch, store)
    asked, started = [], []
    inst.prompt_nas_login = lambda url=None, intro=None: asked.append((url, intro)) or True
    monkeypatch.setattr(dv_utility.threading, 'Thread',
                        lambda target, daemon: type('T', (), {'start': lambda self: started.append(target)})())
    inst._slot_update_needs_login(True)
    assert store.cleared
    assert asked == [(update_check.VERSION_URL, inst._UPDATE_LOGIN_INTRO)]
    assert started == [inst._check_update]
    inst._slot_update_needs_login(False)          # 同一次啟動只問一次
    assert len(asked) == 1


def test_slot_cancel_does_not_recheck(monkeypatch):
    inst, _ = _inst(monkeypatch, Store(None))
    inst.prompt_nas_login = lambda url=None, intro=None: False
    started = []
    monkeypatch.setattr(dv_utility.threading, 'Thread',
                        lambda target, daemon: type('T', (), {'start': lambda self: started.append(target)})())
    inst._slot_update_needs_login(False)
    assert started == []


def test_login_dialog_intro_text():
    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication.instance() or QApplication([])
    dlg = dv_utility.NasLoginDialog(None, 'h', 22, 'u', intro='DV_Utility 的更新檔放在公司 RT NAS')
    texts = [w.text() for w in dlg.findChildren(QLabel)]
    assert any(t.startswith('DV_Utility 的更新檔放在公司 RT NAS') for t in texts)
    dlg2 = dv_utility.NasLoginDialog(None, 'h', 22, 'u')
    assert any(t.startswith('此工具放在公司 RT NAS') for t in [w.text() for w in dlg2.findChildren(QLabel)])
