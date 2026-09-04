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


# 取自 2026-08-10 實機的真實座標。**版面在 v1.7.4 之後變過**：列高 70→54、
# 上下兩排相對列頂的偏移 15/36→11/27。舊常數會讓下排落進上排、又把下一列的
# 上排吃進本列下排，欄位整排錯位 → 一致性檢查失敗 → 整頁讀成 0 檔。
# 這是靜默失敗：系統會誤判「沒有買進候選」而永遠不買，卻不報任何錯。
LAYOUT_ROW_V2 = ("國泰費城半導體, 00830, 價值", 447.0, [
    (161.0, 459.0, "88.45"), (257.0, 458.0, "88.24"),
    (348.0, 458.0, "3"), (407.0, 458.0, "266"),
    (117.0, 475.0, "▲2.1(2.43%)"), (236.0, 474.0, "0.24%"),
    (297.0, 474.0, "267"), (363.0, 474.0, "23,647"),
])
# 下一列（富邦科技 0052）列頂 500，上排在 512–513
LAYOUT_V2_NEXT_ROW_CELLS = [(162.0, 513.0, "60.95"), (257.0, 512.0, "60.93"),
                            (348.0, 512.0, "4"), (407.0, 512.0, "244")]


def parse_layout_row(sample):
    desc, top, cells = sample
    upper, lower = ark.split_layout_cells(cells, top)
    return ark.parse_layout(desc, upper, lower)


class FakeEl:
    def __init__(self, role, desc, x, y):
        self.role, self.desc, self.x, self.y = role, desc, x, y


class FakeAX:
    """layout_elements 只用到 find / attr / point 三個介面。"""

    def __init__(self, elements):
        self.elements = elements

    def find(self, root, pred):
        return [e for e in self.elements if pred(e)]

    def attr(self, el, name):
        return {"AXDescription": el.desc, "AXRole": el.role}.get(name)

    def point(self, el):
        return (el.x, el.y)


class TestLayoutElementsWindowOffset(unittest.TestCase):
    """名稱列靠齊**視窗**左緣，不是螢幕左緣。用絕對座標 0 判斷的話，
    視窗一被移動就一列都認不出來——而且不報錯，整頁靜默讀成 0 檔。"""

    def elements(self, left):
        return [FakeEl("AXStaticText", "國泰費城半導體, 00830, 價值", left, 447.0),
                FakeEl("AXButton", "88.45", left + 133, 459.0),
                FakeEl("AXButton", "88.24", left + 229, 458.0)]

    def test_視窗在螢幕左緣時可讀(self):
        names, cells = ark.layout_elements(FakeAX(self.elements(0)), None, 900, left=0)
        self.assertEqual([d for _y, d in names], ["國泰費城半導體, 00830, 價值"])
        self.assertEqual(len(cells), 2)

    def test_視窗被移開後仍可讀(self):
        names, cells = ark.layout_elements(FakeAX(self.elements(28)), None, 900, left=28)
        self.assertEqual([d for _y, d in names], ["國泰費城半導體, 00830, 價值"])
        self.assertEqual(len(cells), 2)

    def test_非靠左的靜態文字不算名稱列(self):
        els = self.elements(28) + [FakeEl("AXStaticText", "成本 163,970", 128, 437.0)]
        names, _ = ark.layout_elements(FakeAX(els), None, 900, left=28)
        self.assertEqual(len(names), 1)

    def test_底部分頁之下的元素被濾掉(self):
        els = self.elements(28) + [FakeEl("AXButton", "策略", 30, 950.0)]
        _, cells = ark.layout_elements(FakeAX(els), None, 900, left=28)
        self.assertNotIn("策略", [t for _x, _y, t in cells])


