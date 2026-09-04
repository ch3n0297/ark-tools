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

    def __init__(self, value="30,000", clearable=True, keyboard_delay=0):
        self.field = FakeField(value)
        self.clearable = clearable
        self.keyboard_delay = keyboard_delay      # 還要幾次查詢鍵盤才浮起
        self.keyboard = keyboard_delay == 0

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

    def keyboard_up(self, pid):
        if not self.keyboard:
            self.keyboard_delay -= 1
            self.keyboard = self.keyboard_delay <= 0
            return None
        return (82, 368, 442, 419)

    def dismiss_keyboard(self, pid):
        self.keyboard = False

    def perform(self, el, action):
        return 0

    def keystroke(self, pid, s):
        # 鍵盤還沒浮起時前面的字元會被吞掉——1.8.2 實錄：「106」進去只剩「6」
        self.field.type(s[-1:] if not self.keyboard else s)


class TestFillFieldSafety(unittest.TestCase):
    def test_清空成功才寫入(self):
        fake = FakeAX(clearable=True)
        self.assertTrue(ark.fill_field(fake, 0, 0, "30000"))
        self.assertEqual(fake.field.value, "30000")

    def test_鍵盤延遲浮起時要等它_不然前綴被吞掉(self):
        """App 1.8.2 的軟鍵盤是獨立視窗，彈出有延遲；press 後固定睡 0.4 秒就打字，
        「106」會只剩「6」（2026-09-02 實錄，同值寫入因此驗算不符被擋下）。"""
        from unittest import mock
        fake = FakeAX(value="", clearable=True, keyboard_delay=3)
        with mock.patch("time.sleep"):
            self.assertTrue(ark.fill_field(fake, 0, 0, "106"))
        self.assertEqual(fake.field.value, "106")

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
             "posture": {"自選", "運算", "風控 運算"},
             "splash": set()}                      # 冷啟動過場：什麼地標都沒有

    def __init__(self, page="posture", press_effective=True,
                 click_effective=True, restart_fixes=False,
                 press_needs_scroll=False, splash_polls=0):
        self.page = page
        self.splash_polls = splash_polls           # 重啟後要輪詢幾次 window() 才過完過場
        self.press_effective = press_effective
        self.click_effective = click_effective
        self.restart_fixes = restart_fixes
        self.press_needs_scroll = press_needs_scroll
        self.restarted = False
        self.clicks = []
        self.performed = []
        self._last_pressed = None

    def window(self, pid):
        if self.page == "splash":
            self.splash_polls -= 1
            if self.splash_polls <= 0:
                self.page = "watchlist"
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
        self.page = "splash" if self.splash_polls > 0 else "watchlist"
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

    def test_重啟後過場畫面沒有tab_bar時要等而不是立刻判死(self):
        # 2026-09-02 實錄：restart_app 在 AXWindows 一讀得到就回來，此時還是過場畫面，
        # probe 找不到「自選」也找不到「back」立刻回 False → 誤報「重啟後仍無效」
        from unittest import mock
        fake = FakeAx(press_effective=False, click_effective=False,
                      restart_fixes=True, splash_polls=3)
        with mock.patch("time.sleep"):
            pid = ark.ensure_responsive(fake, 1, probe_timeout=0.05)
        self.assertEqual(pid, 99)


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


# v1.8.0（2026-08-26）起，種類後多了「股價, 漲跌幅」兩欄，漲跌幅形如「▲25(1.05%)」；
# 建議調節兩欄仍是可選的、接在漲跌幅之後（依表頭順序）。舊版列必須照常可解析——
# 使用者的 App 不一定同時更新。前兩筆取自實機。
V180_NO_SUGGEST = "台積電, 2330, 價值, 現\n股, 2,400, ▲25(1.05%), 41,145\n18, 43,200, +2,056\n+5%, +450, 2,285.78"
V180_DOWN = "元大電子, 0053, 價值, 現\n股, 236.15, ▼3.8(-1.58%), 491\n2, 473, -18\n-3.81%, -7, 245.5"
# 有建議調節的 1.8.0 列尚未在實機出現（當日參考調節為 0），依表頭順序推定
V180_WITH_SUGGEST = ("富邦摩台, 0057, 價值, 升溫, 現\n股, 311.2, ▼0.15(-0.05%), ≥ 2,456, ≥ 8, 30,905"
                     "\n100, 30,755, -150\n-0.49%, +12, 309.05")


