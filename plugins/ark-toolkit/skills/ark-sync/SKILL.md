---
name: ark-sync
description: 把方舟運算(ARK) App 的台股庫存同步成真實持倉（帳戶清單由 ark-setup 設定；多帳戶時先由 ark-collect 收集確認）。當使用者要求同步／對帳 ARK 與券商持股、更新 ARK 的股數或成交均價、或提到「方舟運算」「ARK」與庫存不符時使用。僅限 macOS。
---

# ark-sync

> **通用性原則**：本 plugin 是通用工具，會發佈給其他使用者。個人化內容
> （帳戶設定、排程參數、專案協定、一次性調整）放執行資料夾 `~/.ark-toolkit/`
> 或專案自己的 `docs/`，**不要直接改 plugin**；只有通用的修復與功能
> （附測試）才動 plugin 本身。

以 **設定的真實持倉為準**（帳戶清單由 `ark-setup` 設定），讓方舟運算的台股庫存向它對齊（修改／新增／刪除）。純 ARK 模式（不對帳）下本 skill 停用。

## 來源判斷（兩階段同步的階段二）

| 帳戶清單 | 行為 |
|---|---|
| 只有永豐 Shioaji | 直接即時讀取，不需要先跑 ark-collect |
| 含檔案帳戶（CSV／Excel） | **只讀 `~/.ark-toolkit/staging.json`**（ark-collect 確認過的當日快照）。缺快照→報「請先執行 ark-collect」；跨日→報過期要求重收，無 override |

確認過的才是實際套用的——sync 絕不在多帳戶時自行重讀來源，
否則「確認的」與「套用的」可能不同。

ARK 沒有官方 API，本 skill 透過 macOS Accessibility API 操作 App 的 UI。

## 僅限 macOS

依賴 macOS Accessibility API（`AXUIElement`）、`pyobjc`，以及 Apple Silicon 上的 iOS App 相容層（ARK 是 wrapped iOS App）。其他平台在入口直接報錯，不會嘗試執行。

## 執行前必須確認

1. **方舟運算已安裝並登入**（未開啟會代為啟動；未安裝會明確提示）
2. **終端機／IDE 已獲得「輔助使用」權限**（系統設定 → 隱私權與安全性 → 輔助使用）
3. 已執行 `ark-setup` 設定帳戶清單（Shioaji 需 API 憑證，由精靈以原生視窗收集；檔案帳戶不需憑證）；含檔案帳戶時當日已跑過 `ark-collect`
4. **執行期間 ARK 視窗保持可見即可**（不必前景，可繼續用電腦）—— 隱藏（Cmd+H）或最小化會讓它被 iOS 生命週期暫停、AX 全部失效。所有操作走 AX action 與 `CGEventPostToPid`，不動游標、不佔鍵盤焦點

## 執行環境（uv）

所有指令以 `uv run` 執行：依賴宣告在腳本開頭（PEP 723），uv 會自動準備 Python 與套件，
機器不需要預先安裝 Python。執行前先確認 uv 存在，沒有就直接安裝後繼續（不必詢問使用者）：

