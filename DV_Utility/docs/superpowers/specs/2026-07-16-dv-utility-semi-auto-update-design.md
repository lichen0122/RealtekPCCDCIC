# DV_Utility 半自動一鍵更新 — 設計文件

- 日期：2026-07-16
- 狀態：設計中（待實作計畫）
- 適用：`DV_Utility/`（不含 WebUtility）

## 1. 背景與問題

`DV_Utility.exe` 是 Nuitka `--mode=onefile` 打包的 PySide6 工具。原本內建**自我更新**
（`self_update.py`：比對 GCS `version.json` → 下載 zip → 把執行中的 exe 改名成 `.old` →
就地覆寫 → detached 重啟新版）。此機制已於 2026-07-16 移除。

移除原因與本設計的核心限制：

- Realtek 的程式碼簽章走 **CTC「數位憑證簽署服務」**，流程是
  **上傳檔案 → OPSWAT 掃毒 → 主管 FEDEX 核准 → HSM 簽署 → 下載已簽章檔**。
- **OPSWAT 掃毒是簽章流程的必經關卡。** 含自我更新程式碼的 exe 會被判為
  `a variant of Python/Packed.Nuitka_AGen.ED suspicious application`，**在掃毒關卡就被擋，
  連簽章都做不成**。
- 實測發現：**移除自我更新後的乾淨主程式能過掃毒** → 代表 OPSWAT 接受 Nuitka 打包本身，
  被判毒的是「下載 PE ＋ 自我改寫 ＋ detached 重啟 ＋ `WinVerifyTrust` ctypes」這組**激進行為**。

因此本設計要重建「自我更新」的使用者價值，但把會觸發判毒的行為與**每次改版都要重簽的主程式**
**解耦**。

## 2. 目標與非目標

**目標**

- 保留「半自動一鍵重啟」的更新體驗：主程式偵測到新版 → 顯示按鈕 → 使用者一鍵完成下載換裝重啟。
- 送去 CTC 簽章的**每一顆 exe 都要能過 OPSWAT 掃毒**。
- 維持 DV_Utility 現有的「單一可攜 exe、解壓即跑」特性與既有捷徑/路徑。

**非目標**

- 不做全自動無感更新（不在使用者未同意下自動換版）。
- 不改動 WebUtility。
- 不追求跨磁碟區安裝、MSI、winget 等企業部署（本設計相容但不納入）。

## 3. 架構

兩顆 exe ＋ 一份 manifest：

### 3.1 `DV_Utility.exe`（主程式，乾淨、每版簽章）

- PySide6 GUI（沿用現有）。
- 開啟時背景執行緒 GET `version.json`（**純 JSON 讀取**）；發現新版 → 顯示
  「有新版 vX → 一鍵更新並重啟」橫幅 ＋ release note。
- 按下按鈕 → 以參數啟動 `dv_updater.exe`，隨即 `app.quit()` 退出。
- **絕不含**：下載 PE、寫 PE 到磁碟、覆寫/改名 exe、detached 重啟、`WinVerifyTrust` ctypes。
  它對更新的唯一動作是「讀 JSON」＋「啟動一顆本機既有的 helper」＋「退出」。

### 3.2 `dv_updater.exe`（更新器，階梯式實作）

唯一負責「下載 ＋ 寫 PE ＋ 執行」的元件，也是唯一需要特別過 OPSWAT／被行為引擎盯的那顆。
極少改版 → **簽一次長期用**。實作採階梯式（見 §6）。

輸入（argv 或暫存 json）：`version`、`zip_url`、`sha256`、`target_exe`（要被替換並重啟的主程式
絕對路徑）、`parent_pid`（主程式 PID，用於等待其退出）。

### 3.3 `version.json`（GCS manifest）

欄位：`version`、`zip_url`、`sha256`、`size`、`release_note`。
主程式讀它做版本比對，並把 `version / zip_url / sha256` 傳給更新器。

## 4. 安裝佈局：就地替換 by helper

- 發佈 `DV_Utility.zip` 內含 **`DV_Utility.exe` ＋ `dv_updater.exe`** 兩顆（皆已簽章）。
- 使用者解壓到任意位置，捷徑指向 `DV_Utility.exe`；**路徑與捷徑始終不變**。
- 更新時主程式**先退出**，才由 `dv_updater.exe`（獨立行程）覆寫那顆**已不在執行**的
  `DV_Utility.exe`。因此：
  - 沒有「自我改寫」（被覆寫的 exe 不是更新器自己）。
  - 沒有「改名自己成 .old」。
  - 沒有 detached 重啟旗標。
  - 只是一個獨立的已簽章小程式在裝新版 —— 行為等同任何 installer。
- 保留 `DV_Utility.exe.bak` 供回滾。

> 對照「版本化資料夾（launcher 當常駐進入點）」：回滾更乾淨、完全不碰舊檔，但需要首次安裝流程且
> 犧牲單檔可攜性。DV_Utility 重視可攜性，故採就地替換 by helper。

## 5. 資料流（序列）

1. App v1 開啟 → 背景 GET `version.json` → 有新版 → 顯示更新按鈕。
2. 使用者按下 → App 把 `{version, zip_url, sha256, target_exe, parent_pid}` 傳給
   `dv_updater.exe` → 啟動它 → App **退出**。
3. 更新器：
   1. 等 `parent_pid` 結束（或重試等檔案鎖釋放）。
   2. 串流下載 zip 到 `target_exe` **同磁碟區**的暫存檔，邊下載邊算 sha256。
   3. 驗 sha256；不符 → 中止（見 §6 錯誤處理）。
   4. 從 zip 解壓出新的 `DV_Utility.exe` 到暫存檔。
   5. 將舊 `target_exe` 改名為 `.bak`，把新檔 `os.replace` 就位（原檔已無鎖）。
   6. 啟動新版 `target_exe`（正常子行程，無特殊旗標）。
   7. 結束。
