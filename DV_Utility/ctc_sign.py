"""用 CTC「數位憑證簽署服務」的 CLI (hsm-cli) 對 exe 做 code signing。

把簽署的機械步驟腳本化, 供 release_dv_utility.py 的 --sign-ctc 模式使用:
    create -> upload -> start -> (輪詢 status) -> download -> 解出已簽 exe

★ 半自動, 非全自動: `start` 之後狀態會停在 FEDEX = 直屬主管要在網頁表單核准, 通過後才進
  HSM 真正簽。因此 sign_file() 會在 status 輪詢處「阻塞等主管核准」, 不可能無人值守。

hsm-cli 介面已對照實測確認:
  - 全域: -c <config> 或 --server/--user-name/--user-token
  - hsm-cli setting list                         -> 列出可用演算法 (JSON), 取 hsmSettingUuid
  - hsm-cli file-sign create --uuid <uuid>        -> 建任務 (JSON, 內含 "id")
  - hsm-cli file-sign upload --id <id> --file X
  - hsm-cli file-sign start  --id <id>
  - hsm-cli file-sign status --id <id>            -> JSON, "status": INIT/OPSWAT/FEDEX/HSM/SUCCESS/FAIL
  - hsm-cli file-sign download --id <id> --output <path>   (預設 __signed.zip)
  輸出皆為 JSON; id 形如 "2026.07.16-3cf20" (非 UUID)。

設定 (皆由環境變數帶入, 不寫進 repo; token 是機密, 放 gitignore 的 hsm/):
    DVUTIL_HSM_CLI     hsm-cli 執行檔路徑 (預設: 從 PATH 找)
    DVUTIL_HSM_UUID    Microsoft Code Signing 的 hsmSettingUuid (由 `hsm-cli setting list` 取得)
    認證二擇一:
      DVUTIL_HSM_CONFIG  hsm.config.yaml 路徑 (內含 SERVER/USER_NAME/USER_TOKEN)
      或 DVUTIL_HSM_SERVER / DVUTIL_HSM_USER / DVUTIL_HSM_TOKEN
"""

import json
import os
import re
import shutil
import subprocess
import time
import zipfile


class CtcError(RuntimeError):
    pass


_DONE_WORD = 'SUCCESS'
_FAIL_WORDS = ('FAIL', 'ERROR', 'REJECT', 'DENY', 'DENIED', 'CANCEL', 'ABORT')


def hsm_cli_path():
    """hsm-cli 執行檔路徑: 先 DVUTIL_HSM_CLI, 再 PATH。找不到丟 CtcError。"""
    p = os.environ.get('DVUTIL_HSM_CLI')
    if p:
        if os.path.isfile(p):
            return p
        raise CtcError(f'DVUTIL_HSM_CLI 指到的檔不存在: {p}')
    for name in ('hsm-cli', 'hsm-cli.exe', 'hsm-cli-windows-x64.exe'):
        found = shutil.which(name)
        if found:
            return found
    raise CtcError('找不到 hsm-cli; 請設 DVUTIL_HSM_CLI 指向 hsm-cli 執行檔')


def _global_flags():
    """認證相關的全域旗標 (config 檔或 server/user/token)。"""
    flags = []
    cfg = os.environ.get('DVUTIL_HSM_CONFIG')
    if cfg:
        flags += ['-c', cfg]
    for env, flag in (('DVUTIL_HSM_SERVER', '--server'),
                      ('DVUTIL_HSM_USER', '--user-name'),
                      ('DVUTIL_HSM_TOKEN', '--user-token')):
        v = os.environ.get(env)
        if v:
            flags += [flag, v]
    return flags


def _run(args):
    """執行 hsm-cli <global-flags> <args>; 回傳 (returncode, stdout+stderr)。測試時可 monkeypatch。"""
    cmd = [hsm_cli_path()] + _global_flags() + list(args)
    # hsm-cli 輸出 UTF-8 JSON; 明確以 UTF-8 解碼 (別用 text=True 走系統 cp950 會爆 UnicodeDecodeError)
    proc = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace')
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


def _try_json(s):
    """把 hsm-cli 輸出解析成 JSON (dict/list); 前後夾雜非 JSON 行時抓第一段 {..}/[..]。失敗回 None。"""
    s = (s or '').strip()
    try:
        return json.loads(s)
    except Exception:
        for open_c, close_c in (('[', ']'), ('{', '}')):
            i, j = s.find(open_c), s.rfind(close_c)
            if 0 <= i < j:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    pass
    return None


def _first_record(obj):
    """從 JSON (dict 或 list of dict) 取出單筆 record dict; 否則 None。"""
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]
    return None


