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
"""前置檢查：確認排程真的跑得起來。**完全無副作用，只讀。**

存在的理由是 macOS 的輔助使用（Accessibility）權限是**按執行檔授權**的——
Terminal 拿到的權限不會自動延伸到 launchd job。這件事若等到 10:00 真要下單時
才發現，那天就報銷了。所以要能在 launchd 環境下先驗一次。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark     # noqa: E402
import source  # noqa: E402


def check_ax():
    import ax
    pid = ax.ensure_ready()
    w = ax.window(pid)
    if w is None:
        return "AX 讀不到主視窗"
    if not ax.by_desc(w, "運算"):
        return "AX 讀不到底部分頁（畫面可能停在編輯頁或彈窗）"
    posture = ark.read_posture(ax, pid)
    if posture is None:
        return "讀不到運算頁金額（隱私眼睛可能開著）"
    return None


def check_broker():
    import shioaji as sj
    source.load_credentials()
    api = sj.Shioaji(simulation=False)
    try:
        api.login(api_key=os.environ["SHIOAJI_API_KEY"],
                  secret_key=os.environ["SHIOAJI_SECRET_KEY"])
        if api.stock_account is None:
            return "登入成功但取不到證券帳戶"
        ca = os.environ.get("SHIOAJI_CA_PATH")
        if ca and not os.path.exists(ca):
            return f"憑證檔不存在：{ca}"
        return None
    finally:
        try:
            api.logout()
        except Exception:                                       # noqa: BLE001
            pass


def main():
    ark.check_platform(tool="ark-agent")
    problems = []
    for name, fn in (("ARK / 輔助使用", check_ax), ("券商連線", check_broker)):
        try:
            err = fn()
        except Exception as e:                                  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        print(f"  {'✅' if err is None else '🛑'} {name}"
              f"{'' if err is None else '：' + err}", flush=True)
        if err:
            problems.append(f"{name}（{err}）")
    if problems:
        print(f"\n🛑 前置檢查未過：{'；'.join(problems)}", file=sys.stderr)
        return 1
    print("\n✅ 前置檢查全過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
