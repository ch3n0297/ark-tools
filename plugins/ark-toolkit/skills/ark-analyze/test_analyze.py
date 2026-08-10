"""ark-analyze 純邏輯測試（不需要 ARK 執行中，任何平台可跑）"""
import unittest

import analyze
import ark
from ark import Holding, Layout, Posture


def H(code, qty, price, value, pnl=0.0, roi=0.0, tiers=(), sug_qty=None, sug_amt=None):
    return Holding(code=code, qty=qty, price=price, cost=qty * price, value=value,
                   pnl=pnl, roi=roi, today_pnl=0.0, tiers=tiers,
                   suggest_amount=sug_amt, suggest_qty=sug_qty)


# 取自真實資料的縮樣：台積電佔比極高
PORTFOLIO = {
    "2330": H("2330", 50, 2062.38, 119000.0, 15881.0, 15.4, ("價值",), 35, 83300.0),
    "0050": H("0050", 17, 96.76, 1756.0, 111.0, 6.71, ("價值",), 13, 1343.0),
    "2308": H("2308", 30, 1822.57, 49500.0, -5177.0, -9.47, ("價值",), 40, 72900.0),
    "0051": H("0051", 2, 133.0, 282.0, 16.0, 5.83, (), 1, 141.0),
}
POSTURE = Posture(suggested_ratio=66.5, stock_value=176664.0, cash=20000.0,
                  suggested_value=130782.0, suggested_cash=65882.0, adjust_amount=45882.0)


# 取自 v1.7.4 布局自選頁的真實座標。數值不在名稱列裡，是各自獨立的元素，
# 上下兩排各四欄，只能靠座標對回列。
LAYOUT_ROW = ("元大全球5G, 00876, 價值", 400.0, [
    (197.0, 416.0, "88"), (301.0, 415.0, "87.74"), (416.0, 415.0, "3"), (492.0, 415.0, "264"),
    (116.0, 437.0, "▼1.65(-1.84%)"), (270.0, 436.0, "0.3%"),
    (350.0, 436.0, "268"), (435.0, 436.0, "23,647"),
])
LAYOUT_TWO_TIERS = ("富邦摩台, 0057, 價值, 升溫", 820.0, [
    (164.0, 836.0, "309.05"), (290.0, 835.0, "308.31"), (416.0, 835.0, "0"), (512.0, 835.0, "0"),
    (116.0, 857.0, "▼1.9(-0.61%)"), (270.0, 856.0, "0.24%"),
    (350.0, 856.0, "76"), (435.0, 856.0, "23,647"),
])


def parse_layout_row(sample):
    desc, top, cells = sample
    upper, lower = ark.split_layout_cells(cells, top)
    return ark.parse_layout(desc, upper, lower)


class TestParseLayout(unittest.TestCase):
    def test_四欄全部解析(self):
        row = parse_layout_row(LAYOUT_ROW)
        self.assertEqual(row.code, "00876")
        self.assertEqual(row.tiers, ("價值",))
        self.assertEqual((row.price, row.nav), (88.0, 87.74))
        self.assertEqual((row.tier_qty, row.tier_amount), (3, 264.0))
        self.assertEqual((row.risk_qty, row.risk_amount), (268, 23647.0))

    def test_漲跌幅保留原文(self):
        """▼/▲ 帶方向資訊，轉成數字就丟了。"""
        self.assertEqual(parse_layout_row(LAYOUT_ROW).change, "▼1.65(-1.84%)")

    def test_折溢價百分比(self):
        self.assertAlmostEqual(parse_layout_row(LAYOUT_ROW).premium, 0.3)

    def test_一檔可以有兩個位階(self):
        row = parse_layout_row(LAYOUT_TWO_TIERS)
        self.assertEqual(row.tiers, ("價值", "升溫"))

    def test_位階股數可以是零(self):
        row = parse_layout_row(LAYOUT_TWO_TIERS)
        self.assertEqual((row.tier_qty, row.tier_amount), (0, 0.0))

    def test_欄位不足回傳None(self):
        self.assertIsNone(ark.parse_layout("甲, 0001, 價值", ["1", "2"], ["3", "4"]))
        self.assertIsNone(ark.parse_layout("", [], []))


class TestNum(unittest.TestCase):
    def test_None_視為零(self):
        """AXValue 缺失時 attr 回傳 None，read_posture 拿去轉數字不能崩潰"""
        self.assertEqual(ark.num(None), 0.0)


