# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""整合驗證：execute.py 的委託路徑在這台機器上真的能送出去。

**只走 simulation**，硬編碼在程式裡，沒有切到正式環境的參數——這支腳本
不該有辦法動到真錢。

驗的是單元測試驗不到的那一段：build_legs 產出的參數組合，Shioaji 收不收。
刻意用 1,500 股讓它同時產生整股腿（Common, 1 張）與零股腿（IntradayOdd,
500 股），把兩種 order_lot 一次驗完；價格掛在跌停，不會成交，送完就撤。

    uv run integration_execute_simulation.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import execute  # noqa: E402
import source   # noqa: E402

CODE = "0050"
SHARES = 1500


def main():
    import shioaji as sj

    source.load_credentials()
    api = sj.Shioaji(simulation=True)          # 寫死：這支腳本不碰正式環境
    api.login(api_key=os.environ["SHIOAJI_API_KEY"],
              secret_key=os.environ["SHIOAJI_SECRET_KEY"])
    # 必須在 login 之後——未認證時呼叫會拋 AuthError
    api.set_order_callback(lambda state, msg: None)   # 收掉預設的 stdout 噪音
    try:
        limits = execute.contract_limits(api, CODE)
        print(f"{CODE} 參考價={limits['reference']}　"
              f"漲停={limits['limit_up']}　跌停={limits['limit_down']}")

        order = {"action": "buy", "code": CODE, "qty": SHARES,
                 "limit_low": limits["limit_down"], "limit_high": limits["limit_down"]}
        legs = execute.build_legs(order, limits)
        print(f"\nbuild_legs（{SHARES} 股）：")
        for leg in legs:
            print(f"  {leg['order_lot']:<12} quantity={leg['quantity']:<4}"
                  f" → {leg['shares']:>4} 股 @ {leg['price']}")
        assert sum(x["shares"] for x in legs) == SHARES, "拆腿後股數不守恆"

        results = execute.place_legs(api, legs)
        api.update_status(api.stock_account)
        print("\nplace_legs：")
        for r in results:
            print(f"  {'✓' if r['ok'] else '✗'} {r['order_lot']:<12}"
                  f" {r.get('status') or r.get('error')}"
                  f"　id={r.get('order_id')} seqno={r.get('seqno')}")

        seqnos = {r["seqno"] for r in results if r["ok"]}
        cancelled = 0
        for trade in api.list_trades():
            if trade.order.seqno in seqnos:
                api.cancel_order(trade)
                cancelled += 1
        api.update_status(api.stock_account)
        print(f"\n撤單 {cancelled}/{len(seqnos)} 筆")
        for trade in api.list_trades():
            if trade.order.seqno in seqnos:
                print(f"  {trade.order.order_lot} → {trade.status.status}")

        ok = all(r["ok"] for r in results) and cancelled == len(seqnos)
        print(f"\n{'✅ 整合驗證通過' if ok else '❌ 整合驗證失敗'}")
        return 0 if ok else 1
    finally:
        api.logout()


if __name__ == "__main__":
    sys.exit(main())
