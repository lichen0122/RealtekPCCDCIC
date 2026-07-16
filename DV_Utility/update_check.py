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
