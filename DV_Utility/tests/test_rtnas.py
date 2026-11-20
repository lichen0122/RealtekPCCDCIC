"""Tests for rtnas — the shared RT NAS (SFTP) client (spec §27).

The pure helpers are tested directly; the I/O helpers run against a fake SFTP
client, so no network and no paramiko are needed."""
import base64
import hashlib
import io
import json
import stat as stat_mod
import sys
from pathlib import Path

import pytest

import rtnas


# ── URL parsing ──────────────────────────────────────────────────────────────

def test_is_sftp_url():
    assert rtnas.is_sftp_url("sftp://rt-nas.realsil.com.cn/Audio/x.zip")
    assert rtnas.is_sftp_url("SFTP://host/x")
    assert not rtnas.is_sftp_url("https://storage.googleapis.com/b/x.zip")
    assert not rtnas.is_sftp_url("")
    assert not rtnas.is_sftp_url(None)


def test_parse_sftp_url_default_port():
    assert rtnas.parse_sftp_url(
        "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/PCDICskills/app.zip"
    ) == ("rt-nas.realsil.com.cn", 22,
          "/Audio/Temp_RT/lichen.liu/PCDICskills/app.zip")


def test_parse_sftp_url_explicit_port_and_quoted_path():
    assert rtnas.parse_sftp_url("sftp://h:2222/a%20b/c.json") == ("h", 2222, "/a b/c.json")


@pytest.mark.parametrize("bad", [
    "https://h/x", "sftp://", "sftp://h", "sftp://h/", "sftp:///x"])
def test_parse_sftp_url_rejects(bad):
    with pytest.raises(ValueError):
        rtnas.parse_sftp_url(bad)


def test_build_sftp_url_round_trips():
    url = rtnas.build_sftp_url("rt-nas.realsil.com.cn", "/Audio/a b/x.zip")
    assert url == "sftp://rt-nas.realsil.com.cn/Audio/a%20b/x.zip"
    assert rtnas.parse_sftp_url(url) == ("rt-nas.realsil.com.cn", 22, "/Audio/a b/x.zip")


# ── hashing / identity ───────────────────────────────────────────────────────

def test_sha256_helpers(tmp_path):
    data = b"hello nas"
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    want = hashlib.sha256(data).hexdigest()
    assert rtnas.sha256_bytes(data) == want
    assert rtnas.sha256_file(p) == want


def test_default_username_prefixes_domain():
    assert rtnas.default_username({"USERNAME": "lichen.liu"}) == "rtdomain\\lichen.liu"


def test_default_username_keeps_existing_domain():
    assert rtnas.default_username({"USERNAME": "rtdomain\\x"}) == "rtdomain\\x"


def test_default_username_empty_when_unknown():
    assert rtnas.default_username({}) == ""


# ── host key pinning ─────────────────────────────────────────────────────────

