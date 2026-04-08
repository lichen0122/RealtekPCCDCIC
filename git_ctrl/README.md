# git_ctrl

A Python module for managing a Gerrit Git server over SSH. All Git operations are executed via `subprocess` calling the git CLI directly.

## Requirements

- Python 3.12+
- PortableGit (auto-downloaded on first run, no system git required)
- `requests` (for PortableGit auto-download in `example.py`)

## File Structure

```
git_ctrl/
├── app_settings.py       # Application settings (user, SSH key, Git path, etc.)
├── git_ssh_manager.py    # GitSSHManager core class
├── example.py            # Usage example / demo script
├── project_init.py       # Batch-initialize app folders across all Gerrit projects
├── example_file/         # Source example files for project_init.py
│   └── RegisterEditor/
│       └── example.json
├── tools/
│   └── encode_ssh_key.py # SSH key compression & encoding utility
├── pyproject.toml
└── README.md
```

## Modules

### app_settings.py

Manages global application settings. The settings file is stored at `~/.PCDV/RegisterEditor/settings.json`.

**Settings Fields:**

| Field | Type | Description |
|---|---|---|
| `user_name` | `str` | User name (must not contain `@`) |
| `ssh_key_path` | `str` | Path to SSH private key (falls back to embedded key when empty) |
| `portable_git_path` | `str` | Root path of PortableGit folder (auto-managed) |
| `fetch_on_page_switch` | `bool` | Whether to auto-fetch on page switch |
| `remind_to_upload` | `bool` | Show a reminder to commit & upload after save |
| `font_size_str` | `str` | Font size preset (`Small` / `Medium` / `Large`) |
| `history_editor_only` | `bool` | Only show commits that touch the RegisterEditor folder |
| `window_geometry` | `str` | Window geometry (base64-encoded) |

**Computed Properties:**

| Property | Description |
|---|---|
| `git_available` | Whether PortableGit is fully installed (git.exe + ssh.exe + marker file) |
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
| `PORTABLE_GIT_MARKER` | `~/.PCDV/PortableGit/.setup_complete` (marker file created after successful download + extraction) |

**Shared Helpers:**

| Function | Description |
|---|---|
| `_is_portable_git_valid(base)` | Validates that a directory contains `cmd/git.exe`, `usr/bin/ssh.exe`, and the marker file. Used by `git_available`, `_validate()`, and callers of `download_portable_git()` — never inline this check. |

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

1. **Startup checks** — Validates user name (auto-detects OS login via `effective_user_name`), auto-downloads PortableGit if not installed (with terminal progress bar and marker file validation), then prints current settings
2. **Gerrit ls-projects** — Lists all projects on the Gerrit server (with permission-based filtering)
3. **Clone** — Clones a specified project locally
4. **Fetch / Pull** — Fetches and pulls remote updates

```bash
python example.py
```

---

### project_init.py

Batch-initializes example files across all filtered Gerrit projects. For each project, it checks whether certain files exist in the repo root; if missing, copies them from example sources (or creates a `.gitkeep`).

**Configuration:**

The `INIT_FILES` list at the top of the file controls which files to initialize:

```python
INIT_FILES: list[str] = [
    f"example_file/{APP_NAME}/example.json",
]
```

- Each entry is a relative path (from `git_ctrl/`) to a source example file to copy into the repo root
- An empty string entry places a `.gitkeep` instead

**Workflow:**

1. Validates user name and auto-downloads PortableGit if needed (same as `example.py`)
2. Validates all source files in `INIT_FILES` exist
3. Lists and filters Gerrit projects (prefix-based + admin filtering)
4. Prompts user to confirm the filtered project list before proceeding
5. For each project: clones (if needed), pulls latest, checks each init file (skips the project if pull fails)
6. Prints a grouped summary of all pending file additions
7. Prompts `y/N` — only commits and pushes on user confirmation (one commit per project)

```bash
python project_init.py
```

---

### tools/encode_ssh_key.py

Compresses and hex-encodes an SSH private key (zlib + hex) for embedding as `_EMBEDDED_KEY` in `app_settings.py`.

```bash
python tools/encode_ssh_key.py [path_to_key]
```

Copy the output and replace the `_EMBEDDED_KEY` value in `app_settings.py`.
