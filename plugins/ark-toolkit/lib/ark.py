"""方舟運算(ARK) 的讀取與解析層，供各 skill 共用。

本檔頂層不匯入 pyobjc —— AX 操作一律以 `ax` 模組當參數傳入，
因此純解析邏輯可在任何平台測試。
"""
import datetime as _dt
import json
import os
import re
import sys
import time
from typing import NamedTuple, Optional

SYNC_LOG = os.path.expanduser("~/.ark-toolkit/sync-log.jsonl")

# 布局自選頁的版面幾何。**v1.7.4 之後變過**：列高 70→54、上下兩排相對列頂的
# 偏移 15/36→11/27。舊值會讓下排落進上排、又把下一列的上排吃進本列下排，
# 欄位整排錯位 → 一致性檢查失敗 → 整頁讀成 0 檔。那是靜默失敗：系統會誤判
# 「沒有買進候選」而永遠不買，卻不報任何錯。改版時先量座標再改這兩個數。
LAYOUT_ROW_H = 54     # 布局自選頁每列高度
LAYOUT_SPLIT = 20     # 同一列內上下兩排的分界（相對列頂）
TAIL_FIELDS = 7       # 調節庫存頁尾端固定欄位數（見 parse_holding）
KIND_TAILS = ("股", "資", "券")   # 「現股／融資／融券」在 desc 中被換行拆成兩半
PRICE_TOL = 0.005     # 均價比對容差（sync 與 analyze 共用）


class UnsupportedPlatform(RuntimeError):
    pass


class ParseFailed(RuntimeError):
    pass


class Holding(NamedTuple):
    """調節庫存頁的一列"""
    code: str
    qty: int
    price: float                        # 成本均價
    cost: float                         # 總成本
    value: float                        # 總市值
    pnl: float                          # 總損益
    roi: float                          # 報酬率(%)
    today_pnl: float                    # 今日損益
    tiers: tuple                        # 位階，一檔可能同時是（價值, 升溫）；App 未給則為空
    suggest_amount: Optional[float]     # 建議調節金額（調節＝賣出側）
    suggest_qty: Optional[int]          # 建議調節股數（「≥N」＝至少調節 N 股）


class Layout(NamedTuple):
    """布局自選頁的一列（尚未持有、或觀察中的標的建議張數）"""
    code: str
    tiers: tuple                # 位階，一檔可能同時是（價值, 升溫）
    price: float                # 股價
    change: str                 # 漲跌幅，保留原文（▼/▲ 帶方向資訊）
    nav: float                  # 即時淨值
    premium: float              # 折溢價(%)
    tier_qty: int               # 位階股數
    tier_amount: float          # 位階布局金額
    risk_qty: int               # 風控股數
    risk_amount: float          # 風控布局金額


class LayoutView(NamedTuple):
    """布局自選頁的一次讀取結果。

    數字屬於哪一份自選清單必須跟著回去——換一份清單數字全變，
    只回傳 rows 的話輸出看不出來源，就成了另一種「失敗偽裝成成功」。
    """
    watchlist: str
    rows: dict


class Posture(NamedTuple):
    """運算頁的整體部位建議"""
    suggested_ratio: float      # App 建議的持股比例(%)
    stock_value: float          # 目前持股市值
    cash: float                 # 現金
    suggested_value: float      # 建議持股金額
    suggested_cash: float       # 建議閒錢
    adjust_amount: float        # 參考調節金額

    @property
    def total(self):
        return self.stock_value + self.cash

    @property
    def actual_ratio(self):
        return 100.0 * self.stock_value / self.total if self.total else 0.0

    @property
    def gap(self):
        """實際持股比例 - 建議持股比例（正 = 部位過重）"""
        return self.actual_ratio - self.suggested_ratio


def check_platform(platform=None, tool="ark-toolkit"):
    """本工具依賴 macOS Accessibility API 與 Apple Silicon 的 iOS App 相容層。"""
    p = platform if platform is not None else sys.platform
    if p != "darwin":
        raise UnsupportedPlatform(
            f"{tool} 僅支援 macOS（偵測到 {p}）。"
            "它依賴 macOS Accessibility API 與 Apple Silicon 上的 iOS App 相容層，"
            "在其他平台無法運作。"
        )


# ---------------------------------------------------------------- 解析

def num(s):
    """把 '≥ 1,343'、'+15,881'、'-21.72%' 這類欄位轉成數字。

    None（AXValue 缺失）視為空值回傳 0.0，不能落到 float("None") 直接崩潰。
    """
    if s is None:
        return 0.0
    cleaned = re.sub(r"[≥≤+%\s]", "", str(s)).replace(",", "")
    return float(cleaned) if cleaned not in ("", "-") else 0.0


def split_fields(desc):
    """千分位逗號後面緊接數字，欄位分隔則是「逗號+空白」——據此安全切分。"""
    return [p.strip() for p in re.split(r"[,\n]", re.sub(r",(?=\d)", "", desc)) if p.strip()]


