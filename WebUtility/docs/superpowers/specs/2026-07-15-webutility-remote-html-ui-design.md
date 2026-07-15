# WebUtility：UI(HTML)改從遠端 GCS 抓取 — 設計文件

- 日期：2026-07-15
- 狀態：已核准設計，待實作計畫
- 影響範圍：`WebUtility/agent.py`、`WebUtility/upload_to_gcs.py`、`WebUtility/README.md`

## 1. 背景與動機

目前 `web/index.html` 在打包時被 Nuitka 焊進 `WebUtility.exe`(`release_web_utility.py:164` 的 `--include-data-files`),`agent.py:_serve_index()` 直接讀 bundle 內的檔案供應。因為 WebUtility **沒有自我更新**,所以:

> 使用者重新點開**同一支 exe**,載入的永遠是打包當下那份舊 HTML;要換新 UI 必須重新 build 並重新部署 exe。

目標是讓「更新 UI」不必重新部署 exe:agent 每次啟動時從 GCS 抓最新的 `index.html`,達成「重開 exe → 載入新 UI」。

## 2. 目標 / 非目標

**目標**
- agent 每次啟動時抓遠端 `index.html`,供應給瀏覽器。
- 遠端不可用時能優雅退回(離線可用)。
- 「只改 UI」可只上傳 `index.html`,不必重新 build/部署 exe。

**非目標**
- 不做 exe 自我更新(維持移除狀態)。
- 不做 UI 內容的雜湊/簽章驗證(見 §4 決策與 §8)。
- 不改變 token 注入、`/api` 授權、heartbeat 等既有機制。

## 3. 選定方案

**純 HTTPS 抓取 + 三層退回**,完全比照現有 `load_catalog()` 抓 `setting.json` 的模式:

```
agent 啟動 (每次點開 exe)
  └─ load_web_ui():
       遠端 GCS (HTTPS GET) ──失敗──▶ 本機快取 ──失敗──▶ exe 內建 bundled
       抓成功 → 寫入本機快取，供應這份
```

- 抓取時機：每次啟動抓一次(在 `main()` 的 `load_catalog()` 之後、開瀏覽器之前),存於 `agent.web_ui_html`(保留 `__AGENT_TOKEN__` / `__APP_VERSION__` 佔位符原樣)。
- 同一 session 內瀏覽器 reload → 沿用本次 HTML;重開 exe → 重新抓 → 取得新 UI。
- token / version 注入維持在 `_serve_index()` 內、供應當下才做(本機注入,快取檔不含 token)。

## 4. 完整性 / 安全決策

