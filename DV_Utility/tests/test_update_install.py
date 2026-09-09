"""update_install 的單元測試: staging 解壓進位、錯誤分類、權限探測、修復引導、行程偵測。

慣例沿用 test_dv_utility_download.py: 不動 ACL, 以檔案系統手段 (同名目錄、
持開檔 handle) 模擬 Windows 鎖定/拒絕; 系統邊界 (subprocess、行程列表) 用注入替身。
"""
import ctypes
import os
import zipfile

import update_install


class _running_exe_handle:
    """模擬「執行中的 exe」的檔案語意: 不能覆寫 (os.replace 失敗) 但可以 rename。

    以 CreateFileW(SHARE_READ|SHARE_DELETE) 持開 — Windows 對執行中 image
    正是這組行為 (rename 可、in-place 覆寫不可)。
    """

    def __init__(self, path):
        self._path = str(path)

    def __enter__(self):
        GENERIC_READ, SHARE_READ_DELETE, OPEN_EXISTING = 0x80000000, 0x1 | 0x4, 3
        self._h = ctypes.windll.kernel32.CreateFileW(
            self._path, GENERIC_READ, SHARE_READ_DELETE, None, OPEN_EXISTING, 0, None)
        assert self._h != -1
        return self

    def __exit__(self, *exc):
        ctypes.windll.kernel32.CloseHandle(self._h)
        return False


def _zip_file(tmp_path, entries, name='pkg.zip'):
    p = tmp_path / name
    with zipfile.ZipFile(p, 'w') as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return str(p)


def test_install_fresh_extracts_all_and_cleans_staging(tmp_path):
    """全新安裝: zip 內容進位到 extract_dir, staging 不得殘留。"""
    zip_path = _zip_file(tmp_path, {'Tool/app.exe': b'EXE',
                                    'Tool/data/cfg.json': b'{}'})
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    staging_root = tmp_path / 'PCDV'

    result = update_install.install_zip_staged(zip_path, str(extract_dir), str(staging_root))

    assert result.ok and result.kind == 'ok'
    assert (extract_dir / 'Tool' / 'app.exe').read_bytes() == b'EXE'
    assert (extract_dir / 'Tool' / 'data' / 'cfg.json').read_bytes() == b'{}'
    # staging 不得殘留: PCDV 下只剩正式目錄
    assert set(os.listdir(staging_root)) == {'RegisterEditor'}


def test_install_corrupt_zip_leaves_existing_install_untouched(tmp_path):
    """zip 損壞: 回報 corrupt, 正式目錄的舊版一個 byte 都不得動, staging 清乾淨。"""
    bad_zip = tmp_path / 'pkg.zip'
    bad_zip.write_bytes(b'this is not a zip file')
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    (extract_dir / 'Tool').mkdir(parents=True)
    (extract_dir / 'Tool' / 'app.exe').write_bytes(b'OLD-EXE')

    result = update_install.install_zip_staged(
        str(bad_zip), str(extract_dir), str(tmp_path / 'PCDV'))

    assert not result.ok
    assert result.kind == 'corrupt'
    assert (extract_dir / 'Tool' / 'app.exe').read_bytes() == b'OLD-EXE'
    assert set(os.listdir(tmp_path / 'PCDV')) == {'RegisterEditor'}


def test_install_upgrade_overwrites_zip_files_and_keeps_user_files(tmp_path):
    """升級既有安裝: zip 內的同名檔覆寫成新版, zip 外的使用者檔案 (log/設定) 保留。"""
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    (extract_dir / 'Tool').mkdir(parents=True)
    (extract_dir / 'Tool' / 'app.exe').write_bytes(b'OLD-EXE')
    (extract_dir / 'Tool' / 'user_notes.txt').write_bytes(b'KEEP-ME')
    zip_path = _zip_file(tmp_path, {'Tool/app.exe': b'NEW-EXE'})

    result = update_install.install_zip_staged(
        zip_path, str(extract_dir), str(tmp_path / 'PCDV'))

    assert result.ok
    assert (extract_dir / 'Tool' / 'app.exe').read_bytes() == b'NEW-EXE'
    assert (extract_dir / 'Tool' / 'user_notes.txt').read_bytes() == b'KEEP-ME'
    assert set(os.listdir(tmp_path / 'PCDV')) == {'RegisterEditor'}


def test_install_reports_locked_when_target_file_held_open(tmp_path):
    """目標檔被持開 (模擬工具還在跑): 回報 locked 並指名檔案, 不得謊報成功。"""
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    (extract_dir / 'Tool').mkdir(parents=True)
    locked = extract_dir / 'Tool' / 'app.exe'
    locked.write_bytes(b'OLD-EXE')
    zip_path = _zip_file(tmp_path, {'Tool/app.exe': b'NEW-EXE'})

    with open(locked, 'rb'):
        result = update_install.install_zip_staged(
            zip_path, str(extract_dir), str(tmp_path / 'PCDV'))

    assert not result.ok
    assert result.kind == 'locked'
    assert 'app.exe' in result.detail
    assert set(os.listdir(tmp_path / 'PCDV')) == {'RegisterEditor'}