class TestParseHoldingV180(unittest.TestCase):
    def test_新增的股價與漲跌幅欄不被當成建議調節(self):
        h = sync.parse_holding(V180_NO_SUGGEST)
        self.assertEqual((h.code, h.qty, h.price), ("2330", 18, 2285.78))
        self.assertEqual(h.tiers, ("價值",))
        self.assertIsNone(h.suggest_qty)
        self.assertIsNone(h.suggest_amount)

    def test_尾端數值欄不受新欄影響(self):
        h = sync.parse_holding(V180_NO_SUGGEST)
        self.assertEqual((h.cost, h.value, h.pnl, h.roi, h.today_pnl),
                         (41145.0, 43200.0, 2056.0, 5.0, 450.0))

    def test_下跌的漲跌幅同樣略過(self):
        h = sync.parse_holding(V180_DOWN)
        self.assertEqual((h.code, h.qty, h.price, h.roi), ("0053", 2, 245.5, -3.81))

    def test_漲跌幅之後仍可接建議調節(self):
        h = sync.parse_holding(V180_WITH_SUGGEST)
        self.assertEqual(h.tiers, ("價值", "升溫"))
        self.assertEqual((h.suggest_amount, h.suggest_qty), (2456.0, 8))
        self.assertEqual((h.qty, h.price), (100, 309.05))

    def test_中段認不得回傳None而非拋例外(self):
        """欄位再變動時要走 looks_like_holding_row → ParseFailed 帶樣本，
        而不是 ValueError traceback 把 visible_codes 整個炸掉（1.8.0 實例）。"""
        odd = "甲, 0001, 現\n股, 1, 2, 3, 100\n1, 100, 0\n0%, 0, 100"
        self.assertIsNone(sync.parse_holding(odd))


class FakePostureAx:
    """模擬 1.8.x 的運算頁：輸入面板在固定儀表板下方的可捲區，App 預設捲到最底的
    摘要區。欄位被儀表板蓋住時 AXPress 不會開鍵盤，之後的座標點擊全部落到別處
    （實機曾因此點進 tab bar、跑到個股頁）。鍵盤座標用 keypad_point 反查。"""
    POS, SIZE = (35.0, 293.0), (288.0, 545.0)
    HIDDEN_Y, VISIBLE_Y = 166.0, 370.0

    def __init__(self, value="88,347", at_top=False):
        self.value = value
        self.at_top = at_top
        self.keypad_open = False
        self.buffer = ""
        self.events = []

    def window(self, pid):
        return "posture"

    def by_desc(self, w, text):
        return ["EL:" + text] if text in ("運算", "風控 運算", "持股配置建議") else []

    def perform(self, el, action):
        return 0

    def text_fields(self, root):
        return ["stock", "us", "cash", "usd"]

    def attr(self, el, name):
        return self.value if (el == "cash" and name == "AXValue") else None

    def point(self, el):
        if el == "cash":
            return (self.POS[0] + 189, self.POS[1] + (self.VISIBLE_Y if self.at_top else self.HIDDEN_Y))
        return self.POS

    def size(self, el):
        return self.SIZE

    def scroll_page(self, pid, action, tries=4):
        self.events.append(("scroll", action))
        if action == "AXScrollUpByPage" and not self.at_top:
            self.at_top = True
            return 0
        return None

    def press(self, el):
        if el == "cash":
            self.events.append(("press", "cash"))
            self.keypad_open = self.at_top          # 被儀表板蓋住時開不了鍵盤
            self.buffer = self.value.replace(",", "")
        return 0

    def click(self, x, y):
        self.events.append(("click", (x, y)))
        if not self.keypad_open:
            return                                  # 沒鍵盤：點到別的東西，欄位不動
        for row in ark.KEYPAD_KEYS:
            for key in row:
                kx, ky = ark.keypad_point(self.POS, self.SIZE, key)
                if abs(kx - x) < 0.5 and abs(ky - y) < 0.5:
                    self._key(key)
                    return

    def _key(self, key):
        if key == "AC":
            self.buffer = ""
        elif key == "確定":
            self.value = f"{int(self.buffer):,}" if self.buffer else ""
            self.keypad_open = False
        elif key.isdigit():
            self.buffer += key


