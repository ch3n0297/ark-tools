"""複盤組裝的純邏輯測試（不需要 journal、Shioaji 或網路，任何平台可跑）"""
import unittest

import review


def decision(date, orders=None, **extra):
    return {"type": "decision", "date": date, "ts": f"{date}T10:05:00",
            "rationale": f"{date} 的整體判斷", "orders": orders or [],
            "news_used": [], **extra}


def order(code, action="buy", qty=10, track="core", **extra):
    return {"action": action, "code": code, "qty": qty, "track": track,
            "reason": f"買 {code} 的理由", "ark_basis": {"signal": "tier_qty"},
            **extra}


def fill(date, ref, fills=None, unfilled=None):
    return {"type": "fill", "date": date, "ts": f"{date}T11:00:00",
            "decision_ref": ref, "fills": fills or [], "unfilled": unfilled or []}


class TestInRange(unittest.TestCase):
    """複盤是對一段期間的回顧，端點要含——使用者說「這週」時，
    週一與週五都該在裡面。"""

    def test_期間內為真(self):
        for d in ("2026-08-10", "2026-08-12", "2026-08-14"):
            self.assertTrue(review.in_range(d, "2026-08-10", "2026-08-14"), d)

    def test_邊界含端點(self):
        self.assertTrue(review.in_range("2026-08-10", "2026-08-10", "2026-08-14"))
        self.assertTrue(review.in_range("2026-08-14", "2026-08-10", "2026-08-14"))

    def test_期間外為假(self):
        for d in ("2026-08-09", "2026-08-15"):
            self.assertFalse(review.in_range(d, "2026-08-10", "2026-08-14"), d)

    def test_省略端點視為不設限(self):
        """複盤預設看全部歷史時，兩端都不給"""
        self.assertTrue(review.in_range("2020-01-01", None, None))
        self.assertTrue(review.in_range("2026-08-09", None, "2026-08-14"))
        self.assertTrue(review.in_range("2026-08-15", "2026-08-10", None))


class TestCasesFrom(unittest.TestCase):
    """一則案例＝當初怎麼想（rationale/reason）＋實際成交＋事後報酬＋紀律分。
    四者要配在一起，否則檢討時看不出「想法」與「結果」的因果。"""

    ENTRIES = [
        decision("2026-08-11", [order("0050")]),
        fill("2026-08-12", "2026-08-11",
             fills=[{"code": "0050", "action": "buy", "qty": 10, "price": 104.2}]),
        decision("2026-08-12", [order("0050"), order("2330", qty=2, track="satellite")]),
        fill("2026-08-13", "2026-08-12", fills=[], unfilled=[{"code": "0050"}]),
    ]
    REPORT = {
        "orders": [
            {"date": "2026-08-11", "action": "buy", "code": "0050", "qty": 10,
             "exec_price": 104.2, "exec_status": "filled",
             "horizons": {5: {"status": "ok", "return_adj": 0.021}}},
            {"date": "2026-08-12", "action": "buy", "code": "0050", "qty": 10,
             "exec_price": 105.5, "exec_status": "no_fill", "horizons": {}},
        ],
        "adherence": {"days": [{"date": "2026-08-11", "score": 1.0, "components": {}},
                               {"date": "2026-08-12", "score": 0.8, "components": {}}]},
    }

    def test_每個決策日一則案例(self):
        cases = review.cases_from(self.ENTRIES, self.REPORT, None, None)
        self.assertEqual([c["date"] for c in cases], ["2026-08-11", "2026-08-12"])

    def test_保留當初的想法(self):
        """複盤問的是「為什麼這樣想」——rationale 與每筆單的 reason 是核心材料"""
        case = review.cases_from(self.ENTRIES, self.REPORT, None, None)[0]
        self.assertEqual(case["rationale"], "2026-08-11 的整體判斷")
        self.assertEqual(case["orders"][0]["reason"], "買 0050 的理由")
        self.assertEqual(case["orders"][0]["ark_basis"], {"signal": "tier_qty"})

    def test_併入成交與前瞻報酬(self):
        case = review.cases_from(self.ENTRIES, self.REPORT, None, None)[0]
        o = case["orders"][0]
        self.assertEqual(o["exec_price"], 104.2)
        self.assertEqual(o["exec_status"], "filled")
        self.assertEqual(o["horizons"][5]["return_adj"], 0.021)

    def test_併入紀律分(self):
        cases = review.cases_from(self.ENTRIES, self.REPORT, None, None)
        self.assertEqual(cases[0]["adherence"]["score"], 1.0)
        self.assertEqual(cases[1]["adherence"]["score"], 0.8)

    def test_衛星軌的單也要進案例(self):
        """實驗統計只算主軌，但決策品質複盤要看全部——衛星軌那筆同樣花掉資金、
        同樣是判斷的產物，濾掉的話「為什麼買它」就永遠不會被檢討"""
        case = review.cases_from(self.ENTRIES, self.REPORT, None, None)[1]
        codes = [(o["code"], o["track"]) for o in case["orders"]]
        self.assertIn(("2330", "satellite"), codes)

    def test_無對應報酬的單不崩(self):
        """衛星軌與尚未到期的單在 report 裡沒有列，欄位留空即可"""
        case = review.cases_from(self.ENTRIES, self.REPORT, None, None)[1]
        sat = next(o for o in case["orders"] if o["code"] == "2330")
        self.assertIsNone(sat["exec_price"])
        self.assertEqual(sat["horizons"], {})

    def test_未成交要看得出來(self):
        case = review.cases_from(self.ENTRIES, self.REPORT, None, None)[1]
        self.assertEqual(case["unfilled"], [{"code": "0050"}])

    def test_期間篩選(self):
        cases = review.cases_from(self.ENTRIES, self.REPORT,
                                  "2026-08-12", "2026-08-12")
        self.assertEqual([c["date"] for c in cases], ["2026-08-12"])


