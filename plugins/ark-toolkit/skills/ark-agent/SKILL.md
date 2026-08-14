---
name: ark-agent
description: 自動交易決策軌：盤中產生決策包、Agent 依 ARK 紀律做出買賣決策並鎖定、自動下單、成交對回、滾動評估。當使用者說「跑今天的決策」「產生決策包」「結算」「看評估成績」時使用。僅限 macOS。
---

# ark-agent

> **通用性原則**：本 plugin 是通用工具，會發佈給其他使用者。個人化內容
> （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾 `~/.ark-toolkit/`
> 或專案自己的 `docs/`，**不要直接改 plugin**；只有通用的修復與功能
> （附測試）才動 plugin 本身。

一條**自動化的每日交易決策軌**：AI Agent 讀取 ARK 讀數與帳戶事實，依 ARK 紀律
做出買賣決策並鎖定，自動下單，盤後對回成交並滾動評估。

**買賣建議只存在於這裡的 journal。** ark-analyze 的設計不變量（只陳述事實、
不產出買賣建議，有測試把關）不受影響，也不可反向污染。

## 運作節奏

- **節奏**：每個工作日 **10:00 盤中決策並鎖定**、**14:30 盤後結算**，由 launchd 自動觸發。
  取 10:00 是因為官方教學明載「指標約每小時重算、**台股盤中算出調台股**」，
  盤前讀到的是前一日盤後的舊值；10:00 也避開 09:00–09:30 的開盤波動
- **執行**：系統自動下單，無人否決（安全由硬性上限與熔斷保證，不由人工把關）
- **評估**：每筆決策的 5/20/60 日前瞻報酬、勝率、對照同期 0050 的超額報酬、ARK 紀律遵循度

## 資訊邊界（Agent 必讀，半開放）

**允許**：
- 當日決策包（packet）的全部內容
- 對 packet `news_scope.codes` 清單內的標的**與其產業**做新聞深度／廣度搜尋

**禁止**：
- 全市場新聞掃描、不在清單內的個股研究
- 盤中即時資訊——決策在盤前鎖定，之後不得修改

違反邊界做出的決策視為無效。搜尋過的新聞要在 decision JSON 的
`news_used` 裡留下摘要與來源，供事後稽核。

## 紀律限制（完全比照 ARK，語意出處：`../../references/ark-app-map.md`）

- 只做現股；**檔數公式**：(持股市值＋閒錢) ÷ 10 萬，90 萬以上封頂 9 檔
- **調節＝賣出側**：挑幾檔把調節金額加總**覆蓋運算頁「參考調節金額」**即可，
  不必每檔照做；**獲利才調節**（虧損賣出侵蝕本金，App 紀律不做）；升溫區優先
- **布局＝買進側**：只挑價值區標的
- **App 顯示參考調節（>0）時，先調節才可買**

這些已算成 packet 的 `discipline` 區塊（機器可查的邊界），`journal.py record`
會以硬規則驗證：違規拒絕寫入，`--override` 可強制寫入但 `violations` 照記、紀律報告照列。
自由度（賣哪幾檔、股數與建議的偏離、限價區間）不擋，事後由 evaluate 量測。

## 執行環境（uv）

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件，
機器不需要預先安裝 Python。執行前先確認 uv 存在，沒有就直接安裝後繼續（不必詢問使用者）：

```bash
command -v uv >/dev/null || brew install uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

curl 安裝落在 `~/.local/bin`；若之後 shell 仍找不到 `uv`，改以 `~/.local/bin/uv` 呼叫。

## 每日流程

整條流程由 `daily.py` 依序驅動，**順序寫死、不可跳過**。Agent 只佔第 3 步，
而且那一步**不給 Bash 工具**——它產出 decision.json 之後就沒有話語權。若讓 LLM
自己編排，它可能「忘記」跑風控或驗證，硬規則就不再是硬的。

> **多帳戶注意**：帳戶清單含檔案帳戶（CSV/Excel）時，第 1 步的自動 sync
> 需要**當日已跑過 ark-collect**（快照僅當日有效），否則會停下要求先收集。
> 全自動排程建議帳戶清單只留永豐 Shioaji。

```bash
# 10:00 決策執行
uv run skills/ark-agent/packet.py            # 1. 事實（自動含：對帳不一致先 sync）
uv run skills/ark-agent/risk.py              # 2. 執行邊界 envelope
#                                              3. ★ Agent 讀 packet+envelope → decision JSON
uv run skills/ark-agent/journal.py record /tmp/decision.json   # 4. 紀律驗證＋鎖定
uv run skills/ark-agent/execute.py --backend live              # 5. 送單

