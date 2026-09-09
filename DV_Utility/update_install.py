"""安裝/更新的非 GUI 邏輯: staging 解壓進位。

zip 先完整解壓到 staging (install dir 下的暫存目錄), 成功後才搬進正式目錄 —
解壓中途失敗只丟 staging, 正式目錄的現有版本不受污染。
"""
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass

log = logging.getLogger('dv_updater')

KIND_OK = 'ok'
KIND_CORRUPT = 'corrupt'  # zip 壞 / staging 解壓失敗 (正式目錄未動, 可安心重試)
KIND_LOCKED = 'locked'    # 檔案被行程占用 (工具還在跑)
KIND_DENIED = 'denied'    # ACL 權限異常 (使用者對目錄無寫入權)
KIND_ERROR = 'error'      # 進位階段其他錯誤 (磁碟滿等)

# 每種失敗各有正確的出口 — 一律不得再要使用者刪整個資料夾
# (權限壞掉或檔案被鎖的人根本刪不掉, 該指示只會製造死路)
FAIL_MESSAGES = {
    KIND_CORRUPT: '更新失敗: 安裝檔下載損壞, 請重試更新 (現有版本未受影響)',
    KIND_LOCKED:  '更新失敗: 工具仍在使用中, 請關閉工具後重試',
    KIND_DENIED:  '更新失敗: 安裝資料夾權限異常, 請依修復指引處理 (詳見 log)',
    KIND_ERROR:   '更新失敗, 請重試 (詳見 log)',
}


@dataclass
class InstallResult:
    ok: bool
    kind: str = KIND_OK
    detail: str = ''


def snapshot_processes(run=subprocess.run):
    """列出全系統 (pid, exe 完整路徑) — tasklist 不給路徑, 用 CIM 查。

    找「上次忘記關」的工具必須全域掃, self.processes 只記得本次啟動的。
    """
    cmd = ['powershell', '-NoProfile', '-Command',
           'Get-CimInstance Win32_Process | ForEach-Object '
           '{ "$($_.ProcessId)|$($_.ExecutablePath)" }']
    out = run(cmd, capture_output=True, text=True, timeout=30)
    procs = []
    for line in (getattr(out, 'stdout', '') or '').splitlines():
        pid_s, _, path = line.partition('|')
        if pid_s.strip().isdigit() and path.strip():
            procs.append((int(pid_s.strip()), path.strip()))
    return procs


def kill_process_tree(pid, run=subprocess.run):
    """taskkill /T /F: 連子行程一起收 — terminate() 漏子行程即「沒關乾淨」的來源。"""
    run(['taskkill', '/PID', str(pid), '/T', '/F'],
        capture_output=True, text=True, timeout=30)


def procs_under(dir_path, procs):
    """篩出 exe 路徑落在 dir_path 底下的行程 — 「上次忘記關的工具」靠這個找到。

    大小寫不敏感 (NTFS), 且比對須在路徑邊界上 (Tool 不得誤中 ToolPro)。
    """
    root = os.path.normcase(os.path.normpath(dir_path))
    hits = []
    for pid, exe in procs:
        if not exe:
            continue
        p = os.path.normcase(os.path.normpath(exe))
        if p == root or p.startswith(root + os.sep):
            hits.append((pid, exe))
    return hits


@dataclass
class RepairPlan:
    """權限修復引導: 指令由使用者自己在 VS Code terminal / PowerShell 執行 —
    DV_Utility (Nuitka exe) 不代跑 icacls, 避免觸發端點防護的行為偵測。"""
    command: str
    vscode_available: bool


def _vscode_default_paths():
    return [
        os.path.join(os.environ.get('LOCALAPPDATA', ''),
                     'Programs', 'Microsoft VS Code', 'Code.exe'),
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                     'Microsoft VS Code', 'Code.exe'),
    ]


def build_repair_plan(pcdv_dir, which=shutil.which, exists=os.path.exists):
    # /reset: 把 ACL 重設回從 home 繼承 (預設給本人 Full Control)。
    # 只要使用者還是 owner, 隱含 WRITE_DAC 讓這步免提權即可成功
    command = f'icacls "{pcdv_dir}" /reset /t /c /q'
    vscode = (which('code') is not None
              or any(exists(p) for p in _vscode_default_paths()))
    return RepairPlan(command, vscode_available=vscode)


def probe_writable(dir_path):
    """實際建立/刪除測試檔驗證目錄可寫 — os.access 對 Windows ACL 不可靠。"""
    probe = os.path.join(dir_path, f'.pcdv_write_probe.{os.getpid()}')
    try:
        os.makedirs(dir_path, exist_ok=True)
        with open(probe, 'w'):
            pass
        os.remove(probe)
        return True
    except OSError:
        log.warning('probe_writable: %s 不可寫', dir_path, exc_info=True)
        return False


def _promote(src, dst):
    """staging 內容進位到正式位置。

    dst 不存在 → 整個 move (同磁碟為 rename, 全新目錄一步到位, 不會留半套);
    dst 為既有目錄 → 逐項遞迴合併 — zip 外的使用者檔案須保留 (extractall 語意);
    dst 為既有檔案 → 覆寫。
    """
    if not os.path.lexists(dst):
        shutil.move(src, dst)
        return
    if os.path.isdir(dst) and os.path.isdir(src):
        for name in os.listdir(src):
            _promote(os.path.join(src, name), os.path.join(dst, name))
        return
    try:
        os.replace(src, dst)
    except PermissionError:
        # 執行中的 exe 不能覆寫但可以 rename — 舊檔讓位成 .bak 再放新檔,
        # 「使用者忘記關工具」因此不再直接失敗。rename 也失敗才交外層分類。
        bak = dst + '.bak'
        _remove_bak(bak)
        os.rename(dst, bak)
        shutil.move(src, dst)
        _remove_bak(bak)   # 真正執行中的 exe 刪不掉 → 留著, 下次更新再清


def _remove_bak(path):
    """best-effort 清 .bak; 失敗只記 log (執行中的舊 exe 本來就刪不掉)。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError:
        log.warning('could not remove bak %s', path, exc_info=True)


def install_zip_staged(zip_path, extract_dir, staging_root):
    os.makedirs(staging_root, exist_ok=True)
    staging = tempfile.mkdtemp(prefix='.staging.', dir=staging_root)
    try:
        # 解壓階段任何失敗都只影響 staging, 正式目錄還沒被碰 → corrupt (可安心重試)
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(staging)
        except Exception:
            log.exception('install_zip_staged: extract to staging failed (%s)', zip_path)
            return InstallResult(False, KIND_CORRUPT, zip_path)

        try:
            os.makedirs(extract_dir, exist_ok=True)
            for name in os.listdir(staging):
                _promote(os.path.join(staging, name), os.path.join(extract_dir, name))
        except PermissionError as exc:
            # Windows 上「檔案被行程持開」與「ACL 拒絕」同樣回 WinError 5,
            # 無法靠錯誤碼區分 → 探測目標目錄可寫性: 目錄可寫 = ACL 正常 = 鎖檔
            dst = exc.filename2 or exc.filename or extract_dir
            kind = (KIND_LOCKED
                    if probe_writable(os.path.dirname(dst) or extract_dir)
                    else KIND_DENIED)
            log.exception('install_zip_staged: promote failed (kind=%s dst=%s)', kind, dst)
            return InstallResult(False, kind, dst)
        except OSError as exc:
            log.exception('install_zip_staged: promote failed (dst=%s)', exc.filename)
            return InstallResult(False, KIND_ERROR, exc.filename or '')
        return InstallResult(True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
