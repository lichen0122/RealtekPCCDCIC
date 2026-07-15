"""DV WebUtility — 本機代理 (agent)

架構
----
這支程式是一個「只綁 127.0.0.1 的本機 HTTP 代理」。UI 是單檔 ``web/index.html``,由本代理
在 localhost 提供;使用者的動作 (選 project 路徑 / 下載安裝工具 / 啟動工具 / 管理程序) 全部
透過 localhost 的 JSON API 交給本代理執行 —— 因為這些動作 (啟動 exe、寫任意路徑、管理程序)
正是瀏覽器沙箱做不到的。

與舊版 DV_Utility 的關係
------------------------
* UI 改成 web (瀏覽器)。網頁本身不是可執行檔,不會被防毒行為引擎 (SentinelOne) 誤判。
* 只剩這支很小的原生代理需要打包 / 簽章 / 加白名單 (見 README 的 SentinelOne 說明)。
* 沿用同一個安裝目錄 ``~/PCDV`` 與 resource 檔;已安裝的工具與歷史紀錄與 DV_Utility 共用。
* 拿掉了自我更新 (下載 exe + 換檔 + detached 重啟) —— 那是最強的 dropper 誘因;web UI 靠
  reload 就是更新,代理本身改由 IT 部署 / 重新下載即可。

安全
----
* 只綁 127.0.0.1 (loopback),不對外服務。
* 每次啟動產生隨機 token;網頁載入時由本代理注入,之後所有 /api 請求都要附上,擋掉其他
  網頁 / DNS-rebinding 驅動本代理。
* 另檢查 Host header 必須是 127.0.0.1 / localhost。
* 只會啟動「工具清單 (setting.json) 內」的工具 —— client 傳的是工具「名稱」,實際 exe 路徑
  一律由可信的 manifest 解析,絕不接受 client 指定任意路徑。
"""

import itertools
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

import requests

APP_NAME = 'WebUtility'

# 工具清單 (與 DV_Utility 同一份): 名稱 -> 該工具的 version manifest URL。
SETTING_URL = 'https://raw.github.com/lichen0122/RealtekPCCDCIC/main/dv_util_resource/setting.json'

# web UI 單檔:agent 啟動時從 GCS 抓取(遠端→快取→bundled),不需重建 exe 即可更新 UI。
WEB_UI_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/WebUtility/index.html'

# 已發佈的 exe 版本 manifest(upload_to_gcs.py 上傳的 publish_version.json)。用於「檢查更新」。
PUBLISH_VERSION_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/WebUtility/version.json'

_CONNECT_TIMEOUT = 15
_READ_TIMEOUT    = 60
_HTTP_TIMEOUT    = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# 網頁每 5s ping 一次 /api/heartbeat;超過這個秒數沒 ping (通常代表分頁被關) -> 代理自行結束。
_HEARTBEAT_TIMEOUT = 20


