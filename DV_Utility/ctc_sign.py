#!/usr/bin/env python3
"""ctc_sign — Realtek CTC「數位憑證簽署服務」(hsm-cli) 的 code-signing 小工具。

可當函式庫 import, 也可當 CLI 跑; 讓任何專案的 release + upload 流程在打包前自動對
exe/dll/... 做 Authenticode 簽章。純標準庫, 無第三方相依。

半自動: 依帳號/政策, `start` 之後可能要主管在 FEDEX 表單核准才會真正簽。本工具會輪詢 status
直到 SUCCESS (故會「阻塞等核准」), 因此無法完全無人值守。若帳號已免核准, status 會直接
INIT→OPSWAT→HSM→SUCCESS, 幾分鐘內完成。

需求:
  - hsm-cli 執行檔 (Realtek DevOps portal 下載; 見 README)
  - 認證: hsm.config.yaml (含 SERVER/USER_NAME/USER_TOKEN), 或直接給 server/user/token
  - 簽章演算法的 hsmSettingUuid (跑 `settings` 子指令取得; 選 "Microsoft Code Signing" 那筆)

設定來源優先序: 函式/CLI 參數 > 環境變數。環境變數:
    HSM_CLI, HSM_CONFIG, HSM_UUID, HSM_SERVER, HSM_USER, HSM_TOKEN

CLI:
    python ctc_sign.py settings                          # 列出可用演算法 (找 uuid)
    python ctc_sign.py sign --file app.exe [--out signed.exe] [--uuid <uuid>]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile


class CtcError(RuntimeError):
    """CTC 簽署流程的任何失敗。"""


class PresignError(CtcError):
    """The bundled pre-sign OPSWAT check rejected the file (fast-fail, before the
    slow FEDEX/HSM flow).

    A subclass of :class:`CtcError`, so callers that only catch ``CtcError`` keep
    treating a rejection as a signing failure (abort), while callers that want to
    react to a pre-check rejection specifically can catch ``PresignError``.
    """


_DONE_WORD = 'SUCCESS'
_FAIL_WORDS = ('FAIL', 'ERROR', 'REJECT', 'DENY', 'DENIED', 'CANCEL', 'ABORT')

# 本模組所在資料夾 —— 設計成可自帶 hsm-cli 執行檔與 hsm.config.yaml (見 README);
# 未明確指定時會優先用同資料夾內的那兩個檔, 複製整包到別專案即可即插即用。
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_NAME = 'hsm.config.yaml'
_CLI_NAMES = ('hsm-cli-windows-x64.exe', 'hsm-cli.exe', 'hsm-cli')


# --------------------------------------------------------------------------- #
#  輸出解析 (hsm-cli 一律輸出 UTF-8 JSON; 保留 regex 後備)
# --------------------------------------------------------------------------- #
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
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]
    return None


def parse_create_id(output):
    """從 `file-sign create` 輸出解析 file-sign id。JSON 的 "id" 優先, 後備 regex。

    觀察到的 id 形式為 "YYYY.MM.DD-xxxxx" (非 UUID)。
    """
    rec = _first_record(_try_json(output))
    if rec and rec.get('id'):
        return str(rec['id'])
    m = re.search(r'"id"\s*:\s*"([^"]+)"', output)
    if m:
        return m.group(1)
    m = re.search(r'\b\d{4}\.\d{2}\.\d{2}-[0-9a-fA-F]+\b', output)
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


def _find_cli(explicit=None):
    """hsm-cli 路徑: explicit 參數 > HSM_CLI 環境變數 > 同資料夾自帶 > PATH。找不到丟 CtcError。"""
    p = explicit or os.environ.get('HSM_CLI')
    if p:
        if os.path.isfile(p):
            return p
        raise CtcError(f'hsm-cli 路徑不存在: {p}')
    for name in _CLI_NAMES:   # 本工具同資料夾自帶的 hsm-cli
        cand = os.path.join(_MODULE_DIR, name)
        if os.path.isfile(cand):
            return cand
    for name in _CLI_NAMES:
        found = shutil.which(name)
        if found:
            return found
    raise CtcError('找不到 hsm-cli; 放進本資料夾 / PATH, 或設 HSM_CLI')


def _extract_one(zip_path, want_name, dest):
    """從 zip 取出 want_name (比對 basename, 否則取唯一檔案) 寫到 dest; 回傳 dest。"""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith('/')]
        name = next((n for n in names if n.lower().rstrip('/').endswith(want_name.lower())), None)
        if name is None and len(names) == 1:
            name = names[0]
        if name is None:
            raise CtcError(f'簽章 zip 內找不到 {want_name} (內容: {names})')
        with z.open(name) as src, open(dest, 'wb') as dst:
            shutil.copyfileobj(src, dst)
    return dest


# --------------------------------------------------------------------------- #
#  Pre-sign OPSWAT gate (bundled File-Sanitizer fast-fail)
#
#  Runs File-Sanitizer's OPSWAT scan — the SAME engine ctc_sign uses internally,
#  but without the slow FEDEX/HSM flow — on the file about to be signed, at the
#  start of CtcSigner.sign(). A definite reject fail-CLOSES (raises PresignError);
#  an inability to run the scan fails OPEN (signs anyway), so a project that has
#  not placed the fsanitize binary/config keeps signing exactly as before.
# --------------------------------------------------------------------------- #
def classify_presign(verdict):
    """Pure: map a File-Sanitizer OPSWAT ``verdict`` to a pre-sign decision.

    Returns ``None`` when the verdict is exactly ``'pass'`` (proceed to sign);
    otherwise a Chinese reason string to raise as :class:`PresignError`.
    Fail-closed — any non-``'pass'`` verdict (a ``'fail'`` or an unexpected
    value leaking out of the scanner) is a rejection.
    """
    if verdict == 'pass':
        return None
    return f'File-Sanitizer OPSWAT 預檢未通過 (verdict={verdict})'


def _skip_presign(presign=True):
    """Whether to skip the pre-sign OPSWAT check.

    Skipped when the caller passes ``presign=False`` or the ``CTC_SKIP_PRESIGN``
    env var is set to a truthy value (``1``/``true``/``yes``/``on``). Unset or
    ``0`` means run the check (the default).
    """
    if presign is False:
        return True
    val = os.environ.get('CTC_SKIP_PRESIGN', '0').strip().lower()
    return val not in ('', '0', 'false', 'no', 'off')


def run_presign_check(file_path, scan_fn, log=print):
    """Run the pre-sign OPSWAT check via ``scan_fn(file_path) -> 'pass'/'fail'``.

    Raises :class:`PresignError` when OPSWAT rejects the file — fail-closed on a
    definite verdict (see :func:`classify_presign`). Fails *open* (logs a notice
    and returns) when the scan cannot run at all — ``FsError``: missing fsanitize
    binary/config, upload error, timeout, unreachable service — so a project that
    has not placed the fsanitize files keeps signing exactly as before.
    """
    try:
        from ctc_sign import file_sanitizer
    except ImportError:   # run as a script from inside ctc_sign/
        import file_sanitizer
    try:
        verdict = scan_fn(file_path)
    except file_sanitizer.FsError as e:
        log(f'（略過 OPSWAT 預檢：{e}）')
        return
    reason = classify_presign(verdict)
    if reason:
        raise PresignError(reason)


# --------------------------------------------------------------------------- #
#  簽署器
# --------------------------------------------------------------------------- #
class CtcSigner:
    """封裝一組認證/設定, 對檔案跑完整 CTC 簽署流程。

    參數皆可省略而改用同名環境變數 (HSM_CLI / HSM_CONFIG / HSM_UUID /
    HSM_SERVER / HSM_USER / HSM_TOKEN)。認證二擇一: config 檔 或 server+user+token。
    """

    def __init__(self, hsm_cli=None, config=None, server=None, user=None, token=None, uuid=None):
        self.cli = _find_cli(hsm_cli)
        self.config = config or os.environ.get('HSM_CONFIG')
        self.server = server or os.environ.get('HSM_SERVER')
        self.user = user or os.environ.get('HSM_USER')
        self.token = token or os.environ.get('HSM_TOKEN')
        self.uuid = uuid or os.environ.get('HSM_UUID')
        # 沒指定認證來源時, 用同資料夾自帶的 hsm.config.yaml
        if not self.config and not (self.server and self.token):
            cand = os.path.join(_MODULE_DIR, _CONFIG_NAME)
            if os.path.isfile(cand):
                self.config = cand

    def _flags(self):
        flags = []
        if self.config:
            flags += ['-c', self.config]
        if self.server:
            flags += ['--server', self.server]
        if self.user:
            flags += ['--user-name', self.user]
        if self.token:
            flags += ['--user-token', self.token]
        return flags

    def _run(self, args):
        """執行 hsm-cli; 回傳 (returncode, stdout+stderr)。UTF-8 解碼 (別用 text= 走 cp950)。"""
        cmd = [self.cli] + self._flags() + list(args)
        proc = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace')
        return proc.returncode, (proc.stdout or '') + (proc.stderr or '')

    def list_settings(self):
        """`setting list` -> 解析後的 JSON (list of dict); 每筆含 hsmSettingUuid/signType/... 。"""
        rc, out = self._run(['setting', 'list'])
        if rc != 0:
            raise CtcError(f'setting list 失敗 (rc={rc}):\n{out}')
        return _try_json(out) or []

    def _presign_opswat(self, file_path, log=print):
        """Glue for :func:`run_presign_check`: scan ``file_path`` with the
        bundled File-Sanitizer scanner (fsanitize binary/config auto-discovered
        beside this package). Verified manually in a real release, like the
        signing subprocess path itself.
        """
        try:
            from ctc_sign import file_sanitizer
        except ImportError:   # run as a script from inside ctc_sign/
            import file_sanitizer

        def _scan(path):
            return file_sanitizer.Sanitizer().scan(path, log=log)

        run_presign_check(file_path, _scan, log=log)

    def sign(self, file_path, out_path=None, uuid=None, description=None,
             poll_interval=15, timeout=1800, log=print, presign=True):
        """對 file_path 走完整簽署流程, 回傳「已簽檔」路徑 (out_path, 預設原路徑+'.signed')。

        會在 status 輪詢處阻塞等核准 (逾時 timeout 秒丟 CtcError, 可日後用印出的 id 續 download)。

        送簽前會先跑一次 File-Sanitizer OPSWAT 預檢 (run_presign_check): OPSWAT 否決丟
        PresignError (在慢速 FEDEX/HSM 前快速失敗); 預檢跑不動則放行照簽。presign=False 或
        環境變數 CTC_SKIP_PRESIGN=1 可略過預檢。
        """
        file_path = os.path.abspath(file_path)
        uuid = uuid or self.uuid
        if not uuid:
            raise CtcError('缺 hsmSettingUuid; 傳 uuid= 或設 HSM_UUID (由 list_settings()/`settings` 取得)')
        out_path = out_path or (file_path + '.signed')
        desc = description or f'code-sign {os.path.basename(file_path)}'

        # Fast-fail OPSWAT pre-check on the file before the slow FEDEX/HSM flow
        # (see run_presign_check): a definite reject raises PresignError; an
        # inability to scan fails open (signs anyway). Opt out via presign=False
        # or CTC_SKIP_PRESIGN=1.
        if not _skip_presign(presign):
            self._presign_opswat(file_path, log=log)

        rc, out = self._run(['file-sign', 'create', '--uuid', uuid, '--description', desc])
        log(out.strip())
        if rc != 0:
            raise CtcError(f'file-sign create 失敗 (rc={rc})')
        sid = parse_create_id(out)
        log(f'file-sign id = {sid}')

        rc, out = self._run(['file-sign', 'upload', '--id', sid, '--file', file_path])
        log(out.strip())
        if rc != 0:
            raise CtcError(f'file-sign upload 失敗 (rc={rc})')

        rc, out = self._run(['file-sign', 'start', '--id', sid])
        log(out.strip())
        if rc != 0:
            raise CtcError(f'file-sign start 失敗 (rc={rc})')

        log('已送簽 -> (若需要) 請主管到 FEDEX 表單核准; 輪詢 status 中 …')
        waited = 0
        while True:
            rc, out = self._run(['file-sign', 'status', '--id', sid])
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

        zip_out = file_path + '.__signed.zip'
        rc, out = self._run(['file-sign', 'download', '--id', sid, '--output', zip_out])
        log(out.strip())
        if rc != 0:
            raise CtcError(f'file-sign download 失敗 (rc={rc})')

        _extract_one(zip_out, os.path.basename(file_path), out_path)
        try:
            os.remove(zip_out)
        except OSError:
            pass
        log(f'已取回簽章版 -> {out_path}')
        return out_path


def sign_file(file_path, out_path=None, uuid=None, hsm_cli=None, config=None,
              server=None, user=None, token=None, poll_interval=15, timeout=1800,
              description=None, log=print, presign=True):
    """便利函式: 建 CtcSigner 並簽一個檔, 回傳已簽檔路徑。

    presign=False (或 CTC_SKIP_PRESIGN=1) 可略過送簽前的 OPSWAT 預檢。
    """
    signer = CtcSigner(hsm_cli=hsm_cli, config=config, server=server, user=user,
                       token=token, uuid=uuid)
    return signer.sign(file_path, out_path=out_path, description=description,
                       poll_interval=poll_interval, timeout=timeout, log=log,
                       presign=presign)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _cli(argv=None):
    ap = argparse.ArgumentParser(prog='ctc_sign', description='Realtek CTC (hsm-cli) code signing')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_set = sub.add_parser('settings', help='列出可用演算法 (找 hsmSettingUuid)')
    p_set.add_argument('--cli')
    p_set.add_argument('--config')

    p_sign = sub.add_parser('sign', help='簽一個檔')
    p_sign.add_argument('--file', required=True)
    p_sign.add_argument('--out', help='已簽檔輸出路徑 (預設: <file>.signed)')
    p_sign.add_argument('--uuid')
    p_sign.add_argument('--cli')
    p_sign.add_argument('--config')
    p_sign.add_argument('--poll', type=int, default=15)
    p_sign.add_argument('--timeout', type=int, default=1800)
    p_sign.add_argument('--skip-presign', action='store_true',
                        help='略過送簽前的 OPSWAT 預檢 (等同 CTC_SKIP_PRESIGN=1)')

    ns = ap.parse_args(argv)
    try:
        if ns.cmd == 'settings':
            signer = CtcSigner(hsm_cli=ns.cli, config=ns.config)
            print(json.dumps(signer.list_settings(), ensure_ascii=False, indent=2))
        elif ns.cmd == 'sign':
            signer = CtcSigner(hsm_cli=ns.cli, config=ns.config, uuid=ns.uuid)
            out = signer.sign(ns.file, out_path=ns.out, poll_interval=ns.poll,
                              timeout=ns.timeout, presign=not ns.skip_presign)
            print(out)
    except CtcError as e:
        print(f'ctc_sign 失敗: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    _cli()