- **完整性等級:純 HTTPS 抓取**(使用者已確認)。信任邊界 = GCS bucket 寫入權 + TLS,不做額外雜湊/簽章驗證。與現行抓 `setting.json` 一致(那也未驗證)。UI 就此移出「已簽章 exe」範圍;bundled 版僅作離線 fallback。
- **SentinelOne 誤判評估:中性(不新增、不修復)**。實際被判的是 Nuitka onefile 解壓後「從非映像記憶體起執行緒」的行為(見 `DV_Utility/docs/sentinelone_false_positive_request.md` §1、§4),與是否下載無關。新增的是**下載 HTML 文字檔**(在瀏覽器沙箱渲染,agent 從不將其當 host 程式碼執行),而 agent 現行 `_run_launch()` 早已「下載 ZIP → 解壓 exe → `subprocess.Popen` 執行」,並以 `load_catalog()` 對外發 HTTPS GET。因此此 GET 未新增任何新行為類別。
- **快取路徑已被涵蓋**:快取寫在 `~/PCDV/`,而 `%USERPROFILE%\PCDV\` 已列於 IT 白名單申請的路徑排除(申請文件 §5.3)。
- 既有阻擋(未簽章 Nuitka onefile)不因本案改變,解法仍為簽章 + IT 白名單。

## 5. 詳細設計

### 5.1 `agent.py`

- 新增常數：
  ```python
  WEB_UI_URL = 'https://storage.googleapis.com/realtek-pccdcic-dv/WebUtility/index.html'
  ```
- `Agent.__init__` 新增快取路徑(置於既有 `self.resource` = `~/PCDV/resource/`):
  ```python
  self.web_ui_cache_file = os.path.join(self.resource, 'ui_cache.html')
  self.web_ui_html = None
  self.web_ui_source = 'none'   # remote | cache | bundled | none
  ```
- 新增方法 `load_web_ui()`(結構比照 `load_catalog()`):
  1. `requests.get(WEB_UI_URL, timeout=_HTTP_TIMEOUT)` → `raise_for_status()` → 取 `r.text`;成功則寫入 `self.web_ui_cache_file`,設 `self.web_ui_html`、`self.web_ui_source='remote'`。
  2. 例外 → `log.warning(..., exc_info=True)`,讀 `self.web_ui_cache_file`(`source='cache'`)。
  3. 快取也失敗 → 讀 `get_bundled_path(os.path.join('web','index.html'))`(`source='bundled'`)。
  4. 全部失敗 → `self.web_ui_html=None`、`source='none'`,`log.exception(...)`。
  - 寫檔失敗僅記 log,不影響供應(比照 `load_catalog` 的快取寫入)。
- `main()`:在 `agent.load_catalog()` 後呼叫 `agent.load_web_ui()`,並 log `web_ui_source`。位置在 `webbrowser.open(url)` 之前 → 無 race。
- `_serve_index()` 改為使用 `self.agent.web_ui_html`:
  ```python
  html = self.agent.web_ui_html
  if not html:
      self._send_json({'error': 'index.html not available (remote/cache/bundled all failed)'}, 500)
      return
  html = html.replace('__AGENT_TOKEN__', self.agent.token) \
             .replace('__APP_VERSION__', self.agent.app_version)
  # 其餘(header / no-store / write）不變
  ```

### 5.2 `upload_to_gcs.py`

- 新增上傳 `web/index.html` → `WebUtility/index.html`(沿用既有 `cache_control='no-cache'`)。
- 調整上傳邏輯:
  - `web/index.html`：**一律上傳**(永遠存在)。
  - `WebUtility.zip`、`publish_version.json`：**檔案存在才上傳**,否則印訊息略過(它們是 build 產物)。
- 效果:「只改 UI」時 → 編輯 `web/index.html` → 跑 `upload_to_gcs.py`,即可在使用者**下次啟動**時生效,不必重新 build/部署 exe;完整 release(有 zip)則三者一起上傳。

### 5.3 `README.md`

- 更正「reload 就是更新」敘述:現在**重開 exe 會抓到新 UI**(HTML 由 GCS 取),但 exe 本身仍無自我更新。
- 「上傳」段:上傳 `index.html` **會**在使用者下次啟動生效(與 zip 不同)。
- 「安全 / 架構」段:註明 UI 已移出簽章 exe、改由 bucket 經 HTTPS 提供,bundled 版僅作離線 fallback;信任邊界 = bucket 寫入權 + TLS。
- 「檔案」表:`web/index.html` 註明「亦上傳 GCS,agent 啟動時抓取」。

## 6. 資料流

```
瀏覽器 ──GET / ──▶ agent(_serve_index)
                      └─ 供應 agent.web_ui_html(啟動時 load_web_ui 決定來源)
                         └─ 注入 __AGENT_TOKEN__ / __APP_VERSION__(本機)
瀏覽器 ──/api/* + X-Auth-Token──▶ agent(既有,不變)
```

## 7. 錯誤處理

- 遠端 5xx/逾時/離線 → 用本機快取。
- 全新安裝且離線(無快取)→ 用 bundled(保證能開)。
- bundled 也讀不到(理論上不會)→ 回 500 並記錄。
- 快取寫入失敗 → 僅記 log,當次仍以遠端內容供應。

## 8. 範圍外 / 未來

- sha256 比對(防損毀/省重抓)與私鑰簽章驗證(§4 選項二、三)未納入本案,日後如需可加。
- 若要「session 中途也更新 UI」→ 目前不做(需求只到「重開 exe」粒度)。

## 9. 驗證計畫

於 dev 模式(未編譯,`get_bundled_path` 指向 repo 內 `web/index.html`)跑 `agent.py`,檢查 log 的 `web_ui_source` 與頁面載入:

1. **遠端成功**:先上傳 `index.html` 至 GCS → 啟動 → `source=remote`、頁面正常、快取檔已寫入。
2. **fallback → cache**:把 `WEB_UI_URL` 改成壞網址(或斷網)→ `source=cache`、頁面正常。
3. **fallback → bundled**:刪掉快取檔且遠端不可用 → `source=bundled`、頁面正常。
4. **token/api 正常**:任一來源下,`/api/state` 等呼叫皆帶 token 成功、heartbeat 正常。
5. **upload 腳本**:無 zip 時跑 `upload_to_gcs.py` 只上傳 `index.html` 並略過 zip/version.json;有 zip 時三者皆上傳。