def parse_holding(desc):
    """解析調節庫存頁的一列，回傳 Holding。

    欄位順序（依表頭）：
        名稱, 代號, [位階], 種類, [建議調節金額], [建議調節股數],
        總成本, 持有股數, 總市值, 總損益, 報酬率, 今日損益, 成本均價

    位階與「建議調節」兩欄是**可選的**（App 未算出建議時整組消失），
    因此一律從尾端定位固定的 7 個數值欄，不能用頭部位置去數。

    位階本身也是變動長度：一檔可同時掛「價值」與「升溫」。用「現」的位置
    （`kind_idx - 1`）當右界，格數由資料自己決定——曾寫死成 `kind_idx == 4`
    只認一格，雙位階的股票於是被讀成「沒有位階」，而那正是價值與升溫同時
    亮起、最需要人工判斷的一類。
    """
    parts = split_fields(desc or "")
    if len(parts) < TAIL_FIELDS + 3:
        return None
    kind_idx = next((i for i, p in enumerate(parts) if p in KIND_TAILS), None)
    if kind_idx is None or kind_idx < 3:
        return None

    tail = parts[-TAIL_FIELDS:]
    middle = parts[kind_idx + 1: len(parts) - TAIL_FIELDS]
    try:
        cost, qty, value, pnl, roi, today, price = (num(x) for x in tail)
    except ValueError:
        return None

    return Holding(
        code=parts[1], qty=int(qty), price=price, cost=cost, value=value,
        pnl=pnl, roi=roi, today_pnl=today,
        tiers=tuple(parts[2:kind_idx - 1]),
        suggest_amount=num(middle[0]) if len(middle) == 2 else None,
        suggest_qty=int(num(middle[1])) if len(middle) == 2 else None,
    )


def parse_edit_row(desc):
    """解析編輯庫存頁的列：名稱, 代號, 種類, 股數, 均價

    「重新排列…」是拖曳把手的 label，格式相同但不是資料列，必須排除。
    """
    m = re.match(
        r"^(?!重新排列)[^,]+, (\w+), (?:現股|融資|融券), ([\d,]+), ([\d,]+(?:\.\d+)?)$",
        desc or "",
    )
    return (m.group(1), int(num(m.group(2))), num(m.group(3))) if m else None


def split_layout_cells(cells, row_top, row_height=LAYOUT_ROW_H, split=LAYOUT_SPLIT):
    """把布局自選頁一列的儲存格分成上下兩排，各自由左至右。

    這一頁的數值不在名稱列裡，而是各自獨立的元素，只能靠座標對回列。
    `cells` 為 [(x, y, text)]；只收 row_top 之下、不到下一列的部分，
    吃到下一列的話整排欄位都會錯位。
    """
    upper, lower = [], []
    for x, y, text in cells:
        offset = y - row_top
        if 0 < offset < split:
            upper.append((x, text))
        elif split <= offset < row_height:
            lower.append((x, text))
    return ([t for _x, t in sorted(upper)], [t for _x, t in sorted(lower)])


def parse_layout(desc, upper, lower):
    """解析布局自選頁的一列，回傳 Layout。

    欄位順序（依表頭，由左至右）：
        上排：股價、即時淨值、位階股數、位階布局金額
        下排：漲跌幅、折溢價%、風控股數、風控布局金額

    名稱列形如「元大全球5G, 00876, 價值」，位階可能有兩個（價值, 升溫）。
    """
    parts = [p.strip() for p in (desc or "").split(",") if p.strip()]
    if len(parts) < 2 or len(upper) < 4 or len(lower) < 4:
        return None
    try:
        return Layout(
            code=parts[1], tiers=tuple(parts[2:]),
            price=num(upper[0]), nav=num(upper[1]),
            tier_qty=int(num(upper[2])), tier_amount=num(upper[3]),
            change=lower[0], premium=num(lower[1]),
            risk_qty=int(num(lower[2])), risk_amount=num(lower[3]),
        )
    except ValueError:
        return None


