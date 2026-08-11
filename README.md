# ark-tools

方舟運算(ARK) App 的持倉同步與分析工具（真實持倉為帳戶清單：永豐 Shioaji API＋任意個券商 CSV/Excel 檔案帳戶，多帳戶自動合併），可同時安裝到 **Claude Code** 與 **Codex**。

兩邊共用同一份 `skills/`，只是各自需要一份 metadata（`.claude-plugin/` 與 `.codex-plugin/`）。

## 內容

| Skill | 用途 |
|---|---|
| `ark-setup` | 帳戶清單設定精靈：管理永豐 Shioaji 與檔案帳戶（CSV/Excel），原生視窗安全收集憑證 |
| `ark-read` | 靜默讀取各帳戶即時持倉與合併結果（不碰 ARK、不彈視窗、只讀不寫） |
| `ark-collect` | 兩階段同步的階段一：收集各帳戶持倉，確認後寫入當日快照 |
| `ark-sync` | 兩階段同步的階段二：以真實持倉同步 ARK 庫存（修改／新增／刪除）；只有永豐時可直接執行 |
| `ark-analyze` | 集中度、與 App 建議部位的偏離、風控檢查、歷史快照復盤 |
| `ark-explore` | 探索全 App 結構，產出功能地圖（以版本號當快取鍵，版本沒變就跳過） |
| `ark-agent` | 自動交易決策軌：盤中決策包、依 ARK 紀律決策並鎖定、自動下單、成交對回、滾動評估 |

| 參考資料 | 用途 |
|---|---|
| `references/ark-app-map.md` | **全 App 功能地圖** —— 每一頁在幹嘛、讀得到什麼、什麼時候該用它 |
| `references/ark-app-ux.svg` | UX 流程圖 —— 核心資料流與六個 tab 的視覺化 |

要回答「這個 App 怎麼用」「策略頁在幹嘛」，直接讀 `references/ark-app-map.md`，
不必跑 `ark-explore`。只有 App 改版後才需要重跑。

## 安裝

### Claude Code

```bash
/plugin marketplace add ch3n0297/ark-tools
/plugin install ark-toolkit@ark-tools
```

### Codex

```bash
codex plugin marketplace add ch3n0297/ark-tools
codex plugin add ark-toolkit
```

本機開發改用 checkout 路徑：把上面的 `ch3n0297/ark-tools` 換成本機路徑（如 `<repo>/plugin`）。

### 更新

```bash
/plugin marketplace update ark-tools          # Claude Code（第三方 marketplace 自動更新預設關閉）
codex plugin marketplace upgrade ark-tools    # Codex（無單一 plugin 的 update 指令，重拉整個 marketplace）
```

## 前置需求

- **macOS**（依賴 Accessibility API 與 Apple Silicon 的 iOS App 相容層）
- 方舟運算已安裝並登入（未開啟會自動代為啟動；未安裝會明確提示先從 App Store 安裝）
- 終端機／IDE 已取得「輔助使用」權限（系統設定 → 隱私權與安全性 → 輔助使用）
- [uv](https://docs.astral.sh/uv/) — 各腳本以 PEP 723 宣告依賴，`uv run` 會自動準備 Python 與套件（機器不需要預先安裝 Python；uv 未裝時各 skill 會自動以 brew 或官方指令安裝）
- 初次使用先跑 `ark-setup` 設定帳戶清單（原生視窗精靈，機密不經終端機與對話）：
  - **永豐 Shioaji**（最多一個）— 隱藏輸入視窗收憑證 → 測試登入 → 寫入 `~/.ark-toolkit/.env`（權限 600）
  - **檔案帳戶**（任意個）— 任何券商匯出的 CSV/Excel 持股清單，無 API 也能對帳
  - **空清單＝純 ARK 模式** — 不對帳，僅用分析功能

## 快速開始

```bash
uv run plugins/ark-toolkit/skills/ark-setup/setup.py             # 帳戶清單設定（原生視窗精靈）
uv run plugins/ark-toolkit/skills/ark-read/read.py               # 靜默看各帳戶持倉
uv run plugins/ark-toolkit/skills/ark-collect/collect.py         # 多帳戶：收集確認寫快照
uv run plugins/ark-toolkit/skills/ark-sync/sync.py --dry-run     # 看差異
uv run plugins/ark-toolkit/skills/ark-analyze/analyze.py         # 看風險
uv run plugins/ark-toolkit/skills/ark-explore/run.py --show      # 看功能地圖快取
```

## 測試

純邏輯測試不需要 ARK 執行中，任何平台皆可跑：

```bash
cd plugins/ark-toolkit/lib                && uv run --no-project --python 3.13 --with openpyxl python -m unittest test_source
cd plugins/ark-toolkit/skills/ark-sync    && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_sync
cd plugins/ark-toolkit/skills/ark-analyze && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_analyze
cd plugins/ark-toolkit/skills/ark-agent   && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_market test_packet test_journal test_evaluate
```

## 設計要點

ARK 是 FairPlay 加密的 wrapped iOS App，沒有官方 API，全靠 Accessibility API 操作 UI。實作中有數個「失敗會偽裝成成功」的陷阱（`AXSetValue` 假成功、被遮擋元素的 `AXPress` 回傳 0 卻無效、解析失效被當成空庫存），各 skill 的 `SKILL.md` 有完整說明——修改程式碼前請先讀。

所有寫入前都以 App 自算的「總成本」交叉驗算，不符即中止不儲存。分析只陳述事實與偏離量，不產出買賣建議。
