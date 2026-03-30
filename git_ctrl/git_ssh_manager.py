"""
GitSSHManager - 透過 SSH 管理 Git Remote 的工具類別

Features:
    - 所有操作透過 subprocess 直接呼叫 git CLI，不依賴第三方套件
    - SSH key path 可自訂，透過 GIT_SSH_COMMAND 注入
    - 網路操作自動 retry（push / pull / clone / fetch）
    - 完整操作歷史紀錄，方便 debug
    - 支援 Context Manager（with 語法）

Requires: Python 3.10+, git CLI installed
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
    """git 指令執行失敗時拋出的例外"""

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
    """單筆操作紀錄"""
    timestamp: datetime
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    attempt: int = 1  # 第幾次嘗試成功（retry 相關）

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
    """透過 SSH 管理 Git Remote 的核心類別

    Parameters
    ----------
    repo_path : str | Path
        本地 git repo 的路徑（clone 時可先指向預定目標）
    ssh_key_path : str | Path | None
        SSH private key 的路徑。None 時不帶 -i，由 ~/.ssh/config 決定
    remote_name : str
        預設的 remote 名稱
    ssh_options : dict[str, str] | None
        額外的 SSH 參數，例如 {"StrictHostKeyChecking": "no"}
    retry_max : int
        網路操作最大重試次數
    retry_delay : float
        重試間隔（秒），每次 retry 會倍增（exponential backoff）
    timeout : int | None
        單次 git 指令的 timeout（秒）
    """

    # 需要與 remote 互動、可能因網路失敗的子命令
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

        # 確保 repo_path 的父資料夾存在（clone 時會用到）
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

        # 驗證 SSH key（有指定才檢查）
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
        return False  # 不吞例外

    # ------------------------------------------------------------------
    # 內部工具
    # ------------------------------------------------------------------

    def _ssh_env(self) -> dict[str, str]:
        """組合包含 GIT_SSH_COMMAND 的環境變數

        若未指定 ssh_key_path，則不帶 -i，SSH 會自動使用 ~/.ssh/config 的設定。
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
        """統一的 git 指令執行入口

        Parameters
        ----------
        *args : str
            git 子命令與參數，例如 ("status", "--short")
        cwd : Path | None
            工作目錄，預設為 self.repo_path
        check : bool
            失敗時是否拋出 GitCommandError
        retry : bool
            是否啟用自動重試（僅對網路命令有效）

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

            # 判斷是否為可重試的網路錯誤
            if attempt < max_attempts and retry and self._is_network_error(result.stderr):
                delay = self.retry_delay * attempt
                logger.warning(
                    "Network error on attempt %d/%d, retrying in %.1fs ...",
                    attempt, max_attempts, delay,
                )
                time.sleep(delay)
                continue

            # 非網路錯誤或已達最大次數，直接 break
            break

        # 如果走到這裡代表失敗
        if check and last_result is not None and last_result.returncode != 0:
            raise GitCommandError(
                cmd, last_result.returncode, last_result.stdout, last_result.stderr
            )
        return last_result  # type: ignore[return-value]

    @staticmethod
    def _is_network_error(stderr: str) -> bool:
        """判斷 stderr 是否為網路相關錯誤"""
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
    # 核心 Git 操作
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
            SSH 格式的 repo URL，例如 git@github.com:user/repo.git
        dest_path : str | Path | None
            clone 目標資料夾，None 時使用 self.repo_path
        branch : str | None
            指定 branch（--branch）
        depth : int | None
            shallow clone 深度（--depth）
        """
        dest = Path(dest_path) if dest_path else self.repo_path
        args: list[str] = ["clone"]

        if branch:
            args += ["--branch", branch]
        if depth is not None:
            args += ["--depth", str(depth)]

        args += [repo_url, str(dest)]

        # clone 的 cwd 用 dest 的 parent（dest 還不存在）
        result = self._run_git(*args, cwd=dest.parent, retry=True)
        # clone 成功後，將 repo_path 指向實際位置
        self.repo_path = dest.resolve()
        return result

    def add(self, *files: str, all_flag: bool = False) -> subprocess.CompletedProcess[str]:
        """git add

        Parameters
        ----------
        *files : str
            要 add 的檔案路徑，預設為 "."（所有變更）
        all_flag : bool
            是否使用 --all（追蹤 + 未追蹤 + 刪除）
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
            允許空 commit
        all_tracked : bool
            自動 stage 所有已追蹤的修改與刪除（等同 -a），但不包含 untracked 檔案
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
            目標 branch，None 則 push 當前 branch
        force : bool
            強制推送（--force-with-lease，比 --force 安全）
        set_upstream : bool
            設定 upstream（-u）
        tags : bool
            一併推送 tags（--tags）
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
            目標 branch
        rebase : bool
            使用 --rebase 而非 merge
        """
        args: list[str] = ["pull"]
        if rebase:
            args.append("--rebase")
        args.append(self.remote_name)
        if branch:
            args.append(branch)
        return self._run_git(*args, retry=True)

    # ------------------------------------------------------------------
    # Debug / 輔助指令
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
        """顯示 remote 資訊"""
        args: list[str] = ["remote"]
        if verbose:
            args.append("-v")
        return self._run_git(*args).stdout

    def stash(self, action: str = "list", message: str | None = None) -> str:
        """git stash 操作

        Parameters
        ----------
        action : str
            stash 子命令："push" | "pop" | "list" | "drop" | "apply"
        message : str | None
            stash push 時的 message
        """
        args: list[str] = ["stash", action]
        match action:
            case "push" if message:
                args += ["-m", message]
            case "push" | "pop" | "list" | "drop" | "apply":
                pass
            case _:
                args = ["stash", action]  # 直接透傳
        return self._run_git(*args).stdout

    def show(self, ref: str = "HEAD", stat: bool = True) -> str:
        """git show"""
        args: list[str] = ["show", ref]
        if stat:
            args.append("--stat")
        return self._run_git(*args).stdout

    def rev_parse(self, ref: str) -> str:
        """git rev-parse — 取得 ref 對應的 SHA

        Parameters
        ----------
        ref : str
            任何合法的 git ref，例如 "HEAD", "ORIG_HEAD", "main", "origin/main"

        Returns
        -------
        str
            SHA hash（已 strip），失敗時回傳空字串
        """
        result = self._run_git("rev-parse", ref, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def rev_list_count(self, from_ref: str, to_ref: str) -> int:
        """計算 from_ref..to_ref 之間的 commit 數量

        Parameters
        ----------
        from_ref : str
            起始 ref（不含），例如 "HEAD"
        to_ref : str
            結束 ref（含），例如 "@{u}"

        Returns
        -------
        int
            commit 數量，失敗時回傳 0
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
            rebase 目標，例如 "origin/main"
        continue_ : bool
            使用 --continue 繼續中斷的 rebase
        abort : bool
            使用 --abort 取消 rebase
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
        """git restore — 將檔案還原到指定 commit 版本（working tree only）

        Parameters
        ----------
        filepath : str
            要還原的檔案路徑
        source : str
            來源 commit hash 或 ref
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
        """萬用逃生口：直接執行任意 git 子命令

        Examples
        --------
        >>> mgr.run("tag", "-a", "v1.0", "-m", "release v1.0")
        >>> mgr.run("cherry-pick", "abc123")
        """
        # 自動判斷是否為網路命令以決定 retry
        is_network = args[0] in self._NETWORK_COMMANDS if args else False
        return self._run_git(*args, check=check, retry=is_network)

    # ------------------------------------------------------------------
    # 操作歷史
    # ------------------------------------------------------------------

    def history_summary(self, last_n: int | None = None) -> str:
        """取得操作歷史的摘要字串"""
        records = self.history[-last_n:] if last_n else self.history
        if not records:
            return "(no operations recorded)"
        lines = [repr(op) for op in records]
        total = len(records)
        failed = sum(1 for op in records if op.returncode != 0)
        lines.append(f"\n--- Total: {total} ops, Failed: {failed} ---")
        return "\n".join(lines)

    def history_clear(self) -> None:
        """清除操作歷史"""
        self.history.clear()

    # ------------------------------------------------------------------
    # 便利屬性
    # ------------------------------------------------------------------

    @property
    def current_branch(self) -> str:
        """取得目前的 branch 名稱（支援空 repo / 尚無 commit 的情況）"""
        return self._run_git("symbolic-ref", "--short", "HEAD").stdout.strip()

    @property
    def last_commit(self) -> str:
        """取得最新一筆 commit 的 short info，若尚無 commit 則回傳空字串"""
        result = self._run_git("log", "-1", "--oneline", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def __repr__(self) -> str:
        key_name = self.ssh_key_path.name if self.ssh_key_path else "ssh_config"
        return (
            f"GitSSHManager(repo={self.repo_path}, "
            f"key={key_name}, "
            f"remote={self.remote_name})"
        )