class TestMissedDays(unittest.TestCase):
    """缺席日必須進複盤：連續缺席代表系統有問題，而那是最該檢討的事。
    把它濾掉的話，複盤只會看到「有做決策的那些天」，倖存者偏差。"""

    ENTRIES = [
        {"type": "missed", "date": "2026-08-13", "ts": "2026-08-14T10:00:00",
         "reason": "未做決策"},
        decision("2026-08-14", [order("0050")]),
    ]

    def test_缺席日成為案例(self):
        cases = review.cases_from(self.ENTRIES, {"orders": [], "adherence": {"days": []}},
                                  None, None)
        self.assertEqual([c["date"] for c in cases], ["2026-08-13", "2026-08-14"])
        self.assertTrue(cases[0]["missed"])
        self.assertEqual(cases[0]["reason"], "未做決策")

    def test_有決策的日子不標缺席(self):
        cases = review.cases_from(self.ENTRIES, {"orders": [], "adherence": {"days": []}},
                                  None, None)
        self.assertFalse(cases[1].get("missed"))


class TestAmendedAndViolations(unittest.TestCase):
    """修訂過或硬規則覆寫的決策要在複盤裡標出來——那是決策品質的警訊，
    平均分會把它稀釋掉。"""

    def test_標出修訂與違規(self):
        entries = [decision("2026-08-11", [order("0050")], amended=True,
                            violations=["買進 0050 違反紀律"])]
        case = review.cases_from(entries, {"orders": [], "adherence": {"days": []}},
                                 None, None)[0]
        self.assertTrue(case["amended"])
        self.assertEqual(case["violations"], ["買進 0050 違反紀律"])

    def test_正常決策不帶這些欄位(self):
        case = review.cases_from([decision("2026-08-11")],
                                 {"orders": [], "adherence": {"days": []}},
                                 None, None)[0]
        self.assertFalse(case["amended"])
        self.assertEqual(case["violations"], [])