class TestSplitLayoutCells(unittest.TestCase):
    def test_上下兩排各自由左至右(self):
        upper, lower = ark.split_layout_cells(LAYOUT_ROW[2], LAYOUT_ROW[1])
        self.assertEqual(upper, ["88", "87.74", "3", "264"])
        self.assertEqual(lower, ["▼1.65(-1.84%)", "0.3%", "268", "23,647"])

    def test_不吃到下一列的儲存格(self):
        """列高 70px。下一列的儲存格若被吃進來，欄位就會整排錯位。"""
        cells = LAYOUT_ROW[2] + [(197.0, 486.0, "33.85")]      # 下一列的股價
        upper, lower = ark.split_layout_cells(cells, LAYOUT_ROW[1])
        self.assertNotIn("33.85", upper + lower)

    def test_名稱列本身不算儲存格(self):
        cells = [(0.0, 400.0, "元大全球5G, 00876, 價值")] + list(LAYOUT_ROW[2])
        upper, _lower = ark.split_layout_cells(cells, LAYOUT_ROW[1])
        self.assertEqual(upper[0], "88")


class TestLayoutConsistency(unittest.TestCase):
    """外部見證：欄位若錯位，App 自己的算術就對不起來。
    不能拿剛解析出來的欄位自己作證——這是 ark-sync 用「總成本」驗算的同一套思路。"""

    def test_真實資料通過(self):
        for sample in (LAYOUT_ROW, LAYOUT_TWO_TIERS):
            self.assertTrue(ark.layout_is_consistent(parse_layout_row(sample)))

    def test_風控股數對不上就不通過(self):
        row = parse_layout_row(LAYOUT_ROW)._replace(risk_qty=999)
        self.assertFalse(ark.layout_is_consistent(row))

    def test_股價為零不除以零(self):
        row = parse_layout_row(LAYOUT_ROW)._replace(price=0.0)
        self.assertFalse(ark.layout_is_consistent(row))


class TestLayoutReport(unittest.TestCase):
    VIEW = ark.LayoutView("0501ETF", {
        "00876": Layout("00876", ("價值",), 88.0, "▼1.65", 87.74, 0.3, 3, 264.0, 268, 23647.0),
        "0057": Layout("0057", ("價值", "升溫"), 309.05, "▼1.9", 308.31, 0.24, 0, 0.0, 76, 23647.0),
    })

    def test_列出每檔的位階與風控股數(self):
        text = analyze.render_layout(self.VIEW)
        self.assertIn("00876", text)
        self.assertIn("價值", text)
        self.assertIn("268", text)

    def test_標出是哪一份自選清單(self):
        """換一份清單數字全變。不標來源就成了另一種『失敗偽裝成成功』。"""
        self.assertIn("0501ETF", analyze.render_layout(self.VIEW))

    def test_兩個位階都列出(self):
        self.assertIn("價值／升溫", analyze.render_layout(self.VIEW))

    def test_空的就說讀不到(self):
        self.assertIn("讀不到", analyze.render_layout(ark.LayoutView("", {})))
        self.assertIn("讀不到", analyze.render_layout(None))

    def test_不含買賣指示用語(self):
        """與風控警示同一條規則：只陳述 App 給的數字，不轉成買賣建議。"""
        text = analyze.render_layout(self.VIEW)
        for word in ("買進", "賣出", "應該買", "建議買", "加碼", "減碼"):
            self.assertNotIn(word, text)


class TestConcentration(unittest.TestCase):
    def test_依市值佔比降序(self):
        rows = analyze.concentration(PORTFOLIO)
        self.assertEqual([r.code for r in rows], ["2330", "2308", "0050", "0051"])

    def test_佔比計算正確(self):
        rows = analyze.concentration(PORTFOLIO)
        total = sum(h.value for h in PORTFOLIO.values())
        self.assertAlmostEqual(rows[0].pct, 100 * 119000.0 / total, places=4)

    def test_佔比總和為百分之百(self):
        self.assertAlmostEqual(sum(r.pct for r in analyze.concentration(PORTFOLIO)), 100.0, places=6)

    def test_空組合不除以零(self):
        self.assertEqual(analyze.concentration({}), [])


