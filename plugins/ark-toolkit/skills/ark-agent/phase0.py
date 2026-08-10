# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""開局整理：把接手的部位降回衛星軌配額內。**一次性、有人監督、用完即棄。**

為什麼獨立成一支腳本，而不是在 execute.py 開一個繞過旗標：那種旗標日後一定
會被誤用，而 execute.py 是無人值守路徑的最後一道防線。把例外放在這裡，例外
就看得見——這支腳本不會被排程呼叫，而且預設 dry-run，要 --execute 才動手。

單筆／單日金額上限在這裡不適用：那些上限是為了限制**無人值守**時的爆炸半徑，
而開局整理是你在現場看著跑的。其餘紀律照守——繼承軌（虧損中）一股都不賣。

    uv run phase0.py              # 只印計畫
    uv run phase0.py --execute    # 真的送單
"""
import argparse
import datetime as dt
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import execute   # noqa: E402
import journal   # noqa: E402
import risk      # noqa: E402
import tracks    # noqa: E402

LIMIT_BAND = 0.004      # 限價區間 ±0.4%：夠寬能成交，又不至於接受離譜的價格


def shares_to_reduce(qty, price, quota):
    """要賣掉幾股才能讓部位回到配額內。

    留倉數**向下取整**：取整方向錯就會留下仍然超額的部位，而超額狀態下
    衛星軌連停損以外的操作都做不了。
    """
    if price <= 0:
        return 0
    keep = min(qty, math.floor(quota / price))
    return max(0, qty - keep)


def build_plan(positions, assigned, quota):
    """要送出的委託。只碰衛星軌的超額部位。

    繼承軌（虧損中、ARK 紀律不可賣）與主軌一律不動——開局整理豁免的只有
    金額上限，不豁免紀律。
    """
    plan = []
    for code, p in sorted(positions.items()):
        if assigned.get(code) != tracks.SATELLITE:
            continue
        price = p["last_price"]
        qty = shares_to_reduce(p["qty"], price, quota)
        if not qty:
            continue
        # 先對齊價格檔位，計畫印出來的就是真的會送出的價格
        etf = execute.is_etf(code)
        plan.append({"action": "sell", "code": code, "qty": qty,
                     "track": tracks.SATELLITE,
                     "limit_low": execute.round_to_tick(price * (1 - LIMIT_BAND),
                                                        etf, "sell"),
                     "limit_high": execute.round_to_tick(price * (1 + LIMIT_BAND),
                                                         etf, "buy"),
                     "reason": f"開局整理：衛星軌降回配額 {quota:,.0f} 內"})
    return plan


def main():
    ap = argparse.ArgumentParser(description="開局整理（一次性，預設只印計畫）")
    ap.add_argument("--execute", action="store_true",
                    help="真的送出委託（正式環境，動真錢）")
    args = ap.parse_args()

    import market
    import shioaji as sj

    with market.session() as api:
        rows = api.list_positions(api.stock_account, unit=sj.Unit.Share)
        positions = {p.code: {"qty": int(p.quantity), "avg_price": float(p.price),
                              "last_price": float(p.last_price), "pnl": float(p.pnl)}
                     for p in rows}
        balance = float(api.account_balance().acc_balance)

    assigned = tracks.load()
    resolved = tracks.resolve(assigned, positions, risk.DEFAULTS["min_trade_value"])
    agg = tracks.by_track(resolved)
    total = sum(r["value"] for r in resolved.values()) + balance
    quota = total * risk.DEFAULTS["satellite_quota_ratio"]

    print(f"總資源 {total:,.0f}｜衛星軌配額 {quota:,.0f}")
    for t in tracks.ALL:
        print(f"  {t:<10} {agg[t]['value']:>10,.0f}  {agg[t]['codes']}")

    plan = build_plan(positions, assigned, quota)
    if not plan:
        print("\n✅ 無需整理：衛星軌已在配額內")
        return 0

    print("\n計畫：")
    for o in plan:
        print(f"  {o['action']} {o['code']} {o['qty']} 股"
              f"（{o['qty'] * o['limit_low']:,.0f}～{o['qty'] * o['limit_high']:,.0f}）"
              f"　限價 {o['limit_low']}–{o['limit_high']}")

    if not args.execute:
        print("\n（dry-run。加 --execute 才會送單）")
        return 0

    print("\n送出中…")
    results = []
    with market.session() as api:
        execute.activate_ca(api, os.environ["SHIOAJI_CA_PATH"],
                            os.environ["SHIOAJI_CA_PASSWORD"],
                            api.stock_account.person_id)
        for o in plan:
            results += execute.place_legs(
                api, execute.build_legs(o, execute.contract_limits(api, o["code"])))
        api.update_status(api.stock_account)

    journal.append_entry({
        "type": "phase0", "date": dt.date.today().isoformat(),
        "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
        "note": "開局整理：一次性豁免金額上限，有人監督",
        "plan": plan, "legs": results,
    })
    ok = sum(1 for r in results if r["ok"])
    for r in results:
        detail = f"id={r['order_id']} {r['status']}" if r["ok"] else r["error"]
        print(f"  {'✓' if r['ok'] else '✗'} {r['action']} {r['code']} "
              f"{r['shares']} 股 @ {r['price']}　{detail}")
    print(f"\n{'✅' if ok == len(results) else '⚠️'} 送出 {ok}/{len(results)} 腿")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