def layout_is_consistent(row, tol=1):
    """外部見證：風控股數應等於 風控布局金額 ÷ 股價 取整。

    欄位若錯位這個等式不會成立。與 ark-sync 拿「總成本」驗算是同一套思路——
    不能拿剛解析出來的欄位自己作證。
    """
    if row is None or row.price <= 0:
        return False
    return abs(row.risk_qty - int(row.risk_amount // row.price)) <= tol


def looks_like_holding_row(desc):
    """看起來是持股列（有種類欄與足夠欄位），用來偵測解析失效。"""
    parts = split_fields(desc or "")
    return len(parts) >= TAIL_FIELDS + 3 and any(p in KIND_TAILS for p in parts)


def check_parsed(parsed, declared):
    """健全性檢查：解析結果必須對得上 App 自己宣告的「總共 N 檔」。

    少了就是解析失效，必須報錯——絕不能把「讀不到」靜默當成「庫存是空的」，
    那會讓 diff 判定要新增全部持股。
    """
    if declared is None:
        return
    if len(parsed) < declared:
        raise ParseFailed(
            f"只解析出 {len(parsed)} 檔，但 App 顯示「總共 {declared} 檔」。"
            "欄位格式可能已變動，請檢查 parse_holding。"
        )


# ---------------------------------------------------------------- 同步紀錄

def append_sync_log(entry, path=SYNC_LOG):
    """追加一筆同步紀錄（JSONL，一行一筆）。

    追加而非覆寫：這份紀錄本身就是復盤用的時間序，
    而且中途出錯時先前寫入的行仍然完好。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def last_sync(path=SYNC_LOG):
    """最後一筆同步紀錄，沒有則為 None。

    壞掉的行直接略過 —— log 是輔助資訊，不該因為一行壞掉就讓主流程掛掉。
    """
    if not os.path.exists(path):
        return None
    latest = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                latest = json.loads(line)
            except ValueError:
                continue
    return latest


def drift_since_last_sync(last, ark_count):
    """上次同步之後 ARK 的檔數是否被改動過；沒有漂移則回傳 None。

    這是不必連 Shioaji 的廉價前置檢查，但**只看得見 ARK 這一側**的漂移。
    券商端的變動（賣出、除權息）只有真的去讀 Shioaji 才知道，
    所以它是提示，不是對帳的替代品。
    """
    if last is None:
        return "沒有同步紀錄，無法判斷 ARK 是否為最新"
    recorded = last.get("ark_count")
    if recorded is None or recorded == ark_count:
        return None
    return (f"ARK 現在 {ark_count} 檔，上次同步（{last.get('ts', '?')}）後是 {recorded} 檔"
            " —— 期間 ARK 被改動過")


# ---------------------------------------------------------------- 讀取

def wait_for(ax, pid, pred, what, timeout=10.0):
    """輪詢等待畫面達到預期狀態。

    不要用固定 sleep 假設操作完成——App 從別頁返回時載入時間不定，
    固定等待會造成「手動測試會過、自動連續執行卻失敗」的間歇性錯誤。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        w = ax.window(pid)
        if pred(w):
            return w
        time.sleep(0.4)
    raise RuntimeError(f"等待逾時：{what}")


def press_verified(ax, pid, el, pred, what, timeout=6.0):
    """AXPress 後驗證 pred 成立才算數；沒生效改座標點擊再驗一次。

    AXPress 對被遮擋元素、或事件層死掉的 App 會**回成功但實際無效**——
    按了不驗證，失敗只會在更遠處的 wait_for 逾時，錯誤現場早就不見了。
    回傳驗證後的 window；兩種方式都無效則拋 RuntimeError。
    """
    ax.press(el)
    try:
        return wait_for(ax, pid, pred, what, timeout=timeout)
    except RuntimeError:
        p, s = ax.point(el), ax.size(el)
        ax.click(p[0] + s[0] / 2, p[1] + s[1] / 2)
        return wait_for(ax, pid, pred, what, timeout=timeout)


def ensure_responsive(ax, pid, probe_timeout=6.0):
    """驗證 UI 事件層活著；死了（殭屍態）就重啟 App 一次，回傳可用 pid。

    殭屍態＝AX 讀得到、更新倒數在走，但所有輸入事件無效且 AXPress 仍回
    成功（2026-08-11 實測）。唯讀檢查驗不出來，只能實際按一顆會改變頁面
    地標的底部 tab。探測完 App 停在自選或運算頁，都是後續導航的合法起點。
    """
    def probe(pid):
        w = ax.window(pid)
        if not ax.by_desc(w, "自選"):            # 停在子頁：先退回 tab 根頁
            back = ax.by_desc(w, "back")
            if not back:
                return False
            ax.press(back[0])
            time.sleep(1.0)
            w = ax.window(pid)
            if not ax.by_desc(w, "自選"):
                return False
        at_watchlist = bool(ax.by_desc(w, "調節 庫存") or ax.by_desc(w, "布局 自選"))
        target, landmark = (("運算", "風控 運算") if at_watchlist
                            else ("自選", "調節 庫存"))
        tab = ax.by_desc(w, target)
        if not tab:
            return False
        ax.press(tab[0])
        try:
            wait_for(ax, pid, lambda w: bool(ax.by_desc(w, landmark)),
                     f"切到{target}", timeout=probe_timeout)
            return True
        except RuntimeError:
            return False

    if probe(pid):
        return pid
    pid = ax.restart_app()
    if not probe(pid):
        raise RuntimeError("ARK UI 無回應，重啟後仍無效")
    return pid


def visible_codes(ax, pid):
    return tuple(sorted(h.code for h in
                        (parse_holding(d) for _e, d in ax.descs(ax.window(pid), "AXStaticText"))
                        if h))


def _scroll_until_stable(step, probe, collect, max_rounds):
    previous = None
    for _ in range(max_rounds):
        if collect:
            collect()
        current = probe()
        if current == previous:
            return
        previous = current
        step()
    raise RuntimeError("捲動未收斂，可能畫面持續變動")


def scroll_until_stable(ax, cx, cy, dy, probe, collect=None, max_rounds=40):
    """朝同一方向捲到內容不再變化為止；每輪先呼叫 collect() 收集當前畫面。

    以「不再變化」為終止條件而非固定次數，因此不受持股檔數限制。
    滾輪需要游標位置——只剩布局自選頁在用（該頁沒有可捲動的 AX 元素）；
    調節庫存頁改用 scroll_page_until_stable，不動游標。
    """
    _scroll_until_stable(lambda: ax.scroll(cx, cy, dy, 3), probe, collect, max_rounds)


def scroll_page_until_stable(ax, pid, action, probe, collect=None, max_rounds=40):
    """scroll_until_stable 的 AX action 版：不動游標、ARK 失焦也有效。"""
    _scroll_until_stable(lambda: ax.scroll_page(pid, action), probe, collect, max_rounds)


def ensure_adjust_mode(ax, pid):
    """確保自選頁停在「調節庫存」而非「布局自選」。

    兩種模式共用同一顆 `watchlist edit` 按鈕，而且 App 會記住上次選的是哪個——
    只確認「不在編輯頁」不夠。停在布局自選時按下去會開到「自選清單編輯」，
    等不到「新增持股」而逾時。`全部庫存` 只存在於調節庫存模式，用它當判準。
    """
    w = ax.window(pid)
    if ax.by_desc(w, "全部庫存"):
        return w
    adjust = ax.by_desc(w, "調節 庫存")
    if not adjust:
        return w
    ax.press(adjust[0])
    return wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "全部庫存")), "切回調節庫存")


