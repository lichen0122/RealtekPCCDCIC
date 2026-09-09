"""dv_utility.download_file / 設定載入的回歸測試。

背景: 使用者回報 PermissionError [Errno 13] 於 download_file 的 open() —
zip 以裸檔名寫入行程 CWD, 而 CWD 取決於啟動方式 (捷徑「開始位置」、唯讀共享等),
部分使用者的 CWD 不可寫。以下測試以「CWD 放一個與 zip 同名的目錄」模擬
該檔名在 CWD 不可建立的情境 (Windows 對此同樣回 Errno 13), 不需動 ACL。
"""

import io
import json
import os
import zipfile

import dv_utility


class _FakeResponse:
    """requests.get(stream=True) 的最小替身。"""

    def __init__(self, payload):
        self._payload = payload
        self.headers = {'content-length': str(len(payload))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _bare_inst():
    """建立不跑 __init__ 的 AutoUpdateGUI (未建 C++ 物件, 不需 QApplication)。"""
    return dv_utility.AutoUpdateGUI.__new__(dv_utility.AutoUpdateGUI)


def _make_inst(monkeypatch, payloads, extract_info, stage_dir):
    """建立不跑 __init__ 的 AutoUpdateGUI, 只掛 download_file 用到的屬性。

    stage_dir 攔截 get_install_dir(): 下載暫存檔一律落在這裡, 不得碰真正的 ~/PCDV。
    """
    inst = _bare_inst()
    inst.signals = dv_utility.WorkerSignals()
    inst.extract_info = extract_info
    inst.update_required = True
    inst.newest_version_info = {'version': '9.9.9'}
    stage_dir.mkdir(parents=True, exist_ok=True)
    inst.get_install_dir = lambda: str(stage_dir)

    statuses = []
    inst.signals.update_status.connect(statuses.append)
    versions_written = []
    inst.set_current_version = versions_written.append
    inst.start = lambda: None

    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda url, stream=True, timeout=None: _FakeResponse(payloads[url]))
    return inst, statuses, versions_written


ZIP_NAME = 'RegisterEditor_cpu_2.3.0.zip'
URL = f'https://example.invalid/version_ctrl/{ZIP_NAME}'


def test_download_file_succeeds_when_cwd_rejects_zip_name(monkeypatch, tmp_path):
    """CWD 無法建立該 zip 檔名時, 下載安裝仍須成功 (zip 不得寫入 CWD)。"""
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    (cwd / ZIP_NAME).mkdir()          # 同名目錄: 在 CWD open(ZIP_NAME,'wb') 會 Errno 13
    monkeypatch.chdir(cwd)

    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'   # 尚不存在: 一併驗證自動建目錄
    stage = tmp_path / 'stage'
    payload = _zip_bytes({'app/tool.txt': b'TOOL-BYTES'})
    inst, statuses, versions = _make_inst(
        monkeypatch, {URL: payload}, [(URL, str(extract_dir), True)], stage)

    inst.download_file()

    assert statuses[-1] == '安裝完成'
    assert (extract_dir / 'app' / 'tool.txt').read_bytes() == b'TOOL-BYTES'
    assert versions == [{'version': '9.9.9'}]
    # CWD 只剩原本的同名目錄, 沒有任何新檔案
    assert set(os.listdir(cwd)) == {ZIP_NAME}
    # 暫存 zip 已清除 (不論暫存在哪, 兩處都不得殘留)
    assert not (extract_dir / ZIP_NAME).exists()
    assert list(stage.iterdir()) == []


def test_download_file_permission_error_not_reported_as_network(monkeypatch, tmp_path):
    """寫檔權限問題須明講, 不得誤導成「請確認網路連線」。"""
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    stage = tmp_path / 'stage'
    payload = _zip_bytes({'app/tool.txt': b'TOOL-BYTES'})
    inst, statuses, versions = _make_inst(
        monkeypatch, {URL: payload}, [(URL, str(extract_dir), True)], stage)
    # 佔住暫存檔路徑的同名目錄: open() 會撞出 PermissionError
    (stage / f'{ZIP_NAME}.{os.getpid()}.part').mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == []
    assert '權限' in statuses[-1]
    assert '網路' not in statuses[-1]


