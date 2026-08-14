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
"""滾動評估 Agent 軌的決策：前瞻報酬、勝率、對照 0050 超額、ARK 紀律遵循度。

交易日曆不維護——直接以 0050 的實際日K 日期序列為準，假日與臨時休市自動正確。
輸出只做描述性統計：樣本小且前瞻視窗互相重疊，任何比較都不是定論（見 render 尾註）。
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import journal  # noqa: E402
import market   # noqa: E402
import packet as packet_mod  # noqa: E402

DIVIDENDS = os.path.expanduser("~/.ark-toolkit/agent/dividends.jsonl")
HORIZONS = (5, 20, 60)


# ---------------------------------------------------------------- 日曆與報酬

def trading_days(benchmark_bars):
    """交易日曆＝基準（0050）實際有 K 棒的日期序列。"""
    return [b["date"] for b in benchmark_bars]


def next_on_or_after(calendar, date):
    """日曆上第一個 >= date 的索引；超出日曆回傳 None。"""
    for i, d in enumerate(calendar):
        if d >= date:
            return i
    return None


def forward_return(daily_by_date, exec_date, exec_price, n, calendar):
    """T+N 前瞻報酬（買進方向），回傳 (報酬或 None, 目標日或 None, status)。

    status: ok / pending（資料未到期）/ insufficient_bars（停牌，退用視窗內
    最後一根可用 bar）/ unevaluable（視窗內完全無 bar）。
    """
    i = next_on_or_after(calendar, exec_date)
    if i is None:
        return None, None, "pending"
    target_i = i + n
    if target_i >= len(calendar):
        return None, None, "pending"
    target = calendar[target_i]
    bar = daily_by_date.get(target)
    status = "ok"
    if bar is None:
        for d in reversed(calendar[i:target_i + 1]):
            if d in daily_by_date:
                bar, target, status = daily_by_date[d], d, "insufficient_bars"
                break
        else:
            return None, None, "unevaluable"
    return bar["close"] / exec_price - 1, target, status


# ---------------------------------------------------------------- 除權息

def load_dividends(path=DIVIDENDS):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def dividends_in_window(divs, code, start, end):
    """(start, end] 內該標的的除權息事件。"""
    return [d for d in divs
            if d.get("code") == code and start < d.get("ex_date", "") <= end]


def dividend_note(n_events):
    """報酬校正的可信度註記。

    `market.fetch_daily` 走 `api.kbars`，那是未還原的原始成交價（實證：0050
    於 2026-07-21 除息 0.6 元，而 2026-07-01 的快取收盤仍是除息前的 109.35）。
    沒有除權息資料時 `adj` 其實等於 `raw`，此時宣稱「已含現金股利校正」會讓
    跨越除息日的低估數字看起來可信——那是靜默失真，比明顯的錯誤更難發現。
    """
    if not n_events:
        return "⚠️ 無除權息資料，adj 未實際校正——跨越除息日的報酬會低估"
    return f"adj 已含現金股利校正（除權息事件 {n_events} 筆）"


# ---------------------------------------------------------------- 單筆決策

def exec_price_for(order, date, fill, daily, calendar):
    """成交價；無成交退用決策日收盤並標 no_fill。"""
    if fill:
        return fill["price"], "filled"
    i = next_on_or_after(calendar, date)
    if i is None:
        return None, "unevaluable"
    bar = daily.get(order["code"], {}).get(calendar[i])
    if bar is None:
        return None, "unevaluable"
    return bar["close"], "no_fill"


def horizon_result(order, exec_date, exec_price, n, code_daily, bench_daily,
                   calendar, dividends, benchmark=packet_mod.BENCHMARK):
    """單一 horizon 的結果：報酬（raw/除息校正）、基準、超額、除權息標記。

    賣出取「避開報酬」（賣後跌為正），且校正含股利——沒賣的話股利也會領到。
    超額 = 決策報酬（買＝漲幅、賣＝避開幅）− 同視窗 0050 買進持有報酬。
    """
    raw, target, status = forward_return(code_daily, exec_date, exec_price, n, calendar)
    out = {"status": status}
    if raw is None:
        return out
    divs = dividends_in_window(dividends, order["code"], exec_date, target)
    cash = sum(d.get("cash", 0.0) for d in divs)
    adj = raw + cash / exec_price
    sign = -1 if order["action"] == "sell" else 1
    out["return_raw"] = round(sign * raw, 6)
    out["return_adj"] = round(sign * adj, 6)
    out["target_date"] = target
    out["dividend_affected"] = bool(divs)
    if any(d.get("stock_ratio") for d in divs):
        out["stock_dividend_unadjusted"] = True

    i = next_on_or_after(calendar, exec_date)
    bench_exec = bench_daily.get(calendar[i]) if i is not None else None
    if bench_exec:
        b_raw, b_target, _b_status = forward_return(
            bench_daily, exec_date, bench_exec["close"], n, calendar)
        if b_raw is not None:
            b_cash = sum(d.get("cash", 0.0)
                         for d in dividends_in_window(dividends, benchmark,
                                                      exec_date, b_target))
            b_adj = b_raw + b_cash / bench_exec["close"]
            out["benchmark"] = round(b_adj, 6)
            out["excess"] = round(out["return_adj"] - b_adj, 6)
    return out


# ---------------------------------------------------------------- 紀律遵循度

def core_orders(decision):
    """只取主軌委託。

    實驗量的是「ARK 判斷準不準」，衛星軌是不受 ARK 紀律約束的量化軌——
    摻進去的話，紀律遵循度會被自己加的停損單扣分，報酬統計也不再回答
    原本那個問題。
    """
    return [o for o in decision.get("orders", [])
            if o.get("track", journal.CORE) == journal.CORE]


def adherence(decision, pk, fill_entry):
    """單日紀律分項與合成分。合成分＝通過的布林項 ÷ 適用項數（不適用不計分母）。"""
    disc = pk["discipline"]
    quotes = pk.get("market", {}).get("quotes", {})
    positions = pk.get("account", {}).get("positions", {})
    orders = core_orders(decision)
    sells = [o for o in orders if o["action"] == "sell"]
    buys = [o for o in orders if o["action"] == "buy"]

    sell_fills = [f for f in (fill_entry or {}).get("fills", []) if f["action"] == "sell"]
    if sell_fills:
        sell_value = sum(f["qty"] * f["price"] for f in sell_fills)
    else:
        sell_value = sum(o["qty"] * float(quotes.get(o["code"], {}).get("close")
                                          or (o.get("limit_low", 0) +
                                              o.get("limit_high", 0)) / 2)
                         for o in sells)

    comps = {}
    if disc.get("adjust_required_before_buy") and buys:
        comps["調節先行"] = sell_value >= disc["adjust_amount"]
    if sells:
        comps["賣出全為獲利中"] = all(o["code"] in disc["sellable"] for o in sells)
    if buys:
        comps["買進全在價值區"] = all(o["code"] in disc["buy_candidates"] for o in buys)
    fully_sold = {o["code"] for o in sells
                  if o["code"] in positions and o["qty"] >= positions[o["code"]]["qty"]}
    names_after = (set(positions) - fully_sold) | {o["code"] for o in buys}
    # 與 journal.validate_orders 同規則:既有超限不歸咎於本日決策
    comps["檔數未超限"] = len(names_after) <= max(disc["max_names"], len(positions))
    comps["非修訂決策"] = not decision.get("amended")
    comps["無硬規則覆寫"] = not decision.get("violations")

    adjust = disc.get("adjust_amount", 0.0)
    coverage = min(1.0, sell_value / adjust) if adjust > 0 and sells else None
    devs = [abs(o["qty"] - s) / s for o in sells
            for s in [pk["ark"]["holdings"].get(o["code"], {}).get("suggest_qty")]
            if s]
    return {"date": decision["date"], "components": comps,
            "score": round(sum(comps.values()) / len(comps), 4),
            "coverage": None if coverage is None else round(coverage, 4),
            "qty_deviation": round(sum(devs) / len(devs), 4) if devs else None}


# ---------------------------------------------------------------- 彙總

def evaluate_all(entries, packets, prices, benchmark_bars, dividends,
                 orders_of=core_orders):
    """`orders_of` 決定要評估哪些委託，預設只取主軌以維持實驗效度。
    複盤（review.py）傳入取全部的版本——衛星軌不進實驗統計，但要能被檢討。"""
    calendar = trading_days(benchmark_bars)
    bench_daily = {b["date"]: b for b in benchmark_bars}
    daily = {code: {b["date"]: b for b in bars} for code, bars in prices.items()}

    decision_dates = sorted({e["date"] for e in entries if e.get("type") == "decision"})
    fills_by_ref = {}
    for e in entries:
        if e.get("type") == "fill" and e.get("decision_ref") is not None:
            fills_by_ref.setdefault(e["decision_ref"], e)
    missed = [e for e in entries if e.get("type") == "missed"]
    amended = sum(1 for e in entries
                  if e.get("type") == "decision" and e.get("amended"))
    overrides = sum(1 for e in entries
                    if e.get("type") == "decision" and e.get("violations"))

    orders_out, adherence_rows = [], []
    for date in decision_dates:
        d = journal.first_decision(entries, date)
        pk = packets.get(date)
        fe = fills_by_ref.get(date)
        if pk:
            adherence_rows.append(adherence(d, pk, fe))
        fills = {(f["action"], f["code"]): f for f in (fe or {}).get("fills", [])}
        for o in orders_of(d):
            f = fills.get((o["action"], o["code"]))
            price, exec_status = exec_price_for(o, date, f, daily, calendar)
            row = {"date": date, "action": o["action"], "code": o["code"],
                   "qty": o["qty"], "exec_price": price, "exec_status": exec_status,
                   "horizons": {}}
            if price is not None:
                for n in HORIZONS:
                    row["horizons"][n] = horizon_result(
                        o, date, price, n, daily.get(o["code"], {}),
                        bench_daily, calendar, dividends)
            orders_out.append(row)

    horizons = {}
    for n in HORIZONS:
        rows = [r["horizons"].get(n, {}) for r in orders_out]
        done = [r for r in rows if "return_adj" in r]
        horizons[n] = {
            "evaluated": len(done),
            "pending": sum(1 for r in rows if r.get("status") == "pending"),
            "unevaluable": sum(1 for r in rows if r.get("status") == "unevaluable"),
            "dividend_affected": sum(1 for r in done if r.get("dividend_affected")),
            "win_rate": (round(sum(1 for r in done if r["return_adj"] > 0)
                               / len(done), 4) if done else None),
            "avg_return": (round(sum(r["return_adj"] for r in done)
                                 / len(done), 6) if done else None),
            "avg_excess": (round(sum(r["excess"] for r in done if "excess" in r)
                                 / len(done), 6) if done else None),
        }

    scores = [a["score"] for a in adherence_rows]
    coverages = [a["coverage"] for a in adherence_rows if a["coverage"] is not None]
    return {
        "n_decision_days": len(decision_dates),
        "n_orders": len(orders_out),
        "dividends_loaded": len(dividends),
        "missed": len(missed),
        "amended": amended,
        "overrides": overrides,
        "horizons": horizons,
        "adherence": {
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
            "avg_coverage": (round(sum(coverages) / len(coverages), 4)
                             if coverages else None),
            "days": adherence_rows,
        },
        "orders": orders_out,
    }


def render(report):
    out = []
    out.append(f"決策日 {report['n_decision_days']}　單量 {report['n_orders']}"
               f"　缺席 {report['missed']}　修訂 {report['amended']}"
               f"　硬規則覆寫 {report['overrides']}")
    out.append("\n【前瞻報酬】（賣出＝避開報酬；"
               f"{dividend_note(report.get('dividends_loaded', 0))}）")
    out.append("  視窗   已評/待期   勝率     平均報酬   平均超額(vs 0050)  受除權息影響")
    for n, h in report["horizons"].items():
        win = f"{h['win_rate']:.0%}" if h["win_rate"] is not None else "-"
        ret = f"{h['avg_return']:+.2%}" if h["avg_return"] is not None else "-"
        exc = f"{h['avg_excess']:+.2%}" if h["avg_excess"] is not None else "-"
        out.append(f"  T+{n:<4} {h['evaluated']:>3}/{h['pending']:<4}  {win:>6}"
                   f"   {ret:>9}   {exc:>15}   {h['dividend_affected']} 筆")
    a = report["adherence"]
    out.append("\n【ARK 紀律遵循度】")
    score = f"{a['avg_score']:.0%}" if a["avg_score"] is not None else "-"
    cov = f"{a['avg_coverage']:.0%}" if a["avg_coverage"] is not None else "-"
    out.append(f"  平均合成分 {score}　平均調節覆蓋率 {cov}")
    out.append("\n以上為描述性統計：樣本數有限、前瞻視窗互相重疊，"
               "數字會隨樣本累積變動，不能當成兩軌優劣的定論。")
    return "\n".join(out)


# ---------------------------------------------------------------- CLI

def load_all_packets(directory=packet_mod.PACKET_DIR):
    out = {}
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                pk = json.load(fh)
            out[pk["date"]] = pk
    return out


def main():
    ap = argparse.ArgumentParser(description="評估 ark-agent 的決策成績")
    ap.add_argument("--json", action="store_true", help="輸出機器可讀 JSON")
    ap.add_argument("--offline", action="store_true",
                    help="只用本地日K快取，不連 Shioaji 補資料")
    args = ap.parse_args()

    entries = journal.load_entries()
    packets = load_all_packets()
    dividends = load_dividends()
    decision_dates = sorted({e["date"] for e in entries if e.get("type") == "decision"})
    if not decision_dates:
        print("尚無任何決策紀錄")
        return 0

    codes = sorted({o["code"] for e in entries if e.get("type") == "decision"
                    for o in e.get("orders", [])} | {packet_mod.BENCHMARK})
    start = (dt.date.fromisoformat(decision_dates[0])
             - dt.timedelta(days=7)).isoformat()
    today = dt.date.today().isoformat()

    if args.offline:
        prices = {c: [b for b in market.load_cache(c) if start <= b["date"] <= today]
                  for c in codes}
    else:
        with market.session() as api:
            prices = {c: market.fetch_daily(api, c, start, today) for c in codes}

    report = evaluate_all(entries, packets, prices,
                          prices.get(packet_mod.BENCHMARK, []), dividends)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
          else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