class TestLayoutGeometryCurrentVersion(unittest.TestCase):
    def test_目前版面的上下排分排正確(self):
        upper, lower = ark.split_layout_cells(LAYOUT_ROW_V2[2], LAYOUT_ROW_V2[1])
        self.assertEqual(upper, ["88.45", "88.24", "3", "266"])
        self.assertEqual(lower, ["▲2.1(2.43%)", "0.24%", "267", "23,647"])

    def test_目前版面不吃到下一列(self):
        cells = LAYOUT_ROW_V2[2] + LAYOUT_V2_NEXT_ROW_CELLS
        upper, lower = ark.split_layout_cells(cells, LAYOUT_ROW_V2[1])
        for v in ("60.95", "60.93", "244"):
            self.assertNotIn(v, upper + lower, v)

    def test_目前版面能完整解析成列(self):
        row = parse_layout_row(LAYOUT_ROW_V2)
        self.assertEqual(row.code, "00830")
        self.assertEqual(row.tiers, ("價值",))
        self.assertEqual((row.price, row.nav), (88.45, 88.24))
        self.assertEqual((row.tier_qty, row.tier_amount), (3, 266.0))
        self.assertEqual((row.risk_qty, row.risk_amount), (267, 23647.0))
        self.assertTrue(ark.layout_is_consistent(row))


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


# v1.8.0（2026-08-26）布局自選頁改成一列一段文字，數值不再是獨立元素：
#   名稱, 代號, [位階…], 股價, 漲跌幅, 即時淨值, 折溢價%, 位階股數, 風控股數, 位階布局金額, 風控布局金額
# 三筆皆取自實機。
LAYOUT_LINE = "富邦科技, 0052, 價值, 61, ▲0.45(0.74%), 61.16, -0.26%, 23, 927, 1,403, 56,561"
LAYOUT_LINE_NO_TIER = "元大全球5G, 00876, 85.95, ▲0.15(0.17%), 86.06, -0.13%, 34, 658, 2,923, 56,561"
LAYOUT_LINE_TWO_TIERS = "野村全球航運龍頭, 00960, 價值, 升溫, 21.64, ▲0.01(0.05%), 21.66, -0.09%, 28, 2613, 606, 56,561"


class TestParseLayoutLine(unittest.TestCase):
    def test_單行文字全部欄位(self):
        row = ark.parse_layout_line(LAYOUT_LINE)
        self.assertEqual((row.code, row.tiers), ("0052", ("價值",)))
        self.assertEqual((row.price, row.change, row.nav), (61.0, "▲0.45(0.74%)", 61.16))
        self.assertAlmostEqual(row.premium, -0.26)
        self.assertEqual((row.tier_qty, row.tier_amount), (23, 1403.0))
        self.assertEqual((row.risk_qty, row.risk_amount), (927, 56561.0))

    def test_無位階(self):
        row = ark.parse_layout_line(LAYOUT_LINE_NO_TIER)
        self.assertEqual((row.code, row.tiers, row.tier_qty), ("00876", (), 34))

    def test_兩個位階(self):
        row = ark.parse_layout_line(LAYOUT_LINE_TWO_TIERS)
        self.assertEqual(row.tiers, ("價值", "升溫"))
        self.assertEqual((row.tier_qty, row.risk_qty), (28, 2613))

    def test_真實資料通過一致性檢查(self):
        for s in (LAYOUT_LINE, LAYOUT_LINE_NO_TIER, LAYOUT_LINE_TWO_TIERS):
            self.assertTrue(ark.layout_is_consistent(ark.parse_layout_line(s)), s)

    def test_舊版名稱列回傳None(self):
        self.assertIsNone(ark.parse_layout_line("元大全球5G, 00876, 價值"))
        self.assertIsNone(ark.parse_layout_line(""))


class TestCollectLayoutRows(unittest.TestCase):
    """read_layout 每一屏的收集邏輯：新舊版面都要能讀，讀不出來的列要被拒絕而不是靜默略過。"""

    def test_新版單行列(self):
        found, rejected = ark.collect_layout_rows([(152.0, LAYOUT_LINE)], [])
        self.assertEqual(list(found), ["0052"])
        self.assertEqual(rejected, [])

    def test_舊版名稱列加儲存格(self):
        desc, top, cells = LAYOUT_ROW_V2
        found, rejected = ark.collect_layout_rows([(top, desc)], cells)
        self.assertEqual(list(found), ["00830"])
        self.assertEqual(rejected, [])

    def test_像資料列卻解析不出來要拒絕(self):
        """1.8.0 實例：舊解析器對新版面一列都認不得、也不報錯，整頁靜默讀成 0 檔。"""
        garbled = "富邦科技, 0052, 價值, 61, ▲0.45(0.74%), 61.16, -0.26%, 23, 927, 1,403, x"
        found, rejected = ark.collect_layout_rows([(152.0, garbled)], [])
        self.assertEqual(found, {})
        self.assertEqual(rejected, [garbled])

    def test_風控股數對不上要拒絕(self):
        bad = LAYOUT_LINE.replace("927", "999")
        _found, rejected = ark.collect_layout_rows([(152.0, bad)], [])
        self.assertEqual(rejected, [bad])