class TestBuildContext(unittest.TestCase):
    """決策理由裡的數字要能在同一份輸出裡驗證。

    實例（2026-08-14）：理由寫「0050 位階金額 107 排第五」「sell_priority 首位」，
    但那些數字散在 packets/ 與 envelopes/——回溯討論時得同時翻四個檔案才拼得回
    當時的判斷情境，等於記錄完整但無法自足。
    """

    PACKET = {
        "ark": {
            "posture": {"suggested_ratio": 64.0, "actual_ratio": 52.16, "gap": -11.84},
            "layout": {"rows": {
                "0050": {"tiers": ["價值", "升溫"], "tier_amount": 107.0,
                         "premium": -0.34, "price": 107.0},
                "0052": {"tiers": ["價值"], "tier_amount": 125.0,
                         "premium": -0.72, "price": 62.5},
                "00875": {"tiers": ["價值"], "tier_amount": 117.0,
                          "premium": -0.09, "price": 58.35},
            }},
        },
        "discipline": {"sell_priority": ["0050"], "sellable": ["0050", "2330"],
                       "buy_candidates": ["0050", "0052", "00875"], "max_names": 2},
    }
    ENVELOPE = {
        "core": {"ratio_band": {"action": "buy"}, "max_names": 1, "raw_max_names": 2},
        "breaker": {"level": "none"},
    }

    def test_帶出方向依據(self):
        ctx = review.build_context(self.PACKET, self.ENVELOPE)
        self.assertEqual(ctx["posture"]["gap"], -11.84)
        self.assertEqual(ctx["posture"]["action"], "buy")

    def test_檔數上限要區分_App_值與生效值(self):
        """兩者不同時才解釋得了「為什麼滿檔」——App 說可以 2 檔，
        但 5 日遲滯把生效值壓在 1，所以買新標的就必須先賣掉手上那檔"""
        ctx = review.build_context(self.PACKET, self.ENVELOPE)
        self.assertEqual(ctx["max_names"], {"app": 2, "effective": 1})

    def test_帶出賣出優先序(self):
        ctx = review.build_context(self.PACKET, self.ENVELOPE)
        self.assertEqual(ctx["sell_priority"], ["0050"])

    def test_候選依位階金額排序(self):
        """跨檔比較是選股的核心，排好序才看得出「為什麼是這檔不是那檔」"""
        ctx = review.build_context(self.PACKET, self.ENVELOPE)
        self.assertEqual([c["code"] for c in ctx["candidates"]],
                         ["0052", "00875", "0050"])

    def test_候選帶比較所需欄位(self):
        ctx = review.build_context(self.PACKET, self.ENVELOPE)
        first = ctx["candidates"][0]
        self.assertEqual(first["tier_amount"], 125.0)
        self.assertEqual(first["premium"], -0.72)
        self.assertEqual(first["tiers"], ["價值"])

    def test_無_envelope_仍給_packet_的部分(self):
        """envelope 產生失敗那天仍要看得到 posture 與候選"""
        ctx = review.build_context(self.PACKET, None)
        self.assertEqual(ctx["posture"]["gap"], -11.84)
        self.assertIsNone(ctx["posture"]["action"])
        self.assertEqual(ctx["max_names"]["effective"], None)

    def test_無_packet_回_None(self):
        self.assertIsNone(review.build_context(None, self.ENVELOPE))


class TestCasesCarryContext(unittest.TestCase):
    def test_案例掛上當時的事實脈絡(self):
        entries = [decision("2026-08-14", [order("0052")])]
        contexts = {"2026-08-14": {"posture": {"gap": -11.84}}}
        case = review.cases_from(entries, {"orders": [], "adherence": {"days": []}},
                                 None, None, contexts)[0]
        self.assertEqual(case["context"]["posture"]["gap"], -11.84)

    def test_沒有脈絡時欄位為_None(self):
        case = review.cases_from([decision("2026-08-14")],
                                 {"orders": [], "adherence": {"days": []}},
                                 None, None)[0]
        self.assertIsNone(case["context"])


