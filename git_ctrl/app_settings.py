from __future__ import annotations

import atexit
import base64
import json
import os
import subprocess
import tempfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

from git_ssh_manager import PORTABLE_GIT_DIR, GitSSHManager

APP_NAME = "RegisterEditor"

SETTINGS_PATH = Path.home() / ".PCDV" / APP_NAME / "settings.json"

# ── Gerrit server constants ──────────────────────────────────────
GERRIT_HOST = "pcicdv-git.rtkbf.com"
GERRIT_PORT = 29418
GERRIT_USER = "lichen.liu"
GERRIT_GIT_ROOT = f"ssh://{GERRIT_USER}@{GERRIT_HOST}:{GERRIT_PORT}"
# Projects hidden from the browse list (Gerrit internals)
_GERRIT_HIDDEN_PROJECTS = {"All-Projects", "All-Users"}
# Projects visible only to admin users
_GERRIT_ADMIN_PROJECTS = {f"{APP_NAME}_Test_Project"}
# Only projects whose name starts with this prefix are shown
_GERRIT_PROJECT_PREFIX = f"{APP_NAME}_"

# SSH key: zlib-compressed then hex-encoded — not plaintext in the compiled binary
_EMBEDDED_KEY = (
    b"78DA9D503B73824018ECF915F60E032AA2292CEE2E175E7ABC41E93854541E5140E1FCF5F132"
    b"9322459A6CB1F3CDECCE7E3B2B8A2F40AC1964643B98F8BE3E723C2302011E5978C74551A033"
    b"58D29A3C533469926DD1ED2F1870406DFEA055C84F4CABB77BE2821F404E9B9E73F7CCA67997"
    b"C482CB48100E363721B835D5D69AB0C3957C1CEDE2EE480562F821E5A87DF49A6C9847D9EF94"
    b"AC40F2327FF94DB46FEE09ADD34525C4BF52FF1B2AF0D2709CC59F0B969EEDA577A0159B6B84"
    b"0497FAF60441901A76EF014B51535DA9F421CAEA5BDFBC1E1472B4547D451ABB6BC3978431B0"
    b"9804A89B979267CD4ECC5D078CF743D570DD4D4F253DCFDB741B42906F56C2F79C98BCFF3DF5"
    b"17158B7AB5"
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
    # Font size preset: "Small" (13px), "Medium" (15px), "Large" (17px)
    font_size_str: str = "Medium"
    # History tab: only show commits that touch the RegisterEditor folder
    history_editor_only: bool = False

    @property
    def git_executable(self) -> str:
        """Full path to git.exe (always PortableGit).

        Returns:
            str: Absolute path to git.exe.
        """
        return str(PORTABLE_GIT_DIR / "cmd" / "git.exe")

    @property
    def ssh_executable(self) -> str:
        """Full path to ssh.exe (always PortableGit).

        Returns:
            str: Absolute path to ssh.exe.
        """
        return str(PORTABLE_GIT_DIR / "usr" / "bin" / "ssh.exe")

    @property
    def git_available(self) -> bool:
        """True when PortableGit is fully installed (git.exe + ssh.exe + marker file).

        Returns:
            bool: Whether PortableGit is ready to use.
        """
        return GitSSHManager.is_portable_git_valid()

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
        """Return True if user name, SSH key, and git are all configured.

        ``git_available`` already validates the marker file, so no extra check needed.

        Returns:
            bool: Whether all setup requirements are met.
        """
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
        if not isinstance(self.font_size_str, str):
            self.font_size_str = "Medium"
            dirty = True
        if not isinstance(self.history_editor_only, bool):
            self.history_editor_only = False
            dirty = True

        # ── value checks ──
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
