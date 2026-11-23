"""A release file missing on the server must not be reported as a network problem.

Background (2026-11-23): a tool maintainer published version_RTK.json 2.11.0 to GCS without the
zip, the download got HTTP 404, and DV_Utility said 「下載失敗, 請確認網路連線 (詳見 log)」: the
user looked for a network fault and could not find the log. Now a 404 (or a missing file on the
NAS) says the release file is missing and to tell the maintainer, and every failure message shows
the log path.
"""

import requests

import dv_utility
from test_dv_utility_download import _FakeResponse, _bare_inst, _make_inst

ZIP_URL = 'https://example.invalid/version_ctrl/Tool_cpu_2.11.0.zip'
NAS_ZIP_URL = 'sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/x/Tool/Tool_cpu_2.11.0.zip'
LOG_PATH = str(dv_utility._get_log_dir() / 'dv_utility.log')


def _response(status):
    r = requests.Response()
    r.status_code = status
    r.url = ZIP_URL
    return r


class _Status(_FakeResponse):
    def __init__(self, status):
        super().__init__(b'')
        self._resp = _response(status)

    def raise_for_status(self):
        self._resp.raise_for_status()


def test_download_404_says_release_file_missing(monkeypatch, tmp_path):
    inst, statuses, versions = _make_inst(
        monkeypatch, {}, [(ZIP_URL, str(tmp_path / 'Tool'), True)], tmp_path / 'stage')
    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda url, stream=True, timeout=None: _Status(404))

    inst.download_file()

    assert versions == []
    last = statuses[-1]
    assert '發佈檔不存在' in last and '工具維護者' in last
    assert '網路' not in last
    assert LOG_PATH in last


def test_download_missing_nas_file_says_release_file_missing(monkeypatch, tmp_path):
    inst, statuses, versions = _make_inst(
        monkeypatch, {}, [(NAS_ZIP_URL, str(tmp_path / 'Tool'), True)], tmp_path / 'stage')

    def missing(url, zip_path, zip_file_name):
        raise FileNotFoundError(2, 'No such file')

    inst._fetch_to_file = missing

    inst.download_file()

    assert versions == []
    assert '發佈檔不存在' in statuses[-1]


def test_download_other_http_error_stays_network_and_shows_log(monkeypatch, tmp_path):
    inst, statuses, _ = _make_inst(
        monkeypatch, {}, [(ZIP_URL, str(tmp_path / 'Tool'), True)], tmp_path / 'stage')
    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda url, stream=True, timeout=None: _Status(503))

    inst.download_file()

    last = statuses[-1]
    assert '網路' in last and '發佈檔不存在' not in last
    assert LOG_PATH in last


def _check_inst(error):
    inst = _bare_inst()
    inst.signals = dv_utility.WorkerSignals()
    inst.target = 'https://example.invalid/version_ctrl/version_RTK.json'

    def boom():
        raise error

    inst.get_newest_version = boom
    statuses = []
    inst.signals.update_status.connect(statuses.append)
    return inst, statuses


def test_manifest_404_says_version_file_missing():
    inst, statuses = _check_inst(requests.HTTPError(response=_response(404)))

    inst.check_for_update()

    last = statuses[-1]
    assert '版本檔不存在' in last and '工具維護者' in last
    assert '網路' not in last
    assert LOG_PATH in last


def test_manifest_network_error_shows_log_path():
    inst, statuses = _check_inst(requests.ConnectionError('down'))

    inst.check_for_update()

    assert '網路' in statuses[-1] and LOG_PATH in statuses[-1]
