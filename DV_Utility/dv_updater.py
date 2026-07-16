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