class TestPostureMath(unittest.TestCase):
    def test_實際持股比例(self):
        self.assertAlmostEqual(POSTURE.actual_ratio, 100 * 176664 / 196664, places=4)

    def test_部位偏離為正表示過重(self):
        self.assertGreater(POSTURE.gap, 0)

    def test_資金為零時不除以零(self):
        p = Posture(66.5, 0.0, 0.0, 0.0, 0.0, 0.0)
        self.assertEqual(p.actual_ratio, 0.0)


class TestTrimSuggestions(unittest.TestCase):
    """「建議調節股數 ≥N」＝App 建議至少調節（賣出）N 股，不是持股目標。
    曾誤讀為持股下限而輸出「低於建議」——方向完全相反。
    語意出處：官方直播教學（見 ark-app-map.md 調節庫存一節）。"""

    def test_轉述App標示的調節股數與金額(self):
        trims = {t.code: t for t in analyze.trim_suggestions(PORTFOLIO)}
        self.assertEqual((trims["2330"].held, trims["2330"].trim_qty), (50, 35))
        self.assertAlmostEqual(trims["2330"].amount, 83300.0)

    def test_標示股數可超過持有股數(self):
        """App 給的照實轉述，不揣測、不修剪——持有 30 標示 ≥40 也是合法資料。"""
        trims = {t.code: t for t in analyze.trim_suggestions(PORTFOLIO)}
        self.assertEqual((trims["2308"].held, trims["2308"].trim_qty), (30, 40))

    def test_依建議調節金額由大到小(self):
        codes = [t.code for t in analyze.trim_suggestions(PORTFOLIO)]
        self.assertEqual(codes, ["2330", "2308", "0050", "0051"])

    def test_沒有建議值的檔略過(self):
        holdings = {"9999": H("9999", 5, 10.0, 50.0)}
        self.assertEqual(analyze.trim_suggestions(holdings), [])


class TestRenderTrims(unittest.TestCase):
    def test_列出合計並對照參考調節金額(self):
        """個股調節金額加總，就是拿來覆蓋運算頁「參考調節金額」用的——兩層有連動。"""
        text = analyze.render(PORTFOLIO, POSTURE, [])
        self.assertIn("157,684", text)     # 83,300 + 72,900 + 1,343 + 141
        self.assertIn("45,882", text)      # posture.adjust_amount

    def test_不再出現高低於建議的錯誤解讀(self):
        text = analyze.render(PORTFOLIO, POSTURE, [])
        self.assertNotIn("低於建議", text)
        self.assertNotIn("高於建議", text)


class TestRiskAlerts(unittest.TestCase):
    def test_單一持股超過上限觸發警示(self):
        alerts = analyze.risk_alerts(PORTFOLIO, POSTURE, max_single_pct=30)
        self.assertTrue(any(a.kind == "concentration" and "2330" in a.message for a in alerts))

    def test_未超過上限則不觸發集中度警示(self):
        alerts = analyze.risk_alerts(PORTFOLIO, POSTURE, max_single_pct=99)
        self.assertFalse(any(a.kind == "concentration" for a in alerts))

    def test_部位偏離超標觸發警示(self):
        alerts = analyze.risk_alerts(PORTFOLIO, POSTURE, max_gap_pct=10)
        self.assertTrue(any(a.kind == "position" for a in alerts))

    def test_部位偏離在容忍內則不觸發(self):
        alerts = analyze.risk_alerts(PORTFOLIO, POSTURE, max_gap_pct=50)
        self.assertFalse(any(a.kind == "position" for a in alerts))

    def test_沒有_posture_時仍能檢查集中度(self):
        alerts = analyze.risk_alerts(PORTFOLIO, None, max_single_pct=30)
        self.assertTrue(any(a.kind == "concentration" for a in alerts))
        self.assertFalse(any(a.kind == "position" for a in alerts))

    def test_警示不包含買賣指示用語(self):
        banned = ("買進", "賣出", "加碼", "減碼", "建議買", "建議賣", "應該買", "應該賣")
        for a in analyze.risk_alerts(PORTFOLIO, POSTURE):
            for word in banned:
                self.assertNotIn(word, a.message)

    def test_不再有持股低於建議類警示(self):
        """舊版把調節股數誤讀為持股下限而發 suggest 警示，語意確認後移除。"""
        alerts = analyze.risk_alerts(PORTFOLIO, POSTURE)
        self.assertFalse([a for a in alerts if a.kind == "suggest"])


