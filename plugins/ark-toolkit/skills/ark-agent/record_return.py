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
"""把當日已實現獲利記進 ARK「運算 › 離職倒數」（盤後結算流程呼叫）。

**只記獲利日**——虧損由本金減少反映，這是 App 自己的紀律（使用者要求，
2026-08-11 起）。金額取券商 `list_profit_loss` 的 pnl 合計，那個數字已含
手續費與證交稅；自己用成交價減成本價推算會少扣稅費，記進去就偏高。

離開碼只有「該記卻沒記成」才是 1。虧損日與只買不賣的日子回 0——回報失敗會讓
結算天天誤報「部分失敗」，真正的故障反而被雜訊淹沒。
"""
import argparse
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark     # noqa: E402
import ax      # noqa: E402
import market  # noqa: E402


# ---------------------------------------------------------------- 純邏輯

def rows_for_date(rows, date):
    """只留下指定日期的成交列。

    `list_profit_loss` 給了區間就該只有那幾天，但補記過去某日時區間仍可能
    夾帶鄰近日期，多算一天會把數字記錯。
    """
    return [r for r in rows if str(getattr(r, "date", "")) == date]


def realized_pnl(rows):
    """已實現損益合計。同日一賺一賠取淨額，不是只挑賺的那筆。"""
    return float(sum(r.pnl for r in rows))


def should_record(amount):
    """是否該記入。App 紀律：只記獲利日；四捨五入後為 0 就沒東西可記。"""
    return int(round(amount)) > 0


def exit_code(should, written):
    """離開碼：只有「該記卻沒記成」算失敗。"""
    return 1 if should and not written else 0


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="把當日已實現獲利記進離職倒數")
    ap.add_argument("--date", help="覆寫日期（預設今天）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印出將記錄的金額，不寫入 ARK")
    args = ap.parse_args()
    date = args.date or dt.date.today().isoformat()

    with market.session() as api:
        import shioaji as sj
        rows = rows_for_date(
            api.list_profit_loss(api.stock_account, date, date, unit=sj.Unit.Share),
            date)
    amount = realized_pnl(rows)

    if not should_record(amount):
        print(f"{date} 已實現損益 {amount:,.0f}——非獲利日，依 App 紀律不記錄")
        return 0
    if args.dry_run:
        print(f"（dry-run）{date} 將記錄已實現獲利 {amount:,.0f} 元")
        return 0

    pid = ark.ensure_responsive(ax, ax.activate())
    written = ark.record_daily_return(ax, pid, amount, date=date)
    print(f"{'✅' if written else '🛑'} {date} 已實現獲利 {amount:,.0f} 元"
          f"{'已記入離職倒數' if written else '寫入失敗'}"
          f"（{'、'.join(r.code for r in rows)}）")
    return exit_code(should=True, written=written)


if __name__ == "__main__":
    sys.exit(main())
