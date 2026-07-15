"""DV WebUtility 打包 (Nuitka onefile) + 自動進版。

* 打包對象是 agent.py (本機代理); web/index.html 與 version.json 一起打包進去。
* 進版: 版本號定義在 version.json, 日期制 vYYYYMMDD + 當日流水號 (同日重複加 .1/.2)。
* 簽章: 與 DV_Utility 同一套 env-gated 流程 (sign_exe)。未設定憑證則自動略過。
  ★ 注意: 這顆代理 exe 仍是 Nuitka onefile 原生執行檔, 一樣可能被 SentinelOne 以
    "Suspicious thread" 誤判 —— web UI 只是把「UI/自我更新」那兩個誘因拿掉, 這顆小代理
    還是需要「簽章 + IT 白名單」。詳見 README。
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

OUTPUT_NAME = 'WebUtility'

# 自我更新已移除; 這顆 zip 僅供 IT / 使用者手動下載部署。
GCS_ZIP_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/WebUtility/WebUtility.zip'

_here    = os.path.dirname(os.path.abspath(__file__))
_script  = os.path.join(_here, 'agent.py')
_web     = os.path.join(_here, 'web', 'index.html')
_verfile = os.path.join(_here, 'version.json')
_pubfile = os.path.join(_here, 'publish_version.json')

DEBUG_BUILD = False    # True 保留 console 視窗以便除錯


# --------------------------------------------------------------------------- #
#  簽章 (Authenticode) — 選配, 由環境變數帶入 (與 DV_Utility 同, 未設定則略過)。
#    DVUTIL_SIGN=1 | DVUTIL_SIGN_PFX(+_PASSWORD) | DVUTIL_SIGN_SUBJECT
#    DVUTIL_SIGN_TS_URL (預設 DigiCert) / SIGNTOOL (signtool 路徑)
# --------------------------------------------------------------------------- #
_DEFAULT_SIGN_TS_URL = 'http://timestamp.digicert.com'


def _find_signtool():
    override = os.environ.get('SIGNTOOL')
    if override and os.path.isfile(override):
        return override
    on_path = shutil.which('signtool') or shutil.which('signtool.exe')
    if on_path:
        return on_path
    best = None
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
        kits_bin = os.path.join(base or '', 'Windows Kits', '10', 'bin')
        if not os.path.isdir(kits_bin):
            continue
        for ver in os.listdir(kits_bin):
            for arch in ('x64', 'x86', 'arm64'):
                cand = os.path.join(kits_bin, ver, arch, 'signtool.exe')
                if not os.path.isfile(cand):
                    continue
                try:
                    key = tuple(int(x) for x in ver.split('.'))
                except ValueError:
                    key = (0,)
                if best is None or key > best[0]:
                    best = (key, cand)
    return best[1] if best else None


def _sign_cert_args():
    pfx = os.environ.get('DVUTIL_SIGN_PFX')
    if pfx:
        args = ['/f', pfx]
        pwd = os.environ.get('DVUTIL_SIGN_PFX_PASSWORD')
        if pwd:
            args += ['/p', pwd]
        return args, f'PFX 檔 {pfx}'
    subject = os.environ.get('DVUTIL_SIGN_SUBJECT')
    if subject:
        return ['/n', subject], f'憑證庫 subject "{subject}"'
    return ['/a'], '憑證庫自動挑選 (/a)'


def sign_exe(path):
    want_sign = any(os.environ.get(k)
                    for k in ('DVUTIL_SIGN', 'DVUTIL_SIGN_PFX', 'DVUTIL_SIGN_SUBJECT'))
    if not want_sign:
        print('簽章: 略過 (未設定 DVUTIL_SIGN* 憑證環境變數) -> 產出為未簽章 build')
        return False
    signtool = _find_signtool()
    if not signtool:
        print('簽章: 略過 — 找不到 signtool.exe (請設定 SIGNTOOL 或安裝 Windows SDK) -> 未簽章 build')
        return False
    cert_args, desc = _sign_cert_args()
    ts_url = os.environ.get('DVUTIL_SIGN_TS_URL', _DEFAULT_SIGN_TS_URL)
    print(f'簽章: {path}  (憑證: {desc}; 時間戳: {ts_url})')
    subprocess.run([signtool, 'sign', '/fd', 'SHA256',
                    '/tr', ts_url, '/td', 'SHA256', *cert_args, path], check=True)
    subprocess.run([signtool, 'verify', '/pa', '/v', path], check=True)
    print(f'簽章: 完成並通過驗證 -> {path}')
    return True


# --------------------------------------------------------------------------- #
#  版本
# --------------------------------------------------------------------------- #
def bump_version(path):
    today = datetime.date.today().strftime('%Y%m%d')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    m = re.match(r'v?(\d{8})(?:\.(\d+))?$', str(data.get('version', '')))
    build = int(m.group(2) or 0) + 1 if (m and m.group(1) == today) else 0
    new = f'v{today}' if build == 0 else f'v{today}.{build}'
    data['version'] = new
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return new


def numeric_version(disp):
    m = re.match(r'v?(\d{4})(\d{2})(\d{2})(?:\.(\d+))?$', disp)
    if not m:
        raise SystemExit(f'version.json 版本格式無法解析: {disp!r}')
    return f'{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3))}.{int(m.group(4) or 0)}'


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def find_icon():
    """沿用既有的 realtek.ico (不再依賴 Pillow); 找不到就不帶圖示。"""
    for cand in (os.path.join(_here, 'realtek.ico'),
                 os.path.join(_here, '..', 'DV_Utility', 'realtek.ico'),
                 os.path.join(_here, '..', 'dv_util_resource', 'realtek.ico')):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def main():
    app_version = bump_version(_verfile)
    num_version = numeric_version(app_version)
    print(f'進版 -> {app_version}  (Nuitka 數字版 {num_version})')
    print(f'解壓快取路徑 (執行期): %LOCALAPPDATA%\\Realtek\\WebUtility\\{num_version}')

    icon = find_icon()
    cmd = [
        sys.executable, '-m', 'nuitka',
        '--mode=onefile',
        '--enable-plugin=tk-inter',                      # agent 的原生選資料夾對話框
        '--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}',
        '--onefile-cache-mode=cached',
        '--company-name=Realtek',
        '--product-name=WebUtility',
        f'--product-version={num_version}',
        f'--file-version={num_version}',
        f'--include-data-files={_web}=web/index.html',
        f'--include-data-files={_verfile}=version.json',
        '--assume-yes-for-downloads',
        '--output-dir=dist',
        f'--output-filename={OUTPUT_NAME}.exe',
        '--remove-output',
    ]
    if icon:
        cmd.append(f'--windows-icon-from-ico={icon}')
    if not DEBUG_BUILD:
        cmd.append('--windows-console-mode=disable')
    cmd.append(_script)

    subprocess.run(cmd, check=True)

    # -- sign (選配; 未設定憑證則略過) --------------------------------------
    built_exe = os.path.join('dist', f'{OUTPUT_NAME}.exe')
    sign_exe(built_exe)

    # -- package ------------------------------------------------------------
    shutil.rmtree(OUTPUT_NAME, ignore_errors=True)
    os.makedirs(OUTPUT_NAME, exist_ok=True)
    shutil.copy2(built_exe, os.path.join(OUTPUT_NAME, f'{OUTPUT_NAME}.exe'))
    shutil.copy2(built_exe, f'{OUTPUT_NAME}.exe')

    # -- zip ----------------------------------------------------------------
    zip_path = f'{OUTPUT_NAME}.zip'
    shutil.make_archive(OUTPUT_NAME, 'zip', root_dir=OUTPUT_NAME, base_dir='.')

    # -- 發佈資訊 (供 IT / 上傳腳本使用) ------------------------------------
    publish = {
        'version': app_version,
        'zip_url': GCS_ZIP_URL,
        'size':    os.path.getsize(zip_path),
        'sha256':  sha256_of(zip_path),
    }
    with open(_pubfile, 'w', encoding='utf-8') as f:
        json.dump(publish, f, ensure_ascii=False, indent=2)
        f.write('\n')

    print('完成:', f'{OUTPUT_NAME} {app_version}',
          '->', f'{OUTPUT_NAME}.exe / {OUTPUT_NAME}.zip / publish_version.json')


if __name__ == '__main__':
    main()
