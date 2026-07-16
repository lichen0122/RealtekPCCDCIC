# DV_Utility 半自動一鍵更新 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 DV_Utility 恢復「一鍵更新並重啟」，但把會被 OPSWAT/防毒判毒的下載-換裝-重啟行為隔離到一顆獨立、極少改版、可單獨簽章的 `dv_updater.exe`，使主程式每版都保持乾淨、能通過 CTC 簽章掃毒。

**Architecture:** 兩顆 exe ＋ 一份 GCS manifest。主程式 `DV_Utility.exe` 只做「讀 version.json（純 JSON）→ 顯示更新按鈕 → 啟動 `dv_updater.exe` → 自己退出」。更新器 `dv_updater.exe` 負責下載新版 zip → 驗 sha256 → 解壓 → 待主程式退出後就地替換（舊檔改 `.bak`）→ 重啟新版。採「就地替換 by helper」佈局，單一可攜 exe、路徑/捷徑不變。更新器採階梯式：先做 A（Nuitka 溫和版）送 OPSWAT 當關卡，沒過升 B（Go 原生，附錄 B），保底 C（通知式，附錄 C）。

**Tech Stack:** Python 3.12、PySide6（主程式 GUI）、requests、Nuitka onefile（打包）、tkinter（更新器極簡狀態視窗）、pytest（測試）。

**Spec:** `DV_Utility/docs/superpowers/specs/2026-07-16-dv-utility-semi-auto-update-design.md`

---

## 前置：測試環境備註

- 全程於 `DV_Utility/` 目錄、用 `venv/Scripts/python` 執行。
- 本機 pytest 預設暫存根目錄被防毒封鎖，跑測試一律帶 `PYTEST_DEBUG_TEMPROOT`：

```bash
cd "D:/lichen.liu/Documents/GitHub/RealtekPCCDCIC/DV_Utility"
venv/Scripts/python -m pip install pytest >/dev/null 2>&1
mkdir -p .pytest_tmp
PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/ -v
```

- `DV_Utility/` 被 `.gitignore` 忽略，新增檔案要 `git add -f`（沿用 repo 既有慣例）。

## File Structure

- Create: `DV_Utility/update_check.py` — 主程式用的「版本比對 + 讀 GCS manifest」純/網路邏輯（不含 GUI，好測試，會被 Nuitka 依 import 一起打包進主程式）。
- Modify: `DV_Utility/dv_utility.py` — 接線 update_check、加更新橫幅/按鈕、按鈕啟動更新器後退出。
- Create: `DV_Utility/dv_updater.py` — 獨立更新器（下載/驗證/解壓/替換/重啟）。
- Modify: `DV_Utility/release_dv_utility.py` — 多建一顆 `dv_updater.exe`、重新產生 GCS manifest（`publish_version.json`，含 `release_note`）、打包兩顆 exe 進 zip。
- Modify: `DV_Utility/upload_to_gcs.py` — 重新上傳 manifest 到 GCS `DVUtility/version.json`。
- Create: `DV_Utility/tests/test_update_check.py` — 版本比對 + manifest 讀取測試。
- Create: `DV_Utility/tests/test_updater.py` — 下載/解壓/替換回滾測試。

---

## Task 1: `update_check.py` — 版本比對純函式

**Files:**
- Create: `DV_Utility/update_check.py`
- Test: `DV_Utility/tests/test_update_check.py`

- [ ] **Step 1: 寫失敗測試**

`DV_Utility/tests/test_update_check.py`:

```python
import update_check as uc


def test_version_key_parses_date_and_build():
    assert uc._version_key('v20260716') == (2026, 7, 16, 0)
    assert uc._version_key('v20260716.3') == (2026, 7, 16, 3)
    assert uc._version_key('20260716.3') == (2026, 7, 16, 3)   # 無 v 前綴亦可
    assert uc._version_key('garbage') == (0, 0, 0, 0)


def test_is_newer():
    assert uc.is_newer('v20260716.2', 'v20260716.1') is True    # 流水號較新
    assert uc.is_newer('v20260717', 'v20260716') is True         # 日期較新
    assert uc.is_newer('v20260716.10', 'v20260716.2') is True    # .10 > .2 (非字串序)
    assert uc.is_newer('v20260716', 'v20260716') is False        # 相等
    assert uc.is_newer('v20260716.1', 'v20260716.2') is False    # 較舊
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_update_check.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'update_check'`

- [ ] **Step 3: 寫最小實作**

`DV_Utility/update_check.py`:

```python
"""DV_Utility 更新檢查 (供主程式使用)。

只做「純 JSON 讀取 + 版本比對」——沒有下載 PE / 換檔 / 執行子程序等會被防毒判毒的行為。
真正的下載換裝重啟由獨立的 dv_updater.exe 負責。
"""

import re

import requests

# 發佈端 (release_dv_utility.py + upload_to_gcs.py) 上傳到 GCS 的 manifest。
VERSION_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/DVUtility/version.json'


def _version_key(v):
    """vYYYYMMDD[.N] -> (Y, M, D, N) tuple, 供大小比較 (避免字串序把 .10 判成 < .2)。"""
    m = re.match(r'v?(\d{4})(\d{2})(\d{2})(?:\.(\d+))?$', str(v))
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0)) if m else (0, 0, 0, 0)


def is_newer(latest, current):
    """latest 是否比 current 新 (皆為 vYYYYMMDD[.N])。"""
    return _version_key(latest) > _version_key(current)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_update_check.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add -f update_check.py tests/test_update_check.py
git commit -m "feat(dv): add update_check version-compare helpers"
```

