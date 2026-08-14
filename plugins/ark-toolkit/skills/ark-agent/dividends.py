# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = []
# ///
"""維護除權息資料，供 evaluate 的報酬校正使用（盤後結算流程呼叫）。

**為什麼需要**：`market.fetch_daily` 走 `api.kbars`，那是分K聚合而成的
**原始成交價**，不做除權息還原（實證：0050 於 2026-07-21 除息 0.6 元，而快取中
2026-07-01 的收盤仍是除息前的 109.35）。跨越除息日的前瞻報酬若不校正，配息
那一段就憑空消失——影響是單邊的：續抱看起來比實際差、賣出看起來比實際好。

**為什麼要每天抓**：TWSE 給的是「除權除息**預告**表」，只含尚未發生的事件，
除息日一過就從表上消失。等到要算報酬時再查已經來不及，必須每天抓、累積存檔——
與 `market.py` 的日K快取同一個模式：外部資料源只給滑動視窗，本地負責留下歷史。

**已知限制**：只涵蓋上市（TWSE）。上櫃（TPEx）標的的除權息不會被抓到，那些
標的跨越除息日的報酬仍會少算配息。股票股利只記錄不校正，由 evaluate 標記
`stock_dividend_unadjusted` 提示該筆數字不完整。
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

STATE = os.path.expanduser("~/.ark-toolkit/agent")
DIVIDENDS = os.path.join(STATE, "dividends.jsonl")
SOURCE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
SOURCE_NAME = "TWSE TWT48U_ALL"


# ---------------------------------------------------------------- 純邏輯

def roc_to_iso(roc):
    """民國年月日（`1150721`）→ ISO 日期（`2026-07-21`）。

    年份長度不固定：民國 100 年起是 7 碼、99 年以前 6 碼，所以從尾端切
    月日、剩下的才是年，不能用固定切片。轉錯的話除權息會落在錯誤的視窗，
    校正就加到別筆決策上了。
    """
    if not roc or not str(roc).isdigit() or len(str(roc)) not in (6, 7):
        return None
    s = str(roc)
    year, month, day = int(s[:-4]) + 1911, s[-4:-2], s[-2:]
    try:
        return dt.date(year, int(month), int(day)).isoformat()
    except ValueError:
        return None


def _num(value):
    """TWSE 的數字欄位可能是空字串（金額未定）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_row(row):
    """TWT48U_ALL 的一列 → evaluate 要的除權息事件；無法用的回 None。

    欄位名稱由 `evaluate.dividends_in_window` 與 `horizon_result` 決定
    （code / ex_date / cash / stock_ratio），這裡只做翻譯。
    現金與股票股利都是零的列沒有校正價值，直接丟掉。
    """
    ex_date = roc_to_iso(row.get("Date"))
    code = (row.get("Code") or "").strip()
    if not ex_date or not code:
        return None
    cash, stock = _num(row.get("CashDividend")), _num(row.get("StockDividendRatio"))
    if cash <= 0 and stock <= 0:
        return None
    return {"code": code, "ex_date": ex_date, "cash": cash, "stock_ratio": stock,
            "name": (row.get("Name") or "").strip(), "source": SOURCE_NAME}


def parse_all(rows):
    """整批解析，壞掉的列跳過。

    TWSE 偶爾夾雜格式異常的列，為了一列放棄整批不划算——少一筆校正
    好過整天沒有校正資料。
    """
    return [d for d in (parse_row(r) for r in rows) if d]


def merge(existing, fresh):
    """依 (代號, 除息日) 去重合併，新值覆蓋舊值，依除息日排序。

    覆蓋而非略過：金額會從預估改為確定（0050 就是 7/1 先公告預估 0.6、
    7/17 二階段才確定），留著舊的預估值會讓校正差一截。
    """
    by_key = {(d["code"], d["ex_date"]): d for d in existing}
    by_key.update({(d["code"], d["ex_date"]): d for d in fresh})
    return sorted(by_key.values(), key=lambda d: (d["ex_date"], d["code"]))


# ---------------------------------------------------------------- 存取

def load(path=DIVIDENDS):
    """讀已累積的除權息；檔案不存在回空清單（首次執行就是這個狀態）。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def save(events, path=DIVIDENDS):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


def fetch(url=SOURCE_URL, timeout=30):
    """抓 TWSE 除權除息預告表。網路或格式出錯時回 None，由呼叫端決定怎麼辦。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:                                             # noqa: BLE001
        return None


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="更新除權息資料（供報酬校正）")
    ap.add_argument("--dry-run", action="store_true", help="只印出將新增的事件")
    args = ap.parse_args()

    rows = fetch()
    if rows is None:
        # 抓不到不算失敗：已累積的資料仍可用，下次結算會再試。讓整條結算流程
        # 因為一個外部網站而回報失敗，只會淹沒真正的故障。
        print("⚠️ 抓不到 TWSE 除權息預告表（網路或來源異常），沿用既有資料")
        return 0

    fresh = parse_all(rows)
    existing = load()
    merged = merge(existing, fresh)
    added = len(merged) - len(existing)

    if args.dry_run:
        print(f"（dry-run）來源 {len(rows)} 列 → 可用事件 {len(fresh)} 筆"
              f"｜現有 {len(existing)} 筆 → 合併後 {len(merged)} 筆（新增 {added}）")
        return 0

    save(merged)
    print(f"✅ 除權息資料已更新：{len(merged)} 筆（本次新增 {added}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
