# DV WebUtility

DV_Utility 的 **web UI 版本**:把介面改成瀏覽器裡的單檔 HTML,只保留一支**很小的本機代理
(agent)** 負責瀏覽器沙箱做不到的事(啟動工具 exe、下載/解壓到指定路徑、管理程序)。

## 為什麼這樣做 / 對 SentinelOne 的意義

- **UI(`web/index.html`)在瀏覽器跑,不是可執行檔** → 不會被 EDR 行為引擎誤判。
- 拿掉了 DV_Utility 的**自我更新**(下載 exe→換檔→detached 重啟)—— 那是最強的 dropper 誘因。
  UI(`index.html`)改由 agent 啟動時從 GCS 抓取,**重開 exe 即載入新 UI**;exe 本身仍無自我更新。
- **但**:啟動本機工具這件事一定要有原生元件,所以還是有一支 `agent.exe`。它仍是 Nuitka
  onefile 原生檔,**一樣可能被 SentinelOne 以 "Suspicious thread" 誤判**。
  → 這顆代理仍需要 **簽章 + IT 白名單**(與 DV_Utility 同一套辦法)。
  換句話說:web 化把部分誘因拿掉、把「要簽章的東西」縮到最小,但**沒有讓誤判問題完全消失**。
- **信任邊界**:UI 現由 GCS 經 HTTPS 提供、已移出「已簽章 exe」範圍(信任 = bucket 寫入權 + TLS);
  打包內建的 `index.html` 僅作離線 fallback。詳見 `docs/superpowers/specs/2026-07-15-webutility-remote-html-ui-design.md`。

## 架構

```
瀏覽器 (單檔 index.html)  ──localhost HTTP + token──▶  agent.exe (127.0.0.1)
                                                          │
                                            讀 setting.json 工具清單
                                            下載/解壓工具到 ~/PCDV 或 project 路徑
                                            subprocess 啟動工具 exe、追蹤/關閉程序
```

- 代理只綁 `127.0.0.1`、每次啟動產生隨機 **token**(注入網頁,之後 /api 都要帶)、檢查 Host header。
- **只會啟動工具清單(`setting.json`)內的工具**;client 傳工具「名稱」,exe 路徑一律由可信
  manifest 解析,不接受任意路徑。
- 沿用 `~/PCDV` 與其 `resource/` 設定檔 → 已安裝工具、work_dir 清單、工具歷史**與 DV_Utility 共用**。
- 關閉瀏覽器分頁後,代理數秒內自動結束(heartbeat 看門狗)。

## 開發執行

```
.venv\Scripts\python agent.py
```

會啟動代理並自動開瀏覽器到 `http://127.0.0.1:<隨機埠>/`。開發只需 `requests`(已在 `.venv`)。

## 打包(`release_web_utility.py`)

```
.venv\Scripts\python -m pip install nuitka        # 首次
.venv\Scripts\python release_web_utility.py
```

進版 → Nuitka onefile 打包 `agent.py`(內含 `web/index.html`+`version.json`)→(選配)簽章 →
產出 `WebUtility.exe` / `WebUtility.zip` / `publish_version.json`。

**簽章**(需 code-signing 憑證,未設定則自動略過,與 DV_Utility 同):

| 方式 | 環境變數 |
|------|----------|
| PFX | `DVUTIL_SIGN_PFX` + `DVUTIL_SIGN_PFX_PASSWORD` |
| 憑證庫(名稱) | `DVUTIL_SIGN_SUBJECT="CN=Realtek..."` |
| 憑證庫(自動) | `DVUTIL_SIGN=1` |

其他:`DVUTIL_SIGN_TS_URL`(預設 DigiCert)、`SIGNTOOL`(signtool 路徑)。

## 上傳(`upload_to_gcs.py`)

```
.venv\Scripts\python -m pip install google-cloud-storage    # 首次
.venv\Scripts\python upload_to_gcs.py
```

上傳 `index.html`(UI)一律進行;`WebUtility.zip` 與 `version.json` 存在才上傳,皆到
`gs://realtek-pccdcic-dv/WebUtility/`。憑證用 `GOOGLE_APPLICATION_CREDENTIALS` 或沿用
DV_Utility 的金鑰(勿進版)。
- **UI**:上傳 `index.html` 後,使用者**下次啟動**即生效(agent 啟動時抓取)。
- **exe 本體**:沒有自我更新,上傳 zip **不會**自動推送給既有使用者(需重新下載 / IT 部署)。

## SentinelOne 誤判 / 排除

代理 exe 未簽章時仍可能被擋。短期請 IT 依 hash / 路徑加白名單;可能的路徑:
- 解壓快取:`%LOCALAPPDATA%\Realtek\WebUtility\`
- 工作/資源:`%USERPROFILE%\PCDV\`

長期:取得正式 CA 憑證簽章 → 請 IT 改用**依簽發者憑證**排除。可直接轉寄給 IT 的申請說明見
`docs/sentinelone_false_positive_request.md`(WebUtility.exe 專屬,含本版 exe 的 SHA-256 與固定路徑)。

## 檔案

| 檔 | 說明 |
|----|------|
| `agent.py` | 本機 HTTP 代理(伺服 UI + 啟動/下載/程序管理 API) |
| `web/index.html` | 單檔 web UI(HTML+CSS+JS,無 build step);亦上傳 GCS,agent 啟動時抓取(打包版為離線 fallback) |
| `version.json` | 版本(release 進版時寫入,一起打包) |
| `release_web_utility.py` | Nuitka 打包 + env-gated 簽章 |
| `upload_to_gcs.py` | 上傳 GCS |
| `requirements.txt` | 相依套件 |
