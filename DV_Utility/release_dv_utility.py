import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

from PIL import Image

# ---------------------------------------------------------------------------
#  Nuitka onefile 打包 + 「解壓快取」 + 自動進版
#
#  進版: 版本號定義在 version.json。每次跑這支 release.py 會「先進版、再打包」:
#        日期制 vYYYYMMDD + 當日流水號 (同日重複 release 加 .1/.2 ...; 隔天重置)。
#
#  圖示: 單一來源是 realtek.png (1024x1024)。build 時即時由 png 產生多解析度 .ico
#        給 Windows exe 用, 完成後刪掉, 不再維護獨立的 realtek.ico。
#
#  解壓快取: Nuitka onefile + 靜態 --onefile-tempdir-spec ({VERSION} 帶到路徑)
#            -> cache-mode=cached: 同版本只解壓一次、之後重用 -> 啟動近乎即時。
#            換版本 -> {VERSION} 改變 -> 解到新資料夾, 不會用到舊版殘留。
#
#  注意: 機器上若無 MSVC, Nuitka 首次編譯會自動下載 MinGW64 (數百 MB)。
# ---------------------------------------------------------------------------

OUTPUT_NAME = 'DV_Utility'
UPDATER_NAME = 'dv_updater'

# 更新檢查/下載: 發佈端上傳到 GCS 的 zip 公開網址 (dv_updater.exe 會抓它)。
GCS_ZIP_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/DVUtility/DV_Utility.zip'

_here    = os.path.dirname(os.path.abspath(__file__))
_png     = os.path.join(_here, 'realtek.png')
_script  = os.path.join(_here, 'dv_utility.py')
_verfile = os.path.join(_here, 'version.json')
_pubfile = os.path.join(_here, 'publish_version.json')   # 上傳成 GCS 的 version.json
_updater_script = os.path.join(_here, 'dv_updater.py')

# Set DEBUG_BUILD=True to keep the console window for troubleshooting.
DEBUG_BUILD = False


def bump_version(path):
    """自動進版: 日期制 vYYYYMMDD + 當日流水號, 回寫 version.json 並回傳新版本字串。

    同一天重複 release -> 加 .1/.2 ...; 隔天 -> 重置為當天日期 (build 0)。
    """
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
    """vYYYYMMDD[.N] -> 'YYYY.M.D.N' (Nuitka product/file-version + cache {VERSION})。"""
    m = re.match(r'v?(\d{4})(\d{2})(\d{2})(?:\.(\d+))?$', disp)
    if not m:
        raise SystemExit(f'version.json 版本格式無法解析: {disp!r}')
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f'{y}.{mo}.{d}.{int(m.group(4) or 0)}'


