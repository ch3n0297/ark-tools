"""ark-agent 決策日誌純邏輯測試（不需要 Shioaji，任何平台可跑）"""
import os
import tempfile
import unittest

import journal

PACKET = {
    "date": "2026-08-10",
    "hash": "sha256:abc",
    "account": {"positions": {"2330": {"qty": 50, "avg_price": 2062.38},
                              "2308": {"qty": 30, "avg_price": 1822.57}}},
    "market": {"quotes": {"2330": {"close": 2380.0}, "2308": {"close": 1650.0},
                          "00876": {"close": 88.0}}},
    "discipline": {"max_names": 2, "adjust_required_before_buy": True,
                   "adjust_amount": 45882.0,
                   "sellable": ["2308", "2330"], "sell_priority": ["2308"],
                   "buy_candidates": ["00876"]},
    "news_scope": {"codes": ["00876", "2308", "2330", "0050"]},
}


def sell(code="2308", qty=30, low=1600.0, high=1700.0):
    return {"action": "sell", "code": code, "qty": qty,
            "limit_low": low, "limit_high": high}


def buy(code="00876", qty=100, low=85.0, high=90.0):
    return {"action": "buy", "code": code, "qty": qty,
            "limit_low": low, "limit_high": high}


def decision(orders, date="2026-08-10", packet_hash="sha256:abc"):
    return {"date": date, "packet_hash": packet_hash, "no_trade": not orders,
            "orders": orders, "rationale": "測試"}


class TestLockHash(unittest.TestCase):
    def test_鎖定雜湊排除lock欄(self):
        entry = decision([sell()])
        h = journal.lock_hash(entry)
        entry["lock"] = h
        self.assertEqual(journal.lock_hash(entry), h)       # 事後重算一致

    def test_改動orders後雜湊不同(self):
        a = decision([sell()])
        b = decision([sell(qty=10)])
        self.assertNotEqual(journal.lock_hash(a), journal.lock_hash(b))


class TestValidateOrders(unittest.TestCase):
    def test_合規單無違規(self):
        self.assertEqual(journal.validate_orders([sell()], PACKET), [])

    def test_賣出虧損中標的被拒絕(self):
        v = journal.validate_orders([sell(code="0057")], PACKET)
        self.assertTrue(any("0057" in x for x in v))

    def test_有參考調節時買單需賣單覆蓋(self):
        v = journal.validate_orders([buy()], PACKET)          # 只買不賣
        self.assertTrue(any("參考調節" in x for x in v))
        ok = journal.validate_orders([sell(), buy()], PACKET)  # 賣 30×1650=49,500 ≥ 45,882
        self.assertEqual(ok, [])

    def test_賣單估值不足仍算未覆蓋(self):
        v = journal.validate_orders([sell(qty=10), buy()], PACKET)   # 16,500 < 45,882
        self.assertTrue(any("參考調節" in x for x in v))

    def test_買進非價值區被拒絕(self):
        v = journal.validate_orders([sell(), buy(code="2330")], PACKET)
        self.assertTrue(any("價值區" in x for x in v))

    def test_超過檔數上限被拒絕(self):
        pk = {**PACKET, "discipline": {**PACKET["discipline"], "max_names": 2,
                                       "buy_candidates": ["00876", "0056"],
                                       "adjust_required_before_buy": False}}
        v = journal.validate_orders([buy("00876"), buy("0056", low=30.0, high=35.0)], pk)
        self.assertTrue(any("檔數" in x for x in v))          # 2 持股 + 2 新倉 > 2

    def test_既有超限不歸咎於本日決策(self):
        """小資組合可能一開始就超過檔數公式；只擋「讓檔數變得更糟」的單"""
        pk = {**PACKET, "discipline": {**PACKET["discipline"], "max_names": 1}}
        self.assertEqual(journal.validate_orders([], pk), [])           # 空單合法
        self.assertEqual(journal.validate_orders([sell(qty=30), buy()], pk), [])  # 換股不更糟
        v = journal.validate_orders(
            [sell(qty=30), buy(), buy("0056", low=30.0, high=35.0)],
            {**pk, "discipline": {**pk["discipline"], "buy_candidates": ["00876", "0056"],
                                  "adjust_required_before_buy": False}})
        self.assertTrue(any("檔數" in x for x in v))                    # 變 3 檔就擋

    def test_賣光的檔不佔檔數(self):
        pk = {**PACKET, "discipline": {**PACKET["discipline"],
                                       "adjust_required_before_buy": False}}
        v = journal.validate_orders([sell(qty=30), buy()], pk)   # 2308 賣光後買 00876
        self.assertEqual(v, [])

    def test_代號不在資訊邊界內被拒絕(self):
        v = journal.validate_orders([sell(code="9999")], PACKET)
        self.assertTrue(any("news_scope" in x for x in v))

    def test_no_trade空單合法(self):
        self.assertEqual(journal.validate_orders([], PACKET), [])


