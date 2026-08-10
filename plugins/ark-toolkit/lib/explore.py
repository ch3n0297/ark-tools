"""方舟運算(ARK) 的全 App 探索層：走訪各頁、記錄結構，產出功能地圖的原料。

與 ark.py 同一個約定：頂層不匯入 pyobjc，AX 操作一律以 `ax` 模組當參數傳入，
因此純邏輯可在任何平台測試。

探索的原則是「按按鈕是為了到達新頁面，不是為了觸發動作」——
按下後畫面沒換頁的元素會被標記為動作型並不再重按。
"""
import hashlib
import json
import os
import re
import time
from typing import NamedTuple

MAP_DIR = os.path.expanduser("~/.ark-toolkit/app-map")

# 唯一一組硬黑名單。金流是不可逆的，登出則會踢掉我們沒有密碼可以復原的登入狀態。
BLOCKED_KEYWORDS = ("訂閱", "購買", "付款", "升級", "續約", "退款", "退訂", "登出")

NAVIGATION, ACTION, BLOCKED = "navigation", "action", "blocked"
UNVISITED, DATA, OFFSCREEN = "unvisited", "data", "offscreen"
SKIP, EXPLORE = "skip", "explore"
TODO_MARK = "TODO(請補)"

MAX_DEPTH = 3
MAX_PAGES = 80

VERSION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)?)\b")
VERSION_LABEL = re.compile(r"版本|版號|Version", re.IGNORECASE)

# 資料格的樣子：純數字、漲跌幅，或「名稱, 代號, 區域, 年數」這種列
DATA_ONLY = re.compile(r"^[\d.,%+()\s▲▼↑↓－＋-]+$")
STOCK_ROW = re.compile(r",\s*\d{4,6}[A-Z]?\s*,")
CONTROL_ID = re.compile(r"^[a-z0-9 ]+$")

# modal 的逃生門，按名字精確比對
DISMISS_NAMES = ("popup close", "close", "關閉", "取消", "back")

# 提交鈕：按下去只會把動作定案，不會產生新的地圖節點
COMMIT_NAMES = ("確認", "確定", "儲存", "送出", "全選")


class Element(NamedTuple):
    name: str
    role: str
    kind: str       # navigation / action / blocked / unvisited


class Page(NamedTuple):
    path: tuple
    fingerprint: str
    texts: tuple
    elements: tuple


# ---------------------------------------------------------------- 純邏輯

def is_blocked(name):
    return any(k in (name or "") for k in BLOCKED_KEYWORDS)


def fingerprint(texts):
    """畫面指紋。

    刻意剝掉數字：股價每秒在跳，含數字的指紋會讓「同一頁」被誤判成「新頁」，
    進而把動作型元素誤分類成導航。但剝光後全數字的畫面會塌成同一個指紋，
    所以補上節點數當第二個維度。
    """
    masked = sorted({re.sub(r"[\d.,+%\s-]+", "", t or "").strip() for t in texts})
    payload = "\n".join(m for m in masked if m) + f"\n#{len(texts)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def classify(before_fp, after_fp):
    return NAVIGATION if before_fp != after_fp else ACTION


def find_version(texts):
    """從畫面文字找版本號。

    必須有「版本」字樣當見證：畫面上滿是 1,234.56 這類股價，
    只看數字格式一定誤判。標籤與數字可能是相鄰的兩個節點，故也看下一則。
    """
    for i, text in enumerate(texts):
        if not VERSION_LABEL.search(text or ""):
            continue
        following = texts[i + 1] if i + 1 < len(texts) else ""
        for candidate in (text, following):
            m = VERSION_RE.search(candidate or "")
            if m:
                return m.group(1)
    return None


def is_control_id(name):
    """ARK 用小寫英文 AX id 標示控制項（add stock off、watchlist edit、
    navigation search icon、pwd eye icon…），內容區段則一律是中文。

    控制項多半是編輯或搜尋的入口，會開出返回鍵定位不到的 modal，
    而且是資料變更面——功能地圖不需要進去。
    """
    return bool(CONTROL_ID.match((name or "").strip()))


