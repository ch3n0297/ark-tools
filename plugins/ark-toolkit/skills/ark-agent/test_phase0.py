"""開局整理的純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import unittest

import phase0


class TestSharesToReduce(unittest.TestCase):
    """把超額的衛星軌部位降回配額內。配額本身不因這筆賣出而改變——
    賣掉的市值變成現金，總資源幾乎不動（只少了稅費）。"""

    def test_實際情境_2330(self):
        """50 股 @2380 = 119,000，配額 48,894 → 留 20 股（47,600），賣 30 股"""
        self.assertEqual(phase0.shares_to_reduce(50, 2380.0, 48894.0), 30)

    def test_未超額不賣(self):
        self.assertEqual(phase0.shares_to_reduce(20, 2380.0, 48894.0), 0)

    def test_恰好等於配額不賣(self):
        self.assertEqual(phase0.shares_to_reduce(20, 2444.7, 48894.0), 0)

    def test_留倉數向下取整不得超額(self):
        """21 股 = 50,004 > 48,894，所以只能留 20 股——取整方向錯就會留下超額部位"""
        self.assertEqual(phase0.shares_to_reduce(50, 2380.0, 50004.0), 29)

    def test_配額不足一股時全賣(self):
        self.assertEqual(phase0.shares_to_reduce(50, 2380.0, 1000.0), 50)

    def test_價格為零不除以零(self):
        self.assertEqual(phase0.shares_to_reduce(50, 0.0, 48894.0), 0)


class TestBuildPlan(unittest.TestCase):
    POSITIONS = {
        "2330": {"qty": 50, "avg_price": 2062.38, "last_price": 2380.0, "pnl": 15881.0},
        "2308": {"qty": 30, "avg_price": 1822.57, "last_price": 1815.0, "pnl": -227.0},
        "0050": {"qty": 17, "avg_price": 96.76, "last_price": 104.25, "pnl": 128.0},
    }
    ASSIGNED = {"2330": "satellite", "2308": "inherited"}

    def plan(self, positions=None, quota=48894.0):
        return phase0.build_plan(positions or self.POSITIONS, self.ASSIGNED, quota)

    def test_只針對衛星軌超額部位(self):
        p = self.plan()
        self.assertEqual([o["code"] for o in p], ["2330"])
        self.assertEqual(p[0]["qty"], 30)
        self.assertEqual(p[0]["action"], "sell")
        self.assertEqual(p[0]["track"], "satellite")

    def test_繼承軌不動即使超額(self):
        """2308 佔 54,450 遠超配額，但虧損中且屬繼承軌——ARK 紀律不准賣"""
        codes = [o["code"] for o in self.plan()]
        self.assertNotIn("2308", codes)

    def test_主軌不動(self):
        self.assertNotIn("0050", [o["code"] for o in self.plan()])

    def test_未超額時計畫為空(self):
        pos = {**self.POSITIONS, "2330": {**self.POSITIONS["2330"], "qty": 20}}
        self.assertEqual(self.plan(pos), [])

    def test_限價區間以現價為中心(self):
        o = self.plan()[0]
        self.assertLess(o["limit_low"], 2380.0)
        self.assertGreater(o["limit_high"], 2380.0)


if __name__ == "__main__":
    unittest.main()
