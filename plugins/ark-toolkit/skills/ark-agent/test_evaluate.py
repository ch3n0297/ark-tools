"""ark-agent 評估純邏輯測試（不需要 Shioaji，任何平台可跑）"""
import unittest

import evaluate

# 12 個交易日（含兩個週末缺口），模擬 0050 的實際日K 日期序列
DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
         "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
         "2026-08-17", "2026-08-18"]


def bars(closes):
    return [{"date": d, "close": c, "open": c, "high": c, "low": c, "volume": 1}
            for d, c in zip(DATES, closes)]


BENCH = bars([50 + 0.5 * i for i in range(12)])            # 0050 緩漲
RISING = bars([100.0 + i for i in range(12)])              # 標的漲
FALLING = bars([1650.0 - 10 * i for i in range(12)])       # 標的跌
CAL = DATES


def daily(bars_list):
    return {b["date"]: b for b in bars_list}


class TestTradingDays(unittest.TestCase):
    def test_交易日曆取自基準日K(self):
        self.assertEqual(evaluate.trading_days(BENCH), DATES)


class TestForwardReturn(unittest.TestCase):
    def test_T加N跨週末假日(self):
        """T+5 是日曆上往後數 5 格，不是日曆天數"""
        ret, target, status = evaluate.forward_return(
            daily(RISING), "2026-08-03", 100.0, 5, CAL)
        self.assertEqual(target, "2026-08-10")
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(ret, 0.05)

    def test_資料未到期標pending不丟棄(self):
        ret, _t, status = evaluate.forward_return(
            daily(RISING), "2026-08-03", 100.0, 60, CAL)
        self.assertIsNone(ret)
        self.assertEqual(status, "pending")

    def test_停牌用最後可用bar並標記(self):
        d = daily(RISING)
        del d["2026-08-10"]                                # 目標日停牌
        ret, target, status = evaluate.forward_return(d, "2026-08-03", 100.0, 5, CAL)
        self.assertEqual(status, "insufficient_bars")
        self.assertEqual(target, "2026-08-07")             # 退用視窗內最後一根
        self.assertAlmostEqual(ret, 0.04)

    def test_視窗內完全無bar為unevaluable(self):
        ret, _t, status = evaluate.forward_return({}, "2026-08-03", 100.0, 5, CAL)
        self.assertIsNone(ret)
        self.assertEqual(status, "unevaluable")


class TestHorizonResult(unittest.TestCase):
    def buy(self, code="2330"):
        return {"action": "buy", "code": code, "qty": 100}

    def sell(self, code="2330"):
        return {"action": "sell", "code": code, "qty": 100}

    def test_買進前瞻報酬方向(self):
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, [])
        self.assertAlmostEqual(r["return_adj"], 0.05)

    def test_賣出避開報酬賣後跌為正(self):
        r = evaluate.horizon_result(self.sell("2308"), "2026-08-03", 1650.0, 5,
                                    daily(FALLING), daily(BENCH), CAL, [])
        self.assertGreater(r["return_adj"], 0)             # 賣掉之後跌了 → 避開成功

    def test_超額報酬同視窗對齊0050(self):
        # 標的 +5%、0050 同視窗 (52.5/50)−1 = +5% → 超額 0
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, [])
        self.assertAlmostEqual(r["benchmark"], 0.05)
        self.assertAlmostEqual(r["excess"], 0.0)

    def test_視窗含除息日標記受影響(self):
        divs = [{"code": "2330", "ex_date": "2026-08-06", "cash": 2.0}]
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, divs)
        self.assertTrue(r["dividend_affected"])

    def test_有現金股利時校正報酬(self):
        divs = [{"code": "2330", "ex_date": "2026-08-06", "cash": 2.0}]
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, divs)
        self.assertAlmostEqual(r["return_raw"], 0.05)
        self.assertAlmostEqual(r["return_adj"], 0.07)      # + 2/100

    def test_基準同受除息校正(self):
        divs = [{"code": "0050", "ex_date": "2026-08-06", "cash": 1.0}]
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, divs)
        self.assertAlmostEqual(r["benchmark"], 0.05 + 1.0 / 50.0)

    def test_除息日在視窗外不標記(self):
        divs = [{"code": "2330", "ex_date": "2026-08-14", "cash": 2.0}]
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, divs)
        self.assertFalse(r["dividend_affected"])

    def test_股票股利只標記不還原(self):
        divs = [{"code": "2330", "ex_date": "2026-08-06", "cash": 0.0,
                 "stock_ratio": 0.1}]
        r = evaluate.horizon_result(self.buy(), "2026-08-03", 100.0, 5,
                                    daily(RISING), daily(BENCH), CAL, divs)
        self.assertTrue(r["stock_dividend_unadjusted"])