def looks_navigational(name, max_len=25):
    """值得按下去看看的名字。

    自選頁一頁就有 66 個可按元素，其中 50 個是股價與資料列——按下去全都通往
    同一種「個股詳情」頁，全展開會讓探索次數爆炸。過長的名字則是廣告橫幅，
    按下去多半離開 App，記為未走訪比誤按安全。
    """
    text = (name or "").strip()
    if not text or len(text) > max_len:
        return False
    if DATA_ONLY.match(text) or STOCK_ROW.search(text) or is_control_id(text):
        return False
    if text in COMMIT_NAMES:
        # 「確認」會重啟 App（清除快取）、「儲存」會改掉離職金額目標——
        # 而它們都不會帶你到新頁面，按了純虧
        return False
    digits = sum(c.isdigit() for c in text)
    return digits / len(text) < 0.4      # 「成本 164,938」「A12069」是資料不是標籤


def is_onscreen(point, bounds):
    """元素是否落在視窗內。

    ETF 橫向捲軸的元素 x 到 1247，而視窗只有 375 寬。按離屏元素會回傳 0
    但靜默失效（ark-sync 陷阱 3），畫面不變就會被誤判成動作型。
    量不到就當可見——交給按下去的結果說話，不要憑空排除。
    """
    if not point or not bounds:
        return True
    x, y = point
    bx, by, bw, bh = bounds
    return bx <= x <= bx + bw and by <= y <= by + bh


def pick_dismiss(names):
    """從畫面上的元素名找關閉鍵。

    modal 的關閉鍵在右上角，pick_back 的左上角啟發式抓不到。
    只認這份明確清單——不能用「含 close 字樣」之類的模糊比對，
    `checkbox uncheck` 那種按下去會變更資料的元素絕不能被當成逃生門。
    """
    lowered = {n.strip().lower(): n for n in names if n}
    return next((lowered[c] for c in DISMISS_NAMES if c in lowered), None)


def pick_back(placed, bounds, max_dx=60, dy_range=(40, 90)):
    """從 [(point, name)] 找左上角的返回鍵，回傳名字。

    ARK 各頁返回鍵的標籤不一致（自選頁叫 back，大盤詳情頁叫「方舟運算」——
    就是左上角那個 App logo）。位置比名字可靠：導覽列高度固定，
    返回鍵永遠在最左，而置中的是標題。
    """
    if not bounds:
        return None
    bx, by = bounds[0], bounds[1]
    best = None
    for point, name in placed:
        if not point:
            continue
        dx, dy = point[0] - bx, point[1] - by
        if dx <= max_dx and dy_range[0] <= dy <= dy_range[1]:
            if best is None or (dy, dx) < best[0]:
                best = ((dy, dx), name)
    return best[1] if best else None


def tab_candidates(placed, bounds, band=100):
    """從 [(x, y, name, role)] 篩出可能是底部 tab 的元素。

    三個條件缺一不可：AXButton、貼齊視窗底部、（交給 tab_row 判的）同列至少 3 個。
    只用最後一個會誤判——大盤詳情頁的數據列正好同列且 4 個。
    """
    if not bounds:
        return []
    floor = bounds[1] + bounds[3] - band
    return [(x, y, name) for x, y, name, role in placed
            if role == "AXButton" and y >= floor and is_onscreen((x, y), bounds)]


def tab_row(placed, min_members=3):
    """從 [(x, y, name)] 找出底部 tab 那一列。

    不能用「視窗底部 N 像素內」——列表最後一列的股價也落在那個範圍。
    tab 的特徵是「同一個 y、至少 min_members 個、位在最下方」。
    """
    rows = {}
    for x, y, name in placed:
        rows.setdefault(y, []).append((x, name))
    rows = {y: members for y, members in rows.items() if len(members) >= min_members}
    if not rows:
        return []
    return [name for _x, name in sorted(rows[max(rows)])]


def next_paths(path, names, known_actions, max_depth=MAX_DEPTH):
    """這一頁值得往下走的路徑。

    排除：已在路徑上的名字（按了會原地打轉）、黑名單、已知的動作型元素。
    """
    if len(path) >= max_depth:
        return []
    out = []
    for name in names:
        child = tuple(path) + (name,)
        if (not name) or is_blocked(name) or name in path or child in known_actions:
            continue
        if child not in out:
            out.append(child)
    return out


def root_is_dead(path, misses, limit):
    """這條路徑所屬的根 tab 是否已經連續失敗到該放棄。

    App 會記住子分頁的選擇：走過「自選›美股庫存」（空的）之後，每次回到自選
    都落在那頁，兄弟節點就全都走不到。這時該放棄的是那一棵子樹，不是整趟探索。
    """
    return bool(path) and misses.get(path[0], 0) >= limit


