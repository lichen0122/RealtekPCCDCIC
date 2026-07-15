# WebUtility Remote-HTML UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 WebUtility 的 `agent.exe` 在每次啟動時從 GCS 抓取最新 `index.html`(遠端→本機快取→打包內建 的三層退回),使「重開 exe 即載入新 UI」,且「只改 UI」不需重建/重新部署 exe。

**Architecture:** 新增一個純函式 `load_web_ui_html(url, cache_file, bundled_path)` 負責三層退回並回傳 `(html, source)`;`Agent` 於啟動時呼叫它、把結果存在 `self.web_ui_html`;`_serve_index()` 改供應這份記憶體中的 HTML(token/version 注入時機不變)。`upload_to_gcs.py` 增加上傳 `index.html`,並讓 build 產物「存在才上傳」。

**Tech Stack:** Python 3.12、`requests`(既有)、`http.server`(既有)、`pytest`(僅開發測試用,不打包進 exe)、Nuitka onefile(既有打包)。

**設計文件:** `WebUtility/docs/superpowers/specs/2026-07-15-webutility-remote-html-ui-design.md`

**分支:** `webutility-remote-html-ui`(已建立)。所有 `git add` 只加本計畫指定的檔案 —— 工作區另有不相關的既有變更(`DV_Utility/*`、`WebUtility/version.json`、`WebUtility/newagent-*.json` 金鑰),**絕不可** `git add -A` 或加入那些檔案。

---

## File Structure

| 檔案 | 動作 | 責任 |
|------|------|------|
| `WebUtility/tests/conftest.py` | Create | 讓 `import agent` 在 tests/ 內可用(把 WebUtility/ 加入 sys.path) |
| `WebUtility/tests/test_agent.py` | Create | `load_web_ui_html` 三層退回邏輯的單元測試 |
| `WebUtility/agent.py` | Modify | 新增 `WEB_UI_URL`、`load_web_ui_html()`、`Agent` 的快取路徑與 `load_web_ui()`、`_serve_index()` 改用記憶體 HTML、`main()` 呼叫 `load_web_ui()` |
| `WebUtility/upload_to_gcs.py` | Modify | 上傳 `web/index.html`;build 產物存在才上傳 |
| `WebUtility/README.md` | Modify | 更正「reload/重開即更新」、上傳生效、安全敘述 |
| `WebUtility/requirements.txt` | Modify | 記錄開發測試需 `pytest`(註解形式) |

---

## Task 1: 測試基礎建設 + `load_web_ui_html` 失敗測試(TDD 紅燈)

**Files:**
- Create: `WebUtility/tests/conftest.py`
- Create: `WebUtility/tests/test_agent.py`
- Modify: `WebUtility/requirements.txt`

- [ ] **Step 1: 安裝 pytest 到 .venv(不進 requirements 執行段)**

Run(於 `WebUtility/`):
```
.venv/Scripts/python -m pip install pytest
```
Expected: 安裝成功(`Successfully installed pytest-...`)。

- [ ] **Step 2: 在 requirements.txt 記錄開發測試相依**

於 `WebUtility/requirements.txt` 末尾(第 12 行之後)新增:
```
#
# --- 開發測試 (tests/) ---
#   .venv\Scripts\python -m pip install pytest
```

- [ ] **Step 3: 建立 conftest.py 讓 agent 可被匯入**

Create `WebUtility/tests/conftest.py`:
```python
import os
import sys

# 讓 tests/ 內可以 `import agent`(agent.py 在上一層 WebUtility/)。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 4: 撰寫 4 個失敗測試(涵蓋 remote / cache / bundled / none)**

Create `WebUtility/tests/test_agent.py`:
```python
import agent


class _FakeResp:
    """模擬 requests.get 回傳物件:只需 .raise_for_status() 與 .text。"""
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _boom(*args, **kwargs):
    raise RuntimeError('network down')


