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


def extract_main_exe(zip_path, dest_exe):
    """從 zip 取出 DV_Utility.exe 寫到 dest_exe; 找不到則丟 RuntimeError。"""
    with zipfile.ZipFile(zip_path) as z:
        name = next((n for n in z.namelist()
                     if n.lower().rstrip('/').endswith('dv_utility.exe')), None)
        if name is None:
            raise RuntimeError('zip 內找不到 DV_Utility.exe')
        with z.open(name) as src, open(dest_exe, 'wb') as dst:
            shutil.copyfileobj(src, dst)


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