class TestWritePostureCashScroll(unittest.TestCase):
    """1.8.x 運算頁改成「固定儀表板＋可捲內容」，輸入面板預設被捲出視野。"""

    def _write(self, fake, value):
        from unittest import mock
        with mock.patch("time.sleep"):
            return ark.write_posture_cash(fake, 0, value)

    def test_輸入面板捲出視野時先捲到頂再寫(self):
        fake = FakePostureAx(at_top=False)
        self.assertTrue(self._write(fake, 81727.0))
        self.assertEqual(fake.value, "81,727")
        first_scroll = fake.events.index(("scroll", "AXScrollUpByPage"))
        first_press = fake.events.index(("press", "cash"))
        self.assertLess(first_scroll, first_press)

    def test_已在頂端時照常寫入(self):
        fake = FakePostureAx(at_top=True)
        self.assertTrue(self._write(fake, 81727.0))
        self.assertEqual(fake.value, "81,727")


if __name__ == "__main__":
    unittest.main()


class FakePopupAx:
    """新增／編輯彈窗開著；右上角 X 在不在 AX tree 由 has_close 決定。"""

    def __init__(self, has_close):
        self.has_close = has_close
        self.open = True
        self.presses, self.clicks = [], []

    def window(self, pid):
        return "win"

    def by_desc(self, w, text):
        if text == "新增持股":
            return [] if self.open else ["add"]
        if text == "popup close" and self.has_close and self.open:
            return ["x"]
        return []

    def press(self, el):
        self.presses.append(el)
        if el == "x":
            self.open = False
        return 0

    def point(self, el):
        return (77.0, 158.0)

    def click(self, x, y):
        self.clicks.append((x, y))


class TestClosePopup(unittest.TestCase):
    """1.8.2 實測（2026-09-02）：新增彈窗的 X 在 tree（desc=popup close，視窗相對 (248, 47)）。
    舊的座標 fallback x+345 是縮放 1.0 時量的，現在視窗 288 寬會點到別的 App。"""

    def test_X在tree就按它不點座標(self):
        from unittest import mock
        fake = FakePopupAx(has_close=True)
        with mock.patch("time.sleep"):
            self.assertTrue(sync._close_popup(fake, 0))
        self.assertEqual(fake.presses, ["x"])
        self.assertEqual(fake.clicks, [])

    def test_X不在tree時回報失敗而不亂點(self):
        from unittest import mock
        fake = FakePopupAx(has_close=False)
        with mock.patch("time.sleep"):
            self.assertFalse(sync._close_popup(fake, 0))
        self.assertEqual(fake.clicks, [])


class FakeCalcPageAx:
    """位階運算機各階段的畫面：input＝輸入頁，result＝1.8.2 留在運算機的結果頁，layout＝舊版跳轉。"""

    def __init__(self, stage):
        self.stage = stage

    def by_desc(self, w, text):
        present = {
            "input": {"位階運算機", "AI運算今天可以買幾股"},
            "loading": {"位階運算機"},
            "result": {"位階運算機"},
            "layout": {"布局 自選", "位階股數"},
        }[self.stage]
        return ["el"] if text in present else []

    def descs(self, w, role=None):
        rows = {"result": ["位階運算機", "富邦科技, 0052, 價值, 62.05, ▼1(-1.59%), 62.12, -0.11%, 1, 90, 63, 5,631"],
                "loading": ["位階運算機", "運算中"]}.get(self.stage, [])
        return [("el", d) for d in rows]


