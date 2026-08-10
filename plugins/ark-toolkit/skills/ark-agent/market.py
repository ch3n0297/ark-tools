"""Shioaji 行情層（僅供 ark-agent 使用）：單一 session、分K→日K 聚合、日K 快取。

Shioaji 的 kbars 一律回傳分K（欄位式平行 list，ts 為納秒 epoch），沒有日K參數，
日K 由這裡聚合。頂層不匯入 shioaji——連線一律走 session()，純邏輯可在任何平台測試。
"""
import contextlib
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import source  # noqa: E402

PRICE_DIR = os.path.expanduser("~/.ark-toolkit/agent/prices")


@contextlib.contextmanager
def session():
    """單一 Shioaji session，結束必 logout。

    packet 一次要取持倉／餘額／交割／報價／K線五種資料，
    不能讓每種呼叫各自登入（連線額度有限，且逐一登入慢）。
    """
    import shioaji as sj
    source.load_credentials()
    api = sj.Shioaji(simulation=False)
    api.login(api_key=os.environ["SHIOAJI_API_KEY"],
              secret_key=os.environ["SHIOAJI_SECRET_KEY"])
    try:
        yield api
    finally:
        api.logout()


def _bar_date(ts_ns):
    """納秒 epoch → ISO 日期。

    以 UTC 取日期即可：台股盤中 09:00–13:30，不論 Shioaji 的 epoch 是 UTC
    還是台北牆鐘時間，同一根 K 的日期都落在同一天，不需要時區換算。
    """
    return dt.datetime.fromtimestamp(ts_ns / 1e9, dt.timezone.utc).date().isoformat()


def daily_from_kbars(ts, opens, highs, lows, closes, volumes):
    """把分K聚合成日K，回傳 [{date, open, high, low, close, volume}] 依日期排序。

    不假設輸入順序，先按 ts 排序；open=當日首筆、close=當日末筆、
    high=max、low=min、volume=sum。
    """
    rows = sorted(zip(ts, opens, highs, lows, closes, volumes))
    days = {}
    for t, o, h, lo, c, v in rows:
        d = _bar_date(t)
        bar = days.get(d)
        if bar is None:
            days[d] = {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v}
        else:
            bar["high"] = max(bar["high"], h)
            bar["low"] = min(bar["low"], lo)
            bar["close"] = c
            bar["volume"] += v
    return [days[d] for d in sorted(days)]


def merge_daily(cached, fresh):
    """合併日K，同日期以 fresh 為準（快取末日當時抓的盤中資料可能不完整）。"""
    by_date = {b["date"]: b for b in cached}
    by_date.update({b["date"]: b for b in fresh})
    return [by_date[d] for d in sorted(by_date)]


def load_cache(code, directory=PRICE_DIR):
    path = os.path.join(directory, f"{code}.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(code, bars, directory=PRICE_DIR):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{code}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bars, fh, ensure_ascii=False)
    return path


def fetch_daily(api, code, start, end, directory=PRICE_DIR):
    """取 [start, end] 的日K。走快取，只補快取末日（含）之後的區間。

    快取末日重抓而非跳過：當時抓的那天可能只有半天資料。
    """
    cached = load_cache(code, directory)
    fetch_start = max(start, cached[-1]["date"]) if cached else start
    contract = api.Contracts.Stocks[code]
    kb = api.kbars(contract, start=fetch_start, end=end)
    fresh = daily_from_kbars(list(kb.ts), list(kb.Open), list(kb.High),
                             list(kb.Low), list(kb.Close), list(kb.Volume))
    merged = merge_daily(cached, fresh)
    save_cache(code, merged, directory)
    return [b for b in merged if start <= b["date"] <= end]


def quotes_from_snapshots(snaps):
    """把 api.snapshots 的回傳整理成 {code: {...}}，只留決策需要的欄位。"""
    out = {}
    for s in snaps:
        out[s.code] = {
            "open": float(s.open), "high": float(s.high), "low": float(s.low),
            "close": float(s.close), "change_rate": float(s.change_rate),
            "volume": int(s.total_volume),
            "ts": dt.datetime.fromtimestamp(s.ts / 1e9, dt.timezone.utc)
                  .replace(tzinfo=None).isoformat(),
        }
    return out
