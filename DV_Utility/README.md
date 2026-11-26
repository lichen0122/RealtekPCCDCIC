# DV_Utility — 建置 / 簽章 / 發佈

Realtek 內部 DV 工具:PySide6 GUI,以 **Nuitka onefile** 打包成單一 `DV_Utility.exe`。

> **已移除自我更新**(下載 exe→換檔→detached 重啟)—— 那是最強的 dropper 誘因,會被防毒
> 以 `Python/Packed.Nuitka_AGen.ED`「suspicious application」誤判。更新改由 IT 部署 /
> 使用者重新下載 zip。(選工具時仍會下載並啟動所選 DV 工具,那不是自我更新。)

## 環境

- Python 3.12 + `venv/`(已含 nuitka、pyside6、pillow、requests、google-cloud-storage、paramiko)。
- Windows。簽章需 Windows SDK 的 `signtool.exe`(本機已具備)。

## 建置 + 打包 — `release_dv_utility.py`

```
python release_dv_utility.py
```

依序執行:**進版**(寫 `version.json`,日期制 `vYYYYMMDD[.N]`)→ Nuitka onefile build →
**(選配)簽章** → 打包 `DV_Utility.zip`。

CTC 簽章前 `scan_before_sign` 先用 File-Sanitizer OPSWAT 掃每顆要簽的 exe 與它們打成的 zip,
任何一個不是 `pass` (含掃描跑不起來) 就中止, 不送簽 (owner 2026-11-26: 掃不過先處理, 不拿去 CTC 簽章)。

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

## 發佈到公司 RT NAS — `release_dv_utility.py --publish-nas`

```
python release_dv_utility.py --publish-nas
```

把 `DV_Utility.zip` 上傳到 `sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/DVUtility/`,
再把 `publish_version.json` 加上 `nas_path`、以發佈者 Ed25519 金鑰簽章後上傳成同目錄的 `version.json`
(需先 `python rtnas.py keygen` / `python rtnas.py login`)。zip 換過 (CTC 簽章重打包) 卻沒重算
manifest 時拒絕上傳。

**更新雙來源: 先 GCS, 不通才 NAS**: 主程式 `update_check` 與 `dv_updater.exe` 先讀 GCS 的
`DVUtility/version.json` (`zip_url` 必須在同一個 `DVUtility/` 目錄、帶 `sha256`); 連不上、HTTP 錯誤或
內容不合格才改讀 NAS 上已簽章的 `version.json` (簽章、`nas_path`、NAS 上的 `zip_url` 與 `sha256` 全部要對),
以使用者存好的 NAS 登入下載 zip。GCS 的 zip 下載失敗 (斷線 / sha256 不符) 也改從 NAS 重下。
GCS 能用時不需要 NAS 登入; GCS 不通且沒存過 NAS 登入時, 主程式跳出 NAS 登入視窗。更新橫幅與更新視窗都顯示
來源 (「來源: GCS」/「來源: NAS」)。`dv_updater.exe` 不信任命令列帶來的網址, 自己重讀 manifest。

更新視窗是 PySide6, 與主程式 splash 同一張深色卡片 (Realtek logo + 金黃進度條); 失敗時顯示原因、
log 路徑 (`dv_updater.log`, 在 exe 同層) 與「開啟目前版本」。

## 發佈到 GCS — `upload_to_gcs.py`

`python upload_to_gcs.py` 把同一顆 zip 放上 GCS, 並上傳 `zip_url` 換成 GCS 的 `version.json`。
新版預設讀這份 (NAS 是備援), 舊版 (v20261123 以前) 也只會讀這份, 所以每次發佈 GCS 與 NAS 兩邊都要上。

> 🚨 **這是正式發佈。** 只在確定要對所有人發佈時才執行。

## 發佈流程(等憑證到位後)

1. 取得 Realtek 正式 code-signing 憑證(PFX 或裝入憑證庫)。
2. 設定上面的 `DVUTIL_SIGN*` 環境變數。
3. `python release_dv_utility.py` → 確認輸出有「簽章: 完成並通過驗證」、
   或用 檔案內容 → 數位簽章 分頁檢查 exe。
4. `python release_dv_utility.py --publish-nas` 與 `python upload_to_gcs.py` 兩個都跑 (NAS + GCS)。
5. 請 IT 把 SentinelOne 排除改為**依簽發者憑證**(一勞永逸,不必逐版加 hash)。

## SentinelOne 誤判

