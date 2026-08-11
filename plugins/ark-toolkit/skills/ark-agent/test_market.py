"""ark-agent 行情層純邏輯測試（不需要 Shioaji 連線，任何平台可跑）"""
import datetime as dt
import tempfile
import unittest
from types import SimpleNamespace

import market


def ns(date_str, hh, mm):
    """把「日期 + 盤中時刻」轉成納秒 epoch（Shioaji kbars 的 ts 格式）"""
    d = dt.datetime.fromisoformat(f"{date_str}T{hh:02d}:{mm:02d}:00")
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1e9)


# 兩個交易日、每日三根分K 的合成樣本
TS = [ns("2026-08-03", 9, 0), ns("2026-08-03", 10, 30), ns("2026-08-03", 13, 30),
      ns("2026-08-04", 9, 0), ns("2026-08-04", 11, 0), ns("2026-08-04", 13, 30)]
OPEN = [100.0, 101.0, 102.0, 105.0, 104.0, 106.0]
HIGH = [101.5, 103.0, 102.5, 105.5, 107.0, 106.5]
LOW = [99.5, 100.5, 101.0, 103.0, 103.5, 105.0]
CLOSE = [101.0, 102.0, 101.5, 104.0, 106.0, 106.2]
VOL = [1000, 500, 800, 1200, 300, 400]


class TestDailyFromKbars(unittest.TestCase):
    def test_納秒時間戳聚合成日K(self):
        bars = market.daily_from_kbars(TS, OPEN, HIGH, LOW, CLOSE, VOL)
        self.assertEqual([b["date"] for b in bars], ["2026-08-03", "2026-08-04"])

    def test_日K開高低收量各自正確(self):
        d1, d2 = market.daily_from_kbars(TS, OPEN, HIGH, LOW, CLOSE, VOL)
        self.assertEqual((d1["open"], d1["close"]), (100.0, 101.5))   # 首筆開、末筆收
        self.assertEqual((d1["high"], d1["low"]), (103.0, 99.5))      # max / min
        self.assertEqual(d1["volume"], 2300)                          # sum
        self.assertEqual((d2["open"], d2["close"], d2["volume"]), (105.0, 106.2, 1900))

    def test_亂序分K仍正確聚合(self):
        """不假設 Shioaji 回傳順序——先按 ts 排序再聚合"""
        order = [3, 0, 5, 2, 1, 4]
        bars = market.daily_from_kbars(
            [TS[i] for i in order], [OPEN[i] for i in order], [HIGH[i] for i in order],
            [LOW[i] for i in order], [CLOSE[i] for i in order], [VOL[i] for i in order])
        self.assertEqual((bars[0]["open"], bars[0]["close"]), (100.0, 101.5))

    def test_空輸入回傳空(self):
        self.assertEqual(market.daily_from_kbars([], [], [], [], [], []), [])


class TestMergeDaily(unittest.TestCase):
    def test_同日期以新資料為準(self):
        """快取末日抓取當時可能不完整，重抓後必須覆蓋"""
        cached = [{"date": "2026-08-03", "close": 999.0, "volume": 1}]
        fresh = [{"date": "2026-08-03", "close": 101.5, "volume": 2300}]
        merged = market.merge_daily(cached, fresh)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["close"], 101.5)

    def test_合併後依日期排序不重複(self):
        cached = [{"date": "2026-08-04"}, {"date": "2026-08-01"}]
        fresh = [{"date": "2026-08-03"}, {"date": "2026-08-04"}]
        merged = market.merge_daily(cached, fresh)
        self.assertEqual([b["date"] for b in merged],
                         ["2026-08-01", "2026-08-03", "2026-08-04"])


