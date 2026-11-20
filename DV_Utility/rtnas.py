"""Shared client for the company RT NAS (SFTP) — release hosting (spec §27).

The RT NAS (``rt-nas.realsil.com.cn``) speaks SFTP only, and every user logs in
with their own domain account (``rtdomain\\<user>``). This module is copied
verbatim into each repo that publishes to or downloads from the NAS; keep the
copies identical.

* Pure helpers (URL parsing, hashing, host-key pinning, the DPAPI credential
  store) need only the standard library.
* ``paramiko`` is imported lazily inside :func:`connect`, so the pure parts and
  their tests load without it.

Command line (run in a real terminal — it prompts for the password)::

    python rtnas.py login      # save rtdomain\\<user> + password (DPAPI)
    python rtnas.py check      # connect with the saved credentials
    python rtnas.py logout     # forget the saved credentials
    python rtnas.py keygen     # publisher: create the release signing key (once)
    python rtnas.py pubkey     # publisher: print the public key to pin
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import posixpath
import stat as _stat
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

NAS_HOST = "rt-nas.realsil.com.cn"
NAS_PORT = 22
DOMAIN_PREFIX = "rtdomain\\"
CONNECT_TIMEOUT = 15  # seconds

# OpenSSH-style SHA256 fingerprints of the accepted host keys, per host. A host
# not listed here, or a key that does not match, is refused (HostKeyError).
PINNED_HOST_KEYS = {
    "rt-nas.realsil.com.cn": {
        "SHA256:Z/IwWr8lc+eo5FRuygy6DVTJyNkAuwF/ckQqGOnPkBc",   # ED25519
        "SHA256:50ucx3zBOvjWqnxXccqfmry8fCRWm8y3Ro3KSSee1/E",   # ECDSA
        "SHA256:7EMUWuDuEoy9h2lt/EJonlfeGKVDqifNm4vN41BwFRs",   # RSA
    },
}

DEFAULT_CREDENTIAL_PATH = Path.home() / ".PCDV" / "rtnas" / "credential.json"

# Ed25519 public keys (base64 of the 32 raw bytes) whose signatures make a JSON
# fetched from the NAS trustworthy (spec §27.5). The private half lives only on
# the publisher's machine (DEFAULT_SIGNING_KEY_PATH, DPAPI-encrypted).
TRUSTED_RELEASE_KEYS = (
    "MdUcG0n/9RNBWZXxj4hIisfTwQmZFJFONW8XeUM85FM=",   # lichen.liu, created 2026-11-20
)
SIGNATURE_FIELD = "signature"
PATH_FIELD = "nas_path"   # signed: where the publisher put this file (anti-swap)
DEFAULT_SIGNING_KEY_PATH = Path.home() / ".PCDV" / "rtnas" / "release_ed25519.key"
_CHUNK = 1024 * 1024


class RtnasError(Exception):
    """Base class for every RT NAS failure."""


class AuthError(RtnasError):
    """No saved credentials, or the NAS rejected them — ask the user again."""


class HostKeyError(RtnasError):
    """The server's host key is not the pinned one."""


class IntegrityError(RtnasError):
    """Size or SHA-256 of a transferred file does not match."""


# ── URLs ─────────────────────────────────────────────────────────────────────

def is_sftp_url(url) -> bool:
    return isinstance(url, str) and url.lower().startswith("sftp://")


def parse_sftp_url(url):
    """``sftp://host[:port]/path`` -> ``(host, port, path)``; ``ValueError`` if
    the URL is not sftp, has no host, or names no file path."""
    if not is_sftp_url(url):
        raise ValueError(f"not an sftp:// URL: {url!r}")
    parts = urlsplit(url)
    host = parts.hostname
    path = unquote(parts.path)
    if not host or not path or path == "/":
        raise ValueError(f"sftp URL needs a host and a path: {url!r}")
    return host, parts.port or NAS_PORT, path


def build_sftp_url(host, path, port=NAS_PORT) -> str:
    netloc = host if port == NAS_PORT else f"{host}:{port}"
    return f"sftp://{netloc}{quote(path)}"


# ── hashing / identity ───────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def default_username(env=None) -> str:
    """``rtdomain\\<Windows user>``, or ``""`` when the user is unknown."""
    env = os.environ if env is None else env
    user = env.get("USERNAME", "")
    if not user:
        return ""
    return user if "\\" in user else DOMAIN_PREFIX + user


