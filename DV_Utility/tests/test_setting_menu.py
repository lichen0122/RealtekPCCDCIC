"""setting_menu.validate: the tool menu bundled into DV_Utility."""
import json

import pytest

import setting_menu


def _write(tmp_path, data):
    p = tmp_path / "setting.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_accepts_https_and_rt_nas_sftp(tmp_path):
    data = {"工具 A": "https://storage.googleapis.com/b/a_version.json",
            "工具 B": "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/B/b_version.json"}
    assert setting_menu.validate(_write(tmp_path, data)) == data


@pytest.mark.parametrize("url", [
    "http://x/v.json",
    "sftp://evil.example/Audio/v.json",       # only the pinned NAS host
    "sftp://rt-nas.realsil.com.cn/",          # no file path
    "",
])
def test_rejects_other_urls(tmp_path, url):
    with pytest.raises(ValueError):
        setting_menu.validate(_write(tmp_path, {"t": url}))


def test_rejects_empty_menu(tmp_path):
    with pytest.raises(ValueError):
        setting_menu.validate(_write(tmp_path, {}))
