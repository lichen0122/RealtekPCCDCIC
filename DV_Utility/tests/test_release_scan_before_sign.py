"""A release scans every exe (and a zip of them) with OPSWAT before CTC signing, fail-closed."""

import pytest

import file_sanitizer
import release_dv_utility as rel


def _exes(tmp_path):
    a = tmp_path / 'DV_Utility.exe'
    b = tmp_path / 'dv_updater.exe'
    a.write_bytes(b'MZ-main')
    b.write_bytes(b'MZ-updater')
    return [str(a), str(b)]


def test_all_pass_scans_exes_and_zip(tmp_path):
    seen = []
    rel.scan_before_sign(_exes(tmp_path), scan=lambda f: seen.append(f) or 'pass', log=lambda m: None)
    assert [s.rsplit('\\', 1)[-1].rsplit('/', 1)[-1] for s in seen] == [
        'DV_Utility.exe', 'dv_updater.exe', 'DV_Utility_prescan.zip']


def test_fail_stops_release(tmp_path):
    verdicts = iter(['pass', 'fail', 'pass'])
    with pytest.raises(SystemExit, match='dv_updater.exe: fail'):
        rel.scan_before_sign(_exes(tmp_path), scan=lambda f: next(verdicts), log=lambda m: None)


def test_scan_that_cannot_run_is_not_a_pass(tmp_path):
    def boom(f):
        raise file_sanitizer.FsError('fsanitize missing')
    with pytest.raises(SystemExit, match='not run: fsanitize missing'):
        rel.scan_before_sign(_exes(tmp_path), scan=boom, log=lambda m: None)