def test_download_file_partial_extract_failure_not_masked(monkeypatch, tmp_path):
    """多個項目時, 前面解壓失敗不得被後面的成功蓋掉 (不得寫入版本檔)。

    zip 損壞走 staging, 舊版未受污染 → 訊息須引導「重試」,
    不得再要使用者刪整個資料夾 (權限壞掉的使用者根本刪不掉)。
    """
    url_bad = 'https://example.invalid/version_ctrl/ToolA_1.0.zip'
    url_ok = 'https://example.invalid/version_ctrl/ToolB_1.0.zip'
    dir_a = tmp_path / 'A'
    dir_a.mkdir()
    (dir_a / 'old.txt').write_bytes(b'OLD')          # 既有安裝
    dir_b = tmp_path / 'B'
    payloads = {
        url_bad: b'this is not a zip file',
        url_ok: _zip_bytes({'b.txt': b'B'}),
    }
    inst, statuses, versions = _make_inst(
        monkeypatch, payloads,
        [(url_bad, str(dir_a), True), (url_ok, str(dir_b), True)],
        tmp_path / 'stage')
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == []
    assert '損壞' in statuses[-1] and '重試' in statuses[-1]
    assert '刪除' not in statuses[-1]
    assert (dir_a / 'old.txt').read_bytes() == b'OLD'   # 舊版一個 byte 都沒動


def test_download_file_locked_tool_reports_close_and_retry(monkeypatch, tmp_path):
    """工具檔案被持開 (忘記關工具): 訊息須指向「關閉工具後重試」, 不得誤導刪資料夾/查網路。"""
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    extract_dir.mkdir(parents=True)
    exe = extract_dir / 'tool.exe'
    exe.write_bytes(b'OLD')
    payload = _zip_bytes({'tool.exe': b'NEW'})
    inst, statuses, versions = _make_inst(
        monkeypatch, {URL: payload}, [(URL, str(extract_dir), True)], tmp_path / 'stage')
    monkeypatch.chdir(tmp_path)

    with open(exe, 'rb'):
        inst.download_file()

    assert versions == []
    assert '關閉' in statuses[-1]
    assert '刪除' not in statuses[-1] and '網路' not in statuses[-1]


def test_download_file_preflight_blocks_when_install_dir_not_writable(monkeypatch, tmp_path):
    """PCDV 權限異常 (ACL 被改壞): 下載前就要攔下並給權限訊息, 不得動手下載。"""
    stage = tmp_path / 'stage'
    extract_dir = tmp_path / 'PCDV' / 'Tool'
    inst, statuses, versions = _make_inst(
        monkeypatch, {}, [(URL, str(extract_dir), True)], stage)   # 一旦下載必 KeyError
    # 佔住 probe 檔路徑 → probe_writable 判定 install dir 不可寫 (慣例: 不動 ACL)
    (stage / f'.pcdv_write_probe.{os.getpid()}').mkdir()
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == []
    assert '權限' in statuses[-1]
    assert not extract_dir.exists()
    assert inst._install_busy is False


def test_download_file_denied_emits_repair_guidance(monkeypatch, tmp_path):
    """權限異常時須同時發出修復引導 (icacls 指令 + 有無 VS Code), 供 GUI 顯示指引。"""
    stage = tmp_path / 'stage'
    inst, statuses, versions = _make_inst(
        monkeypatch, {}, [(URL, str(tmp_path / 'PCDV' / 'T'), True)], stage)
    (stage / f'.pcdv_write_probe.{os.getpid()}').mkdir()
    repairs = []
    inst.signals.show_repair.connect(lambda cmd, vscode: repairs.append((cmd, vscode)))
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert len(repairs) == 1
    cmd, vscode = repairs[0]
    assert 'icacls' in cmd and str(stage) in cmd and '/reset' in cmd
    assert isinstance(vscode, bool)


def test_download_file_blocks_when_tool_still_running(monkeypatch, tmp_path):
    """既有安裝的工具還在跑 (上次忘記關): 下載前就攔下, 引導關閉 — 不得動手覆蓋。"""
    target_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    target_dir.mkdir(parents=True)
    exe = target_dir / 'tool.exe'
    exe.write_bytes(b'OLD')
    inst, statuses, versions = _make_inst(
        monkeypatch, {}, [(URL, str(target_dir), True)], tmp_path / 'stage')
    inst.target_directory = str(target_dir)
    monkeypatch.setattr(dv_utility.update_install, 'snapshot_processes',
                        lambda: [(4321, str(exe)), (99, r'C:\Windows\explorer.exe')])

    inst.download_file()

    assert versions == []
    assert '執行中' in statuses[-1] and '關閉' in statuses[-1]
    assert exe.read_bytes() == b'OLD'
    assert inst._install_busy is False


def test_download_file_fresh_install_skips_process_scan(monkeypatch, tmp_path):
    """全新安裝 (目錄不存在): 不得掃行程 — 沒東西可覆蓋, 掃描只是白跑 PowerShell。"""
    extract_dir = tmp_path / 'PCDV' / 'RegisterEditor'
    payload = _zip_bytes({'tool.exe': b'NEW'})
    inst, statuses, versions = _make_inst(
        monkeypatch, {URL: payload}, [(URL, str(extract_dir), True)], tmp_path / 'stage')
    inst.target_directory = str(extract_dir)

    def _boom():
        raise AssertionError('snapshot_processes must not run on fresh install')
    monkeypatch.setattr(dv_utility.update_install, 'snapshot_processes', _boom)
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert statuses[-1] == '安裝完成'


