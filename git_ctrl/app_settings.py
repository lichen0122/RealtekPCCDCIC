from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import subprocess
import tempfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "Register_Editor"

SETTINGS_PATH = Path.home() / ".PCDV" / APP_NAME / "settings.json"

# ── Gerrit server constants ──────────────────────────────────────
GERRIT_HOST = "pcicdv-git.rtkbf.com"
GERRIT_PORT = 29418
GERRIT_USER = "cychang"
GERRIT_GIT_ROOT = f"ssh://{GERRIT_USER}@{GERRIT_HOST}:{GERRIT_PORT}"
# Projects hidden from the browse list (Gerrit internals)
_GERRIT_HIDDEN_PROJECTS = {"All-Projects", "All-Users"}
# Projects visible only to admin users
_GERRIT_ADMIN_PROJECTS = {"test_0", "test_1"}
# Only projects whose name starts with this prefix are shown (admin users also see _GERRIT_ADMIN_PROJECTS)
_GERRIT_PROJECT_PREFIX = "ALC"

# SSH key: zlib-compressed then hex-encoded — not plaintext in the compiled binary
_EMBEDDED_KEY = (
    b"78DA9D904D8F82301884EFFC0AEFC6448A281EDB5AF9502B2888F446854596AF4541815FAFDD"
    b"640F7BD8CB3E87C99BBC93C96426933788E8261DED6D428F4763641FCC1374C9684302F19C48"
    b"5C41392FE91062F9C6CE59137D122840BAFAE085274EC28B65CB1CF80312B27B0A6D860B481A"
    b"E64B4E4F5DAFDB0B13C6461A7F944C86855F25F120CFB3D92E957DEA69FC7E70F9DC0A36CDD7"
    b"B39DDBA69DBCFD9B55EA789632CBD6588A7EA5FE375412A51126BA7A42196CCDF1B32D0D44EB"
    b"5EBBCA7DB8D842EEAD95C6B93D2ED63964E05EC5963A8E4A0FB330450BFB5CBB49BF3D0592EC"
    b"766DB7A68F2A4F6A50E245A52563D14F2FA81A80EB95171164209F5E140B6C4B2B0FFC6ECAFC"
    b"7B1B8065031D73257D4F4CE8EAEFF95F4DC581D6"
)

_ADMIN_USERS = {"cychang", "lichen.liu", "elly_wu", "chueh_chuang_wei"}
_FONT_SIZE_MAP = {"Small": 13, "Medium": 15, "Large": 17}
_temp_key_path: str | None = None


