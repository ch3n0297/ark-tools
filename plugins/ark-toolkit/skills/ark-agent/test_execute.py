"""下單層純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）

這個檔案守的是「送出去的委託跟決策說的是同一件事」。單位換算與價格檔位
兩處出錯都不會被上游的紀律驗證攔到——journal 只看股數與代號，看不到
最後送進 API 的那組參數。
"""
import unittest

import execute


class TestSplitShares(unittest.TestCase):
    def test_不足一張走盤中零股(self):
        self.assertEqual(execute.split_shares(500),
                         [(execute.INTRADAY_ODD, 500)])

    def test_整張走整股且數量換算成張(self):
        """Common 的 quantity 單位是張——把 2000 股當 quantity=2000 送出去
        就是 2000 張，兩千倍的委託"""
        self.assertEqual(execute.split_shares(2000), [(execute.COMMON, 2)])

    def test_剛好一張(self):
        self.assertEqual(execute.split_shares(1000), [(execute.COMMON, 1)])

    def test_混合股數拆成兩腿(self):
        self.assertEqual(execute.split_shares(1500),
                         [(execute.COMMON, 1), (execute.INTRADAY_ODD, 500)])

    def test_零股數不產生委託(self):
        self.assertEqual(execute.split_shares(0), [])

    def test_負股數拒絕(self):
        with self.assertRaises(ValueError):
            execute.split_shares(-100)

    def test_零股腿永遠小於一千(self):
        """IntradayOdd 的 quantity 上限是 999，超過會被券商拒單"""
        for shares in (999, 1000, 1001, 4321, 10000):
            for lot, qty in execute.split_shares(shares):
                if lot == execute.INTRADAY_ODD:
                    self.assertLess(qty, 1000)

    def test_拆解後股數守恆(self):
        for shares in (1, 999, 1000, 1001, 1500, 4321, 50000):
            total = sum(qty * (1000 if lot == execute.COMMON else 1)
                        for lot, qty in execute.split_shares(shares))
            self.assertEqual(total, shares)


class TestIsETF(unittest.TestCase):
    def test_雙零開頭視為ETF(self):
        for code in ("0050", "0052", "00635U", "00911", "00631L"):
            self.assertTrue(execute.is_etf(code), code)

    def test_一般個股不是ETF(self):
        for code in ("2330", "2308", "1101", "6505"):
            self.assertFalse(execute.is_etf(code), code)


class TestTickSize(unittest.TestCase):
    def test_ETF兩段檔位(self):
        """實證來源：帳戶內 00635U @ 45.03（0.01 檔）、0050 @ 104.25（0.05 檔）"""
        self.assertEqual(execute.tick_size(45.03, is_etf=True), 0.01)
        self.assertEqual(execute.tick_size(104.25, is_etf=True), 0.05)
        self.assertEqual(execute.tick_size(50.0, is_etf=True), 0.05)

    def test_個股六段檔位(self):
        self.assertEqual(execute.tick_size(9.99, is_etf=False), 0.01)
        self.assertEqual(execute.tick_size(10.0, is_etf=False), 0.05)
        self.assertEqual(execute.tick_size(49.95, is_etf=False), 0.05)
        self.assertEqual(execute.tick_size(50.0, is_etf=False), 0.1)
        self.assertEqual(execute.tick_size(100.0, is_etf=False), 0.5)
        self.assertEqual(execute.tick_size(500.0, is_etf=False), 1.0)
        self.assertEqual(execute.tick_size(1000.0, is_etf=False), 5.0)

    def test_實際持倉價格都落在正確檔位上(self):
        """帳戶現況當回歸樣本：這些是真的成交過的價格，一定合法"""
        for code, price in (("00635U", 45.03), ("0050", 104.25), ("0052", 60.95),
                            ("00911", 55.2), ("2330", 2380.0), ("2308", 1815.0)):
            tick = execute.tick_size(price, execute.is_etf(code))
            self.assertAlmostEqual(round(price / tick) * tick, price, places=4,
                                   msg=f"{code} @ {price} 不在 {tick} 檔位上")


class TestRoundToTick(unittest.TestCase):
    def test_買進向下取整不超付(self):
        self.assertAlmostEqual(
            execute.round_to_tick(104.27, is_etf=True, action="buy"), 104.25)

    def test_賣出向上取整不賤賣(self):
        self.assertAlmostEqual(
            execute.round_to_tick(104.27, is_etf=True, action="sell"), 104.30)

    def test_已在檔位上不變動(self):
        self.assertAlmostEqual(
            execute.round_to_tick(104.25, is_etf=True, action="buy"), 104.25)
        self.assertAlmostEqual(
            execute.round_to_tick(2380.0, is_etf=False, action="sell"), 2380.0)

    def test_個股大額檔位(self):
        self.assertAlmostEqual(
            execute.round_to_tick(2378.0, is_etf=False, action="buy"), 2375.0)
        self.assertAlmostEqual(
            execute.round_to_tick(2378.0, is_etf=False, action="sell"), 2380.0)