def test_install_replaces_running_exe_via_bak_rename(tmp_path):
    """執行中的 exe (不能覆寫但可 rename): 舊檔讓位成 .bak, 新版照樣進位成功。

    這是「使用者忘記關工具」的主要救援路徑 — 更新不再直接失敗。
    """
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    (extract_dir / 'Tool').mkdir(parents=True)
    exe = extract_dir / 'Tool' / 'app.exe'
    exe.write_bytes(b'OLD-EXE')
    zip_path = _zip_file(tmp_path, {'Tool/app.exe': b'NEW-EXE'})

    with _running_exe_handle(exe):
        result = update_install.install_zip_staged(
            zip_path, str(extract_dir), str(tmp_path / 'PCDV'))

    assert result.ok and result.kind == 'ok'
    assert exe.read_bytes() == b'NEW-EXE'
    assert set(os.listdir(tmp_path / 'PCDV')) == {'RegisterEditor'}


def test_install_reports_denied_when_target_dir_not_writable(tmp_path):
    """promote 失敗且目標目錄不可寫 (ACL 壞掉情境): 回報 denied — 引導修權限, 不是關工具。"""
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    (extract_dir / 'Tool').mkdir(parents=True)
    exe = extract_dir / 'Tool' / 'app.exe'
    exe.write_bytes(b'OLD-EXE')
    # 佔住 probe 檔路徑 → probe_writable 判定目錄不可寫 (慣例: 不動 ACL)
    (extract_dir / 'Tool' / f'.pcdv_write_probe.{os.getpid()}').mkdir()
    zip_path = _zip_file(tmp_path, {'Tool/app.exe': b'NEW-EXE'})

    with open(exe, 'rb'):
        result = update_install.install_zip_staged(
            zip_path, str(extract_dir), str(tmp_path / 'PCDV'))

    assert not result.ok
    assert result.kind == 'denied'


def test_repair_plan_command_resets_acl_on_pcdv():
    """免提權修復: owner 仍有隱含 WRITE_DAC, icacls /reset 可把 ACL 重設回繼承。"""
    plan = update_install.build_repair_plan(
        r'C:\Users\u\PCDV', which=lambda name: None, exists=lambda p: False)

    assert plan.command == 'icacls "C:\\Users\\u\\PCDV" /reset /t /c /q'
    assert plan.vscode_available is False


def test_repair_plan_detects_vscode_on_path():
    """PATH 上有 code CLI → 引導走 VS Code terminal。"""
    plan = update_install.build_repair_plan(
        r'C:\Users\u\PCDV',
        which=lambda name: r'C:\vsc\bin\code.CMD' if name == 'code' else None,
        exists=lambda p: False)

    assert plan.vscode_available is True


def test_procs_under_matches_only_path_boundary_case_insensitive():
    """篩出 exe 落在目錄底下的行程: 大小寫不敏感, 且須在路徑邊界 (Tool ≠ ToolPro)。"""
    procs = [
        (11, r'C:\Users\u\PCDV\RegisterEditor\Tool\app.exe'),
        (12, r'c:\users\u\pcdv\registereditor\x.exe'),
        (13, r'C:\Users\u\PCDV\RegisterEditorPro\y.exe'),
        (14, r'C:\Windows\explorer.exe'),
    ]

    hits = update_install.procs_under(r'C:\Users\u\PCDV\RegisterEditor', procs)

    assert [pid for pid, _ in hits] == [11, 12]


def test_snapshot_processes_parses_pid_and_path_skips_pathless():
    """快照解析: 只收「pid|完整路徑」行; 系統行程常無路徑, 須跳過不得炸掉。"""
    class _Out:
        stdout = '4321|C:\\T\\a.exe\n999|\n|weird\nabc|C:\\x.exe\n'

    procs = update_install.snapshot_processes(run=lambda *a, **k: _Out)

    assert procs == [(4321, 'C:\\T\\a.exe')]


def test_kill_process_tree_kills_whole_tree_forcefully():
    """關工具須連子行程一起收 (/T /F) — terminate() 漏子行程即「沒關乾淨」的來源。"""
    calls = []
    update_install.kill_process_tree(1234, run=lambda cmd, **k: calls.append(cmd))

    assert calls == [['taskkill', '/PID', '1234', '/T', '/F']]


def test_repair_plan_detects_vscode_default_install():
    """code 不在 PATH 但裝在預設位置 (Code.exe) → 一樣算有 VS Code。"""
    plan = update_install.build_repair_plan(
        r'C:\Users\u\PCDV',
        which=lambda name: None,
        exists=lambda p: p.endswith('Code.exe'))

    assert plan.vscode_available is True
