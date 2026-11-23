"""A release reuses the last signed dv_updater.exe when nothing that goes into it changed.

The updater rarely changes, but every release rebuilt it and sent it to CTC signing (one more
FEDEX approval). Now the signed exe is kept with a stamp of its inputs; the next release copies it
when the inputs match and the exe is byte-identical to what was signed.
"""

import release_dv_utility as rel


def _inputs(tmp_path, script=b'print("updater")', png=b'PNG'):
    s = tmp_path / 'dv_updater.py'
    p = tmp_path / 'realtek.png'
    s.write_bytes(script)
    p.write_bytes(png)
    return str(s), str(p)


def _signed(tmp_path, fp, exe_bytes=b'MZ-signed'):
    exe = tmp_path / 'dv_updater.exe'
    stamp = tmp_path / 'dv_updater.stamp.json'
    exe.write_bytes(exe_bytes)
    rel.write_updater_stamp(fp, 'v20261123', exe=str(exe), stamp=str(stamp))
    return str(exe), str(stamp)


def test_fingerprint_changes_with_script_or_icon(tmp_path):
    s, p = _inputs(tmp_path)
    base = rel.updater_fingerprint(s, p)
    assert rel.updater_fingerprint(s, p) == base
    (tmp_path / 'dv_updater.py').write_bytes(b'print("changed")')
    assert rel.updater_fingerprint(s, p) != base
    (tmp_path / 'dv_updater.py').write_bytes(b'print("updater")')
    (tmp_path / 'realtek.png').write_bytes(b'PNG2')
    assert rel.updater_fingerprint(s, p) != base


def test_reuses_signed_updater_when_inputs_unchanged(tmp_path):
    fp = rel.updater_fingerprint(*_inputs(tmp_path))
    exe, stamp = _signed(tmp_path, fp)
    assert rel.reusable_updater(fp, exe=exe, stamp=stamp) == exe


def test_rebuilds_when_inputs_changed(tmp_path):
    fp = rel.updater_fingerprint(*_inputs(tmp_path))
    exe, stamp = _signed(tmp_path, fp)
    assert rel.reusable_updater('other-fingerprint', exe=exe, stamp=stamp) is None


def test_rebuilds_when_exe_differs_from_what_was_signed(tmp_path):
    fp = rel.updater_fingerprint(*_inputs(tmp_path))
    exe, stamp = _signed(tmp_path, fp)
    (tmp_path / 'dv_updater.exe').write_bytes(b'MZ-unsigned-rebuild')
    assert rel.reusable_updater(fp, exe=exe, stamp=stamp) is None


def test_rebuilds_without_stamp_or_exe(tmp_path):
    fp = rel.updater_fingerprint(*_inputs(tmp_path))
    assert rel.reusable_updater(fp, exe=str(tmp_path / 'dv_updater.exe'),
                                stamp=str(tmp_path / 'missing.json')) is None
    exe, stamp = _signed(tmp_path, fp)
    (tmp_path / 'dv_updater.exe').unlink()
    assert rel.reusable_updater(fp, exe=exe, stamp=stamp) is None


def test_corrupt_stamp_means_rebuild(tmp_path):
    fp = rel.updater_fingerprint(*_inputs(tmp_path))
    exe, stamp = _signed(tmp_path, fp)
    (tmp_path / 'dv_updater.stamp.json').write_text('{not json', encoding='utf-8')
    assert rel.reusable_updater(fp, exe=exe, stamp=stamp) is None


def test_sign_both_skips_reused_updater(monkeypatch):
    signed = []
    monkeypatch.setattr(rel, 'sign_exe', signed.append)
    rel._sign_both('dist/DV_Utility.exe', None, sign_ctc=False)
    assert signed == ['dist/DV_Utility.exe']