def test_remote_success_writes_cache(tmp_path, monkeypatch):
    cache = tmp_path / 'ui_cache.html'
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get',
                        lambda *a, **k: _FakeResp('<html>remote</html>'))

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'remote'
    assert html == '<html>remote</html>'
    assert cache.read_text(encoding='utf-8') == '<html>remote</html>'


def test_remote_fail_uses_cache(tmp_path, monkeypatch):
    cache = tmp_path / 'ui_cache.html'
    cache.write_text('<html>cached</html>', encoding='utf-8')
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get', _boom)

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'cache'
    assert html == '<html>cached</html>'


def test_remote_and_cache_fail_uses_bundled(tmp_path, monkeypatch):
    cache = tmp_path / 'missing_cache.html'      # 不建立 -> 讀取失敗
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get', _boom)

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'bundled'
    assert html == '<html>bundled</html>'


def test_all_fail_returns_none(tmp_path, monkeypatch):
    cache = tmp_path / 'missing_cache.html'
    bundled = tmp_path / 'missing_bundled.html'
    monkeypatch.setattr(agent.requests, 'get', _boom)

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'none'
    assert html is None
```

- [ ] **Step 5: 執行測試確認失敗(函式尚未定義)**

Run(於 `WebUtility/`):
```
.venv/Scripts/python -m pytest tests/ -v
```
Expected: 4 個測試皆 FAIL / ERROR,訊息類似 `AttributeError: module 'agent' has no attribute 'load_web_ui_html'`。

- [ ] **Step 6: Commit**

```bash
git add WebUtility/tests/conftest.py WebUtility/tests/test_agent.py WebUtility/requirements.txt
git commit -m "test: add failing tests for load_web_ui_html three-tier fallback

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 實作 `load_web_ui_html` 與 `Agent` 接線(TDD 綠燈)

**Files:**
- Modify: `WebUtility/agent.py`(新增常數、純函式、Agent 屬性與方法)
- Test: `WebUtility/tests/test_agent.py`(已於 Task 1 建立)

- [ ] **Step 1: 新增 `WEB_UI_URL` 常數**

於 `agent.py` 第 51 行(`SETTING_URL = ...`)之後、第 52 空行前,插入:
```python

# web UI 單檔:agent 啟動時從 GCS 抓取(遠端→快取→bundled),不需重建 exe 即可更新 UI。
WEB_UI_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/WebUtility/index.html'
```

- [ ] **Step 2: 新增純函式 `load_web_ui_html`**

於 `agent.py` `_read_app_version()` 函式結尾(第 117 行 `return 'vUNKNOWN'` 之後、第 118 空行處)之後,新增一個模組層級函式:
```python


def load_web_ui_html(url, cache_file, bundled_path):
    """解析要供應的 index.html:遠端 → 本機快取 → 打包內建。回傳 (html, source)。

    source: 'remote' | 'cache' | 'bundled' | 'none'。
    比照 Agent.load_catalog() 抓 setting.json 的三層退回;純函式以利單元測試。
    """
    # 1) 遠端
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        html = r.text
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                f.write(html)
        except Exception:
            log.exception('failed to cache index.html')
        return html, 'remote'
    except Exception:
        log.warning('fetch index.html failed; falling back to cache/bundled', exc_info=True)

    # 2) 本機快取
    try:
        with open(cache_file, encoding='utf-8') as f:
            return f.read(), 'cache'
    except Exception:
        pass

    # 3) 打包內建
    try:
        with open(bundled_path, encoding='utf-8') as f:
            return f.read(), 'bundled'
    except Exception:
        log.exception('no usable index.html (remote + cache + bundled all failed)')

    return None, 'none'
```

- [ ] **Step 3: `Agent.__init__` 新增快取路徑與狀態**

於 `agent.py` `Agent.__init__`,在 `self.tool_history_file  = os.path.join(self.resource, 'tool_history.json')`(第 168 行)之後新增一行:
```python
        self.web_ui_cache_file  = os.path.join(self.resource, 'ui_cache.html')
```
並在 `self.should_quit = False`(第 177 行)之後新增:
```python
        self.web_ui_html = None
        self.web_ui_source = 'none'      # remote | cache | bundled | none
```