# 位階：價值＝App 用於買進側的標記，升溫＝用於調節側，兩者可同時成立
TIERED = {
    "2330": H("2330", 50, 2062.38, 119000.0, tiers=("價值",)),
    "0057": H("0057", 100, 309.05, 30755.0, tiers=("價值", "升溫")),
    "00875": H("00875", 20, 55.55, 1111.0, tiers=("價值", "升溫")),
    "0051": H("0051", 2, 133.0, 282.0, tiers=()),
}


class TestTierAlerts(unittest.TestCase):
    def test_升溫持股觸發警示(self):
        alerts = analyze.risk_alerts(TIERED, None, max_single_pct=99)
        hot = [a for a in alerts if a.kind == "tier"]
        self.assertTrue(hot)
        self.assertIn("0057", hot[0].message)
        self.assertIn("00875", hot[0].message)

    def test_無升溫持股時不觸發(self):
        plain = {"2330": TIERED["2330"], "0051": TIERED["0051"]}
        alerts = analyze.risk_alerts(plain, None, max_single_pct=99)
        self.assertFalse([a for a in alerts if a.kind == "tier"])

    def test_雙位階另外提示且不再稱訊號相衝(self):
        """官方解讀：價值看長期、升溫看短期，同時成立並非矛盾——不再說「相衝」。"""
        alerts = analyze.risk_alerts(TIERED, None, max_single_pct=99)
        dual = [a for a in alerts if a.kind == "tier-conflict"]
        self.assertTrue(dual)
        self.assertIn("0057", dual[0].message)
        self.assertNotIn("相衝", dual[0].message)

    def test_只有單一位階時不觸發相衝提示(self):
        plain = {"2330": TIERED["2330"]}
        alerts = analyze.risk_alerts(plain, None, max_single_pct=99)
        self.assertFalse([a for a in alerts if a.kind == "tier-conflict"])

    def test_位階警示不含買賣指示用語(self):
        banned = ("買進", "賣出", "加碼", "減碼", "應該買", "應該賣")
        for a in analyze.risk_alerts(TIERED, None):
            for word in banned:
                self.assertNotIn(word, a.message)


class TestRenderTiers(unittest.TestCase):
    def test_集中度表列出位階(self):
        text = analyze.render(TIERED, None, [])
        self.assertIn("位階", text)
        self.assertIn("價值／升溫", text)

    def test_無位階顯示佔位符而非空白(self):
        text = analyze.render(TIERED, None, [])
        self.assertRegex(text, r"0051.*-")


class TestSnapshot(unittest.TestCase):
    def test_快照含持股與時間(self):
        snap = analyze.build_snapshot(PORTFOLIO, POSTURE, "2026-08-06T13:00:00")
        self.assertEqual(snap["taken_at"], "2026-08-06T13:00:00")
        self.assertEqual(snap["holdings"]["2330"]["qty"], 50)
        self.assertAlmostEqual(snap["posture"]["suggested_ratio"], 66.5)

    def test_無_posture_時仍可建立快照(self):
        snap = analyze.build_snapshot(PORTFOLIO, None, "2026-08-06T13:00:00")
        self.assertIsNone(snap["posture"])

    def test_比較快照找出新增減少與變動(self):
        old = analyze.build_snapshot(PORTFOLIO, POSTURE, "2026-08-01T00:00:00")
        newer = dict(PORTFOLIO)
        newer.pop("0051")
        newer["2454"] = H("2454", 1, 1000.0, 1100.0)
        newer["2330"] = H("2330", 60, 2062.38, 142800.0)
        snap = analyze.build_snapshot(newer, POSTURE, "2026-08-06T00:00:00")

        d = analyze.diff_snapshots(old, snap)
        self.assertEqual(d["added"], ["2454"])
        self.assertEqual(d["removed"], ["0051"])
        self.assertEqual([c["code"] for c in d["changed"]], ["2330"])
        self.assertEqual(d["changed"][0]["qty_from"], 50)
        self.assertEqual(d["changed"][0]["qty_to"], 60)

    def test_無變化時三項皆空(self):
        snap = analyze.build_snapshot(PORTFOLIO, POSTURE, "2026-08-06T00:00:00")
        d = analyze.diff_snapshots(snap, snap)
        self.assertEqual((d["added"], d["removed"], d["changed"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