def goto_adjust_page(ax, pid):
    """確保停在（非編輯的）調節庫存頁，並切到台股分頁。

    「在自選頁」的判準用「調節 庫存／布局 自選」模式鈕，不能用 watchlist edit：
    運算頁的離職倒數模式右上也有一顆同名的筆。停在別的 tab 根頁（如運算頁）
    時沒有 back 鈕可按，要按底部「自選」tab 過去。
    """
    def at_watchlist(w):
        return bool(ax.by_desc(w, "調節 庫存") or ax.by_desc(w, "布局 自選"))

    w = ax.window(pid)
    if not at_watchlist(w):
        back = ax.by_desc(w, "back")
        if back:
            ax.press(back[0])
            w = wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "自選")),
                         "返回含底部分頁的頁面")
        tab = ax.by_desc(w, "自選")
        if tab:
            press_verified(ax, pid, tab[0], at_watchlist, "進入自選頁")
        else:
            wait_for(ax, pid, at_watchlist, "進入自選頁")
    w = ensure_adjust_mode(ax, pid)
    tw = ax.by_desc(w, "台股庫存")
    if tw:
        ax.press(tw[0])
        wait_for(ax, pid, lambda w: bool(visible_codes(ax, pid)), "台股庫存列表出現")
    return ax.window(pid)


def enter_edit_page(ax, pid):
    """進入編輯庫存頁；已在該頁則重進一次以重置捲動位置。

    重置很重要：列表被推移後第一列會躲到表頭底下，AXPress 會靜默失效。
    """
    w = ax.window(pid)
    if ax.by_desc(w, "back") and not ax.by_desc(w, "watchlist edit"):
        ax.press(ax.by_desc(w, "back")[0])
        wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "watchlist edit")), "返回調節庫存頁")
    w = ensure_adjust_mode(ax, pid)
    edit = ax.by_desc(w, "watchlist edit")
    if not edit:
        raise RuntimeError("找不到「watchlist edit」按鈕，可能不在自選頁，無法進入編輯庫存頁")
    ax.press(edit[0])
    return wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "新增持股")), "進入編輯庫存頁")


def read_holdings(ax, pid):
    """從調節庫存頁捲動讀出全部台股持股。這一頁可捲動，不受檔數限制。"""
    goto_adjust_page(ax, pid)
    found, unparsed = {}, []

    def collect():
        for _el, d in ax.descs(ax.window(pid), "AXStaticText"):
            holding = parse_holding(d)
            if holding:
                found[holding.code] = holding
            elif looks_like_holding_row(d):
                unparsed.append(d)

    def probe():
        return visible_codes(ax, pid)

    scroll_page_until_stable(ax, pid, "AXScrollUpByPage", probe)              # 先確實捲到頂
    scroll_page_until_stable(ax, pid, "AXScrollDownByPage", probe, collect)   # 再逐屏往下收集

    if unparsed:
        raise ParseFailed(
            f"有 {len(set(unparsed))} 列看起來是持股但解析失敗，欄位格式可能已變動。\n"
            f"樣本：{sorted(set(unparsed))[0]!r}"
        )
    return found


