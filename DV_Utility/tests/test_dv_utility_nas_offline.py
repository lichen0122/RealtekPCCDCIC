"""不在公司內網 (沒連 VPN) 時: 清楚的中文提示; 已安裝的 NAS 工具仍可直接開啟。"""

import json
import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import dv_utility  # noqa: E402
import rtnas  # noqa: E402

SFTP_URL = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/Tool/tool_version.json'
MANIFEST = {'target_directory': 'Tool', 'current_version': 'version.json',
            'exe_name': 'app/app.exe', 'version': '2026-11-20-000', 'release_note': '說明',
            'update_info': []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _inst(tmp_path, target=SFTP_URL):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    inst.target = target
    inst.get_install_dir = lambda: str(tmp_path)
    inst.nas_store = type('S', (), {'load': lambda self: ('u', 'p')})()
    statuses, releases, launched, enabled = [], [], [], []
    inst.signals.update_status.connect(statuses.append)
    inst.signals.update_release.connect(lambda v, n: releases.append(v))
    inst.signals.set_btn_enabled.connect(enabled.append)
    inst.start = lambda: launched.append(inst.target_directory + '/' + inst.exe_name)
    return inst, statuses, releases, launched, enabled


def _unreachable(*a, **k):
    raise rtnas.RtnasError('cannot reach rt-nas.realsil.com.cn:22: timed out')


def test_successful_nas_manifest_is_remembered(tmp_path, monkeypatch):
    inst, *_ = _inst(tmp_path)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', lambda *a, **k: b'{}')
    monkeypatch.setattr(dv_utility.rtnas, 'verify_signed_json', lambda body, expected_path=None: dict(MANIFEST))
    inst.get_newest_version()
    cached = dv_utility.load_nas_manifest_cache(str(tmp_path), SFTP_URL)
    assert cached['exe_name'] == 'app/app.exe' and cached['target_directory'] == 'Tool'


def test_unreachable_nas_opens_the_installed_tool(tmp_path, monkeypatch):
    inst, statuses, releases, launched, enabled = _inst(tmp_path)
    dv_utility.save_nas_manifest_cache(str(tmp_path), SFTP_URL, MANIFEST)
    tool = tmp_path / 'Tool'
    (tool / 'app').mkdir(parents=True)
    (tool / 'app' / 'app.exe').write_bytes(b'MZ')
    (tool / 'version.json').write_text(json.dumps({'version': '2026-11-19-000'}), encoding='utf-8')
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', _unreachable)

    inst.check_for_update()

    assert launched == [str(tmp_path) + '/Tool/app/app.exe']
    assert any('2026-11-19-000' in r and '無法連線公司 RT NAS' in r for r in releases)


def test_unreachable_nas_without_install_says_connect_to_intranet(tmp_path, monkeypatch):
    inst, statuses, releases, launched, enabled = _inst(tmp_path)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', _unreachable)
    inst.check_for_update()
    assert launched == []
    assert any('公司網路或 VPN' in s for s in statuses)
    assert enabled[-1] is True


def test_cached_manifest_but_exe_missing_does_not_launch(tmp_path, monkeypatch):
    inst, statuses, releases, launched, enabled = _inst(tmp_path)
    dv_utility.save_nas_manifest_cache(str(tmp_path), SFTP_URL, MANIFEST)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', _unreachable)
    inst.check_for_update()
    assert launched == []
    assert any('公司網路或 VPN' in s for s in statuses)


def test_auth_error_is_not_treated_as_offline(tmp_path, monkeypatch):
    inst, statuses, releases, launched, enabled = _inst(tmp_path)
    inst.nas_store = type('S', (), {'load': lambda self: None, 'clear': lambda self: None})()
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url',
                        lambda *a, **k: (_ for _ in ()).throw(rtnas.AuthError('bad')))
    inst.check_for_update()
    assert launched == []
    assert not any('VPN' in s for s in statuses)


def test_https_tool_offline_keeps_old_message(tmp_path, monkeypatch):
    inst, statuses, releases, launched, enabled = _inst(tmp_path, target='https://x/v.json')
    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    inst.check_for_update()
    assert launched == []
    assert any('檢查更新失敗' in s for s in statuses)


def test_login_dialog_unreachable_message_mentions_vpn(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(dv_utility.QMessageBox, 'warning', lambda *a, **k: shown.append(a[-1]))
    dlg = dv_utility.NasLoginDialog(None, 'h', 22, 'u',
                                    check=lambda *a: 'cannot reach h:22: timed out')
    dlg.pw_edit.setText('pw')
    dlg.submit()
    import time
    end = time.time() + 3
    while time.time() < end and not shown:
        qapp.processEvents()
        time.sleep(0.01)
    assert shown and '公司網路或 VPN' in shown[0]