if __name__ == "__main__":
    unittest.main()


class FakeLayoutPageAx:
    """布局自選頁的可捲清單：AX 翻頁有效，滾輪若被呼叫就記錄下來（那會動游標）。

    每「頁」露出 3 列、與前一頁重疊 1 列——重疊是逐頁收集不漏列的前提。
    """

    ROWS = ["富邦科技, 0052, 價值, 62.05, ▼1(-1.59%), 62.12, -0.11%, 1, 89, 63, 5,545",
            "元大電子, 0053, 價值, 242.35, ▼4(-1.62%), 241.13, 0.51%, 0, 22, 0, 5,545",
            "元大台灣50正2, 00631L, 價值, 35.93, ▼1.33(-3.57%), 35.9, 0.08%, 2, 154, 72, 5,545",
            "元大台灣50, 0050, 價值, 106.8, ▼1.65(-1.52%), 106.67, 0.12%, 0, 51, 0, 5,545",
            "元大高股息, 0056, 價值, 55.1, ▲0.25(0.46%), 55.35, -0.45%, 1, 100, 56, 5,545"]
    PAGE, OVERLAP = 3, 1

    def __init__(self):
        self.top = 0
        self.wheel_calls = []
        self.page_calls = []

    def window(self, pid):
        return "w"

    def point(self, el):
        return (77.0, 158.0) if el == "w" else (77.0, 200.0)

    def size(self, el):
        return (288.0, 545.0)

    def _visible(self):
        return self.ROWS[self.top:self.top + self.PAGE]

    def find(self, root, pred):
        els = [("name", d) for d in self._visible()]
        return [e for e in els if pred(e)]

    def attr(self, el, name):
        return {"AXDescription": el[1], "AXRole": "AXStaticText"}.get(name)

    def descs(self, w, role=None):
        return [("el", d) for d in self._visible()]

    def by_desc(self, w, text):
        return ["el"] if text in ("自選", "布局 自選", "位階股數", "股票名稱", "全部庫存",
                                  "調節 庫存") else []

    def press(self, el):
        return 0

    def perform(self, el, action):
        return 0

    def scroll(self, cx, cy, dy, times):
        self.wheel_calls.append((cx, cy, dy))       # 滾輪＝會動游標，不該再被呼叫

    def scroll_page(self, pid, action="AXScrollDownByPage", tries=4):
        self.page_calls.append(action)
        step = self.PAGE - self.OVERLAP
        before = self.top
        if action == "AXScrollDownByPage":
            self.top = min(self.top + step, max(0, len(self.ROWS) - self.PAGE))
        else:
            self.top = max(0, self.top - step)
        return 0 if self.top != before else None    # 到端點回 None（同 ax.scroll_page）


class TestReadLayoutScrollsWithoutMouse(unittest.TestCase):
    """讀布局自選頁每天都會跑。2026-08 時這頁沒有任何可捲的 AX 元素，只能用滾輪
    （游標會被搶走）；App 1.8.2 起 38 個元素支援 AXScrollDownByPage（2026-09-02 實測），
    改用 AX 翻頁就不必動游標了。"""

    def _read(self, fake):
        from unittest import mock
        with mock.patch("time.sleep"):
            return ark.read_layout(fake, 0)

    def test_不再使用滾輪(self):
        fake = FakeLayoutPageAx()
        self._read(fake)
        self.assertEqual(fake.wheel_calls, [])
        self.assertIn("AXScrollDownByPage", fake.page_calls)

    def test_先回頂再逐頁收集_不漏列(self):
        fake = FakeLayoutPageAx()
        fake.top = 2                                # 從中段開始，模擬上次停留的位置
        view = self._read(fake)
        self.assertEqual(set(view.rows), {"0052", "0053", "00631L", "0050", "0056"})
        self.assertEqual(fake.page_calls[0], "AXScrollUpByPage")