未簽章的 onefile 會被 SentinelOne 以 **"Suspicious thread detected"** 阻擋。
- **短期解**:請 IT/InfoSec 依 `docs/sentinelone_false_positive_request.md` 加白名單(hash / 路徑)。
- **長期解**:上述簽章流程 + 依簽發者憑證排除。

## 現況

- 簽章管線已本機驗證(signtool 就緒、可連 DigiCert 時間戳、sign+timestamp+embed 正常)。
- **唯一缺的是正式 CA code-signing 憑證。** 沒有它,無法產出「受信任的已簽章」build。

## 工具放在公司 RT NAS (SFTP)

工具的 version.json 網址 (setting.json) 或 `update_info[].url` 寫成
`sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/<工具>/...` 時,DV_Utility 改用
`rtnas.py` 以**使用者自己的網域帳號**下載;`https://` 照舊。

- 第一次開啟這類工具會跳出「登入公司 RT NAS」視窗,實際連線驗證通過才存;密碼以 Windows
  DPAPI 加密存在 `~/.PCDV/rtnas/credential.json`,同一台電腦的其他工具共用。
- NAS 拒絕已存的密碼 (例如公司改密碼) 時會忘掉它,下次按開啟重新詢問。
- 只接受釘選的 rt-nas host key;`update_info[]` 帶 `sha256` 時下載後比對,不符不安裝。
- NAS 人人可寫,所以 NAS 上的 version.json 必須帶發佈者的 Ed25519 簽章 (`rtnas.TRUSTED_RELEASE_KEYS`),
  且 NAS 上的 zip 必須帶 `sha256`;否則顯示「發佈檔簽章驗證失敗」不更新。`https://` 工具不檢查簽章。
  發佈端以 pcdic_skills 的 `upload_to_nas.py` 簽 (私鑰只在發佈者電腦,`python rtnas.py keygen`)。
- `rtnas.py` 與 pcdic_skills 的同名檔逐字相同,改一份要同步另一份。

## 工具選單: 啟動時先 GCS, 不通才 NAS (不打包進 exe)

編輯 `../dv_util_resource/setting.json`, commit 後發佈:

```
venv\Scripts\python.exe setting_menu.py publish          # GCS + 已簽章的 NAS
venv\Scripts\python.exe setting_menu.py publish --no-nas # 只更新 GCS
```

`publish` 驗證後上傳 GCS `DVUtility/menu.json` (`{"menu": {...}}`)、舊版用的 GCS
`DVUtility/setting.json` (每項只留 GCS 網址字串; v20261123 以前只看得懂這種), 以及 NAS
`/Audio/Temp_RT/lichen.liu/DVUtility/menu.json` (發佈者簽章 + nas_path, `rtnas.publish_release`)。

DV_Utility 啟動時 (`setting_menu.load_menu`): GCS menu.json → 不通讀 NAS 那份並驗簽章 / 路徑 →
都不通用上次成功讀到的 `~/PCDV/resource/setting.json` → (只在原始碼執行時) repo 內那份。
改選單不必發 DV_Utility。

### 雙來源: 預設 GCS, GCS 不通才改 NAS

每個工具的值可以是單一網址, 或同時列兩個來源:

```json
"Tool A": "https://storage.googleapis.com/.../a_version.json",
"Tool B": {"gcs": "https://storage.googleapis.com/.../b_version.json",
           "nas": "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/B/b_version.json"}
```

- 雙來源工具一律先讀 GCS 的 version.json; 讀不到 (連不上、逾時、HTTP 錯誤、內容壞) 才改讀 NAS 的
  version.json, 之後 zip 也照 NAS manifest 從 NAS 下載。GCS 下載 zip 途中斷線也會整個改走 NAS 重來。
- 狀態列顯示來源: 「下載中 (來源: GCS) ...」/「下載中 (來源: NAS) ...」, 版本列「當前版本 X (來源: GCS)」;
  切換時顯示「GCS 無法連線, 改從公司 RT NAS 下載 …」。
- GCS 能用時不需要 NAS 登入; 改走 NAS 時才需要 (沒存過就提示再按一次開啟並登入)。
- 改走 NAS 後套用 NAS 的規則: 簽章、sha256、不降版。GCS 與 NAS 都連不上時, 開啟已安裝的版本。
- 兩邊須發佈同一版 (pcdic_skills / 各工具的 `upload_to_gcs.py` 已同時跑 `upload_to_nas`)。
