# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
#     "openpyxl",
# ]
# ///
"""ark-toolkit 帳戶清單設定精靈。

管理「真實持倉」的帳戶清單：永豐 Shioaji API＋任意個檔案帳戶（CSV／Excel）。
清單為空＝純 ARK 模式（不對帳）。

所有問答透過 macOS 原生視窗（osascript）進行；API Key／Secret Key 以
「隱藏輸入」視窗收集，路徑是：視窗 → Python 變數 → ~/.ark-toolkit/.env
（權限 600）。機密全程不落 stdout，因此不會進入任何 AI 助理的對話上下文。
"""
import argparse
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark      # noqa: E402
import dialogs  # noqa: E402
import source   # noqa: E402

ENV_PATH = source.CREDENTIALS_FALLBACK


# ---------------------------------------------------------------- 新增帳戶

def setup_shioaji():
    """收集並驗證永豐金鑰，成功回傳帳戶設定；失敗或取消回傳 None。"""
    key = dialogs.ask_hidden("請輸入 Shioaji API Key\n（輸入不會顯示，也不會出現在終端機）")
    if not key:
        return None
    secret = dialogs.ask_hidden("請輸入 Shioaji Secret Key")
    if not secret:
        return None

    print("驗證憑證（測試登入）…", flush=True)
    try:
        import shioaji as sj
        api = sj.Shioaji(simulation=False)
        api.login(api_key=key, secret_key=secret)
        n = len(api.list_positions(api.stock_account, unit=sj.Unit.Share))
    except Exception as e:
        dialogs.notify(f"登入失敗：{type(e).__name__}\n請確認金鑰是否正確。")
        print(f"✗ Shioaji 登入失敗（{type(e).__name__}），未儲存任何資料", flush=True)
        return None

    os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
    # 同目錄暫存檔（mkstemp 預設 600）寫好再原子替換：不追隨既有 symlink，
    # 中途失敗也不會留下半寫的憑證檔
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(ENV_PATH), prefix=".env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(f"SHIOAJI_API_KEY={key}\nSHIOAJI_SECRET_KEY={secret}\n")
        os.replace(tmp, ENV_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"✓ 憑證已驗證（帳上 {n} 檔持倉）並存至 {ENV_PATH}（權限 600）", flush=True)
    return {"type": "shioaji", "name": "永豐"}


def setup_file(accounts):
    """選檔＋欄位對應＋試讀＋命名，成功回傳帳戶設定；失敗或取消回傳 None。"""
    path = dialogs.choose_file("選擇券商匯出的持股檔（CSV 或 Excel）")
    if not path:
        return None
    try:
        columns = dialogs.pick_columns(path)
        if columns is None:
            return None
        positions = source.read_file_positions(path, columns)
    except RuntimeError as e:
        dialogs.notify(f"檔案讀取失敗：\n{e}")
        print(f"✗ 檔案讀取失敗：{e}", flush=True)
        return None

    default = os.path.splitext(os.path.basename(path))[0]
    while True:
        name = dialogs.ask_text(f"讀到 {len(positions)} 檔持股。\n為這個帳戶取個名字：", default)
        if name is None:
            return None
        name = name.strip()
        if not name:
            continue
        if any(a["name"] == name for a in accounts):
            dialogs.notify(f"帳戶名稱「{name}」已存在，請換一個。")
            continue
        break
    print(f"✓ 檔案驗證通過（{len(positions)} 檔）：{path}", flush=True)
    return {"type": "file", "name": name, "path": path, "columns": columns}


def add_account(accounts):
    kind = dialogs.ask_buttons("要新增哪種帳戶？",
                               ["永豐 Shioaji", "檔案（CSV／Excel）", "返回"])
    if kind == "永豐 Shioaji":
        if any(a["type"] == "shioaji" for a in accounts):
            dialogs.notify("永豐 Shioaji 帳戶已存在。要更新金鑰請先移除再新增。")
            return None
        return setup_shioaji()
    if kind == "檔案（CSV／Excel）":
        return setup_file(accounts)
    return None


# ---------------------------------------------------------------- 主流程

def summarize(accounts):
    if not accounts:
        return "（無帳戶＝純 ARK 模式，不對帳）"
    return "\n".join(
        f"・{a['name']}（{'Shioaji' if a['type'] == 'shioaji' else '檔案：' + a['path']}）"
        for a in accounts)


def apply_cost_basis(cfg, picked):
    """視窗的口徑選擇套回設定；取消（None）就維持原狀。"""
    return cfg if picked is None else {**cfg, "cost_basis": picked}


def run_wizard():
    try:
        cfg = source.load_config()
    except source.SetupRequired:
        cfg = {"version": 2, "accounts": []}
    accounts = [dict(a) for a in cfg["accounts"]]

    while True:
        choice = dialogs.ask_buttons(
            f"目前帳戶清單：\n{summarize(accounts)}\n\n"
            "多帳戶時同檔股票會合併（股數相加、均價加權平均）。",
            ["新增帳戶", "移除帳戶", "完成"])
        if choice is None:
            print("已取消，未變更任何設定", flush=True)
            return 1
        if choice == "新增帳戶":
            account = add_account(accounts)
            if account:
                accounts.append(account)
        elif choice == "移除帳戶":
            if not accounts:
                dialogs.notify("目前沒有帳戶可移除。")
                continue
            picked = dialogs.choose_from_list("選擇要移除的帳戶：", [a["name"] for a in accounts])
            if picked:
                accounts = [a for a in accounts if a["name"] != picked]
        else:  # 完成
            if not accounts and dialogs.ask_buttons(
                "帳戶清單為空＝純 ARK 模式：不與任何券商資料對帳。\n"
                "ark-analyze／ark-explore 照常可用，但 ark-sync／ark-collect／ark-read 停用。\n"
                "確定嗎？",
                ["返回", "確定"],
            ) != "確定":
                continue
            new_cfg = {**cfg, "version": 2, "accounts": accounts}
            if source.has_shioaji(new_cfg):
                # 口徑只對 Shioaji 有意義（檔案帳戶只有一欄均價）。第一次 sync 也會問，
                # 這裡是「之後想改」的入口
                new_cfg = apply_cost_basis(new_cfg, dialogs.ask_cost_basis(source.cost_basis(new_cfg)))
            path = source.save_config(new_cfg)
            print(f"✓ 設定完成：{source.describe(new_cfg)}（{path}）", flush=True)
            return 0


def main():
    ap = argparse.ArgumentParser(description="ark-toolkit 帳戶清單設定精靈（macOS 原生視窗）")
    ap.add_argument("--show", action="store_true", help="只顯示目前設定（不開視窗、不含機密）")
    args = ap.parse_args()

    if args.show:
        try:
            print(f"目前帳戶：{source.describe(source.load_config())}")
        except source.SetupRequired as e:
            print(f"⚠️  {e}")
            return 1
        return 0

    ark.check_platform(tool="ark-setup")
    return run_wizard()


if __name__ == "__main__":
    sys.exit(main())
