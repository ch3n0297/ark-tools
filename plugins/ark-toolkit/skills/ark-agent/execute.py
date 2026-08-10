# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""下單層：把鎖定的決策翻成 Shioaji 委託並送出。

**兩個單位陷阱**，兩個都不會被上游擋到（journal 只看股數與代號，看不到最後
送進 API 的參數）：

1. `Common` 的 quantity 單位是**張**，`IntradayOdd` 是**股**。整個 toolkit 以
   股計價，所以 47 股的 2330 若當成 `Common, quantity=47` 送出，就是 47 張、
   一億一千萬的委託。`split_shares` 負責換算與拆腿。
2. 台股 **ETF 的價格檔位與個股不同**（ETF 兩段、個股六段）。檔位算錯會被券商
   拒單——安全失敗，不會亂成交，但當天就交易不了。

純函式（拆腿、檔位、限價、送出前檢查）不匯入 shioaji，任何平台可測；
連線只發生在 `place_legs` 與 `activate_ca`。
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import journal  # noqa: E402
import risk     # noqa: E402

COMMON = "Common"
INTRADAY_ODD = "IntradayOdd"
SHARES_PER_LOT = 1000

# (價格上界, 檔位)。ETF 兩段、個股六段——實證：帳戶內 00635U @ 45.03 落在
# 0.01 檔、0050 @ 104.25 落在 0.05 檔，套個股表（100–500 為 0.5）都不合法。
ETF_TICKS = ((50.0, 0.01), (math.inf, 0.05))
STOCK_TICKS = ((10.0, 0.01), (50.0, 0.05), (100.0, 0.1),
               (500.0, 0.5), (1000.0, 1.0), (math.inf, 5.0))

EPS = 1e-9

# 限價可以偏離參考價多少。掛得太寬等於下市價單：賣單限價低於市價 2%，就是
# 事先接受最差 −2% 的成交，而無人值守時沒人會發現這種滑價。
MAX_LIMIT_DEVIATION = 0.02


# ---------------------------------------------------------------- 股數與檔位

def split_shares(shares):
    """股數 → [(order_lot, quantity)]，quantity 已換成該 lot 的單位。

    整張走 Common（單位：張），畸零股走 IntradayOdd（單位：股，上限 999）。
    """
    if shares < 0:
        raise ValueError(f"股數不可為負：{shares}")
    lots, odd = divmod(shares, SHARES_PER_LOT)
    out = []
    if lots:
        out.append((COMMON, lots))
    if odd:
        out.append((INTRADAY_ODD, odd))
    return out


def is_etf(code):
    """台股 ETF 代號一律以 00 開頭（0050、00635U、00631L…）。

    判錯的後果是檔位算錯 → 券商拒單 → 當天不交易，不會造成錯誤成交。
    """
    return code.startswith("00")


def tick_size(price, is_etf):
    table = ETF_TICKS if is_etf else STOCK_TICKS
    for upper, tick in table:
        if price < upper:
            return tick
    return table[-1][1]


def round_to_tick(price, is_etf, action):
    """把價格對齊到合法檔位。買進向下、賣出向上——都是對自己較保守的方向。"""
    tick = tick_size(price, is_etf)
    n = price / tick
    n = math.floor(n + EPS) if action == "buy" else math.ceil(n - EPS)
    return round(n * tick, 4)


# ---------------------------------------------------------------- 限價與委託腿

def limit_price(order, limits):
    """決策的限價區間 → 實際限價（夾在漲跌停內並對齊檔位）。

    買進取區間上緣、賣出取下緣——限價的意義是「最差可接受價」，取反方向
    等於大概率不成交。沒有限價區間就拒絕：無人值守系統不下市價單。
    """
    low, high = order.get("limit_low"), order.get("limit_high")
    if not low or not high:
        raise ValueError(f"{order['code']} 缺少限價區間，無人值守不下市價單")
    raw = high if order["action"] == "buy" else low
    clamped = min(limits["limit_up"], max(limits["limit_down"], raw))
    return round_to_tick(clamped, is_etf(order["code"]), order["action"])


def build_legs(order, limits):
    """一筆決策 → 可直接送出的委託腿。`shares` 保留原始股數供成交對帳。"""
    price = limit_price(order, limits)
    return [{"code": order["code"], "action": order["action"],
             "order_lot": lot, "quantity": qty,
             "shares": qty * SHARES_PER_LOT if lot == COMMON else qty,
             "price": price}
            for lot, qty in split_shares(order["qty"])]


def execution_guard(orders, envelope, quotes, packet_hash):
    """送出前的最後一道檢查，回傳阻擋原因（空 = 放行）。

    縱深防禦：journal 驗 ARK 紀律、envelope 定執行邊界，送出前再查一次金額
    上限——前兩道任一出錯時，這道仍能把爆炸半徑鎖住。
    """
    out = []
    if envelope.get("packet_hash") != packet_hash:
        out.append("envelope 與決策的 packet_hash 不符——限額建立在別天的事實上")
    if orders and not envelope.get("can_buy") and any(o["action"] == "buy"
                                                      for o in orders):
        out.append("執行邊界：目前不可買進")
    if orders and not envelope.get("can_sell") and any(o["action"] == "sell"
                                                       for o in orders):
        out.append("執行邊界：目前不可賣出")
    out += limit_deviation_violations(orders, quotes)
    return out + risk.order_caps_violations(orders, quotes, envelope["limits"])


