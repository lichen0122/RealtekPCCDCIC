#!/usr/bin/env python3
"""file_sanitizer — a fast OPSWAT pre-check for Realtek CTCSOC's File-Sanitizer
service (the ``fsanitize`` CLI). Bundled inside the ``ctc_sign`` package and
driven by ``ctc_sign``'s pre-sign gate (:func:`ctc_sign.run_presign_check`) at
the start of every ``sign()``.

``ctc_sign`` (hsm-cli ``file-sign``) runs the same CTCSOC OPSWAT antivirus scan
internally but behind a slow FEDEX approval + HSM flow. File-Sanitizer exposes
that OPSWAT as a quick file-exchange service, so uploading a freshly built exe
here and reading the scan verdict tells us up front whether ``ctc_sign`` would be
rejected by OPSWAT — without paying the FEDEX/HSM cost.

Verdict-only: this module ``upload``s and polls ``get`` for the scan result. It
never ``download``s and never substitutes File-Sanitizer's sanitized output for
the built exe (that would corrupt the binary about to be signed).

Pure stdlib, no third-party dependency (like the rest of ``ctc_sign``). Can be
imported as a library or run as a CLI.

Verified ``fsanitize`` CLI shape:
    fsanitize -c <config> upload <FILE> --key <key>   -> JSON {"id": <int>, "stage": "NEW", ...}
    fsanitize -c <config> get --id <id>               -> JSON {..., "stage": "DONE", "opswatStatus": "OK"}
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time


class FsError(RuntimeError):
    """Any failure in the File-Sanitizer pre-check flow."""


# A file is downloadable (and thus "clean") only once stage == DONE and OPSWAT
# passed. Gate is fail-closed: a DONE file that is not clearly clean is a fail.
_DONE_STAGE = "DONE"
_CLEAN_WORDS = ("OK", "PASS")
_FAIL_WORDS = ("DENY", "ERROR", "FAIL", "REJECT", "BLOCK")


# --------------------------------------------------------------------------- #
#  Output parsing (fsanitize emits UTF-8 JSON; keep a regex fallback)
# --------------------------------------------------------------------------- #
def _try_json(s):
    """Parse fsanitize output into JSON (dict/list); if wrapped in non-JSON
    lines, grab the first {..}/[..] span. Returns None on failure."""
    s = (s or "").strip()
    try:
        return json.loads(s)
    except Exception:
        for open_c, close_c in (("[", "]"), ("{", "}")):
            i, j = s.find(open_c), s.rfind(close_c)
            if 0 <= i < j:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    pass
    return None


def _first_record(obj):
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]
    return None


def parse_upload_id(output):
    """Parse the file id from ``fsanitize upload`` output. Prefers the JSON
    ``"id"`` field (int or string), falls back to a regex. Raises ``FsError``
    if no id can be found."""
    rec = _first_record(_try_json(output))
    if rec and rec.get("id") is not None:
        return str(rec["id"])
    m = re.search(r'"id"\s*:\s*"?([^",}\s]+)"?', output or "")
    if m:
        return m.group(1)
    raise FsError("could not parse a file id from fsanitize upload output:\n" + (output or ""))


def classify_scan(output):
    """Classify ``fsanitize get`` output as ``'pending' | 'pass' | 'fail'``.

    Fail-closed:
      - ``pending`` — stage not yet DONE and no failure word (keep polling).
      - ``pass``    — stage == DONE and opswatStatus is clean (OK/PASS), no failure word.
      - ``fail``    — a failure word (DENY/ERROR/FAIL/REJECT/BLOCK) appears in
                      opswatStatus/opswatResponse, or stage == DONE without a clean verdict.

    With no parseable JSON, only a failure word can be concluded; otherwise pending.
    """
    rec = _first_record(_try_json(output))
    if rec is None:
        return "fail" if any(w in (output or "").upper() for w in _FAIL_WORDS) else "pending"
    stage = str(rec.get("stage", "")).strip().upper()
    status = str(rec.get("opswatStatus", "")).strip().upper()
    resp = str(rec.get("opswatResponse", "")).strip().upper()
    if any(w in status or w in resp for w in _FAIL_WORDS):
        return "fail"
    if stage == _DONE_STAGE:
        return "pass" if status in _CLEAN_WORDS else "fail"
    return "pending"


def build_cli_argv(cli, config, *args):
    """Assemble a full ``fsanitize`` argv: the binary, then the ``-c <config>``
    global flag (when a config is given), then the subcommand and its args."""
    argv = [cli]
    if config:
        argv += ["-c", config]
    argv += list(args)
    return argv


# --------------------------------------------------------------------------- #
#  Impure I/O layer — locate + drive the fsanitize CLI (subprocess + polling).
#  Not unit-tested (like ctc_sign's subprocess layer); verified in a real
#  release. This is the default ``scan_fn`` behind ctc_sign's pre-sign gate
#  (:func:`ctc_sign.run_presign_check`).
# --------------------------------------------------------------------------- #
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_NAME = "file-sanitizer.config.yaml"
# Preferred binary names, this build's vendored x64 first.
_CLI_NAMES = (
    "fsanitize-windows-x64.exe",
    "fsanitize-windows-x86.exe",
    "fsanitize.exe",
    "fsanitize",
)
# Default OPSWAT poll cadence — no FEDEX approval, so far shorter than ctc_sign.
_POLL_INTERVAL = 10
_TIMEOUT = 600
# Upload key labelling our pre-sign scans on the File-Sanitizer server. Kept
# project-neutral because this module is bundled inside the shared ctc_sign/.
_DEFAULT_KEY = "ctc-presign"


def _find_cli(explicit=None):
    """Locate the fsanitize binary: an explicit path or ``$FSANITIZE_CLI``
    first, then a vendored copy beside this module, then ``PATH``. Raises
    ``FsError`` if none is found."""
    candidate = explicit or os.environ.get("FSANITIZE_CLI")
    if candidate:
        if os.path.isfile(candidate):
            return candidate
        raise FsError(f"fsanitize CLI not found at: {candidate}")
    for name in _CLI_NAMES:
        local = os.path.join(_MODULE_DIR, name)
        if os.path.isfile(local):
            return local
    for name in _CLI_NAMES:
        on_path = shutil.which(name)
        if on_path:
            return on_path
    raise FsError(
        "fsanitize CLI not found; place fsanitize-windows-x64.exe beside "
        "file_sanitizer.py, put it on PATH, or set FSANITIZE_CLI.")


def _find_config(explicit=None):
    """Locate the File-Sanitizer config yaml: an explicit path or
    ``$FSANITIZE_CONFIG`` first, then a copy beside this module. Returns
    ``None`` if none is found (fsanitize may still read its own default)."""
    candidate = explicit or os.environ.get("FSANITIZE_CONFIG")
    if candidate:
        if os.path.isfile(candidate):
            return candidate
        raise FsError(f"File-Sanitizer config not found at: {candidate}")
    local = os.path.join(_MODULE_DIR, _CONFIG_NAME)
    return local if os.path.isfile(local) else None


class Sanitizer:
    """Drives the fsanitize CLI to obtain an OPSWAT verdict for a file.

    ``scan(path)`` uploads the file and polls ``get`` until the OPSWAT scan is
    conclusive, returning ``'pass'`` or ``'fail'``. Raises ``FsError`` on any
    infrastructure failure (missing binary/config, upload error, timeout) — the
    caller's gate treats every non-``'pass'`` outcome as "not passed".
    """

    def __init__(self, cli=None, config=None, key=_DEFAULT_KEY):
        self.cli = _find_cli(cli)
        self.config = _find_config(config)
        self.key = key

    def _run(self, *args):
        """Run one fsanitize subcommand; return ``(returncode, stdout+stderr)``."""
        cmd = build_cli_argv(self.cli, self.config, *args)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, encoding="utf-8", errors="replace")
        except OSError as e:
            raise FsError(f"failed to run fsanitize: {e}") from e
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def scan(self, file_path, key=None, poll_interval=_POLL_INTERVAL,
             timeout=_TIMEOUT, log=print):
        """Upload ``file_path`` and poll until the OPSWAT verdict is known.
        Returns ``'pass'`` / ``'fail'``; raises ``FsError`` on infra/timeout.
        Verdict-only: never downloads the sanitized output."""
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            raise FsError(f"file to scan does not exist: {file_path}")
        key = key or self.key

        upload_args = ["upload", file_path] + (["--key", key] if key else [])
        rc, out = self._run(*upload_args)
        log((out or "").strip())
        if rc != 0:
            raise FsError(f"fsanitize upload failed (rc={rc})")
        file_id = parse_upload_id(out)
        log(f"File-Sanitizer id={file_id}; polling OPSWAT scan …")

        waited = 0
        while True:
            rc, out = self._run("get", "--id", file_id)
            log(f"[{waited}s] {(out or '').strip()}")
            verdict = classify_scan(out)
            if verdict in ("pass", "fail"):
                return verdict
            if waited >= timeout:
                raise FsError(
                    f"File-Sanitizer OPSWAT scan timed out after {timeout}s "
                    f"(id={file_id})")
            time.sleep(poll_interval)
            waited += poll_interval


def scan_file(file_path, cli=None, config=None, key=None,
              poll_interval=_POLL_INTERVAL, timeout=_TIMEOUT, log=print):
    """Convenience: build a :class:`Sanitizer` and scan one file. Returns
    ``'pass'`` / ``'fail'``; raises ``FsError`` on infra/timeout. This is the
    scanner behind ``ctc_sign``'s pre-sign gate (:func:`ctc_sign.run_presign_check`)."""
    sanitizer = Sanitizer(cli=cli, config=config)
    return sanitizer.scan(file_path, key=key, poll_interval=poll_interval,
                          timeout=timeout, log=log)