---

## Task 2: `update_check.check_latest_version` — 讀 GCS manifest

**Files:**
- Modify: `DV_Utility/update_check.py`
- Test: `DV_Utility/tests/test_update_check.py`

- [ ] **Step 1: 寫失敗測試（附在現有測試檔）**

追加到 `DV_Utility/tests/test_update_check.py`:

```python
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_check_latest_version_returns_info_when_newer(monkeypatch):
    payload = {'version': 'v20260717', 'zip_url': 'https://x/DV_Utility.zip',
               'sha256': 'abc', 'size': 1, 'release_note': '修了一些東西'}
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    monkeypatch.setattr(uc.requests, 'get', lambda *a, **k: _FakeResp(payload))
    info = uc.check_latest_version('v20260716')
    assert info == payload


def test_check_latest_version_none_when_not_newer(monkeypatch):
    payload = {'version': 'v20260716'}
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    monkeypatch.setattr(uc.requests, 'get', lambda *a, **k: _FakeResp(payload))
    assert uc.check_latest_version('v20260716') is None


def test_check_latest_version_none_when_not_packaged(monkeypatch):
    monkeypatch.setattr(uc, '_is_packaged', lambda: False)
    assert uc.check_latest_version('v20260716') is None


def test_check_latest_version_none_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('network down')
    monkeypatch.setattr(uc, '_is_packaged', lambda: True)
    monkeypatch.setattr(uc.requests, 'get', boom)
    assert uc.check_latest_version('v20260716') is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_update_check.py -v`
Expected: FAIL — `AttributeError: module 'update_check' has no attribute '_is_packaged'` / `check_latest_version`

- [ ] **Step 3: 寫最小實作（追加到 `update_check.py`）**

```python
def _is_packaged():
    """是否為 Nuitka 打包後執行 (非 `python dv_utility.py` 開發模式)。"""
    return globals().get('__compiled__') is not None


def check_latest_version(current_version, timeout=10):
    """GET version.json 比對目前版本; 有更新回 info dict, 否則 None。

    info = {"version","zip_url","sha256","size","release_note"}
    非打包 / 任何網路或解析錯誤 (含 manifest 尚未發佈的 404) -> None。
    """
    if not _is_packaged():
        return None
    try:
        resp = requests.get(VERSION_URL, timeout=timeout)
        resp.raise_for_status()
        info = resp.json()
    except Exception:
        return None
    latest = info.get('version', '')
    if latest and is_newer(latest, current_version):
        return info
    return None
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_update_check.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add -f update_check.py tests/test_update_check.py
git commit -m "feat(dv): read GCS manifest in update_check.check_latest_version"
```

---

## Task 3: 主程式接線更新檢查（背景 thread + 訊號）

**Files:**
- Modify: `DV_Utility/dv_utility.py`（import 區 line 1-18；`WorkerSignals` line 153-160；`__init__` line 189-190；`connect_signals` line 571-579）

- [ ] **Step 1: 加 import**

在 `DV_Utility/dv_utility.py` 的 `from logging.handlers import RotatingFileHandler`（line 18）之後加一行：

```python
from logging.handlers import RotatingFileHandler
import update_check
```

- [ ] **Step 2: `WorkerSignals` 加訊號**

把 `WorkerSignals`（line 153-160）的最後一個欄位後補上更新訊號：

```python
    process_added      = Signal(object)   # 攜帶新 process 紀錄, 於 GUI thread append
    update_available   = Signal(object)   # 更新檢查: 發現新版 (攜帶 manifest info dict)
```

- [ ] **Step 3: `__init__` 啟動背景檢查**

把 `__init__`（line 189-190）的：

```python
        self.init_window()
        self.connect_signals()
```

改成：

```python
        self.init_window()
        self.connect_signals()

        # 背景檢查是否有新版 (僅打包執行; 純 JSON 讀取, 無下載/執行行為); 有更新則通知 GUI
        self._update_info = None
        threading.Thread(target=self._check_update, daemon=True).start()
```

- [ ] **Step 4: `connect_signals` 連接訊號**

把 `connect_signals`（line 578-579）的：

```python
        # queued connection (worker thread -> GUI thread): append + 刷新都在 GUI thread
        self.signals.process_added.connect(self._add_process)
```

改成：

```python
        # queued connection (worker thread -> GUI thread): append + 刷新都在 GUI thread
        self.signals.process_added.connect(self._add_process)
        # 更新檢查 (worker thread -> GUI thread)
        self.signals.update_available.connect(self._slot_update_available)
```

- [ ] **Step 5: 加 `_check_update` 方法**

在 `_slot_show_status_frame`（line 592-594）之後、`Hotkey` 分隔線（line 596）之前插入：

```python
    # ------------------------------------------------------------------ #
    #  Update check (背景 thread) —— 只讀 JSON, 不下載/不執行
    # ------------------------------------------------------------------ #
    def _check_update(self):
        """背景執行緒: 比對 GCS 最新版本, 有新版就通知 GUI thread 顯示更新按鈕。"""
        try:
            info = update_check.check_latest_version(self.version)
            if info:
                log.info('update-check: new version available: %s', info.get('version'))
                self.signals.update_available.emit(info)
            else:
                log.info('update-check: no update (or not packaged)')
        except Exception:
            log.exception('update-check failed')
```

- [ ] **Step 6: 語法編譯確認**

Run: `venv/Scripts/python -m py_compile dv_utility.py update_check.py`
Expected: 無輸出（成功）