def goto_layout_page(ax, pid):
    """切到「自選 › 布局自選」。底部 tab 只在非編輯頁存在，故先確保已離開編輯頁。"""
    w = ax.window(pid)
    if not ax.by_desc(w, "自選"):
        back = ax.by_desc(w, "back")
        if not back:
            return None
        ax.press(back[0])
        w = wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "自選")), "返回含底部分頁的頁面")
    tab = ax.by_desc(w, "自選")
    if not tab:
        return None
    ax.press(tab[0])
    w = wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "布局 自選")), "進入自選頁")
    ax.press(ax.by_desc(w, "布局 自選")[0])
    return wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "位階股數")), "進入布局自選頁")


def press_scrolled(ax, el):
    """先把元素捲進畫面再按。

    離屏元素 `AXPress` 回傳 0 但靜默失效（ark-sync 陷阱 3）。
    `AXScrollToVisible` 不是輸入事件，橫向捲軸也吃得到——
    自選清單分頁一多就會捲出畫面，這是唯一按得到的方法。
    """
    ax.perform(el, "AXScrollToVisible")
    time.sleep(0.4)
    return ax.press(el)


def watchlist_tabs(ax, w):
    """布局自選頁上方的自選清單分頁，回傳 [(名稱, 是否選中, 元素)]，由左至右。

    上下界取自「布局 自選」與「股票名稱」兩列的 y —— 自我定位，
    寫死座標會在版面調整後靜默失準。選中狀態讀 `AXSelected`。
    """
    mode = ax.by_desc(w, "布局 自選")
    header = ax.by_desc(w, "股票名稱")
    if not mode or not header:
        return []
    mode_point, header_point = ax.point(mode[0]), ax.point(header[0])
    if mode_point is None or header_point is None:
        return []
    top, bottom = mode_point[1], header_point[1]
    found = []
    for el in ax.find(w, lambda e: ax.attr(e, "AXRole") == "AXButton"):
        name = (ax.attr(el, "AXDescription") or "").strip()
        point = ax.point(el)
        if name and point and top < point[1] < bottom:
            found.append((point[0], name, bool(ax.attr(el, "AXSelected")), el))
    return [(name, sel, el) for _x, name, sel, el in sorted(found, key=lambda t: t[0])]


NAME_COLUMN_TOL = 2   # 名稱列與視窗左緣的容許誤差（點）


def layout_elements(ax, w, tab_top, left=0):
    """回傳 (名稱列, 數值儲存格)。

    名稱列是靠齊**視窗左緣**的 AXStaticText，數值是各自獨立的 AXButton。
    `left` 必須是視窗的 x —— 早期版本寫死比對絕對座標 0，只有在 ARK 視窗
    剛好貼齊螢幕左邊時才成立；視窗一被移動就一列都認不出來，而且不報錯，
    整頁靜默讀成 0 檔，系統會誤判「沒有買進候選」而永遠不買。

    `tab_top` 之下是底部 tab —— 不濾掉的話「策略／自選／運算…」會被當成
    最後一列的儲存格，整排欄位錯位。
    """
    names, cells = [], []
    for el in ax.find(w, lambda e: True):
        text = (ax.attr(el, "AXDescription") or "").strip()
        point = ax.point(el)
        if not text or point is None or point[1] >= tab_top:
            continue
        role = ax.attr(el, "AXRole")
        if role == "AXStaticText" and abs(point[0] - left) <= NAME_COLUMN_TOL:
            names.append((point[1], text))
        elif role == "AXButton":
            cells.append((point[0], point[1], text))
    return names, cells


