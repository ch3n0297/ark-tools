---
name: ark-setup
description: ark-toolkit 帳戶清單設定精靈：以 macOS 原生視窗管理「真實持倉」帳戶（永豐 Shioaji API＋任意個 CSV/Excel 檔案帳戶；空清單＝純 ARK 模式）並安全收集 API 憑證。當使用者初次安裝、要新增／移除帳戶、更新金鑰，或其他 ark skill 回報「尚未設定真實持倉來源」時使用。僅限 macOS。
---

# ark-setup

> **通用性原則**：本 plugin 是通用工具，會發佈給其他使用者。個人化內容
> （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾 `~/.ark-toolkit/`
> 或專案自己的 `docs/`，**不要直接改 plugin**；只有通用的修復與功能
> （附測試）才動 plugin 本身。

帳戶清單設定精靈。決定 ark-read／ark-collect／ark-sync／ark-analyze 的
「真實持倉」由哪些帳戶組成，並在需要時安全地收集 API 憑證。
多帳戶時同檔股票會合併（股數相加、均價加權平均）。

## ⚠️ 資安護欄（對 Agent 的硬性規則）

1. **絕不在對話中要求使用者提供 API Key／Secret Key**。一律執行 `setup.py`，
   由 macOS 原生「隱藏輸入」視窗收集——機密走「視窗 → Python 變數 → `.env`」，
   全程不經 stdout，因此不會進入對話上下文，也就沒有被注入或外洩的路徑。
2. **絕不讀取或輸出 `~/.ark-toolkit/.env` 的內容**（含 `cat`、`grep`、`printenv`、
   `source` 後 echo 等任何形式）。憑證只由 Python 腳本在執行期載入。
3. `setup.py` 的 stdout 只含進度與結果訊息，不含機密，可安心顯示給使用者。

## 執行環境（uv）

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件，
機器不需要預先安裝 Python。執行前先確認 uv 存在，沒有就直接安裝後繼續（不必詢問使用者）：

```bash
command -v uv >/dev/null || brew install uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

curl 安裝落在 `~/.local/bin`；若之後 shell 仍找不到 `uv`，改以 `~/.local/bin/uv` 呼叫。

## 用法

```bash
uv run skills/ark-setup/setup.py           # 開啟原生視窗精靈
uv run skills/ark-setup/setup.py --show    # 只顯示目前設定（不開視窗）
```

> 執行精靈時，視窗會等使用者操作，請把 Bash 逾時設長（建議 10 分鐘），
> 並提醒使用者留意畫面上的對話視窗（可能被其他視窗遮住）。

## 帳戶清單

精靈是「顯示清單 → 新增／移除帳戶 → 完成」的迴圈，可設定：

| 帳戶類型 | 適用 | 設定內容 |
|---|---|---|
| **永豐 Shioaji**（最多一個） | 永豐證券用戶 | 隱藏視窗收 Key/Secret → 測試登入驗證 → 寫入 `~/.ark-toolkit/.env`（權限 600） |
| **檔案帳戶**（任意個） | 任何券商（無 API 也可） | 原生選檔（CSV／Excel）→ 自動對應表頭（認不出時下拉選）→ 試讀驗證 → 命名 → 記住路徑與欄位 |
| **空清單＝純 ARK** | 不對帳 | ark-sync／ark-collect／ark-read 停用；ark-analyze／ark-explore 照常可用 |

按「完成」時若清單含 Shioaji 帳戶，會再問一次 **ARK 均價口徑**（含息／不含息 ×
含手續費／不含手續費，預選目前值；取消＝不變）。第一次 `ark-sync` 也會問，
這裡是之後想改的入口；口徑的意義見 ark-sync 的「均價口徑」一節。

設定存於 `~/.ark-toolkit/config.json`（僅非機密）。舊版單一來源格式會在
載入時自動遷移成帳戶清單，不必重跑精靈。檔案內容更新後不需重跑
（每次執行時重讀），路徑或欄位變了也可在 ark-collect 執行時當場重選。

## 與其他 skill 的關係

- `ark-read`／`ark-analyze` 透過 `lib/source.py` 的 `read_positions()` 取得
  **全帳戶合併**的 `{代號: (股數, 均價)}`，不知道來源是什麼。
- 有檔案帳戶時，同步走兩階段：`ark-collect`（收集確認寫快照）→ `ark-sync`
  （讀快照執行）。只有永豐時 `ark-sync` 直接即時讀取，不需 collect。
- 未設定時各 skill 會報「尚未設定真實持倉來源」——此時就執行本 skill。
- 純 ARK 模式下 `read_positions()` 回傳 `None`（≠ 空 dict）：ark-sync 拒絕執行，
  ark-analyze 略過對帳段落。
