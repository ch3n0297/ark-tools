# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "pyobjc-framework-cocoa",
#     "pyobjc-framework-applicationservices",
#     "pyobjc-framework-quartz",
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""決策日誌：Agent 建議的鎖定、硬規則驗證、成交對回。

鎖定機制 = append-only JSONL + 內容雜湊 + first-decision-wins：
決策寫入後不可改，同日第二筆標 `amended`（誠實呈現，不靜默取代），
evaluate 只認每個日期的第一筆。買賣建議只存在於這裡——
ark-analyze 的「不產出買賣建議」不變量不受影響。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark      # noqa: E402
import market   # noqa: E402
import tracks   # noqa: E402

JOURNAL = os.path.expanduser("~/.ark-toolkit/agent/journal.jsonl")

CORE, SATELLITE = tracks.CORE, tracks.SATELLITE
INHERITED, FROZEN = tracks.INHERITED, tracks.FROZEN


# ---------------------------------------------------------------- 鎖定與讀寫

def lock_hash(entry):
    """canonical JSON（排除 lock 欄自身）的 sha256。"""
    body = {k: v for k, v in entry.items() if k != "lock"}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def append_entry(entry, path=JOURNAL):
    ark.append_sync_log(entry, path)


def load_entries(path=JOURNAL):
    """讀全部日誌，壞行略過（比照 ark.last_sync：紀錄壞一行不該讓主流程掛掉）。"""
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


def first_decision(entries, date):
    """該日期的第一筆決策——evaluate 只認它，之後的都是 amended。"""
    for e in entries:
        if e.get("type") == "decision" and e.get("date") == date:
            return e
    return None


# ---------------------------------------------------------------- 驗證（硬規則）

def _est_price(order, quotes):
    """估價用前一日收盤，沒有報價就用限價區間中點。"""
    q = quotes.get(order["code"])
    if q and q.get("close"):
        return float(q["close"])
    return (order.get("limit_low", 0.0) + order.get("limit_high", 0.0)) / 2


def track_of(order):
    """委託所屬軌道；未標示者視為主軌（既有決策格式不必改就仍然合法）。"""
    return order.get("track", CORE)


def is_leveraged_or_inverse(code):
    """台股槓桿（正2，代號結尾 L）與反向（反1，結尾 R）ETF。

    主軌不停損，標的必須會均值回歸；這類產品有波動耗損，盤整市慢性失血
    且不保證回得來，沒有出場機制的軌道抱它是最糟的組合。它們在 ARK 的
    價值區裡是合法候選，所以得在紀律層擋。
    """
    return code[:2] == "00" and code[-1:] in ("L", "R")


def _validate_satellite(sat, envelope, quotes):
    """衛星軌自己的規則：白名單、配額、冷卻。

    ARK 紀律（獲利才賣、只買價值區、檔數）不套用在這裡——這一軌存在的理由
    就是做 ARK 不做的事（停損、非價值區標的）。但它不是法外之地：
    標的必須有人先寫進 tracks.json，金額受配額限制。
    """
    if envelope is None:
        return ["有衛星軌委託但缺少執行邊界——查不到配額與白名單，不放行"]
    st = envelope["tracks"][SATELLITE]
    out = []
    allow = set(st.get("allowlist", ()))
    for o in sat:
        if o["code"] not in allow:
            out.append(f"{o['code']} 不在衛星軌白名單內"
                       f"（開新標的須先寫進 tracks.json）")
    buys = [o for o in sat if o["action"] == "buy"]
    if buys and st.get("halted"):
        out.append(f"衛星軌冷卻中不可新倉：{st.get('halt_reason')}")
    net = (sum(o["qty"] * _est_price(o, quotes) for o in buys)
           - sum(o["qty"] * _est_price(o, quotes)
                 for o in sat if o["action"] == "sell"))
    # 配額約束的是**買進**。淨賣出一律放行——remaining 為負代表已經超額，
    # 而賣出正是唯一能修正它的動作；用 net > remaining 一律比較會把減碼也擋掉。
    if net > 0 and net > st["remaining"]:
        out.append(f"衛星軌淨買進 {net:,.0f} 超過剩餘配額 {st['remaining']:,.0f}")
    return out