def read_layout(ax, pid, watchlist=None):
    """從布局自選頁捲動讀出各標的的位階／風控建議股數。

    `watchlist` 指定要讀哪一份自選清單，省略則讀目前選中的那份。
    回傳 LayoutView，讀的是哪一份會跟著回去。
    """
    w = goto_layout_page(ax, pid)
    if w is None:
        return LayoutView("", {})
    tabs = watchlist_tabs(ax, w)
    if watchlist:
        target = next((el for name, _sel, el in tabs if name == watchlist), None)
        if target is None:
            raise RuntimeError(f"找不到自選清單「{watchlist}」，"
                               f"現有：{[n for n, _s, _e in tabs]}")
        press_scrolled(ax, target)
        w = wait_for(ax, pid,
                     lambda w: any(n == watchlist and s for n, s, _e in watchlist_tabs(ax, w)),
                     f"切到自選清單「{watchlist}」")
        tabs = watchlist_tabs(ax, w)
    current = next((name for name, sel, _el in tabs if sel), watchlist or "")

    pos, size = ax.point(w), ax.size(w)
    if pos is None or size is None:
        raise RuntimeError("讀不到 ARK 視窗位置")
    cx, cy = pos[0] + 187, pos[1] + 444
    tab_top = pos[1] + size[1] - 60

    found, rejected = {}, []

    def collect():
        names, cells = layout_elements(ax, ax.window(pid), tab_top, pos[0])
        for row_top, desc in names:
            upper, lower = split_layout_cells(cells, row_top)
            row = parse_layout(desc, upper, lower)
            if row is None:
                continue
            if layout_is_consistent(row):
                found[row.code] = row
            else:
                rejected.append(desc)

    def probe():
        names, _cells = layout_elements(ax, ax.window(pid), tab_top, pos[0])
        return tuple(sorted(d for _y, d in names))

    scroll_until_stable(ax, cx, cy, 300, probe)             # 先確實捲到頂
    scroll_until_stable(ax, cx, cy, -120, probe, collect)   # 再逐屏往下收集
    ensure_adjust_mode(ax, pid)     # 模式選擇會被記住，不還原會害下一個讀取者走錯頁

    if rejected:
        raise ParseFailed(
            f"有 {len(set(rejected))} 列的風控股數對不上「風控布局金額 ÷ 股價」，"
            f"欄位可能已錯位。樣本：{sorted(set(rejected))[0]!r}"
        )
    return LayoutView(current, found)


def read_declared_count(ax, pid):
    """從編輯庫存頁讀 App 自己宣告的「總共 N 檔」，當作解析結果的外部見證。"""
    enter_edit_page(ax, pid)
    for _e, d in ax.descs(ax.window(pid), "AXStaticText"):
        m = re.match(r"總共\s*(\d+)\s*檔", d)
        if m:
            return int(m.group(1))
    return None


def clear_field(ax, field):
    """按欄位隔壁的 ⊗（calculator textfield cancel）清空。

    退格鍵事件（\\x08）時靈時不靈，⊗ 是 AXPress、可靠得多；
    但 iOS 慣例是欄位空著就不顯示 ⊗，找不到不算失敗。
    """
    sibs = ax.attr(ax.attr(field, "AXParent"), "AXChildren") or []
    seen = False
    for k in sibs:
        if not seen:
            seen = k == field
            continue
        if ax.attr(k, "AXDescription") == "calculator textfield cancel":
            ax.press(k)
            return
        if ax.attr(k, "AXRole") == "AXTextField":
            return                   # 已到下一個欄位，這欄沒有 ⊗


def fill_field(ax, pid, field_index, value):
    """填入欄位值並讀回驗證，最多重試一次。回傳是否確認寫入。

    失焦時 AXPress 建立 first responder 偶有不成功（新增表單觀察過），
    沒進去就重按重打。呼叫端仍應做語意驗證（總成本、讀回值）再儲存。
    """
    def current():
        return str(ax.attr(ax.text_fields(ax.window(pid))[field_index], "AXValue") or "")

    for _ in range(2):
        field = ax.text_fields(ax.window(pid))[field_index]
        ax.press(field)
        time.sleep(0.4)
        clear_field(ax, field)
        time.sleep(0.3)
        left = str(ax.attr(field, "AXValue") or "")
        if left:
            ax.backspace(pid, len(left) + 3)     # ⊗ 沒清乾淨才動用退格
            time.sleep(0.3)
        # 清不掉就**絕不打字**：打字是插入而非取代，會在游標處塞進去，留下比
        # 原值更錯的數字。實例：運算頁現金 30,000 被寫成 30,0300000，ARK 隨即
        # 依三億現金給出相反的建議。回報失敗遠比留下錯值安全。
        if current():
            continue
        ax.keystroke(pid, value)
        time.sleep(0.6)
        if current().replace(",", "") == value:
            return True
    return False


# 運算頁欄位序：[0] 台股庫存 [1] 美股庫存 [2] 台幣現金 [3] 美元
POSTURE_CASH_FIELD = 2

# 運算頁的數字鍵盤。**這些鍵不在 AX tree 裡**（與五指標儀表板同樣是純圖形），
# 只能算座標點擊，因此每一鍵按完都必須讀回驗證——點歪了要當場停手，不能繼續。
KEYPAD_KEYS = (("7", "8", "9", "⌫"),
               ("4", "5", "6", "-"),
               ("1", "2", "3", "+"),
               ("AC", "0", ".", "確定"))
# 以下比例量自實機（視窗 288×545）：鍵盤是貼齊視窗底部、佔滿寬度的 4×4 格
KEYPAD_ROW_H = 44.3          # 列高（點）
KEYPAD_BOTTOM_MARGIN = 24.8  # 最後一列中心距視窗底部（點）