def test_download_file_preserves_same_named_user_file_in_work_dir(monkeypatch, tmp_path):
    """work_dir 項目: 使用者專案目錄裡的同名檔案 (如 input.zip) 不得被暫存檔截斷/刪除。

    正式 manifest (如 RomCodeConvertion / RalfAutoGen) 都有
    {url: .../input.zip, extract_to: work_dir, overwrite: false} 項目,
    暫存檔絕不可落在 work_dir。
    """
    work_dir = tmp_path / 'project'
    work_dir.mkdir()
    (work_dir / 'input.zip').write_bytes(b'USER-DATA')   # 使用者自己的檔案
    url = 'https://example.invalid/RomCodeConvertion/input.zip'
    payload = _zip_bytes({'input/a.txt': b'A'})
    inst, statuses, versions = _make_inst(
        monkeypatch, {url: payload}, [(url, str(work_dir), False)], tmp_path / 'stage')
    monkeypatch.chdir(tmp_path)

    # overwrite=False 且 work_dir/input 不存在 -> 會下載並解壓
    inst.download_file()

    assert statuses[-1] == '安裝完成'
    assert (work_dir / 'input' / 'a.txt').read_bytes() == b'A'
    assert (work_dir / 'input.zip').read_bytes() == b'USER-DATA'


def test_download_file_removes_partial_zip_on_download_error(monkeypatch, tmp_path):
    """下載中斷不得留下半截 zip (暫存區與目的地都不得殘留)。"""
    stage = tmp_path / 'stage'
    extract_dir = tmp_path / 'PCDV' / 'Tool'
    inst, statuses, versions = _make_inst(
        monkeypatch, {}, [(URL, str(extract_dir), True)], stage)

    class _Boom(_FakeResponse):
        def iter_content(self, chunk_size=8192):
            yield self._payload[:8]
            raise OSError('connection dropped')

    monkeypatch.setattr(dv_utility.requests, 'get',
                        lambda url, stream=True, timeout=None: _Boom(b'x' * 64))
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert versions == []
    assert list(stage.iterdir()) == []
    assert not extract_dir.exists() or not any(extract_dir.iterdir())


def test_download_file_marks_install_busy_during_run(monkeypatch, tmp_path):
    """安裝進行中須設 _install_busy (closeEvent 關窗警告依據), 結束後須還原。"""
    extract_dir = tmp_path / 'PCDV' / 'Tool'
    payload = _zip_bytes({'a.txt': b'A'})
    inst, statuses, versions = _make_inst(
        monkeypatch, {URL: payload}, [(URL, str(extract_dir), True)], tmp_path / 'stage')
    seen = []
    real_extract = inst.extract_zip
    inst.extract_zip = lambda z, d: (seen.append(inst._install_busy), real_extract(z, d))[1]
    monkeypatch.chdir(tmp_path)

    inst.download_file()

    assert seen == [True]
    assert inst._install_busy is False


def test_check_for_update_starts_daemon_download_thread(monkeypatch):
    """下載執行緒須為 daemon: 使用者關窗後行程不得殘留等下載跑完。"""
    inst = _bare_inst()
    inst.signals = dv_utility.WorkerSignals()
    inst.get_newest_version = lambda: None
    inst.get_current_version = lambda: None
    inst.get_extract_info = lambda: None
    inst.current_version_info = {'version': 'x'}
    inst.newest_version_info = {'version': 'x'}

    created = {}
    real_thread = dv_utility.threading.Thread

    class RecordingThread(real_thread):
        def __init__(self, *args, **kwargs):
            created.update(kwargs)
            super().__init__(*args, **kwargs)

        def start(self):
            pass

    monkeypatch.setattr(dv_utility.threading, 'Thread', RecordingThread)
    inst.check_for_update()

    assert created.get('target') == inst.download_file
    assert created.get('daemon') is True


def test_load_setting_reads_utf8(tmp_path):
    """setting.json 由 GitHub 以 UTF-8 bytes 下載, 讀取不得依賴系統編碼 (cp950)。"""
    inst = _bare_inst()
    setting_file = tmp_path / 'setting.json'
    setting_file.write_bytes(
        json.dumps({'暫存器編輯器': 'https://x/version.json'},
                   ensure_ascii=False).encode('utf-8'))
    inst.setting_file = str(setting_file)

    setting = inst.load_setting()

    assert setting == {'暫存器編輯器': 'https://x/version.json'}