- [ ] **Step 4: `Agent` 新增 `load_web_ui()` 方法**

於 `agent.py` `Agent.load_catalog()` 方法結尾(第 199 行 `return list(self.catalog.keys())`)之後、`get_work_dir_list` 之前,新增方法:
```python

    def load_web_ui(self):
        """啟動時抓 web UI,結果存於 self.web_ui_html / self.web_ui_source。"""
        bundled = get_bundled_path(os.path.join('web', 'index.html'))
        self.web_ui_html, self.web_ui_source = load_web_ui_html(
            WEB_UI_URL, self.web_ui_cache_file, bundled)
        log.info('web ui: source=%s (%d bytes)',
                 self.web_ui_source, len(self.web_ui_html or ''))
        return self.web_ui_source
```

- [ ] **Step 5: 執行測試確認通過**

Run(於 `WebUtility/`):
```
.venv/Scripts/python -m pytest tests/ -v
```
Expected: 4 個測試皆 PASS。

- [ ] **Step 6: Commit**

```bash
git add WebUtility/agent.py
git commit -m "feat: add load_web_ui_html with remote/cache/bundled fallback

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 供應端接線 —— `_serve_index()` 用記憶體 HTML、`main()` 啟動時抓取

**Files:**
- Modify: `WebUtility/agent.py`(`_serve_index()`、`main()`)

此任務改的是整合面(HTTP 供應 + 啟動流程),以 dev 模式實跑驗證(無適合單元測試的介面)。

- [ ] **Step 1: 改寫 `_serve_index()` 使用 `self.agent.web_ui_html`**

於 `agent.py`,將 `_serve_index()` 開頭讀檔區塊(第 411–416 行):
```python
        try:
            with open(get_bundled_path(os.path.join('web', 'index.html')), encoding='utf-8') as f:
                html = f.read()
        except Exception:
            self._send_json({'error': 'index.html not found'}, 500)
            return
```
替換為:
```python
        html = self.agent.web_ui_html
        if not html:
            self._send_json(
                {'error': 'index.html not available (remote/cache/bundled all failed)'}, 500)
            return
```
(其後的 `html.replace('__AGENT_TOKEN__', ...)` 等注入與回應邏輯維持不變。)

- [ ] **Step 2: `main()` 在載入 catalog 後抓取 web UI**

於 `agent.py` `main()`,在
```python
    agent.load_catalog()
    log.info('catalog: %d tool(s)', len(agent.catalog))
```
(第 505–506 行)之後新增一行:
```python
    agent.load_web_ui()
```
(位置在 `webbrowser.open(url)` 之前,確保開瀏覽器時 HTML 已就緒。)

- [ ] **Step 3: dev 模式實跑 —— 驗證 remote 來源**

前置:確認 GCS 上已有 `WebUtility/index.html`(若尚未上傳,先做 Task 4 再回來;或先用 Step 4 的 fallback 驗證)。
Run(於 `WebUtility/`,背景啟動,約 3 秒後看 log):
```
.venv/Scripts/python -c "import agent; a=agent.Agent(); print(a.load_web_ui()); print(len(a.web_ui_html or ''))"
```
Expected: 印出 `remote` 與一個 > 0 的位元組數;`~/PCDV/resource/ui_cache.html` 被寫入。

- [ ] **Step 4: 驗證 fallback → cache → bundled**

Run(於 `WebUtility/`,用壞網址觸發退回;bundled 指向 repo 內 `web/index.html`):
```
.venv/Scripts/python -c "import agent, os; b=os.path.join(os.path.dirname(agent.__file__),'web','index.html'); print(agent.load_web_ui_html('http://127.0.0.1:1/nope', os.path.expanduser('~/PCDV/resource/ui_cache.html'), b)[1])"
```
Expected: 若快取存在印 `cache`;刪掉快取再跑印 `bundled`。兩者皆非 `none`。

- [ ] **Step 5: 端到端頁面驗證(使用 verify 技能或手動)**

Run(於 `WebUtility/`):
```
.venv/Scripts/python agent.py
```
Expected: 自動開瀏覽器到 `http://127.0.0.1:<port>/`,頁面正常載入、版本顯示、工具清單出現;log 有 `web ui: source=...`。確認後關閉瀏覽器分頁,agent 於數秒內自行結束。