def keypad_point(win_pos, win_size, key):
    """數字鍵在螢幕上的中心座標。

    鍵盤貼齊視窗底部而非固定絕對座標——視窗被移動或改變高度時仍要算得對。
    """
    for r, row in enumerate(KEYPAD_KEYS):
        if key in row:
            c = row.index(key)
            x = win_pos[0] + (c + 0.5) * win_size[0] / len(row)
            bottom = win_pos[1] + win_size[1]
            y = bottom - KEYPAD_BOTTOM_MARGIN - (len(KEYPAD_KEYS) - 1 - r) * KEYPAD_ROW_H
            return x, y
    raise KeyError(f"數字鍵盤沒有這個鍵：{key!r}")


def write_posture_cash(ax, pid, value):
    """把運算頁的台幣現金欄改成 value，回傳是否確認寫入。

    ARK 用這個數字算建議持股比例與參考調節金額，官方教學明講「現金輸錯，
    整個方向都反」（有股東漏算現金，結果從『參考調節』翻成『建議布局』）。

    運算頁的欄位**不吃系統鍵盤**：按下去會彈出 App 自製的數字鍵盤，而那些鍵
    不在 AX tree 裡，⊗ 的 AXPress 也是假成功。因此 keystroke／backspace／⊗
    三條路都進不去，只能座標點擊（**會移動游標**）。這與編輯庫存彈窗不同，
    那裡是真的文字欄位。

    **鍵盤緩衝讀不到**：按鍵期間 `AXValue` 一律維持舊值，要按下「確定」才更新
    （這點花了幾輪才確認——逐鍵比對 AXValue 會永遠判定失敗）。因此驗證只能
    在提交後做：寫入 → 驗證 → 不符就還原原值 → 回報失敗。「確定」是原子提交，
    錯值的暴露窗口只有一次往返。

    呼叫端應把回傳 False 當失效保護條件（不交易），而不是重試。
    """
    w = goto_posture_page(ax, pid)
    if w is None:
        return False
    target = str(int(value))
    original = str(ax.attr(ax.text_fields(w)[POSTURE_CASH_FIELD], "AXValue")
                   or "").replace(",", "")

    def current():
        got = ax.attr(ax.text_fields(ax.window(pid))[POSTURE_CASH_FIELD], "AXValue")
        return str(got or "").replace(",", "")

    def commit(digits):
        """開鍵盤 → AC → 逐鍵 → 確定，回傳提交後的值。

        每次點擊前重讀視窗幾何：使用者中途搬動視窗的話，用舊座標會點到
        別的東西上。幾何一變就中止。
        """
        ax.press(ax.text_fields(ax.window(pid))[POSTURE_CASH_FIELD])
        time.sleep(1.0)
        win = ax.window(pid)
        pos, size = ax.point(win), ax.size(win)
        for key in ("AC", *digits, "確定"):
            now = ax.window(pid)
            if ax.point(now) != pos or ax.size(now) != size:
                return None               # 視窗被搬動，座標已失效
            x, y = keypad_point(pos, size, key)
            ax.click(x, y)
            time.sleep(0.35)
        time.sleep(0.8)
        return current()

    if commit(target) == target:
        return True
    if original and current() != original:
        commit(original)                  # 盡力還原；不論成敗都回報失敗
    return False


def goto_posture_page(ax, pid):
    """導到運算 › 風控運算，回傳該頁 window；找不到「運算」tab 回 None。

    底部 tab 只在非編輯頁存在，因此先確保已離開編輯庫存頁
    （read_declared_count 會把 App 留在那裡）。
    """
    w = ax.window(pid)
    if not ax.by_desc(w, "運算"):
        back = ax.by_desc(w, "back")
        if back:
            ax.press(back[0])
            w = wait_for(ax, pid, lambda w: bool(ax.by_desc(w, "運算")), "返回含底部分頁的頁面")
    calc = ax.by_desc(w, "運算")
    if not calc:
        return None
    w = press_verified(ax, pid, calc[0],
                       lambda w: bool(ax.by_desc(w, "風控 運算")), "進入運算頁")

    # 運算頁有三個模式（方舟啟航／風控運算／離職倒數），選擇會被記住，
    # 而「風控 運算」是三個模式都有的切換鈕——等到它出現不代表已經在那一頁。
    # 不明確切過去的話會讀到目標管理頁，然後靜默回傳 None。
    if not ax.by_desc(w, "持股配置建議"):
        w = press_verified(ax, pid, ax.by_desc(w, "風控 運算")[0],
                           lambda w: bool(ax.by_desc(w, "持股配置建議")), "切到風控運算")
    return w


