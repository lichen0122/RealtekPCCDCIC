# SentinelOne 誤判排除申請 — DV_Utility.exe

> 這是一份可直接轉寄給 IT / InfoSec (SentinelOne 管理者) 的說明。
> 目的:`DV_Utility.exe` 被 SentinelOne 以 **"Suspicious thread detected"** 阻擋而無法執行,
> 研判為**誤判 (false positive)**,申請將其加入排除 / 標記為誤判。

---

## 1. 摘要 (給管理者)

- **應用程式**:`DV_Utility.exe` — Realtek 內部 DV 驗證工具 (PC DV/CIC 團隊自行開發)。
- **技術本質**:Python + PySide6 (Qt) 桌面 GUI,使用 **Nuitka `onefile`** 打包成單一 exe。
- **被擋原因 (研判)**:onefile 啟動時會把壓縮的程式碼「解壓到暫存/快取資料夾後從該處啟動執行緒」。
  「從剛寫出的非映像 (non-image) 記憶體啟動的執行緒」是封裝器 (packer) 的常見特徵,
  被 SentinelOne 的 **Behavioral AI** 引擎判為 *Suspicious thread*。此為 Nuitka/PyInstaller 這類
  打包工具**已知的誤判**,並非惡意行為。
- **申請**:標記為 False Positive,並依下方「建議排除方式」加入白名單,讓內部人員可正常執行。

---

## 2. 檔案資訊 (File identity)

| 項目 | 值 |
|------|----|
| 檔名 | `DV_Utility.exe` |
| 版本 (version.json) | `v20260703` |
| 檔案大小 | `28,153,856` bytes |
| **SHA-256** | `dd9beb2cad2418f1463f348afaa6d95dc90aae6dec87e19ca9853beb44b96b02` |
| 打包方式 | Nuitka `--mode=onefile` (Python 3.12 + PySide6) |
| 數位簽章 | 目前**尚未簽章** (取得 code-signing 憑證後將簽章,屆時可改用簽發者排除) |
| 發佈來源 | `https://storage.googleapis.com/realtek-pccdcic-dv/DVUtility/DV_Utility.zip` (Realtek GCS) |

> ⚠️ 上述 SHA-256 只對應這一版 build。每次改版 (`vYYYYMMDD`) hash 都會改變。
> 若採 hash 排除,改版後需重新加入 → 因此更建議路徑排除或 (未來) 簽發者排除,見第 5 節。
>
> 重新計算 hash:Windows 內建 `certutil -hashfile DV_Utility.exe SHA256`。

---

## 3. 如何在 SentinelOne 主控台擷取這筆偵測

請管理者在 SentinelOne Management Console 找到這筆事件,並回報以下欄位 (加速分析):

- **Threat ID / Storyline ID**
- **Engine**:應為 *Behavioral AI* (行為引擎,非 Static AI)
- **Threat Name / Classification**:*Suspicious thread detected*
- **Endpoint / 使用者、發生時間**
- 觸發時的 process 路徑 (應為使用者執行的 `...\DV_Utility.exe`,以及其解壓出的子行程,
  路徑位於 `%LOCALAPPDATA%\Realtek\DV_Utility\...`)

---

## 4. 為何研判為誤判

1. **來源可信**:由 Realtek PC DV/CIC 團隊開發、經內部 GCS bucket (`realtek-pccdcic-dv`) 發佈,
   非外部/未知來源。
2. **偵測屬行為啟發式,非特徵碼命中**:被判的是「執行緒起始位置在解壓後的記憶體」這個*行為*,
   這正是 Nuitka onefile 的正常啟動流程 (bootstrap 解壓 → 執行),與封裝型惡意程式在靜態行為上
   相似,故產生誤判。
3. **用途單純**:PySide6 GUI 工具,供內部 DV 流程使用 (下載/啟動其他 DV 工具、檢查更新)。
4. 此類「Python 打包 exe 被 EDR 行為引擎誤判」是業界普遍且已知的現象。

---

## 5. 建議的排除方式 (依優先序)

### (A) 立即 — 現在就能解鎖
擇一或並用:

1. **標記為 False Positive**(在 Console 對該 Threat 選 *Mark as false positive*),並建立排除。
2. **依 SHA-256 排除**(對應本版 build):
   `dd9beb2cad2418f1463f348afaa6d95dc90aae6dec87e19ca9853beb44b96b02`
   *(缺點:每次改版 hash 變動,需重新加入。)*
3. **依路徑排除**(涵蓋所有版本,較耐用)。以下兩個目錄是程式**固定**會用到的:
   - onefile 執行期解壓/快取目錄:`%LOCALAPPDATA%\Realtek\DV_Utility\`
     (實際會是其下的版本子資料夾,例如 `...\DV_Utility\2026.7.3.0\`;建議對整個
     `%LOCALAPPDATA%\Realtek\DV_Utility\` 做資料夾排除)
   - 程式工作/資源目錄:`%USERPROFILE%\PCDV\`
   > 注意:使用者實際點擊的那顆 `DV_Utility.exe` 放在哪由使用者決定 (可能在下載/桌面),
   > 因此**不建議**用「exe 所在路徑」排除;請優先用上面兩個固定目錄 + hash。

### (B) durable — 取得 code-signing 憑證後 (最推薦、一勞永逸)
待本工具以 Realtek 的 code-signing 憑證簽章後,請改用 **依簽發者/發行者憑證 (publisher / signer) 排除**。
如此每次改版都自動涵蓋,不必再逐版加 hash。(開發端已備妥簽章流程,只待憑證到位。)

### (C) 選配
可將本檔案樣本提交給 SentinelOne 原廠 (Threat sample submission) 做誤判回報 / 特徵調整。

---

## 6. 排除範圍建議

行為引擎的誤判建議採「可執行 + 行為監控」層級的排除即可 (依貴司 SentinelOne 政策命名),
讓該程式可執行且不再觸發 *Suspicious thread*,同時保留對其他未知行為的監控。
如政策允許,依「簽發者憑證」的排除是最安全且維護成本最低的做法 (見 5-B)。

---

## 7. 聯絡

- 提出人 / 開發團隊:PC DV/CIC (DV_Utility 維護者)
- 若需進一步技術說明 (打包參數、行為說明、原始碼) 可提供。