```bash
command -v uv >/dev/null || brew install uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

curl 安裝落在 `~/.local/bin`；若之後 shell 仍找不到 `uv`，改以 `~/.local/bin/uv` 呼叫。

## 用法

一律先 dry-run 確認差異，再實際執行：

```bash
uv run skills/ark-sync/sync.py --dry-run        # 只顯示差異，不寫入
uv run skills/ark-sync/sync.py                  # 執行（略過刪除）
uv run skills/ark-sync/sync.py --allow-delete   # 含刪除
uv run skills/ark-sync/sync.py --allow-delete --force   # 略過安全閘
uv run skills/ark-sync/sync.py --no-prompt      # 排程用：缺均價口徑時不開視窗
```

刪除預設略過，需明確加旗標。執行後再跑一次 `--dry-run` 確認結果。

## 均價口徑：第一次問一次，之後沿用

券商 API（`list_positions`）給的均價是**不含息、不含手續費**的純買進均價。
要把已領現金股利扣掉、或把買進手續費加進成本，得從每筆買進分錄
（`list_position_detail`）加總換算——四種口徑：

| `cost_basis` | 均價 = | 意義 |
|---|---|---|
| 不含息、不含手續費（預設） | 券商原值 | 與券商 App 庫存頁一致 |
| 不含息、含手續費 | (Σ金額 ＋ Σ手續費) ÷ 股數 | 實際付出的錢 |
| 含息、不含手續費 | (Σ金額 － Σ已領股息) ÷ 股數 | 股息視為成本回收 |
| 含息、含手續費 | (Σ金額 ＋ 手續費 － 已領股息) ÷ 股數 | 最接近真實投入 |

這是使用者的投資觀點，工具不代選：**有 Shioaji 帳戶且 `config.json` 還沒有
`cost_basis` 時，sync（單帳戶）或 collect（多帳戶）啟動會開原生視窗問一次**，選了就
寫進 config、之後永久沿用；按取消則本次用券商原值、不存、下次再問。之後想改，跑
`ark-setup` 按「完成」時會再問。`--no-prompt`（排程 `daily.py` 一律帶）下缺設定不問，
直接用券商原值。

口徑套用在 `source.read_account_positions(account, basis)`（`basis` 必填，漏傳會當場
報錯而非靜默退回原值）——ark-sync／ark-collect／ark-read／ark-analyze 四條路都經過它。
ark-agent 的 equity／journal／packet 仍用券商原值——journal 靠「均價 diff」反推
買進成交價，均價若含息，除息日就會反推出錯的價格。

**換算前必驗算** Σ分錄金額 ÷ 股數 ≈ 券商均價（容差 0.01），對不上就指名代號中止，
不寫進 ARK。原因：分錄的 `price` 欄實測是**該筆總金額**不是單價、零股 `quantity`
回 0（2026-08-22 實測，全為零股樣本），整張持倉的語意沒驗證過——寧可中止也不寫錯值。
檔案帳戶只有一欄均價，口徑對它無效，`describe()` 只在有 Shioaji 時附上口徑。

`ark-analyze` 對帳不一致時會自動呼叫本工具（含 `--allow-delete`），使用者不需手動執行。

## 安全閘：不讓讀取失敗變成刪光庫存

`sync_is_safe` 在執行前擋下兩種情況，`--force` 才能略過：

1. **來源回傳 0 檔** —— Shioaji 登入失敗時就是回空 dict（檔案空檔則直接報錯），
   而 `plan_changes` 會照規則判定「ARK 全部刪除」
2. **刪除量超過半數**

多帳戶快照另有更上游的防線：`write_staging` 對任一帳戶 0 檔直接拒寫
（見 ark-collect），sync 端安全閘照樣把關。

這與 `check_parsed` 防的是同一類「失敗偽裝成成功」，差別在發生於**寫入端**：
讀取端讀錯只是報告不準，寫入端寫錯是庫存不見了。

## 同步紀錄

每次實際執行（非 dry-run）會在 `~/.ark-toolkit/sync-log.jsonl` 追加一行：
時間、同步前後的 ARK 檔數、來源檔數、套用了哪些動作、成功／失敗數、均價口徑。
收尾的檔數取自 App 自己宣告的「總共 N 檔」，不是推算的。

`ark.drift_since_last_sync` 拿最後一筆與當下的 ARK 檔數比對，
是不必連 Shioaji 的廉價前置檢查 —— 但**只看得見 ARK 這一側**的漂移，
券商端的變動（賣出、除權息）仍須真的去讀 Shioaji。

測試（純邏輯，不需 ARK 執行中，任何平台可跑）：

```bash
cd skills/ark-sync && PYTHONPATH=../../lib uv run --no-project --python 3.13 python -m unittest test_sync -v
```

## 編輯頁的捲動：不能用輸入事件，但 AX action 可以

編輯庫存頁在 `isEditing` 狀態下，reorder 手勢辨識器吃掉了**輸入事件** —— 滾輪、Page Down、方向鍵、Tab、拖到邊緣自動捲動全部無效，離屏 cell 也不在 AX tree 裡。

但 **AX action 不是輸入事件**：`AXScrollDownByPage` 由 UIKit 的 accessibility 層直接派送給 scroll view，繞過手勢辨識器。實測回傳 0 且畫面確實捲動：

```
捲動前：['0053', '2330', '2308', '00941', '00911', '00893', '00876', '00861']
捲動後：['00893', '00876', '00861', '00830', '00635U', '0052', '0051', '0050']
```

`scroll_to_row` 據此運作：**先 `AXScrollUpByPage` 翻到頂，再 `AXScrollDownByPage` 逐頁找**，每翻一頁重讀畫面上有誰。不能對目標列做 `AXScrollToVisible` —— 離屏的列根本不在 AX tree 裡，沒有元素可以引用。

因此**沒有檔數限制，也不需要使用者介入**。（2026-08-07 以前的版本會請使用者手動捲動，第 12 列的 0051 就因此刪除失敗。）

編輯頁的每一列另外還暴露自訂 action `Name:向上移動` / `Name:向下移動`，可不靠拖曳就重排。

## 操作這個 App 的六個陷阱

實作已處理，修改程式碼時務必保留：

1. **`AXSetValue` 是假成功** —— 欄位顯示會變、回報成功，但沒觸發 `editingChanged`，App 模型收不到，儲存會存進舊值。必須 `AXPress` 聚焦後送真實鍵盤事件。
2. **一律用「總成本」驗算** —— 那行字是 App 自算的股數 × 均價，是唯一可信的獨立見證。不要拿剛寫進去的欄位自己作證。驗算不過就取消，不儲存。
3. **被遮擋的元素 `AXPress` 回傳 0 但無效** —— (a) 打完字後 iOS 輸入輔助列的「完成」鈕會蓋住儲存鈕，它不在 AX tree 裡，只能用座標點（`ax.dismiss_keyboard`）；(b) 列表被推移後第一列會躲到表頭下，靠每檔操作後重進編輯頁重置。**動作莫名失效時先截圖看畫面，不要繼續追問 AX。**
4. **「新增持股」不能改既有部位** —— 會出現紅色錯誤 `已存在此檔現股…`，儲存鈕雖 enabled 但一律拒絕。修改只能從列表點進「編輯持股」。
5. **不要用固定 `sleep` 假設操作完成** —— App 從別頁返回的載入時間不定，固定等待會造成「手動測試會過、自動連續執行卻失敗」的間歇性錯誤。一律用 `ark.wait_for` 等狀態；捲動用 `ark.scroll_until_stable` 捲到內容不變為止。
6. **絕不使用排序鈕與拖曳** —— 兩者都會永久改變使用者拖曳出來的自訂排列且無法還原。程式碼中沒有任何地方碰它們，請維持。
7. **不能用 `osascript` 的 keystroke 送文字** —— 它會經過**當前輸入來源**。中文輸入法在全形模式下會把 `2308` 打成 `２３０８`，搜尋於是一無所獲（2026-08-06 實測，欄位值 `'２３０８'`），股數與均價也會寫進全形數字。`ax.keystroke` 已改用 `CGEventKeyboardSetUnicodeString` 直接指定字元，繞過輸入法。**這個 bug 只在使用者切到中文輸入法時才出現，環境一換就消失，極難重現。**
8. **彈窗的關閉鍵不一定在 AX tree 裡** —— 「新增台股持股」右上角的 X 就不在（同陷阱 3a）。而且搜尋下拉開著時要點兩次：第一次只收下拉。`_close_popup` 用「按到 `新增持股` 重新出現為止」自我驗證，不要退回 `by_desc(...)[0]` —— 那在找不到時會拋 `IndexError`，把已處理的失敗變成 traceback。

## 解析欄位時的陷阱

調節庫存頁的「位階」與「建議調節金額／股數」是**可選欄位**，App 未算出建議時整組消失。因此 `parse_holding` 一律**從尾端定位**固定的 7 個數值欄，不能用頭部位置去數 —— 曾因此把整份庫存讀成空的，而 diff 邏輯把「空」當成合法狀態，判定要新增全部持股（失敗偽裝成成功）。

防線是 `check_parsed`：解析結果必須對得上 App 自己宣告的「總共 N 檔」，少了就報錯。

## 檔案

- `sync.py` — 差異計算與編輯頁寫入流程
- `test_sync.py` — 純邏輯測試
- `../../lib/ark.py` — 共用的讀取與解析層
- `../../lib/source.py` — 真實持倉來源層（帳戶清單、多帳戶合併、收集快照）
- `../../lib/ax.py` — macOS Accessibility 操作層，唯一匯入 pyobjc 的地方
- `../../references/ark-app-map.md` — 全 App 功能地圖（各頁結構、AX 陷阱清單）
