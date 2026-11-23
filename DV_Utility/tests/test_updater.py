import json
import os

import pytest

import dv_updater
import rtnas
import update_check


def test_parse_args_target_only():
    ns = dv_updater.parse_args(['--target', r'C:\PCDV\DV_Utility.exe'])
    assert ns.target == r'C:\PCDV\DV_Utility.exe'
    assert ns.version == ''


def test_parse_args_accepts_and_ignores_legacy_url_args():
    # 舊版主程式仍會帶 --zip-url/--sha256; 解析得了, 但 run_update 不用它們
    ns = dv_updater.parse_args([
        '--version', 'v20260717', '--zip-url', 'https://x/DV_Utility.zip',
        '--sha256', 'deadbeef', '--target', r'C:\PCDV\DV_Utility.exe', '--parent-pid', '4321',
    ])
    assert ns.version == 'v20260717'
    assert ns.parent_pid == 4321


def test_updater_does_not_import_requests_or_gcs():
    src = open(dv_updater.__file__, encoding='utf-8').read()
    assert 'import requests' not in src
    assert 'storage.googleapis.com' not in src
    assert 'storage.googleapis.com' not in open(update_check.__file__, encoding='utf-8').read()


import zipfile


def test_extract_main_exe(tmp_path):
    zpath = tmp_path / 'DV_Utility.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('DV_Utility.exe', b'NEW-EXE-BYTES')
        z.writestr('dv_updater.exe', b'UPDATER-BYTES')
        z.writestr('resource/x.txt', b'noise')
    out = tmp_path / 'extracted.exe'
    dv_updater.extract_main_exe(str(zpath), str(out))
    assert out.read_bytes() == b'NEW-EXE-BYTES'


def test_extract_main_exe_missing(tmp_path):
    zpath = tmp_path / 'bad.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('readme.txt', b'no exe here')
    import pytest
    with pytest.raises(RuntimeError):
        dv_updater.extract_main_exe(str(zpath), str(tmp_path / 'out.exe'))


def test_swap_with_backup_happy(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    new = tmp_path / 'new.exe'
    new.write_bytes(b'NEW')
    dv_updater.swap_with_backup(str(target), str(new))
    assert target.read_bytes() == b'NEW'
    assert (tmp_path / 'DV_Utility.exe.bak').read_bytes() == b'OLD'
    assert not new.exists()


def test_swap_with_backup_rollback_on_failure(tmp_path, monkeypatch):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    new = tmp_path / 'new.exe'
    new.write_bytes(b'NEW')

    real_replace = os.replace
    calls = {'n': 0}

    def flaky_replace(src, dst):
        calls['n'] += 1
        if calls['n'] == 2:
            raise OSError('boom placing new exe')
        return real_replace(src, dst)

    monkeypatch.setattr(dv_updater.os, 'replace', flaky_replace)
    import pytest
    with pytest.raises(OSError):
        dv_updater.swap_with_backup(str(target), str(new))
    assert target.read_bytes() == b'OLD'


# --- run_update: 版本資訊只取自 NAS 上已簽章的 manifest ------------------------

def _zip_with_exe(path, payload=b'NEW-EXE'):
    import zipfile
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('DV_Utility.exe', payload)
        z.writestr('dv_updater.exe', b'UPD')


def test_run_update_happy(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    info = {'version': 'v20261201', 'zip_url': 'sftp://h/x.zip', 'sha256': 'abc'}
    seen = []

    def fake_download(i, dest, progress_cb=None):
        assert i is info
        _zip_with_exe(dest)
        progress_cb(5, 10)

    v = dv_updater.run_update(str(target), seen.append, lambda d, t: seen.append((d, t)),
                              fetch_manifest=lambda: info, download=fake_download)
    assert v == 'v20261201'
    assert target.read_bytes() == b'NEW-EXE'
    assert (tmp_path / 'DV_Utility.exe.bak').read_bytes() == b'OLD'
    assert (5, 10) in seen
    assert not (tmp_path / 'DV_Utility.update.part').exists()


def test_run_update_manifest_failure_keeps_old(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')

    def bad_manifest():
        raise rtnas.IntegrityError('bad signature')

    with pytest.raises(rtnas.IntegrityError):
        dv_updater.run_update(str(target), lambda t: None, lambda d, t: None,
                              fetch_manifest=bad_manifest,
                              download=lambda *a, **k: pytest.fail('must not download'))
    assert target.read_bytes() == b'OLD'


def test_run_update_download_failure_cleans_part(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    info = {'version': 'v20261201', 'zip_url': 'sftp://h/x.zip', 'sha256': 'abc'}

    def broken(i, dest, progress_cb=None):
        with open(dest, 'wb') as f:
            f.write(b'half')
        raise rtnas.RtnasError('network')

    with pytest.raises(rtnas.RtnasError):
        dv_updater.run_update(str(target), lambda t: None, lambda d, t: None,
                              fetch_manifest=lambda: info, download=broken)
    assert target.read_bytes() == b'OLD'
    assert not (tmp_path / 'DV_Utility.update.part').exists()


def test_download_zip_uses_saved_login_and_checks_sha(tmp_path, monkeypatch):
    calls = {}

    class FakeSession:
        def __enter__(self):
            return 'SFTP'

        def __exit__(self, *a):
            calls['closed'] = True

    def fake_connect_url(url, store=None):
        calls['url'] = url
        return FakeSession(), '/Audio/x.zip'

    def fake_download_to_file(sftp, remote, dest, expected_sha256=None, progress_cb=None):
        calls.update(sftp=sftp, remote=remote, sha=expected_sha256)

    monkeypatch.setattr(rtnas, 'download_to_file', fake_download_to_file)
    dv_updater.download_zip({'zip_url': 'sftp://h/Audio/x.zip', 'sha256': 'abc'},
                            str(tmp_path / 'p'), connect_url=fake_connect_url)
    assert calls == {'url': 'sftp://h/Audio/x.zip', 'sftp': 'SFTP', 'remote': '/Audio/x.zip',
                     'sha': 'abc', 'closed': True}


@pytest.mark.parametrize('exc, words', [
    (rtnas.AuthError('x'), '登入'),
    (rtnas.IntegrityError('x'), '驗證失敗'),
    (FileNotFoundError('x'), '找不到'),
    (rtnas.RtnasError('x'), '內網'),
])
def test_failure_message(exc, words):
    assert words in dv_updater.failure_message(exc)