class TestTierCalcDone(unittest.TestCase):
    """2026-09-02 實錄（App 1.8.2）：AI運算後不再跳布局自選，結果留在運算機頁；
    輸入頁與結果頁都有「位階運算機」標題，只能靠「輸入鈕消失＋結果列出現」判定。"""

    def test_輸入頁不算完成(self):
        self.assertFalse(ark.tier_calc_done(FakeCalcPageAx("input"), "w"))

    def test_運算中沒有結果列不算完成(self):
        self.assertFalse(ark.tier_calc_done(FakeCalcPageAx("loading"), "w"))

    def test_結果頁算完成(self):
        self.assertTrue(ark.tier_calc_done(FakeCalcPageAx("result"), "w"))

    def test_舊版跳轉布局自選也算完成(self):
        self.assertTrue(ark.tier_calc_done(FakeCalcPageAx("layout"), "w"))


class FakeIosFieldAx:
    """位階運算機的兩個 iOS 軟鍵盤欄位：每欄旁有 ⊗（calculator textfield cancel）。

    元素模型：("field", i)、("cancel", i) 同屬父元素 "form"。
    ⊗ 清空但掉焦點；AXPress 聚焦可設定失靈；退格可設定失靈；打字只在有焦點時生效。
    """

    def __init__(self, values, press_focus_works=True, backspace_works=True, typing_works=True):
        self.values = list(values)
        self.press_focus_works = press_focus_works
        self.backspace_works = backspace_works
        self.typing_works = typing_works
        self.focused = None
        self.clicks, self.presses = [], []

    def window(self, pid):
        return "w"

    def text_fields(self, w):
        return [("field", i) for i in range(len(self.values))]

    def attr(self, el, name):
        if el == "form" and name == "AXChildren":
            return [x for i in range(len(self.values)) for x in (("field", i), ("cancel", i))]
        kind, i = el
        return {"AXParent": "form",
                "AXRole": "AXTextField" if kind == "field" else "AXButton",
                "AXDescription": "calculator textfield cancel" if kind == "cancel" else "",
                "AXValue": self.values[i] if kind == "field" else None,
                "AXFocused": (self.focused == i) if kind == "field" else None}.get(name)

    def press(self, el):
        self.presses.append(el)
        kind, i = el
        if kind == "cancel":
            self.values[i] = ""
            self.focused = None
        elif self.press_focus_works:
            self.focused = i
        return 0

    def point(self, el):
        return (0.0, 20.0 * el[1])           # 每欄高 20，中心在 20i+10

    def size(self, el):
        return (100.0, 20.0)

    def click(self, x, y):
        self.clicks.append((x, y))
        self.focused = int(y // 20)          # 座標點擊一律能聚焦

    def keystroke(self, pid, s):
        if self.focused is None:
            return
        if s == "\r":
            self.focused = None
        elif s and set(s) == {"\x08"}:
            if self.backspace_works:
                self.values[self.focused] = self.values[self.focused][:-len(s)]
        elif self.typing_works:
            self.values[self.focused] = self.values[self.focused].replace(",", "") + s

    def backspace(self, pid, n):
        self.keystroke(pid, "\x08" * n)

    # 收鍵盤：焦點還在就當鍵盤浮著（write_ios_field 打完字要驗證收掉）
    def keyboard_up(self, pid):
        return (0, 0, 10, 10) if self.focused is not None else None

    def dismiss_keyboard(self, pid):
        self.keystroke(pid, "\r")

    def perform(self, el, action):
        if action == "AXCancel":
            self.focused = None
        return 0


class TestWriteIosField(unittest.TestCase):
    """2026-09-02 App 1.8.2 實錄：座標點擊聚焦後退格時靈時不靈（「2」退格再打「4」變「24」），
    有時連焦點都沒建立，位階運算機的檔數欄三次都寫不進去。⊗ 清空→AXPress 聚焦→打字 6/6 成功。"""

    def _write(self, fake, idx, digits):
        from unittest import mock
        with mock.patch("time.sleep"):
            return ark.write_ios_field(fake, 0, idx, digits)

    def test_清空鈕後AXPress聚焦打字_不動滑鼠(self):
        fake = FakeIosFieldAx(["16,637", "3"])
        self.assertTrue(self._write(fake, 1, "1"))
        self.assertEqual(fake.values, ["16,637", "1"])
        self.assertEqual(fake.clicks, [])

    def test_退格失靈也寫得進去(self):
        fake = FakeIosFieldAx(["16,637", "3"], backspace_works=False)
        self.assertTrue(self._write(fake, 1, "1"))
        self.assertEqual(fake.values[1], "1")

    def test_AXPress聚焦失敗才退回座標點擊(self):
        fake = FakeIosFieldAx(["16,637", "3"], press_focus_works=False)
        self.assertTrue(self._write(fake, 0, "20000"))
        self.assertEqual(fake.values[0], "20000")
        self.assertGreaterEqual(len(fake.clicks), 1)

    def test_完全寫不進去回False不拋(self):
        fake = FakeIosFieldAx(["16,637", "3"], typing_works=False)
        self.assertFalse(self._write(fake, 1, "1"))


class FakeKeyboardAx:
    """軟鍵盤浮起中：`\\r` 與欄位的 AXCancel 各自可設定有效與否。"""

    def __init__(self, cr_works=False, cancel_works=True, focused=1, nfields=2):
        self.cr_works = cr_works
        self.cancel_works = cancel_works
        self.focused = focused
        self.nfields = nfields
        self.up = True
        self.events = []

    def window(self, pid):
        return "w"

    def text_fields(self, w):
        return [("field", i) for i in range(self.nfields)]

    def attr(self, el, name):
        return (el[1] == self.focused) if name == "AXFocused" else None

    def keyboard_up(self, pid):
        return (194, 404, 342, 279) if self.up else None

    def dismiss_keyboard(self, pid):
        self.events.append("cr")
        if self.cr_works:
            self.up, self.focused = False, None

    def perform(self, el, action):
        self.events.append((el, action))
        if action == "AXCancel" and self.cancel_works:
            self.up, self.focused = False, None
        return 0


class TestDismissKeyboard(unittest.TestCase):
    """2026-09-02 App 1.8.2 實測：位階運算機的軟鍵盤是獨立的 layer-101 視窗
    （AXWindows 看不到它），`\\r` 收不掉，蓋住「AI運算」讓 AXPress 假成功。
    欄位的 AXCancel 收得掉，收掉後 AXPress 立刻生效。"""

    def _dismiss(self, fake):
        from unittest import mock
        with mock.patch("time.sleep"):
            return ark.dismiss_keyboard(fake, 0)

    def test_cr有效就不必再AXCancel(self):
        fake = FakeKeyboardAx(cr_works=True)
        self.assertTrue(self._dismiss(fake))
        self.assertEqual(fake.events, ["cr"])

    def test_cr無效時對聚焦欄位AXCancel(self):
        fake = FakeKeyboardAx(cr_works=False)
        self.assertTrue(self._dismiss(fake))
        self.assertIn((("field", 1), "AXCancel"), fake.events)

    def test_沒有欄位聚焦就對每個欄位都試(self):
        fake = FakeKeyboardAx(cr_works=False, focused=None)
        self.assertTrue(self._dismiss(fake))
        self.assertIn((("field", 0), "AXCancel"), fake.events)

    def test_都收不掉回False(self):
        fake = FakeKeyboardAx(cr_works=False, cancel_works=False)
        self.assertFalse(self._dismiss(fake))

    def test_鍵盤本來就沒浮起直接過(self):
        fake = FakeKeyboardAx()
        fake.up = False
        self.assertTrue(self._dismiss(fake))
        self.assertEqual(fake.events, [])


class FakeSaveAx:
    """編輯持股彈窗：兩個欄位已填好、總成本正確；鍵盤是否浮著與是否收得掉可設定。

    儲存鈕被鍵盤蓋住時 AXPress **回成功但無效**（彈窗不關），正是 1.8.1 的
    「儲存看似成功、下一檔全部寫空」的來源。
    """

    def __init__(self, kb_up=False, cr_works=True, cancel_works=True):
        self.kb_up = kb_up
        self.cr_works = cr_works
        self.cancel_works = cancel_works
        self.popup_open = True
        self.saved = False
        self.events = []

    def window(self, pid):
        return "w"

    def text_fields(self, w):
        return ["qty", "price"]

    def attr(self, el, name):
        if name == "AXValue":
            return {"qty": "106", "price": "53.6"}.get(el)
        if name == "AXFocused":
            return self.kb_up and el == "price"
        return None

    def descs(self, w, role=None):
        return [("el", "總成本：5,682 台幣")] if self.popup_open else []

    def by_desc(self, w, text):
        if text == "儲存" and self.popup_open:
            return ["save"]
        if text == "編輯持股" and self.popup_open:
            return ["popup"]
        if text == "popup close" and self.popup_open:
            return ["x"]
        if text == "新增持股" and not self.popup_open:
            return ["add"]
        return []

    def keyboard_up(self, pid):
        return (194, 404, 342, 279) if self.kb_up else None

    def dismiss_keyboard(self, pid):
        self.events.append("cr")
        if self.cr_works:
            self.kb_up = False

    def perform(self, el, action):
        self.events.append((el, action))
        if action == "AXCancel" and self.cancel_works:
            self.kb_up = False
        return 0

    def press(self, el):
        self.events.append(("press", el))
        if el in ("save", "x") and not self.kb_up:      # 鍵盤蓋住就假成功
            self.popup_open = False
        return 0


class TestVerifyAndSave(unittest.TestCase):
    """存檔前一定要確認鍵盤收掉：儲存鈕在彈窗底部，鍵盤蓋著時 AXPress 假成功
    （1.8.1 實錄：儲存看似成功，殘留的 first responder 讓下一檔全部寫空）。"""

    def _save(self, fake):
        from unittest import mock
        with mock.patch("time.sleep"):
            return sync._verify_and_save(fake, 0, 106, 53.6, "編輯持股")

    def test_沒有鍵盤時直接儲存(self):
        fake = FakeSaveAx(kb_up=False)
        self.assertTrue(self._save(fake))
        self.assertTrue(fake.popup_open is False)

    def test_鍵盤浮著要先收掉再儲存(self):
        fake = FakeSaveAx(kb_up=True, cr_works=False)
        self.assertTrue(self._save(fake))
        self.assertIn(("price", "AXCancel"), fake.events)

    def test_鍵盤收不掉就不儲存並回報失敗(self):
        fake = FakeSaveAx(kb_up=True, cr_works=False, cancel_works=False)
        self.assertFalse(self._save(fake))
        self.assertNotIn(("press", "save"), fake.events)


class TestCrossCheckWithServer(unittest.TestCase):
    """AX 讀到的庫存要和伺服器對一次帳才動手寫。

    parser 被 App 改版弄壞時，read_holdings 可能靜默少讀幾檔（1.8.0 的布局頁就這樣
    回過空 dict）；差異計算會據此判斷「ARK 少了這幾檔」而去新增，把好好的資料弄亂。
    伺服器那份是同一批資料的獨立來源，不一致就該停手讓人看一眼。
    API 讀不到（token 過期、沒網路）**不是**停手的理由——每日同步不能被一個輔助檢查綁架。
    """

    def test_一致就放行(self):
        ok, msg = sync.cross_check({"0050": (53, 106.28)}, {"0050": (53, 106.28)})
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_股數不一致要擋下並指名是哪一檔(self):
        ok, msg = sync.cross_check({"0050": (53, 106.28)}, {"0050": (63, 106.28)})
        self.assertFalse(ok)
        self.assertIn("0050", msg)

    def test_少讀一檔要擋下(self):
        ok, msg = sync.cross_check({"0050": (53, 106.28)},
                                   {"0050": (53, 106.28), "0056": (106, 53.6)})
        self.assertFalse(ok)
        self.assertIn("0056", msg)

    def test_均價容差內視為一致(self):
        # 兩邊都是「成交均價」但小數位可能差一點，用與 plan_changes 相同的容差
        ok, _msg = sync.cross_check({"0050": (53, 106.28)}, {"0050": (53, 106.281)})
        self.assertTrue(ok)

    def test_伺服器讀不到就放行不擋(self):
        ok, msg = sync.cross_check({"0050": (53, 106.28)}, None)
        self.assertTrue(ok)
        self.assertIsNone(msg)
