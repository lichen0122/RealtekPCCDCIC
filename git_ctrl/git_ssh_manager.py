"""
GitSSHManager - Manage Git remotes over SSH.

Features:
    - All operations go through subprocess calling git CLI directly, no third-party Git libraries
    - Customizable SSH key path, injected via GIT_SSH_COMMAND
    - Portable Git support (custom git / ssh executable paths)
    - Auto retry with exponential backoff for network operations (push / pull / clone / fetch / ls-remote)
    - Repo path restricted to ~/PCDV/GitRepo/
    - Full operation history for debugging
    - Context manager support (with statement)

Exports:
    - GIT_REPO_ROOT: Root directory for Git repos (~/PCDV/GitRepo/)
    - GitCommandError: Exception raised when a git command fails
    - GitOperation: Single operation record dataclass
    - GitSSHManager: Core manager class

Requires: Python 3.12+, git CLI installed (system PATH or portable)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Self

logger = logging.getLogger(__name__)

# Git repos are stored under ~/PCDV/GitRepo/
GIT_REPO_ROOT = Path.home() / "PCDV" / "GitRepo"


# ---------------------------------------------------------------------------
# Custom Exception
# ---------------------------------------------------------------------------

class GitCommandError(Exception):
    """Exception raised when a git command fails."""

    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        cmd_str = " ".join(cmd)
        super().__init__(
            f"Command failed (rc={returncode}): {cmd_str}\n"
            f"--- stderr ---\n{stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Operation History
# ---------------------------------------------------------------------------

@dataclass
class GitOperation:
    """Single operation record."""
    timestamp: datetime
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    attempt: int = 1  # Which attempt succeeded (retry-related)

    def __repr__(self) -> str:
        status = "OK" if self.returncode == 0 else f"FAIL(rc={self.returncode})"
        cmd_str = " ".join(self.cmd)
        return (
            f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] {status} "
            f"({self.duration_ms:.0f}ms, attempt={self.attempt}) {cmd_str}"
        )


# ---------------------------------------------------------------------------
# Main Class
# ---------------------------------------------------------------------------

class GitSSHManager:
    """Core class for managing Git remotes over SSH.

    Parameters
    ----------
    repo_path : str | Path
        Local git repo path (may point to the intended target before clone).
        Must be inside GIT_REPO_ROOT.
    ssh_key_path : str | Path | None
        Path to SSH private key. When None, -i is omitted and SSH falls back
        to ~/.ssh/config.
    remote_name : str
        Default remote name.
    ssh_options : dict[str, str] | None
        Extra SSH options, e.g. {"StrictHostKeyChecking": "no"}.
    retry_max : int
        Maximum retry attempts for network operations.
    retry_delay : float
        Base retry delay in seconds; doubles on each attempt (exponential backoff).
    timeout : int | None
        Timeout in seconds for a single git command.
    git_exe : str
        Git executable path or name. Supports Portable Git
        (e.g. "C:/PortableGit/cmd/git.exe").
    ssh_exe : str
        SSH executable path or name. Supports Portable Git
        (e.g. "C:/PortableGit/usr/bin/ssh.exe").
    """

    # Subcommands that interact with remotes and may fail due to network issues
    _NETWORK_COMMANDS: set[str] = {"clone", "push", "pull", "fetch", "ls-remote"}

    def __init__(
        self,
        repo_path: str | Path,
        ssh_key_path: str | Path | None = None,
        remote_name: str = "origin",
        ssh_options: dict[str, str] | None = None,
        retry_max: int = 3,
        retry_delay: float = 2.0,
        timeout: int | None = 60,
        git_exe: str = "git",
        ssh_exe: str = "ssh",
    ) -> None:
        # Validate git executable: full path → check file exists; bare name → check PATH
        if os.sep in git_exe or (os.altsep and os.altsep in git_exe):
            if not Path(git_exe).is_file():
                raise EnvironmentError(f"Git executable not found: {git_exe}")
        elif shutil.which(git_exe) is None:
            raise EnvironmentError("Git CLI not found. Please install Git and ensure it's in your PATH.")
        self._git_exe = git_exe
        self._ssh_exe = ssh_exe

        # Ensure repo_path's parent directory exists (needed for clone)
        self.git_repo_root_path = GIT_REPO_ROOT
        if not self.git_repo_root_path.exists():
            self.git_repo_root_path.mkdir(parents=True)

        self.repo_path = Path(repo_path).resolve()
        if not self.repo_path.parent.exists():
            raise FileNotFoundError(f"Repo path's parent directory does not exist: {self.repo_path.parent}")
        else:
            if self.repo_path.parent != self.git_repo_root_path:
                raise ValueError(f'Repo path must be inside "{self.git_repo_root_path}"\nBut you provided: "{self.repo_path}"')
        self.ssh_key_path = Path(ssh_key_path).resolve() if ssh_key_path else None
        self.remote_name = remote_name
        self.ssh_options = ssh_options or {"StrictHostKeyChecking": "no"}
        self.retry_max = retry_max
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.history: list[GitOperation] = []

        # Validate SSH key (only when explicitly provided)
        if self.ssh_key_path and not self.ssh_key_path.exists():
            raise FileNotFoundError(f"SSH key not found: {self.ssh_key_path}")

    # ------------------------------------------------------------------
    # Context Manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        logger.info("GitSSHManager entered for repo: %s", self.repo_path)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None:
            logger.error(
                "GitSSHManager exiting with error: %s: %s", exc_type.__name__, exc_val
            )
        summary = self.history_summary()
        if summary:
            logger.info("Session summary:\n%s", summary)
        return False  # Do not suppress exceptions

    # ------------------------------------------------------------------
    # Internal Utilities
    # ------------------------------------------------------------------

    def _ssh_env(self) -> dict[str, str]:
        """Build environment dict containing GIT_SSH_COMMAND.

        When ssh_key_path is not set, -i is omitted and SSH uses ~/.ssh/config.
        """
        # Use forward slashes — backslashes are treated as escape chars
        # inside GIT_SSH_COMMAND on Windows.  Quote in case path has spaces.
        ssh = str(self._ssh_exe).replace("\\", "/")
        parts: list[str] = [f'"{ssh}"' if " " in ssh else ssh]
        if self.ssh_key_path:
            # Use forward slashes — backslashes are treated as escape chars
            # inside GIT_SSH_COMMAND on Windows.
            key = str(self.ssh_key_path).replace("\\", "/")
            parts.append(f'-i "{key}"')
        for k, v in self.ssh_options.items():
            parts.append(f'-o "{k}={v}"')

        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = " ".join(parts)
        return env

    def _run_git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        retry: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Unified entry point for executing git commands.

        Parameters
        ----------
        *args : str
            Git subcommand and arguments, e.g. ("status", "--short").
        cwd : Path | None
            Working directory. Defaults to self.repo_path.
        check : bool
            Whether to raise GitCommandError on failure.
        retry : bool
            Whether to enable auto retry (only effective for network commands).

        Returns
        -------
        subprocess.CompletedProcess[str]
        """
        cmd = [self._git_exe, *args]
        work_dir = cwd or self.repo_path
        env = self._ssh_env()

        max_attempts = self.retry_max if retry else 1
        last_result: subprocess.CompletedProcess[str] | None = None

        for attempt in range(1, max_attempts + 1):
            start = time.monotonic()
            try:
                result = subprocess.run(
                    cmd,
                    cwd=work_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except subprocess.TimeoutExpired as exc:
                elapsed = (time.monotonic() - start) * 1000
                op = GitOperation(
                    timestamp=datetime.now(),
                    cmd=cmd,
                    returncode=-1,
                    stdout="",
                    stderr=f"TimeoutExpired after {self.timeout}s",
                    duration_ms=elapsed,
                    attempt=attempt,
                )
                self.history.append(op)
                logger.warning("Timeout on attempt %d/%d: %s", attempt, max_attempts, cmd)
                if attempt < max_attempts and retry:
                    time.sleep(self.retry_delay * attempt)
                    continue
                raise GitCommandError(cmd, -1, "", str(exc)) from exc

            elapsed = (time.monotonic() - start) * 1000

            op = GitOperation(
                timestamp=datetime.now(),
                cmd=cmd,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=elapsed,
                attempt=attempt,
            )
            self.history.append(op)
            logger.debug("%s", op)

            if result.returncode == 0:
                return result

            last_result = result

            # Check if this is a retryable network error
            if attempt < max_attempts and retry and self._is_network_error(result.stderr):
                delay = self.retry_delay * attempt
                logger.warning(
                    "Network error on attempt %d/%d, retrying in %.1fs ...",
                    attempt, max_attempts, delay,
                )
                time.sleep(delay)
                continue

            # Not a network error or max attempts reached — break
            break

        # If we reach here, the command failed
        if check and last_result is not None and last_result.returncode != 0:
            raise GitCommandError(
                cmd, last_result.returncode, last_result.stdout, last_result.stderr
            )
        return last_result  # type: ignore[return-value]

    @staticmethod
    def _is_network_error(stderr: str) -> bool:
        """Check whether stderr indicates a network-related error."""
        indicators = [
            "Could not resolve hostname",
            "Connection refused",
            "Connection reset",
            "Connection timed out",
            "Network is unreachable",
            "ssh: connect to host",
            "fatal: unable to access",
            "the remote end hung up unexpectedly",
        ]
        lower = stderr.lower()
        return any(ind.lower() in lower for ind in indicators)

    # ------------------------------------------------------------------
    # Core Git Operations
    # ------------------------------------------------------------------

    def clone(
        self,
        repo_url: str,
        dest_path: str | Path | None = None,
        branch: str | None = None,
        depth: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """git clone via SSH

        Parameters
        ----------
        repo_url : str
            SSH-format repo URL, e.g. git@github.com:user/repo.git
        dest_path : str | Path | None
            Clone destination directory. Defaults to self.repo_path when None.
        branch : str | None
            Target branch (--branch).
        depth : int | None
            Shallow clone depth (--depth).
        """
        dest = Path(dest_path) if dest_path else self.repo_path
        args: list[str] = ["clone"]

        if branch:
            args += ["--branch", branch]
        if depth is not None:
            args += ["--depth", str(depth)]

        args += [repo_url, str(dest)]

        # Use dest's parent as cwd since dest doesn't exist yet
        result = self._run_git(*args, cwd=dest.parent, retry=True)
        # After successful clone, update repo_path to the actual location
        self.repo_path = dest.resolve()
        return result

    def add(self, *files: str, all_flag: bool = False) -> subprocess.CompletedProcess[str]:
        """git add

        Parameters
        ----------
        *files : str
            File paths to add. Defaults to "." (all changes).
        all_flag : bool
            Use --all (tracked + untracked + deleted).
        """
        args: list[str] = ["add"]
        if all_flag:
            args.append("--all")
        args += files if files else ["."]
        return self._run_git(*args)

    def commit(
        self,
        message: str,
        allow_empty: bool = False,
        all_tracked: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """git commit

        Parameters
        ----------
        message : str
            commit message
        allow_empty : bool
            Allow empty commit.
        all_tracked : bool
            Auto-stage all tracked modifications and deletions (-a flag).
            Does not include untracked files.
        """
        args: list[str] = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        if all_tracked:
            args.append("-a")
        return self._run_git(*args)

    def push(
        self,
        branch: str | None = None,
        force: bool = False,
        set_upstream: bool = False,
        tags: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """git push

        Parameters
        ----------
        branch : str | None
            Target branch. When None, pushes the current branch.
        force : bool
            Force push (uses --force-with-lease, safer than --force).
        set_upstream : bool
            Set upstream tracking (-u).
        tags : bool
            Also push tags (--tags).
        """
        args: list[str] = ["push"]
        if force:
            args.append("--force-with-lease")
        if set_upstream:
            args.append("-u")
        if tags:
            args.append("--tags")

        args.append(self.remote_name)
        if branch:
            args.append(branch)

        return self._run_git(*args, retry=True)

    def pull(
        self,
        branch: str | None = None,
        rebase: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """git pull

        Parameters
        ----------
        branch : str | None
            Target branch.
        rebase : bool
            Use --rebase instead of merge.
        """
        args: list[str] = ["pull"]
        if rebase:
            args.append("--rebase")
        args.append(self.remote_name)
        if branch:
            args.append(branch)
        return self._run_git(*args, retry=True)

    # ------------------------------------------------------------------
    # Debug / Utility Commands
    # ------------------------------------------------------------------

    def status(self, short: bool = False) -> str:
        """git status"""
        args = ["status"]
        if short:
            args.append("--short")
        return self._run_git(*args).stdout

    def log(
        self,
        n: int = 10,
        oneline: bool = True,
        graph: bool = False,
        all_branches: bool = False,
    ) -> str:
        """git log"""
        args: list[str] = ["log", f"-{n}"]
        if oneline:
            args.append("--oneline")
        if graph:
            args.append("--graph")
        if all_branches:
            args.append("--all")
        return self._run_git(*args).stdout

    def diff(self, staged: bool = False, file: str | None = None) -> str:
        """git diff"""
        args: list[str] = ["diff"]
        if staged:
            args.append("--staged")
        if file:
            args.append(file)
        return self._run_git(*args).stdout

    def remote_info(self, verbose: bool = True) -> str:
        """Show remote information."""
        args: list[str] = ["remote"]
        if verbose:
            args.append("-v")
        return self._run_git(*args).stdout

    def stash(self, action: str = "list", message: str | None = None) -> str:
        """git stash operations.

        Parameters
        ----------
        action : str
            Stash subcommand: "push" | "pop" | "list" | "drop" | "apply".
        message : str | None
            Message for stash push.
        """
        args: list[str] = ["stash", action]
        match action:
            case "push" if message:
                args += ["-m", message]
            case "push" | "pop" | "list" | "drop" | "apply":
                pass
            case _:
                args = ["stash", action]  # Pass through as-is
        return self._run_git(*args).stdout

    def show(self, ref: str = "HEAD", stat: bool = True) -> str:
        """git show"""
        args: list[str] = ["show", ref]
        if stat:
            args.append("--stat")
        return self._run_git(*args).stdout

    def rev_parse(self, ref: str) -> str:
        """git rev-parse — resolve a ref to its SHA hash.

        Parameters
        ----------
        ref : str
            Any valid git ref, e.g. "HEAD", "ORIG_HEAD", "main", "origin/main".

        Returns
        -------
        str
            SHA hash (stripped). Returns empty string on failure.
        """
        result = self._run_git("rev-parse", ref, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def rev_list_count(self, from_ref: str, to_ref: str) -> int:
        """Count commits between from_ref..to_ref.

        Parameters
        ----------
        from_ref : str
            Start ref (exclusive), e.g. "HEAD".
        to_ref : str
            End ref (inclusive), e.g. "@{u}".

        Returns
        -------
        int
            Commit count. Returns 0 on failure.
        """
        result = self._run_git(
            "rev-list", f"{from_ref}..{to_ref}", "--count", check=False,
        )
        count_str = result.stdout.strip()
        return int(count_str) if count_str.isdigit() else 0

    def rebase(
        self,
        target: str | None = None,
        *,
        continue_: bool = False,
        abort: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """git rebase

        Parameters
        ----------
        target : str | None
            Rebase target, e.g. "origin/main".
        continue_ : bool
            Use --continue to resume an interrupted rebase.
        abort : bool
            Use --abort to cancel the rebase.
        """
        args: list[str] = ["rebase"]
        if abort:
            args.append("--abort")
        elif continue_:
            args.append("--continue")
        elif target:
            args.append(target)
        return self._run_git(*args, check=False)

    def restore(
        self,
        filepath: str,
        source: str,
    ) -> subprocess.CompletedProcess[str]:
        """git restore — restore a file to a specific commit version (working tree only).

        Parameters
        ----------
        filepath : str
            Path of the file to restore.
        source : str
            Source commit hash or ref.
        """
        return self._run_git("restore", f"--source={source}", "--worktree", filepath)

    def fetch(self, prune: bool = False) -> subprocess.CompletedProcess[str]:
        """git fetch"""
        args: list[str] = ["fetch", self.remote_name]
        if prune:
            args.append("--prune")
        return self._run_git(*args, retry=True)

    def checkout(self, branch: str, create: bool = False) -> subprocess.CompletedProcess[str]:
        """git checkout"""
        args: list[str] = ["checkout"]
        if create:
            args.append("-b")
        args.append(branch)
        return self._run_git(*args)

    def reset(self, ref: str = "HEAD", hard: bool = False) -> subprocess.CompletedProcess[str]:
        """git reset"""
        args: list[str] = ["reset"]
        if hard:
            args.append("--hard")
        args.append(ref)
        return self._run_git(*args)

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Escape hatch: execute any arbitrary git subcommand.

        Examples
        --------
        >>> mgr.run("tag", "-a", "v1.0", "-m", "release v1.0")
        >>> mgr.run("cherry-pick", "abc123")
        """
        # Auto-detect network commands to decide whether to retry
        is_network = args[0] in self._NETWORK_COMMANDS if args else False
        return self._run_git(*args, check=check, retry=is_network)

    # ------------------------------------------------------------------
    # Operation History
    # ------------------------------------------------------------------

    def history_summary(self, last_n: int | None = None) -> str:
        """Get a summary string of the operation history."""
        records = self.history[-last_n:] if last_n else self.history
        if not records:
            return "(no operations recorded)"
        lines = [repr(op) for op in records]
        total = len(records)
        failed = sum(1 for op in records if op.returncode != 0)
        lines.append(f"\n--- Total: {total} ops, Failed: {failed} ---")
        return "\n".join(lines)

    def history_clear(self) -> None:
        """Clear the operation history."""
        self.history.clear()

    # ------------------------------------------------------------------
    # Convenience Properties
    # ------------------------------------------------------------------

    @property
    def current_branch(self) -> str:
        """Get the current branch name."""
        return self._run_git("symbolic-ref", "--short", "HEAD").stdout.strip()

    @property
    def last_commit(self) -> str:
        """Get the latest commit's short info. Returns empty string if no commits exist."""
        result = self._run_git("log", "-1", "--oneline", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def __repr__(self) -> str:
        key_name = self.ssh_key_path.name if self.ssh_key_path else "ssh_config"
        return (
            f"GitSSHManager(repo={self.repo_path}, "
            f"key={key_name}, "
            f"remote={self.remote_name})"
        )