def validate_orders(orders, packet, envelope=None):
    """硬規則驗證，回傳違規清單（空 = 合規）。

    主軌（core）＝「完全比照 ARK 紀律」，違反硬規則是 bug 不是自由度；
    自由度（賣哪幾檔、股數偏離、限價）不在這裡擋，事後由 evaluate 量測。
    衛星軌（satellite）走 `_validate_satellite`，不受 ARK 紀律約束。

    **「先調節才可買」兩軌都適用**：那是 ARK 的帳戶級曝險訊號，若衛星軌能在
    被要求調節時加碼，帳戶總曝險就失控了。
    """
    disc = packet["discipline"]
    quotes = packet.get("market", {}).get("quotes", {})
    positions = packet.get("account", {}).get("positions", {})
    scope = set(packet.get("news_scope", {}).get("codes", []))
    core = [o for o in orders if track_of(o) == CORE]
    sat = [o for o in orders if track_of(o) == SATELLITE]
    violations = [f"{o['code']} 的軌道不明：{track_of(o)!r}"
                  for o in orders if track_of(o) not in (CORE, SATELLITE)]

    # 資訊邊界是 ARK 實驗協定的一部分，只約束主軌；衛星軌以白名單為界
    for o in core:
        if o["code"] not in scope:
            violations.append(f"{o['code']} 不在 news_scope 清單內（資訊邊界外的標的）")
    for o in core:
        if o["action"] == "sell" and o["code"] not in disc["sellable"]:
            violations.append(f"賣出 {o['code']} 違反紀律：不在可調節清單（虧損中或非持股）")
        if o["action"] == "buy" and o["code"] not in disc["buy_candidates"]:
            violations.append(f"買進 {o['code']} 違反紀律：不在價值區候選清單")
        if o["action"] == "buy" and is_leveraged_or_inverse(o["code"]):
            violations.append(f"買進 {o['code']} 違反主軌設定：槓桿／反向 ETF "
                              f"有波動耗損，不可放進沒有停損的主軌（衛星軌才行）")
    if sat:
        violations += _validate_satellite(sat, envelope, quotes)

    buys = [o for o in orders if o["action"] == "buy"]
    sells = [o for o in orders if o["action"] == "sell"]
    if buys and disc["adjust_required_before_buy"]:
        sell_value = sum(o["qty"] * _est_price(o, quotes) for o in sells)
        if sell_value < disc["adjust_amount"]:
            violations.append(
                f"參考調節 {disc['adjust_amount']:,.0f} 未被賣單覆蓋"
                f"（賣單估值 {sell_value:,.0f}）——App 紀律：先調節才可買")

    # 檔數是 ARK 模型的概念，只算主軌部位；其餘三軌不在 ARK 的分配模型裡
    core_positions = set(positions) - non_core_codes(envelope)
    core_sells = [o for o in core if o["action"] == "sell"]
    fully_sold = {o["code"] for o in core_sells
                  if o["code"] in positions and o["qty"] >= positions[o["code"]]["qty"]}
    names_after = ((core_positions - fully_sold)
                   | {o["code"] for o in core if o["action"] == "buy"})
    # 既有超限不歸咎於本日決策——只擋「讓檔數變得更糟」的單
    allowed = max(disc["max_names"], len(core_positions))
    if len(names_after) > allowed:
        violations.append(
            f"執行後主軌將持有 {len(names_after)} 檔，超過檔數上限 {disc['max_names']}")
    return violations


def non_core_codes(envelope):
    """不屬於主軌的代號（衛星／繼承／凍結）。無 envelope 時視為全部都在主軌。"""
    if envelope is None:
        return set()
    return {c for t in (SATELLITE, INHERITED, FROZEN)
            for c in envelope["tracks"].get(t, {}).get("codes", ())}


