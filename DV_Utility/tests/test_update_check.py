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


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_check_latest_version_returns_info_when_newer(monkeypatch):
    payload = {'version': 'v20260717', 'zip_url': 'https://x/DV_Utility.zip',
               'sha256': 'abc', 'size': 1, 'release_note': '修了一些東西'}
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    monkeypatch.setattr(uc.requests, 'get', lambda *a, **k: _FakeResp(payload))
    info = uc.check_latest_version('v20260716')
    assert info == payload


def test_check_latest_version_none_when_not_newer(monkeypatch):
    payload = {'version': 'v20260716'}
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    monkeypatch.setattr(uc.requests, 'get', lambda *a, **k: _FakeResp(payload))
    assert uc.check_latest_version('v20260716') is None


def test_check_latest_version_none_when_not_packaged(monkeypatch):
    monkeypatch.setattr(uc, '_is_packaged', lambda: False)
    assert uc.check_latest_version('v20260716') is None


def test_check_latest_version_none_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('network down')
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    monkeypatch.setattr(uc.requests, 'get', boom)
    assert uc.check_latest_version('v20260716') is None
