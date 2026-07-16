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