def kind_of(path, name, visited_paths, action_paths, onscreen=True):
    if is_blocked(name):
        return BLOCKED
    child = tuple(path) + (name,)
    if child in action_paths:
        return ACTION
    if child in visited_paths:
        return NAVIGATION
    if not looks_navigational(name):
        return DATA
    if not onscreen:
        return OFFSCREEN
    return UNVISITED


# ---------------------------------------------------------------- 快取

def cache_path(version, directory=MAP_DIR):
    return os.path.join(directory, re.sub(r"[^\w.-]", "_", version or "unknown") + ".json")


def load_cache(version, directory=MAP_DIR):
    path = cache_path(version, directory)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_map(app_map, directory=MAP_DIR):
    os.makedirs(directory, exist_ok=True)
    path = cache_path(app_map.get("version"), directory)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(app_map, fh, ensure_ascii=False, indent=2)
    return path


def cache_decision(version, cached):
    """回傳 (SKIP|EXPLORE, 理由)。

    版本讀不到時一律探索——拿不可靠的鍵去命中快取，
    會讓「沒探索到」偽裝成「已經探索過」。
    """
    if version is None:
        return (EXPLORE, "讀不到版本號，無法判定快取有效性，本次完整探索")
    if cached is None:
        return (EXPLORE, f"版本 {version} 尚無快取")
    return (SKIP, f"版本 {version} 已於 {cached.get('explored_at')} 探索過（--force 可強制重跑）")


def build_map(pages, version, explored_at):
    return {
        "version": version,
        "explored_at": explored_at,
        "pages": [{
            "path": list(p.path),
            "fingerprint": p.fingerprint,
            "texts": list(p.texts),
            "elements": [e._asdict() for e in p.elements],
        } for p in pages],
    }


def element_report(pages):
    """全 App 可按元素分類。

    `blocked` 那份清單是用來驗證「本 App 除訂閱外不涉及真實交易」這個斷言的證據。
    """
    counts, blocked, unvisited = {}, [], []
    for page in pages:
        where = " › ".join(page.path) or "(根)"
        for el in page.elements:
            counts[el.kind] = counts.get(el.kind, 0) + 1
            if el.kind == BLOCKED:
                blocked.append({"page": where, "name": el.name})
            elif el.kind == UNVISITED:
                unvisited.append({"page": where, "name": el.name})
    return {"counts": counts, "blocked": blocked, "unvisited": unvisited}


# ---------------------------------------------------------------- Markdown 骨架

