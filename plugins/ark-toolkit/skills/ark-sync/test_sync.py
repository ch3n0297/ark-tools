"""ark-sync 純邏輯測試（不需要 ARK 執行中，任何平台可跑）"""
import json
import os
import tempfile
import unittest

import ark
import sync

# 調節庫存頁真實樣本 ------------------------------------------------------
# 欄位順序（表頭）：股票名稱, 代號, [位階], 種類, [建議調節金額], [建議調節股數],
#                   總成本 / 持有股數, 總市值, 總損益 / 報酬率, 今日損益, 成本均價
# 位階與建議調節欄位是「可選的」——App 未算出建議時整組消失，因此只能從尾端定位。

WITH_SUGGEST = "元大台灣50, 0050, 價值, 現\n股, ≥ 1,343, ≥ 13, 1,645\n17, 1,756, +111\n+6.71%, -9, 96.76"
NO_TIER = "元大中型100, 0051, 現\n股, ≥ 141, ≥ 1, 266\n2, 282, +16\n+5.83%, +2, 133"
NO_SUGGEST = "元大台灣50, 0050, 價值, 現\n股, 1,655\n17, 1,765, +110\n+6.63%, +54, 97.35"
BARE = "期元大S&P黃金, 00635U, 現\n股, 1,595\n29, 1,248, -346\n-21.72%, +25, 54.97"
BIG = "台積電, 2330, 價值, 現\n股, ≥ 83,300, ≥ 35, 103,119\n50, 119,000, +15,881\n+15.4%, -1,250, 2,062.38"
# 一檔可同時掛兩個位階（0057 富邦摩台實例）。價值＝可買、升溫＝該調節，
# 兩者同時亮起是最需要人工判斷的矛盾訊號，絕不能被讀成「沒有位階」。
DUAL_TIER = ("富邦摩台, 0057, 價值, 升溫, 現\n股, ≥ 2,456, ≥ 8, 30,905"
             "\n100, 30,755, -150\n-0.49%, +12, 309.05")


class TestParseHolding(unittest.TestCase):
    def test_有位階有建議調節(self):
        h = sync.parse_holding(WITH_SUGGEST)
        self.assertEqual((h.code, h.qty, h.price), ("0050", 17, 96.76))
        self.assertEqual(h.tiers, ("價值",))
        self.assertEqual((h.suggest_amount, h.suggest_qty), (1343.0, 13))

    def test_無位階有建議調節(self):
        h = sync.parse_holding(NO_TIER)
        self.assertEqual((h.code, h.qty, h.price), ("0051", 2, 133.0))
        self.assertEqual(h.tiers, ())
        self.assertEqual((h.suggest_amount, h.suggest_qty), (141.0, 1))

    def test_有位階無建議調節(self):
        h = sync.parse_holding(NO_SUGGEST)
        self.assertEqual((h.code, h.qty, h.price), ("0050", 17, 97.35))
        self.assertEqual(h.tiers, ("價值",))
        self.assertIsNone(h.suggest_qty)

    def test_無位階無建議調節(self):
        h = sync.parse_holding(BARE)
        self.assertEqual((h.code, h.qty, h.price), ("00635U", 29, 54.97))
        self.assertEqual(h.tiers, ())
        self.assertIsNone(h.suggest_qty)

    def test_一檔可以有兩個位階(self):
        h = sync.parse_holding(DUAL_TIER)
        self.assertEqual(h.tiers, ("價值", "升溫"))

    def test_兩個位階不影響其餘欄位(self):
        """位階欄多一格會把後面全部推移，尾端定位必須不受影響"""
        h = sync.parse_holding(DUAL_TIER)
        self.assertEqual((h.code, h.qty, h.price), ("0057", 100, 309.05))
        self.assertEqual((h.suggest_amount, h.suggest_qty), (2456.0, 8))
        self.assertEqual(h.value, 30755.0)

    def test_完整數值欄位(self):
        h = sync.parse_holding(BIG)
        self.assertEqual(h.cost, 103119.0)
        self.assertEqual(h.value, 119000.0)
        self.assertEqual(h.pnl, 15881.0)
        self.assertEqual(h.roi, 15.4)
        self.assertEqual(h.today_pnl, -1250.0)
        self.assertEqual((h.suggest_amount, h.suggest_qty), (83300.0, 35))

    def test_負報酬率(self):
        h = sync.parse_holding(BARE)
        self.assertEqual(h.roi, -21.72)
        self.assertEqual(h.pnl, -346.0)

    def test_非持股列回傳_None(self):
        for s in ("成交均價 (台幣)", "總共 13 檔", "", "建議調節股數"):
            self.assertIsNone(sync.parse_holding(s), s)


