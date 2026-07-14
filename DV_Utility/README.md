# DV_Utility — 建置 / 簽章 / 發佈

Realtek 內部 DV 工具:PySide6 GUI,以 **Nuitka onefile** 打包成單一 `DV_Utility.exe`,
並具**自我更新**(app 端比對 GCS 上的 `version.json`,有新版就下載 zip、換檔重啟)。

## 環境

- Python 3.12 + `venv/`(已含 nuitka、pyside6、pillow、requests、google-cloud-storage)。
- Windows。簽章需 Windows SDK 的 `signtool.exe`(本機已具備)。

## 建置 + 打包 — `release_dv_utility.py`

```
python release_dv_utility.py
```

依序執行:**進版**(寫 `version.json`,日期制 `vYYYYMMDD[.N]`)→ Nuitka onefile build →
**(選配)簽章** → 打包 `DV_Utility.zip` → 產生 `publish_version.json`(自我更新用)。

## 簽章(選配 — 需 code-signing 憑證)

由環境變數啟用;**未設定則自動略過**(產出未簽章 build,流程不受影響)。憑證來源擇一:

| 方式 | 環境變數 |
|------|----------|
| PFX 檔 | `DVUTIL_SIGN_PFX`=檔案路徑、`DVUTIL_SIGN_PFX_PASSWORD`=密碼 |
| 憑證庫(依名稱) | `DVUTIL_SIGN_SUBJECT`=`"CN=Realtek..."` |
| 憑證庫(自動挑選) | `DVUTIL_SIGN=1`(用 signtool `/a`) |

其他:`DVUTIL_SIGN_TS_URL`（RFC-3161 時間戳,預設 `http://timestamp.digicert.com`)、
`SIGNTOOL`（signtool 完整路徑,否則自動搜尋 Windows SDK）。

範例(PowerShell,PFX):

```powershell
$env:DVUTIL_SIGN_PFX = "C:\path\realtek_codesign.pfx"
$env:DVUTIL_SIGN_PFX_PASSWORD = "********"
python release_dv_utility.py    # build 後自動 signtool sign + verify /pa
```

> ⚠️ **自簽憑證不算數。** 自簽憑證無發行者信譽,SentinelOne 不會信任,無法用來驗證
> 「簽章後是否還被擋」。要做這個測試,必須用**正式 CA 簽發**的 code-signing 憑證。

## 發佈到 GCS — `upload_to_gcs.py`

```
python upload_to_gcs.py
```

覆蓋公開 bucket 上的 `DVUtility/DV_Utility.zip` 與 `DVUtility/version.json`。

> 🚨 **這是正式發佈,不是私下測試。** 上傳後**所有使用者的自我更新器都會自動下載並安裝**
> 這個版本。只在確定要對所有人發佈時才執行。

## 發佈流程(等憑證到位後)

1. 取得 Realtek 正式 code-signing 憑證(PFX 或裝入憑證庫)。
2. 設定上面的 `DVUTIL_SIGN*` 環境變數。
3. `python release_dv_utility.py` → 確認輸出有「簽章: 完成並通過驗證」、
   或用 檔案內容 → 數位簽章 分頁檢查 exe。
4. `python upload_to_gcs.py` 發佈。
5. 把 `self_update.py` 的 `VERIFY_DOWNLOAD_SIGNATURE` 改為 `True`(拒絕未簽章/被竄改的更新)。
6. 請 IT 把 SentinelOne 排除改為**依簽發者憑證**(一勞永逸,不必逐版加 hash)。

## SentinelOne 誤判

未簽章的 onefile 會被 SentinelOne 以 **"Suspicious thread detected"** 阻擋。
- **短期解**:請 IT/InfoSec 依 `docs/sentinelone_false_positive_request.md` 加白名單(hash / 路徑)。
- **長期解**:上述簽章流程 + 依簽發者憑證排除。

## 現況

- 簽章管線已本機驗證(signtool 就緒、可連 DigiCert 時間戳、sign+timestamp+embed 正常)。
- **唯一缺的是正式 CA code-signing 憑證。** 沒有它,無法產出「受信任的已簽章」build。
