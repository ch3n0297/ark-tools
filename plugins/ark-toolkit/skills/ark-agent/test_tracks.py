"""軌道歸屬純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import json
import os
import tempfile
import unittest

import tracks


def P(qty, last_price, pnl=0.0, avg_price=None):
    return {"qty": qty, "last_price": last_price, "pnl": pnl,
            "avg_price": avg_price if avg_price is not None else last_price}


POSITIONS = {
    "2330": P(20, 2380.0, pnl=6145.0),      # 47,600
    "2308": P(30, 1815.0, pnl=-464.0),      # 54,450
    "0050": P(900, 104.25, pnl=6741.0),     # 93,825
    "0053": P(2, 237.0, pnl=-17.0),         # 474  ← 低於門檻
    "00911": P(15, 55.2, pnl=-7.0),         # 828  ← 低於門檻
}
ASSIGNED = {"2330": tracks.SATELLITE, "2308": tracks.INHERITED}
MIN_TRADE = 3000.0


class TestResolve(unittest.TestCase):
    def resolve(self, assigned=None, positions=None, min_trade=MIN_TRADE):
        return tracks.resolve(ASSIGNED if assigned is None else assigned,
                              POSITIONS if positions is None else positions,
                              min_trade)

    def test_未指定的部位預設為主軌(self):
        self.assertEqual(self.resolve()["0050"]["track"], tracks.CORE)

    def test_指定軌道被沿用(self):
        r = self.resolve()
        self.assertEqual(r["2330"]["track"], tracks.SATELLITE)
        self.assertEqual(r["2308"]["track"], tracks.INHERITED)

    def test_市值低於門檻實際降為凍結(self):
        r = self.resolve()
        self.assertEqual(r["0053"]["track"], tracks.FROZEN)
        self.assertEqual(r["00911"]["track"], tracks.FROZEN)
        self.assertTrue(r["0053"]["frozen"])

    def test_凍結時指定軌道仍保留供解凍(self):
        """降凍結是現況造成的，不該把使用者的指定抹掉——加碼回門檻上就該回原軌"""
        r = self.resolve(assigned={**ASSIGNED, "0053": tracks.SATELLITE})
        self.assertEqual(r["0053"]["track"], tracks.FROZEN)
        self.assertEqual(r["0053"]["assigned"], tracks.SATELLITE)

    def test_市值恰好等於門檻不凍結(self):
        r = self.resolve(positions={"X": P(10, 300.0)}, min_trade=3000.0)
        self.assertFalse(r["X"]["frozen"])

    def test_市值以現價計不以成本計(self):
        r = self.resolve(positions={"X": P(10, 400.0, avg_price=100.0)})
        self.assertEqual(r["X"]["value"], 4000.0)

    def test_只認實際持倉忽略殘留指定(self):
        """賣光的標的留在 tracks.json 裡不該冒出來"""
        r = self.resolve(assigned={**ASSIGNED, "9999": tracks.SATELLITE})
        self.assertNotIn("9999", r)

    def test_繼承部位轉正時標記可解凍(self):
        pos = {**POSITIONS, "2308": P(30, 1900.0, pnl=2320.0)}
        self.assertTrue(self.resolve(positions=pos)["2308"]["unfreezable"])

    def test_繼承部位虧損中不標記可解凍(self):
        self.assertFalse(self.resolve()["2308"]["unfreezable"])

    def test_非繼承部位不標記可解凍(self):
        """unfreezable 只對 inherited 有意義，獲利的主軌部位不該被標"""
        self.assertFalse(self.resolve()["0050"]["unfreezable"])

    def test_不自動搬軌(self):
        """轉正只給訊號，軌道歸屬仍是使用者指定的——狀態機不該自己動真錢部位"""
        pos = {**POSITIONS, "2308": P(30, 1900.0, pnl=2320.0)}
        self.assertEqual(self.resolve(positions=pos)["2308"]["track"], tracks.INHERITED)


class TestByTrack(unittest.TestCase):
    def test_彙總各軌市值與代號(self):
        agg = tracks.by_track(tracks.resolve(ASSIGNED, POSITIONS, MIN_TRADE))
        self.assertEqual(agg[tracks.SATELLITE]["value"], 47600.0)
        self.assertEqual(agg[tracks.SATELLITE]["codes"], ["2330"])
        self.assertEqual(agg[tracks.INHERITED]["value"], 54450.0)
        self.assertEqual(agg[tracks.CORE]["codes"], ["0050"])
        self.assertEqual(agg[tracks.FROZEN]["codes"], ["0053", "00911"])
        self.assertEqual(agg[tracks.FROZEN]["value"], 1302.0)

    def test_四軌恆存在即使為空(self):
        """下游直接取 agg[SATELLITE]["value"]，不該因為空軌而 KeyError"""
        agg = tracks.by_track({})
        for t in tracks.ALL:
            self.assertEqual(agg[t]["value"], 0.0)
            self.assertEqual(agg[t]["codes"], [])

    def test_代號排序(self):
        agg = tracks.by_track(tracks.resolve({}, POSITIONS, 0.0))
        self.assertEqual(agg[tracks.CORE]["codes"], sorted(agg[tracks.CORE]["codes"]))


class TestAssign(unittest.TestCase):
    def test_指派回傳新字典不動原本(self):
        before = dict(ASSIGNED)
        after = tracks.assign(ASSIGNED, "0050", tracks.SATELLITE)
        self.assertEqual(ASSIGNED, before)
        self.assertEqual(after["0050"], tracks.SATELLITE)

    def test_拒絕未知軌道(self):
        with self.assertRaises(ValueError):
            tracks.assign(ASSIGNED, "0050", "moon")

    def test_可覆寫既有指派(self):
        after = tracks.assign(ASSIGNED, "2330", tracks.CORE)
        self.assertEqual(after["2330"], tracks.CORE)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "tracks.json")

    def test_檔案不存在回空指派(self):
        self.assertEqual(tracks.load(self.path), {})

    def test_存讀往返(self):
        tracks.save(ASSIGNED, self.path)
        self.assertEqual(tracks.load(self.path), ASSIGNED)

    def test_存檔會建立目錄(self):
        nested = os.path.join(self.dir, "a", "b", "tracks.json")
        tracks.save(ASSIGNED, nested)
        self.assertTrue(os.path.exists(nested))

    def test_載入時拒絕未知軌道(self):
        """手改壞的 tracks.json 不該靜默把部位當 core 處理"""
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"2330": "moon"}, fh)
        with self.assertRaises(ValueError):
            tracks.load(self.path)


if __name__ == "__main__":
    unittest.main()
