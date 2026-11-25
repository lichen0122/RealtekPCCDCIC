"""GCS + RT NAS dual-source tools: GCS first, the NAS only when GCS fails; the source is shown.

setting.json value {"gcs": https manifest, "nas": sftp manifest}. No network: requests.get and
rtnas.fetch_url / verify_signed_json are replaced.
"""

import io
import json
import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests  # noqa: E402

import dv_utility  # noqa: E402
import rtnas  # noqa: E402
import setting_menu  # noqa: E402

GCS = 'https://storage.googleapis.com/realtek-pccdcic-dv/Tool/tool_version.json'
NAS = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/Tool/tool_version.json'
ENTRY = {'gcs': GCS, 'nas': NAS}
GCS_ZIP = 'https://storage.googleapis.com/realtek-pccdcic-dv/Tool/t.zip'
NAS_ZIP = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/Tool/t.zip'


def _manifest(version, zip_url):
    return {'target_directory': 'Tool', 'current_version': 'version.json',
            'exe_name': 'app/app.exe', 'version': version, 'release_note': 'note',
            'update_info': [{'url': zip_url, 'overwrite': True, 'extract_to': 'target_directory',
                             'sha256': '0' * 64}]}


class _Resp:
    def __init__(self, body):
        self.text = json.dumps(body)

    def raise_for_status(self):
        pass


class _Store:
    def __init__(self, creds=('u', 'p')):
        self.creds = creds

    def load(self):
        return self.creds

    def clear(self):
        self.creds = None


def _inst(tmp_path, entry=ENTRY):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    inst.sources = setting_menu.sources(entry)
    inst.target = inst.sources[0]
    inst.get_install_dir = lambda: str(tmp_path)
    inst.nas_store = _Store()
    rec = {'status': [], 'release': [], 'launched': []}
    inst.signals.update_status.connect(rec['status'].append)
    inst.signals.update_release.connect(lambda v, n: rec['release'].append(v))
    inst.start = lambda: rec['launched'].append(inst.target)
    return inst, rec


def _no_thread(monkeypatch):
    """Record the functions check_for_update / download_file hand to a thread instead of running them."""
    calls = []

    class _T:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            calls.append(self.target.__name__)

    monkeypatch.setattr(dv_utility.threading, 'Thread', _T)
    return calls


def _gcs_down(*a, **k):
    raise requests.ConnectionError('storage.googleapis.com unreachable')


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


def _nas_serves(monkeypatch, manifest):
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', lambda url, store=None: b'{}')
    monkeypatch.setattr(dv_utility.rtnas, 'verify_signed_json',
                        lambda body, expected_path=None: dict(manifest))


# -- setting.json --------------------------------------------------------------------------------

def _write(tmp_path, data):
    p = tmp_path / 'setting.json'
    p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return p


def test_menu_accepts_gcs_nas_pair_and_single_sources(tmp_path):
    data = {'雙來源': ENTRY, '只有 GCS': GCS, '只有 NAS': NAS, '只寫 nas': {'nas': NAS}}
    assert setting_menu.validate(_write(tmp_path, data)) == data


@pytest.mark.parametrize('entry', [
    {'gcs': NAS, 'nas': NAS},                       # gcs must be https
    {'gcs': GCS, 'nas': GCS},                       # nas must be the RT NAS
    {'gcs': GCS, 'nas': 'sftp://evil.example/Audio/v.json'},
    {'gcs': GCS, 'ftp': NAS},                       # unknown key
    {},
    [GCS, NAS],
])
def test_menu_rejects_bad_pairs(tmp_path, entry):
    with pytest.raises(ValueError):
        setting_menu.validate(_write(tmp_path, {'t': entry}))


def test_sources_order_is_gcs_then_nas():
    assert setting_menu.sources({'nas': NAS, 'gcs': GCS}) == [GCS, NAS]
    assert setting_menu.sources(GCS) == [GCS]
    assert setting_menu.source_label(GCS) == 'GCS' and setting_menu.source_label(NAS) == 'NAS'


# -- manifest ------------------------------------------------------------------------------------

def test_gcs_reachable_never_touches_the_nas(tmp_path, monkeypatch):
    inst, rec = _inst(tmp_path)
    calls = _no_thread(monkeypatch)
    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda url, timeout=None: _Resp(_manifest('2', GCS_ZIP)))
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', lambda *a, **k: pytest.fail('NAS used while GCS is up'))

    inst.check_for_update()

    assert inst.target == GCS and calls == ['download_file']
    assert not any('RT NAS' in s for s in rec['status'])
    # a later GCS + NAS outage can still open the installed tool
    assert dv_utility.load_nas_manifest_cache(str(tmp_path), NAS)['exe_name'] == 'app/app.exe'