def _cli(argv=None):
    """CLI: upload a file and report its OPSWAT verdict. Exit 0 on pass, 2 on
    fail, 1 on infrastructure error (mirrors ctc_sign's operator-facing CLI)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="file_sanitizer",
        description="File-Sanitizer OPSWAT pre-check: upload a file and report "
                    "the scan verdict. Verdict-only — never downloads the "
                    "cleaned file.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan_p = sub.add_parser(
        "scan", help="upload FILE and report the OPSWAT verdict (pass/fail)")
    scan_p.add_argument("--file", required=True, help="file to scan")
    scan_p.add_argument("--cli", help="path to the fsanitize binary")
    scan_p.add_argument("--config", help="path to file-sanitizer.config.yaml")
    scan_p.add_argument("--key", help="upload key label")
    scan_p.add_argument("--poll", type=int, default=_POLL_INTERVAL,
                        help="poll interval in seconds (default %(default)s)")
    scan_p.add_argument("--timeout", type=int, default=_TIMEOUT,
                        help="give-up timeout in seconds (default %(default)s)")
    ns = parser.parse_args(argv)

    try:
        verdict = scan_file(
            ns.file, cli=ns.cli, config=ns.config, key=ns.key,
            poll_interval=ns.poll, timeout=ns.timeout)
    except FsError as e:
        print(f"file_sanitizer error: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(verdict)
    raise SystemExit(0 if verdict == "pass" else 2)


if __name__ == "__main__":
    _cli()