def _fp(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def test_fingerprint_matches_openssh_format():
    blob = b"\x00\x00\x00\x0bssh-ed25519fake"
    assert rtnas.fingerprint(blob) == _fp(blob)


def test_host_key_ok_accepts_only_pinned(monkeypatch):
    blob = b"pinned-key"
    monkeypatch.setitem(rtnas.PINNED_HOST_KEYS, "nas.example", {_fp(blob)})
    assert rtnas.host_key_ok("nas.example", blob)
    assert not rtnas.host_key_ok("nas.example", b"other-key")
    assert not rtnas.host_key_ok("unknown.example", blob)


def test_rt_nas_fingerprint_is_pinned():
    assert ("SHA256:Z/IwWr8lc+eo5FRuygy6DVTJyNkAuwF/ckQqGOnPkBc"
            in rtnas.PINNED_HOST_KEYS["rt-nas.realsil.com.cn"])


# ── credential store ─────────────────────────────────────────────────────────

def _fake_protect(b):
    return b[::-1] + b"!"


def _fake_unprotect(b):
    assert b.endswith(b"!")
    return b[:-1][::-1]


def test_credential_store_round_trip(tmp_path):
    store = rtnas.CredentialStore(tmp_path / "c" / "credential.json",
                                  protect=_fake_protect, unprotect=_fake_unprotect)
    assert store.load() is None
    store.save("rtdomain\\u", "s3cret-pw")
    assert store.load() == ("rtdomain\\u", "s3cret-pw")


def test_credential_store_file_has_no_plaintext(tmp_path):
    path = tmp_path / "credential.json"
    store = rtnas.CredentialStore(path, protect=_fake_protect, unprotect=_fake_unprotect)
    store.save("rtdomain\\u", "s3cret-pw")
    assert b"s3cret-pw" not in path.read_bytes()


def test_credential_store_corrupt_file_is_none(tmp_path):
    path = tmp_path / "credential.json"
    path.write_text("{not json", encoding="utf-8")
    store = rtnas.CredentialStore(path, protect=_fake_protect, unprotect=_fake_unprotect)
    assert store.load() is None


def test_credential_store_clear(tmp_path):
    path = tmp_path / "credential.json"
    store = rtnas.CredentialStore(path, protect=_fake_protect, unprotect=_fake_unprotect)
    store.save("u", "p")
    store.clear()
    assert not path.exists()
    store.clear()  # idempotent


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_round_trip():
    secret = "pässwörd-測試".encode("utf-8")
    blob = rtnas.dpapi_protect(secret)
    assert blob != secret
    assert rtnas.dpapi_unprotect(blob) == secret


# ── fake SFTP client ─────────────────────────────────────────────────────────

class _Attr:
    def __init__(self, size, is_dir):
        self.st_size = size
        self.st_mode = (stat_mod.S_IFDIR if is_dir else stat_mod.S_IFREG) | 0o777


class FakeSftp:
    """Minimal in-memory stand-in for paramiko.SFTPClient."""

    def __init__(self, dirs=("/",), files=None, short_write=False):
        self.dirs = set(dirs)
        self.files = dict(files or {})
        self.short_write = short_write
        self.calls = []

    def stat(self, path):
        if path in self.dirs:
            return _Attr(0, True)
        if path in self.files:
            return _Attr(len(self.files[path]), False)
        raise FileNotFoundError(path)

    def mkdir(self, path):
        self.calls.append(("mkdir", path))
        if path in self.dirs or path in self.files:
            raise OSError("Failure")
        self.dirs.add(path)

    def putfo(self, fl, remotepath, file_size=0, callback=None, confirm=True):
        data = fl.read()
        if self.short_write:
            data = data[:-1]
        self.files[remotepath] = data
        self.calls.append(("putfo", remotepath))

    def getfo(self, remotepath, fl, callback=None):
        data = self.files[remotepath]
        fl.write(data)
        if callback:
            callback(len(data), len(data))
        return len(data)

    def posix_rename(self, old, new):
        self.calls.append(("posix_rename", old, new))
        self.files[new] = self.files.pop(old)

    def remove(self, path):
        self.calls.append(("remove", path))
        del self.files[path]


# ── makedirs ─────────────────────────────────────────────────────────────────

def test_makedirs_creates_missing_levels_only():
    sftp = FakeSftp(dirs={"/", "/Audio", "/Audio/Temp_RT"})
    rtnas.makedirs(sftp, "/Audio/Temp_RT/lichen.liu/PCDICskills")
    assert ("mkdir", "/Audio/Temp_RT") not in sftp.calls
    assert sftp.calls == [("mkdir", "/Audio/Temp_RT/lichen.liu"),
                          ("mkdir", "/Audio/Temp_RT/lichen.liu/PCDICskills")]


def test_makedirs_noop_when_present():
    sftp = FakeSftp(dirs={"/", "/a", "/a/b"})
    rtnas.makedirs(sftp, "/a/b")
    assert sftp.calls == []


# ── put_atomic ───────────────────────────────────────────────────────────────

def test_put_atomic_uploads_tmp_then_renames(tmp_path):
    src = tmp_path / "app.zip"
    src.write_bytes(b"zipdata")
    sftp = FakeSftp(dirs={"/", "/d"}, files={"/d/app.zip": b"old"})
    rtnas.put_atomic(sftp, src, "/d/app.zip")
    assert sftp.files == {"/d/app.zip": b"zipdata"}
    assert ("putfo", "/d/app.zip.tmp") in sftp.calls
    assert ("posix_rename", "/d/app.zip.tmp", "/d/app.zip") in sftp.calls


def test_put_atomic_creates_parent_dirs(tmp_path):
    src = tmp_path / "v.json"
    src.write_bytes(b"{}")
    sftp = FakeSftp(dirs={"/", "/d"})
    rtnas.put_atomic(sftp, src, "/d/new/v.json")
    assert sftp.files["/d/new/v.json"] == b"{}"


def test_put_atomic_size_mismatch_aborts_and_cleans_tmp(tmp_path):
    src = tmp_path / "app.zip"
    src.write_bytes(b"zipdata")
    sftp = FakeSftp(dirs={"/", "/d"}, files={"/d/app.zip": b"old"}, short_write=True)
    with pytest.raises(rtnas.IntegrityError):
        rtnas.put_atomic(sftp, src, "/d/app.zip")
    assert sftp.files == {"/d/app.zip": b"old"}  # old release untouched, tmp removed


# ── fetch ────────────────────────────────────────────────────────────────────

def test_fetch_bytes_returns_content():
    sftp = FakeSftp(files={"/d/v.json": b'{"v":1}'})
    assert rtnas.fetch_bytes(sftp, "/d/v.json") == b'{"v":1}'


def test_fetch_bytes_verifies_sha256():
    data = b"archive"
    sftp = FakeSftp(files={"/d/s.zip": data})
    assert rtnas.fetch_bytes(sftp, "/d/s.zip",
                             expected_sha256=hashlib.sha256(data).hexdigest()) == data
    with pytest.raises(rtnas.IntegrityError):
        rtnas.fetch_bytes(sftp, "/d/s.zip", expected_sha256="0" * 64)


def test_download_to_file_writes_and_reports_progress(tmp_path):
    data = b"x" * 100
    sftp = FakeSftp(files={"/d/app.zip": data})
    seen = []
    dest = tmp_path / "app.zip.part"
    rtnas.download_to_file(sftp, "/d/app.zip", dest,
                           progress_cb=lambda done, total: seen.append((done, total)))
    assert dest.read_bytes() == data
    assert seen[-1] == (100, 100)


def test_download_to_file_sha_mismatch_removes_partial(tmp_path):
    sftp = FakeSftp(files={"/d/app.zip": b"data"})
    dest = tmp_path / "app.zip.part"
    with pytest.raises(rtnas.IntegrityError):
        rtnas.download_to_file(sftp, "/d/app.zip", dest, expected_sha256="0" * 64)
    assert not dest.exists()


# ── fetch_url (credentials + connect wiring) ─────────────────────────────────

class _Session:
    def __init__(self, sftp):
        self.sftp = sftp
        self.closed = False

    def __enter__(self):
        return self.sftp

    def __exit__(self, *exc):
        self.closed = True


def test_fetch_url_uses_stored_credentials(tmp_path):
    store = rtnas.CredentialStore(tmp_path / "c.json",
                                  protect=_fake_protect, unprotect=_fake_unprotect)
    store.save("rtdomain\\u", "pw")
    seen = {}

    def fake_connect(host, port, username, password, timeout):
        seen.update(host=host, port=port, username=username, password=password)
        return _Session(FakeSftp(files={"/d/v.json": b"hi"}))

    out = rtnas.fetch_url("sftp://nas.example/d/v.json", store=store, connect=fake_connect)
    assert out == b"hi"
    assert seen == {"host": "nas.example", "port": 22,
                    "username": "rtdomain\\u", "password": "pw"}


def test_fetch_url_without_credentials_raises_auth_error(tmp_path):
    store = rtnas.CredentialStore(tmp_path / "c.json",
                                  protect=_fake_protect, unprotect=_fake_unprotect)
    with pytest.raises(rtnas.AuthError):
        rtnas.fetch_url("sftp://nas.example/d/v.json", store=store,
                        connect=lambda *a, **k: pytest.fail("must not connect"))
