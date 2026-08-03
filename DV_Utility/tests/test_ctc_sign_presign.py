"""Tests for ctc_sign's bundled pre-sign OPSWAT gate — the pure decision helpers
(``classify_presign``, ``_skip_presign``) and the fail-open/fail-closed policy of
``run_presign_check``. The live fsanitize upload is verified manually in a real
release, like the signing path itself."""
import pytest

import ctc_sign as cs
from file_sanitizer import FsError


# -- classify_presign -------------------------------------------------------
# Pure: only a 'pass' verdict clears the gate; every other verdict (including a
# stray/unexpected one leaking out of the scanner) is fail-closed to a reason.
def test_classify_presign_pass_returns_none():
    assert cs.classify_presign("pass") is None


def test_classify_presign_fail_returns_reason():
    reason = cs.classify_presign("fail")
    assert reason is not None
    assert "fail" in reason


def test_classify_presign_unexpected_verdict_is_failclosed():
    # A stray verdict (e.g. 'pending' leaking out of the scanner) is never a
    # pass — guards the strict == "pass" check.
    reason = cs.classify_presign("pending")
    assert reason is not None
    assert "pending" in reason


# -- _skip_presign ----------------------------------------------------------
# The check runs by default; either presign=False or a truthy CTC_SKIP_PRESIGN
# opts out. An unset or "0" env var means run (never skip).
def test_skip_presign_default_runs(monkeypatch):
    monkeypatch.delenv("CTC_SKIP_PRESIGN", raising=False)
    assert cs._skip_presign(True) is False


def test_skip_presign_param_false_skips(monkeypatch):
    monkeypatch.delenv("CTC_SKIP_PRESIGN", raising=False)
    assert cs._skip_presign(False) is True


def test_skip_presign_env_1_skips(monkeypatch):
    monkeypatch.setenv("CTC_SKIP_PRESIGN", "1")
    assert cs._skip_presign(True) is True


def test_skip_presign_env_0_runs(monkeypatch):
    # Explicit "0" is the documented "run" value — same as unset.
    monkeypatch.setenv("CTC_SKIP_PRESIGN", "0")
    assert cs._skip_presign(True) is False


def test_skip_presign_env_truthy_word_skips(monkeypatch):
    monkeypatch.setenv("CTC_SKIP_PRESIGN", "true")
    assert cs._skip_presign(True) is True


# -- run_presign_check ------------------------------------------------------
# Injected scan_fn(file_path) -> 'pass'/'fail'; may raise FsError. A definite
# reject fail-CLOSES (PresignError); an inability to scan fails OPEN (no raise).
def test_run_presign_check_pass_does_not_raise():
    # No exception == gate cleared, signing proceeds.
    cs.run_presign_check("app.exe", scan_fn=lambda p: "pass", log=lambda *a: None)


def test_run_presign_check_fail_raises_presign_error():
    with pytest.raises(cs.PresignError):
        cs.run_presign_check("app.exe", scan_fn=lambda p: "fail", log=lambda *a: None)


def test_run_presign_check_unexpected_verdict_raises():
    with pytest.raises(cs.PresignError):
        cs.run_presign_check("app.exe", scan_fn=lambda p: "weird", log=lambda *a: None)


def test_run_presign_check_failopen_when_scan_cannot_run():
    # FsError = can't get a verdict (missing binary/config, upload error,
    # timeout, unreachable). Fail OPEN: no raise, log a notice, sign proceeds.
    logged = []

    def boom(p):
        raise FsError("fsanitize CLI not found")

    cs.run_presign_check("app.exe", scan_fn=boom, log=logged.append)
    assert any("OPSWAT" in line for line in logged)
    assert any("fsanitize CLI not found" in line for line in logged)