- [ ] **Step 7: Commit**

```bash
git add -f dv_utility.py
git commit -m "feat(dv): background update-check wiring in main app"
```

---

## Task 4: 主程式更新橫幅 + 一鍵按鈕 + 啟動更新器

**Files:**
- Modify: `DV_Utility/dv_utility.py`（`init_window` 內 release_note_frame 之後 line 456 附近；新增 slot 與 handler）

- [ ] **Step 1: `init_window` 加更新橫幅（隱藏）**

在 `init_window` 中 `main_layout.addWidget(self.release_note_frame)`（line 456）之後插入：

```python
        # ---- 更新提示橫幅 (預設隱藏; 發現新版時由 _slot_update_available 顯示) ----
        self.update_frame = QFrame()
        uf_layout = QVBoxLayout(self.update_frame)
        self.update_label = QLabel("")
        self.update_label.setFont(default_font)
        self.update_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_label.setWordWrap(True)
        uf_layout.addWidget(self.update_label)
        self.update_button = QPushButton("一鍵更新並重啟")
        self.update_button.setFont(default_font)
        self.update_button.clicked.connect(self.on_update_clicked)
        uf_layout.addWidget(self.update_button)
        self.update_frame.hide()
        main_layout.addWidget(self.update_frame)
```

- [ ] **Step 2: 加 `_slot_update_available` 與 `on_update_clicked`**

在 Task 3 加入的 `_check_update` 方法之後插入：

```python
    def _slot_update_available(self, info):
        """GUI thread: 顯示更新橫幅 (版本 + release note) 與一鍵按鈕。"""
        self._update_info = info
        latest = info.get('version', '')
        note = info.get('release_note', '')
        text = f"發現新版本 {latest} (目前 {self.version})"
        if note:
            text += f"\n{note}"
        self.update_label.setText(text)
        self.update_frame.show()

    def on_update_clicked(self):
        """啟動 dv_updater.exe (下載/換裝/重啟由它負責), 隨即結束本程式讓它替換。"""
        info = self._update_info
        target = update_check_launcher_exe()
        if not info or not target:
            QMessageBox.warning(self, "無法更新",
                                "此為開發模式或找不到可更新的執行檔 (需打包後執行)。")
            return
        updater = os.path.join(os.path.dirname(target), 'dv_updater.exe')
        if not os.path.isfile(updater):
            QMessageBox.warning(self, "無法更新", f"找不到更新器:\n{updater}")
            return
        args = [
            updater,
            '--version', info.get('version', ''),
            '--zip-url', info.get('zip_url', ''),
            '--sha256', info.get('sha256', ''),
            '--target', target,
            '--parent-pid', str(os.getpid()),
        ]
        try:
            # 一般子行程 (無 detached 旗標); Windows 上子行程於父行程退出後仍繼續執行。
            subprocess.Popen(args, cwd=os.path.dirname(target), close_fds=True)
        except Exception as e:
            log.exception('failed to launch updater')
            QMessageBox.warning(self, "無法更新", f"啟動更新器失敗:\n{e}")
            return
        log.info('updater launched (target=%s); quitting for swap', target)
        self.app.quit()
```

- [ ] **Step 3: 加定位 launcher exe 的函式**

在 `update_check.py` 加入（主程式需要「使用者點的那顆 DV_Utility.exe」路徑；onefile 下 `sys.executable` 指向解壓快取，須用 `__compiled__.original_argv0`）：

```python
import os
import sys


def launcher_exe():
    """使用者啟動的那顆 DV_Utility.exe 絕對路徑 (= 要被更新器替換的本體)。

    onefile 執行期 sys.executable 指向解壓快取, 非 launcher; 需用 __compiled__.original_argv0。
    非打包 / 抓不到合法 .exe -> None。
    """
    comp = globals().get('__compiled__')
    if comp is None:
        return None
    argv0 = getattr(comp, 'original_argv0', None) or sys.argv[0]
    if not argv0:
        return None
    path = os.path.realpath(os.path.abspath(argv0))
    return path if path.lower().endswith('.exe') else None
```

並把 `dv_utility.py` 中 `on_update_clicked` 的 `update_check_launcher_exe()` 改為 `update_check.launcher_exe()`：

```python
        target = update_check.launcher_exe()
```

- [ ] **Step 4: 語法編譯確認**

Run: `venv/Scripts/python -m py_compile dv_utility.py update_check.py`
Expected: 無輸出（成功）

- [ ] **Step 5: Commit**

```bash
git add -f dv_utility.py update_check.py
git commit -m "feat(dv): update banner + one-click button launches dv_updater and quits"
```

---

## Task 5: `dv_updater.py` — 參數解析

**Files:**
- Create: `DV_Utility/dv_updater.py`
- Test: `DV_Utility/tests/test_updater.py`

- [ ] **Step 1: 寫失敗測試**

`DV_Utility/tests/test_updater.py`:

```python
import dv_updater


def test_parse_args():
    ns = dv_updater.parse_args([
        '--version', 'v20260717',
        '--zip-url', 'https://x/DV_Utility.zip',
        '--sha256', 'deadbeef',
        '--target', r'C:\PCDV\DV_Utility.exe',
        '--parent-pid', '4321',
    ])
    assert ns.version == 'v20260717'
    assert ns.zip_url == 'https://x/DV_Utility.zip'
    assert ns.sha256 == 'deadbeef'
    assert ns.target == r'C:\PCDV\DV_Utility.exe'
    assert ns.parent_pid == 4321
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dv_updater'`