def parse_create_id(output):
    """從 `file-sign create` 輸出解析 file-sign id。JSON 的 "id" 優先, 後備 regex。"""
    rec = _first_record(_try_json(output))
    if rec and rec.get('id'):
        return str(rec['id'])
    m = re.search(r'"id"\s*:\s*"([^"]+)"', output)
    if m:
        return m.group(1)
    m = re.search(r'\b\d{4}\.\d{2}\.\d{2}-[0-9a-fA-F]+\b', output)   # 觀察到的 id 形式
    if m:
        return m.group(0)
    raise CtcError('無法從 create 輸出解析出 file-sign id (請核對 hsm-cli 實際輸出):\n' + output)


def classify_status(output):
    """把 `file-sign status` 輸出分類為 'done' | 'failed' | 'pending'。

    JSON 的 "status" 欄優先 (SUCCESS -> done; FAIL/ERROR/REJECT... -> failed; 其餘 pending);
    無 JSON 則對整段文字找關鍵字。INIT/OPSWAT/FEDEX/HSM 皆屬 pending, 續等。
    """
    rec = _first_record(_try_json(output))
    text = str(rec.get('status')) if (rec and rec.get('status') is not None) else output
    up = text.upper()
    if _DONE_WORD in up:
        return 'done'
    if any(w in up for w in _FAIL_WORDS):
        return 'failed'
    return 'pending'


def _extract_signed_exe(zip_path, exe_name, dest):
    """從 __signed.zip 取出已簽 exe (比對檔名, 否則取唯一 .exe) 寫到 dest; 回傳 dest。"""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        name = next((n for n in names if n.lower().rstrip('/').endswith(exe_name.lower())), None)
        if name is None:
            exes = [n for n in names if n.lower().endswith('.exe')]
            if len(exes) == 1:
                name = exes[0]
        if name is None:
            raise CtcError(f'__signed.zip 內找不到 {exe_name} (內容: {names})')
        with z.open(name) as src, open(dest, 'wb') as dst:
            shutil.copyfileobj(src, dst)
    return dest


def sign_file(exe_path, uuid=None, out_path=None, poll_interval=15, timeout=1800, log=print):
    """對 exe_path 走完整 CTC 簽署流程, 回傳「已簽 exe」路徑 (out_path)。

    會在 status 輪詢處阻塞, 等直屬主管於 FEDEX 核准 (逾時 timeout 秒丟 CtcError, 可日後用印出的
    id 續 download)。每步印原始輸出便於除錯。
    """
    exe_path = os.path.abspath(exe_path)
    uuid = uuid or os.environ.get('DVUTIL_HSM_UUID')
    if not uuid:
        raise CtcError('缺 HSM Setting UUID; 設 DVUTIL_HSM_UUID 或傳 uuid (由 `hsm-cli setting list` 取得)')
    out_path = out_path or (exe_path + '.signed')

    rc, out = _run(['file-sign', 'create', '--uuid', uuid,
                    '--description', f'DV_Utility {os.path.basename(exe_path)}'])
    log(out.strip())
    if rc != 0:
        raise CtcError(f'file-sign create 失敗 (rc={rc})')
    sid = parse_create_id(out)
    log(f'file-sign id = {sid}')

    rc, out = _run(['file-sign', 'upload', '--id', sid, '--file', exe_path])
    log(out.strip())
    if rc != 0:
        raise CtcError(f'file-sign upload 失敗 (rc={rc})')

    rc, out = _run(['file-sign', 'start', '--id', sid])
    log(out.strip())
    if rc != 0:
        raise CtcError(f'file-sign start 失敗 (rc={rc})')

    log('已送簽 -> 請直屬主管到 FEDEX 表單核准; 輪詢 status 中 …')
    waited = 0
    while True:
        rc, out = _run(['file-sign', 'status', '--id', sid])
        log(f'[{waited}s] {out.strip()}')
        state = classify_status(out)
        if state == 'done':
            break
        if state == 'failed':
            raise CtcError(f'簽署失敗/被拒 (id={sid}):\n{out}')
        if waited >= timeout:
            raise CtcError(f'等待逾時 {timeout}s (id={sid}); 稍後可用此 id 續 download')
        time.sleep(poll_interval)
        waited += poll_interval

    zip_out = exe_path + '.__signed.zip'
    rc, out = _run(['file-sign', 'download', '--id', sid, '--output', zip_out])
    log(out.strip())
    if rc != 0:
        raise CtcError(f'file-sign download 失敗 (rc={rc})')

    _extract_signed_exe(zip_out, os.path.basename(exe_path), out_path)
    try:
        os.remove(zip_out)
    except OSError:
        pass
    log(f'已取回簽章版 -> {out_path}')
    return out_path
