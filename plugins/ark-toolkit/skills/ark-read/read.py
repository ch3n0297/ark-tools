# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
#     "openpyxl",
# ]
# ///
"""靜默讀取各帳戶的即時持倉——不碰 ARK、不彈視窗、只讀不寫。

輸出各帳戶明細與全帳戶合併結果；--json 出機器格式。
檔案帳戶順帶顯示檔案的最後修改時間，讓使用者知道資料多舊。
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import source  # noqa: E402


def table(title, positions):
    lines = [f"{title} — {len(positions)} 檔"]
    for code, (qty, price) in sorted(positions.items()):
        lines.append(f"  {code:<8} {qty:>10,} 股  均價 {price:,.2f}")
    return "\n".join(lines)


def read_all(cfg):
    """回傳 (各帳戶持倉, 檔案帳戶的最後修改時間)。任一帳戶失敗即整體失敗。"""
    by_account, mtimes = {}, {}
    for account in cfg["accounts"]:
        if account["type"] == "file":
            path = os.path.expanduser(account["path"])
            if os.path.exists(path):
                mtimes[account["name"]] = dt.datetime.fromtimestamp(
                    os.path.getmtime(path)).replace(microsecond=0).isoformat()
        by_account[account["name"]] = source.read_account_positions(account)
    return by_account, mtimes


def main():
    ap = argparse.ArgumentParser(description="靜默讀取各帳戶持倉（不碰 ARK、不彈視窗、只讀不寫）")
    ap.add_argument("--json", action="store_true", help="輸出機器可讀的 JSON")
    args = ap.parse_args()

    try:
        cfg = source.load_config()
        if source.is_pure_ark(cfg):
            print("⚠️  目前為純 ARK 模式（無任何帳戶）。請執行 ark-setup 新增帳戶。")
            return 2
        by_account, mtimes = read_all(cfg)
    except source.SetupRequired as e:
        print(f"⚠️  {e}")
        return 2
    except Exception as e:
        print(f"✗ 讀取失敗（{type(e).__name__}）：{e}")
        return 1

    merged = source.merge_positions(by_account)
    if args.json:
        print(json.dumps({"accounts": by_account, "merged": merged,
                          "file_mtime": mtimes}, ensure_ascii=False, indent=2))
        return 0

    kind = {a["name"]: a["type"] for a in cfg["accounts"]}
    for name, positions in by_account.items():
        label = f"帳戶「{name}」（{'Shioaji' if kind[name] == 'shioaji' else '檔案'}）"
        if name in mtimes:
            label += f"　檔案時間 {mtimes[name]}"
        print(table(label, positions))
        print()
    if len(by_account) > 1:
        print(table("合併結果", merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
