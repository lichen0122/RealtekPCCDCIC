# git_ctrl

A Python module for managing a Gerrit Git server over SSH. All Git operations are executed via `subprocess` calling the git CLI directly.

## Requirements

- Python 3.12+
- Git CLI (system PATH or PortableGit — auto-download available)
- `requests` (for PortableGit auto-download in `example.py`)

## File Structure

```
git_ctrl/
├── app_settings.py       # Application settings (user, SSH key, Git path, etc.)
├── git_ssh_manager.py    # GitSSHManager core class
├── example.py            # Usage example / demo script
├── tools/
│   └── encode_ssh_key.py # SSH key compression & encoding utility
├── pyproject.toml
└── README.md
```

## Modules

### app_settings.py

Manages global application settings. The settings file is stored at `~/.PCDV/Register_Editor/settings.json`.

**Settings Fields:**

| Field | Type | Description |
|---|---|---|
| `user_name` | `str` | User name (must not contain `@`) |
| `ssh_key_path` | `str` | Path to SSH private key (falls back to embedded key when empty) |
| `portable_git_path` | `str` | Path to PortableGit folder (uses system PATH when empty) |
| `fetch_on_page_switch` | `bool` | Whether to auto-fetch on page switch |
| `remind_to_upload` | `bool` | Show a reminder to commit & upload after save |
| `font_size_str` | `str` | Font size preset (`Small` / `Medium` / `Large`) |
| `history_editor_only` | `bool` | Only show commits that touch the Register_Editor folder |
| `window_geometry` | `str` | Window geometry (base64-encoded) |

**Computed Properties:**

| Property | Description |
|---|---|
| `git_available` | Whether Git is reachable (system PATH or PortableGit) |
| `git_executable` | Full path to git.exe |
| `ssh_executable` | Full path to ssh.exe |
| `os_login_name` | Cached OS login name (empty string if unavailable) |
| `effective_user_name` | OS login name if available, otherwise saved `user_name` |
| `is_admin` | Whether the current user is an admin |
| `is_setup_complete` | Whether user name, SSH key, and git are all configured |
| `using_embedded_key` | Whether the embedded SSH key is in use |
| `font_pixel_size` | Pixel size for the current font preset |

**Gerrit Server Constants:**

| Constant | Value |
|---|---|
| `GERRIT_HOST` | `pcicdv-git.rtkbf.com` |
| `GERRIT_PORT` | `29418` |
| `GERRIT_GIT_ROOT` | `ssh://cychang@pcicdv-git.rtkbf.com:29418` |
| `PORTABLE_GIT_DIR` | `~/.PCDV/PortableGit` (auto-download target directory) |
| `PORTABLE_GIT_URL` | GCS URL for PortableGit.zip (~160 MB) |

---

### git_ssh_manager.py

`GitSSHManager` — the core class for managing Git remotes over SSH.

**Features:**

- Injects SSH key via the `GIT_SSH_COMMAND` environment variable
- Automatic retry with exponential backoff for network operations (push / pull / clone / fetch / ls-remote)
- Full operation history (`GitOperation` records) for debugging
- Context manager support (`with` statement)
- All repos are stored under `~/PCDV/GitRepo/`

**Supported Git Operations:**

| Method | Description |
|---|---|
| `clone()` | Clone a repo (supports branch selection / shallow clone) |
| `add()` | Stage files |
| `commit()` | Commit (supports `--allow-empty`, `-a`) |
| `push()` | Push (uses `--force-with-lease`, supports `-u`) |
| `pull()` | Pull (supports `--rebase`) |
| `fetch()` | Fetch (supports `--prune`) |
| `checkout()` | Checkout a branch (supports `-b` to create) |
| `reset()` | Reset (supports `--hard`) |
| `rebase()` | Rebase (supports `--continue` / `--abort`) |
| `restore()` | Restore a file to a specific commit version |
| `status()` | Show working directory status |
| `log()` | Show commit log |
| `diff()` | Show diff (supports `--staged`) |
| `stash()` | Stash operations (push / pop / list / drop / apply) |
| `show()` | Show commit info |
| `rev_parse()` | Get the SHA for a given ref |
| `rev_list_count()` | Count commits between two refs |
| `remote_info()` | Show remote info |
| `run()` | Escape hatch — execute any arbitrary git subcommand |

**Usage Example:**

```python
from app_settings import app_settings
from git_ssh_manager import GitSSHManager, GIT_REPO_ROOT

mgr = GitSSHManager(
    repo_path=GIT_REPO_ROOT / "my_project",
    ssh_key_path=app_settings.ssh_key_path,
    git_exe=app_settings.git_executable,
    ssh_exe=app_settings.ssh_executable,
)

# Clone
mgr.clone("ssh://cychang@pcicdv-git.rtkbf.com:29418/my_project")

# Add, Commit, Push
mgr.add(".")
mgr.commit("my commit message")
mgr.push(branch="main", set_upstream=True)

# Fetch & Pull
mgr.fetch()
mgr.pull(branch="main")

# View operation history
print(mgr.history_summary())
```

**Context Manager:**

```python
with GitSSHManager(repo_path=..., ssh_key_path=...) as mgr:
    mgr.fetch()
    mgr.pull(branch="main")
# Automatically logs session summary on exit
```

---

### example.py

A full demo script that walks through the following steps:

1. **Startup checks** — Validates user name (auto-detects OS login via `effective_user_name`), checks Git availability (offers auto-download of PortableGit with terminal progress bar), then prints current settings
2. **Gerrit ls-projects** — Lists all projects on the Gerrit server (with permission-based filtering)
3. **Clone** — Clones a specified project locally
4. **Fetch / Pull** — Fetches and pulls remote updates

```bash
python example.py
```

---

### tools/encode_ssh_key.py

Compresses and hex-encodes an SSH private key (zlib + hex) for embedding as `_EMBEDDED_KEY` in `app_settings.py`.

```bash
python tools/encode_ssh_key.py [path_to_key]
```

Copy the output and replace the `_EMBEDDED_KEY` value in `app_settings.py`.
