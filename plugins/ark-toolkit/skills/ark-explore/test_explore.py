"""explore 的純邏輯測試：不需要 ARK 執行中，任何平台可跑。"""
import json
import os
import tempfile
import unittest

import explore


class TestBlocklist(unittest.TestCase):
    def test_金流與登出被擋(self):
        for name in ("訂閱會員", "立即購買", "前往付款", "升級方案", "自動續約", "登出",
                     "退訂退款"):
            self.assertTrue(explore.is_blocked(name), name)

    def test_一般導航不被擋(self):
        for name in ("運算", "自選", "策略", "活動中心", "方舟投資",
                     "達人觀點", "外部精華", "舟聲電台", "離職倒數", "方舟啟航"):
            self.assertFalse(explore.is_blocked(name), name)

    def test_黑名單不得為空(self):
        """防止日後有人把防線改空——真實金流是唯一不可逆的操作。"""
        self.assertTrue(explore.BLOCKED_KEYWORDS)
        self.assertIn("付款", explore.BLOCKED_KEYWORDS)

    def test_空值不當成被擋(self):
        self.assertFalse(explore.is_blocked(None))
        self.assertFalse(explore.is_blocked(""))


class TestFingerprint(unittest.TestCase):
    def test_同一頁的股價跳動不改變指紋(self):
        """股價每秒在跳。含數字的指紋會讓同一頁被誤判成新頁，
        進而把動作型元素誤分類成導航。"""
        before = ["台積電", "1,234.5", "+2.31%", "總市值"]
        after = ["台積電", "1,240.0", "-0.15%", "總市值"]
        self.assertEqual(explore.fingerprint(before), explore.fingerprint(after))

    def test_不同頁指紋不同(self):
        self.assertNotEqual(
            explore.fingerprint(["調節庫存", "台股庫存"]),
            explore.fingerprint(["達人觀點", "最新文章"]),
        )

    def test_順序不影響指紋(self):
        self.assertEqual(
            explore.fingerprint(["甲", "乙", "丙"]),
            explore.fingerprint(["丙", "甲", "乙"]),
        )

    def test_純數字畫面不會塌成同一個指紋(self):
        """全數字剝光後會變空字串——這種畫面必須仍能區分，否則整批誤判為同一頁。"""
        self.assertNotEqual(explore.fingerprint(["1", "2"]), explore.fingerprint(["3"]))


class TestClassify(unittest.TestCase):
    def test_指紋改變是導航(self):
        self.assertEqual(explore.classify("aaa", "bbb"), explore.NAVIGATION)

    def test_指紋不變是動作(self):
        self.assertEqual(explore.classify("aaa", "aaa"), explore.ACTION)


class TestFindVersion(unittest.TestCase):
    def test_從同一行文字抓出(self):
        self.assertEqual(explore.find_version(["版本 3.2.1"]), "3.2.1")
        self.assertEqual(explore.find_version(["Version 1.9"]), "1.9")

    def test_從標籤的下一行抓出(self):
        self.assertEqual(explore.find_version(["版本", "3.2.1"]), "3.2.1")

    def test_股價不會被誤認成版本(self):
        """1,234.56 這種數字滿畫面都是，沒有『版本』字樣就不能採信。"""
        self.assertIsNone(explore.find_version(["台積電", "1234.56", "+2.31"]))

    def test_找不到回傳None(self):
        self.assertIsNone(explore.find_version([]))
        self.assertIsNone(explore.find_version(["版本"]))


class TestCacheDecision(unittest.TestCase):
    def test_已有該版本則跳過(self):
        action, _why = explore.cache_decision("3.2.1", {"explored_at": "2026-08-06T19:00:00"})
        self.assertEqual(action, explore.SKIP)

    def test_無快取則探索(self):
        action, _why = explore.cache_decision("3.2.1", None)
        self.assertEqual(action, explore.EXPLORE)

    def test_版本讀不到一律探索(self):
        """不能拿不可靠的鍵去命中快取——那會讓『沒探索到』偽裝成『已探索過』。"""
        action, why = explore.cache_decision(None, {"explored_at": "2026-08-06T19:00:00"})
        self.assertEqual(action, explore.EXPLORE)
        self.assertIn("版本", why)

    def test_存檔後讀得回來(self):
        with tempfile.TemporaryDirectory() as d:
            app_map = explore.build_map([], "3.2.1", "2026-08-06T19:00:00")
            path = explore.save_map(app_map, d)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(explore.load_cache("3.2.1", d)["version"], "3.2.1")

    def test_版本號含斜線也能安全當檔名(self):
        with tempfile.TemporaryDirectory() as d:
            path = explore.cache_path("3.2/1", d)
            self.assertEqual(os.path.dirname(path), d)