# --------------------------------------------------------------------------- #
#  Logging
# --------------------------------------------------------------------------- #
def _get_log_dir():
    log_dir = Path.home() / '.PCDV' / APP_NAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except Exception:
        fallback = Path(tempfile.gettempdir()) / ('PCDV_' + APP_NAME)
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def _setup_logging():
    logger = logging.getLogger(APP_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] (%(threadName)s) %(funcName)s:%(lineno)d - %(message)s')
    try:
        fh = RotatingFileHandler(str(_get_log_dir() / 'web_utility.log'),
                                 maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


log = _setup_logging()


def get_bundled_path(filename):
    """打包後 (Nuitka) 或開發時的資源檔路徑 (version.json / web/index.html)。"""
    if globals().get('__compiled__'):
        return os.path.join(os.path.dirname(__file__), filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def _read_app_version():
    try:
        with open(get_bundled_path('version.json'), encoding='utf-8') as f:
            return json.load(f).get('version', 'vUNKNOWN')
    except Exception:
        log.warning('could not read version.json', exc_info=True)
        return 'vUNKNOWN'


def load_web_ui_html(url, cache_file, bundled_path):
    """解析要供應的 index.html:遠端 → 本機快取 → 打包內建。回傳 (html, source)。

    source: 'remote' | 'cache' | 'bundled' | 'none'。
    比照 Agent.load_catalog() 抓 setting.json 的三層退回;純函式以利單元測試。
    """
    # 1) 遠端
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        html = r.content.decode('utf-8')   # GCS 送 text/html 無 charset;明確以 UTF-8 解碼,避免中文亂碼
        if html.strip():
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    f.write(html)
            except Exception:
                log.exception('failed to cache index.html')
            return html, 'remote'
        log.warning('remote index.html empty; falling back to cache/bundled')
    except Exception:
        log.warning('fetch index.html failed; falling back to cache/bundled', exc_info=True)

    # 2) 本機快取
    try:
        with open(cache_file, encoding='utf-8') as f:
            cached = f.read()
        if cached.strip():
            return cached, 'cache'
    except Exception:
        pass

    # 3) 打包內建
    try:
        with open(bundled_path, encoding='utf-8') as f:
            bundled = f.read()
        if bundled.strip():
            return bundled, 'bundled'
    except Exception:
        pass

    log.exception('no usable index.html (remote + cache + bundled all failed)')
    return None, 'none'


def _parse_version_key(v):
    """'v20260715.2' -> (2026, 7, 15, 2);無法解析回 None。"""
    m = re.match(r'v?(\d{4})(\d{2})(\d{2})(?:\.(\d+))?$', str(v or ''))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))


def is_update_available(current, latest):
    """latest 是否比 current 新(皆為 vYYYYMMDD[.N]);任一無法解析回 False。"""
    ck, lk = _parse_version_key(current), _parse_version_key(latest)
    if ck is None or lk is None:
        return False
    return lk > ck


# --------------------------------------------------------------------------- #
#  Native folder dialog —— 一律在「主執行緒」執行 (Tk 不可在 worker thread 跑)。
#  HTTP handler (worker thread) 需要選資料夾時, 把請求丟進 _dialog_q, 由 main() 的
#  主迴圈取出、跑 Tk、回填結果。
# --------------------------------------------------------------------------- #
_dialog_q: "queue.Queue" = queue.Queue()