class TestParseEditRow(unittest.TestCase):
    def test_基本(self):
        self.assertEqual(
            sync.parse_edit_row("兆豐洲際半導體, 00911, 現股, 15, 55.47"), ("00911", 15, 55.47))

    def test_千分位均價(self):
        self.assertEqual(
            sync.parse_edit_row("台積電, 2330, 現股, 50, 2,062.38"), ("2330", 50, 2062.38))

    def test_排序鈕的_description_不可誤判為列(self):
        self.assertIsNone(sync.parse_edit_row("重新排列台積電, 2330, 現股, 50, 2,062.38"))

    def test_非列資料(self):
        self.assertIsNone(sync.parse_edit_row("總共 13 檔"))


class TestSanityCheck(unittest.TestCase):
    """讀到 0 檔但 App 說有 N 檔 —— 解析失效，必須報錯而非當成空庫存"""

    def test_解析全失敗時報錯(self):
        with self.assertRaises(sync.ParseFailed):
            sync.check_parsed({}, 13)

    def test_讀到的檔數少於宣告時報錯(self):
        with self.assertRaises(sync.ParseFailed):
            sync.check_parsed({"0050": None}, 13)

    def test_檔數相符時通過(self):
        sync.check_parsed({f"{i:04d}": None for i in range(13)}, 13)

    def test_App_未宣告檔數時略過檢查(self):
        sync.check_parsed({}, None)

    def test_真的是空庫存且宣告為零(self):
        sync.check_parsed({}, 0)


class TestSyncIsSafe(unittest.TestCase):
    """自動同步的安全閘。

    Shioaji 登入失敗時 read_shioaji_positions 會回傳空 dict，
    照 diff 邏輯就是「把 ARK 全部刪光」——與 check_parsed 防的是同一類
    「失敗偽裝成成功」，只是這次發生在寫入端，代價是整份庫存。
    """

    ARK = {f"{i:04d}": (10, 50.0) for i in range(10)}

    def test_來源讀到空的一律拒絕(self):
        ok, reason = sync.sync_is_safe(self.ARK, {})
        self.assertFalse(ok)
        self.assertIn("0 檔", reason)

    def test_要刪掉超過半數時拒絕(self):
        target = {k: v for k, v in list(self.ARK.items())[:4]}   # 刪 6 / 10
        ok, reason = sync.sync_is_safe(self.ARK, target)
        self.assertFalse(ok)
        self.assertIn("刪除", reason)

    def test_刪除未過半時通過(self):
        target = {k: v for k, v in list(self.ARK.items())[:6]}   # 刪 4 / 10
        ok, _reason = sync.sync_is_safe(self.ARK, target)
        self.assertTrue(ok)

    def test_只新增不刪除時通過(self):
        target = dict(self.ARK, **{"9999": (1, 1.0)})
        ok, _reason = sync.sync_is_safe(self.ARK, target)
        self.assertTrue(ok)

    def test_ARK_本來就是空的不受刪除比例限制(self):
        ok, _reason = sync.sync_is_safe({}, {"0050": (1, 1.0)})
        self.assertTrue(ok)

    def test_雙空不可視為一致(self):
        """來源讀失敗＋ARK 也是空的：看似一致，其實是失敗偽裝成成功"""
        ok, reason = sync.sync_is_safe({}, {})
        self.assertFalse(ok)
        self.assertIn("0 檔", reason)

    def test_未開允許刪除時_刪除過半不攔下新增與更新(self):
        """刪除根本不會執行（需 --allow-delete），不能連安全的新增／更新一起攔下"""
        target = {k: v for k, v in list(self.ARK.items())[:4]}   # 名義上刪 6 / 10
        ok, _reason = sync.sync_is_safe(self.ARK, target, allow_delete=False)
        self.assertTrue(ok)

    def test_未開允許刪除時_來源讀空仍拒絕(self):
        """來源回空多半是登入失敗——就算什麼都不會執行，也要把訊號留給使用者"""
        ok, reason = sync.sync_is_safe(self.ARK, {}, allow_delete=False)
        self.assertFalse(ok)
        self.assertIn("0 檔", reason)


class TestSyncLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sync-log.jsonl")
        self.addCleanup(self.dir.cleanup)

    def test_沒有紀錄時回傳_None(self):
        self.assertIsNone(ark.last_sync(self.path))

    def test_寫入後讀得回最後一筆(self):
        ark.append_sync_log({"ts": "2026-08-07T10:00:00", "ark_count": 11}, self.path)
        ark.append_sync_log({"ts": "2026-08-07T12:00:00", "ark_count": 13}, self.path)
        self.assertEqual(ark.last_sync(self.path)["ark_count"], 13)

    def test_一行一筆不覆寫(self):
        for i in range(3):
            ark.append_sync_log({"ts": f"t{i}", "ark_count": i}, self.path)
        with open(self.path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([r["ark_count"] for r in rows], [0, 1, 2])

    def test_壞掉的行不會讓讀取整個失效(self):
        """log 是輔助資訊，不該因為一行壞掉就讓主流程掛掉"""
        ark.append_sync_log({"ts": "t0", "ark_count": 9}, self.path)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{壞掉的 json\n")
        self.assertEqual(ark.last_sync(self.path)["ark_count"], 9)


class TestDriftSinceLastSync(unittest.TestCase):
    def test_沒有紀錄時說明從未同步(self):
        self.assertIn("沒有", ark.drift_since_last_sync(None, 13))

    def test_檔數相同時無漂移(self):
        self.assertIsNone(ark.drift_since_last_sync({"ts": "t", "ark_count": 13}, 13))

    def test_檔數不同時指出差異(self):
        msg = ark.drift_since_last_sync({"ts": "2026-08-07T10:00:00", "ark_count": 13}, 11)
        self.assertIn("13", msg)
        self.assertIn("11", msg)
        self.assertIn("2026-08-07T10:00:00", msg)

    def test_紀錄缺少檔數時不誤判為漂移(self):
        self.assertIsNone(ark.drift_since_last_sync({"ts": "t"}, 13))


class TestPlanChanges(unittest.TestCase):
    def test_完全一致時無動作(self):
        ark = {"2330": (50, 2062.38), "0050": (17, 96.76)}
        self.assertEqual(sync.plan_changes(ark, dict(ark)), [])

    def test_股數不同要更新(self):
        plan = sync.plan_changes({"2330": (40, 2062.38)}, {"2330": (50, 2062.38)})
        self.assertEqual(plan, [("update", "2330", 50, 2062.38, (40, 2062.38))])

    def test_均價不同要更新(self):
        self.assertEqual(
            sync.plan_changes({"0050": (17, 97.35)}, {"0050": (17, 96.76)})[0][0], "update")

    def test_ARK_缺少要新增(self):
        self.assertEqual(sync.plan_changes({}, {"0053": (2, 245.0)}),
                         [("add", "0053", 2, 245.0, None)])

    def test_ARK_多出要刪除(self):
        self.assertEqual(sync.plan_changes({"9999": (1, 10.0)}, {}),
                         [("delete", "9999", 0, 0.0, (1, 10.0))])

    def test_浮點容差內視為一致(self):
        self.assertEqual(sync.plan_changes({"0050": (17, 96.760001)}, {"0050": (17, 96.76)}), [])

    def test_動作依代號排序以確保可預期(self):
        plan = sync.plan_changes({}, {"2330": (1, 1.0), "0050": (1, 1.0)})
        self.assertEqual([p[1] for p in plan], ["0050", "2330"])


class FakeField:
    def __init__(self, value=""):
        self.value = value
        self.typed = []

    def type(self, s):
        self.value += s          # 打字是「插入」，不是「取代」——這正是危險所在
        self.typed.append(s)


class FakeAX:
    """最小的 ax 替身。`clearable` 為 False 時模擬 ARK 運算頁：
    ⊗ 按了回傳成功但值不變（AXPress 假成功），退格也打不進去。"""

    def __init__(self, value="30,000", clearable=True):
        self.field = FakeField(value)
        self.clearable = clearable

    def window(self, pid):
        return "window"

    def text_fields(self, root):
        return [self.field]

    def attr(self, el, name):
        if el is self.field and name == "AXValue":
            return self.field.value
        if name in ("AXParent", "AXChildren"):
            return [] if name == "AXChildren" else "parent"
        return None

    def press(self, el):
        pass

    def backspace(self, pid, n):
        if self.clearable:
            self.field.value = ""

    def keystroke(self, pid, s):
        self.field.type(s)


class TestFillFieldSafety(unittest.TestCase):
    def test_清空成功才寫入(self):
        fake = FakeAX(clearable=True)
        self.assertTrue(ark.fill_field(fake, 0, 0, "30000"))
        self.assertEqual(fake.field.value, "30000")

    def test_清不掉時絕不打字(self):
        """ARK 運算頁用自製數字鍵盤，⊗ 與退格都進不去。往沒清空的欄位打字會
        把文字插在游標處——30,000 曾因此變成 30,0300000，讓 ARK 依三億現金
        給出完全相反的建議。寧可回報失敗，也不能留下一個更錯的數字。"""
        fake = FakeAX(value="30,000", clearable=False)
        self.assertFalse(ark.fill_field(fake, 0, 0, "30000"))
        self.assertEqual(fake.field.value, "30,000")     # 原值原封不動
        self.assertEqual(fake.field.typed, [])           # 一個字都沒打

    def test_原本就空的欄位可直接寫入(self):
        fake = FakeAX(value="", clearable=False)
        self.assertTrue(ark.fill_field(fake, 0, 0, "30000"))
        self.assertEqual(fake.field.value, "30000")


class TestKeypadGeometry(unittest.TestCase):
    """運算頁的數字鍵盤不在 AX tree 裡，只能算座標點。
    以下期望值全部量自實機（視窗 pos=(35,293) size=(288,545)，1512px 螢幕）。"""

    POS, SIZE = (35.0, 293.0), (288.0, 545.0)

    def at(self, key):
        return ark.keypad_point(self.POS, self.SIZE, key)

    def test_四角鍵位(self):
        self.assertEqual(tuple(round(v) for v in self.at("7")), (71, 680))
        self.assertEqual(tuple(round(v) for v in self.at("⌫")), (287, 680))
        self.assertEqual(tuple(round(v) for v in self.at("AC")), (71, 813))
        self.assertEqual(tuple(round(v) for v in self.at("確定")), (287, 813))

    def test_中間鍵位(self):
        self.assertEqual(tuple(round(v) for v in self.at("5")), (143, 725))
        self.assertEqual(tuple(round(v) for v in self.at("0")), (143, 813))

    def test_所有鍵都落在視窗內(self):
        for row in ark.KEYPAD_KEYS:
            for key in row:
                x, y = self.at(key)
                self.assertTrue(self.POS[0] <= x <= self.POS[0] + self.SIZE[0], key)
                self.assertTrue(self.POS[1] <= y <= self.POS[1] + self.SIZE[1], key)

    def test_鍵盤貼齊視窗底部(self):
        """視窗變高時鍵盤跟著往下，不能用固定的絕對座標"""
        taller = ark.keypad_point(self.POS, (288.0, 645.0), "確定")
        self.assertAlmostEqual(taller[1] - self.at("確定")[1], 100.0, places=1)

    def test_未知鍵拒絕(self):
        with self.assertRaises(KeyError):
            self.at("%")


class TestPostureCash(unittest.TestCase):
    """運算頁現金欄的值。官方語意：輸入「所有的錢」，含緊急備用金與薪水，
    且未交割款要自行預計（總資源＝現金＋T+2 內可動用資金）。"""

    SETTLE = {"today": 0.0, "t1": 960.0, "t2": 0.0}

    def test_券商現金加未交割款(self):
        self.assertEqual(sync.posture_cash(14673.0, self.SETTLE, 0.0), 15633.0)

    def test_加上帳戶外現金(self):
        """緊急備用金與薪水不在券商帳上，但官方定義要求納入"""
        self.assertEqual(sync.posture_cash(14673.0, self.SETTLE, 14367.0), 30000.0)

    def test_未交割款為負時扣除(self):
        """當日買進會讓 T+1 為負——先扣掉才是真的可動用資金"""
        settle = {"today": 0.0, "t1": -35000.0, "t2": 0.0}
        self.assertEqual(sync.posture_cash(50000.0, settle, 0.0), 15000.0)

    def test_取整數(self):
        """ARK 的欄位吃整數字串，帶小數點會填不進去"""
        self.assertEqual(sync.posture_cash(14673.4, self.SETTLE, 0.0), 15633.0)
        self.assertIsInstance(sync.posture_cash(14673.4, self.SETTLE, 0.0), float)

    def test_不得為負(self):
        """未交割款吃掉全部現金時填 0，負數 ARK 收不了"""
        settle = {"today": 0.0, "t1": -99999.0, "t2": 0.0}
        self.assertEqual(sync.posture_cash(1000.0, settle, 0.0), 0.0)


class TestCashStepWhenNoChanges(unittest.TestCase):
    """庫存無變更時仍要不要同步現金。

    現金與庫存是兩件獨立的事：庫存一致不代表現金欄也對。實例
    （2026-08-14）——換股當天庫存同步成功、現金欄寫入失敗，重跑時因為
    庫存已一致就提早收工，現金永遠不會補上，欄位停在舊值直到有人察覺。
    """

    def test_要求同步現金時仍要做(self):
        self.assertTrue(sync.cash_step_needed(with_cash=True, dry_run=False))

    def test_未要求就不做(self):
        self.assertFalse(sync.cash_step_needed(with_cash=False, dry_run=False))

    def test_dry_run_不寫入(self):
        """預演不能真的去點 App 的數字鍵盤"""
        self.assertFalse(sync.cash_step_needed(with_cash=True, dry_run=True))


class TestPlatformGuard(unittest.TestCase):
    def test_非_macOS_應報錯(self):
        for p in ("linux", "win32"):
            with self.assertRaises(sync.UnsupportedPlatform):
                sync.check_platform(p)

    def test_macOS_通過(self):
        sync.check_platform("darwin")


class FakeAx:
    """模擬 ax 模組：press／click 可各自設定有效與否，重啟可設定是否治癒。

    頁面模型：window() 回傳頁名字串，by_desc 依頁面的 desc 集合回應；
    press/click 有效時把「自選／運算」的按壓轉成換頁。
    """
    PAGES = {"watchlist": {"自選", "運算", "調節 庫存", "布局 自選"},
             "posture": {"自選", "運算", "風控 運算"}}

    def __init__(self, page="posture", press_effective=True,
                 click_effective=True, restart_fixes=False,
                 press_needs_scroll=False):
        self.page = page
        self.press_effective = press_effective
        self.click_effective = click_effective
        self.restart_fixes = restart_fixes
        self.press_needs_scroll = press_needs_scroll
        self.restarted = False
        self.clicks = []
        self.performed = []
        self._last_pressed = None

    def window(self, pid):
        return self.page

    def by_desc(self, w, text):
        page = w if isinstance(w, str) else self.page
        return [f"EL:{text}"] if text in self.PAGES[page] else []

    def _act(self, el):
        name = el.split(":", 1)[1]
        if name == "自選":
            self.page = "watchlist"
        elif name == "運算":
            self.page = "posture"

    def perform(self, el, action):
        self.performed.append((el, action))
        return 0

    def press(self, el):
        self._last_pressed = el
        scrolled = (el, "AXScrollToVisible") in self.performed
        if self.press_effective and (scrolled or not self.press_needs_scroll):
            self._act(el)
        return 0                                   # AXPress 永遠「成功」

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.click_effective and self._last_pressed:
            self._act(self._last_pressed)

    def point(self, el):
        return (0.0, 0.0)

    def size(self, el):
        return (10.0, 10.0)

    def restart_app(self):
        self.restarted = True
        self.page = "watchlist"
        if self.restart_fixes:
            self.press_effective = True
        return 99


class TestStrategyRowParsing(unittest.TestCase):
    def test_解析列代號(self):
        self.assertEqual(ark.parse_strategy_row_code("兆豐洲際半導體, 00911, 全球, 4"),
                         "00911")
        self.assertEqual(ark.parse_strategy_row_code("元大台灣50正2, 00631L, 台灣, 10"),
                         "00631L")

    def test_非列格式回None(self):
        for t in ("股票名稱", "54.6", "▼0.6(-1.09%)", "", None):
            self.assertIsNone(ark.parse_strategy_row_code(t))

    def test_解析檔數文字(self):
        self.assertEqual(ark.parse_count("共選入9檔", "共選入"), 9)
        self.assertEqual(ark.parse_count("❮取代❯完成後共 9 檔", "完成後共"), 9)
        self.assertIsNone(ark.parse_count("❮取代❯完成後共 - 檔", "完成後共"))
        self.assertIsNone(ark.parse_count("共選入9檔", "完成後共"))   # marker 不符


class TestMonthTotal(unittest.TestCase):
    def test_取第一個純金額文字(self):
        texts = ["總計當月已實現報酬", "4,174 元", "日期", "報酬金額",
                 "08月11日, 4,174 元"]
        self.assertEqual(ark.month_total_from_texts(texts), 4174.0)

    def test_日期列不會被誤認(self):
        self.assertIsNone(ark.month_total_from_texts(["08月11日, 4,174 元"]))

    def test_零元(self):
        self.assertEqual(ark.month_total_from_texts(["0 元"]), 0.0)


class TestPressVerified(unittest.TestCase):
    def pred(self, fake):
        return lambda w: bool(fake.by_desc(w, "調節 庫存"))

    def test_press有效直接通過(self):
        fake = FakeAx()
        w = ark.press_verified(fake, 1, "EL:自選", self.pred(fake), "去自選",
                               timeout=0.05)
        self.assertEqual(w, "watchlist")
        self.assertEqual(fake.clicks, [])          # 不需要 fallback

    def test_press假成功時座標點擊兜底(self):
        fake = FakeAx(press_effective=False)
        w = ark.press_verified(fake, 1, "EL:自選", self.pred(fake), "去自選",
                               timeout=0.05)
        self.assertEqual(w, "watchlist")
        self.assertEqual(len(fake.clicks), 1)

    def test_兩種方式都無效才報錯(self):
        fake = FakeAx(press_effective=False, click_effective=False)
        with self.assertRaises(RuntimeError):
            ark.press_verified(fake, 1, "EL:自選", self.pred(fake), "去自選",
                               timeout=0.05)

    def test_按之前先捲進可視範圍(self):
        # 離屏元素 AXPress 靜默失效、座標點擊點在視窗外——唯一救法是先捲進畫面
        fake = FakeAx(press_needs_scroll=True)
        w = ark.press_verified(fake, 1, "EL:自選", self.pred(fake), "去自選",
                               timeout=0.05)
        self.assertEqual(w, "watchlist")
        self.assertIn(("EL:自選", "AXScrollToVisible"), fake.performed)


class TestEnsureResponsive(unittest.TestCase):
    def test_UI健康時不重啟(self):
        fake = FakeAx()
        pid = ark.ensure_responsive(fake, 1, probe_timeout=0.05)
        self.assertEqual(pid, 1)
        self.assertFalse(fake.restarted)

    def test_殭屍態重啟後治癒(self):
        fake = FakeAx(press_effective=False, click_effective=False,
                      restart_fixes=True)
        pid = ark.ensure_responsive(fake, 1, probe_timeout=0.05)
        self.assertEqual(pid, 99)
        self.assertTrue(fake.restarted)

    def test_重啟仍無效則報錯(self):
        fake = FakeAx(press_effective=False, click_effective=False)
        with self.assertRaises(RuntimeError):
            ark.ensure_responsive(fake, 1, probe_timeout=0.05)
        self.assertTrue(fake.restarted)            # 有試過重啟才放棄


SHIOAJI_CFG = {"version": 2, "accounts": [{"type": "shioaji", "name": "永豐"}]}
FILE_CFG = {"version": 2, "accounts": [
    {"type": "file", "name": "國泰", "path": "p.csv", "columns": {}}]}
FEES = {"include_dividends": False, "include_fees": True}


class TestSyncLogEntry(unittest.TestCase):
    def test_紀錄含均價口徑(self):
        entry = sync.log_entry(current={"2330": (18, 2282.61)}, target={"2330": (18, 2285.78)},
                               applied=[["update", "2330"]], ok=1, fail=0, after=7,
                               cfg={**SHIOAJI_CFG, "cost_basis": FEES}, now="2026-08-22T17:00:00")
        self.assertEqual(entry["cost_basis"], "不含息、含手續費")
        self.assertEqual(entry["ark_count"], 7)
        self.assertEqual(entry["ts"], "2026-08-22T17:00:00")

    def test_讀不到收尾檔數就不寫_ark_count(self):
        entry = sync.log_entry(current={}, target={"2330": (18, 2282.61)}, applied=[],
                               ok=0, fail=1, after=None, cfg=SHIOAJI_CFG, now="t")
        self.assertNotIn("ark_count", entry)
        self.assertEqual(entry["cost_basis"], "不含息、不含手續費")


if __name__ == "__main__":
    unittest.main()
