import zipfile

import pytest

import ctc_sign


# --- parse_create_id ---------------------------------------------------------
def test_parse_create_id_json_dict():
    out = '{"id":"2026.07.16-0103d","status":"INIT","algoName":"EVSign SHA256 演算法"}'
    assert ctc_sign.parse_create_id(out) == '2026.07.16-0103d'


def test_parse_create_id_json_list():
    out = '[{"id":"2026.07.16-abc12","status":"INIT"}]'
    assert ctc_sign.parse_create_id(out) == '2026.07.16-abc12'


def test_parse_create_id_regex_fallback():
    out = 'noise line\nid: 2026.07.16-deadb\ntrailing'
    assert ctc_sign.parse_create_id(out) == '2026.07.16-deadb'


def test_parse_create_id_raises_when_absent():
    with pytest.raises(ctc_sign.CtcError):
        ctc_sign.parse_create_id('no identifier here at all')


# --- classify_status ---------------------------------------------------------
def test_classify_status_terminal_and_progress():
    assert ctc_sign.classify_status('{"status":"SUCCESS"}') == 'done'
    assert ctc_sign.classify_status('{"status":"FAIL"}') == 'failed'
    for st in ('INIT', 'OPSWAT', 'FEDEX', 'HSM'):
        assert ctc_sign.classify_status('{"status":"%s"}' % st) == 'pending'
    # 純文字後備 (非 JSON)
    assert ctc_sign.classify_status('current: SUCCESS') == 'done'
    assert ctc_sign.classify_status('got ERROR from server') == 'failed'


# --- sign_file (mock hsm-cli via CtcSigner._run) ----------------------------
# The new ctc_sign drives hsm-cli through the CtcSigner._run *method*
# (signature (self, args)), so the fake is patched onto the class. Each test
# passes an existing hsm_cli (so _find_cli succeeds in __init__) and
# presign=False (isolate the signing flow from the OPSWAT pre-check).
def _install_fake_run(monkeypatch, statuses, signed_zip_src):
    calls = []

    def fake_run(self, args):
        calls.append(list(args))
        sub = args[1] if len(args) > 1 else ''
        if sub == 'create':
            return 0, '{"id":"2026.07.16-test1","status":"INIT"}'
        if sub in ('upload', 'start'):
            return 0, '{"ok":true}'
        if sub == 'status':
            return 0, next(statuses)
        if sub == 'download':
            import shutil
            out = args[args.index('--output') + 1]
            shutil.copyfile(signed_zip_src, out)
            return 0, '{"ok":true}'
        return 1, 'unexpected'

    monkeypatch.setattr(ctc_sign.CtcSigner, '_run', fake_run)
    monkeypatch.setattr(ctc_sign.time, 'sleep', lambda s: None)
    return calls


def test_sign_file_happy(tmp_path, monkeypatch):
    exe = tmp_path / 'DV_Utility.exe'
    exe.write_bytes(b'UNSIGNED')
    signed_src = tmp_path / 'src.zip'
    with zipfile.ZipFile(signed_src, 'w') as z:
        z.writestr('DV_Utility.exe', b'SIGNED-BYTES')
    fake_cli = tmp_path / 'hsm-cli.exe'
    fake_cli.write_bytes(b'x')

    statuses = iter(['{"status":"INIT"}', '{"status":"OPSWAT"}',
                     '{"status":"FEDEX"}', '{"status":"SUCCESS"}'])
    calls = _install_fake_run(monkeypatch, statuses, signed_src)

    out_path = str(tmp_path / 'signed.exe')
    result = ctc_sign.sign_file(str(exe), uuid='u', out_path=out_path,
                                hsm_cli=str(fake_cli), presign=False,
                                poll_interval=0, log=lambda *a: None)
    assert result == out_path
    assert open(out_path, 'rb').read() == b'SIGNED-BYTES'
    subs = [c[1] for c in calls if len(c) > 1]
    assert subs[:3] == ['create', 'upload', 'start']
    assert subs.count('status') == 4
    assert subs[-1] == 'download'


def test_sign_file_failed_status(tmp_path, monkeypatch):
    exe = tmp_path / 'DV_Utility.exe'
    exe.write_bytes(b'x')
    fake_cli = tmp_path / 'hsm-cli.exe'
    fake_cli.write_bytes(b'x')
    statuses = iter(['{"status":"OPSWAT"}', '{"status":"FAIL"}'])
    _install_fake_run(monkeypatch, statuses, tmp_path / 'unused.zip')
    with pytest.raises(ctc_sign.CtcError):
        ctc_sign.sign_file(str(exe), uuid='u', hsm_cli=str(fake_cli),
                           presign=False, poll_interval=0, log=lambda *a: None)


def test_sign_file_timeout(tmp_path, monkeypatch):
    exe = tmp_path / 'DV_Utility.exe'
    exe.write_bytes(b'x')
    fake_cli = tmp_path / 'hsm-cli.exe'
    fake_cli.write_bytes(b'x')

    def fake_run(self, args):
        sub = args[1]
        if sub == 'create':
            return 0, '{"id":"i","status":"INIT"}'
        if sub in ('upload', 'start'):
            return 0, 'ok'
        if sub == 'status':
            return 0, '{"status":"FEDEX"}'   # 永遠 pending -> 應逾時
        return 1, ''

    monkeypatch.setattr(ctc_sign.CtcSigner, '_run', fake_run)
    monkeypatch.setattr(ctc_sign.time, 'sleep', lambda s: None)
    with pytest.raises(ctc_sign.CtcError):
        ctc_sign.sign_file(str(exe), uuid='u', hsm_cli=str(fake_cli),
                           presign=False, poll_interval=1, timeout=2, log=lambda *a: None)


# --- _find_cli (hsm-cli discovery) ------------------------------------------
# Replaces the old test_hsm_cli_path_env: the new module locates hsm-cli via
# _find_cli() reading the HSM_CLI env (was DVUTIL_HSM_CLI). Same shape — an
# existing path is returned, a missing one raises CtcError.
def test_find_cli_env(tmp_path, monkeypatch):
    fake = tmp_path / 'hsm-cli.exe'
    fake.write_bytes(b'x')
    monkeypatch.setenv('HSM_CLI', str(fake))
    assert ctc_sign._find_cli() == str(fake)
    monkeypatch.setenv('HSM_CLI', str(tmp_path / 'nope.exe'))
    with pytest.raises(ctc_sign.CtcError):
        ctc_sign._find_cli()
