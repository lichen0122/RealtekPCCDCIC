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
