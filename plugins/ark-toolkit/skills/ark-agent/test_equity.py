"""淨值曲線與熔斷判定純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import os
import tempfile
import unittest

import equity


def PT(date, total, satellite=0.0):
    return {"date": date, "total": total, "stock": total, "cash": 0.0,
            "satellite": satellite}


RISING = [PT("2026-08-10", 194613.0), PT("2026-08-11", 198000.0),
          PT("2026-08-12", 202000.0)]
FALLEN = RISING + [PT("2026-08-13", 190000.0)]


class TestMakePoint(unittest.TestCase):
    def test_總資源為持股加現金(self):
        p = equity.make_point("2026-08-10", stock_value=179940.0, cash=14673.0,
                              satellite_value=47600.0)
        self.assertEqual(p["total"], 194613.0)
        self.assertEqual(p["satellite"], 47600.0)
        self.assertEqual(p["date"], "2026-08-10")


class TestTradableCash(unittest.TestCase):
    def test_未入帳的應收付要計入(self):
        """2026-08-12 實測：大額賣出款 T+2 才入帳，只看已交割餘額會造出假回撤"""
        self.assertEqual(equity.tradable_cash(15633.0, 79674.0, 0.0), 95307.0)

    def test_交割日不重複計算今日交割款(self):
        """2026-08-13 實測回歸：79,674 已入帳成 balance，t 欄那筆同款不得再加，
        否則淨值灌成 278,515，幽靈峰值永久鎖死熔斷"""
        self.assertEqual(equity.tradable_cash(95307.0, -1051.0, 0.0), 94256.0)

    def test_應收付為負時扣減(self):
        self.assertEqual(equity.tradable_cash(94283.0, -997.0, 0.0), 93286.0)

    def test_無應收付時等於餘額(self):
        self.assertEqual(equity.tradable_cash(94283.0, 0.0, 0.0), 94283.0)


class TestPeak(unittest.TestCase):
    def test_取歷史最大(self):
        self.assertEqual(equity.peak(FALLEN), 202000.0)

    def test_無資料回None(self):
        self.assertIsNone(equity.peak([]))

    def test_可指定欄位(self):
        pts = [PT("2026-08-10", 100.0, satellite=50.0),
               PT("2026-08-11", 90.0, satellite=60.0)]
        self.assertEqual(equity.peak(pts, key="satellite"), 60.0)


class TestDrawdown(unittest.TestCase):
    def test_當前相對峰值為負(self):
        self.assertAlmostEqual(equity.drawdown(FALLEN), -0.059406, places=6)

    def test_創新高時為零(self):
        self.assertEqual(equity.drawdown(RISING), 0.0)

    def test_無資料為零(self):
        self.assertEqual(equity.drawdown([]), 0.0)

    def test_單點為零(self):
        self.assertEqual(equity.drawdown([PT("2026-08-10", 194613.0)]), 0.0)

    def test_以最後一筆為當前值(self):
        """同日重跑會 append 第二筆，當前值必須取最後一筆而非最大或平均"""
        pts = FALLEN + [PT("2026-08-13", 185000.0)]
        self.assertAlmostEqual(equity.drawdown(pts), -0.084158, places=6)

    def test_峰值為零時回零不除以零(self):
        self.assertEqual(equity.drawdown([PT("2026-08-10", 0.0)]), 0.0)

    def test_可指定欄位獨立算分軌回撤(self):
        pts = [PT("2026-08-10", 200000.0, satellite=50000.0),
               PT("2026-08-11", 199000.0, satellite=35000.0)]
        self.assertAlmostEqual(equity.drawdown(pts, key="satellite"), -0.3, places=6)


class TestBreakerLevel(unittest.TestCase):
    def test_未達門檻不觸發(self):
        self.assertEqual(equity.breaker_level(-0.05, l1=0.08, l2=0.15), equity.NONE)

    def test_達L1停買(self):
        self.assertEqual(equity.breaker_level(-0.09, l1=0.08, l2=0.15), equity.L1)

    def test_達L2全停(self):
        self.assertEqual(equity.breaker_level(-0.20, l1=0.08, l2=0.15), equity.L2)

    def test_恰好等於門檻即觸發(self):
        """門檻是「到了就停」，不是「超過才停」——邊界上少停一次可能就是全部差別"""
        self.assertEqual(equity.breaker_level(-0.08, l1=0.08, l2=0.15), equity.L1)
        self.assertEqual(equity.breaker_level(-0.15, l1=0.08, l2=0.15), equity.L2)

    def test_L2優先於L1(self):
        self.assertEqual(equity.breaker_level(-0.99, l1=0.08, l2=0.15), equity.L2)

    def test_正回撤視為未觸發(self):
        self.assertEqual(equity.breaker_level(0.0, l1=0.08, l2=0.15), equity.NONE)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "equity.jsonl")

    def test_檔案不存在回空清單(self):
        self.assertEqual(equity.load_points(self.path), [])

    def test_附加後可讀回且保序(self):
        for p in RISING:
            equity.append_point(p, self.path)
        self.assertEqual([p["date"] for p in equity.load_points(self.path)],
                         ["2026-08-10", "2026-08-11", "2026-08-12"])

    def test_壞行略過不讓主流程掛掉(self):
        equity.append_point(RISING[0], self.path)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{壞掉的 json\n")
        equity.append_point(RISING[1], self.path)
        self.assertEqual(len(equity.load_points(self.path)), 2)

    def test_附加會建立目錄(self):
        nested = os.path.join(self.dir, "a", "b", "equity.jsonl")
        equity.append_point(RISING[0], nested)
        self.assertEqual(len(equity.load_points(nested)), 1)


if __name__ == "__main__":
    unittest.main()
