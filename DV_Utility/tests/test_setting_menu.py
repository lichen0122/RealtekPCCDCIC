"""setting_menu: validating, reading (GCS -> NAS -> cache) and publishing the tool menu."""
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


# ---------------------------------------------------------------- reading at start
class _Resp:
    def __init__(self, data, status=200):
        self.content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


MENU = {"Cue": "https://storage.googleapis.com/realtek-pccdcic-dv/Cue/cue_version.json"}


def _fail(*a, **k):
    raise OSError("unreachable")


def test_gcs_first_and_the_cache_is_saved(tmp_path):
    cache = tmp_path / "setting.json"
    menu, src = setting_menu.load_menu(str(cache), get=lambda url, timeout: _Resp({"menu": MENU}), fetch=_fail)
    assert (menu, src) == (MENU, "GCS")
    assert json.loads(cache.read_text(encoding="utf-8")) == MENU


def test_nas_when_gcs_fails_and_its_signature_is_checked(tmp_path, monkeypatch):
    seen = {}

    def verify(raw, expected_path=None):
        seen["path"] = expected_path
        return json.loads(raw)
    monkeypatch.setattr(setting_menu.rtnas, "verify_signed_json", verify)
    menu, src = setting_menu.load_menu(str(tmp_path / "c.json"), get=_fail,
                                       fetch=lambda url, store=None: json.dumps({"menu": MENU}).encode())
    assert (menu, src) == (MENU, "NAS") and seen["path"] == setting_menu.NAS_MENU_PATH


def test_unsigned_nas_menu_is_refused(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"Old": "https://x/v.json"}), encoding="utf-8")
    menu, src = setting_menu.load_menu(str(cache), get=_fail,
                                       fetch=lambda url, store=None: json.dumps({"menu": MENU}).encode())
    assert src == "cache" and menu == {"Old": "https://x/v.json"}  # NAS copy without signature: not used


def test_offline_with_nothing_cached_uses_the_repo_file_or_fails(tmp_path):
    repo = _write(tmp_path, MENU)
    assert setting_menu.load_menu(str(tmp_path / "none.json"), get=_fail, fetch=_fail,
                                  fallback_path=str(repo)) == (MENU, "repo")
    with pytest.raises(RuntimeError):
        setting_menu.load_menu(str(tmp_path / "none.json"), get=_fail, fetch=_fail)


def test_a_bad_gcs_menu_falls_through(tmp_path):
    menu, src = setting_menu.load_menu(str(tmp_path / "c.json"),
                                       get=lambda url, timeout: _Resp({"menu": {"X": "ftp://bad"}}),
                                       fetch=lambda url, store=None: (_ for _ in ()).throw(OSError("no")),
                                       fallback_path=str(_write(tmp_path, MENU)))
    assert src == "repo"


# ---------------------------------------------------------------- publishing
def test_publish_writes_menu_legacy_and_signed_nas(tmp_path):
    src = _write(tmp_path, {**MENU, "Both": {"gcs": "https://g/b.json",
                                             "nas": "sftp://rt-nas.realsil.com.cn/Audio/b.json"},
                            "NasOnly": "sftp://rt-nas.realsil.com.cn/Audio/n.json"})
    up, nas = {}, {}
    setting_menu.publish(str(src), upload_gcs=lambda obj, data: up.__setitem__(obj, json.loads(data)),
                         publish_nas=lambda plan: nas.update(plan=plan, body=json.loads(open(plan[0][0], encoding="utf-8").read())),
                         log=lambda *a: None)
    assert set(up["DVUtility/menu.json"]["menu"]) == {"Cue", "Both", "NasOnly"}
    assert up["DVUtility/setting.json"] == {"Cue": MENU["Cue"], "Both": "https://g/b.json"}  # strings only
    assert nas["plan"][0][1] == setting_menu.NAS_MENU_PATH and "Both" in nas["body"]["menu"]
