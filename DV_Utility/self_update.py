"""DV_Utility 自我更新 (Nuitka onefile, Windows)。

設計重點 (皆對照 Nuitka 4.1.2 原始碼確認):

* onefile 執行期, 真正在跑的是「解壓快取資料夾」裡的副本:
      sys.executable / __file__ / __nuitka_binary_exe  -> %LOCALAPPDATA%\\Realtek\\DV_Utility\\{VERSION}\\...
  改它們對自我更新「無效」(而且檔案被鎖)。
  使用者點的那顆 DV_Utility.exe (要被替換的本體) 只能透過:
      sys.argv[0]  或  __compiled__.original_argv0
  取得 -> 見 _launcher_exe()。

* 執行中的 exe 在 Windows「不能覆寫/刪除, 但可以同磁碟區改名/搬移」(映像以 FILE_SHARE_DELETE 開啟)。
  因此自我更新採用業界標準招式:
      1. 把自己改名成  *.old   (執行中允許)
      2. 把新 exe 寫到原路徑
      3. 啟動新版 (detached) 後結束本程式
      4. 下次開機再刪掉 *.old  (舊行程退出後才解得了鎖)
  os.replace == MoveFileExW(MOVEFILE_REPLACE_EXISTING) 不帶 COPY_ALLOWED,
  跨磁碟區會失敗 -> 下載/暫存檔一律放在 exe 同資料夾。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

import requests

# 發佈端 (release_dv_utility.py + upload_to_gcs.py) 上傳到 GCS 的小檔, 內含最新版本資訊。
VERSION_URL = "https://storage.googleapis.com/realtek-pccdcic-dv/DVUtility/version.json"

# Win32 process creation flags (本工具僅 Windows)。
_DETACHED_PROCESS         = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# 選配硬化: 換檔前用 WinVerifyTrust 驗證「下載的新 exe」是否帶有效且受信任的 Authenticode
# 簽章。需發佈端已簽章 (release_dv_utility.py 的 sign_exe)。目前尚無 code-signing 憑證,
# 故預設關閉 (維持現行行為)。待有憑證且 build 開始簽章後改成 True, 即可在自我更新時
# 拒絕未簽章 / 被竄改的更新檔 (既有的 sha256 完整性檢查一律照做, 與此旗標無關)。
VERIFY_DOWNLOAD_SIGNATURE = False


# --------------------------------------------------------------------------- #
#  路徑 / 版本
# --------------------------------------------------------------------------- #
def _compiled():
    """取得 Nuitka 的 __compiled__ struct (優先用 __main__ 的, 再退回本模組); 未打包回 None。

    __compiled__ 是每個編譯模組都有的 module-global; original_argv0 為 process 全域,
    各模組一致, 但 Nuitka 自身慣例是讀 __main__ 的, 故照辦。
    """
    main_mod = sys.modules.get('__main__')
    return getattr(main_mod, '__compiled__', None) or globals().get('__compiled__')


def is_packaged():
    """是否為 Nuitka 打包後執行 (非 `python dv_utility.py` 開發模式)。"""
    return _compiled() is not None


def _launcher_exe():
    """使用者啟動的那顆 DV_Utility.exe 絕對路徑 (= 要被替換的本體)。

    來源優先序 (皆指向 launcher, 非解壓快取):
      __compiled__.original_argv0 -> sys.argv[0]
    非打包 / 抓不到合法 .exe 路徑 -> 回傳 None (代表不進行自我更新)。
    """
    comp = _compiled()
    if comp is None:
        return None
    argv0 = getattr(comp, 'original_argv0', None) or sys.argv[0]
    if not argv0:
        return None
    path = os.path.realpath(os.path.abspath(argv0))
    return path if path.lower().endswith('.exe') else None


def _version_key(v):
    """vYYYYMMDD[.N] -> (Y, M, D, N) tuple, 供大小比較 (避免字串序把 .10 判成 < .2)。"""
    import re
    m = re.match(r'v?(\d{4})(\d{2})(\d{2})(?:\.(\d+))?$', str(v))
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0)) if m else (0, 0, 0, 0)


# --------------------------------------------------------------------------- #
#  啟動清理
# --------------------------------------------------------------------------- #
def cleanup_old():
    """刪除上次更新留下的 *.old (上個行程完全退出後才刪得掉; 失敗則靜默, 下次再清)。"""
    exe = _launcher_exe()
    if not exe:
        return
    try:
        os.remove(exe + '.old')
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  版本檢查
# --------------------------------------------------------------------------- #
def check_for_new_version(current_version, timeout=10):
    """比對遠端 version.json 與目前版本; 有更新時回傳 info dict, 否則 None。

    info = {"version": "v20260604", "zip_url": "...", "size": <bytes>, "sha256": "..."}
    任何網路 / 解析錯誤 (含 version.json 尚未發佈造成的 404) 都視為「無更新」-> 回傳 None。
    """
    if not is_packaged():
        return None
    try:
        resp = requests.get(VERSION_URL, timeout=timeout)
        resp.raise_for_status()
        info = resp.json()
    except Exception:
        return None
    latest = info.get('version', '')
    if latest and _version_key(latest) > _version_key(current_version):
        return info
    return None


# --------------------------------------------------------------------------- #
#  下載 + 替換 + 重啟
# --------------------------------------------------------------------------- #
def _replace_with_retry(src, dst, attempts=8):
    """os.replace(src, dst) 帶退避重試 (防毒可能在新檔剛出現時短暫鎖住造成 PermissionError)。"""
    import time
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.3 * (i + 1))


def _verify_authenticode(path):
    """用 WinVerifyTrust 驗證 exe 是否帶「有效且受信任」的 Authenticode 簽章; 回傳 bool。

    不依賴 signtool 是否安裝 (WinVerifyTrust 是 Windows 內建 API), 適合在使用者端執行。
    未簽章 / 簽章被竄改 / 憑證不受信任 -> 回傳 False。任何呼叫失敗亦視為未通過 (回 False)。
    """
    import ctypes
    from ctypes import POINTER, Structure, byref, c_void_p, sizeof, wintypes

    class GUID(Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(Structure):
        _fields_ = [('cbStruct', wintypes.DWORD), ('pcwszFilePath', wintypes.LPCWSTR),
                    ('hFile', wintypes.HANDLE), ('pgKnownSubject', POINTER(GUID))]

    class WINTRUST_DATA(Structure):
        _fields_ = [('cbStruct', wintypes.DWORD), ('pPolicyCallbackData', c_void_p),
                    ('pSIPClientData', c_void_p), ('dwUIChoice', wintypes.DWORD),
                    ('fdwRevocationChecks', wintypes.DWORD), ('dwUnionChoice', wintypes.DWORD),
                    ('pFile', POINTER(WINTRUST_FILE_INFO)), ('dwStateAction', wintypes.DWORD),
                    ('hWVTStateData', wintypes.HANDLE), ('pwszURLReference', wintypes.LPWSTR),
                    ('dwProvFlags', wintypes.DWORD), ('dwUIContext', wintypes.DWORD)]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2 = {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    WTD_UI_NONE, WTD_REVOKE_NONE, WTD_CHOICE_FILE = 2, 0, 1
    WTD_STATEACTION_VERIFY, WTD_STATEACTION_CLOSE, WTD_SAFER_FLAG = 1, 2, 0x100

    fi = WINTRUST_FILE_INFO()
    fi.cbStruct = sizeof(WINTRUST_FILE_INFO)
    fi.pcwszFilePath = path
    data = WINTRUST_DATA()
    data.cbStruct = sizeof(WINTRUST_DATA)
    data.dwUIChoice = WTD_UI_NONE
    data.fdwRevocationChecks = WTD_REVOKE_NONE
    data.dwUnionChoice = WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(fi)
    data.dwStateAction = WTD_STATEACTION_VERIFY
    data.dwProvFlags = WTD_SAFER_FLAG

    try:
        wvt = ctypes.WinDLL('wintrust').WinVerifyTrust
        wvt.restype = wintypes.LONG
        wvt.argtypes = [wintypes.HANDLE, POINTER(GUID), c_void_p]
        rc = wvt(None, byref(action), byref(data))
        data.dwStateAction = WTD_STATEACTION_CLOSE
        wvt(None, byref(action), byref(data))   # 釋放 state
        return rc == 0                          # 0 == ERROR_SUCCESS: 已簽章且受信任
    except Exception:
        return False


def download_and_swap(info, progress_cb=None):
    """下載新版 zip -> 驗 sha256 -> 換掉執行中的 launcher -> 啟動新版 (detached)。

    成功後「呼叫端必須結束本程式」(app.quit / sys.exit), 讓 bootstrap 退出、新版接手。
    回傳新 exe 路徑。任何步驟失敗會丟出例外, 並盡量回滾到原狀態。

    progress_cb(downloaded:int, total:int) 會在下載過程中被呼叫 (供 UI 顯示進度)。
    """
    exe = _launcher_exe()
    if not exe:
        raise RuntimeError('非 onefile 打包執行, 不支援自我更新')

    work_dir = os.path.dirname(exe)
    ver      = info['version']
    part     = os.path.join(work_dir, f'DV_Utility.{ver}.part')   # 下載暫存 (同磁碟區)
    new_exe  = os.path.join(work_dir, f'DV_Utility.{ver}.new')    # 解壓出的新 exe (同磁碟區)
    old_exe  = exe + '.old'

    # 清掉前次殘留
    for leftover in (part, new_exe):
        try:
            os.remove(leftover)
        except OSError:
            pass

    # 1) 下載到 exe 同資料夾, 邊下載邊算 sha256
    sha = hashlib.sha256()
    with requests.get(info['zip_url'], stream=True, timeout=120) as r:
        r.raise_for_status()
        try:
            total = int(r.headers.get('content-length') or info.get('size') or 0)
        except (TypeError, ValueError):
            total = 0
        downloaded = 0
        with open(part, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                f.write(chunk)
                sha.update(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(downloaded, total)

    # 2) 驗證完整性 (防半截 / 損毀的下載直接把安裝弄壞)
    expected = (info.get('sha256') or '').lower()
    if expected and sha.hexdigest().lower() != expected:
        os.remove(part)
        raise RuntimeError('下載檔 sha256 不符, 已中止更新')

    # 3) 從 zip 取出新的 DV_Utility.exe
    try:
        with zipfile.ZipFile(part) as z:
            name = next((n for n in z.namelist() if n.lower().rstrip('/').endswith('dv_utility.exe')), None)
            if name is None:
                raise RuntimeError('zip 內找不到 DV_Utility.exe')
            with z.open(name) as src, open(new_exe, 'wb') as dst:
                shutil.copyfileobj(src, dst)
    finally:
        try:
            os.remove(part)
        except OSError:
            pass

    # 3.5) 選配: 驗證新 exe 的 Authenticode 簽章 (預設關閉; 見 VERIFY_DOWNLOAD_SIGNATURE)
    if VERIFY_DOWNLOAD_SIGNATURE and not _verify_authenticode(new_exe):
        try:
            os.remove(new_exe)
        except OSError:
            pass
        raise RuntimeError('新版 exe 未通過 Authenticode 簽章驗證, 已中止更新')

    # 4) 原子替換: 先把執行中的 launcher 改名挪開, 再把新檔放回原路徑; 失敗則回滾
    try:
        os.remove(old_exe)
    except OSError:
        pass
    os.replace(exe, old_exe)              # 執行中可改名 (FILE_SHARE_DELETE)
    try:
        _replace_with_retry(new_exe, exe)  # 新檔就位
    except Exception:
        os.replace(old_exe, exe)           # 還原
        raise

    # 5) 啟動新版 (與本行程脫鉤); 之後呼叫端結束本程式 -> .old 於下次開機可刪
    creationflags = 0
    if sys.platform == 'win32':
        creationflags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [exe], cwd=work_dir, close_fds=True, creationflags=creationflags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return exe
