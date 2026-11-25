"""DV_Utility self-update: GCS version.json first, the signed RT NAS one only when GCS fails.

No network: requests.get, rtnas.fetch_url and the NAS download are replaced.
"""

import hashlib
import io
import json
import zipfile

import pytest
import requests

import dv_updater
import rtnas
import update_check as uc

GCS_ZIP = uc.GCS_ZIP_PREFIX + 'DV_Utility.zip'
NAS_ZIP = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/DVUtility/DV_Utility.zip'


class _Resp:
    def __init__(self, body=None, payload=b'', status=200):
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.payload = payload
        self.status = status
        self.headers = {'content-length': str(len(payload))}

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f'{self.status}')

    def iter_content(self, chunk_size=8192):
        yield self.payload

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


def _gcs_manifest(**over):
    data = {'version': 'v20261201', 'zip_url': GCS_ZIP, 'sha256': 'abc', 'size': 1}
    data.update(over)
    return data


def _gcs_down(*a, **k):
    raise requests.ConnectionError('storage.googleapis.com unreachable')


def _signed_nas(monkeypatch, **over):
    private, public = rtnas.generate_signing_key()
    monkeypatch.setattr(rtnas, 'TRUSTED_RELEASE_KEYS', (public,))
    data = {'version': 'v20261201', 'zip_url': NAS_ZIP, 'sha256': 'abc', 'size': 1,
            'nas_path': rtnas.parse_sftp_url(uc.VERSION_URL)[2]}
    data.update(over)
    return json.dumps(rtnas.sign_manifest(data, private)).encode('utf-8')


# -- update_check ----------------------------------------------------------------------------

def test_gcs_manifest_is_used_first(monkeypatch):
    seen = []

    def get(url, timeout=None):
        seen.append(url)
        return _Resp(_gcs_manifest())

    info = uc.fetch_manifest(fetch=lambda *a, **k: pytest.fail('NAS used while GCS is up'), get=get)
    assert info['source'] == 'GCS' and info['zip_url'] == GCS_ZIP
    assert seen == [uc.GCS_VERSION_URL]


def test_gcs_down_falls_back_to_signed_nas(monkeypatch):
    raw = _signed_nas(monkeypatch)
    fell = []
    info = uc.fetch_manifest(fetch=lambda u, store=None: raw, get=_gcs_down, on_fallback=fell.append)
    assert info['source'] == 'NAS' and info['zip_url'] == NAS_ZIP
    assert isinstance(fell[0], requests.ConnectionError)


@pytest.mark.parametrize('resp', [
    _Resp(_gcs_manifest(), status=404),
    _Resp('not json'),
    _Resp(_gcs_manifest(zip_url='https://evil.example/DV_Utility.zip')),
    _Resp(_gcs_manifest(zip_url=NAS_ZIP)),
    _Resp(_gcs_manifest(sha256='')),
])
def test_bad_gcs_manifest_falls_back_to_nas(monkeypatch, resp):
    raw = _signed_nas(monkeypatch)
    info = uc.fetch_manifest(fetch=lambda u, store=None: raw, get=lambda *a, **k: resp)
    assert info['source'] == 'NAS'


def test_gcs_down_and_no_nas_login_raises_auth_error(monkeypatch):
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)

    def no_login(*a, **k):
        raise rtnas.AuthError('no saved RT NAS credentials')

    with pytest.raises(rtnas.AuthError):
        uc.check_for_update('v20261101', fetch=no_login, get=_gcs_down)


def test_gcs_reachable_needs_no_nas_login(monkeypatch):
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)

    def no_login(*a, **k):
        raise rtnas.AuthError('no saved RT NAS credentials')

    info = uc.check_for_update('v20261101', fetch=no_login,
                               get=lambda *a, **k: _Resp(_gcs_manifest()))
    assert info['version'] == 'v20261201' and info['source'] == 'GCS'


# -- dv_updater ------------------------------------------------------------------------------

def _zip_payload():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('DV_Utility.exe', b'NEW-EXE')
    return buf.getvalue()


def test_download_zip_from_gcs_checks_sha(tmp_path):
    payload = _zip_payload()
    good = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / 'part'
    dv_updater.download_zip({'zip_url': GCS_ZIP, 'sha256': good}, str(dest),
                            get=lambda *a, **k: _Resp(payload=payload))
    assert dest.read_bytes() == payload

    with pytest.raises(rtnas.IntegrityError):
        dv_updater.download_zip({'zip_url': GCS_ZIP, 'sha256': '0' * 64}, str(dest),
                                get=lambda *a, **k: _Resp(payload=payload))
    assert not dest.exists()


def test_run_update_shows_gcs_source(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    statuses = []

    def download(info, dest, progress_cb=None):
        with open(dest, 'wb') as f:
            f.write(_zip_payload())

    v = dv_updater.run_update(str(target), statuses.append, lambda d, t: None,
                              fetch_manifest=lambda **k: _gcs_manifest(), download=download)
    assert v == 'v20261201' and target.read_bytes() == b'NEW-EXE'
    assert any('來源: GCS' in s for s in statuses)


def test_run_update_gcs_download_failure_retries_from_nas(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    statuses, urls = [], []
    nas_info = {'version': 'v20261201', 'zip_url': NAS_ZIP, 'sha256': 'abc', 'source': 'NAS'}

    def download(info, dest, progress_cb=None):
        urls.append(info['zip_url'])
        if info['zip_url'] == GCS_ZIP:
            with open(dest, 'wb') as f:
                f.write(b'half')
            raise requests.ConnectionError('reset')
        with open(dest, 'wb') as f:
            f.write(_zip_payload())

    v = dv_updater.run_update(str(target), statuses.append, lambda d, t: None,
                              fetch_manifest=lambda **k: _gcs_manifest(), download=download,
                              fetch_nas_manifest=lambda: nas_info)
    assert v == 'v20261201' and target.read_bytes() == b'NEW-EXE'
    assert urls == [GCS_ZIP, NAS_ZIP]
    assert any('改從公司 RT NAS' in s for s in statuses)
    assert any('來源: NAS' in s for s in statuses)


def test_run_update_nas_download_failure_is_not_retried(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')

    def download(info, dest, progress_cb=None):
        raise rtnas.IntegrityError('sha')

    with pytest.raises(rtnas.IntegrityError):
        dv_updater.run_update(str(target), lambda s: None, lambda d, t: None,
                              fetch_manifest=lambda **k: {'version': 'v1', 'zip_url': NAS_ZIP,
                                                          'sha256': 'a'},
                              download=download,
                              fetch_nas_manifest=lambda: pytest.fail('already on the NAS'))
    assert target.read_bytes() == b'OLD'


def test_failure_message_names_both_sources():
    assert 'GCS 與公司 RT NAS' in dv_updater.failure_message(requests.ConnectionError('x'))