- [ ] **Step 6: Commit**

```bash
git add WebUtility/agent.py
git commit -m "feat: serve web UI from startup-loaded html; fetch on launch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `upload_to_gcs.py` —— 上傳 index.html;build 產物存在才上傳

**Files:**
- Modify: `WebUtility/upload_to_gcs.py`

- [ ] **Step 1: 新增 `_upload_if_exists` 並改寫 `__main__`**

於 `upload_to_gcs.py`,將檔尾 `__main__` 區塊(第 37–40 行):
```python
if __name__ == '__main__':
    client = _client()
    upload_file(client, os.path.join(_here, 'WebUtility.zip'), 'WebUtility/WebUtility.zip')
    upload_file(client, os.path.join(_here, 'publish_version.json'), 'WebUtility/version.json')
```
替換為:
```python
def _upload_if_exists(client, source_file, destination_blob):
    if os.path.isfile(source_file):
        upload_file(client, source_file, destination_blob)
    else:
        print(f'略過 (檔案不存在): {source_file}')


if __name__ == '__main__':
    client = _client()
    # UI:一律上傳 —— agent 啟動時抓取,達成「不重建 exe 也能更新 UI」。
    upload_file(client, os.path.join(_here, 'web', 'index.html'), 'WebUtility/index.html')
    # build 產物:存在才上傳(只改 UI 時通常沒有新的 zip/publish_version.json)。
    _upload_if_exists(client, os.path.join(_here, 'WebUtility.zip'), 'WebUtility/WebUtility.zip')
    _upload_if_exists(client, os.path.join(_here, 'publish_version.json'), 'WebUtility/version.json')
```

- [ ] **Step 2: 語法檢查**

Run(於 `WebUtility/`):
```
.venv/Scripts/python -m py_compile upload_to_gcs.py
```
Expected: 無輸出、離開碼 0(語法正確)。

- [ ] **Step 3: (選配)實跑上傳驗證**

僅在具備 GCS 憑證時執行(需 `google-cloud-storage` + 金鑰)。Run(於 `WebUtility/`):
```
.venv/Scripts/python upload_to_gcs.py
```
Expected: 印出 `index.html` 的 Public URL;若當下無 `WebUtility.zip`/`publish_version.json`,印「略過 (檔案不存在)」。無憑證/套件時本步驟略過,由 release 時驗證。

- [ ] **Step 4: Commit**

```bash
git add WebUtility/upload_to_gcs.py
git commit -m "feat: upload index.html to GCS; upload build artifacts only if present

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 更新 `README.md`

**Files:**
- Modify: `WebUtility/README.md`

- [ ] **Step 1: 更正「reload 就是更新」敘述**

於 `README.md`,將(第 9–10 行):
```
- 拿掉了 DV_Utility 的**自我更新**(下載 exe→換檔→detached 重啟)—— 那是最強的 dropper 誘因;
  web 版靠 reload 就是更新。
```
替換為:
```
- 拿掉了 DV_Utility 的**自我更新**(下載 exe→換檔→detached 重啟)—— 那是最強的 dropper 誘因。
  UI(`index.html`)改由 agent 啟動時從 GCS 抓取,**重開 exe 即載入新 UI**;exe 本身仍無自我更新。
```

- [ ] **Step 2: 更新「上傳」段的推送說明**