class TestElementReport(unittest.TestCase):
    def setUp(self):
        self.pages = [
            explore.Page(
                path=("運算",), fingerprint="a", texts=("風控 運算",),
                elements=(
                    explore.Element("離職倒數", "AXButton", explore.NAVIGATION),
                    explore.Element("重新整理", "AXButton", explore.ACTION),
                ),
            ),
            explore.Page(
                path=("會員",), fingerprint="b", texts=("方案",),
                elements=(explore.Element("立即訂閱", "AXButton", explore.BLOCKED),),
            ),
        ]

    def test_統計各類數量(self):
        report = explore.element_report(self.pages)
        self.assertEqual(report["counts"][explore.NAVIGATION], 1)
        self.assertEqual(report["counts"][explore.ACTION], 1)
        self.assertEqual(report["counts"][explore.BLOCKED], 1)

    def test_列出金流類元素的所在頁(self):
        """使用者斷言『本 App 除訂閱外不涉及真實交易』——這份清單就是驗證它的證據。"""
        report = explore.element_report(self.pages)
        self.assertEqual(report["blocked"], [{"page": "會員", "name": "立即訂閱"}])


class TestToMarkdown(unittest.TestCase):
    def test_每頁產出五個固定欄位(self):
        app_map = explore.build_map(
            [explore.Page(path=("方舟投資", "達人觀點"), fingerprint="a",
                          texts=("最新文章",), elements=())],
            "3.2.1", "2026-08-06T19:00:00")
        md = explore.to_markdown(app_map)
        self.assertIn("### 達人觀點", md)
        for field in ("**路徑**", "**功能**", "**讀得到**", "**作用**", "**限制**"):
            self.assertIn(field, md)

    def test_作用欄留給使用者填(self):
        """『這頁什麼時候該用』只有會員答得出來，dump 推不出——留 TODO 比編造好。"""
        app_map = explore.build_map(
            [explore.Page(path=("策略",), fingerprint="a", texts=(), elements=())],
            "3.2.1", "2026-08-06T19:00:00")
        self.assertIn(explore.TODO_MARK, explore.to_markdown(app_map))

    def test_標註版本與探索時間(self):
        app_map = explore.build_map([], "3.2.1", "2026-08-06T19:00:00")
        md = explore.to_markdown(app_map)
        self.assertIn("3.2.1", md)
        self.assertIn("2026-08-06", md)


class TestLooksNavigational(unittest.TestCase):
    """自選頁一頁就有 66 個可按元素，其中 50 個是股價與資料列。
    不過濾的話深度 3 的展開次數會爆炸，而且它們全都通往同一種「個股詳情」頁。"""

    def test_純數字與漲跌幅不是導航(self):
        for name in ("54.05", "▼257(-0.58%)", "-0.41%", "0.3%", "103.05", "▼1.25(-2.26%)"):
            self.assertFalse(explore.looks_navigational(name), name)

    def test_含股票代號的資料列不是導航(self):
        for name in ("兆豐洲際半導體, 00911, 全球, 4", "元大台灣50正2, 00631L, 台灣, 11",
                     "元大台灣50, 0050, 台灣, 23"):
            self.assertFalse(explore.looks_navigational(name), name)

    def test_過長文案不是導航(self):
        """『想知道下一步策略？歡迎來我們的官方社群討論！…』是廣告橫幅，
        按下去多半離開 App。記為未走訪比誤按安全。"""
        self.assertFalse(explore.looks_navigational(
            "想知道下一步策略？歡迎來我們的官方社群討論！一個友善的討論空間等你加入！可以邀請朋友唷"))

    def test_真正的導航標籤通過(self):
        for name in ("策略", "自選", "運算", "活動中心", "方舟投資", "設定", "期貨",
                     "ETF價值區", "ETF哨兵系統", "台股成交金額排行",
                     "達人 觀點", "舟聲 電台", "離職 倒數", "方舟 啟航"):
            self.assertTrue(explore.looks_navigational(name), name)

    def test_排序鈕會通過但靠指紋判定為動作(self):
        """股票名稱／成立年數是表頭排序鈕。它們會被按到，但排序只改變列的順序、
        不改變文字集合，所以指紋不變 → 自動歸類為動作型，不會被當成新頁面。"""
        self.assertTrue(explore.looks_navigational("股票名稱"))
        rows = ["甲, 0001, 台灣, 1", "乙, 0002, 台灣, 2"]
        self.assertEqual(explore.fingerprint(rows), explore.fingerprint(list(reversed(rows))))