class TestExecPrice(unittest.TestCase):
    def test_無成交回退決策日收盤並標記(self):
        order = {"action": "buy", "code": "2330", "qty": 10}
        price, status = evaluate.exec_price_for(
            order, "2026-08-03", None, {"2330": daily(RISING)}, CAL)
        self.assertEqual(price, 100.0)
        self.assertEqual(status, "no_fill")

    def test_有成交用成交價(self):
        order = {"action": "buy", "code": "2330", "qty": 10}
        price, status = evaluate.exec_price_for(
            order, "2026-08-03", {"price": 100.5}, {"2330": daily(RISING)}, CAL)
        self.assertEqual((price, status), (100.5, "filled"))


PK = {
    "date": "2026-08-03",
    "hash": "sha256:abc",
    "ark": {"holdings": {"2308": {"suggest_qty": 40}}},
    "account": {"positions": {"2330": {"qty": 50, "avg_price": 2062.38},
                              "2308": {"qty": 30, "avg_price": 1822.57}}},
    "market": {"quotes": {"2308": {"close": 1650.0}}},
    "discipline": {"max_names": 2, "adjust_required_before_buy": True,
                   "adjust_amount": 45882.0, "sellable": ["2308", "2330"],
                   "sell_priority": ["2308"], "buy_candidates": ["00876"]},
    "news_scope": {"codes": ["2308", "2330", "00876", "0050"]},
}
DECISION = {"type": "decision", "date": "2026-08-03", "packet_hash": "sha256:abc",
            "no_trade": False,
            "orders": [{"action": "sell", "code": "2308", "qty": 30,
                        "limit_low": 1600.0, "limit_high": 1700.0}]}
FILL = {"type": "fill", "date": "2026-08-04", "decision_ref": "2026-08-03",
        "fills": [{"code": "2308", "action": "sell", "qty": 30, "price": 1650.0,
                   "source": "profit_loss", "slippage": 0.0}],
        "unfilled": []}


