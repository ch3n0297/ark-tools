# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = []
# ///
"""風控層：把決策包算成「執行邊界」，讓判斷層不必自己重推限制。

與 ARK 紀律的分工：**紀律管方向，風控管幅度**。ARK 決定買什麼賣什麼（journal
的硬規則把關），風控決定一次能動多少、什麼時候完全不准動。兩者不重疊——
風控不會叫你買 ARK 沒點名的標的，也不會放行 ARK 禁止的虧損賣出。

唯一的例外是**衛星軌**：該軌不受 ARK 紀律約束，停損由這裡定義。

全部是純函式，不碰 AX 與 API，任何平台可測。連線只發生在 CLI 的日曆載入，
而且走本地快取（--offline 是預設行為）。
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import equity   # noqa: E402
import tracks   # noqa: E402

EXITS = os.path.expanduser("~/.ark-toolkit/agent/satellite_exits.jsonl")
ENVELOPE_DIR = os.path.expanduser("~/.ark-toolkit/agent/envelopes")

REDUCE, BUY, HOLD = "reduce", "buy", "hold"

DEFAULTS = {
    # 實測本帳戶（2026-08 對帳）：200–500 元的交易總費用僅 1–2.5 元，**沒有
    # 20 元低消**。1,000 元的成本率約 0.3–0.4%，與大額交易相當；再低下去
    # 手續費的每筆底價會開始吃掉報酬，而且部位小到不影響組合、只會產生雜訊。
    "min_trade_value": 1000.0,
    "per_order_cap": 20000.0,
    "daily_buy_cap": 40000.0,
    "daily_turnover_cap": 60000.0,
    "core_concentration_cap": 0.60,
    "ratio_band_pp": 5.0,
    "max_names_hysteresis_days": 5,
    "satellite_quota_ratio": 0.25,
    "satellite_stop_loss": -0.12,
    "satellite_stop_streak": 3,
    "satellite_stop_cooldown_days": 10,
    "breaker_l1": 0.08,
    "breaker_l2": 0.15,
}


# ---------------------------------------------------------------- 檔數遲滯

def max_names_with_hysteresis(effective, recent_raw, days):
    """檔數上限的遲滯。

    公式是 `總資源 // 10 萬` 的硬斷崖，總資源在門檻附近震盪會讓上限天天跳，
    而官方教學明說「不要天天改，資產每多 10 萬才加一檔」。所以要求新值連續
    `days` 個交易日成立才生效。
    """
    if not recent_raw:
        return effective
    if effective is None:
        return recent_raw[-1]
    tail = recent_raw[-days:]
    if len(tail) < days:
        return effective
    candidate = tail[0]
    return candidate if all(v == candidate for v in tail) else effective


# ---------------------------------------------------------------- 持股比例偏離帶

def ratio_band_action(suggested_ratio, actual_ratio, band_pp):
    """ARK 建議比例 vs 實際比例 → 動作。

    無動作帶存在的理由：官方教學說建議比例一天內可移動數個百分點，每天照著
    微調會被來回成本吃掉。帶寬含端點——邊界上為了 0.0 個百分點付一次成本不划算。
    """
    gap = actual_ratio - suggested_ratio
    if gap > band_pp:
        return REDUCE
    if gap < -band_pp:
        return BUY
    return HOLD


# ---------------------------------------------------------------- 集中度

def concentration_breaches(resolved, total, cap):
    """單一標的市值佔比超過上限者，依超額金額由大到小。

    只看 core 與 satellite：inherited 與 frozen 動不了，列出來只會製造
    做不到的待辦。`actionable` 標記獲利與否——虧損中的部位主軌不准賣。
    """
    out = []
    for code, r in resolved.items():
        if r["track"] not in (tracks.CORE, tracks.SATELLITE) or not total:
            continue
        ratio = r["value"] / total
        if ratio <= cap:
            continue
        out.append({"code": code, "ratio": ratio,
                    "excess_value": r["value"] - cap * total,
                    "actionable": r["pnl"] > 0})
    return sorted(out, key=lambda x: -x["excess_value"])


# ---------------------------------------------------------------- 衛星軌停損

def stop_loss_candidates(resolved, positions, stop_pct):
    """跌破停損的衛星軌部位。主軌與繼承軌守 ARK『虧損不賣』，不在此列。"""
    out = []
    for code, r in resolved.items():
        if r["track"] != tracks.SATELLITE:
            continue
        p = positions[code]
        cost = p["qty"] * p["avg_price"]
        if not cost:
            continue
        roi = p["pnl"] / cost
        if roi <= stop_pct:
            out.append({"code": code, "roi": roi, "value": r["value"]})
    return sorted(out, key=lambda x: x["roi"])


def consecutive_stops(exits):
    """由尾端數連續的停損出場次數。中間出現非停損出場就歸零。"""
    n = 0
    for e in reversed(exits):
        if not e.get("stopped"):
            break
        n += 1
    return n


def trading_day_offset(calendar, date, n):
    """日曆上 `date`（或其後第一個交易日）之後第 n 個交易日；超出日曆回 None。"""
    for i, d in enumerate(calendar):
        if d >= date:
            return calendar[i + n] if i + n < len(calendar) else None
    return None


def satellite_cooldown(exits, streak_cap, cooldown_days, calendar, today):
    """連續停損熔斷，回傳 (halted, reason, until)。

    衛星軌配額 25% 若連續停損累積，會在帳戶級 L2 攔下來之前就吃掉一大塊，
    所以需要軌內自己這道。冷卻**只擋新倉**，不擋停損——冷卻中還不准出場
    等於把虧損部位鎖死。
    """
    streak = consecutive_stops(exits)
    if streak < streak_cap:
        return False, None, None
    until = trading_day_offset(calendar, exits[-1]["date"], cooldown_days)
    if until is None:
        return True, f"連續 {streak} 次停損，冷卻期超出已知日曆", None
    return (today < until,
            f"連續 {streak} 次停損，冷卻至 {until}",
            until)


# ---------------------------------------------------------------- 金額上限

def _est_value(order, quotes):
    """估算單筆金額：優先用報價收盤，沒有就用限價區間中點（同 journal._est_price）。"""
    q = quotes.get(order["code"])
    if q and q.get("close"):
        price = float(q["close"])
    else:
        price = (order.get("limit_low", 0.0) + order.get("limit_high", 0.0)) / 2
    return order["qty"] * price


def order_caps_violations(orders, quotes, limits):
    """金額上限檢查，回傳違規訊息清單（空 = 合規）。

    這是無人值守的爆炸半徑控制：即使判斷層或紀律驗證出錯，單日能動的錢
    仍有硬上限。**金額上限只管買進側**：買進是新增曝險；賣出是調節既有
    部位，上界天然受持倉量與「獲利才調節」紀律約束，設上限反而會把 ARK
    要求的調節卡成好幾天。最小可交易金額買賣皆查——那是成本佔比問題，
    與方向無關。
    """
    out = []
    buy_total = 0.0
    for o in orders:
        value = _est_value(o, quotes)
        if o["action"] == "buy":
            buy_total += value
            if value > limits["per_order_cap"]:
                out.append(f"{o['code']} 單筆 {value:,.0f} 超過上限 "
                           f"{limits['per_order_cap']:,.0f}")
        if value < limits["min_trade_value"]:
            out.append(f"{o['code']} 金額 {value:,.0f} 低於最小可交易金額 "
                       f"{limits['min_trade_value']:,.0f}（成本佔比過高）")
    if buy_total > limits["daily_buy_cap"]:
        out.append(f"單日買進 {buy_total:,.0f} 超過上限 {limits['daily_buy_cap']:,.0f}")
    if buy_total > limits["daily_turnover_cap"]:
        out.append(f"單日成交 {buy_total:,.0f} 超過上限 "
                   f"{limits['daily_turnover_cap']:,.0f}")
    return out


# ---------------------------------------------------------------- 組裝

def total_resource(packet):
    """總資源＝持股市值＋現金，與 ARK 運算頁的「資金總額」同義。

    運算頁讀值優先——它才是 ARK 算建議比例與參考調節的依據；讀不到才退用
    持倉與券商餘額推算。
    """
    posture = packet.get("ark", {}).get("posture")
    if posture:
        return posture["stock_value"] + posture["cash"]
    positions = packet.get("account", {}).get("positions", {})
    stock = sum(tracks.position_value(p) for p in positions.values())
    return stock + packet.get("account", {}).get("balance", 0.0)


def build_envelope(packet, assigned, equity_points, satellite_exits, calendar,
                   today, effective_max_names, max_names_history, config):
    """把決策包＋軌道＋淨值史合成執行邊界。

    `blocks` 非空代表有東西被擋住；`can_buy` / `can_sell` 是最終結論，
    判斷層只要看這兩個布林值即可。
    """
    positions = packet["account"]["positions"]
    resolved = tracks.resolve(assigned, positions, config["min_trade_value"])
    agg = tracks.by_track(resolved)
    total = total_resource(packet)
    stock_value = sum(r["value"] for r in resolved.values())

    dd = equity.drawdown(equity_points)
    level = equity.breaker_level(dd, config["breaker_l1"], config["breaker_l2"])
    halted, reason, until = satellite_cooldown(
        satellite_exits, config["satellite_stop_streak"],
        config["satellite_stop_cooldown_days"], calendar, today)

    posture = packet.get("ark", {}).get("posture")
    sync_ok = packet["account"].get("sync_ok", True)

    blocks = []
    if level == equity.L2:
        blocks.append(f"熔斷 L2：自峰值回撤 {dd:.2%}，系統停機，需人工重置")
    elif level == equity.L1:
        blocks.append(f"熔斷 L1：自峰值回撤 {dd:.2%}，停止買進")
    if not sync_ok:
        blocks.append("ARK 與券商持倉對帳不一致——紀律邊界會建立在錯的持倉上")
    if posture is None:
        # read_posture 在隱私眼睛開啟時只讀得到趴數、讀不到金額，會靜默回 None，
        # 而 build_discipline(None, …) 會退化成現金為零、不需調節。無人值守下
        # 「建立在錯的事實上」比「今天不交易」危險得多。
        blocks.append("讀不到 ARK 運算頁（隱私眼睛開啟或畫面異常）——"
                      "紀律邊界會退化成現金為零、不需調節")

    facts_ok = sync_ok and posture is not None
    quota = total * config["satellite_quota_ratio"]
    actual_ratio = (stock_value / total * 100) if total else 0.0
    suggested = (posture or {}).get("suggested_ratio")

    return {
        "schema": 1,
        "date": packet["date"],
        "packet_hash": packet["hash"],
        "can_buy": level == equity.NONE and facts_ok,
        "can_sell": level != equity.L2 and facts_ok,
        "blocks": blocks,
        "breaker": {"level": level, "drawdown": round(dd, 6),
                    "peak": equity.peak(equity_points)},
        "limits": {**{k: config[k] for k in
                      ("per_order_cap", "daily_buy_cap", "daily_turnover_cap",
                       "min_trade_value")},
                   # 決策層照 envelope 自律，語意必須寫在資料裡，不能只在程式碼註解
                   "scope": "金額上限只適用買進側；賣出不設金額上限"
                            "（min_trade_value 買賣皆查）"},
        "tracks": {
            tracks.CORE: agg[tracks.CORE],
            tracks.SATELLITE: {**agg[tracks.SATELLITE], "quota": quota,
                               "remaining": quota - agg[tracks.SATELLITE]["value"],
                               "halted": halted, "halt_reason": reason,
                               "halted_until": until,
                               # 白名單取自「指定」而非「持倉」：還沒買的標的
                               # 不在持倉裡，但開新倉必須先有人把它寫進 tracks.json
                               "allowlist": sorted(c for c, t in assigned.items()
                                                   if t == tracks.SATELLITE)},
            tracks.INHERITED: {**agg[tracks.INHERITED],
                               "unfreezable": sorted(c for c, r in resolved.items()
                                                     if r["unfreezable"])},
            tracks.FROZEN: agg[tracks.FROZEN],
        },
        "core": {
            "max_names": max_names_with_hysteresis(
                effective_max_names, max_names_history,
                config["max_names_hysteresis_days"]),
            # 留下公式原值：遲滯要比對歷次原值，不留就沒得比
            "raw_max_names": max_names_history[-1] if max_names_history else None,
            "concentration_cap": config["core_concentration_cap"],
            "breaches": concentration_breaches(resolved, total,
                                               config["core_concentration_cap"]),
            "ratio_band": {
                "suggested": suggested,
                "actual": round(actual_ratio, 2),
                "band_pp": config["ratio_band_pp"],
                "action": (HOLD if suggested is None else
                           ratio_band_action(suggested, actual_ratio,
                                             config["ratio_band_pp"])),
            },
        },
        "satellite": {
            "stop_loss": config["satellite_stop_loss"],
            "candidates": stop_loss_candidates(resolved, positions,
                                               config["satellite_stop_loss"]),
        },
    }


# ---------------------------------------------------------------- 持久化與 CLI

def is_fully_blocked(envelope):
    """今天完全不能動（買賣都被擋）。

    排程據此離開碼 3，與「程式壞了」的 2 區分開——只擋買（熔斷 L1）不算全停，
    還能賣就仍該讓 Agent 跑一輪，否則熔斷期間連調節都做不了。
    """
    return not envelope["can_buy"] and not envelope["can_sell"]


def load_exits(path=EXITS):
    """衛星軌出場紀錄，壞行略過（比照 equity.load_points）。"""
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


def save_envelope(env, directory=ENVELOPE_DIR):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{env['date']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(env, fh, ensure_ascii=False, indent=2)
    return path


def max_names_history(directory=ENVELOPE_DIR):
    """歷次 envelope 的 (公式原值序列, 最後生效值)——遲滯的輸入。

    歷史存在自己寫出的 envelope 裡，不寄生在 equity 點上：檔數上限是風控的
    狀態，讓淨值紀錄扛它會讓兩邊的欄位互相牽制。
    """
    if not os.path.isdir(directory):
        return [], None
    raws, effective = [], None
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as fh:
            core = json.load(fh).get("core", {})
        if core.get("raw_max_names") is not None:
            raws.append(core["raw_max_names"])
        if core.get("max_names") is not None:
            effective = core["max_names"]
    return raws, effective


def main():
    import market
    import packet as packet_mod

    ap = argparse.ArgumentParser(description="產生 ark-agent 的執行邊界")
    ap.add_argument("--date", help="覆寫決策日（預設今天）")
    ap.add_argument("--json", action="store_true", help="把 envelope 印到 stdout")
    args = ap.parse_args()

    date = args.date or dt.date.today().isoformat()
    pk = packet_mod.load_packet(date)
    if pk is None:
        print(f"🛑 找不到 {date} 的決策包，先執行 packet.py", file=sys.stderr)
        return 2

    # 日曆走本地日K快取，不連線——風控不該因為網路而算不出來
    calendar = [b["date"] for b in market.load_cache(packet_mod.BENCHMARK)]
    past_raws, effective = max_names_history()

    env = build_envelope(packet=pk, assigned=tracks.load(),
                         equity_points=equity.load_points(),
                         satellite_exits=load_exits(), calendar=calendar, today=date,
                         effective_max_names=effective,
                         max_names_history=past_raws + [pk["discipline"]["max_names"]],
                         config=DEFAULTS)
    path = save_envelope(env)

    if args.json:
        print(json.dumps(env, ensure_ascii=False, indent=2))
    else:
        s = env["tracks"][tracks.SATELLITE]
        print(f"✅ 執行邊界已存至 {path}")
        print(f"   熔斷 {env['breaker']['level']}（回撤 {env['breaker']['drawdown']:.2%}）"
              f"｜可買 {env['can_buy']}｜可賣 {env['can_sell']}")
        print(f"   衛星軌 {s['value']:,.0f} / 配額 {s['quota']:,.0f}"
              f"（剩 {s['remaining']:,.0f}）{'　⛔ 冷卻中' if s['halted'] else ''}")
        for b in env["blocks"]:
            print(f"   ⚠️ {b}")
    # 3 = 今天完全不能動（正常結果，非錯誤）；2 = 產不出邊界（錯誤）
    return 3 if is_fully_blocked(env) else 0


if __name__ == "__main__":
    sys.exit(main())
