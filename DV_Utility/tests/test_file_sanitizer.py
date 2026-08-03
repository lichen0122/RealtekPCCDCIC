"""Tests for file_sanitizer pure logic — upload-id parsing, scan classification,
and CLI argv shape. The live subprocess/OPSWAT path is verified manually in a
real release (like signing), not here."""
import pytest

import file_sanitizer as fs


# -- parse_upload_id --------------------------------------------------------
def test_parse_upload_id_from_json_int():
    # `fsanitize upload` returns an object whose "id" is an integer.
    out = '{"id":64,"key":"ctc-presign","fileName":"app.exe","stage":"NEW","opswatStatus":""}'
    assert fs.parse_upload_id(out) == "64"


def test_parse_upload_id_from_json_string():
    assert fs.parse_upload_id('{"id":"2026.08.03-abc","stage":"NEW"}') == "2026.08.03-abc"


def test_parse_upload_id_regex_fallback_without_braces():
    # Defensive: no JSON object, but an "id": N pair is present in the text.
    assert fs.parse_upload_id('uploaded ok, "id": 88, done') == "88"


def test_parse_upload_id_raises_when_absent():
    with pytest.raises(fs.FsError):
        fs.parse_upload_id("connection refused; no id anywhere")


# -- classify_scan ----------------------------------------------------------
# Doc facts: stage NEW/INIT -> DONE; scan result PASS/DENY/ERROR; a DONE file is
# downloadable only when OPSWAT is PASS. CLI `get` carries stage + opswatStatus
# (+ opswatResponse). Gate is fail-closed: only a clean DONE is a pass.
def test_classify_pending_right_after_upload():
    out = '{"id":64,"stage":"NEW","opswatStatus":"","opswatResponse":""}'
    assert fs.classify_scan(out) == "pending"


def test_classify_pending_while_scanning():
    assert fs.classify_scan('{"id":64,"stage":"INIT","opswatStatus":""}') == "pending"


def test_classify_pass_done_ok():
    out = '{"id":64,"stage":"DONE","opswatStatus":"OK","opswatResponse":""}'
    assert fs.classify_scan(out) == "pass"


def test_classify_pass_done_pass_word():
    assert fs.classify_scan('{"id":64,"stage":"DONE","opswatStatus":"PASS"}') == "pass"


def test_classify_pass_is_case_insensitive():
    assert fs.classify_scan('{"id":64,"stage":"done","opswatStatus":"ok"}') == "pass"


def test_classify_fail_deny():
    out = '{"id":64,"stage":"DONE","opswatStatus":"DENY","opswatResponse":"blocked"}'
    assert fs.classify_scan(out) == "fail"


def test_classify_fail_error():
    assert fs.classify_scan('{"id":64,"stage":"DONE","opswatStatus":"ERROR"}') == "fail"


def test_classify_fail_word_in_response():
    # Failure signalled in opswatResponse even if opswatStatus is not clean.
    out = '{"id":64,"stage":"DONE","opswatStatus":"","opswatResponse":"REJECT: infected"}'
    assert fs.classify_scan(out) == "fail"


def test_classify_fail_closed_done_but_not_clean():
    # DONE but no clean verdict -> not downloadable -> treat as fail, not pending.
    assert fs.classify_scan('{"id":64,"stage":"DONE","opswatStatus":""}') == "fail"


def test_classify_non_json_without_failword_is_pending():
    assert fs.classify_scan("still working, please wait") == "pending"


def test_classify_non_json_with_failword_is_fail():
    assert fs.classify_scan("fatal ERROR talking to opswat") == "fail"


# -- build_cli_argv ---------------------------------------------------------
# `-c/--config` is a global flag; put it before the subcommand (matches the doc's
# `fsanitize -c config.yaml upload ...`).
def test_build_cli_argv_config_flag_before_subcommand():
    assert fs.build_cli_argv("fsanitize.exe", "cfg.yaml", "get", "--id", "64") == \
        ["fsanitize.exe", "-c", "cfg.yaml", "get", "--id", "64"]


def test_build_cli_argv_upload_shape():
    assert fs.build_cli_argv("x", "c.yaml", "upload", "/p/app.exe", "--key", "K") == \
        ["x", "-c", "c.yaml", "upload", "/p/app.exe", "--key", "K"]


def test_build_cli_argv_omits_config_when_absent():
    assert fs.build_cli_argv("x", None, "version") == ["x", "version"]
