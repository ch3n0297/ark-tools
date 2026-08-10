---
name: ark-explore
description: 探索方舟運算(ARK) App 的全 App 結構，產出給 Agent 讀的功能地圖。當使用者問「這個 App 有什麼功能」「策略頁在幹嘛」「離職倒數怎麼用」，或 App 改版後要更新地圖時使用。僅限 macOS。
---

# ark-explore

唯讀走訪 ARK 全 App，把每一頁的結構記下來，存成 JSON 快取
（`~/.ark-toolkit/app-map/<版本號>.json`）。`references/ark-app-map.md`（功能地圖）與
`references/ark-app-ux.svg`（UX 流程圖）是以探索結果為底、再由人工補上語意說明的成品——
探索不會自動覆寫它們；`--markdown` 只輸出改版時重寫地圖用的 Guide 骨架。

**要回答「這個 App 在幹嘛」的問題，直接讀 `references/ark-app-map.md` 就好，不必跑這個 skill。**
只有 App 改版、地圖對不上現況時才需要重跑。

## 執行環境（uv）

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件，
機器不需要預先安裝 Python。執行前先確認 uv 存在，沒有就直接安裝後繼續（不必詢問使用者）：

```bash
command -v uv >/dev/null || brew install uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

curl 安裝落在 `~/.local/bin`；若之後 shell 仍找不到 `uv`，改以 `~/.local/bin/uv` 呼叫。

## 用法

```bash
uv run skills/ark-explore/run.py             # 版本沒變就跳過（探索一次要 8 分鐘）
uv run skills/ark-explore/run.py --force     # 強制重跑
uv run skills/ark-explore/run.py --show      # 只印既有快取，完全不碰 App
uv run skills/ark-explore/run.py --probe     # 只 dump 當前畫面 AX tree，不按任何東西
uv run skills/ark-explore/run.py --markdown out.md   # 順便輸出 Guide 骨架
```

快取存於 `~/.ark-toolkit/app-map/<版本號>.json`。

## 版本號當快取鍵

版本號在**設定頁**（格式 `Version: 1.7.4.0`）。版本相同就跳過探索。

讀不到版本號時**一律完整探索**，不會退而求其次去命中某個快取 ——
拿不可靠的鍵去命中快取，會讓「沒探索到」偽裝成「已經探索過」。

**版本號只能當「UI 結構」的快取鍵，不能當「內容」的快取鍵。** 達人觀點今天是誰、
活動中心有什麼活動每天都在變，而版本號不會跟著變。所以地圖只描述結構與功能意義，
不記錄當日內容。

## 探索原則

**按按鈕是為了到達新頁面，不是為了觸發動作。** 按下後畫面沒換頁的元素會被標記為
動作型並不再重按。排序鈕、儲存鈕、確認鈕依此原則自動排除 —— 不是因為危險，
是因為按了不會產生新的地圖節點。

不按的東西只有兩類：

| 類別 | 內容 | 理由 |
|---|---|---|
| 金流／登出 | 訂閱・購買・付款・升級・續約・退款・退訂・登出 | 不可逆；登出後我們沒有密碼 |
| 提交鈕 | 確認・確定・儲存・送出・全選 | 「確認」會重啟 App、「儲存」會改掉離職金額目標，而且都不換頁 |

排序與拖曳**未被禁止**（使用者已明確授權），但依上述原則探索器不會主動按。

## 操作這個 App 的六個陷阱

實作已處理，修改程式碼時務必保留。這些都是實際踩到才發現的：

1. **返回鍵的標籤各頁不一致** —— 自選頁叫 `back`，大盤詳情頁叫 `方舟運算`（左上角 App logo），
   modal 叫 `popup close`（右上角）。**用位置判定比用名字可靠**（`pick_back`），
   modal 則用精確名單（`pick_dismiss`）。三道逃生門依序試，最後一道是按畫面上任一個已知 tab。
2. **底部 tab 不能只用「同一列 ≥3 個」判定** —— 大盤詳情頁的
   `成交量(億) / 9,421.57 / 昨量(億) / 12,002.13` 正好同列且 4 個，曾被當成 tab，
   害 `go_home` 以為已回到根頁，整趟探索從錯的根開始。必須再加 **AXButton** 與 **貼齊視窗底部** 兩個條件。
3. **App 會記住子分頁的選擇** —— 走過空的「自選 › 美股庫存」之後，每次回到自選都落在那頁，
   兄弟節點全部走不到。因此失敗計數是**逐根**的：一棵子樹走死就跳過它，不能拖垮整趟探索。
4. **離屏元素 `AXPress` 回傳 0 但靜默失效** —— 策略頁橫向捲軸的元素 x 到 1247，
   而視窗只有 375 寬。不先過濾就會被誤分類成「動作型」。
5. **小寫英文 AX id ＝ 控制項** —— `add stock off`、`watchlist edit`、`navigation search icon`…
   內容區段一律是中文。控制項多半是編輯／搜尋入口，會開出 modal 把探索器困住。
6. **有些項目會開外部瀏覽器** —— 設定 › APP使用教學、方舟擺渡人計畫。ARK 被推到背景後
   iOS 會暫停它，AX 全數失效並拋例外。`walk` 必須接住例外、把 App 拉回前景繼續，
   **絕不能讓一次例外把已收集的資料一起陪葬**（實際發生過，32 頁全丟）。

## 畫面指紋為什麼要剝掉數字

判斷「按下去有沒有換頁」靠比對畫面指紋。股價每秒在跳，含數字的指紋會讓同一頁被誤判成新頁，
進而把動作型元素誤分類成導航。但剝光後全數字的畫面會塌成同一個指紋，所以補上節點數當第二維度。

副作用是好的：排序鈕只改變列的順序、不改變文字集合，指紋因此不變，自動歸類為動作型。

## 檔案

- `run.py` — CLI 入口
- `test_explore.py` — 純邏輯測試
- `../../lib/explore.py` — 探索引擎（頂層不匯入 pyobjc，AX 以參數注入）
- `../../lib/ax.py` — macOS Accessibility 操作層
- `../../references/ark-app-map.md` — 產出的功能地圖
- `../../references/ark-app-ux.svg` — 產出的 UX 流程圖

測試（純邏輯，不需要 ARK 執行中，任何平台可跑）：

```bash
cd skills/ark-explore && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_explore -v
```

## 前置需求

與 ark-sync 相同：macOS、方舟運算已安裝並登入（未開啟會代為啟動）、終端機已取得「輔助使用」權限。
**執行期間不要操作電腦** —— App 必須保持前景。完整探索約 8 分鐘。
