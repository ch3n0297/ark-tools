---
name: ark-analyze
description: 分析方舟運算(ARK) App 的台股庫存：集中度、與 App 建議部位的偏離、風控門檻檢查、歷史快照復盤。當使用者要求分析持股結構、檢查風險、看集中度、復盤或比較歷史變化時使用。僅限 macOS。
---

# ark-analyze

> **通用性原則**：本 plugin 是通用工具，會發佈給其他使用者。個人化內容
> （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾 `~/.ark-toolkit/`
> 或專案自己的 `docs/`，**不要直接改 plugin**；只有通用的修復與功能
> （附測試）才動 plugin 本身。

讀出 ARK 的台股庫存與運算頁的部位建議，算出集中度、偏離與風險，並可存下快照供日後復盤。

**只陳述事實與偏離量，不產出買賣建議** —— 決策是使用者的，工具負責把數字攤開。

## 這個 App 在算什麼（理解輸出的前提）

方舟運算是一套**擇時 + 部位控制**工具：

- **運算頁** 用五個市場指標（全球散戶、外資情緒、位階增溫、社交反指、量能爆衝）算出一個「**建議持股比例**」，再用你的持股市值與現金推出「建議持股金額／建議閒錢／參考調節金額」。
- **調節庫存頁** 為每檔標「**位階**」並給出「**建議調節金額／股數**」——**調節＝賣出側**。
  「≥N」＝建議至少調節 N 股，可多可少；用法是挑幾檔把調節金額**加總，
  覆蓋運算頁的「參考調節金額」**即可，不是每檔都要照做，更**不是持股目標下限**
  （曾反向誤讀成「低於建議」，語意出處見 `../../references/ark-app-map.md`）。

**位階有兩個獨立旗標，一檔可同時掛上**：`價值`＝被低估、App 用於買進側；`升溫`＝已過熱、用於調節側。兩者同時出現**並非矛盾**——官方解讀：價值看**長期**、升溫看**短期**，兩側各自成立（布局挑價值區、調節時升溫區優先），工具會另外標示（見 `tier_alerts`）。位階本身是**連續的綜合評分**（含股價、成交量、K線與 MACD/KD 等指標線），「價值／升溫」只是它的離散表層，與股價高低無絕對關係。

關鍵是：**這些建議全部以你手動維護的庫存為輸入**。庫存過期，所有建議都算錯 —— 所以本 skill 預設會與真實持倉來源（`ark-setup` 設定的 Shioaji API 或 CSV）對帳，**不一致就直接自動執行 `ark-sync` 修好再分析**，不要求使用者自己去跑（`--no-auto-sync` 可關閉；純 ARK 模式自動略過對帳）。

其餘頁面的功能、欄位與作用見 `../../references/ark-app-map.md`（全 App 功能地圖）。

## 執行環境（uv）

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件，
機器不需要預先安裝 Python。執行前先確認 uv 存在，沒有就直接安裝後繼續（不必詢問使用者）：

```bash
command -v uv >/dev/null || brew install uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

curl 安裝落在 `~/.local/bin`；若之後 shell 仍找不到 `uv`，改以 `~/.local/bin/uv` 呼叫。

## 用法

```bash
uv run skills/ark-analyze/analyze.py                    # 分析並與真實持倉對帳
uv run skills/ark-analyze/analyze.py --snapshot         # 順便存下快照
uv run skills/ark-analyze/analyze.py --compare          # 與最近一次快照比較
uv run skills/ark-analyze/analyze.py --no-cross-check   # 略過對帳（較快）
uv run skills/ark-analyze/analyze.py --no-auto-sync     # 對帳不一致時只回報，不自動修
uv run skills/ark-analyze/analyze.py --no-layout        # 略過布局自選頁（較快）
uv run skills/ark-analyze/analyze.py --max-single-pct 25 --max-gap-pct 5
```

門檻預設：單一持股佔比上限 30%、持股比例與 App 建議的容忍差距 10 個百分點。

快照存於 `~/.ark-toolkit/snapshots/`，為 JSON，可自行讀取。

## 輸出內容

- **開場的漂移提示** — 拿 `~/.ark-toolkit/sync-log.jsonl` 最後一筆的檔數與當下比對，
  看得出「上次同步後 ARK 被改動過」
- **集中度** — 各檔市值佔比與**位階**，由大到小
- **App 標示的調節股數** — 轉述每檔「≥N」與金額，並加總對照運算頁「參考調節金額」
- **布局自選的建議張數** — 各標的的位階、位階股數、風控股數（以「風控股數 ＝ 風控布局金額 ÷ 股價」驗算）
- **風控檢查** — 只列出超過設定門檻的項目，含**升溫區持股**與**雙位階**
- **與真實持倉對帳** — 一致與否；不一致時已自動同步並重讀（來源由 `ark-setup` 設定）
- **快照比較**（`--compare`）— 新增／移除／股數與均價變動

## 前置需求

與 ark-sync 相同：macOS、方舟運算已安裝並登入（未開啟會代為啟動）、終端機已取得「輔助使用」權限、已執行 `ark-setup` 設定對帳來源（`--no-cross-check` 或純 ARK 模式時不需要）。執行期間 App 需保持前景。

## 實作注意

- 讀取與解析共用 `lib/ark.py`；本 skill 只放分析邏輯，因此測試不需要 ARK 執行中
- `read_posture` 會切換到運算頁再切回，且**必須先離開編輯庫存頁**（底部 tab 只在非編輯頁存在）
- **黏性子模式**：自選與運算頁的子模式選擇會被 App 記住，而模式切換鈕在所有模式下都存在。
  等到 `風控 運算` 出現就開始讀，可能讀的是離職倒數頁然後靜默回傳 `None`。
  一律改等該頁**獨有**的元素（`持股配置建議`、`全部庫存`），並在讀完後還原模式
  （`ensure_adjust_mode`）——否則下一個讀取者會走錯頁
- 風控訊息刻意避開買賣指示用語，並有測試把關（`test_警示不包含買賣指示用語`）
- **自動同步以 subprocess 呼叫 `ark-sync` 的 CLI**，不 import —— 同步有安全閘、
  總成本驗算與紀錄，走同一支入口才不會生出「analyze 專用的同步」這種第二套實作
- **同步後一律重讀，不管它回報成功或失敗**。`sync.py` 只要有一項失敗就回傳非零，
  曾把「部分成功」當成完全失敗而不重讀，結果整份分析建立在同步前的舊資料上，
  外表卻完全正常 —— 只要動過寫入，手上的 holdings 就過期了

測試：

```bash
cd skills/ark-analyze && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_analyze -v
```