def limit_deviation_violations(orders, quotes):
    """限價偏離參考價過大者。

    賣單看 `limit_low`（願意接受的最低價）、買單看 `limit_high`（願付的最高價）——
    那才是實際會送出的價格，也是滑價的上限。沒有參考價時不判，交給金額上限把關。
    """
    out = []
    for o in orders:
        ref = (quotes.get(o["code"]) or {}).get("close")
        edge = o.get("limit_low") if o["action"] == "sell" else o.get("limit_high")
        if not ref or not edge:
            continue
        deviation = abs(edge - ref) / ref
        if deviation > MAX_LIMIT_DEVIATION:
            out.append(f"{o['code']} 限價 {edge} 偏離參考價 {ref} 達 {deviation:.1%}"
                       f"，超過 {MAX_LIMIT_DEVIATION:.0%}（等同下市價單）")
    return out


# ---------------------------------------------------------------- 連線與送出

def activate_ca(api, ca_path, ca_passwd, person_id):
    """啟用憑證。正式環境下單的前提，模擬環境不需要。"""
    if not os.path.exists(ca_path):
        raise RuntimeError(f"找不到憑證檔：{ca_path}")
    if not api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd,
                           person_id=person_id):
        raise RuntimeError("憑證啟用失敗——請檢查密碼與 person_id 是否與憑證相符")
    return True


def place_legs(api, legs):
    """送出委託腿，回傳每腿的結果（成功與失敗都回報，不中途拋出）。

    一腿失敗不該讓其餘的腿無聲消失——後續對帳要知道哪幾腿真的送出去了。
    """
    import shioaji as sj

    lots = {COMMON: sj.StockOrderLot.Common,
            INTRADAY_ODD: sj.StockOrderLot.IntradayOdd}
    actions = {"buy": sj.Action.Buy, "sell": sj.Action.Sell}
    out = []
    for leg in legs:
        try:
            order = sj.StockOrder(
                action=actions[leg["action"]], price=leg["price"],
                quantity=leg["quantity"], price_type=sj.StockPriceType.LMT,
                order_type=sj.OrderType.ROD, order_lot=lots[leg["order_lot"]],
                order_cond=sj.StockOrderCond.Cash, account=api.stock_account)
            trade = api.place_order(api.Contracts.Stocks[leg["code"]], order)
            out.append({**leg, "ok": True, "order_id": trade.status.id,
                        "seqno": trade.order.seqno,
                        "status": str(trade.status.status)})
        except Exception as e:                                    # noqa: BLE001
            out.append({**leg, "ok": False,
                        "error": f"{type(e).__name__}: {e}"})
    return out


def contract_limits(api, code):
    c = api.Contracts.Stocks[code]
    return {"limit_up": float(c.limit_up), "limit_down": float(c.limit_down),
            "reference": float(c.reference)}


# ---------------------------------------------------------------- CLI

def main():
    import market
    import packet as packet_mod

    ap = argparse.ArgumentParser(description="送出已鎖定的決策")
    ap.add_argument("--backend", choices=("simulation", "live"), required=True,
                    help="simulation 走模擬環境；live 需憑證，會動真錢")
    ap.add_argument("--date", help="覆寫決策日（預設今天）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印出將送出的委託腿，不連線")
    args = ap.parse_args()

    date = args.date or dt.date.today().isoformat()
    pk = packet_mod.load_packet(date)
    decision = journal.first_decision(journal.load_entries(), date)
    env_path = os.path.join(risk.ENVELOPE_DIR, f"{date}.json")
    if pk is None or decision is None or not os.path.exists(env_path):
        missing = [n for n, ok in (("決策包", pk), ("決策", decision),
                                   ("執行邊界", os.path.exists(env_path))) if not ok]
        print(f"🛑 缺少 {'、'.join(missing)}，不執行", file=sys.stderr)
        return 2
    with open(env_path, encoding="utf-8") as fh:
        envelope = json.load(fh)

    orders = decision.get("orders", [])
    if not orders:
        print("本日決策為不動作，無委託可送")
        return 0

    blocked = execution_guard(orders, envelope,
                              pk.get("market", {}).get("quotes", {}), pk["hash"])
    if blocked:
        print("🛑 送出前檢查未通過：", file=sys.stderr)
        for b in blocked:
            print(f"  - {b}", file=sys.stderr)
        return 2

    if args.dry_run:
        with market.session() as api:
            for o in orders:
                for leg in build_legs(o, contract_limits(api, o["code"])):
                    print(f"  {leg['action']} {leg['code']} "
                          f"{leg['order_lot']} qty={leg['quantity']}"
                          f"（{leg['shares']} 股）@ {leg['price']}")
        return 0

    import shioaji as sj
    import source
    source.load_credentials()
    api = sj.Shioaji(simulation=args.backend == "simulation")
    api.login(api_key=os.environ["SHIOAJI_API_KEY"],
              secret_key=os.environ["SHIOAJI_SECRET_KEY"])
    try:
        if args.backend == "live":
            activate_ca(api, os.environ["SHIOAJI_CA_PATH"],
                        os.environ["SHIOAJI_CA_PASSWORD"],
                        api.stock_account.person_id)
        results = []
        for o in orders:
            results += place_legs(api, build_legs(o, contract_limits(api, o["code"])))
        api.update_status(api.stock_account)
    finally:
        api.logout()

    entry = {"type": "execution", "date": date, "backend": args.backend,
             "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
             "decision_lock": decision["lock"], "legs": results}
    journal.append_entry(entry)

    ok = sum(1 for r in results if r["ok"])
    print(f"{'✅' if ok == len(results) else '⚠️'} 送出 {ok}/{len(results)} 腿"
          f"（{args.backend}）")
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        detail = (f"id={r['order_id']} {r['status']}" if r["ok"] else r["error"])
        print(f"  {mark} {r['action']} {r['code']} {r['shares']} 股 @ {r['price']}"
              f"　{detail}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
