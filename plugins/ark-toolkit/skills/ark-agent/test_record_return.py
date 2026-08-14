"""當日已實現獲利記錄的純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import unittest

import record_return as rr


class Row:
    """list_profit_loss 回傳的最小替身"""

    def __init__(self, code, pnl, date="2026-08-14"):
        self.code = code
        self.pnl = pnl
        self.date = date


class TestRealizedPnl(unittest.TestCase):
    """已實現損益＝券商 list_profit_loss 的 pnl 合計（已含手續費與證交稅）。
    自己用成交價減成本價推算會少扣稅費，記進 App 的數字就會偏高。"""

    def test_多筆合計(self):
        self.assertEqual(rr.realized_pnl([Row("0050", 193.0), Row("2330", 807.0)]),
                         1000.0)

    def test_無成交為零(self):
        self.assertEqual(rr.realized_pnl([]), 0.0)

    def test_含虧損筆數要相抵(self):
        """同日一賺一賠，記入的是淨額而非只挑賺的那筆"""
        self.assertEqual(rr.realized_pnl([Row("0050", 500.0), Row("0052", -300.0)]),
                         200.0)


class TestShouldRecord(unittest.TestCase):
    """App 紀律：只記獲利日，虧損由本金減少反映（使用者要求，2026-08-11 起）。"""

    def test_獲利要記(self):
        self.assertTrue(rr.should_record(193.0))

    def test_虧損不記(self):
        self.assertFalse(rr.should_record(-500.0))

    def test_零不記(self):
        """只買不賣的日子沒有已實現損益，不是獲利日"""
        self.assertFalse(rr.should_record(0.0))

    def test_不足一元不記(self):
        """ARK 欄位吃整數，四捨五入後為 0 就沒有東西可記"""
        self.assertFalse(rr.should_record(0.4))


class TestExitCode(unittest.TestCase):
    """只有「該記卻沒記成」才算失敗。

    虧損日與無成交日回報失敗的話，settle 會天天誤報「部分失敗」，
    真正的故障就被雜訊淹沒了。
    """

    def test_該記且寫入成功(self):
        self.assertEqual(rr.exit_code(should=True, written=True), 0)

    def test_該記但寫入失敗(self):
        self.assertEqual(rr.exit_code(should=True, written=False), 1)

    def test_不需記錄不算失敗(self):
        self.assertEqual(rr.exit_code(should=False, written=False), 0)


class TestRowsForDate(unittest.TestCase):
    """只取當日的成交。list_profit_loss 給了日期區間就該只有當天，
    但補記（--date 指定過去某日）時區間仍可能夾帶鄰近日期。"""

    ROWS = [Row("0050", 193.0, "2026-08-14"), Row("2330", 500.0, "2026-08-13")]

    def test_濾出指定日(self):
        got = rr.rows_for_date(self.ROWS, "2026-08-14")
        self.assertEqual([r.code for r in got], ["0050"])

    def test_日期為_date_物件也能比對(self):
        """shioaji 有時回 datetime.date 而非字串"""
        import datetime as dt
        import types
        rows = [types.SimpleNamespace(code="0050", pnl=193.0,
                                      date=dt.date(2026, 8, 14))]
        self.assertEqual(len(rr.rows_for_date(rows, "2026-08-14")), 1)


if __name__ == "__main__":
    unittest.main()
