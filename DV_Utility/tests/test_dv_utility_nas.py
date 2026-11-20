"""DV_Utility 從公司 RT NAS (SFTP) 下載工具的測試。

工具的 version.json / update_info.url 若為 sftp://, 改用 rtnas 以使用者自己的網域帳號下載;
https:// 照舊走 requests。沒有網路、沒有 paramiko —— rtnas.connect_url / fetch_url 以替身取代。
"""

import hashlib
import io
import json
import zipfile

import pytest

import dv_utility
import rtnas

ZIP = 'Tool.zip'
SFTP_URL = f'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/Tool/{ZIP}'
REMOTE = f'/Audio/Temp_RT/lichen.liu/Tool/{ZIP}'


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


class _FakeSftp:
    def __init__(self, files):
        self.files = files

    def getfo(self, path, fl, callback=None):
        data = self.files[path]
        fl.write(data)
        if callback:
            callback(len(data), len(data))
        return len(data)


class _Session:
    def __init__(self, sftp):
        self.sftp = sftp

    def __enter__(self):
        return self.sftp

    def __exit__(self, *exc):
        return False


class _FakeStore:
    def __init__(self, creds=('rtdomain\\u', 'pw')):
        self.creds = creds
        self.cleared = False

    def load(self):
        return self.creds

    def save(self, user, pw):
        self.creds = (user, pw)

    def clear(self):
        self.cleared = True
        self.creds = None


def _make_inst(monkeypatch, tmp_path, files, sha=None):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    extract_dir = tmp_path / 'PCDV' / 'Tool'
    inst.extract_info = [(SFTP_URL, str(extract_dir), True)]
    inst.sha_by_url = {SFTP_URL: sha} if sha else {}
    inst.update_required = True
    inst.newest_version_info = {'version': '9.9.9'}
    stage = tmp_path / 'stage'
    stage.mkdir()
    inst.get_install_dir = lambda: str(stage)
    statuses, versions = [], []
    inst.signals.update_status.connect(statuses.append)
    inst.set_current_version = versions.append
    inst.start = lambda: None
    store = _FakeStore()
    inst.nas_store = store
    monkeypatch.setattr(dv_utility.rtnas, 'connect_url',
                        lambda url, store=None, **kw: (_Session(_FakeSftp(files)),
                                                       rtnas.parse_sftp_url(url)[2]))
    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda *a, **k: pytest.fail('sftp URL must not use requests'))
    return inst, statuses, versions, extract_dir, stage, store


def test_download_file_installs_from_sftp(monkeypatch, tmp_path):
    payload = _zip_bytes({'a.txt': b'A'})
    inst, statuses, versions, extract_dir, stage, _ = _make_inst(
        monkeypatch, tmp_path, {REMOTE: payload})
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == [{'version': '9.9.9'}]
    assert (extract_dir / 'a.txt').read_bytes() == b'A'
    assert list(stage.iterdir()) == []          # 暫存 zip 已清掉


def test_download_file_verifies_sha256_from_manifest(monkeypatch, tmp_path):
    payload = _zip_bytes({'a.txt': b'A'})
    good = hashlib.sha256(payload).hexdigest()
    inst, _s, versions, extract_dir, _st, _ = _make_inst(
        monkeypatch, tmp_path, {REMOTE: payload}, sha=good)
    monkeypatch.chdir(tmp_path)
    inst.download_file()
    assert versions == [{'version': '9.9.9'}]


def test_download_file_rejects_sha256_mismatch(monkeypatch, tmp_path):
    payload = _zip_bytes({'a.txt': b'A'})
    inst, statuses, versions, extract_dir, stage, _ = _make_inst(
        monkeypatch, tmp_path, {REMOTE: payload}, sha='0' * 64)
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == []                       # 版本檔不得寫入
    assert not extract_dir.exists()             # 不得解壓
    assert list(stage.iterdir()) == []
    assert any('檔案驗證失敗' in s for s in statuses)


def test_download_file_auth_error_forgets_login_and_says_so(monkeypatch, tmp_path):
    inst, statuses, versions, _e, _st, store = _make_inst(monkeypatch, tmp_path, {})

    def _reject(url, store=None, **kw):
        raise rtnas.AuthError('rejected')

    monkeypatch.setattr(dv_utility.rtnas, 'connect_url', _reject)
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == []
    assert store.cleared                        # 下次點選會重新詢問密碼
    assert any('RT NAS 登入失敗' in s for s in statuses)


def test_get_newest_version_reads_sftp_manifest(monkeypatch):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.nas_store = _FakeStore()
    inst.target = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/Tool/v.json'
    inst.get_install_dir = lambda: 'C:/PCDV'
    manifest = {
        'target_directory': 'Tool', 'current_version': 'v.json', 'exe_name': 'app/app.exe',
        'version': '1', 'release_note': '說明',
        'update_info': [{'url': SFTP_URL, 'overwrite': True, 'extract_to': 'install_dir',
                         'sha256': 'ab' * 32}],
    }
    seen = []
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url',
                        lambda url, store=None, **kw: seen.append(url)
                        or json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda *a, **k: pytest.fail('sftp manifest must not use requests'))

    inst.get_newest_version()
    inst.get_extract_info()

    assert seen == [inst.target]
    assert inst.release_note == '說明'
    assert inst.sha_by_url == {SFTP_URL: 'ab' * 32}


def test_get_extract_info_keeps_https_entries_without_sha():
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.work_dir = 'W'
    inst.target_directory = 'T'
    inst.update_info = [{'url': 'https://x/a.zip', 'overwrite': False, 'extract_to': 'work_dir'}]
    inst.get_extract_info()
    assert inst.extract_info == [('https://x/a.zip', 'W', False)]
    assert inst.sha_by_url == {}


# ── 啟動前登入 (GUI thread) ───────────────────────────────────────────────────

def _bare_for_login(target, creds):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.target = target
    inst.nas_store = _FakeStore(creds)
    return inst


def test_needs_nas_login_only_for_sftp_without_saved_login():
    assert _bare_for_login(SFTP_URL, None).needs_nas_login()
    assert not _bare_for_login(SFTP_URL, ('u', 'p')).needs_nas_login()
    assert not _bare_for_login('https://x/v.json', None).needs_nas_login()


def test_ensure_work_dir_aborts_when_login_cancelled(monkeypatch, tmp_path):
    inst = _bare_for_login(SFTP_URL, None)
    inst.choose_work_dir = type('C', (), {'currentText': lambda self: str(tmp_path)})()
    inst.prompt_nas_login = lambda: False
    started = []
    monkeypatch.setattr(dv_utility.threading, 'Thread',
                        lambda *a, **k: started.append(k) or pytest.fail('must not start'))
    inst.ensure_work_dir()
    assert started == []