class TestControlId(unittest.TestCase):
    """ARK 用小寫英文 AX id 標示控制項，內容區段一律中文。
    控制項是編輯／搜尋入口——會開出返回鍵定位失效的 modal，而且是資料變更面。
    `add stock off` 曾把探索器困在「加入自選」頁，之後全部走不到。"""

    def test_小寫英文id是控制項(self):
        for name in ("add stock off", "watchlist edit", "individual stock edit",
                     "navigation search icon", "pwd eye icon", "twd to usd",
                     "line nav button", "bell off", "calendar dot", "back"):
            self.assertTrue(explore.is_control_id(name), name)

    def test_中文內容標籤不是控制項(self):
        for name in ("ETF價值區", "達人觀點", "離職倒數", "方舟啟航", "調節庫存"):
            self.assertFalse(explore.is_control_id(name), name)

    def test_含大寫的外文內容不是控制項(self):
        for name in ("Palo Alto Networks Inc", "ETF共同持股"):
            self.assertFalse(explore.is_control_id(name), name)

    def test_控制項不列為導航候選(self):
        self.assertFalse(explore.looks_navigational("watchlist edit"))
        self.assertTrue(explore.looks_navigational("調節 庫存"))


class TestCommitButtons(unittest.TestCase):
    """提交鈕按下去不會產生新的地圖節點，只會把某個動作定案——依「按按鈕是為了
    到達新頁面」的原則本來就不該按。實際遇到的：設定›清除快取資料 的「確認」
    會重啟 App，運算›設定目標 的「儲存」會改掉離職金額目標。"""

    def test_提交鈕不是導航(self):
        for name in ("確認", "確定", "儲存", "送出", "全選"):
            self.assertFalse(explore.looks_navigational(name), name)

    def test_一般標籤不受影響(self):
        for name in ("設定目標", "確認信箱設定"):
            self.assertTrue(explore.looks_navigational(name), name)


class TestDigitRatio(unittest.TestCase):
    """名字裡數字佔比高的多半是資料而非導航標籤。"""

    def test_資料標籤被擋(self):
        for name in ("成本 164,938", "A12069", "2027/05/01", "44,487.94"):
            self.assertFalse(explore.looks_navigational(name), name)

    def test_含少量數字的導航標籤通過(self):
        for name in ("元大台灣50", "ETF共同持股", "台股成交金額排行"):
            self.assertTrue(explore.looks_navigational(name), name)


class TestPickBack(unittest.TestCase):
    """ARK 各頁返回鍵的標籤不一致——自選頁叫 back，大盤詳情頁叫「方舟運算」
    （左上角的 App logo）。用位置判定比用名字可靠。"""

    BOUNDS = (0.0, 222.0, 375.0, 699.0)

    def test_左上角的元素是返回鍵(self):
        placed = [((16.0, 274.0), "方舟運算"), ((169.5, 285.0), "大盤")]
        self.assertEqual(explore.pick_back(placed, self.BOUNDS), "方舟運算")

    def test_置中的標題不是返回鍵(self):
        self.assertIsNone(explore.pick_back([((169.5, 285.0), "大盤")], self.BOUNDS))

    def test_內容區的元素不是返回鍵(self):
        self.assertIsNone(explore.pick_back([((16.0, 860.0), "加權指數")], self.BOUNDS))

    def test_多個候選取最左上(self):
        placed = [((50.0, 280.0), "乙"), ((16.0, 274.0), "甲")]
        self.assertEqual(explore.pick_back(placed, self.BOUNDS), "甲")


