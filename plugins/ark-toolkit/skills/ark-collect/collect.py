# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
#     "openpyxl",
# ]
# ///
"""兩階段同步的階段一：收集各帳戶持倉 → 確認 → 寫入當日快照（staging.json）。

有檔案帳戶時 ark-sync 只吃這裡確認過的快照——確認過的才是實際套用的。
檔案缺失或不是今天匯出的會跳原生選檔視窗，解決券商匯出檔每次檔名都不同的現實。
機密全程不經 stdout。
"""
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark      # noqa: E402
import dialogs  # noqa: E402
import source   # noqa: E402


def table(title, positions):
    lines = [f"{title} — {len(positions)} 檔"]
    for code, (qty, price) in sorted(positions.items()):
        lines.append(f"  {code:<8} {qty:>10,} 股  均價 {price:,.2f}")
    return "\n".join(lines)


def collect_file_account(account):
    """讀一個檔案帳戶，缺檔／過舊時以原生視窗救援。

    回傳 (持倉, 可能更新過路徑或欄位的帳戶設定)；使用者取消回傳 (None, None)。
    """
    acc = dict(account)
    path = os.path.expanduser(acc["path"])
    reason = None
    if not os.path.exists(path):
        reason = f"找不到檔案：\n{path}"
    else:
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
        if mtime.date() != dt.date.today():
            if dialogs.ask_buttons(
                f"帳戶「{acc['name']}」的檔案是 {mtime:%Y-%m-%d %H:%M} 匯出的"
                f"（不是今天的資料）。\n{path}",
                ["重新選檔", "直接使用"],
            ) != "直接使用":
                reason = "要改用新的匯出檔。"
    if reason:
        picked = dialogs.choose_file(
            f"帳戶「{acc['name']}」{reason}\n請選擇最新的持股匯出檔（CSV／Excel）")
        if not picked:
            return None, None
        acc["path"] = path = picked

    while True:
        try:
            return source.read_file_positions(path, acc["columns"]), acc
        except RuntimeError as e:
            # 新選的檔案表頭可能與原本不同——給一次重新對應欄位的機會
            if dialogs.ask_buttons(f"帳戶「{acc['name']}」讀取失敗：\n{e}",
                                   ["重新對應欄位", "取消"]) != "重新對應欄位":
                return None, None
            columns = dialogs.pick_columns(path)
            if columns is None:
                return None, None
            acc["columns"] = columns


def main():
    ark.check_platform(tool="ark-collect")
    try:
        cfg = source.load_config()
    except source.SetupRequired as e:
        print(f"⚠️  {e}")
        return 2
    if source.is_pure_ark(cfg):
        print("⚠️  目前為純 ARK 模式（無任何帳戶）。請執行 ark-setup 新增帳戶。")
        return 2

    by_account, accounts = {}, []
    for account in cfg["accounts"]:
        name = account["name"]
        if account["type"] == "shioaji":
            print(f"讀取「{name}」（Shioaji API）…", flush=True)
            try:
                by_account[name] = source.read_account_positions(account)
            except Exception as e:
                print(f"✗ 帳戶「{name}」讀取失敗（{type(e).__name__}）：{e}")
                return 1
            accounts.append(account)
        else:
            print(f"讀取「{name}」（檔案）…", flush=True)
            positions, acc = collect_file_account(account)
            if positions is None:
                print("已取消，未寫入快照", flush=True)
                return 1
            by_account[name] = positions
            accounts.append(acc)
        print(f"  {len(by_account[name])} 檔", flush=True)

    merged = source.merge_positions(by_account)
    print()
    for name, positions in by_account.items():
        print(table(f"帳戶「{name}」", positions))
        print()
    if len(by_account) > 1:
        print(table("合併結果（股數相加、均價加權平均）", merged))

    summary = "＋".join(f"{n} {len(p)} 檔" for n, p in by_account.items())
    if dialogs.ask_buttons(
        f"{summary} → 合併 {len(merged)} 檔。\n"
        "確認寫入今日收集快照？（ark-sync 將以此為準）",
        ["取消", "確認"],
    ) != "確認":
        print("已取消，未寫入快照", flush=True)
        return 1

    path = source.write_staging(by_account)
    if accounts != cfg["accounts"]:
        source.save_config({**cfg, "accounts": accounts})
        print("✓ 帳戶的檔案路徑／欄位對應已更新", flush=True)
    print(f"✓ 快照已寫入：{path}（僅今日有效）", flush=True)
    print("接著可執行 ark-sync（建議先 --dry-run）。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
