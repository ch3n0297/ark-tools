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


class TestDecisionWindow(unittest.TestCase):
    """launchd 對錯過的 StartCalendarInterval 會在喚醒後補跑一次。10:00 的判斷
    在 13:00 執行是另一回事——盤中價格早已不同，限價也全部失效。時間窗必須由
    程式自己守，不能寄望排程器的語意。"""

    def test_排定時間附近放行(self):
        for t in ("09:55", "10:00", "10:20", "10:45"):
            self.assertTrue(daily.is_within_window(t, "10:00", 60), t)

    def test_超出容許範圍拒絕(self):
        for t in ("08:30", "11:05", "13:20", "23:00"):
            self.assertFalse(daily.is_within_window(t, "10:00", 60), t)

    def test_排定時間之前也擋(self):
        """機器提早喚醒時，09:00 就跑會拿到還沒重算過的 ARK 指標"""
        self.assertFalse(daily.is_within_window("08:59", "10:00", 60))

    def test_邊界含端點(self):
        self.assertTrue(daily.is_within_window("09:00", "10:00", 60))
        self.assertTrue(daily.is_within_window("11:00", "10:00", 60))

    def test_結算窗較寬鬆(self):
        """結算是對既成事實的記錄，晚幾小時做結果一樣，窗開大一點無妨"""
        self.assertTrue(daily.is_within_window("17:00", "14:30", 240))


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


class TestQuotaExhausted(unittest.TestCase):
    """決策模型撞到用量上限時，claude CLI 也是非零離開——與 prompt 寫錯、
    網路斷線混在一起的話，換模型重試就會在不該重試的時候白燒一次決策。"""

    LIMIT = ("You've reached your Fable 5 limit. Run /usage-credits to "
             "continue or switch models with /model.")

    def test_額度訊息為真(self):
        self.assertTrue(daily.is_quota_exhausted(self.LIMIT))

    def test_額度訊息夾在其他輸出中也認得(self):
        """實際 log 裡前面還有工具呼叫與警告，額度訊息在最後一行"""
        self.assertTrue(daily.is_quota_exhausted(
            f"讀取 packet…\nDeprecationWarning: ...\n{self.LIMIT}\n"))

    def test_其他失敗為假(self):
        for out in ("", "Error: connection refused",
                    "提示模板有未取代的佔位符：['MYSTERY_PATH']",
                    "✅ 決策已寫入 /Users/hjc/.ark-toolkit/agent/decisions/x.json"):
            self.assertFalse(daily.is_quota_exhausted(out), out)


class TestFallbackModel(unittest.TestCase):
    """額度耗盡就整天不交易的話，實驗數據會出現非市場因素的缺口——
    但降級只在額度這一種失敗上成立，其餘照舊直接失敗。"""

    LIMIT = "You've reached your Fable 5 limit. Run /usage-credits to continue."

    def test_額度耗盡回傳接手模型(self):
        self.assertEqual(
            daily.fallback_model(1, self.LIMIT, "fable", "opus"), "opus")

    def test_成功時不降級(self):
        """離開碼 0 代表決策已產出，輸出裡出現什麼字樣都不該再跑一次"""
        self.assertIsNone(daily.fallback_model(0, self.LIMIT, "fable", "opus"))

    def test_非額度失敗不降級(self):
        self.assertIsNone(
            daily.fallback_model(1, "Error: connection refused", "fable", "opus"))

    def test_接手模型與現用相同時不降級(self):
        """否則會拿同一個已耗盡的額度池再撞一次牆"""
        self.assertIsNone(
            daily.fallback_model(1, self.LIMIT, "opus", "opus"))

    def test_未設定接手模型時不降級(self):
        for fallback in ("", None):
            self.assertIsNone(
                daily.fallback_model(1, self.LIMIT, "fable", fallback), fallback)


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
