"""軌道歸屬：決定每個部位適用哪一套規則。

四軌的意義見 SKILL.md。**軌道不自動推斷**——指定由 Phase 0 與後續決策明確
寫入 tracks.json，這裡只做兩件自動的事：實際持倉沒有的代號不出現、市值低於
最小可交易金額者實際降為 frozen（指定保留，加碼回門檻上就回原軌）。

自動搬軌在真錢系統裡會讓「這筆為什麼被賣掉」變得無法解釋，所以繼承部位轉正
時只標 `unfreezable` 給決策層看，軌道本身不動。
"""
import json
import os

CORE = "core"              # 完全照 ARK 紀律，虧損不賣，只做 ETF
SATELLITE = "satellite"    # 不受 ARK 紀律，允許硬停損，受配額限制
INHERITED = "inherited"    # 虧損中且 ARK 不可賣，凍結至轉正；不佔衛星軌配額
FROZEN = "frozen"          # 低於最小可交易金額，不進出場計算
ALL = (CORE, SATELLITE, INHERITED, FROZEN)

TRACKS_PATH = os.path.expanduser("~/.ark-toolkit/agent/tracks.json")


def position_value(p):
    """部位市值以現價計——成本價會讓凍結門檻判斷偏離實際可變現金額。"""
    return p["qty"] * p["last_price"]


def resolve(assigned, positions, min_trade_value):
    """把指定軌道與實際持倉合成實際軌道狀態。

    回傳 {code: {track, assigned, value, pnl, frozen, unfreezable}}。
    """
    out = {}
    for code, p in positions.items():
        a = assigned.get(code, CORE)
        value = position_value(p)
        pnl = p.get("pnl", 0.0)
        frozen = value < min_trade_value
        out[code] = {
            "track": FROZEN if frozen else a,
            "assigned": a,
            "value": value,
            "pnl": pnl,
            "frozen": frozen,
            "unfreezable": a == INHERITED and pnl > 0,
        }
    return out


def by_track(resolved):
    """各軌的市值與代號。四軌恆存在——下游直接取值，不該因空軌而 KeyError。"""
    agg = {t: {"value": 0.0, "codes": []} for t in ALL}
    for code, r in sorted(resolved.items()):
        agg[r["track"]]["value"] += r["value"]
        agg[r["track"]]["codes"].append(code)
    return agg


def assign(assigned, code, track):
    """指派軌道，回傳新字典（不動原本）。"""
    if track not in ALL:
        raise ValueError(f"未知軌道 {track!r}（應為 {'/'.join(ALL)}）")
    return {**assigned, code: track}


def load(path=TRACKS_PATH):
    """讀指定軌道 {代號: 軌道}；檔案不存在回空字典。

    壞值一律報錯，不靜默當 core——手改壞的歸屬會讓停損規則套錯部位。
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for code, track in data.items():
        if track not in ALL:
            raise ValueError(f"{path} 中 {code} 的軌道不明：{track!r}")
    return data


def save(assigned, path=TRACKS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(assigned, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return path
