# ISP Setting Class Generator

`isp_reg_to_setting.py` 將 ISP 模組的 register header (`*_reg.h`) 解析後，產生供 UVM testbench 使用的 SystemVerilog setting class (`<mod>_setting.sv`)。
支援單一模組處理與整個 IP 專案的批次處理。

## Requirements

- Python 3.12+
- 套件：`colorlog`（選用，未安裝時會 fallback 到內建 `logging`）

## Usage

### Single mode — 處理單一模組

```bash
python isp_reg_to_setting.py single <mod_name> <path_cmod> <path_spec> [path_hw_sheet] [--reg_prefix ISP]
```

| 參數 | 說明 |
| --- | --- |
| `mod_name` | 模組名稱（如 `ipu_ive`、`ipu_r2yl`），決定 class 名稱、guard、register 前綴 |
| `path_cmod` | Register header 檔路徑（`<mod>_reg.h`） |
| `path_spec` | 輸出 SystemVerilog 設定檔路徑（`.sv`） |
| `path_hw_sheet` | （選用，目前未使用，保留以維持 CLI 相容） |
| `--reg_prefix` | （目前未使用，保留以維持 CLI 相容） |

範例：

```bash
python isp_reg_to_setting.py single ipu_ive regh2setting/ipu_ive/ive_reg.h out/ipu_ive_setting.sv
```

### Batch mode — 批次處理整個 IP 專案

```bash
python isp_reg_to_setting.py batch <ip_name> <path_ip_src> <path_out> [--reg_prefix ISP] [--bypass_mod mod1,mod2]
```

| 參數 | 說明 |
| --- | --- |
| `ip_name` | IP 專案名稱（顯示用） |
| `path_ip_src` | 包含 `*_reg.h` 的根目錄，會遞迴搜尋 |
| `path_out` | 輸出資料夾 |
| `--reg_prefix` | （目前未使用，保留以維持 CLI 相容） |
| `--bypass_mod` | 以逗號分隔的模組名稱清單，用以略過 |

Batch 模式會以 `<path_ip_src>` 下所有 `*_reg.h` 為輸入，模組名稱取自其所在目錄名稱，輸出至 `<path_out>/<mod_name>_setting.sv`。

## Input format

`*_reg.h` 內每筆 `OP(...)` 對應一個 register 欄位。`a, b, c, d, e` 之後的欄位順序為：

```
[NAME_UPPER,] name_lower, sign, array_type, array_size,
                bit_width, min, max, default, /* description */
```

- `NAME_UPPER, name_lower`：可為兩個（如 `ive_reg.h`）或只有一個 lower-case name（如 `ipu_r2yl_reg.h`，UPPER 由解析器自動轉換）
- `sign`：`U` 或 `S`
- `array_type` / `array_size`：陣列為 `A` 與整數，純量為空
- `default`：純量為單一整數；陣列為 `"v0, v1, ..."` 引號字串
- 描述以 `cmodel_only` 開頭的欄位會被略過（不參與 .sv 產出）

## Output

產生的 `.sv` 內含一個 `<mod_name>_setting extends isp_base_setting` class，包含：

- `rand bit [signed] [N-1:0] <field>;` 欄位宣告（陣列展開為 `<field>_0..<field>_N-1`，1-bit 欄位省略 `[N:M]`）
- `legal_ipu` 與 `random_frame_data` 兩個基本 constraint
- `reg_constraint_by_xlsx`：當任一欄位的 `[min, max]` 小於 bit-width 全範圍時才產生，並列出所有欄位
- 標準 method：`new` / `class2cmodel` / `reg2class` / `post_randomize` / `reset` / `config_reg` / `update_set`
- `do_equation`（空 body）：與 `reg_constraint_by_xlsx` 同步觸發
- `` `uvm_object_utils_*`` field 註冊區塊

## Behavior Notes

- **Reset hex**：以 bit-width 為位寬輸出 two's-complement hex（如 11-bit 中的 `-29` → `11'h7e3`）。
- **`reg_constraint_by_xlsx` 對齊**：欄位名稱左對齊至 `max_field_name_len + 2` 後接 `inside`。
- **`config_reg` padding**：以 `{(32 - bit_width)'h0, this.<field>}` 補齊 32-bit。
- **欄位順序**：嚴格依 `*_reg.h` 中 `OP()` 的出現順序展開，陣列展開於原位。
- **錯誤處理**：解析失敗會以 log 顯示；批次模式會繼續處理後續模組並在結尾彙整失敗清單。
