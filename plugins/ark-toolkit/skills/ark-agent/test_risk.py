"""風控層純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import tempfile
import unittest

import equity
import risk
import tracks

# 2026-08 的交易日曆（跳過 8/15、8/16 週末）
CALENDAR = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
            "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
            "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]


def P(qty, avg_price, last_price, pnl):
    return {"qty": qty, "avg_price": avg_price, "last_price": last_price, "pnl": pnl}


POSITIONS = {
    "2330": P(20, 2062.38, 2380.0, 6352.4),     # 市值 47,600　成本 41,247.6　roi +15.4%
    "2308": P(30, 1822.57, 1815.0, -227.1),     # 市值 54,450　虧損
    "0050": P(900, 96.76, 104.25, 6741.0),      # 市值 93,825
    "0053": P(2, 245.0, 237.0, -16.0),          # 市值 474　低於門檻
}
ASSIGNED = {"2330": tracks.SATELLITE, "2308": tracks.INHERITED}


def make_packet(suggested_ratio=66.5, stock_value=196349.0, cash=14673.0,
                adjust_amount=45882.0, sync_ok=True, positions=None):
    return {
        "schema": 1, "date": "2026-08-11", "hash": "sha256:x",
        "ark": {
            "holdings": {},
            "posture": {"suggested_ratio": suggested_ratio, "actual_ratio": 93.05,
                        "gap": 26.55, "stock_value": stock_value, "cash": cash,
                        "suggested_value": 0.0, "suggested_cash": 0.0,
                        "adjust_amount": adjust_amount},
            "layout": None,
        },
        "account": {"positions": POSITIONS if positions is None else positions,
                    "balance": cash, "settlements": {}, "sync_ok": sync_ok, "diff": []},
        "market": {"quotes": {c: {"close": p["last_price"]}
                              for c, p in (POSITIONS if positions is None
                                           else positions).items()}},
        "discipline": {"max_names": 2, "adjust_required_before_buy": True,
                       "adjust_amount": adjust_amount, "sellable": [], "sell_priority": [],
                       "buy_candidates": []},
        "news_scope": {"codes": []},
    }


class TestMaxNamesHysteresis(unittest.TestCase):
    def test_首次無既有值直接採用當日(self):
        self.assertEqual(risk.max_names_with_hysteresis(None, [2], days=5), 2)

    def test_連續天數不足維持原值(self):
        self.assertEqual(risk.max_names_with_hysteresis(1, [1, 2, 2, 2], days=5), 1)

    def test_歷史長度不足時即使全部一致也不變更(self):
        """剛開始記錄就讓檔數跳動等於沒有遲滯——必須先湊滿觀察天數"""
        self.assertEqual(risk.max_names_with_hysteresis(1, [2, 2, 2], days=5), 1)

    def test_連續達標才變更(self):
        self.assertEqual(risk.max_names_with_hysteresis(1, [1, 2, 2, 2, 2, 2], days=5), 2)

    def test_期間跳動一次就重新計數(self):
        """資產在 10 萬門檻附近震盪時，跳動的那天必須讓計數歸零"""
        self.assertEqual(risk.max_names_with_hysteresis(1, [2, 2, 1, 2, 2, 2], days=5), 1)

    def test_與既有值相同不變更(self):
        self.assertEqual(risk.max_names_with_hysteresis(2, [2, 2, 2, 2, 2], days=5), 2)

    def test_空歷史維持原值(self):
        self.assertEqual(risk.max_names_with_hysteresis(3, [], days=5), 3)


class TestRatioBandAction(unittest.TestCase):
    def test_實際高於建議超過帶寬要降(self):
        self.assertEqual(risk.ratio_band_action(66.5, 93.0, band_pp=5.0), risk.REDUCE)

    def test_實際低於建議超過帶寬可買(self):
        self.assertEqual(risk.ratio_band_action(66.5, 55.0, band_pp=5.0), risk.BUY)

    def test_帶寬內不動作(self):
        self.assertEqual(risk.ratio_band_action(66.5, 68.0, band_pp=5.0), risk.HOLD)

    def test_恰好等於帶寬仍不動作(self):
        """無動作帶含端點——邊界上為了 0.0 個百分點付一次來回成本不划算"""
        self.assertEqual(risk.ratio_band_action(66.5, 71.5, band_pp=5.0), risk.HOLD)
        self.assertEqual(risk.ratio_band_action(66.5, 61.5, band_pp=5.0), risk.HOLD)


class TestConcentrationBreaches(unittest.TestCase):
    def breaches(self, positions=None, cap=0.35):
        resolved = tracks.resolve(ASSIGNED, positions or POSITIONS, 3000.0)
        return risk.concentration_breaches(resolved, total=211022.0, cap=cap)

    def test_超過上限列出並附超額金額(self):
        b = {x["code"]: x for x in self.breaches(cap=0.35)}
        self.assertIn("0050", b)                                  # 93,825 / 211,022 = 44.5%
        self.assertAlmostEqual(b["0050"]["ratio"], 0.44462, places=4)
        self.assertAlmostEqual(b["0050"]["excess_value"], 19967.3, places=1)

    def test_未超過不列(self):
        self.assertEqual(self.breaches(cap=0.60), [])

    def test_繼承與凍結軌不列(self):
        """2308 佔 25.8% 但虧損中動不了，列出來只會製造做不到的待辦"""
        codes = [x["code"] for x in self.breaches(cap=0.20)]
        self.assertNotIn("2308", codes)
        self.assertNotIn("0053", codes)
        self.assertIn("2330", codes)

    def test_獲利者標為可處置(self):
        b = {x["code"]: x for x in self.breaches(cap=0.20)}
        self.assertTrue(b["2330"]["actionable"])

    def test_虧損者標為不可處置(self):
        pos = {**POSITIONS, "2330": P(20, 2062.38, 2380.0, -500.0)}
        b = {x["code"]: x for x in self.breaches(positions=pos, cap=0.20)}
        self.assertFalse(b["2330"]["actionable"])

    def test_依超額由大到小排序(self):
        b = self.breaches(cap=0.10)
        self.assertEqual([x["code"] for x in b], ["0050", "2330"])


class TestStopLossCandidates(unittest.TestCase):
    def candidates(self, positions, stop=-0.12):
        resolved = tracks.resolve(ASSIGNED, positions, 3000.0)
        return risk.stop_loss_candidates(resolved, positions, stop_pct=stop)

    def test_衛星軌跌破停損列出(self):
        pos = {**POSITIONS, "2330": P(20, 2062.38, 1800.0, -5247.6)}
        c = self.candidates(pos)
        self.assertEqual([x["code"] for x in c], ["2330"])
        self.assertAlmostEqual(c[0]["roi"], -0.12722, places=4)

    def test_恰好等於門檻即觸發(self):
        pos = {**POSITIONS, "2330": P(20, 2000.0, 1760.0, -4800.0)}   # roi = -12.0%
        self.assertEqual([x["code"] for x in self.candidates(pos)], ["2330"])

    def test_未跌破不列(self):
        self.assertEqual(self.candidates(POSITIONS), [])

    def test_主軌虧損不列(self):
        """主軌守 ARK『虧損不賣』，停損只存在於衛星軌"""
        pos = {**POSITIONS, "0050": P(900, 96.76, 60.0, -33084.0)}
        self.assertEqual(self.candidates(pos), [])

    def test_繼承軌虧損不列(self):
        pos = {**POSITIONS, "2308": P(30, 1822.57, 1000.0, -24677.1)}
        self.assertEqual(self.candidates(pos), [])

    def test_成本為零不除以零(self):
        """現價拉高到凍結門檻之上，才真的走到除法那一行"""
        pos = {"2330": P(20, 0.0, 200.0, 2000.0)}
        self.assertEqual(self.candidates(pos), [])


class TestConsecutiveStops(unittest.TestCase):
    def test_由尾端計算連續次數(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True}]
        self.assertEqual(risk.consecutive_stops(exits), 2)

    def test_中間有非停損出場則歸零重算(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": False},
                 {"date": "2026-08-12", "stopped": True}]
        self.assertEqual(risk.consecutive_stops(exits), 1)

    def test_空清單為零(self):
        self.assertEqual(risk.consecutive_stops([]), 0)

    def test_最後一筆非停損為零(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": False}]
        self.assertEqual(risk.consecutive_stops(exits), 0)


class TestTradingDayOffset(unittest.TestCase):
    def test_偏移以交易日計不含週末(self):
        self.assertEqual(risk.trading_day_offset(CALENDAR, "2026-08-13", 3),
                         "2026-08-18")

    def test_日期不在日曆上取其後第一個交易日(self):
        self.assertEqual(risk.trading_day_offset(CALENDAR, "2026-08-15", 1),
                         "2026-08-18")

    def test_超出日曆回None(self):
        self.assertIsNone(risk.trading_day_offset(CALENDAR, "2026-08-27", 10))


class TestSatelliteCooldown(unittest.TestCase):
    def cooldown(self, exits, today, streak=3, days=10):
        return risk.satellite_cooldown(exits, streak_cap=streak, cooldown_days=days,
                                       calendar=CALENDAR, today=today)

    def test_未達連續次數不停(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True}]
        halted, _reason, until = self.cooldown(exits, "2026-08-12")
        self.assertFalse(halted)
        self.assertIsNone(until)

    def test_達連續次數後冷卻期內停止新倉(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True},
                 {"date": "2026-08-12", "stopped": True}]
        halted, reason, until = self.cooldown(exits, "2026-08-13", days=5)
        self.assertTrue(halted)
        self.assertEqual(until, "2026-08-19")
        self.assertIn("連續", reason)

    def test_冷卻期滿解除(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True},
                 {"date": "2026-08-12", "stopped": True}]
        halted, _reason, _until = self.cooldown(exits, "2026-08-19", days=5)
        self.assertFalse(halted)

    def test_空紀錄不停(self):
        self.assertEqual(self.cooldown([], "2026-08-12")[0], False)


class TestOrderCaps(unittest.TestCase):
    LIMITS = {"per_order_cap": 20000.0, "daily_buy_cap": 40000.0,
              "daily_turnover_cap": 60000.0, "min_trade_value": 3000.0}
    QUOTES = {"0050": {"close": 100.0}, "2330": {"close": 2380.0}}

    def caps(self, orders):
        return risk.order_caps_violations(orders, self.QUOTES, self.LIMITS)

    def test_全部合規回空(self):
        self.assertEqual(self.caps([{"action": "buy", "code": "0050", "qty": 100}]), [])

    def test_單筆超限(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 250}])   # 25,000
        self.assertEqual(len(v), 1)
        self.assertIn("單筆", v[0])

    def test_單日買進總額超限(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 190},
                       {"action": "buy", "code": "2330", "qty": 10}])     # 19,000 + 23,800
        self.assertTrue(any("單日買進" in x for x in v))

    def test_單日成交總額超限(self):
        v = self.caps([{"action": "sell", "code": "2330", "qty": 15},     # 35,700
                       {"action": "sell", "code": "0050", "qty": 190},    # 19,000
                       {"action": "buy", "code": "0050", "qty": 120}])    # 12,000
        self.assertTrue(any("單日成交" in x for x in v))

    def test_低於最小可交易金額(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 10}])     # 1,000
        self.assertTrue(any("最小可交易" in x for x in v))

    def test_無報價時以限價中點估值(self):
        v = self.caps([{"action": "buy", "code": "9999", "qty": 10,
                        "limit_low": 2400.0, "limit_high": 2600.0}])      # 25,000
        self.assertTrue(any("單筆" in x for x in v))


class TestTotalResource(unittest.TestCase):
    def test_有運算頁讀值時以其為準(self):
        self.assertEqual(risk.total_resource(make_packet()), 211022.0)

    def test_無運算頁讀值時以持倉加餘額推算(self):
        pk = make_packet()
        pk["ark"]["posture"] = None
        self.assertAlmostEqual(risk.total_resource(pk), 211022.0, places=0)


class TestBuildEnvelope(unittest.TestCase):
    def env(self, packet=None, points=None, exits=(), config=None, effective_names=2):
        return risk.build_envelope(
            packet=packet or make_packet(),
            assigned=ASSIGNED,
            equity_points=list(points if points is not None
                               else [equity.make_point("2026-08-10", 196349.0,
                                                       14673.0, 47600.0)]),
            satellite_exits=list(exits),
            calendar=CALENDAR,
            today="2026-08-11",
            effective_max_names=effective_names,
            max_names_history=[2],
            config={**risk.DEFAULTS, **(config or {})})

    def test_正常狀態可買可賣(self):
        e = self.env()
        self.assertTrue(e["can_buy"])
        self.assertTrue(e["can_sell"])
        self.assertEqual(e["blocks"], [])
        self.assertEqual(e["breaker"]["level"], equity.NONE)

    def test_L1熔斷停買仍可賣(self):
        points = [equity.make_point("2026-08-10", 200000.0, 11022.0, 47600.0),
                  equity.make_point("2026-08-11", 180000.0, 11022.0, 47600.0)]
        e = self.env(points=points)
        self.assertEqual(e["breaker"]["level"], equity.L1)
        self.assertFalse(e["can_buy"])
        self.assertTrue(e["can_sell"])

    def test_L2熔斷全停(self):
        points = [equity.make_point("2026-08-10", 200000.0, 11022.0, 47600.0),
                  equity.make_point("2026-08-11", 160000.0, 11022.0, 47600.0)]
        e = self.env(points=points)
        self.assertEqual(e["breaker"]["level"], equity.L2)
        self.assertFalse(e["can_buy"])
        self.assertFalse(e["can_sell"])
        self.assertTrue(any("L2" in b for b in e["blocks"]))

    def test_運算頁讀不到時全停(self):
        """隱私眼睛開啟會遮蔽金額，read_posture 靜默回 None，discipline 隨即
        退化成 cash=0、不需調節——建立在錯的事實上遠比不交易危險"""
        pk = make_packet()
        pk["ark"]["posture"] = None
        e = self.env(packet=pk)
        self.assertFalse(e["can_buy"])
        self.assertFalse(e["can_sell"])
        self.assertTrue(any("運算頁" in b for b in e["blocks"]))

    def test_對帳不一致全停(self):
        """紀律邊界建立在錯的持倉上，比不交易還危險"""
        e = self.env(packet=make_packet(sync_ok=False))
        self.assertFalse(e["can_buy"])
        self.assertFalse(e["can_sell"])
        self.assertTrue(any("對帳" in b for b in e["blocks"]))

    def test_衛星軌白名單來自指定而非持倉(self):
        """開新衛星標的必須有人手動寫進 tracks.json——程式不自己擴充白名單。
        持倉清單做不到這件事：還沒買的標的不在持倉裡。"""
        e = risk.build_envelope(
            packet=make_packet(), assigned={**ASSIGNED, "00631L": tracks.SATELLITE},
            equity_points=[equity.make_point("2026-08-10", 196349.0, 14673.0, 47600.0)],
            satellite_exits=[], calendar=CALENDAR, today="2026-08-11",
            effective_max_names=2, max_names_history=[2], config=risk.DEFAULTS)
        s = e["tracks"]["satellite"]
        self.assertEqual(s["allowlist"], ["00631L", "2330"])
        self.assertEqual(s["codes"], ["2330"])          # 白名單 ≠ 已持有

    def test_衛星軌配額不含繼承部位(self):
        """2308 的 54,450 若計入配額，衛星軌開場就爆表且永遠買不了"""
        s = self.env()["tracks"]["satellite"]
        self.assertEqual(s["value"], 47600.0)
        self.assertAlmostEqual(s["quota"], 52755.5, places=1)     # 211,022 × 25%
        self.assertAlmostEqual(s["remaining"], 5155.5, places=1)

    def test_四軌市值加總等於持倉總市值(self):
        t = self.env()["tracks"]
        total = sum(t[k]["value"] for k in tracks.ALL)
        self.assertAlmostEqual(total, 196349.0, places=0)

    def test_持股比例偏離帶納入建議(self):
        e = self.env()
        self.assertEqual(e["core"]["ratio_band"]["action"], risk.REDUCE)

    def test_衛星軌冷卻中不可新倉但可停損(self):
        """停損是出場，冷卻只擋新倉——冷卻中還不准停損等於把虧損部位鎖死"""
        e = self.env(exits=[{"date": "2026-08-10", "stopped": True}] * 3)
        self.assertTrue(e["tracks"]["satellite"]["halted"])
        self.assertTrue(e["can_sell"])

    def test_envelope帶入限額供下游檢查(self):
        limits = self.env()["limits"]
        self.assertEqual(limits["per_order_cap"], risk.DEFAULTS["per_order_cap"])
        self.assertEqual(limits["min_trade_value"], risk.DEFAULTS["min_trade_value"])

    def test_記錄當日原始檔數供日後遲滯計算(self):
        """遲滯要比對歷次的『公式原值』，envelope 不留它就沒得比"""
        self.assertEqual(self.env()["core"]["raw_max_names"], 2)


class TestFullyBlocked(unittest.TestCase):
    """排程要能分辨「今天不該交易」與「程式壞了」——前者離開碼 3、後者 2。
    只擋買（L1）不算全停：還能賣就仍該讓 Agent 跑一輪。"""

    def env(self, can_buy, can_sell):
        return {"can_buy": can_buy, "can_sell": can_sell}

    def test_完全不能動才算全停(self):
        self.assertTrue(risk.is_fully_blocked(self.env(False, False)))

    def test_只擋買不算全停(self):
        self.assertFalse(risk.is_fully_blocked(self.env(False, True)))

    def test_只擋賣不算全停(self):
        self.assertFalse(risk.is_fully_blocked(self.env(True, False)))

    def test_暢通不算全停(self):
        self.assertFalse(risk.is_fully_blocked(self.env(True, True)))


class TestEnvelopeHistory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def env(self, date, raw, effective):
        return {"schema": 1, "date": date,
                "core": {"raw_max_names": raw, "max_names": effective}}

    def test_無歷史時回空與None(self):
        self.assertEqual(risk.max_names_history(self.dir), ([], None))

    def test_依日期排序取出原值序列與最後生效值(self):
        for e in (self.env("2026-08-12", 2, 1), self.env("2026-08-10", 1, 1),
                  self.env("2026-08-11", 2, 1)):
            risk.save_envelope(e, self.dir)
        self.assertEqual(risk.max_names_history(self.dir), ([1, 2, 2], 1))

    def test_略過缺欄位的舊檔(self):
        risk.save_envelope({"schema": 1, "date": "2026-08-10", "core": {}}, self.dir)
        risk.save_envelope(self.env("2026-08-11", 2, 1), self.dir)
        self.assertEqual(risk.max_names_history(self.dir), ([2], 1))


if __name__ == "__main__":
    unittest.main()