class TestLimitPrice(unittest.TestCase):
    LIMITS = {"limit_up": 113.1, "limit_down": 92.6}

    def price(self, action, low, high, limits=None):
        return execute.limit_price(
            {"action": action, "code": "0050", "limit_low": low, "limit_high": high},
            limits or self.LIMITS)

    def test_買進取區間上緣(self):
        """買方願付的最高價才是限價的意義——取下緣等於大概率不成交"""
        self.assertAlmostEqual(self.price("buy", 103.0, 105.0), 105.0)

    def test_賣出取區間下緣(self):
        self.assertAlmostEqual(self.price("sell", 103.0, 105.0), 103.0)

    def test_超過漲停夾回漲停(self):
        self.assertAlmostEqual(self.price("buy", 110.0, 120.0), 113.1)

    def test_低於跌停夾回跌停(self):
        self.assertAlmostEqual(self.price("sell", 80.0, 90.0), 92.6)

    def test_夾回後仍落在檔位上(self):
        p = self.price("buy", 110.0, 120.0)
        self.assertAlmostEqual(round(p / 0.05) * 0.05, p, places=4)

    def test_缺限價區間時拒絕(self):
        """沒有限價就只能下市價，無人值守系統不下市價單"""
        with self.assertRaises(ValueError):
            execute.limit_price({"action": "buy", "code": "0050"}, self.LIMITS)


class TestBuildLegs(unittest.TestCase):
    LIMITS = {"limit_up": 2615.0, "limit_down": 2145.0}

    def test_單筆決策展開成可送出的委託腿(self):
        legs = execute.build_legs(
            {"action": "sell", "code": "2330", "qty": 1500,
             "limit_low": 2370.0, "limit_high": 2390.0}, self.LIMITS)
        self.assertEqual([(x["order_lot"], x["quantity"]) for x in legs],
                         [(execute.COMMON, 1), (execute.INTRADAY_ODD, 500)])
        self.assertTrue(all(x["price"] == 2370.0 for x in legs))
        self.assertTrue(all(x["action"] == "sell" for x in legs))
        self.assertTrue(all(x["code"] == "2330" for x in legs))

    def test_每腿都帶上原始股數供對帳(self):
        legs = execute.build_legs(
            {"action": "buy", "code": "2330", "qty": 1500,
             "limit_low": 2370.0, "limit_high": 2390.0}, self.LIMITS)
        self.assertEqual([x["shares"] for x in legs], [1000, 500])


class TestExecutionGuard(unittest.TestCase):
    ENV = {"can_buy": True, "can_sell": True, "packet_hash": "sha256:abc",
           "limits": {"per_order_cap": 20000.0, "daily_buy_cap": 40000.0,
                      "daily_turnover_cap": 60000.0, "min_trade_value": 3000.0}}
    QUOTES = {"0050": {"close": 104.25}}

    def guard(self, orders, env=None, packet_hash="sha256:abc"):
        return execute.execution_guard(orders, env or self.ENV, self.QUOTES,
                                       packet_hash)

    def test_合規回空(self):
        self.assertEqual(self.guard([{"action": "buy", "code": "0050", "qty": 50}]), [])

    def test_不可買時擋下買單(self):
        env = {**self.ENV, "can_buy": False}
        v = self.guard([{"action": "buy", "code": "0050", "qty": 50}], env)
        self.assertTrue(any("買" in x for x in v))

    def test_不可賣時擋下賣單(self):
        env = {**self.ENV, "can_sell": False}
        v = self.guard([{"action": "sell", "code": "0050", "qty": 50}], env)
        self.assertTrue(any("賣" in x for x in v))

    def test_邊界不符擋下(self):
        """envelope 與決策必須看的是同一份 packet，否則限額建立在別天的事實上"""
        v = self.guard([{"action": "buy", "code": "0050", "qty": 50}],
                       packet_hash="sha256:zzz")
        self.assertTrue(any("packet" in x for x in v))

    def test_限價偏離現價過大被擋(self):
        """賣單限價 2.1% 低於市價 = 接受最差 −2.1% 的市價單。無人值守下沒人
        會發現這種滑價，實測 LLM 就下過這種單（市價 2380 掛 2330）。"""
        env = {**self.ENV, "limits": {**self.ENV["limits"], "min_trade_value": 100.0}}
        v = execute.execution_guard(
            [{"action": "sell", "code": "0050", "qty": 50,
              "limit_low": 101.0, "limit_high": 110.0}],       # 收盤 104.25
            env, self.QUOTES, "sha256:abc")
        self.assertTrue(any("限價" in x for x in v))

    def test_限價貼近現價放行(self):
        env = {**self.ENV, "limits": {**self.ENV["limits"], "min_trade_value": 100.0}}
        self.assertEqual(execute.execution_guard(
            [{"action": "sell", "code": "0050", "qty": 50,
              "limit_low": 103.5, "limit_high": 105.0}],
            env, self.QUOTES, "sha256:abc"), [])

    def test_買單限價過高同樣被擋(self):
        env = {**self.ENV, "limits": {**self.ENV["limits"], "min_trade_value": 100.0}}
        v = execute.execution_guard(
            [{"action": "buy", "code": "0050", "qty": 50,
              "limit_low": 100.0, "limit_high": 108.0}],       # 高於 104.25 達 3.6%
            env, self.QUOTES, "sha256:abc")
        self.assertTrue(any("限價" in x for x in v))

    def test_無報價時不判限價偏離(self):
        """沒有參考價就無從判斷偏離，這時交給金額上限把關即可"""
        env = {**self.ENV, "limits": {**self.ENV["limits"], "min_trade_value": 100.0}}
        self.assertEqual(execute.execution_guard(
            [{"action": "sell", "code": "9999", "qty": 5,
              "limit_low": 100.0, "limit_high": 110.0}],
            env, self.QUOTES, "sha256:abc"), [])

    def test_金額上限在送出前再查一次(self):
        """縱深防禦：journal 驗紀律、envelope 定邊界，送出前還要再擋一次"""
        v = self.guard([{"action": "buy", "code": "0050", "qty": 300}])   # 31,275
        self.assertTrue(any("單筆" in x for x in v))


if __name__ == "__main__":
    unittest.main()
