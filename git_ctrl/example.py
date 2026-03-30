"""Example: Using GitSSHManager with Gerrit Server

Demonstrates:
    1. gerrit ls-projects  — list all projects on the Gerrit server
    2. git clone           — clone a project from Gerrit
    3. git add             — stage files
    4. git commit          — commit staged changes
    5. git push            — push commits to remote
    6. git fetch           — fetch remote updates
    7. git pull            — pull remote changes (fetch + merge)

Requirements:
    - Python 3.10+
    - git CLI installed (system PATH or PortableGit)

Usage:
    python example_git.py
"""

from __future__ import annotations

import subprocess
import sys

from app_settings import (
    GERRIT_HOST,
    GERRIT_PORT,
    GERRIT_USER,
    GERRIT_GIT_ROOT,
    _GERRIT_HIDDEN_PROJECTS,
    _GERRIT_ADMIN_PROJECTS,
    _GERRIT_PROJECT_PREFIX,
    app_settings,
)
from git_ssh_manager import GitSSHManager, GitCommandError, GIT_REPO_ROOT


# ── Helper functions ─────────────────────────────────────────────


def list_gerrit_projects() -> list[str]:
    """List all projects on the Gerrit server via SSH.

    Equivalent to:
        ssh -p 29418 -i <key> cychang@pcicdv-git.rtkbf.com gerrit ls-projects
    """
    ssh_exe = app_settings.ssh_executable
    key_path = app_settings.ssh_key_path

    cmd = [
        ssh_exe,
        "-p", str(GERRIT_PORT),
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        f"{GERRIT_USER}@{GERRIT_HOST}",
        "gerrit", "ls-projects",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    if result.returncode != 0:
        print(f"Error listing projects: {result.stderr}", file=sys.stderr)
        return []

    return [p for p in result.stdout.strip().split("\n") if p]


def create_manager(project_name: str) -> GitSSHManager:
    """Create a GitSSHManager instance for a Gerrit project.

    The repository will be stored under ~/PCDV/GitRepo/<project_name>.
    """
    repo_path = GIT_REPO_ROOT / project_name

    return GitSSHManager(
        repo_path=repo_path,
        ssh_key_path=app_settings.ssh_key_path,
        git_exe=app_settings.git_executable,
        ssh_exe=app_settings.ssh_executable,
    )


def clone_project(mgr: GitSSHManager, project_name: str) -> None:
    """Clone a project from Gerrit. Skips if the repo already exists locally."""
    if mgr.repo_path.exists():
        print(f"  Repository already exists at: {mgr.repo_path}")
        return

    repo_url = f"{GERRIT_GIT_ROOT}/{project_name}"
    print(f"  Cloning {repo_url} -> {mgr.repo_path} ...")
    mgr.clone(repo_url)
    print("  Clone complete.")


def add_files(mgr: GitSSHManager) -> None:
    """Stage all changes (git add .)."""
    mgr.add(".")
    print("  Staged all changes.")


def commit_changes(mgr: GitSSHManager, message: str) -> None:
    """Commit staged changes."""
    mgr.commit(message)
    print(f"  Committed: {mgr.last_commit}")


def push_to_remote(mgr: GitSSHManager, branch: str = "main") -> None:
    """Push commits to the remote branch."""
    mgr.push(branch=branch, set_upstream=True)
    print(f"  Pushed to {branch}.")


def fetch_updates(mgr: GitSSHManager) -> None:
    """Fetch remote updates without merging."""
    mgr.fetch()
    print("  Fetch complete.")


def pull_changes(mgr: GitSSHManager, branch: str = "main") -> None:
    """Pull remote changes (fetch + merge)."""
    mgr.pull(branch=branch)
    print("  Pull complete.")


# ── Main ─────────────────────────────────────────────────────────


if __name__ == "__main__":
    # ── 0a. Check user_name ─────────────────────────────────────
    while True:
        name = app_settings.user_name.strip()
        if name and "@" not in name:
            break
        if not name:
            print("尚未設定使用者名稱。")
        else:
            print("使用者名稱不可包含 '@' 字元。")
        name = input("請輸入使用者名稱: ").strip()
        if name and "@" not in name:
            app_settings.user_name = name
            break
        if not name:
            print("使用者名稱不可為空，請重新輸入。")
        else:
            print("使用者名稱不可包含 '@' 字元，請重新輸入。")

    # ── 0b. Check git_available ─────────────────────────────────
    while not app_settings.git_available:
        print(
            "\n如果電腦尚未安裝 Git 套件，請按照以下步驟安裝。\n"
            "到 Git 官網 https://git-scm.com/install/windows 下載安裝程式。\n"
            "下載 Git for Windows/x64 Setup 進行安裝 (過程中都選預設選項)，\n"
            "成功安裝後，重啟此程式即可。\n"
            "\n"
            "如果因權限的問題無法安裝，請下載 Git for Windows/x64 Portable，\n"
            "解壓縮後會有一個 PortableGit 資料夾，\n"
            "將此資料夾移動到你想要的位置後，回到這邊，輸入你的 PortableGit 資料夾即可。"
        )
        portable_path = input("\n請輸入 PortableGit 資料夾路徑 (或按 Enter 離開): ").strip()
        if not portable_path:
            sys.exit(0)
        app_settings.portable_git_path = portable_path
        if not app_settings.git_available:
            print(f"路徑 '{portable_path}' 下找不到 cmd/git.exe，請重新輸入。")

    # ── 0c. Print settings ──────────────────────────────────────
    print("=" * 50)
    print("App Settings")
    print("=" * 50)
    print(f'  ssh_key_path       : "{app_settings.ssh_key_path}"')
    print(f'  user_name          : "{app_settings.user_name}"')
    print(f'  portable_git_path  : "{app_settings.portable_git_path}"')
    print(f'  git_available      : {app_settings.git_available}')
    print(f'  is_admin           : {app_settings.is_admin}')
    print(f'  using_embedded_key : {app_settings.using_embedded_key}')
    print()

    question = input("存檔後真的會去覆蓋 Register_Editor 的設定檔，可能會影響到真的 Tool 的設定檔，請謹慎使用！\n是否要存檔覆蓋設定？(y/N): ").strip().lower()
    if question == "y":
        app_settings.save()  # 這個存檔後真的會去覆蓋 Register_Editor 的設定檔，可能會影響到真的 Tool 的設定檔，請謹慎使用

    # ── 1. List Gerrit projects ──────────────────────────────────
    print("=" * 50)
    print("1. Gerrit ls-projects")
    print("=" * 50)
    projects = list_gerrit_projects()
    if projects:
        for p in projects:
            print(f"  {p}")
        print(f"\n  Total: {len(projects)} projects\n")
        print("-" * 50)
        print("  Filtered projects (excluding hidden/admin/prefix):\n")
        for fp in projects:
            if not fp or fp in _GERRIT_HIDDEN_PROJECTS:
                continue
            if fp.startswith(_GERRIT_PROJECT_PREFIX):
                print(f"  {fp}")
            elif app_settings.is_admin and fp in _GERRIT_ADMIN_PROJECTS:
                print(f"  {fp}")
    else:
        print("No projects found or error occurred.\n")

    # ── 2. Clone a project ───────────────────────────────────────
    print("=" * 50)
    print("2. Clone project")
    print("=" * 50)
    PROJECT_NAME = "test_1"  # <-- change this to your project name
    try:
        mgr = create_manager(PROJECT_NAME)
        clone_project(mgr, PROJECT_NAME)
    except (GitCommandError, EnvironmentError) as e:
        print(f"  Clone failed: {e}")
        sys.exit(1)

    # ── 3-5 are commented out to avoid accidental writes ─────────
    # Uncomment below to test add / commit / push.

    # ── 3. Add files ─────────────────────────────────────────────
    # print("=" * 50)
    # print("3. Add files")
    # print("=" * 50)
    # add_files(mgr)

    # ── 4. Commit changes ────────────────────────────────────────
    # print("=" * 50)
    # print("4. Commit")
    # print("=" * 50)
    # commit_changes(mgr, "my commit message")

    # ── 5. Push ──────────────────────────────────────────────────
    # print("=" * 50)
    # print("5. Push")
    # print("=" * 50)
    # push_to_remote(mgr, branch="main")

    # ── 6. Fetch ─────────────────────────────────────────────────
    print("=" * 50)
    print("6. Fetch")
    print("=" * 50)
    try:
        fetch_updates(mgr)
    except GitCommandError as e:
        print(f"  Fetch failed: {e}")

    # ── 7. Pull ──────────────────────────────────────────────────
    print("=" * 50)
    print("7. Pull")
    print("=" * 50)
    try:
        pull_changes(mgr, branch="master")
    except GitCommandError as e:
        print(f"  Pull failed: {e}")

    print("\nDone!")