def read_posture(ax, pid):
    """讀運算頁的整體部位建議。讀完會切回自選頁。"""
    w = goto_posture_page(ax, pid)
    if w is None:
        return None

    texts = [d for _e, d in ax.descs(w, "AXStaticText")]
    fields = [ax.attr(f, "AXValue") for f in ax.text_fields(w)]
    ratios = [num(t) for t in texts if re.fullmatch(r"\d+(\.\d+)?%", t or "")]
    adjust = next((num(re.sub(r"[^\d.]", "", t)) for t in texts if "參考調節" in (t or "")), 0.0)
    amounts = [num(t) for t in texts if re.fullmatch(r"[\d,]+", t or "") and num(t) > 1000]

    back = ax.by_desc(ax.window(pid), "自選")
    if back:
        ax.press(back[0])

    if not ratios or len(amounts) < 2:
        return None
    stock = num(fields[0]) if fields else (max(amounts) if amounts else 0.0)
    cash = num(fields[2]) if len(fields) > 2 else 0.0
    suggested_ratio = min(ratios)
    total = stock + cash                       # 建議持股 = 總資金 × 建議比例
    suggested_value = total * suggested_ratio / 100.0
    return Posture(
        suggested_ratio=suggested_ratio,
        stock_value=stock,
        cash=cash,
        suggested_value=suggested_value,
        suggested_cash=total - suggested_value,
        adjust_amount=adjust,
    )


def month_total_from_texts(texts):
    """報酬紀錄頁文字 → 「總計當月已實現報酬」金額；解析不到回 None。

    第一個 fullmatch「N 元」就是總計——日期列長「08月11日, 4,174 元」，
    不會整串匹配。
    """
    for t in texts:
        m = re.fullmatch(r"([\d,]+) 元", t or "")
        if m:
            return num(m.group(1))
    return None


def record_daily_return(ax, pid, amount, date=None):
    """把當日已實現獲利記進「運算 › 離職倒數」，回傳是否確認寫入。

    App 紀律：只記獲利日（虧損由本金減少反映），amount ≤ 0 直接回 False。
    當日已有紀錄則點開該列改值（重跑冪等）。欄位 AXValue 在儲存前讀不到
    （ark-app-map 陷阱 17），驗證走「儲存後外層總計＝舊總計−舊值＋新值」。
    結束一律切回風控運算再停自選頁——停在離職倒數會讓隔日 read_posture
    靜默失敗。
    """
    amount = int(round(amount))
    if amount <= 0:
        return False
    d = date or _dt.date.today().isoformat()
    day_label = f"{int(d[5:7]):02d}月{int(d[8:10]):02d}日"

    try:
        w = goto_posture_page(ax, pid)
        if w is None:
            return False
        w = press_verified(ax, pid, ax.by_desc(w, "離職 倒數")[0],
                           lambda w: bool(ax.by_desc(w, "目標離職金額")),
                           "切到離職倒數")
        pen = ax.by_desc(w, "watchlist edit")
        if not pen:
            return False
        w = press_verified(ax, pid, pen[0],
                           lambda w: bool(ax.by_desc(w, "記錄今日報酬")),
                           "進入報酬紀錄頁")

        texts = [dsc for _e, dsc in ax.descs(w, "AXStaticText")]
        before_total = month_total_from_texts(texts)
        if before_total is None:
            return False
        today_row = next((t for t in texts if t.startswith(day_label)), None)
        old = 0.0
        if today_row:
            m = re.search(r"([\d,]+) 元", today_row)
            old = num(m.group(1)) if m else 0.0
            opener = ax.by_desc(w, today_row)[0]       # 已有紀錄：點列改值
        else:
            opener = ax.by_desc(w, "記錄今日報酬")[0]   # 沒有：新增
        w = press_verified(ax, pid, opener,
                           lambda w: bool(ax.by_desc(w, "儲存")), "開報酬彈窗")

        fields = ax.text_fields(w)
        if not fields:
            return False
        ax.press(fields[0])
        time.sleep(1.0)                    # 等游標與「完成」浮鈕
        ax.backspace(pid, 12)              # 清舊值；⊗ 會掉焦點，不用
        time.sleep(0.4)
        ax.keystroke(pid, str(amount))
        time.sleep(0.6)
        ax.keystroke(pid, "\r")            # 收鍵盤——開著會遮住儲存鈕
        time.sleep(0.8)
        save = ax.by_desc(ax.window(pid), "儲存")
        if not save:
            return False
        ax.press(save[0])

        expect = before_total - old + amount
        try:
            wait_for(ax, pid,
                     lambda w: month_total_from_texts(
                         [dsc for _e, dsc in ax.descs(w, "AXStaticText")]) == expect,
                     "報酬寫入後總計更新")
        except RuntimeError:
            return False
        return True
    finally:
        # 成敗都要把 App 停回安全狀態：報酬紀錄 → 離職倒數 → 風控運算 → 自選
        close = ax.by_desc(ax.window(pid), "popup close")
        if close:
            ax.press(close[0])
            time.sleep(0.6)
        back = ax.by_desc(ax.window(pid), "back")
        if back:
            ax.press(back[0])
            time.sleep(0.8)
        if goto_posture_page(ax, pid) is not None:
            tab = ax.by_desc(ax.window(pid), "自選")
            if tab:
                ax.press(tab[0])