def to_markdown(app_map):
    """產出 Guide 骨架。

    「功能／作用／限制」留 TODO 而不編造——那些是 dump 推不出來的語意，
    只有實際用過 App 的人答得出來。編一個看似合理的答案比留白危險得多。
    """
    version = app_map.get("version") or "未知"
    lines = [
        "# ARK App 功能地圖",
        "",
        f"探索版本：**{version}**　探索時間：{app_map.get('explored_at')}",
        "",
        f"由 `ark-explore` 從實際 AX 探索產生。標記 `{TODO_MARK}` 的欄位需人工補上。",
        "",
        "**本檔只描述結構與功能意義，不記錄當日內容。**",
        "達人觀點今天是誰、活動中心有什麼活動每天都在變，寫進來就會過期——",
        "而 App 版本號不會因為內容更新而變，快取永遠不會失效，過期的內容會被當成事實。",
        "",
        "---",
        "",
    ]
    for page in app_map.get("pages", []):
        path = page.get("path") or []
        labels = [t for t in page.get("texts", []) if not VERSION_RE.fullmatch(t.strip())][:8]
        lines += [
            f"### {path[-1] if path else '(根)'}",
            f"- **路徑**：{' › '.join(path) or '(根)'}",
            f"- **功能**：{TODO_MARK}",
            f"- **讀得到**：{'、'.join(labels) if labels else '（無文字內容）'}",
            f"- **作用**：{TODO_MARK}",
            f"- **限制**：{TODO_MARK}",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------- AX 讀取

def screen_texts(ax, w):
    """畫面上所有文字。AXDescription 為主，空的退回 AXValue。"""
    out = []
    for el, desc in ax.descs(w, "AXStaticText"):
        out.append(desc or str(ax.attr(el, "AXValue") or ""))
    return tuple(t for t in out if t)


def named_elements(ax, w):
    """回傳 [(name, role, element, point)]，只取按得下去且有名字的。

    用 AXUIElementCopyActionNames 判斷可按性，不必試按——
    這是唯一不需要製造副作用就能知道元素能不能按的方法。
    """
    out = []
    for el in ax.find(w, lambda e: True):
        name = (ax.attr(el, "AXDescription") or ax.attr(el, "AXTitle") or "").strip()
        if name and "AXPress" in ax.actions(el):
            out.append((name, ax.attr(el, "AXRole") or "", el, ax.point(el)))
    return out


def window_bounds(ax, w):
    pos, size = ax.point(w), ax.size(w)
    return (pos[0], pos[1], size[0], size[1]) if pos and size else None


def navigable(ax, w):
    """這一頁值得按下去的元素名（已濾掉資料格與離屏元素），保持畫面順序。"""
    bounds = window_bounds(ax, w)
    out = []
    for name, _role, _el, point in named_elements(ax, w):
        if looks_navigational(name) and is_onscreen(point, bounds) and name not in out:
            out.append(name)
    return out


def find_named(ax, w, name):
    """找畫面上叫這個名字、且在視窗內的可按元素。"""
    bounds = window_bounds(ax, w)
    for n, _role, el, point in named_elements(ax, w):
        if n == name and is_onscreen(point, bounds):
            return el
    return None


def settle(ax, pid, timeout=6.0, quiet=0.9):
    """等畫面連續 quiet 秒不再變化。

    不要用固定 sleep 假設載入完成——App 各頁載入時間不定，
    固定等待會造成「手動測試會過、自動連續執行卻失敗」的間歇性錯誤
    （ark-sync SKILL.md 陷阱 5）。
    """
    deadline = time.time() + timeout
    last, since = None, time.time()
    while time.time() < deadline:
        fp = fingerprint(screen_texts(ax, ax.window(pid)))
        if fp != last:
            last, since = fp, time.time()
        elif time.time() - since >= quiet:
            return fp
        time.sleep(0.3)
    return last


def bottom_tabs(ax, w):
    """底部 tab，由左至右。"""
    bounds = window_bounds(ax, w)
    placed = [(point[0], point[1], name, role)
              for name, role, _el, point in named_elements(ax, w) if point]
    return tab_row(tab_candidates(placed, bounds))


def go_home(ax, pid, tabs=(), tries=8):
    """連按返回鍵直到底部 tab 出現。

    tabs 是已知的根頁名稱：某些 modal 的返回鍵定位不到，但畫面上仍找得到
    某個 tab，直接按它就能脫身。這是最後一道逃生門。
    """
    for _ in range(tries):
        w = ax.window(pid)
        if bottom_tabs(ax, w):
            return True
        elements = named_elements(ax, w)
        # 三道逃生門依序試：modal 關閉鍵 → 左上角返回鍵 → 畫面上任一個已知 tab
        name = (pick_dismiss([n for n, _r, _e, _p in elements])
                or pick_back([(point, n) for n, _r, _e, point in elements],
                             window_bounds(ax, w)))
        target = find_named(ax, w, name) if name else None
        if target is None:
            target = next((el for t in tabs if (el := find_named(ax, w, t))), None)
        if target is None:
            return False
        ax.press(target)
        settle(ax, pid)
    return False


def goto(ax, pid, path, tabs=()):
    """從根重播整條路徑。

    深度上限只有 3，重播比記錄返回路徑穩健得多——返回鍵的位置與行為
    各頁不一致，重播只依賴「從根出發」這一個假設。
    """
    if not go_home(ax, pid, tabs):
        return None
    for name in path:
        target = find_named(ax, ax.window(pid), name)
        if target is None:
            return None
        ax.press(target)
        settle(ax, pid)
    return ax.window(pid)


def read_version(ax, pid, settings_names=("設定", "我的", "更多", "個人")):
    """到設定類頁面找版本號。找不到回傳 None，不猜。"""
    if not go_home(ax, pid):
        return None
    for name in settings_names:
        target = find_named(ax, ax.window(pid), name)
        if target is None:
            continue
        ax.press(target)
        settle(ax, pid)
        found = find_version(screen_texts(ax, ax.window(pid)))
        if found:
            go_home(ax, pid)
            return found
        go_home(ax, pid)
    return None


def walk(ax, pid, max_depth=MAX_DEPTH, max_pages=MAX_PAGES, max_misses=8, log=print):
    """廣度優先走訪全 App。回傳 (pages, report_extras)。"""
    if not go_home(ax, pid):
        raise RuntimeError("回不到根頁（找不到底部 tab），無法開始探索")
    tabs = bottom_tabs(ax, ax.window(pid))
    log(f"底部 tab：{'、'.join(tabs)}")

    raw, visited_fp, visited_paths, action_paths = {}, {}, set(), set()
    frontier = [(t,) for t in tabs]
    misses, dead = {}, []

    aborted = False
    while frontier and len(raw) < max_pages and not aborted:
        nxt = []
        for path in frontier:
            if len(raw) >= max_pages:      # 外層 while 只在整輪之間檢查，這裡擋輪內超收
                break
            label = " › ".join(path)
            if root_is_dead(path, misses, max_misses):
                continue
            try:
                w = goto(ax, pid, path, tabs)
                if w is None:
                    misses[path[0]] = misses.get(path[0], 0) + 1
                    log(f"  ✗ 走不到 {label}")
                    if root_is_dead(path, misses, max_misses):
                        dead.append(path[0])
                        log(f"  ⚠️ 「{path[0]}」連續 {max_misses} 次走不到，"
                            "放棄這棵子樹（App 記住了某個子分頁選擇），繼續其他 tab")
                    continue
                misses[path[0]] = 0
                texts = screen_texts(ax, w)
                fp = fingerprint(texts)

                if visited_fp.get(path[:-1]) == fp:
                    action_paths.add(path)
                    log(f"  · {label} 沒換頁 → 動作型")
                    continue

                visited_fp[path] = fp
                visited_paths.add(path)
                if fp in {data["fp"] for data in raw.values()}:
                    log(f"  ↺ {label} 與既有頁面相同，不重複展開")
                    continue

                bounds = window_bounds(ax, w)
                raw[path] = {
                    "fp": fp,
                    "texts": texts,
                    "elements": [(name, role, is_onscreen(point, bounds))
                                 for name, role, _el, point in named_elements(ax, w)],
                }
                # tab 每頁都在，從非根頁再按一次只會繞回已走過的頁
                candidates = [n for n in navigable(ax, w) if n not in tabs]
                log(f"  ✓ {label}（{len(texts)} 段文字、{len(candidates)} 個待展開）")
                nxt += next_paths(path, candidates, action_paths, max_depth)
            except Exception as exc:
                # 有些元素會開外部連結，把 ARK 推到背景讓 iOS 暫停它，AX 全數失效。
                # 這種事不該讓整趟探索連同已收集的資料一起陪葬。
                log(f"  ✗ {label} 例外：{exc}")
                misses[path[0]] = misses.get(path[0], 0) + 1
                try:
                    pid = ax.activate()
                    log("    已把 App 拉回前景，繼續")
                except Exception:
                    log("    拉不回前景，中止探索，保留已收集的資料")
                    aborted = True
                    break
        frontier = nxt

    pages = [Page(path=path, fingerprint=data["fp"], texts=data["texts"],
                  elements=tuple(
                      Element(name, role,
                              kind_of(path, name, visited_paths, action_paths, onscreen))
                      for name, role, onscreen in data["elements"]))
             for path, data in raw.items()]
    try:
        go_home(ax, pid, tabs)  # 別把 App 丟在某個內層頁面
    except Exception:
        pass
    return pages, {"tabs": tabs, "actions": sorted(action_paths), "dead_roots": dead}


# ---------------------------------------------------------------- 診斷

def _short(value, limit=120):
    text = str(value) if value is not None else None
    return text[:limit] if text else None


def probe(ax, el, depth=0, max_depth=40):
    """遞迴 dump AX tree。純讀取，不按任何東西。"""
    node = {
        "role": ax.attr(el, "AXRole"),
        "subrole": ax.attr(el, "AXSubrole"),
        "desc": _short(ax.attr(el, "AXDescription")),
        "title": _short(ax.attr(el, "AXTitle")),
        "value": _short(ax.attr(el, "AXValue")),
        "pos": ax.point(el),
        "size": ax.size(el),
        "actions": ax.actions(el),
    }
    if depth < max_depth:
        kids = ax.attr(el, "AXChildren") or []
        if kids:
            node["children"] = [probe(ax, k, depth + 1, max_depth) for k in kids]
    return node
