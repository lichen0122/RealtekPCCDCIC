import update_check as uc


def test_version_key_parses_date_and_build():
    assert uc._version_key('v20260716') == (2026, 7, 16, 0)
    assert uc._version_key('v20260716.3') == (2026, 7, 16, 3)
    assert uc._version_key('20260716.3') == (2026, 7, 16, 3)   # 無 v 前綴亦可
    assert uc._version_key('garbage') == (0, 0, 0, 0)


def test_is_newer():
    assert uc.is_newer('v20260716.2', 'v20260716.1') is True
    assert uc.is_newer('v20260717', 'v20260716') is True
    assert uc.is_newer('v20260716.10', 'v20260716.2') is True
    assert uc.is_newer('v20260716', 'v20260716') is False
    assert uc.is_newer('v20260716.1', 'v20260716.2') is False


import json

import pytest
import requests

import rtnas


def _gcs_down(*a, **k):
    raise requests.ConnectionError('storage.googleapis.com unreachable')


def _signed(monkeypatch, **over):
    private, public = rtnas.generate_signing_key()
    monkeypatch.setattr(rtnas, 'TRUSTED_RELEASE_KEYS', (public,))
    data = {'version': 'v20260717', 'zip_url': 'sftp://rt-nas.realsil.com.cn/x/DV_Utility.zip',
            'sha256': 'abc', 'size': 1, 'release_note': '修了一些東西',
            'nas_path': rtnas.parse_sftp_url(uc.VERSION_URL)[2]}
    data.update(over)
    return json.dumps(rtnas.sign_manifest(data, private)).encode('utf-8')


def test_version_url_is_on_the_nas():
    assert rtnas.is_sftp_url(uc.VERSION_URL)
    assert uc.VERSION_URL.endswith('/DVUtility/version.json')


def test_check_latest_version_returns_info_when_newer(monkeypatch):
    raw = _signed(monkeypatch)
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    seen = {}

    def fetch(url, store=None):
        seen['url'] = url
        return raw

    info = uc.check_latest_version('v20260716', fetch=fetch, get=_gcs_down)
    assert info['version'] == 'v20260717'
    assert seen['url'] == uc.VERSION_URL


def test_check_latest_version_none_when_not_newer(monkeypatch):
    raw = _signed(monkeypatch, version='v20260716')
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    assert uc.check_latest_version('v20260716', fetch=lambda u, store=None: raw, get=_gcs_down) is None


def test_check_latest_version_none_when_not_packaged(monkeypatch):
    monkeypatch.setattr(uc, '_is_packaged', lambda: False)
    assert uc.check_latest_version('v20260716') is None


def test_check_latest_version_none_on_nas_error(monkeypatch):
    def boom(*a, **k):
        raise rtnas.AuthError('no saved RT NAS credentials')
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    assert uc.check_latest_version('v20260716', fetch=boom, get=_gcs_down) is None


def test_fetch_manifest_rejects_unsigned():
    raw = json.dumps({'version': 'v20990101', 'zip_url': 'sftp://h/x.zip', 'sha256': 'a'}).encode()
    with pytest.raises(rtnas.IntegrityError):
        uc.fetch_nas_manifest(fetch=lambda u, store=None: raw)


def test_fetch_manifest_rejects_wrong_nas_path(monkeypatch):
    raw = _signed(monkeypatch, nas_path='/Audio/Temp_RT/cychang/Other/version.json')
    with pytest.raises(rtnas.IntegrityError):
        uc.fetch_nas_manifest(fetch=lambda u, store=None: raw)


def test_fetch_manifest_rejects_gcs_zip_url(monkeypatch):
    raw = _signed(monkeypatch, zip_url='https://example.invalid/DVUtility/DV_Utility.zip')
    with pytest.raises(rtnas.IntegrityError):
        uc.fetch_nas_manifest(fetch=lambda u, store=None: raw)


def test_fetch_manifest_rejects_missing_sha(monkeypatch):
    raw = _signed(monkeypatch, sha256='')
    with pytest.raises(rtnas.IntegrityError):
        uc.fetch_nas_manifest(fetch=lambda u, store=None: raw)


def test_cleanup_stale_backup_removes_bak(tmp_path, monkeypatch):
    exe = tmp_path / 'DV_Utility.exe'
    exe.write_bytes(b'app')
    bak = tmp_path / 'DV_Utility.exe.bak'
    bak.write_bytes(b'old')
    monkeypatch.setattr(uc, 'launcher_exe', lambda: str(exe))
    uc.cleanup_stale_backup()
    assert not bak.exists()
    assert exe.exists()   # 只刪 .bak, 不動本體


def test_cleanup_stale_backup_noop_when_not_packaged(monkeypatch):
    monkeypatch.setattr(uc, 'launcher_exe', lambda: None)
    uc.cleanup_stale_backup()   # 不應丟例外


def test_check_for_update_raises_auth_error(monkeypatch):
    def no_login(*a, **k):
        raise rtnas.AuthError('no saved RT NAS credentials')
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    with pytest.raises(rtnas.AuthError):
        uc.check_for_update('v20260716', fetch=no_login, get=_gcs_down)
