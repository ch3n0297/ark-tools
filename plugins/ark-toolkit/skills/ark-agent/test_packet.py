"""ark-agent 決策包純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import unittest

import packet
from ark import Holding, Layout, LayoutView, Posture


def H(code, qty, price, value, pnl=0.0, tiers=(), sug_qty=None, sug_amt=None):
    return Holding(code=code, qty=qty, price=price, cost=qty * price, value=value,
                   pnl=pnl, roi=0.0, today_pnl=0.0, tiers=tiers,
                   suggest_amount=sug_amt, suggest_qty=sug_qty)


def L(code, tiers, price=100.0, tier_qty=0, risk_qty=0):
    return Layout(code=code, tiers=tiers, price=price, change="▼1(-1%)", nav=price,
                  premium=0.1, tier_qty=tier_qty, tier_amount=tier_qty * price,
                  risk_qty=risk_qty, risk_amount=risk_qty * price)


HOLDINGS = {
    "2330": H("2330", 50, 2062.38, 119000.0, pnl=15881.0, tiers=("價值",),
              sug_qty=35, sug_amt=83300.0),
    "0057": H("0057", 100, 309.05, 30755.0, pnl=-150.0, tiers=("價值", "升溫")),
    "0050": H("0050", 17, 96.76, 1756.0, pnl=111.0, tiers=("價值",),
              sug_qty=13, sug_amt=1343.0),
    "2308": H("2308", 30, 1822.57, 49500.0, pnl=5000.0, tiers=("升溫",),
              sug_qty=40, sug_amt=72900.0),
}
POSTURE = Posture(suggested_ratio=66.5, stock_value=176664.0, cash=20000.0,
                  suggested_value=130782.0, suggested_cash=65882.0, adjust_amount=45882.0)
LAYOUT = LayoutView("0501ETF", {
    "00876": L("00876", ("價值",), tier_qty=3, risk_qty=268),
    "00893": L("00893", ("升溫",), risk_qty=10),
})


class TestComputeMaxNames(unittest.TestCase):
    def test_檔數公式十萬一檔(self):
        self.assertEqual(packet.compute_max_names(176664.0, 20000.0), 1)
        self.assertEqual(packet.compute_max_names(250000.0, 60000.0), 3)

    def test_九十萬以上封頂九檔(self):
        self.assertEqual(packet.compute_max_names(850000.0, 50000.0), 9)
        self.assertEqual(packet.compute_max_names(5000000.0, 0.0), 9)

    def test_資金不足十萬至少一檔(self):
        self.assertEqual(packet.compute_max_names(50000.0, 0.0), 1)


class TestBuildDiscipline(unittest.TestCase):
    def disc(self, posture=POSTURE):
        return packet.build_discipline(posture, HOLDINGS, LAYOUT)

    def test_參考調節為正時標記先調節才可買(self):
        self.assertTrue(self.disc()["adjust_required_before_buy"])
        zero = POSTURE._replace(adjust_amount=0.0)
        self.assertFalse(self.disc(zero)["adjust_required_before_buy"])

    def test_只有獲利持股列入可調節(self):
        """官方語意：獲利才調節（虧損賣出侵蝕本金，App 紀律不做）"""
        self.assertEqual(self.disc()["sellable"], ["0050", "2308", "2330"])
        self.assertNotIn("0057", self.disc()["sellable"])          # 虧損中

    def test_升溫區排在調節優先序前面(self):
        priority = self.disc()["sell_priority"]
        self.assertEqual(priority[0], "2308")                      # 升溫且獲利
        self.assertNotIn("0057", priority)                         # 升溫但虧損
        self.assertIn("2330", priority)                            # 有建議調節且獲利

    def test_布局候選只含價值區(self):
        self.assertEqual(self.disc()["buy_candidates"], ["00876"])
        self.assertNotIn("00893", self.disc()["buy_candidates"])   # 純升溫不買

    def test_無posture時以持倉市值推檔數(self):
        d = packet.build_discipline(None, HOLDINGS, LAYOUT)
        self.assertEqual(d["max_names"], 2)                        # 201,011 // 100,000
        self.assertFalse(d["adjust_required_before_buy"])


class TestNewsScope(unittest.TestCase):
    def test_涵蓋持倉與布局與基準(self):
        codes = packet.news_scope_codes(HOLDINGS, LAYOUT)
        for c in ("2330", "0057", "00876", "00893", "0050"):
            self.assertIn(c, codes)

    def test_排序且不重複(self):
        codes = packet.news_scope_codes(HOLDINGS, LAYOUT)
        self.assertEqual(codes, sorted(set(codes)))

    def test_無layout時仍含基準(self):
        codes = packet.news_scope_codes(HOLDINGS, None)
        self.assertIn("0050", codes)


class TestBuildPacket(unittest.TestCase):
    def build(self):
        return packet.build_packet(
            date="2026-08-10", generated_at="2026-08-10T08:31:02",
            holdings=HOLDINGS, declared=4, posture=POSTURE, layout=LAYOUT,
            positions={"2330": {"qty": 50, "avg_price": 2062.38,
                                "last_price": 2380.0, "pnl": 15881.0}},
            balance=20000.0, settlements={"today": 0.0, "t1": -35000.0, "t2": 0.0},
            quotes={"2330": {"close": 2380.0}},
            recent_daily={"2330": [{"date": "2026-08-08", "close": 2380.0}]},
            sync_ok=True, diff=[])

    def test_build_packet不需AX與API(self):
        pk = self.build()
        self.assertEqual(pk["date"], "2026-08-10")
        self.assertEqual(pk["ark"]["holdings"]["2330"]["suggest_qty"], 35)
        self.assertEqual(pk["ark"]["posture"]["adjust_amount"], 45882.0)
        self.assertEqual(pk["discipline"]["max_names"], 1)
        self.assertIn("0050", pk["news_scope"]["codes"])
        self.assertTrue(pk["hash"].startswith("sha256:"))

    def test_holdings保留成本與今日損益(self):
        """build_snapshot 丟掉的 cost/today_pnl/suggest_amount，決策包必須保留"""
        h = self.build()["ark"]["holdings"]["2330"]
        self.assertIn("cost", h)
        self.assertIn("today_pnl", h)
        self.assertEqual(h["suggest_amount"], 83300.0)

    def test_packet雜湊對內容敏感(self):
        a, b = self.build(), self.build()
        self.assertEqual(a["hash"], b["hash"])                     # 決定性
        b["account"]["balance"] = 99999.0
        self.assertNotEqual(a["hash"], packet.packet_hash(b))

    def test_雜湊不含hash欄自身(self):
        pk = self.build()
        self.assertEqual(packet.packet_hash(pk), pk["hash"])       # 事後重算一致


class TestPositionsDiff(unittest.TestCase):
    def test_一致時為空(self):
        positions = {c: {"qty": h.qty, "avg_price": h.price} for c, h in HOLDINGS.items()}
        self.assertEqual(packet.positions_diff(HOLDINGS, positions), [])

    def test_股數不同列入(self):
        positions = {c: {"qty": h.qty, "avg_price": h.price} for c, h in HOLDINGS.items()}
        positions["2330"]["qty"] = 40
        self.assertEqual(packet.positions_diff(HOLDINGS, positions), ["2330"])

    def test_單邊缺少列入(self):
        positions = {c: {"qty": h.qty, "avg_price": h.price} for c, h in HOLDINGS.items()}
        del positions["0050"]
        positions["9999"] = {"qty": 1, "avg_price": 1.0}
        self.assertEqual(packet.positions_diff(HOLDINGS, positions), ["0050", "9999"])


if __name__ == "__main__":
    unittest.main()
