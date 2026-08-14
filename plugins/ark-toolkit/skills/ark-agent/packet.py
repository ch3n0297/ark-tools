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
"""決策包：把 Agent 做判斷需要的全部事實收進一份結構化 JSON。

實驗協定見 SKILL.md。這裡只收集事實與機器可算的紀律邊界，**不做任何買賣判斷**——
判斷發生在 Agent 的對話中，記錄在 journal。ARK 讀取共用 lib/ark.py，
行情走 market.py 的單一 session。

排程在**平日 10:00** 執行：官方教學說五指標約每小時重算、且「台股盤中算出調
台股」，此時 ARK 已用當日盤中資料重算過至少一次；同時避開 09:00–09:30 的開盤
波動。本模組本身與時點無關（snapshots 給的就是當下），不需要為盤中特別分支。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark      # noqa: E402
import market   # noqa: E402

PACKET_DIR = os.path.expanduser("~/.ark-toolkit/agent/packets")
SYNC_PY = os.path.abspath(os.path.join(HERE, "..", "ark-sync", "sync.py"))
BENCHMARK = "0050"
RECENT_CALENDAR_DAYS = 40      # 約 20 個交易日的回看視窗

# 決策準則檔：複盤累積下來、要餵回決策層的經驗。內容是個人化的（每個使用者的
# 檢討結論不同），所以預設放執行資料夾，`ARK_RULES` 可指到專案內以便版控。
RULES_PATH = os.environ.get(
    "ARK_RULES", os.path.expanduser("~/.ark-toolkit/decision-rules.md"))


# ---------------------------------------------------------------- 決策準則

def rule_ids(text):
    """抽出準則編號（標題行形如 `### R-001 標題`）。

    只認標題行：內文引用 R-999 是在講另一條準則，不是宣告新的一條，
    否則決策層會拿到一堆不存在的編號去填 rules_applied。
    """
    return re.findall(r"^###\s+(R-\d+)", text, re.MULTILINE)


def load_rules(path=RULES_PATH):
    """讀決策準則檔；讀不到就當作沒有準則。

    這是後加的功能，它故障不該讓當天整天不交易——決策照常走硬邊界，
    只是少了經驗輔助。原文與編號都帶上：原文給決策層脈絡，編號供其引用。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {"source": path, "text": "", "ids": []}
    return {"source": path, "text": text, "ids": rule_ids(text)}


# ---------------------------------------------------------------- 紀律邊界（純函式）