def _get_embedded_key_path() -> str:
    """Decode the embedded SSH key to a temp file and return its path."""
    global _temp_key_path
    if _temp_key_path and Path(_temp_key_path).exists():
        return _temp_key_path
    raw = zlib.decompress(base64.b16decode(_EMBEDDED_KEY))
    fd, path = tempfile.mkstemp(prefix="", suffix=".tmp")
    os.write(fd, raw)
    os.close(fd)
    # Windows NTFS: remove inherited ACLs, grant only current user read access
    subprocess.run(
        ["icacls", path, "/inheritance:r", "/grant:r", f"{os.getlogin()}:(R)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _temp_key_path = path
    def _cleanup(p: str = path) -> None:
        try:
            subprocess.run(
                ["icacls", p, "/grant:r", f"{os.getlogin()}:(F)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_cleanup)
    return path


@dataclass
class AppSettings:
    fetch_on_page_switch: bool = True
    # Show a reminder to commit & upload after save / new-file / merge
    remind_to_upload: bool = True
    # Absolute path to SSH private key for Gerrit server
    ssh_key_path: str = ""
    # Window geometry stored as base64-encoded string of QByteArray
    window_geometry: str = ""
    # User name displayed in the status bar
    user_name: str = ""
    # Root path of a PortableGit folder (leave empty to use system PATH git)
    portable_git_path: str = ""
    # Font size preset: "Small" (13px), "Medium" (15px), "Large" (17px)
    font_size_str: str = "Medium"
    # History tab: only show commits that touch the Register_Editor folder
    history_editor_only: bool = False

    @property
    def git_executable(self) -> str:
        """Full path to git.exe when using PortableGit, otherwise 'git'."""
        if self.portable_git_path:
            return str(Path(self.portable_git_path) / "cmd" / "git.exe")
        return "git"

    @property
    def ssh_executable(self) -> str:
        """Full path to ssh.exe when using PortableGit, otherwise 'ssh'."""
        if self.portable_git_path:
            return str(Path(self.portable_git_path) / "usr" / "bin" / "ssh.exe")
        return "ssh"

    @property
    def git_available(self) -> bool:
        """True when git is reachable (system PATH or valid portable_git_path)."""
        if shutil.which("git"):
            return True
        if self.portable_git_path:
            return Path(self.portable_git_path, "cmd", "git.exe").is_file()
        return False

    @property
    def font_pixel_size(self) -> int:
        """Pixel size corresponding to the current font_size preset."""
        return _FONT_SIZE_MAP.get(self.font_size_str, 15)

    @property
    def os_login_name(self) -> str:
        """Cached OS login name (empty string if unavailable)."""
        try:
            name = self._os_login_cache
        except AttributeError:
            try:
                name = os.getlogin().strip()
            except OSError:
                name = ""
            name = ""
            self._os_login_cache = name  # type: ignore[misc]
        return name

    @property
    def effective_user_name(self) -> str:
        """Return OS login name if available, otherwise the saved *user_name*."""
        return self.os_login_name or self.user_name.strip()

    @property
    def is_admin(self) -> bool:
        """True when the current user_name is in the admin list."""
        return self.effective_user_name in _ADMIN_USERS

    @property
    def is_setup_complete(self) -> bool:
        """Return True if user name, SSH key, and git are all configured."""
        return (
            bool(self.effective_user_name)
            and bool(self.ssh_key_path.strip())
            and self.git_available
        )

    @property
    def using_embedded_key(self) -> bool:
        """True when ssh_key_path points to the auto-generated embedded key."""
        return _temp_key_path is not None and self.ssh_key_path == _temp_key_path

    def load(self) -> None:
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(self, k):
                        setattr(self, k, v)
            except Exception:
                pass
        self._validate()
        if not self.ssh_key_path:
            self.ssh_key_path = _get_embedded_key_path()

    def _validate(self) -> None:
        """Reset invalid settings to defaults and persist corrections."""
        dirty = False

        # ── type checks ──
        if not isinstance(self.fetch_on_page_switch, bool):
            self.fetch_on_page_switch = False
            dirty = True
        if not isinstance(self.remind_to_upload, bool):
            self.remind_to_upload = True
            dirty = True
        if not isinstance(self.ssh_key_path, str):
            self.ssh_key_path = ""
            dirty = True
        if not isinstance(self.window_geometry, str):
            self.window_geometry = ""
            dirty = True
        if not isinstance(self.user_name, str):
            self.user_name = ""
            dirty = True
        if not isinstance(self.portable_git_path, str):
            self.portable_git_path = ""
            dirty = True
        if not isinstance(self.font_size_str, str):
            self.font_size_str = "Medium"
            dirty = True
        if not isinstance(self.history_editor_only, bool):
            self.history_editor_only = False
            dirty = True

        # ── value checks ──
        # portable_git_path: must contain cmd/git.exe when set
        if self.portable_git_path:
            if not Path(self.portable_git_path, "cmd", "git.exe").is_file():
                self.portable_git_path = ""
                dirty = True

        # font_size: must be a valid preset
        if self.font_size_str not in _FONT_SIZE_MAP:
            self.font_size_str = "Medium"
            dirty = True

        # ssh_key_path: file must exist when set (empty → embedded key fallback)
        if self.ssh_key_path and not Path(self.ssh_key_path).is_file():
            self.ssh_key_path = ""
            dirty = True

        # user_name: reject '@' (same rule as SettingPage UI)
        if self.user_name and "@" in self.user_name:
            self.user_name = self.user_name.replace("@", "")
            dirty = True

        if dirty:
            self.save()

    def save(self) -> None:
        data = asdict(self)
        if self.using_embedded_key:
            data["ssh_key_path"] = ""
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


app_settings = AppSettings()
app_settings.load()
