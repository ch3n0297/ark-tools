# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""每日淨值曲線與熔斷判定。

熔斷的基準是**歷史峰值**而非期初本金——用期初本金當基準的話，賺了一波之後
回吐同樣的金額不會觸發任何防線，而那正是最該停手的時候。

分軌淨值（satellite）獨立記錄：衛星軌配額 25% 全滅等於帳戶 −25%，已超過
帳戶級 L2 的 15%，單靠總資產回撤會攔太晚，需要軌內自己那道。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark  # noqa: E402

EQUITY = os.path.expanduser("~/.ark-toolkit/agent/equity.jsonl")

NONE, L1, L2 = "none", "L1", "L2"


def make_point(date, stock_value, cash, satellite_value):
    """一天的淨值快照。total 與 ARK 運算頁的「資金總額」同義（持股＋現金）。"""
    return {"date": date, "total": stock_value + cash, "stock": stock_value,
            "cash": cash, "satellite": satellite_value}


def peak(points, key="total"):
    """歷史峰值；無資料回 None。"""
    return max((p[key] for p in points), default=None)


def drawdown(points, key="total"):
    """當前相對歷史峰值的回撤（≤ 0）；無資料或峰值為零回 0。

    當前值取**最後一筆**：同日重跑會 append 第二筆，取最大或平均都會低估回撤。
    """
    if not points:
        return 0.0
    high = peak(points, key)
    if not high:
        return 0.0
    return min(0.0, points[-1][key] / high - 1)


def breaker_level(dd, l1, l2):
    """回撤 → 熔斷等級。門檻是「到了就停」，不是「超過才停」。"""
    if dd <= -l2:
        return L2
    if dd <= -l1:
        return L1
    return NONE


def append_point(point, path=EQUITY):
    ark.append_sync_log(point, path)


def load_points(path=EQUITY):
    """讀全部淨值點，壞行略過（比照 journal.load_entries）。"""
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


def main():
    """記錄當日淨值點（盤後結算流程呼叫）。

    只算**交易帳戶**的價值（持股市值＋券商現金），不含 ARK 現金欄可能納入的
    帳戶外資金——回撤要衡量的是承擔風險的那部分錢，把不在市場裡的錢算進來
    會稀釋回撤、讓熔斷失去意義。
    """
    import argparse
    import datetime as dt

    import market
    import risk           # 函式內匯入：risk 於模組層匯入 equity，頂層互匯會成環
    import tracks

    ap = argparse.ArgumentParser(description="記錄當日淨值點")
    ap.add_argument("--date", help="覆寫日期（預設今天）")
    args = ap.parse_args()
    date = args.date or dt.date.today().isoformat()

    import shioaji as sj
    with market.session() as api:
        rows = api.list_positions(api.stock_account, unit=sj.Unit.Share)
        positions = {p.code: {"qty": int(p.quantity), "avg_price": float(p.price),
                              "last_price": float(p.last_price), "pnl": float(p.pnl)}
                     for p in rows}
        balance = float(api.account_balance().acc_balance)
        # 未交割應收付必須計入現金：大額調節日的賣出款 T+2 才入帳，只看
        # 已交割餘額會出現假回撤（2026-08-11 實測 −40%），把熔斷誤觸成 L2。
        s = api.list_settlements(api.stock_account)
        pending = float(s.t_money) + float(s.t1_money) + float(s.t2_money)

    cash = balance + pending
    resolved = tracks.resolve(tracks.load(), positions,
                              risk.DEFAULTS["min_trade_value"])
    stock = sum(r["value"] for r in resolved.values())
    satellite = tracks.by_track(resolved)[tracks.SATELLITE]["value"]
    point = make_point(date, stock, cash, satellite)
    append_point(point)

    points = load_points()
    dd = drawdown(points)
    level = breaker_level(dd, risk.DEFAULTS["breaker_l1"], risk.DEFAULTS["breaker_l2"])
    print(f"✅ {date} 淨值 {point['total']:,.0f}"
          f"（持股 {stock:,.0f}＋現金 {balance:,.0f}＋未交割 {pending:,.0f}）")
    print(f"   峰值 {peak(points):,.0f}｜回撤 {dd:.2%}｜熔斷 {level}"
          f"｜衛星軌 {satellite:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
