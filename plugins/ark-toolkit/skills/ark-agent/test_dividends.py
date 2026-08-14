"""除權息資料維護的純邏輯測試（不需要網路，任何平台可跑）"""
import unittest

import dividends as dv


def row(code="0050", date="1150721", cash="0.600000", stock="", kind="息",
        name="元大台灣50"):
    """TWT48U_ALL 的一列（欄位名稱與大小寫比照 TWSE 原樣）"""
    return {"Date": date, "Code": code, "Name": name, "Exdividend": kind,
            "StockDividendRatio": stock, "CashDividend": cash,
            "SubscriptionRatio": "", "SubscriptionPricePerShare": ""}


class TestRocToIso(unittest.TestCase):
    """TWSE 用民國年。轉錯的話除權息會落在錯誤的視窗，校正就加到別筆決策上。"""

    def test_基本轉換(self):
        self.assertEqual(dv.roc_to_iso("1150721"), "2026-07-21")

    def test_跨年(self):
        self.assertEqual(dv.roc_to_iso("1150101"), "2026-01-01")
        self.assertEqual(dv.roc_to_iso("1141231"), "2025-12-31")

    def test_百年以下三碼年份(self):
        """民國 99 年以前是 6 碼，不能只用固定切片"""
        self.assertEqual(dv.roc_to_iso("990615"), "2010-06-15")

    def test_格式不對回_None(self):
        for bad in ("", "abc", "115", None):
            self.assertIsNone(dv.roc_to_iso(bad), bad)


class TestParseRow(unittest.TestCase):
    """evaluate 要的欄位是 code / ex_date / cash / stock_ratio，
    格式由它決定——這裡只負責把 TWSE 的原始列翻譯過去。"""

    def test_純現金股利(self):
        d = dv.parse_row(row())
        self.assertEqual(d["code"], "0050")
        self.assertEqual(d["ex_date"], "2026-07-21")
        self.assertEqual(d["cash"], 0.6)
        self.assertEqual(d["stock_ratio"], 0.0)

    def test_現金股利為空字串視為零(self):
        """TWSE 對尚未確定金額的公告會留空，不能當成 0.0 之外的東西"""
        d = dv.parse_row(row(cash=""))
        self.assertIsNone(d)

    def test_只有股票股利也要記(self):
        """evaluate 靠 stock_ratio 標記 stock_dividend_unadjusted——
        沒記的話那筆報酬會被當成已完整校正，實際上股票股利根本沒處理"""
        d = dv.parse_row(row(cash="", stock="0.100000", kind="權"))
        self.assertIsNotNone(d)
        self.assertEqual(d["cash"], 0.0)
        self.assertEqual(d["stock_ratio"], 0.1)

    def test_現金與股票並存(self):
        d = dv.parse_row(row(cash="0.670000", stock="0.674000", kind="息及權"))
        self.assertEqual(d["cash"], 0.67)
        self.assertEqual(d["stock_ratio"], 0.674)

    def test_日期壞掉回_None(self):
        self.assertIsNone(dv.parse_row(row(date="bad")))

    def test_保留代號與名稱(self):
        d = dv.parse_row(row())
        self.assertEqual(d["name"], "元大台灣50")


class TestMerge(unittest.TestCase):
    """每日抓取會重複拿到同一批預告，累積時必須去重；
    但金額會從預估改為確定（0050 就是 7/1 預估、7/17 二階段公告），
    同一事件的新版本要覆蓋舊的。"""

    OLD = [{"code": "0050", "ex_date": "2026-07-21", "cash": 0.5, "stock_ratio": 0.0}]

    def test_新事件附加(self):
        fresh = [{"code": "2330", "ex_date": "2026-09-18", "cash": 7.0,
                  "stock_ratio": 0.0}]
        got = dv.merge(self.OLD, fresh)
        self.assertEqual(len(got), 2)

    def test_同代號同除息日視為同一事件並以新值覆蓋(self):
        fresh = [{"code": "0050", "ex_date": "2026-07-21", "cash": 0.6,
                  "stock_ratio": 0.0}]
        got = dv.merge(self.OLD, fresh)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["cash"], 0.6)

    def test_同代號不同除息日是兩個事件(self):
        fresh = [{"code": "0050", "ex_date": "2026-01-17", "cash": 1.0,
                  "stock_ratio": 0.0}]
        self.assertEqual(len(dv.merge(self.OLD, fresh)), 2)

    def test_依除息日排序(self):
        fresh = [{"code": "2330", "ex_date": "2026-03-18", "cash": 4.5,
                  "stock_ratio": 0.0}]
        got = dv.merge(self.OLD, fresh)
        self.assertEqual([d["ex_date"] for d in got],
                         ["2026-03-18", "2026-07-21"])

    def test_空輸入不崩(self):
        self.assertEqual(dv.merge([], []), [])
        self.assertEqual(len(dv.merge(self.OLD, [])), 1)


class TestParseAll(unittest.TestCase):
    """整批解析時，壞掉的列要跳過而不是讓整批失敗——
    TWSE 偶爾夾雜格式異常的列，為了一列放棄整批不划算。"""

    def test_跳過壞列保留好列(self):
        rows = [row(), row(date="bad"), row(code="2330", cash="7.000000")]
        got = dv.parse_all(rows)
        self.assertEqual([d["code"] for d in got], ["0050", "2330"])

    def test_全部壞掉回空清單(self):
        self.assertEqual(dv.parse_all([row(date="bad"), row(cash="")]), [])


if __name__ == "__main__":
    unittest.main()
