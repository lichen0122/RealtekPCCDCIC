"""DV_Utility 更新檢查 (供主程式使用)。

只做「純 JSON 讀取 + 版本比對」——沒有下載 PE / 換檔 / 執行子程序等會被防毒判毒的行為。
真正的下載換裝重啟由獨立的 dv_updater.exe 負責。
"""

import os
import re
import sys

import rtnas

# 發佈端 (release_dv_utility.py --publish-nas) 簽好並上傳到公司 RT NAS 的 manifest。
# 只從 NAS 讀: 主程式與 dv_updater.exe 都不再向 GCS 要版本資訊。
VERSION_URL = rtnas.build_sftp_url(rtnas.NAS_HOST, rtnas.NAS_RELEASE_ROOT + '/DVUtility/version.json')


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


def fetch_manifest(store=None, fetch=None):
    """從 NAS 讀 DV_Utility 的 version.json 並驗證; 回傳 manifest dict。

    NAS 人人可寫, 所以必須: 發佈者 Ed25519 簽章有效、簽進去的 nas_path = VERSION_URL 的路徑
    (防止別的簽章檔被搬過來)、zip_url 是 NAS 的 sftp:// 且帶 sha256。任一不符丟 rtnas 例外
    (未存 NAS 登入 = rtnas.AuthError)。
    """
    raw = (fetch or rtnas.fetch_url)(VERSION_URL, store=store)
    info = rtnas.verify_signed_json(raw, expected_path=rtnas.parse_sftp_url(VERSION_URL)[2])
    if not rtnas.is_sftp_url(info.get('zip_url')):
        raise rtnas.IntegrityError(f"version.json zip_url is not on the RT NAS: {info.get('zip_url')!r}")
    if not info.get('sha256'):
        raise rtnas.IntegrityError('version.json has no sha256 for the zip')
    return info


def check_latest_version(current_version, store=None, fetch=None):
    """讀 NAS version.json 比對目前版本; 有更新回 info dict, 否則 None。

    info = {"version","zip_url","sha256","size","release_note", ...}
    非打包 / 未存 NAS 登入 / 連不上 / 簽章不符等任何錯誤 -> None。
    """
    if not _is_packaged():
        return None
    try:
        info = fetch_manifest(store=store, fetch=fetch)
    except Exception:
        return None
    latest = info.get('version', '')
    if latest and is_newer(latest, current_version):
        return info
    return None


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


def cleanup_stale_backup():
    """刪除上次更新留下的 DV_Utility.exe.bak (best-effort; 打包執行且上次有更新過才有)。

    由主程式啟動時呼叫 —— 此時舊行程已退出、鎖已釋放, 刪得掉那顆 ~36MB 備份, 避免無限累積。
    刪一顆備份檔屬良性行為, 不影響主程式的 OPSWAT 乾淨度。
    """
    exe = launcher_exe()
    if not exe:
        return
    try:
        os.remove(exe + '.bak')
    except OSError:
        pass
