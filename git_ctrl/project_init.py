"""project_init.py - Initialize app folders across all Gerrit projects.

For each filtered Gerrit project, ensures that certain app_name folders
exist. If a folder is missing, it is created and populated with an
example file (or .gitkeep). Changes are batched, summarized, and only
committed+pushed after user confirmation.

Usage:
    python project_init.py
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from app_settings import (
    PORTABLE_GIT_DIR,
    PORTABLE_GIT_MARKER,
    _is_portable_git_valid,
    _GERRIT_ADMIN_PROJECTS,
    _GERRIT_HIDDEN_PROJECTS,
    _GERRIT_PROJECT_PREFIX,
    app_settings,
)
from example import (
    clone_project,
    create_manager,
    download_portable_git,
    list_gerrit_projects,
    pull_changes,
)
from git_ssh_manager import GitCommandError, GitSSHManager


# ── Constants ───────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent

# Keys   = folder names to create inside each project repo
# Values = relative path (from SCRIPT_DIR) to the source example file
#          empty string → place a .gitkeep instead
APP_INIT_MAP: dict[str, str] = {
    "Register_Editor": "example_file/Register_Editor/example.json",
}


# ── Data ────────────────────────────────────────────────────────


@dataclass
class PendingChange:
    """Tracks a pending file addition for a single project."""

    project_name: str
    app_name: str
    target_path: Path
    source_path: Path | None  # None means .gitkeep was used
    mgr: GitSSHManager
    branch: str = ""


# ── Helper functions ────────────────────────────────────────────


def filter_projects(projects: list[str]) -> list[str]:
    """Filter Gerrit projects: exclude hidden, apply prefix filter, include admin projects."""
    filtered: list[str] = []
    for name in projects:
        if not name or name in _GERRIT_HIDDEN_PROJECTS:
            continue
        if name.startswith(_GERRIT_PROJECT_PREFIX):
            filtered.append(name)
        elif app_settings.is_admin and name in _GERRIT_ADMIN_PROJECTS:
            filtered.append(name)
    return filtered


def resolve_source_file(relative_path: str) -> Path | None:
    """Resolve a source example file path relative to SCRIPT_DIR.

    Returns None if relative_path is empty (meaning .gitkeep should be used).
    Raises FileNotFoundError if the path is non-empty but the file does not exist.
    """
    if not relative_path.strip():
        return None
    source = SCRIPT_DIR / relative_path
    if not source.is_file():
        raise FileNotFoundError(f"Example file not found: {source}")
    return source


def ensure_app_folder(
    mgr: GitSSHManager,
    project_name: str,
    app_name: str,
    source_file: Path | None,
) -> PendingChange | None:
    """Check if app_name folder exists in repo. If not, create it and place the example file.

    Returns a PendingChange if a new file was created, None if the folder already exists.
    """
    app_dir = mgr.repo_path / app_name
    if app_dir.exists():
        print(f"    [SKIP] {project_name}/{app_name}/ already exists")
        return None

    app_dir.mkdir(parents=True, exist_ok=True)

    if source_file is not None:
        dest_file = app_dir / source_file.name
        shutil.copy2(source_file, dest_file)
        print(f"    [ADD]  {project_name}/{app_name}/{source_file.name}")
    else:
        dest_file = app_dir / ".gitkeep"
        dest_file.touch()
        print(f"    [ADD]  {project_name}/{app_name}/.gitkeep")

    return PendingChange(
        project_name=project_name,
        app_name=app_name,
        target_path=dest_file,
        source_path=source_file,
        mgr=mgr,
    )


def _group_by_project(changes: list[PendingChange]) -> dict[str, list[PendingChange]]:
    by_project: dict[str, list[PendingChange]] = {}
    for c in changes:
        by_project.setdefault(c.project_name, []).append(c)
    return by_project


def print_summary(changes: list[PendingChange]) -> None:
    """Print a summary of all files to be committed and pushed."""
    print("\n" + "=" * 60)
    print("Summary of pending changes")
    print("=" * 60)

    by_project = _group_by_project(changes)

    for proj, proj_changes in by_project.items():
        print(f"\n  Project: {proj}")
        for c in proj_changes:
            rel_path = c.target_path.relative_to(c.mgr.repo_path)
            print(f"    + {rel_path}")

    print(f"\n  Total: {len(changes)} file(s) across {len(by_project)} project(s)")
    print("=" * 60)


def commit_and_push_changes(changes: list[PendingChange]) -> None:
    """Stage, commit, and push all pending changes, grouped by project."""
    by_project = _group_by_project(changes)

    for proj, proj_changes in by_project.items():
        mgr = proj_changes[0].mgr
        branch = proj_changes[0].branch
        app_names = ", ".join(c.app_name for c in proj_changes)

        try:
            rel_paths = [str(c.target_path.relative_to(mgr.repo_path)) for c in proj_changes]
            mgr.add(*rel_paths)
            commit_message = f"Initialize app folder(s): {app_names}"
            mgr.commit(commit_message)
            print(f"  Committed: {mgr.last_commit}")
            mgr.push(branch=branch, set_upstream=True)
            print(f"  [OK] {proj}: pushed to {branch}")
        except GitCommandError as e:
            print(f"  [ERROR] {proj}: {e}")


# ── Main ────────────────────────────────────────────────────────


if __name__ == "__main__":
    # ── 0a. Check user_name ─────────────────────────────────────
    if app_settings.effective_user_name:
        print(f"User: {app_settings.effective_user_name}")
    else:
        while True:
            name = input("Please enter user name: ").strip()
            if not name:
                print("User name cannot be empty.")
                continue
            if "@" in name:
                print("User name must not contain '@'.")
                continue
            app_settings.user_name = name
            break

    # ── 0b. Auto-setup PortableGit ────────────────────────────────
    if not app_settings.git_available:
        # Fully valid installation — just reuse it
        if _is_portable_git_valid(PORTABLE_GIT_DIR):
            app_settings.portable_git_path = str(PORTABLE_GIT_DIR)
            app_settings.save()
            print(f"  PortableGit already exists: {PORTABLE_GIT_DIR}")
        else:
            # Incomplete or corrupt — wipe and re-download
            if PORTABLE_GIT_DIR.exists():
                shutil.rmtree(PORTABLE_GIT_DIR, ignore_errors=True)
            if not download_portable_git():
                print("  [Error] PortableGit installation failed, exiting.")
                sys.exit(1)
            # Mark installation as complete
            PORTABLE_GIT_MARKER.touch()
            app_settings.portable_git_path = str(PORTABLE_GIT_DIR)
            app_settings.save()

    # ── 1. Validate APP_INIT_MAP source files ───────────────────
    print("=" * 50)
    print("Validating APP_INIT_MAP source files...")
    print("=" * 50)
    source_files: dict[str, Path | None] = {}
    for app_name, rel_path in APP_INIT_MAP.items():
        try:
            src = resolve_source_file(rel_path)
            source_files[app_name] = src
            label = str(src) if src else ".gitkeep"
            print(f"  {app_name} -> {label}")
        except FileNotFoundError as e:
            print(f"  [FATAL] {e}")
            sys.exit(1)

    # ── 2. List and filter Gerrit projects ──────────────────────
    print("\n" + "=" * 50)
    print("Listing Gerrit projects...")
    print("=" * 50)
    all_projects = list_gerrit_projects()
    if not all_projects:
        print("No projects found or error occurred.")
        sys.exit(1)

    projects = filter_projects(all_projects)
    print(f"  Found {len(projects)} project(s) after filtering.\n")
    for p in projects:
        print(f"  {p}")

    # ── 3. Clone/pull each project, check folders ───────────────
    print("\n" + "=" * 50)
    print("Scanning projects for missing app folders...")
    print("=" * 50)
    all_pending: list[PendingChange] = []

    for project_name in projects:
        print(f"\n  Processing: {project_name}")
        try:
            mgr = create_manager(project_name)
            clone_project(mgr, project_name)

            try:
                branch = mgr.current_branch
                pull_changes(mgr, branch=branch)
            except GitCommandError:
                branch = "master"
                print(f"    [WARN] Pull failed (may be empty repo), continuing...")

            for app_name, source_file in source_files.items():
                change = ensure_app_folder(mgr, project_name, app_name, source_file)
                if change is not None:
                    change.branch = branch
                    all_pending.append(change)

        except (GitCommandError, EnvironmentError) as e:
            print(f"    [ERROR] Skipping {project_name}: {e}")
            continue

    # ── 4. Summary and confirmation ─────────────────────────────
    if not all_pending:
        print("\nNo changes needed. All app folders already exist.")
        sys.exit(0)

    print_summary(all_pending)

    answer = input("\nProceed with commit and push? (y/N): ").strip().lower()
    if answer != "y":
        print("Aborted by user. No changes committed.")
        sys.exit(0)

    # ── 5. Commit and push ──────────────────────────────────────
    print("\nCommitting and pushing changes...")
    commit_and_push_changes(all_pending)

    print("\nDone!")