def test_gcs_down_switches_to_nas_and_says_so(tmp_path, monkeypatch):
    inst, rec = _inst(tmp_path)
    calls = _no_thread(monkeypatch)
    monkeypatch.setattr(dv_utility.requests, 'get', _gcs_down)
    _nas_serves(monkeypatch, _manifest('2', NAS_ZIP))

    inst.check_for_update()

    assert inst.target == NAS and calls == ['download_file']
    assert any('改從公司 RT NAS' in s for s in rec['status'])
    assert inst.extract_info[0][0] == NAS_ZIP


def test_gcs_only_tool_does_not_fall_back(tmp_path, monkeypatch):
    inst, rec = _inst(tmp_path, entry=GCS)
    _no_thread(monkeypatch)
    monkeypatch.setattr(dv_utility.requests, 'get', _gcs_down)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', lambda *a, **k: pytest.fail('no NAS source'))

    inst.check_for_update()

    assert inst.target == GCS
    assert any('請確認網路連線' in s for s in rec['status'])


def test_both_down_opens_the_installed_tool(tmp_path, monkeypatch):
    inst, rec = _inst(tmp_path)
    dv_utility.save_nas_manifest_cache(str(tmp_path), NAS, _manifest('1', GCS_ZIP))
    (tmp_path / 'Tool' / 'app').mkdir(parents=True)
    (tmp_path / 'Tool' / 'app' / 'app.exe').write_bytes(b'MZ')
    (tmp_path / 'Tool' / 'version.json').write_text(json.dumps({'version': '1'}), encoding='utf-8')
    monkeypatch.setattr(dv_utility.requests, 'get', _gcs_down)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', _raise(rtnas.RtnasError('cannot reach rt-nas')))

    inst.check_for_update()

    assert rec['launched'] == [NAS]
    assert any('GCS 與公司 RT NAS 都無法連線' in r for r in rec['release'])


def test_both_down_without_install_names_both(tmp_path, monkeypatch):
    inst, rec = _inst(tmp_path)
    monkeypatch.setattr(dv_utility.requests, 'get', _gcs_down)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', _raise(rtnas.RtnasError('down')))

    inst.check_for_update()

    assert rec['launched'] == []
    assert any('GCS 與公司 RT NAS 都無法連線' in s for s in rec['status'])


def test_nas_login_needed_after_fallback_is_asked_on_next_click(tmp_path, monkeypatch):
    inst, rec = _inst(tmp_path)
    inst.nas_store = _Store(creds=None)
    monkeypatch.setattr(dv_utility.requests, 'get', _gcs_down)
    monkeypatch.setattr(dv_utility.rtnas, 'fetch_url', _raise(rtnas.AuthError('no saved login')))

    inst.check_for_update()

    assert any('登入公司 RT NAS' in s for s in rec['status'])
    # next click: start_update starts from GCS again, but the NAS login is asked up front
    inst.target = inst.sources[0]
    assert inst.nas_login_url() == NAS
    assert inst.needs_nas_login()


def test_dual_tool_needs_no_nas_login_up_front(tmp_path):
    inst, _ = _inst(tmp_path)
    inst.nas_store = _Store(creds=None)
    assert not inst.needs_nas_login()


# -- download ------------------------------------------------------------------------------------

def _download_inst(tmp_path, url):
    inst, rec = _inst(tmp_path)
    inst.extract_info = [(url, str(tmp_path / 'PCDV' / 'Tool'), True)]
    inst.sha_by_url = {}
    inst.update_required = True
    inst.newest_version_info = {'version': '2'}
    inst.set_current_version = lambda v: None
    return inst, rec


class _Stream:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {'content-length': str(len(payload))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self.payload

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


def test_download_status_names_gcs(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('a.txt', b'A')
    inst, rec = _download_inst(tmp_path, GCS_ZIP)
    monkeypatch.setattr(dv_utility.requests, 'get', lambda *a, **k: _Stream(buf.getvalue()))
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert '下載中 (來源: GCS) ...' in rec['status']
    assert rec['launched'] == [GCS]


def test_gcs_zip_download_failure_restarts_from_nas(tmp_path, monkeypatch):
    inst, rec = _download_inst(tmp_path, GCS_ZIP)
    calls = _no_thread(monkeypatch)
    monkeypatch.setattr(dv_utility.requests, 'get', _gcs_down)
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert inst.target == NAS and calls == ['check_for_update']
    assert any('改從公司 RT NAS' in s for s in rec['status'])
    assert not any('下載失敗' in s for s in rec['status'])


def test_release_label_names_the_source(tmp_path):
    inst = dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)
    inst.signals = dv_utility.WorkerSignals()
    got = []
    inst.signals.update_release.connect(lambda v, n: got.append(v))
    inst.newest_version_info = {'version': '2'}
    inst.release_note = ''
    inst.target_directory = str(tmp_path)
    inst.exe_name = 'missing.exe'
    inst.work_dir = str(tmp_path)
    inst.tool_history = 'Tool'
    inst.target = NAS
    inst.start()
    assert got[0] == '當前版本 2 (來源: NAS)'