class TestPickDismiss(unittest.TestCase):
    """modal 的關閉鍵在右上角（popup close @ (313,351)），pick_back 的左上角
    啟發式抓不到——「加入自選」modal 因此困住探索器，連下一次執行都起不了跑。"""

    def test_找出modal關閉鍵(self):
        names = ["「ETF價值區」股票加入自選", "popup close", "checkbox uncheck", "全選"]
        self.assertEqual(explore.pick_dismiss(names), "popup close")

    def test_一般頁面沒有關閉鍵(self):
        self.assertIsNone(explore.pick_dismiss(["策略", "自選", "運算"]))

    def test_中文關閉鍵也認得(self):
        self.assertEqual(explore.pick_dismiss(["關閉", "確定"]), "關閉")

    def test_不會把勾選框當成關閉鍵(self):
        """checkbox uncheck 按下去會把股票加入自選——那是資料變更，不是逃生門。"""
        self.assertIsNone(explore.pick_dismiss(["checkbox uncheck", "全選"]))


class TestIsOnscreen(unittest.TestCase):
    """視窗 (0,222) 375×699。ETF 橫向捲軸的元素 x 到 1247，遠在畫面外。
    按離屏元素會回傳 0 但靜默失效（ark-sync 陷阱 3），會被誤判成動作型。"""

    BOUNDS = (0.0, 222.0, 375.0, 699.0)

    def test_畫面內(self):
        self.assertTrue(explore.is_onscreen((0.0, 871.0), self.BOUNDS))
        self.assertTrue(explore.is_onscreen((313.0, 871.0), self.BOUNDS))

    def test_水平離屏(self):
        self.assertFalse(explore.is_onscreen((1247.0, 358.0), self.BOUNDS))

    def test_垂直離屏(self):
        self.assertFalse(explore.is_onscreen((100.0, 1500.0), self.BOUNDS))

    def test_量不到就當可見(self):
        """判斷不了就交給按下去的結果說話，不要憑空排除。"""
        self.assertTrue(explore.is_onscreen(None, self.BOUNDS))
        self.assertTrue(explore.is_onscreen((0.0, 0.0), None))


class TestTabRow(unittest.TestCase):
    """『視窗底部 N 像素內』會把最後一列的股價也抓進來——
    tab 的真正特徵是『同一個 y、至少 3 個、位在最下方』。"""

    def test_挑出最下方的同列群組(self):
        placed = [
            (0.0, 358.0, "ETF價值區"), (111.0, 358.0, "ETF升溫區"), (222.0, 358.0, "美股價值區"),
            (0.0, 871.0, "策略"), (63.0, 871.0, "自選"), (125.0, 871.0, "運算"),
            (188.0, 871.0, "活動中心"), (250.0, 871.0, "方舟投資"), (313.0, 871.0, "設定"),
            (176.0, 866.0, "103.3"), (226.0, 886.0, "0.24%"), (116.0, 887.0, "▼0.5(-0.48%)"),
        ]
        self.assertEqual(explore.tab_row(placed),
                         ["策略", "自選", "運算", "活動中心", "方舟投資", "設定"])

    def test_成員不足不算一列(self):
        self.assertEqual(explore.tab_row([(0.0, 900.0, "甲"), (10.0, 900.0, "乙")]), [])

    def test_沒有元素回傳空(self):
        self.assertEqual(explore.tab_row([]), [])


