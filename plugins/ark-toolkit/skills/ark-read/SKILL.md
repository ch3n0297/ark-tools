---
name: ark-read
description: 靜默讀取各券商帳戶的即時持倉（永豐 Shioaji＋檔案帳戶 CSV/Excel）並顯示合併結果。不開 ARK App、不彈視窗、只讀不寫。當使用者想快速看帳戶持倉、確認券商端資料、或需要機器格式（--json）的持倉資料時使用。
---

# ark-read

> **通用性原則**：本 plugin 是通用工具，會發佈給其他使用者。個人化內容
> （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾 `~/.ark-toolkit/`
> 或專案自己的 `docs/`，**不要直接改 plugin**；只有通用的修復與功能
> （附測試）才動 plugin 本身。

靜默讀取指令：把 `ark-setup` 設定的每個帳戶讀一輪，輸出各帳戶明細與
全帳戶合併結果（股數相加、均價加權平均）。**不碰 ARK App、不彈任何視窗、
不寫任何檔案**——與 ark-collect 的差別就在這裡：collect 是為了同步而收集並
寫入快照，read 只是看。

不依賴 ARK 與 macOS Accessibility，任何平台可跑（Shioaji 需網路）。

## 執行環境（uv）

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件。
執行前先確認 uv 存在，沒有就直接安裝後繼續（不必詢問使用者）：

```bash
command -v uv >/dev/null || brew install uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 用法

```bash
uv run skills/ark-read/read.py          # 各帳戶明細＋合併結果（表格）
uv run skills/ark-read/read.py --json   # 機器格式：{accounts, merged, file_mtime}
```

- 檔案帳戶會順帶顯示**檔案最後修改時間**——資料多舊一目瞭然
- 任一帳戶讀不到（永豐登入失敗、檔案缺失、解析錯誤）即整體失敗並指名帳戶，
  **絕不部分合併**——少一個帳戶的合併結果會誤導後續判斷
- 尚未設定帳戶時提示執行 `ark-setup`

## 與其他 skill 的關係

- 帳戶清單由 `ark-setup` 管理；本 skill 只讀不改
- 要把資料寫進 ARK 請走 `ark-collect`（收集確認）→ `ark-sync`（執行）