def load_envelope(date):
    """當日的執行邊界；沒有則回 None（此時衛星軌委託一律不放行）。

    在這裡讀而不由呼叫端傳入：record 是 CLI 入口，忘了帶 envelope 會讓
    衛星軌規則靜默失效，而那正是最不該靠人記得的地方。
    """
    import risk
    path = os.path.join(risk.ENVELOPE_DIR, f"{date}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def record_decision(decision, packet, entries, override=False, envelope=None):
    """驗證並準備寫入的決策 entry，回傳 (entry 或 None, violations)。

    packet_hash 必須綁定當日決策包——證明決策看的是哪一份資料，
    對舊資料做的決策不可掛到新包上。
    """
    if packet is None:
        return None, ["當日決策包不存在，先執行 packet.py"]
    if decision.get("packet_hash") != packet["hash"]:
        return None, ["packet_hash 與當日決策包不符（決策依據的不是這份資料）"]
    violations = validate_orders(decision.get("orders", []), packet, envelope)
    if violations and not override:
        return None, violations
    entry = dict(decision)
    entry["type"] = "decision"
    if violations:
        entry["violations"] = violations
    if first_decision(entries, decision["date"]) is not None:
        entry["amended"] = True
    entry["lock"] = lock_hash(entry)
    return entry, violations


# ---------------------------------------------------------------- 成交對回

def infer_buy_price(old_qty, old_avg, new_qty, new_avg):
    """買進成交價由持倉均價反推——list_profit_loss 只有賣出，買進得用 diff。"""
    return (new_qty * new_avg - old_qty * old_avg) / (new_qty - old_qty)


def _slippage(price, order):
    low, high = order.get("limit_low"), order.get("limit_high")
    if not low or not high:
        return None
    mid = (low + high) / 2
    return (price - mid) / mid


def match_fills(orders, pl_rows, positions_before, positions_after):
    """把決策單對回實際成交，回傳 (fills, unfilled)。

    賣出：list_profit_loss 的列直接給成交價（可能多筆，加權平均）。
    買進：持倉 diff + 均價反推。
    """
    fills, unfilled = [], []
    for o in orders:
        code = o["code"]
        if o["action"] == "sell":
            rows = [r for r in pl_rows if r["code"] == code]
            qty = sum(r["quantity"] for r in rows)
            if qty:
                price = sum(r["quantity"] * r["price"] for r in rows) / qty
                fills.append({"code": code, "action": "sell", "qty": qty,
                              "price": round(price, 4), "source": "profit_loss",
                              "slippage": _slippage(price, o)})
            else:
                unfilled.append({"code": code, "action": "sell",
                                 "reason": "當日無賣出成交"})
        else:
            before = positions_before.get(code, {"qty": 0, "avg_price": 0.0})
            after = positions_after.get(code)
            gained = (after["qty"] if after else 0) - before["qty"]
            if gained > 0:
                price = infer_buy_price(before["qty"], before["avg_price"],
                                        after["qty"], after["avg_price"])
                fills.append({"code": code, "action": "buy", "qty": gained,
                              "price": round(price, 4), "source": "position_diff",
                              "slippage": _slippage(price, o)})
            else:
                unfilled.append({"code": code, "action": "buy",
                                 "reason": "持倉未增加（當日無買進成交）"})
    return fills, unfilled


def previous_weekday(date_iso):
    d = dt.date.fromisoformat(date_iso) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def settle_previous(api, today, path=JOURNAL, price_dir=None):
    """結算前一交易日的決策成交（packet 盤前順手呼叫）。回傳寫入的 entry 或 None。

    前一平日若休市（國定假日，以 0050 當日有無行情判定）不算缺席；
    已結算或已記缺席的日期不重複寫。
    """
    import shioaji as sj
    import packet as packet_mod

    prev = previous_weekday(today)
    kwargs = {"directory": price_dir} if price_dir else {}
    if not market.fetch_daily(api, packet_mod.BENCHMARK, prev, prev, **kwargs):
        return None                      # 休市日
    entries = load_entries(path)
    if any(e.get("type") == "fill" and e.get("decision_ref") == prev for e in entries):
        return None
    if any(e.get("type") == "missed" and e.get("date") == prev for e in entries):
        return None

    now = dt.datetime.now().replace(microsecond=0).isoformat()
    decision = first_decision(entries, prev)
    if decision is None:
        pk = packet_mod.load_packet(prev)
        entry = {"type": "missed", "date": prev, "ts": now,
                 "reason": "未做決策" if pk else "packet 未產生"}
        append_entry(entry, path)
        return entry
    if decision.get("no_trade"):
        entry = {"type": "fill", "date": today, "ts": now, "decision_ref": prev,
                 "fills": [], "unfilled": [], "note": "no_trade"}
        append_entry(entry, path)
        return entry

    pl = api.list_profit_loss(api.stock_account, prev, prev, unit=sj.Unit.Share)
    pl_rows = [{"code": r.code, "quantity": int(r.quantity), "price": float(r.price)}
               for r in pl]
    after = {p.code: {"qty": int(p.quantity), "avg_price": float(p.price)}
             for p in api.list_positions(api.stock_account, unit=sj.Unit.Share)}
    pk = packet_mod.load_packet(prev)
    before = (pk or {}).get("account", {}).get("positions", {})
    fills, unfilled = match_fills(decision.get("orders", []), pl_rows, before, after)
    entry = {"type": "fill", "date": today, "ts": now, "decision_ref": prev,
             "fills": fills, "unfilled": unfilled}
    append_entry(entry, path)
    return entry


# ---------------------------------------------------------------- CLI

def main():
    import packet as packet_mod

    ap = argparse.ArgumentParser(description="ark-agent 決策日誌")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="寫入一筆決策（鎖定，不可改）")
    rec.add_argument("decision_file", help="Agent 組好的決策 JSON 檔")
    rec.add_argument("--override", action="store_true",
                     help="硬規則違規仍寫入（entry 會帶 violations，紀律報告照列）")
    st = sub.add_parser("settle", help="結算前一交易日的成交")
    st.add_argument("--date", help="以此日為「今天」（預設今天）")
    sh = sub.add_parser("show", help="列出日誌")
    sh.add_argument("--date", help="只列此日期相關的紀錄")
    args = ap.parse_args()

    if args.cmd == "record":
        with open(args.decision_file, encoding="utf-8") as fh:
            decision = json.load(fh)
        decision.setdefault("ts", dt.datetime.now().replace(microsecond=0).isoformat())
        pk = packet_mod.load_packet(decision["date"])
        entries = load_entries()
        entry, violations = record_decision(decision, pk, entries, args.override,
                                            load_envelope(decision["date"]))
        if entry is None:
            print("🛑 拒絕寫入：")
            for v in violations:
                print(f"  - {v}")
            return 2
        append_entry(entry)
        n = len(entry.get("orders", []))
        print(f"✅ 決策已鎖定：{entry['date']}　{n} 筆單　lock={entry['lock'][:19]}…")
        if entry.get("amended"):
            print("  ⚠️ 本日已有決策，這筆標記為 amended——evaluate 只認第一筆")
        for v in violations:
            print(f"  ⚠️ 違規（--override 已寫入）：{v}")
        return 0

    if args.cmd == "settle":
        today = args.date or dt.date.today().isoformat()
        with market.session() as api:
            entry = settle_previous(api, today)
        if entry is None:
            print("無需結算（前一交易日已結算、已記缺席、或休市）")
        elif entry["type"] == "missed":
            print(f"⚠️ {entry['date']} 缺席：{entry['reason']}")
        else:
            print(f"✅ 已結算 {entry['decision_ref']}："
                  f"成交 {len(entry['fills'])} 筆、未成交 {len(entry['unfilled'])} 筆")
            for f in entry["fills"]:
                slip = f" 滑價 {f['slippage']:+.2%}" if f.get("slippage") is not None else ""
                print(f"  {f['action']} {f['code']} {f['qty']} 股 @ {f['price']}{slip}")
        return 0

    entries = load_entries()
    if args.date:
        entries = [e for e in entries
                   if e.get("date") == args.date or e.get("decision_ref") == args.date]
    for e in entries:
        print(json.dumps(e, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