於 `README.md`,將(第 67–69 行):
```
上傳 `WebUtility.zip` 與 `version.json` 到 `gs://realtek-pccdcic-dv/WebUtility/`。
憑證用 `GOOGLE_APPLICATION_CREDENTIALS` 或沿用 DV_Utility 的金鑰(勿進版)。
沒有自我更新,所以上傳**不會**自動推送給既有使用者。
```
替換為:
```
上傳 `index.html`(UI)一律進行;`WebUtility.zip` 與 `version.json` 存在才上傳,皆到
`gs://realtek-pccdcic-dv/WebUtility/`。憑證用 `GOOGLE_APPLICATION_CREDENTIALS` 或沿用
DV_Utility 的金鑰(勿進版)。
- **UI**:上傳 `index.html` 後,使用者**下次啟動**即生效(agent 啟動時抓取)。
- **exe 本體**:沒有自我更新,上傳 zip **不會**自動推送給既有使用者(需重新下載 / IT 部署)。
```

- [ ] **Step 3: 更新「安全」段,註明 UI 已移出簽章 exe**

於 `README.md`「為什麼這樣做 / 對 SentinelOne 的意義」段落末尾(第 14 行「換句話說…沒有讓誤判問題完全消失。」之後)新增一行:
```

- **信任邊界**:UI 現由 GCS 經 HTTPS 提供、已移出「已簽章 exe」範圍(信任 = bucket 寫入權 + TLS);
  打包內建的 `index.html` 僅作離線 fallback。詳見 `docs/superpowers/specs/2026-07-15-webutility-remote-html-ui-design.md`。
```

- [ ] **Step 4: 更新「檔案」表對 index.html 的說明**

於 `README.md`,將(第 84 行):
```
| `web/index.html` | 單檔 web UI(HTML+CSS+JS,無 build step) |
```
替換為:
```
| `web/index.html` | 單檔 web UI(HTML+CSS+JS,無 build step);亦上傳 GCS,agent 啟動時抓取(打包版為離線 fallback) |
```

- [ ] **Step 5: Commit**

```bash
git add WebUtility/README.md
git commit -m "docs: update README for remote-fetched web UI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 最終整合驗證

**Files:** 無(僅驗證)

- [ ] **Step 1: 全測試綠燈**

Run(於 `WebUtility/`):
```
.venv/Scripts/python -m pytest tests/ -v
```
Expected: 4 passed。

- [ ] **Step 2: 三種來源皆能供應頁面**

依 Task 3 Step 3/4 的指令,分別在「遠端可用」「遠端壞+有快取」「遠端壞+無快取(bundled)」三情境確認 `load_web_ui_html` 回傳來源正確且 `html` 非空;並用 Task 3 Step 5 的實跑確認瀏覽器頁面正常。

- [ ] **Step 3: 確認未混入不相關變更**

Run:
```
git log --oneline webutility-remote-html-ui ^main
git show --stat HEAD~5..HEAD
```
Expected: 只有本計畫的檔案(`tests/*`、`agent.py`、`upload_to_gcs.py`、`README.md`、`requirements.txt`);**不含** `DV_Utility/*`、`version.json`、`newagent-*.json`。

---

## Self-Review 註記

- **Spec 覆蓋**:§5.1 agent.py → Task 2/3;§5.2 upload → Task 4;§5.3 README → Task 5;§7 錯誤處理 → `load_web_ui_html` 分支(Task 2)+ 測試(Task 1);§9 驗證 → Task 3/6。皆有對應。
- **命名一致**:函式 `load_web_ui_html`、方法 `Agent.load_web_ui`、屬性 `web_ui_html` / `web_ui_source` / `web_ui_cache_file`、常數 `WEB_UI_URL` 全計畫一致。
- **與 spec 的刻意差異**:spec §5.1 原描述為方法內含退回邏輯;計畫改抽為模組層級純函式 `load_web_ui_html` 以利單元測試,行為等價,Agent 方法薄薄包一層。
