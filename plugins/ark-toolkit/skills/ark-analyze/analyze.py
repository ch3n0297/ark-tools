# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "pyobjc-framework-cocoa",
#     "pyobjc-framework-applicationservices",
#     "pyobjc-framework-quartz",
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""分析 ARK 庫存：集中度、部位偏離、與 App 建議的差距、風控警示、歷史快照。

只陳述事實與偏離量，不產出買賣建議 —— 決策是使用者的。
讀取與解析共用 lib/ark.py。
"""
import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark     # noqa: E402
import source  # noqa: E402

SNAPSHOT_DIR = os.path.expanduser("~/.ark-toolkit/snapshots")
SYNC_PY = os.path.abspath(os.path.join(HERE, "..", "ark-sync", "sync.py"))

DEFAULT_MAX_SINGLE_PCT = 30.0   # 單一持股佔總市值上限
DEFAULT_MAX_GAP_PCT = 10.0      # 實際持股比例與 App 建議的容忍差距（百分點）


class Weight(NamedTuple):
    code: str
    value: float
    pct: float


class Trim(NamedTuple):
    code: str
    held: int
    trim_qty: int      # App 建議至少調節（賣出）的股數，「≥N」的 N
    amount: float      # 建議調節金額（約 = trim_qty × 現價）


class Alert(NamedTuple):
    level: str         # high / medium
    kind: str          # concentration / position / tier / tier-conflict
    message: str


# ---------------------------------------------------------------- 分析

def concentration(holdings):
    """各檔市值佔比，由大到小。"""
    total = sum(h.value for h in holdings.values())
    if not total:
        return []
    rows = [Weight(h.code, h.value, 100.0 * h.value / total) for h in holdings.values()]
    return sorted(rows, key=lambda r: r.pct, reverse=True)


def trim_suggestions(holdings):
    """轉述 App 的「建議調節股數／金額」，只列 App 有給的檔。

    「調節」＝賣出側；「≥N」＝建議至少調節 N 股，可多可少。
    曾誤讀為持股目標下限而輸出「低於建議」——方向完全相反。
    照實轉述、不修剪：持有 30 標示 ≥40 也是合法資料。
    """
    rows = [Trim(h.code, h.qty, h.suggest_qty, h.suggest_amount or 0.0)
            for h in holdings.values() if h.suggest_qty is not None]
    return sorted(rows, key=lambda t: -t.amount)


def risk_alerts(holdings, posture, max_single_pct=DEFAULT_MAX_SINGLE_PCT,
                max_gap_pct=DEFAULT_MAX_GAP_PCT):
    """規則式檢查，只描述事實與偏離量，不給買賣指示。"""
    alerts = []

    for row in concentration(holdings):
        if row.pct > max_single_pct:
            alerts.append(Alert(
                "high" if row.pct > max_single_pct * 1.5 else "medium",
                "concentration",
                f"{row.code} 佔總市值 {row.pct:.1f}%，超過設定上限 {max_single_pct:.0f}%",
            ))

    if posture is not None:
        gap = posture.gap
        if abs(gap) > max_gap_pct:
            direction = "高於" if gap > 0 else "低於"
            alerts.append(Alert(
                "high" if abs(gap) > max_gap_pct * 2 else "medium",
                "position",
                f"持股比例 {posture.actual_ratio:.2f}% {direction} App 建議的 "
                f"{posture.suggested_ratio:.1f}%，差距 {abs(gap):.2f} 個百分點"
                f"（App 標示參考調節 {posture.adjust_amount:,.0f}）",
            ))

    alerts.extend(tier_alerts(holdings))
    return alerts


def shioaji_diff(holdings, positions):
    """ARK 與 Shioaji 對不上的代號。自動同步前後各算一次，故獨立成純函式。"""
    return sorted(c for c in set(holdings) | set(positions)
                  if c not in holdings or c not in positions
                  or holdings[c].qty != positions[c][0]
                  or abs(holdings[c].price - positions[c][1]) >= ark.PRICE_TOL)


def tier_alerts(holdings):
    """位階提示。

    價值＝App 用於買進側的標記，升溫＝用於調節側；兩者是獨立旗標，可同時成立。
    官方解讀：價值看長期、升溫看短期，同時掛上並非矛盾（見 ark-app-map.md）。
    這裡只轉述 App 標了什麼，不替使用者把標記翻譯成買賣動作。
    """
    hot = sorted(h.code for h in holdings.values() if "升溫" in h.tiers)
    dual = sorted(h.code for h in holdings.values() if len(h.tiers) > 1)
    alerts = []
    if hot:
        alerts.append(Alert("medium", "tier",
                            f"{len(hot)} 檔持股在升溫區（App 以此標記提示調節）："
                            f"{'、'.join(hot)}"))
    if dual:
        alerts.append(Alert("medium", "tier-conflict",
                            f"{len(dual)} 檔同時標記價值與升溫（官方解讀：長期具價值、"
                            f"短期過熱，兩側各自成立）：{'、'.join(dual)}"))
    return alerts


# ---------------------------------------------------------------- 快照

def build_snapshot(holdings, posture, taken_at):
    return {
        "taken_at": taken_at,
        "holdings": {h.code: {"qty": h.qty, "price": h.price, "value": h.value,
                              "pnl": h.pnl, "roi": h.roi, "tiers": list(h.tiers),
                              "suggest_qty": h.suggest_qty}
                     for h in holdings.values()},
        "posture": None if posture is None else {
            "suggested_ratio": posture.suggested_ratio,
            "actual_ratio": posture.actual_ratio,
            "stock_value": posture.stock_value,
            "cash": posture.cash,
            "adjust_amount": posture.adjust_amount,
        },
    }


def diff_snapshots(old, new):
    """比較兩份快照：新增、消失、股數或均價有變動的檔。"""
    a, b = old["holdings"], new["holdings"]
    changed = []
    for code in sorted(set(a) & set(b)):
        if (a[code]["qty"] != b[code]["qty"]
                or abs(a[code]["price"] - b[code]["price"]) >= ark.PRICE_TOL):
            changed.append({
                "code": code,
                "qty_from": a[code]["qty"], "qty_to": b[code]["qty"],
                "price_from": a[code]["price"], "price_to": b[code]["price"],
                "pnl_from": a[code].get("pnl"), "pnl_to": b[code].get("pnl"),
            })
    return {"added": sorted(set(b) - set(a)),
            "removed": sorted(set(a) - set(b)),
            "changed": changed}


def save_snapshot(snap, directory=SNAPSHOT_DIR):
    os.makedirs(directory, exist_ok=True)
    stamp = snap["taken_at"].replace(":", "").replace("-", "").replace("T", "-")
    path = os.path.join(directory, f"{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=2)
    return path


def load_snapshots(directory=SNAPSHOT_DIR):
    out = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            out.append((path, json.load(fh)))
    return out


# ---------------------------------------------------------------- 輸出

def render_layout(view):
    """布局自選頁的建議張數。

    與風控警示同一條規則：只陳述 App 給的數字，不轉成買賣建議。
    一定要標出是哪一份自選清單——換一份數字全變。
    """
    if not view or not view.rows:
        return "  讀不到布局自選（略過）"
    # 位階放最後一欄：中文是雙寬字元，用 :<n 補齊會對不齊
    rows = [f"  自選清單：{view.watchlist or '（不明）'}",
            "  代號          股價   位階股數   風控股數   位階"]
    for row in sorted(view.rows.values(), key=lambda r: -r.risk_qty):
        rows.append(f"  {row.code:<8}{row.price:>10.2f}{row.tier_qty:>9}"
                    f"{row.risk_qty:>11}   {'／'.join(row.tiers) or '-'}")
    return "\n".join(rows)


def render(holdings, posture, alerts, cross_check=None, layout=None):
    total = sum(h.value for h in holdings.values())
    cost = sum(h.cost for h in holdings.values())
    pnl = sum(h.pnl for h in holdings.values())
    out = []

    out.append(f"總市值 {total:,.0f}　成本 {cost:,.0f}　未實現損益 {pnl:+,.0f}"
               f"（{100 * pnl / cost:+.2f}%）" if cost else f"總市值 {total:,.0f}")
    if posture:
        out.append(f"持股比例 {posture.actual_ratio:.2f}%　App 建議 {posture.suggested_ratio:.1f}%"
                   f"　現金 {posture.cash:,.0f}")

    out.append("\n【集中度】")
    out.append("  代號        佔比        市值  分布與位階")
    for row in concentration(holdings):
        bar = "█" * max(1, int(row.pct / 2))
        # 位階擺最後：中文是雙寬字元，夾在中間會把長條圖的起點推歪
        tiers = "／".join(holdings[row.code].tiers) or "-"
        out.append(f"  {row.code:<8}{row.pct:>6.2f}%  {row.value:>10,.0f}  {bar} {tiers}")

    trims = trim_suggestions(holdings)
    if trims:
        out.append("\n【App 標示的調節股數】（調節＝賣出側；≥ 表示至少此數，取捨由使用者決定）")
        for t in trims:
            out.append(f"  {t.code:<8} 持有 {t.held:>5}　標示調節 ≥{t.trim_qty:<5}"
                       f"　約 {t.amount:>10,.0f} 元")
        line = f"  合計全依標示調節約 {sum(t.amount for t in trims):,.0f} 元"
        if posture:
            line += f"（運算頁參考調節金額 {posture.adjust_amount:,.0f} 元）"
        out.append(line)

    out.append("\n【風控檢查】")
    if not alerts:
        out.append("  ✅ 未觸發任何設定門檻")
    for a in alerts:
        icon = "🔴" if a.level == "high" else "🟡"
        out.append(f"  {icon} {a.message}")

    if layout is not None:
        out.append("\n【布局自選的建議張數】")
        out.append(render_layout(layout))

    if cross_check:
        out.append("\n【與真實持倉對帳】")
        out.append(f"  {cross_check}")
    out.append("\n以上為事實陳述與偏離計算，不構成投資建議。")
    return "\n".join(out)


def run_sync():
    """對帳不一致時直接把 ARK 修好，而不是叫使用者自己去跑另一個工具。

    以 subprocess 呼叫 ark-sync 而非 import：同步有安全閘、總成本驗算與紀錄，
    走同一支 CLI 才不會生出「analyze 專用的同步」這種第二套實作。
    輸出不攔截，直接讓使用者看到每一步。
    """
    if not os.path.exists(SYNC_PY):
        print(f"  ✗ 找不到 ark-sync（{SYNC_PY}），略過自動同步")
        return False
    done = subprocess.run([sys.executable, SYNC_PY, "--allow-delete"])
    print()
    return done.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="分析 ARK 庫存的集中度、部位偏離與風險")
    ap.add_argument("--snapshot", action="store_true", help="存下這次的快照供日後復盤")
    ap.add_argument("--compare", action="store_true", help="與最近一次快照比較")
    ap.add_argument("--max-single-pct", type=float, default=DEFAULT_MAX_SINGLE_PCT,
                    help=f"單一持股佔比上限，預設 {DEFAULT_MAX_SINGLE_PCT:.0f}")
    ap.add_argument("--max-gap-pct", type=float, default=DEFAULT_MAX_GAP_PCT,
                    help=f"持股比例與 App 建議的容忍差距，預設 {DEFAULT_MAX_GAP_PCT:.0f}")
    ap.add_argument("--no-cross-check", action="store_true",
                    help="略過與真實持倉來源的對帳檢查")
    ap.add_argument("--no-auto-sync", action="store_true",
                    help="對帳不一致時只回報，不自動執行 ark-sync 修正")
    ap.add_argument("--no-layout", action="store_true", help="略過布局自選頁的讀取（較快）")
    ap.add_argument("--watchlist", metavar="名稱",
                    help="指定要讀哪一份自選清單，省略則讀目前選中的那份")
    args = ap.parse_args()

    ark.check_platform(tool="ark-analyze")
    import ax

    pid = ax.activate()
    print("讀取 ARK 庫存…", flush=True)
    holdings = ark.read_holdings(ax, pid)
    declared = ark.read_declared_count(ax, pid)
    ark.check_parsed(holdings, declared)
    print(f"  {len(holdings)} 檔", flush=True)

    drift = ark.drift_since_last_sync(ark.last_sync(), declared or len(holdings))
    if drift:
        print(f"  ℹ️ {drift}", flush=True)

    print("讀取運算頁部位建議…", flush=True)
    posture = ark.read_posture(ax, pid)
    print("  " + (f"建議持股 {posture.suggested_ratio:.1f}%" if posture else "讀不到（略過）"),
          flush=True)

    layout = None
    if not args.no_layout:
        print("讀取布局自選…", flush=True)
        layout = ark.read_layout(ax, pid, args.watchlist)
        print(f"  「{layout.watchlist}」{len(layout.rows)} 檔", flush=True)

    cross = None
    if not args.no_cross_check:
        try:
            cfg = source.load_config()
        except source.SetupRequired:
            cfg = None
        if cfg is None:
            cross = "（未設定對帳來源，略過。執行 ark-setup 可設定）"
        elif source.is_pure_ark(cfg):
            cross = "（純 ARK 模式，略過對帳）"
        else:
            print(f"與真實持倉對帳（{source.describe(cfg)}）…", flush=True)
            positions = source.read_positions(cfg)
            diff = shioaji_diff(holdings, positions)

            if diff and not args.no_auto_sync:
                print(f"  ⚠️ {len(diff)} 檔不一致：{', '.join(diff)}　→ 自動同步中…\n",
                      flush=True)
                attempted = run_sync()
                # 不論同步回報成功與否都要重讀：只要動過寫入，手上的 holdings 就過期了。
                # 曾把「部分成功」（sync 有一項失敗即回傳非零）當成完全失敗而不重讀，
                # 結果整份分析建立在同步前的舊資料上，且外表完全正常。
                holdings = ark.read_holdings(ax, pid)
                declared = ark.read_declared_count(ax, pid)
                ark.check_parsed(holdings, declared)
                posture = ark.read_posture(ax, pid)   # 部位建議站在庫存上，也一起重讀
                positions = source.read_positions(cfg)
                diff = shioaji_diff(holdings, positions)
                if not diff:
                    cross = f"✅ {len(holdings)} 檔與真實持倉一致（本次已自動同步）"
                else:
                    why = "自動同步後仍有" if attempted else "自動同步未執行完成，仍有"
                    cross = (f"⚠️ {why} {len(diff)} 檔不一致：{', '.join(diff)}"
                             "　→ 以下分析建立在與真實持倉不一致的資料上")
            else:
                cross = (f"✅ {len(holdings)} 檔與真實持倉一致" if not diff
                         else f"⚠️ {len(diff)} 檔不一致：{', '.join(diff)}"
                              "　→ 請執行 ark-sync，否則以下分析建立在過期資料上")

    alerts = risk_alerts(holdings, posture, args.max_single_pct, args.max_gap_pct)
    print()
    print(render(holdings, posture, alerts, cross, layout))

    now = dt.datetime.now().replace(microsecond=0).isoformat()
    snap = build_snapshot(holdings, posture, now)

    if args.compare:
        history = load_snapshots()
        if history:
            _path, previous = history[-1]
            d = diff_snapshots(previous, snap)
            print(f"\n【與 {previous['taken_at']} 的快照比較】")
            if not (d["added"] or d["removed"] or d["changed"]):
                print("  無變化")
            for code in d["added"]:
                print(f"  ➕ 新增 {code}")
            for code in d["removed"]:
                print(f"  ➖ 移除 {code}")
            for c in d["changed"]:
                print(f"  ✏️  {c['code']}：{c['qty_from']} → {c['qty_to']} 股，"
                      f"均價 {c['price_from']} → {c['price_to']}")
        else:
            print(f"\n（尚無歷史快照，資料夾：{SNAPSHOT_DIR}）")

    if args.snapshot:
        print(f"\n快照已存至 {save_snapshot(snap)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
