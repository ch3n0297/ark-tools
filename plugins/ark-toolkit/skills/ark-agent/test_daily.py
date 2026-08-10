"""每日編排的純邏輯測試（不需要 ARK、Shioaji 或 launchd，任何平台可跑）"""
import unittest

import daily


class TestIsTradingWeekday(unittest.TestCase):
    def test_平日為真(self):
        for d in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
                  "2026-08-14"):
            self.assertTrue(daily.is_trading_weekday(d), d)

    def test_週末為假(self):
        self.assertFalse(daily.is_trading_weekday("2026-08-15"))   # 六
        self.assertFalse(daily.is_trading_weekday("2026-08-16"))   # 日


class TestRiskExitCode(unittest.TestCase):
    """risk.py 用離開碼區分「今天不該交易」（3）與「產不出邊界」（2）。
    編排把兩者混為一談的話，全面熔斷會被誤報成系統故障。"""

    def test_零為正常繼續(self):
        self.assertEqual(daily.classify_risk_exit(0), daily.PROCEED)

    def test_三為今日不交易(self):
        self.assertEqual(daily.classify_risk_exit(3), daily.SKIP)

    def test_其餘為錯誤(self):
        for code in (1, 2, 127, -9):
            self.assertEqual(daily.classify_risk_exit(code), daily.FAIL, code)


class TestPromptRendering(unittest.TestCase):
    TEMPLATE = ("讀 PACKET_PATH 與 ENVELOPE_PATH，"
                "寫到 DECISION_PATH，日期 TODAY。")

    def test_四個佔位符都被換掉(self):
        out = daily.render_prompt(self.TEMPLATE, packet="/p.json",
                                  envelope="/e.json", decision="/d.json",
                                  date="2026-08-11")
        self.assertEqual(out, "讀 /p.json 與 /e.json，寫到 /d.json，日期 2026-08-11。")

    def test_未被取代的佔位符會報錯(self):
        """漏換 DECISION_PATH 的話 Agent 會把決策寫到字面上的 'DECISION_PATH'，
        排程接著找不到檔案——與其那樣，不如當場失敗"""
        with self.assertRaises(ValueError):
            daily.render_prompt("讀 PACKET_PATH 和 MYSTERY_PATH", packet="/p",
                                envelope="/e", decision="/d", date="2026-08-11")


class TestSettleOutcome(unittest.TestCase):
    """結算的每一步都要盡力做完再回報——成交對回失敗不該讓淨值記錄也跳過，
    否則熔斷基準會斷掉。"""

    def test_全成功(self):
        self.assertEqual(daily.settle_failures([("對回", 0), ("淨值", 0)]), [])

    def test_只回報失敗的步驟(self):
        self.assertEqual(
            daily.settle_failures([("對回", 1), ("淨值", 0), ("同步", 2)]),
            ["對回", "同步"])


if __name__ == "__main__":
    unittest.main()