def compute_max_names(stock_value, cash):
    """檔數公式（官方教學）：(持股市值＋閒錢) ÷ 10 萬，90 萬以上封頂 9，至少 1。

    檔數同時是「當天最多可買的股票種數」。語意出處：references/ark-app-map.md。
    """
    return max(1, min(9, int((stock_value + cash) // 100_000)))


def build_discipline(posture, holdings, layout):
    """把 ARK 紀律算成機器可查的邊界，Agent 不必自己重推。

    語意出處皆為 references/ark-app-map.md（官方教學）：
    調節＝賣出側且獲利才調節；升溫區優先調節；布局只挑價值區；
    App 顯示參考調節（>0）時先調節才可買。
    """
    stock_value = posture.stock_value if posture else sum(h.value for h in holdings.values())
    cash = posture.cash if posture else 0.0
    adjust = posture.adjust_amount if posture else 0.0

    sellable = sorted(h.code for h in holdings.values() if h.pnl > 0)
    hot = sorted((h for h in holdings.values() if "升溫" in h.tiers and h.pnl > 0),
                 key=lambda h: -h.pnl)
    priority = [h.code for h in hot]
    priority += [h.code for h in holdings.values()
                 if h.suggest_qty is not None and h.pnl > 0 and h.code not in priority]
    rows = layout.rows.values() if layout else ()
    return {
        "max_names": compute_max_names(stock_value, cash),
        "adjust_required_before_buy": adjust > 0,
        "adjust_amount": adjust,
        "sellable": sellable,
        "sell_priority": priority,
        "buy_candidates": sorted(r.code for r in rows if "價值" in r.tiers),
    }


def news_scope_codes(holdings, layout, benchmark=BENCHMARK):
    """資訊邊界的機器可查證清單：持倉 ∪ 布局自選 ∪ 基準。

    Agent 只可就這份清單內的標的與其產業做新聞搜尋（實驗協定的半開放邊界）。
    """
    codes = set(holdings)
    if layout:
        codes |= set(layout.rows)
    codes.add(benchmark)
    return sorted(codes)


def positions_diff(holdings, positions):
    """ARK 與券商持倉對不上的代號（與 analyze.shioaji_diff 同規則，欄位格式不同）。"""
    return sorted(c for c in set(holdings) | set(positions)
                  if c not in holdings or c not in positions
                  or holdings[c].qty != positions[c]["qty"]
                  or abs(holdings[c].price - positions[c]["avg_price"]) >= ark.PRICE_TOL)


# ---------------------------------------------------------------- 序列化與組裝

def serialize_holdings(holdings):
    """與 analyze.build_snapshot 不同：cost / today_pnl / suggest_amount 必須保留，
    決策要用到（覆蓋參考調節金額的算術、當日動能）。"""
    return {h.code: {"qty": h.qty, "price": h.price, "cost": h.cost, "value": h.value,
                     "pnl": h.pnl, "roi": h.roi, "today_pnl": h.today_pnl,
                     "tiers": list(h.tiers), "suggest_qty": h.suggest_qty,
                     "suggest_amount": h.suggest_amount}
            for h in holdings.values()}


def serialize_posture(p):
    if p is None:
        return None
    return {"suggested_ratio": p.suggested_ratio, "actual_ratio": round(p.actual_ratio, 2),
            "gap": round(p.gap, 2), "stock_value": p.stock_value, "cash": p.cash,
            "suggested_value": p.suggested_value, "suggested_cash": p.suggested_cash,
            "adjust_amount": p.adjust_amount}


def serialize_layout(view):
    if view is None:
        return None
    return {"watchlist": view.watchlist,
            "rows": {r.code: {"tiers": list(r.tiers), "price": r.price, "change": r.change,
                              "nav": r.nav, "premium": r.premium,
                              "tier_qty": r.tier_qty, "tier_amount": r.tier_amount,
                              "risk_qty": r.risk_qty, "risk_amount": r.risk_amount}
                     for r in view.rows.values()}}


def build_packet(date, generated_at, holdings, declared, posture, layout,
                 positions, balance, settlements, quotes, recent_daily, sync_ok, diff,
                 rules=None):
    """組裝決策包（全注入，不碰 AX 與 API，任何平台可測）。

    `rules` 一併進 hash：journal 每筆決策都綁 packet_hash，等於鎖住「當天用的是
    哪一版準則」。少了這條，準則檔改過之後就無法把損益歸因回當時的準則。
    """
    pk = {
        "schema": 1,
        "date": date,
        "generated_at": generated_at,
        "ark": {
            "holdings": serialize_holdings(holdings),
            "declared_count": declared,
            "posture": serialize_posture(posture),
            "layout": serialize_layout(layout),
        },
        "account": {
            "positions": positions,
            "balance": balance,
            "settlements": settlements,
            "sync_ok": sync_ok,
            "diff": diff,
        },
        "market": {
            "quotes": quotes,
            "recent_daily": recent_daily,
            "note": ("quotes 為 generated_at 當下的快照——盤中執行時是即時價，"
                     "非交易時段則是最近一次收盤"),
        },
        "discipline": build_discipline(posture, holdings, layout),
        "news_scope": {
            "codes": news_scope_codes(holdings, layout),
            "note": "僅可就以上標的與其產業做新聞搜尋，不可掃全市場",
        },
        "rules": rules or {"source": None, "text": "", "ids": []},
    }
    pk["hash"] = packet_hash(pk)
    return pk


def packet_hash(pk):
    """canonical JSON（排除 hash 欄自身）的 sha256——journal 鎖定時綁定它。"""
    body = {k: v for k, v in pk.items() if k != "hash"}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def save_packet(pk, directory=PACKET_DIR):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{pk['date']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pk, fh, ensure_ascii=False, indent=2)
    return path


def load_packet(date, directory=PACKET_DIR):
    """讀某日的決策包，沒有則回傳 None。"""
    path = os.path.join(directory, f"{date}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 編排（AX + Shioaji）

def read_ark(ax, pid):
    holdings = ark.read_holdings(ax, pid)
    declared = ark.read_declared_count(ax, pid)
    ark.check_parsed(holdings, declared)
    posture = ark.read_posture(ax, pid)
    return holdings, declared, posture


def run_sync():
    """與 analyze.run_sync 同一模式：走 ark-sync 的 CLI，不生第二套同步實作。"""
    if not os.path.exists(SYNC_PY):
        print(f"  ✗ 找不到 ark-sync（{SYNC_PY}），略過自動同步")
        return False
    done = subprocess.run([sys.executable, SYNC_PY, "--allow-delete"])
    print()
    return done.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="產生 ark-agent 的盤前決策包")
    ap.add_argument("--date", help="覆寫決策日（預設今天；僅供測試或補跑）")
    ap.add_argument("--force", action="store_true", help="週末也照跑")
    ap.add_argument("--no-sync", action="store_true", help="ARK 與真實持倉不一致時不自動同步")
    ap.add_argument("--no-settle", action="store_true", help="不順手結算前一交易日的成交")
    args = ap.parse_args()

    date = args.date or dt.date.today().isoformat()
    if dt.date.fromisoformat(date).weekday() >= 5 and not args.force:
        print(f"⚠️  {date} 是週末非交易日，不產生決策包（--force 可強制）")
        return 2

    ark.check_platform(tool="ark-agent")
    import ax
    import shioaji as sj

    pid = ark.ensure_responsive(ax, ax.activate())   # 殭屍態在這裡自癒，不留到讀取中途

    # 佈局的眼睛：先把策略頁分區的當日標的「取代」進布局自選，買進候選才是
    # 今天的價值區而非建清單那天的快照。刷新失敗沿用舊清單（位階欄仍會擋掉
    # 過時標的，只是看不到新進場者），不值得為此放棄整天交易。
    strategy_tab = os.environ.get("ARK_STRATEGY_TAB", "ETF價值區")
    print(f"刷新布局自選（策略 › {strategy_tab}）…", flush=True)
    layout_list = ark.current_layout_list(ax, pid)
    refreshed = (ark.replace_watchlist_from_strategy(ax, pid, strategy_tab, layout_list)
                 if layout_list else None)
    if refreshed is None:
        print("  ⚠️ 刷新失敗，沿用既有清單", flush=True)
    else:
        print(f"  {len(refreshed)} 檔", flush=True)

    print("讀取 ARK…", flush=True)
    holdings, declared, posture = read_ark(ax, pid)
    print(f"  {len(holdings)} 檔", flush=True)

    # 買進側的錨：黃鈕為「建議布局」時先跑位階運算機，布局自選的
    # 位階股數／風控股數才是今日值——那是 App 給的每檔買進建議，
    # 決策層應以它為錨（與賣出側的 suggest_qty 對稱）。
    if posture is not None:
        budget = max(0.0, posture.suggested_value - posture.stock_value)
        names = compute_max_names(posture.stock_value, posture.cash)
        if budget > 0 and ark.run_tier_calculator(ax, pid, budget, names):
            print(f"  位階運算完成（閒錢 {budget:,.0f}／{names} 檔）", flush=True)

    print("讀取布局自選…", flush=True)
    layout = ark.read_layout(ax, pid)
    print(f"  「{layout.watchlist}」{len(layout.rows)} 檔", flush=True)

    with market.session() as api:
        rows = api.list_positions(api.stock_account, unit=sj.Unit.Share)
        positions = {p.code: {"qty": int(p.quantity), "avg_price": float(p.price),
                              "last_price": float(p.last_price), "pnl": float(p.pnl)}
                     for p in rows}
        diff = positions_diff(holdings, positions)
        sync_ok = not diff
        if diff and not args.no_sync:
            print(f"⚠️ {len(diff)} 檔與券商不一致：{', '.join(diff)}　→ 自動同步中…\n",
                  flush=True)
            run_sync()
            # 不論同步回報成功與否都要重讀——動過寫入，手上的資料就過期了
            holdings, declared, posture = read_ark(ax, pid)
            rows = api.list_positions(api.stock_account, unit=sj.Unit.Share)
            positions = {p.code: {"qty": int(p.quantity), "avg_price": float(p.price),
                                  "last_price": float(p.last_price), "pnl": float(p.pnl)}
                         for p in rows}
            diff = positions_diff(holdings, positions)
            sync_ok = not diff

        balance = float(api.account_balance().acc_balance)
        s = api.list_settlements(api.stock_account)
        settlements = {"today": float(s.t_money), "t1": float(s.t1_money),
                       "t2": float(s.t2_money)}

        codes = news_scope_codes(holdings, layout)
        contracts = [c for c in (api.Contracts.Stocks.get(code) for code in codes) if c]
        quotes = market.quotes_from_snapshots(api.snapshots(contracts)) if contracts else {}
        start = (dt.date.fromisoformat(date)
                 - dt.timedelta(days=RECENT_CALENDAR_DAYS)).isoformat()
        print(f"抓取 {len(codes)} 檔近 20 個交易日日K…", flush=True)
        recent = {code: market.fetch_daily(api, code, start, date) for code in codes}

        if not args.no_settle:
            import journal
            journal.settle_previous(api, date)

    now = dt.datetime.now().replace(microsecond=0).isoformat()
    rules = load_rules()
    pk = build_packet(date=date, generated_at=now, holdings=holdings, declared=declared,
                      posture=posture, layout=layout, positions=positions, balance=balance,
                      settlements=settlements, quotes=quotes, recent_daily=recent,
                      sync_ok=sync_ok, diff=diff, rules=rules)
    path = save_packet(pk)

    print(f"\n✅ 決策包已存至 {path}")
    print(f"   持倉 {len(holdings)} 檔｜對帳 {'一致' if sync_ok else f'不一致 {diff}'}"
          f"｜參考調節 {pk['discipline']['adjust_amount']:,.0f}"
          f"｜news_scope {len(codes)} 檔｜準則 {len(rules['ids'])} 條")
    if not sync_ok:
        print("   ⚠️ 對帳不一致——建議先處理再做決策，否則紀律邊界建立在錯的持倉上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
