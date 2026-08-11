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

    def test_衛星軌只減到配額不清倉(self):
        """2330 是刻意保留的部位，只降到門檻——與主軌的歸零重建不同"""
        o = next(x for x in self.plan() if x["code"] == "2330")
        self.assertEqual(o["qty"], 30)
        self.assertEqual(o["action"], "sell")
        self.assertEqual(o["track"], "satellite")

    def test_繼承軌不動即使超額(self):
        """2308 佔 54,450 遠超配額，但虧損中且屬繼承軌——ARK 紀律不准賣"""
        codes = [o["code"] for o in self.plan()]
        self.assertNotIn("2308", codes)

    def test_衛星軌未超額時仍清主軌(self):
        pos = {**self.POSITIONS, "2330": {**self.POSITIONS["2330"], "qty": 20}}
        self.assertEqual([o["code"] for o in self.plan(pos)], ["0050"])

    def test_限價區間以現價為中心(self):
        o = next(x for x in self.plan() if x["code"] == "2330")
        self.assertLess(o["limit_low"], 2380.0)
        self.assertGreater(o["limit_high"], 2380.0)


class TestCleanup(unittest.TestCase):
    """接手的組合裡有一堆幾百元的零碎部位。它們小到不影響組合，卻會佔滿主軌
    檔數（上限只有 1 檔）而讓系統永遠買不了東西。實測手續費只要 1–2.5 元，
    清掉的代價可以忽略——所以主軌歸零重建。"""

    POSITIONS = {
        "2330": {"qty": 50, "avg_price": 2062.38, "last_price": 2380.0, "pnl": 15881.0},
        "2308": {"qty": 30, "avg_price": 1822.57, "last_price": 1815.0, "pnl": -227.0},
        "0050": {"qty": 17, "avg_price": 96.76, "last_price": 104.25, "pnl": 128.0},
        "0052": {"qty": 7, "avg_price": 56.43, "last_price": 60.95, "pnl": 32.0},
        "0053": {"qty": 2, "avg_price": 245.0, "last_price": 237.0, "pnl": -16.0},
        "00911": {"qty": 15, "avg_price": 55.47, "last_price": 55.2, "pnl": -4.0},
    }
    ASSIGNED = {"2330": "satellite", "2308": "inherited"}

    def plan(self):
        return phase0.build_plan(self.POSITIONS, self.ASSIGNED, 48894.0)

    def by_code(self):
        return {o["code"]: o for o in self.plan()}

    def test_獲利的零碎部位全數清掉(self):
        p = self.by_code()
        self.assertEqual(p["0050"]["qty"], 17)
        self.assertEqual(p["0052"]["qty"], 7)

    def test_虧損的不賣(self):
        """ARK 硬規則：獲利才調節。虧損賣出侵蝕本金，紀律不做"""
        p = self.by_code()
        self.assertNotIn("0053", p)
        self.assertNotIn("00911", p)

    def test_繼承軌不碰(self):
        self.assertNotIn("2308", self.by_code())

    def test_衛星軌走配額邏輯而非全清(self):
        """2330 是刻意保留的衛星軌部位，只降到配額內，不是清倉"""
        self.assertEqual(self.by_code()["2330"]["qty"], 30)

    def test_清倉單標記為主軌(self):
        self.assertEqual(self.by_code()["0050"]["track"], "core")

    def test_指定保留的標的不清掉(self):
        """0050 就是主軌要買的標的，手上已有的股數是起點——賣掉再買回是白繞，
        還多付一次來回成本"""
        plan = phase0.build_plan(self.POSITIONS, self.ASSIGNED, 48894.0, keep={"0050"})
        codes = [o["code"] for o in plan]
        self.assertNotIn("0050", codes)
        self.assertIn("0052", codes)      # 其餘照清
        self.assertIn("2330", codes)      # 衛星軌照減


if __name__ == "__main__":
    unittest.main()