def _run_folder_dialog():
    """真正呼叫 Tk 選資料夾 (只在主執行緒被 main loop 呼叫)。回傳路徑字串或 ''。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title='選擇 project 路徑')
        root.update()
        root.destroy()
        return path or ''
    except Exception:
        log.exception('folder dialog failed')
        return ''


def request_folder_dialog(timeout=180):
    """worker thread 呼叫: 排到主執行緒跑資料夾對話框, 等結果回來。"""
    done = threading.Event()
    box = {'path': ''}
    _dialog_q.put((done, box))
    done.wait(timeout=timeout)
    return box['path']


# --------------------------------------------------------------------------- #
#  Agent state
# --------------------------------------------------------------------------- #
class Agent:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.app_version = _read_app_version()

        self.install_dir = str((Path.home() / 'PCDV'))
        os.makedirs(self.install_dir, exist_ok=True)
        self.resource = os.path.join(self.install_dir, 'resource')
        os.makedirs(self.resource, exist_ok=True)
        self.setting_file       = os.path.join(self.resource, 'setting.json')
        self.work_dir_list_file = os.path.join(self.resource, 'work_dir_list.json')
        self.tool_history_file  = os.path.join(self.resource, 'tool_history.json')
        self.web_ui_cache_file  = os.path.join(self.resource, 'ui_cache.html')

        self.catalog = {}                       # 工具名稱 -> manifest url
        self.jobs = {}                          # job_id -> dict
        self.processes = {}                     # proc_id -> {popen, tool, work_dir, label}
        self._job_ids = itertools.count(1)
        self._proc_ids = itertools.count(1)
        self._lock = threading.Lock()
        self.last_heartbeat = time.time()
        self.should_quit = False
        self.web_ui_html = None
        self.web_ui_source = 'none'      # remote | cache | bundled | none

    # -- 工具清單 / 歷史 ---------------------------------------------------- #
    def load_catalog(self):
        """抓遠端 setting.json (存快取);失敗則退回上次快取。"""
        try:
            r = requests.get(SETTING_URL, timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            self.catalog = r.json()
            try:
                with open(self.setting_file, 'w', encoding='utf-8') as f:
                    json.dump(self.catalog, f, ensure_ascii=False, indent=2)
            except Exception:
                log.exception('failed to cache setting.json')
        except Exception:
            log.warning('fetch setting.json failed; falling back to cache', exc_info=True)
            try:
                with open(self.setting_file, encoding='utf-8') as f:
                    self.catalog = json.load(f)
            except Exception:
                log.exception('no usable setting.json (remote + cache both failed)')
                self.catalog = {}
        return list(self.catalog.keys())

    def load_web_ui(self):
        """啟動時抓 web UI,結果存於 self.web_ui_html / self.web_ui_source。"""
        bundled = get_bundled_path(os.path.join('web', 'index.html'))
        self.web_ui_html, self.web_ui_source = load_web_ui_html(
            WEB_UI_URL, self.web_ui_cache_file, bundled)
        log.info('web ui: source=%s (%d bytes)',
                 self.web_ui_source, len(self.web_ui_html or ''))
        return self.web_ui_source

    def check_update(self):
        """比對 GCS 已發佈版本與本 exe 版本,供前端顯示更新提示。抓取/解析失敗則安靜回無更新。"""
        result = {'current': self.app_version, 'latest': None,
                  'update_available': False, 'download_url': None}
        try:
            r = requests.get(PUBLISH_VERSION_URL, timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            info = r.json()
            result['latest'] = info.get('version')
            result['download_url'] = info.get('zip_url')
            result['update_available'] = is_update_available(self.app_version, result['latest'])
        except Exception:
            log.warning('update-check failed', exc_info=True)
        return result

    def get_work_dir_list(self):
        if os.path.exists(self.work_dir_list_file):
            try:
                with open(self.work_dir_list_file, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                log.exception('read work_dir_list failed')
        return []

    def add_work_dir(self, new_dir):
        if not new_dir:
            return self.get_work_dir_list()
        lst = [new_dir] + [d for d in self.get_work_dir_list() if d != new_dir]
        try:
            with open(self.work_dir_list_file, 'w', encoding='utf-8') as f:
                json.dump(lst, f, ensure_ascii=False)
        except Exception:
            log.exception('write work_dir_list failed')
        return lst

    def get_tool_history(self):
        if os.path.exists(self.tool_history_file):
            try:
                with open(self.tool_history_file, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                log.exception('read tool_history failed')
        return ''

    def set_tool_history(self, tool):
        try:
            with open(self.tool_history_file, 'w', encoding='utf-8') as f:
                json.dump(tool, f, ensure_ascii=False)
        except Exception:
            log.exception('write tool_history failed')

    # -- 啟動工具 (下載 -> 解壓 -> 執行), 對照 DV_Utility 的流程 ------------- #
    def start_launch(self, tool, work_dir):
        job_id = str(next(self._job_ids))
        job = {'id': job_id, 'tool': tool, 'work_dir': work_dir,
               'status': 'running', 'phase': 'checking',
               'downloaded': 0, 'total': 0, 'version': '', 'release_note': '',
               'error': '', 'launched': None}
        with self._lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._run_launch, args=(job,), daemon=True).start()
        return job_id

    def _run_launch(self, job):
        tool, work_dir = job['tool'], job['work_dir']
        try:
            murl = self.catalog.get(tool)
            if not murl:
                raise RuntimeError(f'工具不在清單內: {tool}')
            log.info('launch: tool=%s work_dir=%s manifest=%s', tool, work_dir, murl)

            r = requests.get(murl, timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            m = r.json()

            target_directory     = os.path.join(self.install_dir, m['target_directory'])
            current_version_file  = os.path.join(target_directory, m['current_version'])
            exe_name              = m['exe_name']
            newest                = m['version']
            update_info           = m['update_info']
            job['version']        = newest
            job['release_note']   = m.get('release_note', '')

            installed = None
            if os.path.isfile(current_version_file):
                try:
                    with open(current_version_file, encoding='utf-8') as f:
                        installed = json.load(f).get('version')
                except Exception:
                    log.exception('read current_version failed (treated as none)')
            update_required = installed != newest
            log.info('launch: update_required=%s (installed=%s newest=%s)',
                     update_required, installed, newest)

            for info in update_info:
                url        = info['url']
                overwrite  = info['overwrite']
                extract_to = info['extract_to']
                extract_dir = work_dir if extract_to == 'work_dir' else target_directory
                zip_name = url.split('/')[-1]
                dir_name = zip_name[:-4] if zip_name.lower().endswith('.zip') else zip_name

                case1 = update_required and overwrite
                case2 = (not overwrite) and (not os.path.exists(os.path.join(extract_dir, dir_name)))
                if not (case1 or case2):
                    log.info('launch: skip %s (already present / no update)', url)
                    continue

                os.makedirs(extract_dir, exist_ok=True)
                job['phase'] = 'downloading'
                job['downloaded'] = 0
                log.info('launch: downloading %s -> %s', url, extract_dir)
                fd, tmp_zip = tempfile.mkstemp(suffix='.zip')
                os.close(fd)
                try:
                    with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as resp:
                        resp.raise_for_status()
                        try:
                            job['total'] = int(resp.headers.get('content-length') or 0)
                        except (TypeError, ValueError):
                            job['total'] = 0
                        with open(tmp_zip, 'wb') as f:
                            for chunk in resp.iter_content(chunk_size=1 << 16):
                                if chunk:
                                    f.write(chunk)
                                    job['downloaded'] += len(chunk)
                    job['phase'] = 'installing'
                    with zipfile.ZipFile(tmp_zip) as z:
                        z.extractall(extract_dir)
                    log.info('launch: extracted %s', zip_name)
                finally:
                    try:
                        os.remove(tmp_zip)
                    except OSError:
                        pass

            os.makedirs(target_directory, exist_ok=True)
            try:
                with open(current_version_file, 'w', encoding='utf-8') as f:
                    json.dump({'version': newest}, f)
            except Exception:
                log.exception('write current_version failed')

            job['phase'] = 'launching'
            exe_path = os.path.join(target_directory, exe_name)
            log.info('launch: starting %s (work_dir=%s)', exe_path, work_dir)
            proc = subprocess.Popen([exe_path, work_dir], cwd=target_directory)

            proc_id = str(next(self._proc_ids))
            label = f'{tool}  ({work_dir})'
            with self._lock:
                self.processes[proc_id] = {'popen': proc, 'tool': tool,
                                           'work_dir': work_dir, 'label': label}
            self.set_tool_history(tool)
            self.add_work_dir(work_dir)
            job['launched'] = {'id': proc_id, 'pid': getattr(proc, 'pid', None), 'label': label}
            job['phase'] = 'done'
            job['status'] = 'done'
            log.info('launch: done pid=%s tool=%s', getattr(proc, 'pid', '?'), tool)
        except Exception as e:
            log.exception('launch FAILED (tool=%s)', tool)
            job['status'] = 'error'
            job['phase'] = 'error'
            job['error'] = str(e)

    # -- 程序面板 ----------------------------------------------------------- #
    def list_processes(self):
        out = []
        with self._lock:
            items = list(self.processes.items())
        for pid_id, rec in items:
            alive = rec['popen'].poll() is None
            out.append({'id': pid_id, 'label': rec['label'],
                        'pid': getattr(rec['popen'], 'pid', None), 'alive': alive})
        return out

    def close_process(self, proc_id):
        with self._lock:
            rec = self.processes.get(proc_id)
        if not rec:
            return False
        try:
            rec['popen'].terminate()
        except Exception:
            log.exception('terminate failed (id=%s)', proc_id)
        with self._lock:
            self.processes.pop(proc_id, None)
        return True


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    agent: Agent = None          # main() 會設定
    server_version = 'WebUtilityAgent'

    def log_message(self, fmt, *args):     # 靜音預設 stderr log, 改走我們的 logger
        log.debug('http: ' + fmt, *args)

    # -- helpers -- #
    def _host_ok(self):
        host = (self.headers.get('Host') or '').split(':')[0]
        return host in ('127.0.0.1', 'localhost')

    def _authed(self):
        return self.headers.get('X-Auth-Token') == self.agent.token

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return {}

    def _serve_index(self):
        html = self.agent.web_ui_html
        if not html:
            self._send_json(
                {'error': 'index.html not available (remote/cache/bundled all failed)'}, 500)
            return
        html = html.replace('__AGENT_TOKEN__', self.agent.token) \
                   .replace('__APP_VERSION__', self.agent.app_version)
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    # -- routing -- #
    def do_GET(self):
        if not self._host_ok():
            self._send_json({'error': 'forbidden host'}, 403)
            return
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._serve_index()
            return
        if not path.startswith('/api/'):
            self._send_json({'error': 'not found'}, 404)
            return
        if not self._authed():
            self._send_json({'error': 'unauthorized'}, 401)
            return
        a = self.agent
        if path == '/api/state':
            self._send_json({'version': a.app_version,
                             'tools': list(a.catalog.keys()),
                             'tool_history': a.get_tool_history(),
                             'work_dirs': a.get_work_dir_list()})
        elif path == '/api/job':
            from urllib.parse import parse_qs
            jid = (parse_qs(urlparse(self.path).query).get('id') or [''])[0]
            job = a.jobs.get(jid)
            self._send_json(job or {'error': 'no such job'}, 200 if job else 404)
        elif path == '/api/processes':
            self._send_json({'processes': a.list_processes()})
        elif path == '/api/update-check':
            self._send_json(a.check_update())
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_POST(self):
        if not self._host_ok():
            self._send_json({'error': 'forbidden host'}, 403)
            return
        path = urlparse(self.path).path
        if not path.startswith('/api/') or not self._authed():
            self._send_json({'error': 'unauthorized'}, 401)
            return
        a = self.agent
        if path == '/api/heartbeat':
            a.last_heartbeat = time.time()
            self._send_json({'ok': True})
        elif path == '/api/pick-workdir':
            picked = request_folder_dialog()
            work_dirs = a.add_work_dir(picked) if picked else a.get_work_dir_list()
            self._send_json({'path': picked, 'work_dirs': work_dirs})
        elif path == '/api/launch':
            data = self._read_json()
            tool = data.get('tool', '')
            work_dir = data.get('work_dir', '')
            if tool not in a.catalog:
                self._send_json({'error': '未知的工具'}, 400)
                return
            if not (work_dir and os.path.isdir(work_dir)):
                self._send_json({'error': 'work_dir 不存在,請重新選擇 project 路徑'}, 400)
                return
            job_id = a.start_launch(tool, work_dir)
            self._send_json({'job_id': job_id})
        elif path == '/api/close':
            data = self._read_json()
            ok = a.close_process(str(data.get('id', '')))
            self._send_json({'ok': ok})
        elif path == '/api/quit':
            a.should_quit = True
            self._send_json({'ok': True})
        else:
            self._send_json({'error': 'not found'}, 404)


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    log.info('=' * 60)
    agent = Agent()
    log.info('%s agent starting (version=%s, install_dir=%s)',
             APP_NAME, agent.app_version, agent.install_dir)
    agent.load_catalog()
    log.info('catalog: %d tool(s)', len(agent.catalog))
    agent.load_web_ui()

    Handler.agent = agent
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = httpd.server_address[1]
    url = f'http://127.0.0.1:{port}/'
    log.info('serving on %s', url)

    threading.Thread(target=httpd.serve_forever, daemon=True, name='http').start()
    try:
        webbrowser.open(url)
    except Exception:
        log.exception('failed to open browser; 請手動開啟 %s', url)
    print(f'{APP_NAME} agent: {url}  (關閉瀏覽器分頁即會自動結束)')

    # 主迴圈: 跑資料夾對話框 (Tk 需主執行緒) + heartbeat 看門狗。
    while True:
        try:
            done, box = _dialog_q.get(timeout=0.5)
            box['path'] = _run_folder_dialog()
            done.set()
        except queue.Empty:
            pass
        if agent.should_quit:
            log.info('quit requested by UI')
            break
        if time.time() - agent.last_heartbeat > _HEARTBEAT_TIMEOUT:
            log.info('no heartbeat for %ss -> shutting down', _HEARTBEAT_TIMEOUT)
            break

    try:
        httpd.shutdown()
    except Exception:
        pass
    log.info('%s agent stopped', APP_NAME)


if __name__ == '__main__':
    main()