def fingerprint(key_blob: bytes) -> str:
    """OpenSSH ``SHA256:<base64, no padding>`` fingerprint of a public key blob."""
    digest = hashlib.sha256(key_blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def host_key_ok(host: str, key_blob: bytes) -> bool:
    return fingerprint(key_blob) in PINNED_HOST_KEYS.get(host, set())


# ── DPAPI (Windows, CurrentUser scope) ───────────────────────────────────────

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1,  # UI_FORBIDDEN
            ctypes.byref(blob_out))
    if not ok:
        raise RtnasError(f"DPAPI {'protect' if protect else 'unprotect'} failed "
                         f"(error {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi(data, True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi(data, False)


class CredentialStore:
    """The user's NAS login, password DPAPI-encrypted, in one JSON file shared by
    every tool on the machine. ``load()`` never raises: a missing or unreadable
    file means "no credentials" (the caller then asks the user)."""

    def __init__(self, path=None, *, protect=dpapi_protect, unprotect=dpapi_unprotect):
        self.path = Path(path) if path else DEFAULT_CREDENTIAL_PATH
        self._protect = protect
        self._unprotect = unprotect

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            secret = self._unprotect(base64.b64decode(data["password"]))
            return data["username"], secret.decode("utf-8")
        except Exception:
            return None

    def save(self, username: str, password: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._protect(password.encode("utf-8"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "username": username,
            "password": base64.b64encode(blob).decode("ascii"),
        }), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ── signed manifests (Ed25519, spec §27.5) ──────────────────────────────────

def canonical_manifest_bytes(data: dict) -> bytes:
    """The bytes a signature covers: *data* without its signature field, keys
    sorted, no whitespace, UTF-8."""
    body = {k: v for k, v in data.items() if k != SIGNATURE_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def generate_signing_key():
    """New key pair -> ``(private_raw_32_bytes, public_base64)``."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat)

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return private, public_key_of(private)


def public_key_of(private_raw: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    pub = Ed25519PrivateKey.from_private_bytes(private_raw).public_key()
    return base64.b64encode(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")


def sign_manifest(data: dict, private_raw: bytes) -> dict:
    """Copy of *data* carrying a fresh ``signature`` field."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sig = Ed25519PrivateKey.from_private_bytes(private_raw).sign(canonical_manifest_bytes(data))
    out = {k: v for k, v in data.items() if k != SIGNATURE_FIELD}
    out[SIGNATURE_FIELD] = base64.b64encode(sig).decode("ascii")
    return out


def verify_manifest(data, trusted=None, expected_path=None) -> dict:
    """Return *data* if its signature verifies against a trusted key; else raise
    :class:`IntegrityError`. Never accepts an unsigned object. With
    *expected_path*, the signed ``nas_path`` must equal it, so a validly signed
    file copied to another product's location is refused."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(data, dict) or not isinstance(data.get(SIGNATURE_FIELD), str):
        raise IntegrityError("manifest is not signed")
    try:
        sig = base64.b64decode(data[SIGNATURE_FIELD], validate=True)
    except ValueError as e:
        raise IntegrityError("manifest signature is not valid base64") from e
    try:
        message = canonical_manifest_bytes(data)
    except (TypeError, ValueError) as e:  # incl. UnicodeEncodeError (lone surrogate)
        raise IntegrityError(f"manifest cannot be canonicalised: {e}") from e
    for key in (TRUSTED_RELEASE_KEYS if trusted is None else trusted):
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, message)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        raise IntegrityError("manifest signature does not match any trusted release key")
    if expected_path is not None and data.get(PATH_FIELD) != expected_path:
        raise IntegrityError(
            f"signed manifest belongs at {data.get(PATH_FIELD)!r}, not {expected_path!r}")
    return data


def verify_signed_json(raw: bytes, expected_path=None) -> dict:
    """Parse *raw* JSON and :func:`verify_manifest` it."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise IntegrityError(f"manifest is not valid JSON: {e}") from e
    return verify_manifest(data, expected_path=expected_path)


def _version_key(v):
    import re
    return tuple(int(n) for n in re.findall(r"\d+", str(v)))


def is_newer(remote, local) -> bool:
    """Whether *remote* is a strictly newer version than *local* (numeric parts
    compared in order: ``YYYY-MM-DD-NNN``, ``vYYYYMMDD.N``). A NAS client only
    moves forward, so an old validly-signed manifest replayed onto the
    world-writable NAS cannot roll it back. ``local`` ``None`` = not installed."""
    if not remote or not _version_key(remote):
        return False
    if not local or not _version_key(local):
        return True
    return _version_key(remote) > _version_key(local)


def require_sha256_for_sftp(manifest: dict) -> None:
    """Every ``sftp://`` archive named by *manifest* (top-level ``url`` or an
    ``update_info`` entry) must carry a ``sha256``, so the signature covers the
    archive bytes too."""
    entries = [manifest] + [e for e in manifest.get("update_info", []) if isinstance(e, dict)]
    for entry in entries:
        if is_sftp_url(entry.get("url")) and not entry.get("sha256"):
            raise IntegrityError(f"signed manifest lists {entry['url']} without a sha256")


class SigningKeyStore:
    """The publisher's Ed25519 private key, DPAPI-encrypted on disk."""

    def __init__(self, path=None, *, protect=dpapi_protect, unprotect=dpapi_unprotect):
        self.path = Path(path) if path else DEFAULT_SIGNING_KEY_PATH
        self._protect = protect
        self._unprotect = unprotect

    def load(self):
        try:
            return self._unprotect(base64.b64decode(self.path.read_bytes()))
        except Exception:
            return None

    def save(self, private_raw: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(base64.b64encode(self._protect(private_raw)))
        os.replace(tmp, self.path)


# ── connection ───────────────────────────────────────────────────────────────

class _Session:
    """Context manager yielding a ``paramiko.SFTPClient``; closes the transport."""

    def __init__(self, transport, sftp):
        self._transport = transport
        self.sftp = sftp

    def __enter__(self):
        return self.sftp

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.sftp.close()
        finally:
            self._transport.close()


def connect(host, port, username, password, timeout=CONNECT_TIMEOUT):
    """Open an SFTP session after checking the pinned host key.

    Raises :class:`HostKeyError`, :class:`AuthError` (bad login) or
    :class:`RtnasError` (network). Use as ``with connect(...) as sftp:``."""
    import socket

    try:
        import paramiko  # lazy: keep the dependency off the pure path
    except ImportError as e:  # a frozen build that left paramiko out
        raise RtnasError(f"SFTP support is missing from this build: {e}") from e

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise RtnasError(f"cannot reach {host}:{port}: {e}") from e
    try:
        transport = paramiko.Transport(sock)
    except Exception as e:
        sock.close()
        raise RtnasError(f"SFTP connection to {host} failed: {e}") from e
    try:
        transport.banner_timeout = timeout
        transport.auth_timeout = timeout
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        if not host_key_ok(host, key.asbytes()):
            raise HostKeyError(f"unexpected host key for {host}: {fingerprint(key.asbytes())}")
        try:
            transport.auth_password(username, password)
        except paramiko.AuthenticationException as e:
            raise AuthError(f"NAS login rejected for {username}") from e
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.get_channel().settimeout(60)
        return _Session(transport, sftp)
    except RtnasError:
        transport.close()
        raise
    except Exception as e:
        transport.close()
        raise RtnasError(f"SFTP connection to {host} failed: {e}") from e


def connect_url(url, *, store=None, connect=connect, timeout=CONNECT_TIMEOUT):
    """Connect to the host named by *url* with the saved credentials.
    Returns ``(session, remote_path)``; :class:`AuthError` if none are saved."""
    host, port, path = parse_sftp_url(url)
    creds = (store or CredentialStore()).load()
    if creds is None:
        raise AuthError("no saved RT NAS credentials")
    username, password = creds
    return connect(host, port, username, password, timeout), path


# ── remote operations (take an SFTP client) ──────────────────────────────────

def _is_dir(sftp, path) -> bool:
    try:
        return _stat.S_ISDIR(sftp.stat(path).st_mode)
    except (IOError, OSError):
        return False


def makedirs(sftp, remote_dir) -> None:
    """``mkdir -p``. Stats first: the NAS answers a bare ``Failure`` to mkdir of
    an existing directory, which would be indistinguishable from a real error."""
    remote_dir = posixpath.normpath(remote_dir)
    if remote_dir in ("/", "") or _is_dir(sftp, remote_dir):
        return
    makedirs(sftp, posixpath.dirname(remote_dir))
    sftp.mkdir(remote_dir)


def _remove_quiet(sftp, path):
    try:
        sftp.remove(path)
    except (IOError, OSError, KeyError):
        pass


def put_atomic(sftp, local_path, remote_path, progress_cb=None) -> None:
    """Upload to ``<remote>.tmp``, check the size, then rename over *remote_path*
    so a reader never sees a half-written file. On any failure the tmp file is
    removed and the previous release stays in place."""
    local_path = Path(local_path)
    size = local_path.stat().st_size
    makedirs(sftp, posixpath.dirname(remote_path))
    tmp = remote_path + ".tmp"
    try:
        with open(local_path, "rb") as f:
            sftp.putfo(f, tmp, file_size=size, callback=progress_cb, confirm=False)
        remote_size = sftp.stat(tmp).st_size
        if remote_size != size:
            raise IntegrityError(
                f"uploaded size {remote_size} != local {size} for {remote_path}")
        if hasattr(sftp, "posix_rename"):
            sftp.posix_rename(tmp, remote_path)
        else:  # pragma: no cover — every OpenSSH server has posix-rename
            _remove_quiet(sftp, remote_path)
            sftp.rename(tmp, remote_path)
    except Exception:
        _remove_quiet(sftp, tmp)
        raise


def _check_sha(actual, expected, what):
    if expected and actual.lower() != expected.lower():
        raise IntegrityError(f"SHA-256 mismatch for {what}: got {actual}, want {expected}")


def fetch_bytes(sftp, remote_path, expected_sha256=None, progress_cb=None) -> bytes:
    buf = io.BytesIO()
    sftp.getfo(remote_path, buf, callback=progress_cb)
    data = buf.getvalue()
    _check_sha(sha256_bytes(data), expected_sha256, remote_path)
    return data


def download_to_file(sftp, remote_path, dest, expected_sha256=None, progress_cb=None) -> int:
    """Stream *remote_path* into *dest*; a SHA-256 mismatch deletes *dest*."""
    dest = Path(dest)
    try:
        with open(dest, "wb") as f:
            sftp.getfo(remote_path, f, callback=progress_cb)
        _check_sha(sha256_file(dest), expected_sha256, remote_path)
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    return dest.stat().st_size


def fetch_url(url, *, store=None, connect=connect, expected_sha256=None,
              timeout=CONNECT_TIMEOUT) -> bytes:
    """One-shot download of an ``sftp://`` URL with the saved credentials."""
    session, path = connect_url(url, store=store, connect=connect, timeout=timeout)
    with session as sftp:
        return fetch_bytes(sftp, path, expected_sha256=expected_sha256)


# ── command line ─────────────────────────────────────────────────────────────

def _cli(argv) -> int:
    import getpass

    cmd = argv[0] if argv else "check"
    if cmd in ("keygen", "pubkey"):
        keys = SigningKeyStore()
        private = keys.load()
        if cmd == "keygen":
            if keys.path.exists():
                if "--force" not in argv:
                    print(f"A signing key file already exists at {keys.path} "
                          f"(use --force to replace; the old file is kept as a backup).")
                    return 1
                import time
                backup = keys.path.with_name(
                    f"{keys.path.name}.bak{time.strftime('%Y%m%d%H%M%S')}")
                os.replace(keys.path, backup)
                print(f"Old key file moved to {backup}")
            private, _pub = generate_signing_key()
            keys.save(private)
            print(f"Saved a new release signing key at {keys.path}")
        elif private is None:
            print("No signing key — run: python rtnas.py keygen")
            return 1
        print(f"Public key (pin in TRUSTED_RELEASE_KEYS): {public_key_of(private)}")
        return 0
    store = CredentialStore()
    if cmd == "logout":
        store.clear()
        print("Saved RT NAS credentials removed.")
        return 0
    if cmd == "login":
        user = input(f"Username [{default_username()}]: ").strip() or default_username()
        if "\\" not in user:
            user = DOMAIN_PREFIX + user
        password = getpass.getpass("Domain password: ")
        try:
            with connect(NAS_HOST, NAS_PORT, user, password):
                pass
        except RtnasError as e:
            print(f"Login failed: {e}")
            return 1
        store.save(user, password)
        print(f"OK — saved for {user} at {store.path}")
        return 0
    if cmd == "check":
        creds = store.load()
        if creds is None:
            print("No saved credentials — run: python rtnas.py login")
            return 1
        try:
            with connect(NAS_HOST, NAS_PORT, *creds) as sftp:
                print(f"Connected to {NAS_HOST} as {creds[0]}; / = {sorted(sftp.listdir('/'))}")
        except RtnasError as e:
            print(f"Connect failed: {e}")
            return 1
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