ENVELOPE = {
    "packet_hash": "sha256:abc", "can_buy": True, "can_sell": True,
    "limits": {"per_order_cap": 200000.0, "daily_buy_cap": 400000.0,
               "daily_turnover_cap": 600000.0, "min_trade_value": 1000.0},
    "tracks": {
        "core": {"codes": ["2308"], "value": 49500.0},
        "satellite": {"codes": ["2330"], "allowlist": ["2330", "00631L"],
                      "value": 47600.0, "quota": 52755.5, "remaining": 5155.5,
                      "halted": False},
        "inherited": {"codes": [], "value": 0.0},
        "frozen": {"codes": [], "value": 0.0},
    },
}
# 2330 虧損中（不在 sellable），且不必先調節——用來單獨檢驗軌道分流
LOSS_PACKET = {**PACKET, "discipline": {**PACKET["discipline"],
                                        "sellable": ["2308"],
                                        "adjust_required_before_buy": False}}


def sat(order):
    return {**order, "track": "satellite"}


class TestTrackRouting(unittest.TestCase):
    def validate(self, orders, packet=None, envelope=ENVELOPE):
        return journal.validate_orders(orders, packet or LOSS_PACKET, envelope)

    def test_衛星軌可賣虧損中的部位(self):
        """停損是衛星軌存在的理由——主軌的『獲利才賣』不該套到它身上"""
        self.assertEqual(self.validate([sat(sell(code="2330", qty=20,
                                                 low=2370.0, high=2390.0))]), [])

    def test_主軌賣虧損中部位仍被拒(self):
        v = self.validate([sell(code="2330", qty=20, low=2370.0, high=2390.0)])
        self.assertTrue(any("2330" in x for x in v))

    def test_衛星軌買進不受價值區限制(self):
        self.assertEqual(self.validate([sat(buy(code="2330", qty=1,
                                                low=2370.0, high=2390.0))]), [])

    def test_衛星軌買進超過配額被拒(self):
        v = self.validate([sat(buy(code="2330", qty=5, low=2370.0, high=2390.0))])
        self.assertTrue(any("配額" in x for x in v))     # 5×2380=11,900 > 5,155.5

    def test_已超額時仍可賣出減碼(self):
        """remaining 為負代表已超過配額，此時賣出正是唯一能修正的動作。
        用 net > remaining 判斷會把淨賣出也擋掉——開局整理將永遠無法通過。"""
        over = {**ENVELOPE, "tracks": {**ENVELOPE["tracks"],
                "satellite": {**ENVELOPE["tracks"]["satellite"],
                              "value": 119000.0, "remaining": -70106.0}}}
        orders = [sat(sell(code="2330", qty=8, low=2330.0, high=2430.0))] * 3
        self.assertEqual(self.validate(orders, envelope=over), [])

    def test_已超額時不得再買進(self):
        over = {**ENVELOPE, "tracks": {**ENVELOPE["tracks"],
                "satellite": {**ENVELOPE["tracks"]["satellite"],
                              "value": 119000.0, "remaining": -70106.0}}}
        v = self.validate([sat(buy(code="2330", qty=1, low=2370.0, high=2390.0))],
                          envelope=over)
        self.assertTrue(any("配額" in x for x in v))

    def test_買賣互抵後淨賣出可放行(self):
        """同日既買又賣時看淨額：淨賣出不增加曝險，不該被配額擋"""
        over = {**ENVELOPE, "tracks": {**ENVELOPE["tracks"],
                "satellite": {**ENVELOPE["tracks"]["satellite"],
                              "value": 119000.0, "remaining": -70106.0}}}
        orders = [sat(sell(code="2330", qty=8, low=2330.0, high=2430.0)),
                  sat(buy(code="2330", qty=2, low=2370.0, high=2390.0))]
        self.assertEqual(self.validate(orders, envelope=over), [])

    def test_衛星軌標的須在白名單內(self):
        """開新衛星標的要有人手動寫進 tracks.json，程式不自己擴充"""
        pk = {**LOSS_PACKET,
              "news_scope": {"codes": LOSS_PACKET["news_scope"]["codes"] + ["2317"]},
              "market": {"quotes": {**LOSS_PACKET["market"]["quotes"],
                                    "2317": {"close": 200.0}}}}
        v = self.validate([sat(buy(code="2317", qty=10, low=195.0, high=205.0))], pk)
        self.assertTrue(any("白名單" in x for x in v))

    def test_衛星軌部位不計入主軌檔數(self):
        """2330 歸衛星軌後，主軌只剩 2308 一檔，還買得下一檔"""
        pk = {**LOSS_PACKET, "discipline": {**LOSS_PACKET["discipline"],
                                            "max_names": 2}}
        self.assertEqual(self.validate([buy()], pk), [])
        without_track = journal.validate_orders([buy()], pk)     # 無 envelope
        self.assertTrue(any("檔數" in x for x in without_track))

    def test_缺執行邊界時衛星軌單一律拒絕(self):
        """沒有邊界就沒有配額與白名單可查，此時放行等於無條件繞過紀律"""
        v = journal.validate_orders(
            [sat(sell(code="2330", qty=20, low=2370.0, high=2390.0))], LOSS_PACKET)
        self.assertTrue(any("執行邊界" in x for x in v))

    def test_調節先行對衛星軌買單同樣適用(self):
        """ARK 的參考調節是帳戶級曝險訊號；衛星軌趁機加碼會讓總曝險失控"""
        v = self.validate([sat(buy(code="2330", qty=1, low=2370.0, high=2390.0))],
                          PACKET)
        self.assertTrue(any("參考調節" in x for x in v))

    def test_未標軌道者視為主軌(self):
        self.assertEqual(journal.validate_orders([sell()], PACKET, ENVELOPE), [])