class TestAdherence(unittest.TestCase):
    def test_覆蓋率封頂於一(self):
        a = evaluate.adherence(DECISION, PK, FILL)
        self.assertEqual(a["coverage"], 1.0)               # 49,500/45,882 → 封頂

    def test_紀律分不適用項不計分母(self):
        """沒有買單 → 「調節先行」「買進全在價值區」不適用，不出現在分項裡"""
        a = evaluate.adherence(DECISION, PK, FILL)
        self.assertNotIn("調節先行", a["components"])
        self.assertNotIn("買進全在價值區", a["components"])
        self.assertEqual(a["score"], 1.0)

    def test_股數偏離以App建議為基準(self):
        a = evaluate.adherence(DECISION, PK, FILL)
        self.assertAlmostEqual(a["qty_deviation"], abs(30 - 40) / 40)

    def test_修訂決策扣分(self):
        amended = {**DECISION, "amended": True}
        a = evaluate.adherence(amended, PK, FILL)
        self.assertFalse(a["components"]["非修訂決策"])
        self.assertLess(a["score"], 1.0)

    def test_衛星軌委託不計入ARK紀律(self):
        """衛星軌本來就不受 ARK 紀律約束，把它算進遵循度等於自己扣自己分"""
        mixed = {**DECISION, "orders": DECISION["orders"] + [
            {"action": "sell", "code": "2330", "qty": 20, "track": "satellite",
             "limit_low": 2370.0, "limit_high": 2390.0}]}
        self.assertEqual(evaluate.adherence(mixed, PK, FILL)["score"], 1.0)

    def test_衛星軌委託不進ARK實驗的報酬統計(self):
        """實驗量的是『ARK 判斷準不準』，摻進我自己的量化單就不是那個問題了"""
        self.assertEqual(evaluate.core_orders(
            {"orders": [{"action": "buy", "code": "0050", "qty": 1},
                        {"action": "buy", "code": "2330", "qty": 1,
                         "track": "satellite"}]}),
            [{"action": "buy", "code": "0050", "qty": 1}])

    def test_未標軌道者視為主軌(self):
        self.assertEqual(len(evaluate.core_orders(DECISION)), 1)


class TestEvaluateAll(unittest.TestCase):
    def report(self):
        entries = [DECISION, FILL,
                   {"type": "missed", "date": "2026-08-04", "reason": "未做決策"}]
        prices = {"2308": FALLING, "0050": BENCH}
        return evaluate.evaluate_all(entries, {"2026-08-03": PK}, prices, BENCH, [])

    def test_勝率計算(self):
        r = self.report()
        self.assertEqual(r["horizons"][5]["evaluated"], 1)
        self.assertEqual(r["horizons"][5]["win_rate"], 1.0)   # 賣後跌 → 勝

    def test_未到期horizon列pending(self):
        r = self.report()
        self.assertEqual(r["horizons"][60]["pending"], 1)
        self.assertIsNone(r["horizons"][60]["win_rate"])

    def test_缺席日計入紀律報告(self):
        self.assertEqual(self.report()["missed"], 1)

    def test_評估輸出不含顯著性宣稱用語(self):
        """統計誠實條款：樣本小、視窗重疊，輸出不得使用統計檢定的宣稱用語"""
        text = evaluate.render(self.report())
        for word in ("顯著", "p 值", "p<", "信賴區間", "統計檢定"):
            self.assertNotIn(word, text)
        self.assertIn("描述性統計", text)


class TestDividendHonesty(unittest.TestCase):
    """kbars 給的是未還原原始價（實證：0050 於 2026-07-21 除息 0.6 元，而
    2026-07-01 的快取收盤仍是除息前的 109.35）。沒有除權息資料時 `adj` 其實
    等於 `raw`，此時宣稱「已含現金股利校正」會讓跨越除息日的低估數字看起來
    可信——那是靜默失真，比明顯的錯誤更難發現。"""

    def test_無資料時不得宣稱已校正(self):
        note = evaluate.dividend_note(0)
        self.assertNotIn("已含", note)
        self.assertIn("未", note)
        self.assertIn("低估", note)

    def test_有資料時標明筆數(self):
        self.assertIn("12", evaluate.dividend_note(12))

    def test_報告帶出除權息資料筆數(self):
        """render 要據此決定講哪一句，report 就得先知道有沒有資料"""
        self.assertEqual(evaluate.evaluate_all([], {}, {}, [], [])["dividends_loaded"],
                         0)
        divs = [{"code": "0050", "ex_date": "2026-07-21", "cash": 0.6}]
        self.assertEqual(evaluate.evaluate_all([], {}, {}, [], divs)["dividends_loaded"],
                         1)

    def test_render_無資料時出現警告(self):
        report = evaluate.evaluate_all([], {}, {}, [], [])
        self.assertIn("未實際校正", evaluate.render(report))


if __name__ == "__main__":
    unittest.main()