class TestCache(unittest.TestCase):
    def test_快取讀寫往返(self):
        with tempfile.TemporaryDirectory() as d:
            bars = [{"date": "2026-08-03", "close": 101.5}]
            market.save_cache("2330", bars, d)
            self.assertEqual(market.load_cache("2330", d), bars)

    def test_無快取回傳空(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(market.load_cache("9999", d), [])


class FakeApi:
    """只實作 fetch_daily 用到的介面：Contracts.Stocks[code] 與 kbars()"""

    def __init__(self, kb):
        self.kb = kb
        self.calls = []
        self.Contracts = SimpleNamespace(Stocks={"2330": "CONTRACT-2330"})

    def kbars(self, contract, start=None, end=None):
        self.calls.append((contract, start, end))
        return self.kb


class TestFetchDaily(unittest.TestCase):
    def kb(self):
        return SimpleNamespace(ts=TS, Open=OPEN, High=HIGH, Low=LOW, Close=CLOSE, Volume=VOL)

    def test_無快取時從起日抓(self):
        with tempfile.TemporaryDirectory() as d:
            api = FakeApi(self.kb())
            bars = market.fetch_daily(api, "2330", "2026-08-01", "2026-08-05", d)
            self.assertEqual(api.calls[0][1], "2026-08-01")
            self.assertEqual([b["date"] for b in bars], ["2026-08-03", "2026-08-04"])

    def test_快取只補末日之後(self):
        """已有快取時從快取末日（含，該日可能不完整）續抓，不重抓整段"""
        with tempfile.TemporaryDirectory() as d:
            market.save_cache("2330", [{"date": "2026-08-03", "open": 100.0, "high": 103.0,
                                        "low": 99.5, "close": 999.0, "volume": 1}], d)
            api = FakeApi(self.kb())
            bars = market.fetch_daily(api, "2330", "2026-08-01", "2026-08-05", d)
            self.assertEqual(api.calls[0][1], "2026-08-03")       # 從快取末日續抓
            self.assertEqual(bars[0]["close"], 101.5)             # 不完整的那天被覆蓋

    def test_回傳只含區間內的日期(self):
        with tempfile.TemporaryDirectory() as d:
            api = FakeApi(self.kb())
            bars = market.fetch_daily(api, "2330", "2026-08-04", "2026-08-05", d)
            self.assertEqual([b["date"] for b in bars], ["2026-08-04"])
            cached = market.load_cache("2330", d)                 # 快取保留全部
            self.assertEqual(len(cached), 2)

    def test_快取末日越過區間時不呼叫API(self):
        """packet 先抓到今日、settle_previous 再查昨日：fetch_start 會算成
        快取末日（今日）> end（昨日），Shioaji 對 start > end 回 400。
        區間內都是收盤後的完整日K，必須直接回快取、不打 API。"""
        with tempfile.TemporaryDirectory() as d:
            market.save_cache("2330", [{"date": "2026-08-03", "close": 101.5},
                                       {"date": "2026-08-04", "close": 106.2}], d)
            api = FakeApi(self.kb())
            bars = market.fetch_daily(api, "2330", "2026-08-03", "2026-08-03", d)
            self.assertEqual(api.calls, [])
            self.assertEqual([b["date"] for b in bars], ["2026-08-03"])

    def test_快取末日越過區間時休市日回空(self):
        """settle_previous 以「回傳為空」判定休市——快取覆蓋下查無該日
        （週末／假日）要回空 list，同樣不打 API。"""
        with tempfile.TemporaryDirectory() as d:
            market.save_cache("2330", [{"date": "2026-08-03", "close": 101.5},
                                       {"date": "2026-08-04", "close": 106.2}], d)
            api = FakeApi(self.kb())
            bars = market.fetch_daily(api, "2330", "2026-08-02", "2026-08-02", d)
            self.assertEqual(api.calls, [])
            self.assertEqual(bars, [])


class TestQuotesFromSnapshots(unittest.TestCase):
    def test_整理成以代號為鍵(self):
        snap = SimpleNamespace(code="2330", open=2360.0, high=2385.0, low=2350.0,
                               close=2380.0, change_rate=0.85, total_volume=21000,
                               ts=ns("2026-08-08", 13, 30))
        quotes = market.quotes_from_snapshots([snap])
        self.assertEqual(quotes["2330"]["close"], 2380.0)
        self.assertEqual(quotes["2330"]["volume"], 21000)
        self.assertTrue(quotes["2330"]["ts"].startswith("2026-08-08"))


if __name__ == "__main__":
    unittest.main()