class TestRuleAttribution(unittest.TestCase):
    """準則要能被自己的損益推翻，否則「自我學習」只是換一種方式憑感覺。
    歸因鏈：決策記下採用了哪條準則 → 複盤依準則分組算前瞻報酬。"""

    CASES = [
        {"date": "2026-08-11", "rules_applied": ["R-001"],
         "orders": [{"code": "0050", "horizons": {5: {"return_adj": 0.02}}},
                    {"code": "0052", "horizons": {5: {"return_adj": 0.01}}}]},
        {"date": "2026-08-12", "rules_applied": ["R-001", "R-002"],
         "orders": [{"code": "0050", "horizons": {5: {"return_adj": -0.03}}}]},
        {"date": "2026-08-13", "rules_applied": [],
         "orders": [{"code": "2330", "horizons": {5: {"return_adj": 0.05}}}]},
    ]

    def test_依準則分組統計(self):
        perf = review.rule_performance(self.CASES, horizon=5)
        self.assertEqual(perf["R-001"]["n_decisions"], 2)
        self.assertEqual(perf["R-002"]["n_decisions"], 1)

    def test_平均報酬取該準則所有單(self):
        """R-001 用了兩天共三筆單：+2%、+1%、−3% → 平均 0%"""
        perf = review.rule_performance(self.CASES, horizon=5)
        self.assertAlmostEqual(perf["R-001"]["avg_return"], 0.0, places=6)
        self.assertAlmostEqual(perf["R-002"]["avg_return"], -0.03, places=6)

    def test_未採用準則的決策不計入(self):
        perf = review.rule_performance(self.CASES, horizon=5)
        self.assertNotIn("2330", str(perf.get("R-001", {})))
        self.assertEqual(sorted(perf), ["R-001", "R-002"])

    def test_尚無報酬的單不污染平均(self):
        """T+5 未到期時 horizons 是空的——不能當成 0% 報酬拉低平均"""
        cases = [{"date": "2026-08-14", "rules_applied": ["R-003"],
                  "orders": [{"code": "0050", "horizons": {}},
                             {"code": "0052", "horizons": {5: {"return_adj": 0.04}}}]}]
        perf = review.rule_performance(cases, horizon=5)
        self.assertEqual(perf["R-003"]["n_evaluated"], 1)
        self.assertAlmostEqual(perf["R-003"]["avg_return"], 0.04, places=6)

    def test_完全無報酬時平均為_None(self):
        """新準則剛上路就是這個狀態，要看得出「還沒有證據」而非「表現為 0」"""
        cases = [{"date": "2026-08-14", "rules_applied": ["R-004"],
                  "orders": [{"code": "0050", "horizons": {}}]}]
        perf = review.rule_performance(cases, horizon=5)
        self.assertIsNone(perf["R-004"]["avg_return"])
        self.assertEqual(perf["R-004"]["n_evaluated"], 0)


class TestExecLabel(unittest.TestCase):
    """`no_fill` 不等於「沒成交」——當日複盤時多半只是還沒對回（成交對回要等
    隔日 settle）。印成「未成交」會讓複盤誤判委託失敗，那是危險的誤讀。"""

    def test_已對回顯示成交價(self):
        self.assertEqual(review.exec_label("filled", 107.25), "成交 107.25")

    def test_未對回要講清楚是估算(self):
        got = review.exec_label("no_fill", 107.0)
        self.assertIn("尚未對回", got)
        self.assertIn("估算", got)
        self.assertNotIn("未成交", got)

    def test_無法評估(self):
        self.assertEqual(review.exec_label("unevaluable", None), "無法評估")


class TestDefaultWindow(unittest.TestCase):
    """預設複盤範圍：使用者說「這週」的意思是最近一週，不是全部歷史。"""

    def test_預設回看七天(self):
        self.assertEqual(review.default_window("2026-08-14"),
                         ("2026-08-08", "2026-08-14"))

    def test_跨月正確(self):
        self.assertEqual(review.default_window("2026-09-02"),
                         ("2026-08-27", "2026-09-02"))


if __name__ == "__main__":
    unittest.main()