# 14:30 盤後結算（daily.py settle 依序跑這五步，每步盡力做完再回報）
uv run skills/ark-agent/journal.py settle    # 1. 成交對回
uv run skills/ark-agent/equity.py            # 2. 淨值與熔斷狀態
uv run skills/ark-agent/dividends.py         # 3. 除權息資料（報酬校正用，須每日累積）
uv run skills/ark-sync/sync.py --allow-delete --with-cash   # 4. 庫存與現金同步回 ARK
uv run skills/ark-agent/record_return.py     # 5. 當日已實現獲利記進離職倒數（僅獲利日）

# 隨時看成績
uv run skills/ark-agent/evaluate.py          # --json 出機器格式；--offline 不連線
```

## 排程安裝（launchd）

```bash
cp skills/ark-agent/launchd/*.plist ~/Library/LaunchAgents/
for j in decide settle; do launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hjc.ark-agent.$j.plist; done

# 改動排程或系統升級後，在真正的 launchd 環境下驗一次前置條件
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hjc.ark-agent.check.plist
cat ~/.ark-toolkit/agent/launchd.check.out
```

⚠️ **launchd 的進入點必須是 `uv`，不能是 `/bin/bash`。** macOS 封鎖系統二進位檔
在 launchd 下存取 `~/Documents` 等 TCC 保護目錄（防止用腳本繞過權限）——實測
`/bin/bash` 連讀取 `daily.py` 都會 `Operation not permitted`（離開碼 126）。
`uv` 這類第三方 binary 有自己的 TCC 身分，讀得到。AX（輔助使用）權限在 launchd
下則正常，實測 `AXIsProcessTrusted() = True`。

`decide` 不補跑（`StartCalendarIntervalRunAtLoad=false`）：10:00 的判斷在 13:00
執行是另一回事，盤中價格早已不同。`settle` 會補跑——那是對既成事實的記錄，
晚幾小時做結果一樣，漏做卻會讓熔斷基準斷掉。

## 軌道模型

`~/.ark-toolkit/agent/tracks.json` 指定每個部位屬於哪一軌，決定它適用哪套規則。
**軌道不自動推斷**——開新衛星標的必須有人手動寫進去。

| 軌道 | ARK 紀律 | 停損 | 說明 |
|---|---|---|---|
| `core` | 完全適用 | 無 | 只做 ETF。預設軌，evaluate 只統計這一軌 |
| `satellite` | **豁免** | **−12%** | 個股／產業／槓桿 ETF；配額 25%，需在 allowlist 內 |
| `inherited` | — | — | 虧損中且 ARK 不可賣，凍結至轉正；**不佔衛星軌配額** |
| `frozen` | — | — | 低於最小可交易金額，不進出場計算 |

**「先調節才可買」兩軌都適用**：那是 ARK 的帳戶級曝險訊號，若衛星軌能在被要求
調節時加碼，帳戶總曝險就失控了。

## 失效保護：預設永遠是「不交易」

任一條成立即跳過當日交易並記錄原因，**絕不猜測**：ARK App 未開／AX 讀取失敗／
`check_parsed` 不過／**運算頁讀不到（隱私眼睛開啟會讓 `read_posture` 靜默回
`None`，紀律邊界隨即退化成現金為零、不需調節）**／對帳不一致／Shioaji 登入或
CA 啟用失敗／`packet_hash` 不符／熔斷 L2 已觸發／當日已有決策。

看到 `news_scope` 標的的除權息公告時，補一行到 `~/.ark-toolkit/agent/dividends.jsonl`：
`{"code": "0056", "ex_date": "2026-08-19", "cash": 1.2, "stock_ratio": 0}`
（0050 的配息也要記——它是基準，1 月與 7 月除息，60 日視窗大概率遇到。）

## decision JSON 格式

```json
{
  "date": "2026-08-10",
  "packet_hash": "sha256:…（抄當日 packet 的 hash，綁定決策依據）",
  "no_trade": false,
  "orders": [
    {"action": "sell", "code": "0057", "qty": 76,
     "limit_low": 305.0, "limit_high": 310.0,
     "ark_basis": {"signal": "suggest_qty", "value": 76, "tiers": ["價值", "升溫"]},
     "reason": "升溫且獲利，單檔即覆蓋參考調節金額的一半"}
  ],
  "rationale": "整體判斷理由，引用 posture.gap 與位階警示",
  "news_used": [{"code": "0057", "summary": "…", "source": "…"}]
}
```

無操作也要記：`"no_trade": true, "orders": []`——「今天不動」也是一筆決策。

## 鎖定機制

- journal 為 **append-only** JSONL；每筆決策帶 `lock`（內容雜湊）與 `packet_hash`
- **first-decision-wins**：同日第二筆標 `amended`，evaluate 只認第一筆；
  修訂不會被隱藏，紀律報告照列
- 缺席（沒跑 packet 或沒做決策）由隔日的 settle 自動記 `missed`，不回填

## 邊界情況

- **週末**：packet 拒跑（`--force` 可強制）；**國定假日**：settle 以 0050 當日
  有無行情判定休市，不會誤記缺席
- **停牌**：evaluate 標 `insufficient_bars`（退用視窗內最後可用價）或
  `unevaluable`，剔除彙總但筆數照報
- **未成交**：退用決策日收盤價評估並標 `no_fill`；滑價照錄

## 統計誠實條款

決策日樣本少、前瞻視窗互相重疊（自相關），**不做統計檢定、不宣稱優劣的定論**。
evaluate 只輸出點估計＋樣本數＋pending 筆數；60 日前瞻報酬要等決策後再
60 個交易日才齊，`pending` 常態存在。
測試以 `test_評估輸出不含顯著性宣稱用語` 把關。

## 檔案

- `packet.py` — 盤前決策包（ARK 讀數＋帳戶＋行情＋紀律邊界 → JSON）
- `journal.py` — 決策鎖定（record）、成交對回（settle）、日誌（show）
- `evaluate.py` — 四類指標滾動評估
- `market.py` — Shioaji 行情層（單一 session、分K→日K 聚合、快取）
- `tracks.py` — 軌道歸屬（core／satellite／inherited／frozen），決定每個部位適用哪套規則
- `equity.py` — 每日淨值曲線與熔斷判定（基準為歷史峰值，非期初本金）
- `risk.py` — 風控層：熔斷、軌道配額、衛星軌停損、金額上限 → 執行邊界 envelope
- `execute.py` — 下單層：股數→張數換算、價格檔位對齊、送出前檢查、`place_order`
- `daily.py` — 每日編排（decide／settle／check），順序寫死；launchd 的進入點
- `preflight.py` — 前置檢查（AX 權限、券商連線），無副作用只讀
- `phase0.py` — 開局整理（一次性、有人監督、預設 dry-run）。**豁免金額上限**，
  因為那些上限是為了限制無人值守時的爆炸半徑；紀律照守，繼承軌一股不賣。
  刻意獨立成一支腳本而非在 execute.py 開繞過旗標——那種旗標日後一定會被誤用
- `prompts/decide.md` — 決策層的提示模板；`launchd/` — 三份排程設定
- 委託路徑實機驗證在 **ark-start** skill（`skills/ark-start/verify.py`，寫死 simulation，
  碰不到真錢）——切 live 前先跑它
- 持久化：`~/.ark-toolkit/agent/`（packets/、envelopes/、prices/、journal.jsonl、
  equity.jsonl、tracks.json、satellite_exits.jsonl、dividends.jsonl）

測試（純邏輯，不需要 ARK 或 Shioaji，任何平台可跑）：

```bash
cd skills/ark-agent && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_market test_packet test_journal test_evaluate test_risk test_tracks test_equity test_execute test_daily test_phase0 -v
```

## 前置需求

與 ark-sync 相同：macOS、方舟運算已安裝並登入（未開啟會代為啟動）、
終端機已取得「輔助使用」權限、已執行 `ark-setup` 設定 Shioaji 來源。
執行 packet 期間 App 需保持前景。