4. App v2 全新開啟，版本已與 manifest 相符 → 不再顯示按鈕。下次啟動時清除殘留 `.bak`。

## 6. 更新器的階梯式實作（以 OPSWAT 當關卡）

**A. Nuitka Python 更新器（溫和版）— 最省工，先試**

- 獨立小程式 `dv_updater.py`，與主程式一樣用 Nuitka 打包，但只用
  `requests`（下載）、`hashlib`（sha256）、`zipfile`（解壓）、`os.replace`（替換已無鎖的檔）、
  `subprocess.Popen`（正常啟動子程序，無旗標）。
- **明確不用** `ctypes` `WinVerifyTrust`、不用 `DETACHED_PROCESS`/`CREATE_NEW_PROCESS_GROUP`、
  不改名自己。
- 既然乾淨主程式能過 OPSWAT，這顆溫和下載器**很可能也過**。
- **OPSWAT go/no-go**：建好 A → 送 CTC/OPSWAT → 過就採用 A（零新工具鏈）；被判毒 → 升 B。

**B. Go 原生更新器 — 最穩，逃離 Nuitka 簽名家族**

- 用 Go 重寫同一套邏輯（`net/http`、`archive/zip`、`crypto/sha256`、`os`、`os/exec` 全內建），
  產出單一 static exe，直接避開 `Python/Packed.Nuitka_AGen` 整類靜態簽名。
- 可再加 `WinVerifyTrust`（Go syscall）驗證新 exe 的 Authenticode 做縱深防禦。
- 需在 build 機安裝 Go 工具鏈。

**C. 通知式 — 保底**

- 若連原生下載器都過不了 OPSWAT，拿掉更新器：主程式只比對版本、按鈕改為「開啟下載頁」，
  使用者自行下載已簽章新版。零下載-執行行為、必過掃毒；犧牲「一鍵」。

## 7. 錯誤處理與回滾

- **先做完所有會失敗的步驟（下載／驗 sha256／解壓）再動原檔。** 任一步在替換前失敗 →
  原 `target_exe` 完好 → **重啟舊版**，使用者不會落得沒程式可開。
- 替換採「舊檔改 `.bak` → 新檔就位」：
  - 就位失敗 → 還原 `.bak` → 重啟舊版。
  - 新版啟動後立即崩潰（可選：短時間內偵測）→ 還原 `.bak` → 重啟舊版。
- 成功後，下次主程式啟動時清除殘留 `.bak`。
- 檔案鎖：主程式為 onefile，其 bootstrap 在執行時持有 exe 映像；行程退出後鎖釋放。更新器以
  等待 `parent_pid` ＋ 退避重試 `os.replace` 因應短暫鎖定。

## 8. 安全

- 只走 HTTPS（GCS）。manifest 的 `sha256` 保障下載完整性（防半截/損毀）。
- 更新器是唯一具「網路 ＋ 寫檔 ＋ 執行」能力者，集中審視並簽章。
- B（Go）版可加 Authenticode 驗簽（要求下載的新 exe 帶有效受信任簽章）做縱深防禦；
  A 版為求過掃毒先略過（僅 HTTPS ＋ sha256）。
- 傳入更新器的參數（argv/暫存 json）做基本驗證：`target_exe` 必須是既有 `.exe` 絕對路徑；
  `zip_url` 限定 GCS 網域。

## 9. 簽章整合

- `DV_Utility.exe` 與 `dv_updater.exe` 皆走 CTC（OPSWAT → 核准 → HSM 簽）。
- 主程式常改版但乾淨 → 每次都能過掃毒、每版重簽。
- 更新器極少改版 → 簽一次長期沿用；改版時才重簽。
- 簽章 = 根本解：簽過後 ESET/SentinelOne 幾乎不誤判，且 IT 可**依簽發者憑證一次排除**，
  不必逐版加 hash。

## 10. Build / 發佈調整（`release_dv_utility.py`）

- 產出兩顆 exe：
  - 主程式：Nuitka onefile（沿用現有流程）。
  - 更新器：A＝Nuitka onefile（`dv_updater.py`）；B＝`go build`。
- 發佈 `DV_Utility.zip` 內含兩顆**已簽章** exe。
- `version.json` 需含 `zip_url / sha256 / size / release_note`（供主程式比對、更新器下載驗證）。
- 簽章因 CTC 有人工核准關卡而非全自動：build 未簽 → 送簽 → 取回已簽 → 重打包 zip → 上傳 GCS。

## 11. 測試計畫

- **OPSWAT 關卡（關鍵、go/no-go）**：更新器 A 建好 → 送 CTC/OPSWAT 掃 → 過則採用 A、否則升 B。
- **功能測試**（用 `verify` / `run` skill 端到端）：
  - 架本機/測試 URL 放假的較新 `version.json` ＋ v2 zip → 跑 v1 → 按更新 →
    驗證下載/驗 sha256/替換/重啟到 v2。
  - sha256 不符 → 應中止並重啟舊版，原檔無損。
  - 替換後新版啟動失敗 → 應還原 `.bak` 並重啟舊版。
- 無法單元測試 AV；OPSWAT 是真正的關卡。

## 12. 風險

- 即使原生 ＋ 簽章，極 aggressive 的行為引擎仍可能對「下載並執行」示警 → 靠簽章 ＋ 精簡 ＋
  溫和機制 ＋ OPSWAT 實測壓下去；最終保底為通知式（C）。
- A（Nuitka 溫和版）能否過 OPSWAT 為經驗性結果，須實測；計畫已內建升級路徑。