class TestRecordDecision(unittest.TestCase):
    def test_合規決策產生鎖定entry(self):
        entry, v = journal.record_decision(decision([sell()]), PACKET, [])
        self.assertEqual(v, [])
        self.assertEqual(entry["type"], "decision")
        self.assertTrue(entry["lock"].startswith("sha256:"))

    def test_packet_hash不符拒絕(self):
        entry, v = journal.record_decision(
            decision([sell()], packet_hash="sha256:old"), PACKET, [])
        self.assertIsNone(entry)
        self.assertTrue(any("packet_hash" in x for x in v))

    def test_違規未覆寫拒絕寫入(self):
        entry, v = journal.record_decision(decision([sell("0057")]), PACKET, [])
        self.assertIsNone(entry)
        self.assertTrue(v)

    def test_override寫入並記violations(self):
        entry, v = journal.record_decision(decision([sell("0057")]), PACKET, [],
                                           override=True)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["violations"], v)

    def test_同日第二筆決策標記為修訂(self):
        first, _ = journal.record_decision(decision([sell()]), PACKET, [])
        second, _ = journal.record_decision(decision([]), PACKET, [first])
        self.assertNotIn("amended", first)
        self.assertTrue(second["amended"])

    def test_evaluate只認第一筆(self):
        first, _ = journal.record_decision(decision([sell()]), PACKET, [])
        second, _ = journal.record_decision(decision([]), PACKET, [first])
        picked = journal.first_decision([first, second], "2026-08-10")
        self.assertIs(picked, first)


class TestJournalIO(unittest.TestCase):
    def test_壞行略過不中斷(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            journal.append_entry({"type": "decision", "date": "2026-08-10"}, path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{壞掉的 json\n")
            journal.append_entry({"type": "fill", "date": "2026-08-11"}, path)
            entries = journal.load_entries(path)
            self.assertEqual([e["type"] for e in entries], ["decision", "fill"])


class TestInferBuyPrice(unittest.TestCase):
    def test_均價反推買進成交價(self):
        # 原 50 股 @2062.38，買 10 股 @2380 → 新均價 (50*2062.38+10*2380)/60
        new_avg = (50 * 2062.38 + 10 * 2380.0) / 60
        got = journal.infer_buy_price(50, 2062.38, 60, new_avg)
        self.assertAlmostEqual(got, 2380.0, places=4)

    def test_原持股為零時即為新均價(self):
        self.assertAlmostEqual(journal.infer_buy_price(0, 0.0, 100, 88.0), 88.0)


class TestMatchFills(unittest.TestCase):
    PL = [{"code": "2308", "quantity": 20, "price": 1660.0},
          {"code": "2308", "quantity": 10, "price": 1650.0}]

    def test_成交對回配對買賣單(self):
        before = {"2308": {"qty": 30, "avg_price": 1822.57}}
        after = {"00876": {"qty": 100, "avg_price": 88.0}}
        fills, unfilled = journal.match_fills(
            [sell(), buy()], self.PL, before, after)
        by_code = {f["code"]: f for f in fills}
        self.assertEqual(by_code["2308"]["qty"], 30)
        self.assertAlmostEqual(by_code["2308"]["price"], (20 * 1660 + 10 * 1650) / 30,
                               places=3)
        self.assertEqual(by_code["2308"]["source"], "profit_loss")
        self.assertAlmostEqual(by_code["00876"]["price"], 88.0)
        self.assertEqual(by_code["00876"]["source"], "position_diff")
        self.assertEqual(unfilled, [])

    def test_未成交列入unfilled(self):
        fills, unfilled = journal.match_fills([sell(code="2330", qty=10)], [], {}, {})
        self.assertEqual(fills, [])
        self.assertEqual(unfilled[0]["code"], "2330")

    def test_滑價計算(self):
        # sell 限價區間 1600–1700，中點 1650；成交 1656.67 → 滑價 ≈ +0.4%
        fills, _ = journal.match_fills(
            [sell()], self.PL, {"2308": {"qty": 30, "avg_price": 1822.57}}, {})
        self.assertAlmostEqual(fills[0]["slippage"],
                               (fills[0]["price"] - 1650.0) / 1650.0, places=6)


class TestPreviousWeekday(unittest.TestCase):
    def test_跨週末(self):
        self.assertEqual(journal.previous_weekday("2026-08-10"), "2026-08-07")  # 一→五

    def test_平日取前一日(self):
        self.assertEqual(journal.previous_weekday("2026-08-07"), "2026-08-06")


if __name__ == "__main__":
    unittest.main()