- [ ] **Step 3: 寫最小實作**

`DV_Utility/dv_updater.py`:

```python
"""DV_Utility 更新器 (半自動一鍵更新的 helper)。

由 DV_Utility.exe 在使用者按下「一鍵更新並重啟」後啟動; 主程式隨即退出。
本程式負責: 下載新版 zip -> 驗 sha256 -> 解壓出新 DV_Utility.exe -> 待舊檔解鎖後就地替換
(舊檔改 .bak) -> 啟動新版 -> 結束。

刻意保持「溫和」以通過 OPSWAT 掃毒: 不改寫自己、不用 DETACHED_PROCESS 旗標、
不改名自己; 只是一個獨立小程式把已不在執行的 exe 換成新版 (等同任何 installer)。
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile

import requests


def parse_args(argv):
    p = argparse.ArgumentParser(description='DV_Utility updater')
    p.add_argument('--version', required=True)
    p.add_argument('--zip-url', required=True)
    p.add_argument('--sha256', default='')
    p.add_argument('--target', required=True, help='要被替換並重啟的 DV_Utility.exe 絕對路徑')
    p.add_argument('--parent-pid', type=int, default=0)
    return p.parse_args(argv)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: Commit**

```bash
git add -f dv_updater.py tests/test_updater.py
git commit -m "feat(dv): dv_updater arg parsing"
```

---

## Task 6: `dv_updater.download` — 下載 + 驗 sha256

**Files:**
- Modify: `DV_Utility/dv_updater.py`
- Test: `DV_Utility/tests/test_updater.py`

- [ ] **Step 1: 寫失敗測試（追加）**

追加到 `DV_Utility/tests/test_updater.py`（用本機 HTTP server 供檔，避免真連網）：

```python
import hashlib
import http.server
import threading
import functools