class TestTabCandidates(unittest.TestCase):
    """同列至少 3 個還不夠——大盤詳情頁的「成交量(億) / 9,421.57 / 昨量(億) / 12,002.13」
    正好同列且 4 個，曾被當成 tab，害 go_home 以為已回到根頁，整趟探索從錯的根開始。
    真正的 tab 還要是 AXButton 且貼齊視窗底部。"""

    BOUNDS = (0.0, 222.0, 375.0, 699.0)     # 底邊 y=921

    def test_真正的tab通過(self):
        placed = [(x, 871.0, name, "AXButton") for x, name in
                  ((0.0, "策略"), (63.0, "自選"), (125.0, "運算"),
                   (188.0, "活動中心"), (250.0, "方舟投資"), (313.0, "設定"))]
        self.assertEqual(explore.tab_row(explore.tab_candidates(placed, self.BOUNDS)),
                         ["策略", "自選", "運算", "活動中心", "方舟投資", "設定"])

    def test_大盤頁的數據列不被當成tab(self):
        placed = [(24.0, 700.0, "成交量(億)", "AXStaticText"),
                  (144.0, 700.0, "9,421.57", "AXStaticText"),
                  (224.0, 700.0, "昨量(億)", "AXStaticText"),
                  (330.0, 700.0, "12,002.13", "AXStaticText")]
        self.assertEqual(explore.tab_row(explore.tab_candidates(placed, self.BOUNDS)), [])

    def test_底部的按鈕但不足三個不算(self):
        placed = [(101.0, 860.0, "加權指數", "AXButton"),
                  (199.0, 860.0, "櫃買指數", "AXButton")]
        self.assertEqual(explore.tab_row(explore.tab_candidates(placed, self.BOUNDS)), [])

    def test_中段的按鈕列不算(self):
        placed = [(x, 358.0, name, "AXButton") for x, name in
                  ((0.0, "ETF價值區"), (111.0, "ETF升溫區"), (222.0, "美股價值區"))]
        self.assertEqual(explore.tab_candidates(placed, self.BOUNDS), [])


class TestNextPaths(unittest.TestCase):
    def test_不重複走已在路徑上的元素(self):
        """按下自己所在頁的名字會原地打轉。"""
        nxt = explore.next_paths(("方舟投資",), ["方舟投資", "達人觀點"], set(), max_depth=3)
        self.assertEqual(nxt, [("方舟投資", "達人觀點")])

    def test_深度上限(self):
        nxt = explore.next_paths(("a", "b", "c"), ["d"], set(), max_depth=3)
        self.assertEqual(nxt, [])

    def test_跳過黑名單(self):
        nxt = explore.next_paths((), ["立即訂閱", "策略"], set(), max_depth=3)
        self.assertEqual(nxt, [("策略",)])

    def test_跳過已知的動作型元素(self):
        nxt = explore.next_paths(("運算",), ["重新整理"], {("運算", "重新整理")}, max_depth=3)
        self.assertEqual(nxt, [])


class TestDeadRoots(unittest.TestCase):
    """App 會記住子分頁的選擇：走過「自選›美股庫存」（空的）之後，
    每次回到自選都落在那頁，自選底下其他兄弟節點就都走不到了。
    這時該放棄的是那一棵子樹，不是整趟探索——否則運算／活動中心／方舟投資
    都還沒走到就被殺掉。"""

    def test_某根走死不影響其他根(self):
        misses = {"自選": 8}
        self.assertTrue(explore.root_is_dead(("自選", "股價"), misses, limit=8))
        self.assertFalse(explore.root_is_dead(("運算", "離職 倒數"), misses, limit=8))

    def test_未達上限不算死(self):
        self.assertFalse(explore.root_is_dead(("自選", "股價"), {"自選": 7}, limit=8))

    def test_空路徑不算死(self):
        self.assertFalse(explore.root_is_dead((), {"自選": 99}, limit=8))


class TestBuildMap(unittest.TestCase):
    def test_map可JSON往返(self):
        pages = [explore.Page(path=("運算",), fingerprint="a", texts=("風控",),
                              elements=(explore.Element("離職倒數", "AXButton",
                                                        explore.NAVIGATION),))]
        app_map = explore.build_map(pages, "3.2.1", "2026-08-06T19:00:00")
        restored = json.loads(json.dumps(app_map, ensure_ascii=False))
        self.assertEqual(restored["pages"][0]["path"], ["運算"])
        self.assertEqual(restored["pages"][0]["elements"][0]["name"], "離職倒數")


if __name__ == "__main__":
    unittest.main()