def sha256_of(path):
    """檔案 sha256 (供 manifest 記錄, dv_updater.exe 下載後比對完整性)。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(app_version, zip_path, out_path=None):
    """依「當下的 zip_path」算出 sha256/size, 寫出 GCS manifest (publish_version.json)。

    ★ sha256 是對 zip_path 當下內容計算的。CTC 簽章是「build 後手動送簽 → 取回已簽 exe →
      重打包 zip」的事後流程, 會改變 zip 內容; 因此**簽章重打包後必須再跑一次本函式**
      (見 --manifest-only), 對「最終已簽的 zip」重算, 否則 updater 的 sha256 檢查會失敗。
    """
    publish = {
        'version':      app_version,
        'zip_url':      GCS_ZIP_URL,
        'size':         os.path.getsize(zip_path),
        'sha256':       sha256_of(zip_path),
        'release_note': os.environ.get('DVUTIL_RELEASE_NOTE', ''),
    }
    with open(out_path or _pubfile, 'w', encoding='utf-8') as f:
        json.dump(publish, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return publish


def make_ico_from_png(png_path, ico_path):
    """由 realtek.png 產生多解析度 .ico (圖示單一來源 = png, 不另存 realtek.ico)。"""
    img = Image.open(png_path).convert('RGBA')
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico_path, format='ICO', sizes=sizes)


# ---------------------------------------------------------------------------
#  簽章 (Authenticode) — 選配, 全部由環境變數帶入, 不把憑證/密碼寫進 repo。
#
#    啟用: 設定 DVUTIL_SIGN=1 (用憑證庫自動挑) 或直接給下列任一憑證來源。
#    憑證來源 (擇一, 優先序): PFX 檔 > 憑證庫 subject 名稱 > /a 自動挑選。
#      DVUTIL_SIGN_PFX / DVUTIL_SIGN_PFX_PASSWORD   (PFX 檔 + 密碼)
#      DVUTIL_SIGN_SUBJECT                          (憑證庫, signtool /n)
#    時間戳: DVUTIL_SIGN_TS_URL (RFC-3161), 預設 DigiCert。
#    signtool: SIGNTOOL 指定完整路徑; 否則自 PATH / Windows SDK 自動尋找。
#
#    ★ 未設定任何憑證來源, 或找不到 signtool -> 印警告並略過 (產出未簽章 build),
#      發佈流程完全不受影響。等 Realtek 取得 code-signing 憑證後, 只需帶入上述環境
#      變數即可自動簽章 (簽章是解 SentinelOne 誤判的長期解: 有簽發者信譽後,
#      IT 可改用「依簽發者憑證」排除, 不必逐版加 hash)。
# ---------------------------------------------------------------------------
_DEFAULT_SIGN_TS_URL = 'http://timestamp.digicert.com'


def _find_signtool():
    """找出 signtool.exe: 先看 SIGNTOOL 環境變數, 再 PATH, 最後掃 Windows SDK (挑最新版)。"""
    override = os.environ.get('SIGNTOOL')
    if override and os.path.isfile(override):
        return override
    on_path = shutil.which('signtool') or shutil.which('signtool.exe')
    if on_path:
        return on_path
    best = None       # (version_tuple, path)
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
    """回傳 (signtool 憑證參數, 人可讀描述); 依 PFX > subject > /a 優先序。"""
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
    """對 exe 做 Authenticode 簽章 + RFC-3161 時間戳, 再驗證。

    由環境變數啟用 (見上方說明)。未設定憑證來源 / 找不到 signtool -> 印警告並略過,
    產出為未簽章 build, 不中斷發佈流程。回傳 True 表示有簽章。
    """
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


def _resolve_ctc_params(hsm_dir=None):
    """把 DV_Utility 既有的 DVUTIL_HSM_* env 橋接成新版 ctc_sign 需要的參數。

    新版 ctc_sign 讀 HSM_*/參數 (不再讀 DVUTIL_HSM_*)。這裡集中解析一次, 供送簽前置
    檢查 (_check_ctc_prereqs) 與實際簽署 (_sign_both) 共用, 兩邊看到的設定必然一致。
    hsm_dir 可注入 (測試用), 預設為本檔同層的 hsm/。回傳 dict:
    hsm_dir / hsm_cli / config / server / user / token / uuid。

    認證擇一: config 設定檔 或 server+token (user 選配) — 與舊 _global_flags 同義。
    hsm_cli / config 未由 env 指定時, 回退到 hsm/ 內自帶檔 (存在才回退, 否則留 None
    交給 ctc_sign 自動探索同層/PATH)。
    """
    if hsm_dir is None:
        hsm_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hsm')
    server = os.environ.get('DVUTIL_HSM_SERVER')
    user   = os.environ.get('DVUTIL_HSM_USER')
    token  = os.environ.get('DVUTIL_HSM_TOKEN')
    hsm_cli = os.environ.get('DVUTIL_HSM_CLI')
    if not hsm_cli:                              # 未指定 -> 用 hsm/ 內自帶; 沒有再交給 ctc_sign 自動找 (同層/PATH)
        cand = os.path.join(hsm_dir, 'hsm-cli-windows-x64.exe')
        hsm_cli = cand if os.path.isfile(cand) else None
    config = os.environ.get('DVUTIL_HSM_CONFIG')
    if not config and not (server and token):   # 認證擇一: 設定檔 或 server+token
        cand = os.path.join(hsm_dir, 'hsm.config.yaml')
        config = cand if os.path.isfile(cand) else None
    uuid = os.environ.get('DVUTIL_HSM_UUID') or os.environ.get('HSM_UUID')
    return {'hsm_dir': hsm_dir, 'hsm_cli': hsm_cli, 'config': config,
            'server': server, 'user': user, 'token': token, 'uuid': uuid}


def _check_ctc_prereqs(hsm_dir=None):
    """CTC 送簽前置檢查 (fail-fast): 在耗時的 Nuitka build 之前先確認 hsm-cli / uuid /
    認證來源齊全, 缺任一項就中止 (SystemExit) 並一次列出所有缺項。

    目的: 避免白 build 一整輪 (甚至白進一次版) 才在送簽當下發現設定漏了。齊全時印一行
    提示 (送簽會先跑 OPSWAT 預檢) 後正常返回。hsm_dir 可注入 (測試用)。
    """
    import ctc_sign
    p = _resolve_ctc_params(hsm_dir)
    problems = []
    try:
        ctc_sign._find_cli(p['hsm_cli'])
    except ctc_sign.CtcError as e:
        problems.append(f'找不到 hsm-cli: {e}')
    if not p['uuid']:
        problems.append('缺簽章 uuid (請設 DVUTIL_HSM_UUID 或 HSM_UUID)')
    if not (p['config'] or (p['server'] and p['token'])):
        problems.append('缺 CTC 認證: 需 hsm/hsm.config.yaml, 或 DVUTIL_HSM_SERVER + DVUTIL_HSM_TOKEN')
    if problems:
        raise SystemExit('CTC 送簽前置檢查未過 (build 前中止):\n  - ' + '\n  - '.join(problems))
    print('CTC 送簽前置檢查通過 (送簽時會先跑 OPSWAT 預檢, 再送 hsm-cli; 半自動流程可能需 FEDEX 核准)')


def _sign_both(built_exe, built_updater, sign_ctc):
    """簽兩顆 exe: sign_ctc=True 走 CTC (hsm-cli, 半自動; 送簽前先跑 OPSWAT 預檢,
    再等主管 FEDEX 核准); 否則走既有 signtool sign_exe (未設憑證則略過)。"""
    if not sign_ctc:
        sign_exe(built_updater)
        sign_exe(built_exe)
        return
    import ctc_sign
    p = _resolve_ctc_params()   # 與 build 前 _check_ctc_prereqs 用同一份解析結果
    # 把內建的送簽前 OPSWAT 預檢指向 hsm/ (否則預檢只會找 ctc_sign.py 同層 = repo root)。
    # 存在才指過去 (指向不存在的路徑會擋掉 ctc_sign 自身的同層自動探索; 沒放檔時保持
    # 不設 -> 預檢自動略過 = fail-open)。
    fs_cli = os.path.join(p['hsm_dir'], 'fsanitize-windows-x64.exe')
    fs_cfg = os.path.join(p['hsm_dir'], 'file-sanitizer.config.yaml')
    if os.path.isfile(fs_cli):
        os.environ.setdefault('FSANITIZE_CLI', fs_cli)
    if os.path.isfile(fs_cfg):
        os.environ.setdefault('FSANITIZE_CONFIG', fs_cfg)
    for exe in (built_updater, built_exe):   # 更新器少改, 但一次發佈仍一起簽較單純
        print(f'CTC 簽署 {exe} … (送簽前先跑 OPSWAT 預檢; 送簽後如需核准請至 FEDEX)')
        tmp = exe + '.signed'
        ctc_sign.sign_file(exe, out_path=tmp, uuid=p['uuid'], hsm_cli=p['hsm_cli'],
                           config=p['config'], server=p['server'], user=p['user'],
                           token=p['token'])
        os.replace(tmp, exe)
        print(f'CTC 簽署完成 -> {exe}')


def main(sign_ctc=True):
    # -- CTC 送簽前置檢查 (預設送簽; 在進版/build 之前 fail-fast) --------------
    #    缺 hsm-cli / uuid / 認證就中止, 免得白 build 一輪 (甚至白進一次版)。
    #    走 --no-sign-ctc 時跳過 (退回 signtool, 未設憑證則產出未簽 build)。
    if sign_ctc:
        _check_ctc_prereqs()

    # -- 進版 (先進版, 再打包) ------------------------------------------------
    app_version = bump_version(_verfile)          # 例: v20260604
    num_version = numeric_version(app_version)     # 例: 2026.6.4.0
    print(f'進版 -> {app_version}  (Nuitka 數字版 {num_version})')
    print(f'解壓快取路徑 (執行期): %LOCALAPPDATA%\\Realtek\\DV_Utility\\{num_version}')

    # 由 png 即時產生 exe 圖示, build 後刪除
    tmp_ico = os.path.join(_here, '_build_icon.ico')
    make_ico_from_png(_png, tmp_ico)

    # -- build (Nuitka onefile + 快取解壓) ------------------------------------
    cmd = [
        sys.executable, '-m', 'nuitka',
        '--mode=onefile',
        '--enable-plugin=pyside6',
        '--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}',
        '--onefile-cache-mode=cached',
        '--company-name=Realtek',
        '--product-name=DV_Utility',
        f'--product-version={num_version}',
        f'--file-version={num_version}',
        f'--windows-icon-from-ico={tmp_ico}',
        f'--include-data-files={_png}=realtek.png',
        f'--include-data-files={_verfile}=version.json',   # 把進版後的 version.json 一起打包
        '--assume-yes-for-downloads',
        '--output-dir=dist',
        f'--output-filename={OUTPUT_NAME}.exe',
        '--remove-output',
    ]
    if not DEBUG_BUILD:
        cmd.append('--windows-console-mode=disable')
    cmd.append(_script)

    try:
        subprocess.run(cmd, check=True)
    finally:
        if os.path.exists(tmp_ico):
            os.remove(tmp_ico)

    # -- sign (選配) ---------------------------------------------------------
    #    在下面 copy / zip 之前簽 dist 的 built_exe -> 所有派送副本 (含 zip 內的 exe)
    #    都帶同一份簽章。未設定憑證則自動略過 (見 sign_exe)。
    built_exe = os.path.join('dist', f'{OUTPUT_NAME}.exe')

    # -- build 更新器 (dv_updater.exe; Nuitka onefile, 溫和版) --------------
    #    主 build 的 finally 已刪掉 tmp_ico, 這裡若不存在就重建。
    if not os.path.exists(tmp_ico):
        make_ico_from_png(_png, tmp_ico)
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
    try:
        subprocess.run(updater_cmd, check=True)
    finally:
        if os.path.exists(tmp_ico):
            os.remove(tmp_ico)
    built_updater = os.path.join('dist', f'{UPDATER_NAME}.exe')

    # 簽章: 預設走 CTC (hsm-cli); --no-sign-ctc 則退回 signtool sign_exe (未設憑證則略過)。
    _sign_both(built_exe, built_updater, sign_ctc)

    # -- package -------------------------------------------------------------
    shutil.rmtree(OUTPUT_NAME, ignore_errors=True)
    os.makedirs(OUTPUT_NAME, exist_ok=True)
    shutil.copy2(built_exe, os.path.join(OUTPUT_NAME, f'{OUTPUT_NAME}.exe'))
    shutil.copy2(built_exe, f'{OUTPUT_NAME}.exe')
    shutil.copy2(built_updater, os.path.join(OUTPUT_NAME, f'{UPDATER_NAME}.exe'))
    shutil.copy2(built_updater, f'{UPDATER_NAME}.exe')

    # -- zip (含 DV_Utility.exe + dv_updater.exe) ----------------------------
    zip_path = f'{OUTPUT_NAME}.zip'
    shutil.make_archive(OUTPUT_NAME, 'zip', root_dir=OUTPUT_NAME, base_dir='.')

    # -- 發佈資訊 (主程式 update_check 比版本; dv_updater.exe 下載 + 驗 sha256) --
    #    ⚠ 此處對「未簽 zip」算 sha256。若之後走 CTC 簽章重打包, 務必再跑 --manifest-only
    #      對最終已簽 zip 重算, 否則 updater 會因 sha256 不符而拒絕更新。
    write_manifest(app_version, zip_path)

    print('完成:', f'{OUTPUT_NAME} {app_version}',
          '->', f'{OUTPUT_NAME}.exe / {UPDATER_NAME}.exe / {OUTPUT_NAME}.zip / publish_version.json')


def _regen_manifest_only():
    """不重建; 依現有 version.json 版本 + 現有 DV_Utility.zip 重算 manifest。

    用於 CTC 簽章 + 重打包「最終已簽 zip」之後, 讓 publish_version.json 的 sha256/size 對得上。
    """
    with open(_verfile, encoding='utf-8') as f:
        app_version = json.load(f)['version']
    pub = write_manifest(app_version, f'{OUTPUT_NAME}.zip')
    print(f'manifest 重算完成 (對現有 {OUTPUT_NAME}.zip): {pub["version"]}  sha256={pub["sha256"]}')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='DV_Utility release / manifest 工具')
    ap.add_argument('--manifest-only', action='store_true',
                    help='不重建; 只依現有 DV_Utility.zip 重算 manifest (簽章重打包後用)')
    # CTC 送簽現為預設: build 前先做前置檢查 (缺 hsm-cli/uuid/認證即中止), 送簽時先跑
    # OPSWAT 預檢, 再送 hsm-cli (半自動, 可能需主管 FEDEX 核准)。需 DVUTIL_HSM_* 設定。
    ap.add_argument('--no-sign-ctc', dest='sign_ctc', action='store_false',
                    help='不走 CTC 送簽; 退回既有 signtool 簽章 (未設 DVUTIL_SIGN* 憑證則產出未簽 build)')
    ap.add_argument('--sign-ctc', dest='sign_ctc', action='store_true',
                    help='(相容保留, 現已是預設) build 後用 CTC (hsm-cli) 簽兩顆 exe')
    ap.set_defaults(sign_ctc=True)
    ns = ap.parse_args()
    if ns.manifest_only:
        _regen_manifest_only()
    else:
        main(sign_ctc=ns.sign_ctc)