def _serve_dir(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = http.server.HTTPServer(('127.0.0.1', 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def test_download_ok(tmp_path):
    payload = b'hello-update-payload'
    (tmp_path / 'a.bin').write_bytes(payload)
    httpd, port = _serve_dir(tmp_path)
    try:
        dest = tmp_path / 'out.bin'
        sha = hashlib.sha256(payload).hexdigest()
        dv_updater.download(f'http://127.0.0.1:{port}/a.bin', str(dest), sha)
        assert dest.read_bytes() == payload
    finally:
        httpd.shutdown()


def test_download_sha_mismatch_removes_file(tmp_path):
    (tmp_path / 'a.bin').write_bytes(b'xxxx')
    httpd, port = _serve_dir(tmp_path)
    try:
        dest = tmp_path / 'out.bin'
        import pytest
        with pytest.raises(RuntimeError):
            dv_updater.download(f'http://127.0.0.1:{port}/a.bin', str(dest), 'wrong_sha')
        assert not dest.exists()
    finally:
        httpd.shutdown()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -k download -v`
Expected: FAIL — `AttributeError: module 'dv_updater' has no attribute 'download'`

- [ ] **Step 3: 寫最小實作（追加到 `dv_updater.py`）**

```python
def download(url, dest, expected_sha256, progress_cb=None, timeout=120):
    """串流下載到 dest, 邊下載邊算 sha256; 不符則刪檔並丟 RuntimeError。"""
    sha = hashlib.sha256()
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        try:
            total = int(r.headers.get('content-length') or 0)
        except (TypeError, ValueError):
            total = 0
        done = 0
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(done, total)
    if expected_sha256 and sha.hexdigest().lower() != expected_sha256.lower():
        try:
            os.remove(dest)
        except OSError:
            pass
        raise RuntimeError('下載檔 sha256 不符, 已中止更新')
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -k download -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add -f dv_updater.py tests/test_updater.py
git commit -m "feat(dv): dv_updater streaming download with sha256 verify"
```

---

## Task 7: `dv_updater.extract_main_exe` — 從 zip 取出新 exe

**Files:**
- Modify: `DV_Utility/dv_updater.py`
- Test: `DV_Utility/tests/test_updater.py`

- [ ] **Step 1: 寫失敗測試（追加）**

```python
import zipfile


def test_extract_main_exe(tmp_path):
    zpath = tmp_path / 'DV_Utility.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('DV_Utility.exe', b'NEW-EXE-BYTES')
        z.writestr('dv_updater.exe', b'UPDATER-BYTES')
        z.writestr('resource/x.txt', b'noise')
    out = tmp_path / 'extracted.exe'
    dv_updater.extract_main_exe(str(zpath), str(out))
    assert out.read_bytes() == b'NEW-EXE-BYTES'


def test_extract_main_exe_missing(tmp_path):
    zpath = tmp_path / 'bad.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('readme.txt', b'no exe here')
    import pytest
    with pytest.raises(RuntimeError):
        dv_updater.extract_main_exe(str(zpath), str(tmp_path / 'out.exe'))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -k extract -v`
Expected: FAIL — `AttributeError: ... 'extract_main_exe'`

- [ ] **Step 3: 寫最小實作（追加到 `dv_updater.py`）**

```python
def extract_main_exe(zip_path, dest_exe):
    """從 zip 取出 DV_Utility.exe 寫到 dest_exe; 找不到則丟 RuntimeError。"""
    with zipfile.ZipFile(zip_path) as z:
        name = next((n for n in z.namelist()
                     if n.lower().rstrip('/').endswith('dv_utility.exe')), None)
        if name is None:
            raise RuntimeError('zip 內找不到 DV_Utility.exe')
        with z.open(name) as src, open(dest_exe, 'wb') as dst:
            shutil.copyfileobj(src, dst)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -k extract -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add -f dv_updater.py tests/test_updater.py
git commit -m "feat(dv): dv_updater extract DV_Utility.exe from zip"
```

---

## Task 8: `dv_updater.swap_with_backup` — 就地替換 + 回滾

**Files:**
- Modify: `DV_Utility/dv_updater.py`
- Test: `DV_Utility/tests/test_updater.py`

- [ ] **Step 1: 寫失敗測試（追加）**

```python
def test_swap_with_backup_happy(tmp_path):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    new = tmp_path / 'new.exe'
    new.write_bytes(b'NEW')
    dv_updater.swap_with_backup(str(target), str(new))
    assert target.read_bytes() == b'NEW'
    assert (tmp_path / 'DV_Utility.exe.bak').read_bytes() == b'OLD'
    assert not new.exists()


def test_swap_with_backup_rollback_on_failure(tmp_path, monkeypatch):
    target = tmp_path / 'DV_Utility.exe'
    target.write_bytes(b'OLD')
    new = tmp_path / 'new.exe'
    new.write_bytes(b'NEW')

    real_replace = os.replace
    calls = {'n': 0}

    def flaky_replace(src, dst):
        calls['n'] += 1
        # 第 1 次 (target -> .bak) 放行; 第 2 次 (new -> target) 失敗一次觸發回滾
        if calls['n'] == 2:
            raise OSError('boom placing new exe')
        return real_replace(src, dst)

    monkeypatch.setattr(dv_updater.os, 'replace', flaky_replace)
    import pytest
    with pytest.raises(OSError):
        dv_updater.swap_with_backup(str(target), str(new))
    # 回滾後 target 應恢復為 OLD
    assert target.read_bytes() == b'OLD'
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -k swap -v`
Expected: FAIL — `AttributeError: ... 'swap_with_backup'`

- [ ] **Step 3: 寫最小實作（追加到 `dv_updater.py`）**

```python
def swap_with_backup(target_exe, new_exe, attempts=20, delay=0.3):
    """舊檔改 .bak -> 新檔就位; 帶退避重試 (等主程式退出釋放檔案鎖)。

    失敗會自動回滾 (把 .bak 還原成 target) 並把例外往上丟。
    """
    bak = target_exe + '.bak'
    # 1) 把舊檔挪到 .bak (等鎖釋放; 主程式為 onefile, 退出後才解得了鎖)
    for i in range(attempts):
        try:
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(target_exe, bak)
            break
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
    # 2) 新檔就位; 失敗則回滾
    try:
        os.replace(new_exe, target_exe)
    except Exception:
        os.replace(bak, target_exe)
        raise
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/test_updater.py -k swap -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 全測試回歸**

Run: `PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/ -v`
Expected: PASS（全部）

- [ ] **Step 6: Commit**

```bash
git add -f dv_updater.py tests/test_updater.py
git commit -m "feat(dv): dv_updater in-place swap with .bak rollback"
```

---

## Task 9: `dv_updater.main` — 組裝 + 極簡狀態視窗 + 重啟

**Files:**
- Modify: `DV_Utility/dv_updater.py`

- [ ] **Step 1: 加極簡 tkinter 狀態視窗（best-effort，失敗即無視窗）**

追加到 `dv_updater.py`（放在 `parse_args` 之後、`download` 之前）：

```python
class _StatusUI:
    """更新期間的極簡狀態視窗 (tkinter, best-effort)。主程式已退出, 需自己給點回饋。

    tkinter 失敗 (無顯示器等) 時全部 no-op, 不影響更新流程。
    """

    def __init__(self):
        self._tk = None
        self._label = None

    def start(self, text):
        try:
            import tkinter as tk
            self._tk = tk.Tk()
            self._tk.title('DV Utility 更新')
            self._tk.attributes('-topmost', True)
            self._tk.geometry('320x90')
            self._label = tk.Label(self._tk, text=text, padx=16, pady=20)
            self._label.pack(expand=True, fill='both')
            self._tk.update()
        except Exception:
            self._tk = None

    def set_text(self, text):
        if not self._tk:
            return
        try:
            self._label.config(text=text)
            self._tk.update()
        except Exception:
            pass

    def close(self):
        if not self._tk:
            return
        try:
            self._tk.destroy()
        except Exception:
            pass
        self._tk = None
```

- [ ] **Step 2: 加日誌 + `main()`**

追加到 `dv_updater.py`（`swap_with_backup` 之後）：

```python
def relaunch(exe):
    """啟動新版 (一般子行程, 無特殊旗標)。"""
    subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)


def _log(msg):
    """寫一行 log 到 target 同層的更新記錄 (best-effort)。"""
    try:
        with open(os.path.join(os.path.dirname(_LOG_TARGET or '.'), 'dv_updater.log'),
                  'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass


_LOG_TARGET = None


def main(argv=None):
    global _LOG_TARGET
    args = parse_args(argv if argv is not None else sys.argv[1:])
    target = os.path.realpath(args.target)
    _LOG_TARGET = target
    work_dir = os.path.dirname(target)
    part = os.path.join(work_dir, 'DV_Utility.update.part')   # 下載暫存 (同磁碟區)
    new_exe = os.path.join(work_dir, 'DV_Utility.update.new')  # 解壓出的新 exe (同磁碟區)

    ui = _StatusUI()
    ui.start(f'正在更新到 {args.version} …')
    # 給主程式一點時間完成退出 (釋放 exe 檔案鎖); 下載本身也會再拖幾秒
    time.sleep(0.5)

    # 先把「殘留暫存」清掉
    for leftover in (part, new_exe):
        try:
            os.remove(leftover)
        except OSError:
            pass

    try:
        ui.set_text(f'下載中 … ({args.version})')
        download(args.zip_url, part, args.sha256,
                 progress_cb=lambda d, t: ui.set_text(f'下載中 … {int(d / t * 100)}%'))
        ui.set_text('安裝中 …')
        extract_main_exe(part, new_exe)
        os.remove(part)
        swap_with_backup(target, new_exe)
    except Exception as e:
        # 失敗: 清暫存, 重啟舊版 (仍完好), 讓使用者不會沒程式可用
        _log(f'update FAILED: {e!r}')
        for leftover in (part, new_exe):
            try:
                os.remove(leftover)
            except OSError:
                pass
        ui.close()
        try:
            if os.path.isfile(target):
                relaunch(target)
        except Exception:
            _log('relaunch old exe FAILED')
        sys.exit(1)

    _log(f'update OK -> {args.version}')
    ui.close()
    relaunch(target)


if __name__ == '__main__':
    main()
```

- [ ] **Step 3: 語法編譯 + 全測試回歸**

Run:
```bash
venv/Scripts/python -m py_compile dv_updater.py
PYTEST_DEBUG_TEMPROOT="$PWD/.pytest_tmp" venv/Scripts/python -m pytest tests/ -v
```
Expected: py_compile 無輸出；pytest 全數 PASS

- [ ] **Step 4: Commit**

```bash
git add -f dv_updater.py
git commit -m "feat(dv): dv_updater main() orchestration + status window + relaunch"
```

---

## Task 10: `release_dv_utility.py` — 建更新器 exe + 重建 manifest + 打包兩顆

**Files:**
- Modify: `DV_Utility/release_dv_utility.py`

- [ ] **Step 1: 重新加回 manifest 所需的常數與 sha256**

在 `OUTPUT_NAME = 'DV_Utility'`（line 28）之後補：

```python
UPDATER_NAME = 'dv_updater'

# 更新檢查/下載: 發佈端上傳到 GCS 的 zip 公開網址 (dv_updater.exe 會抓它)。
GCS_ZIP_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/DVUtility/DV_Utility.zip'
```

在 `_verfile = ...` 那組路徑常數後補（若移除自我更新時已刪 `_pubfile`，此處補回）：

```python
_pubfile = os.path.join(_here, 'publish_version.json')   # 上傳成 GCS 的 version.json
_updater_script = os.path.join(_here, 'dv_updater.py')
```

在檔案上方（`make_ico_from_png` 之前）補回 sha256 工具與 import：

在 import 區把 `import json` 那段補上 `import hashlib`（若移除自我更新時已刪）。並加函式：

```python
def sha256_of(path):
    """檔案 sha256 (供 manifest 記錄, dv_updater.exe 下載後比對完整性)。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()
```

- [ ] **Step 2: 在 `main()` build 主程式之後，加建 `dv_updater.exe`**

在 `release_dv_utility.py` `main()` 內、主程式 `subprocess.run(cmd, check=True)`（build 主程式）與 `sign_exe(built_exe)` 之間，插入更新器的 build：

```python
    # -- build 更新器 (dv_updater.exe; Nuitka onefile, 溫和版) --------------
    updater_cmd = [
        sys.executable, '-m', 'nuitka',
        '--mode=onefile',
        '--enable-plugin=tk-inter',
        '--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}_Updater/{VERSION}',
        '--onefile-cache-mode=cached',
        '--company-name=Realtek',
        '--product-name=DV_Utility_Updater',
        f'--product-version={num_version}',
        f'--file-version={num_version}',
        f'--windows-icon-from-ico={tmp_ico}',
        '--assume-yes-for-downloads',
        '--output-dir=dist',
        f'--output-filename={UPDATER_NAME}.exe',
        '--remove-output',
    ]
    if not DEBUG_BUILD:
        updater_cmd.append('--windows-console-mode=disable')
    updater_cmd.append(_updater_script)
    subprocess.run(updater_cmd, check=True)
    built_updater = os.path.join('dist', f'{UPDATER_NAME}.exe')
    sign_exe(built_updater)   # 選配 signtool 簽章; 未設憑證則略過 (CTC 為實際簽章路徑)
```

> 註：`tmp_ico` / `num_version` / `sign_exe` 皆為主程式 build 流程既有變數/函式，沿用即可。

- [ ] **Step 3: package 段一起複製更新器**

把 package 段（複製 built_exe 進 `OUTPUT_NAME/` 資料夾與頂層那兩行）擴充為同時複製更新器：

原本：

```python
    shutil.copy2(built_exe, os.path.join(OUTPUT_NAME, f'{OUTPUT_NAME}.exe'))
    shutil.copy2(built_exe, f'{OUTPUT_NAME}.exe')
```

改為：

```python
    shutil.copy2(built_exe, os.path.join(OUTPUT_NAME, f'{OUTPUT_NAME}.exe'))
    shutil.copy2(built_exe, f'{OUTPUT_NAME}.exe')
    shutil.copy2(built_updater, os.path.join(OUTPUT_NAME, f'{UPDATER_NAME}.exe'))
    shutil.copy2(built_updater, f'{UPDATER_NAME}.exe')
```

- [ ] **Step 4: zip 之後重新產生 manifest（publish_version.json，含 release_note）**

把 zip 段與結尾 print 改成（`shutil.make_archive` 產生 `DV_Utility.zip` 後）：

```python
    # -- zip (含 DV_Utility.exe + dv_updater.exe) ----------------------------
    zip_path = f'{OUTPUT_NAME}.zip'
    shutil.make_archive(OUTPUT_NAME, 'zip', root_dir=OUTPUT_NAME, base_dir='.')

    # -- 發佈資訊 (主程式 update_check 比版本; dv_updater.exe 下載 + 驗 sha256) --
    #    release_note 由環境變數 DVUTIL_RELEASE_NOTE 帶入 (可空)。上傳成 GCS 的
    #    DVUtility/version.json (見 upload_to_gcs.py)。
    publish = {
        'version':      app_version,
        'zip_url':      GCS_ZIP_URL,
        'size':         os.path.getsize(zip_path),
        'sha256':       sha256_of(zip_path),
        'release_note': os.environ.get('DVUTIL_RELEASE_NOTE', ''),
    }
    with open(_pubfile, 'w', encoding='utf-8') as f:
        json.dump(publish, f, ensure_ascii=False, indent=2)
        f.write('\n')

    print('完成:', f'{OUTPUT_NAME} {app_version}',
          '->', f'{OUTPUT_NAME}.exe / {UPDATER_NAME}.exe / {OUTPUT_NAME}.zip / publish_version.json')
```

- [ ] **Step 5: 語法編譯確認**

Run: `venv/Scripts/python -m py_compile release_dv_utility.py`
Expected: 無輸出（成功）

- [ ] **Step 6: Commit**

```bash
git add -f release_dv_utility.py
git commit -m "build(dv): build dv_updater.exe, bundle both exes, regenerate GCS manifest"
```

---

## Task 11: `upload_to_gcs.py` — 重新上傳 manifest

**Files:**
- Modify: `DV_Utility/upload_to_gcs.py`

- [ ] **Step 1: 在 zip 上傳之後，補回 manifest 上傳**

把 `if __name__ == "__main__":` 內、上傳 zip 之後補上：

```python
    # 更新用的版本資訊 (release_dv_utility.py 產生); 主程式 update_check 會抓這顆比版本、
    # dv_updater.exe 依它下載並驗 sha256。
    upload_file(
        bucket,
        os.path.join(here, "publish_version.json"),
        "DVUtility/version.json",
    )
```

並把上傳 zip 的註解由「已移除自我更新…」改回中性：

```python
    # 主程式 zip (含 DV_Utility.exe + dv_updater.exe)
```

- [ ] **Step 2: 語法編譯確認**

Run: `venv/Scripts/python -m py_compile upload_to_gcs.py`
Expected: 無輸出（成功）

- [ ] **Step 3: Commit**

```bash
git add -f upload_to_gcs.py
git commit -m "build(dv): re-upload GCS manifest for update-check"
```

---

## Task 12: 端到端功能驗證（本機假 v2，不連正式 GCS）

**Files:** 無（驗證步驟；用 `verify` / `run` skill）

- [ ] **Step 1: 建置兩顆 exe**

Run:
```bash
DVUTIL_RELEASE_NOTE="測試更新流程" venv/Scripts/python release_dv_utility.py
```
Expected: 產出 `dist/DV_Utility.exe`、`dist/dv_updater.exe`、頂層 `DV_Utility.exe` / `dv_updater.exe`、`DV_Utility.zip`、`publish_version.json`（含 `version` 為今天日期版本、`sha256`、`release_note`）。

- [ ] **Step 2: 架本機 HTTP server 供「假新版」**

做法：把剛產生的 `DV_Utility.zip` 當成「新版」，並手動寫一份 `version.json`，版本號比目前 build 高一號（例如把 `.1` 或日期 +1），`zip_url` 指向本機 server，`sha256` 用 `publish_version.json` 內的值。

```bash
mkdir -p /tmp/dvtest && cp DV_Utility.zip /tmp/dvtest/
# 依 publish_version.json 的 sha256 手寫一份較新版本的 manifest 到 /tmp/dvtest/version.json
# 例如 version 改成比目前高 (v<今日>.9), zip_url= http://127.0.0.1:8009/DV_Utility.zip
cd /tmp/dvtest && python -m http.server 8009 &
```

- [ ] **Step 3: 暫時把 `update_check.VERSION_URL` 指向本機 server 後重建，或以 hosts/環境測試**

最省事：在 `update_check.py` 暫時把 `VERSION_URL` 改為 `http://127.0.0.1:8009/version.json`，重建主程式（只需 build 主程式那段），跑起來驗證流程。驗證完還原 `VERSION_URL`。

- [ ] **Step 4: 用 `run` skill 實際跑主程式並驗證**

- 啟動 `DV_Utility.exe` → 幾秒後應出現「發現新版本 v… / 一鍵更新並重啟」橫幅。
- 按下按鈕 → 主程式關閉 → `dv_updater.exe` 出現「更新中」小視窗 → 下載/安裝 → 新版自動開啟。
- 確認 `DV_Utility.exe.bak` 產生、`DV_Utility.exe` 已被替換、`dv_updater.log` 記到 `update OK`。

- [ ] **Step 5: 驗證失敗回滾**

把本機 `version.json` 的 `sha256` 改成錯的 → 重跑一鍵更新 → 應：下載後 sha256 不符 → 不動原檔 → 重啟「舊版」；`dv_updater.log` 記到 `update FAILED`。

- [ ] **Step 6: 還原 `VERSION_URL` 並重建、清理測試檔**

- 還原 `update_check.py` 的 `VERSION_URL`。
- Run: `venv/Scripts/python -m py_compile update_check.py`
- 清掉 `/tmp/dvtest`、關掉 http.server。

- [ ] **Step 7: Commit（若步驟 6 有還原改動）**

```bash
git add -f update_check.py
git commit -m "test(dv): verified end-to-end one-click update + rollback (VERSION_URL restored)"
```

---

## Task 13: OPSWAT 關卡（go/no-go）

**Files:** 無（人工關卡；決定採用 A 或升級 B）

- [ ] **Step 1: 把 `dist/dv_updater.exe` 送 CTC 數位憑證簽署服務的掃毒關卡**（`file-sign create/upload/start`；見記憶 realtek-code-signing-service 的流程）。

- [ ] **Step 2: 判讀結果**
  - **OPSWAT 通過（status 走到 HSM/SUCCESS，沒被判毒）** → 採用 **A（本計畫）**；繼續 Task 14。
  - **被判毒（如 `Python/Packed.Nuitka_AGen.*`）** → **升級 B**：改用附錄 B 的 Go 原生更新器（介面與 argv 完全相同，主程式不用改），重跑 Task 13。
  - **連 B 都被判毒** → 退到附錄 C 通知式。

---

## Task 14: 簽章 + 發佈

**Files:** 無（發佈流程）

- [ ] **Step 1: 兩顆 exe 皆走 CTC 簽章**（主程式 `DV_Utility.exe`、更新器 `dv_updater.exe`；OPSWAT → 主管核准 → HSM 簽 → 下載已簽章檔）。
- [ ] **Step 2: 用已簽章的兩顆 exe 重打包 `DV_Utility.zip`**（重跑 package/zip 段，或手動替換 zip 內兩顆 exe），並重算 `publish_version.json` 的 `sha256`（因 zip 內容已換成簽章版）。
- [ ] **Step 3: `venv/Scripts/python upload_to_gcs.py`** 上傳 `DV_Utility.zip` 與 `version.json`。
- [ ] **Step 4: 請 IT 把 SentinelOne/防毒排除改為「依簽發者憑證」**（一勞永逸，不必逐版加 hash）。
- [ ] **Step 5: 冒煙測試**：從乾淨機器下載 zip、解壓、執行；確認不被擋、且能一鍵更新到下一版。

---

## 附錄 B（contingency）：Go 原生更新器

僅在 Task 13 判定 A 被 OPSWAT 判毒時啟用。**argv 介面與 A 完全相同**（`--version --zip-url --sha256 --target --parent-pid`），主程式 `on_update_clicked` 不需改動。

- Create: `DV_Utility/updater_go/main.go`（`net/http` 下載、`crypto/sha256` 驗證、`archive/zip` 解壓、`os.Rename`+回滾、`os/exec` 重啟）。
- Build：`go build -ldflags="-H windowsgui -s -w" -o dist/dv_updater.exe ./updater_go`（需先裝 Go）。
- `release_dv_utility.py`：把 Task 10 的 Nuitka updater build 段換成 `go build` 呼叫（其餘打包/manifest 不變）。
- 邏輯與行為對照 A 逐項等價：下載到 target 同層 `.part` → 驗 sha256 → 解出 `DV_Utility.exe` → 舊檔改 `.bak` → 新檔就位（失敗回滾）→ `exec` 重啟。
- 可選：用 `WinVerifyTrust`（syscall）驗證新 exe 的 Authenticode 做縱深防禦。
- 送 OPSWAT 重驗（回到 Task 13）。

## 附錄 C（contingency）：通知式 fallback

僅在連原生下載器都過不了 OPSWAT 時啟用。

- 拿掉 `dv_updater.exe` 與其打包/上傳。
- 主程式 `on_update_clicked` 改為：以瀏覽器開啟下載頁 / GCS zip 網址（`webbrowser.open(info['zip_url'])`），提示使用者自行下載並解壓覆蓋。
- 主程式仍保有 update_check（比版本、顯示橫幅）。零下載-執行行為，必過掃毒。

---

## Self-Review 對照 spec

- spec §3.1 主程式乾淨（只讀 JSON + 按鈕 + 啟動 helper + 退出）→ Task 3、Task 4。✔
- spec §3.2 更新器階梯式 → Task 5–9（A）、Task 13 關卡、附錄 B/C。✔
- spec §3.3 manifest 欄位（version/zip_url/sha256/size/release_note）→ Task 10 Step 4。✔
- spec §4 就地替換 by helper、`.bak` 回滾、路徑不變 → Task 8、Task 4（launcher_exe 定位、同資料夾 updater）。✔
- spec §5 資料流（傳參、等退出、下載/驗/解/替換/重啟）→ Task 4 Step 2、Task 9。✔
- spec §6 階梯式 A/B/C → Task 5–9 / 附錄 B / 附錄 C。✔
- spec §7 錯誤處理與回滾（先做完可失敗步驟、失敗重啟舊版、就位失敗回滾）→ Task 8、Task 9 main()。✔
- spec §8 安全（HTTPS + sha256、參數驗證）→ Task 6、Task 4（updater 路徑檢查）。✔（`zip_url` 限定 GCS 網域可於 Task 6/9 追加，屬強化項）
- spec §9 簽章整合（兩顆走 CTC）→ Task 14。✔
- spec §10 build 調整（產兩顆、manifest）→ Task 10、Task 11。✔
- spec §11 測試（OPSWAT 關卡 + 功能）→ Task 12、Task 13。✔
