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
"""探索 ARK 全 App 結構，產出功能地圖的原料。

以 App 版本號當快取鍵：版本沒變就不重跑（探索一次要好幾分鐘）。
唯讀——只按會導致換頁的元素，不打字、不儲存、不觸發金流。
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark  # noqa: E402
import explore  # noqa: E402


def show_cached():
    paths = sorted(glob.glob(os.path.join(explore.MAP_DIR, "*.json")))
    maps = [p for p in paths if not os.path.basename(p).startswith("probe-")]
    if not maps:
        print(f"尚無任何地圖快取（{explore.MAP_DIR}）")
        return 0
    for path in maps:
        with open(path, encoding="utf-8") as fh:
            app_map = json.load(fh)
        print(f"\n版本 {app_map.get('version')}　探索於 {app_map.get('explored_at')}"
              f"　{len(app_map.get('pages', []))} 頁　{path}")
        for page in app_map.get("pages", []):
            print(f"  {' › '.join(page['path'])}")
    return 0


def run_probe(ax, pid):
    """純讀取診斷：dump 當前畫面完整 AX tree。不按任何東西。"""
    tree = explore.probe(ax, ax.window(pid))
    os.makedirs(explore.MAP_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(explore.MAP_DIR, f"probe-{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(tree, fh, ensure_ascii=False, indent=2)

    w = ax.window(pid)
    texts = explore.screen_texts(ax, w)
    elements = explore.named_elements(ax, w)
    print(f"AX tree 已寫入 {path}")
    print(f"\n畫面文字 {len(texts)} 段：")
    for t in texts[:40]:
        print(f"  {t}")
    print(f"\n可按元素 {len(elements)} 個：")
    for name, role, _el, point in elements:
        mark = "" if explore.is_onscreen(point, explore.window_bounds(ax, w)) else "  ← 離屏"
        print(f"  [{role}] {name}　@{point}{mark}")
    print(f"\n底部 tab 推定：{explore.bottom_tabs(ax, w)}")
    print(f"版本號推定：{explore.find_version(texts)}")
    return 0


def print_report(pages, extras):
    report = explore.element_report(pages)
    print("\n【全 App 可按元素分類】")
    labels = {explore.NAVIGATION: "導航類（可到達新頁）",
              explore.ACTION: "動作類（原地生效）",
              explore.BLOCKED: "金流／登出類（已擋，未按）",
              explore.DATA: "資料格（股價／持股列，未展開）",
              explore.OFFSCREEN: "離屏（橫向捲軸外，按了會靜默失效）",
              explore.UNVISITED: "未走訪"}
    for kind, label in labels.items():
        print(f"  {label:<24}{report['counts'].get(kind, 0)} 個")

    print("\n【金流／登出類的實際位置】")
    if not report["blocked"]:
        print("  （無）—— 全 App 沒有任何符合訂閱／購買／付款／升級／續約／登出的可按元素")
    for item in report["blocked"]:
        print(f"  {item['page']} › {item['name']}")

    if report["unvisited"]:
        print(f"\n【未走訪的元素 {len(report['unvisited'])} 個】（深度上限或黑名單所致，請判讀）")
        for item in report["unvisited"][:40]:
            print(f"  {item['page']} › {item['name']}")

    if extras.get("dead_roots"):
        print(f"\n【放棄的子樹】{'、'.join(extras['dead_roots'])}")
        print("  App 記住了某個子分頁的選擇，導致兄弟節點走不到。"
              "手動把該 tab 切回預設子分頁後重跑 --force 可補齊。")

    if extras.get("actions"):
        print(f"\n【動作型元素 {len(extras['actions'])} 個】按下不換頁，已標記不重按")
        for path in extras["actions"][:40]:
            print(f"  {' › '.join(path)}")


def main():
    ap = argparse.ArgumentParser(description="探索 ARK 全 App 結構")
    ap.add_argument("--probe", action="store_true",
                    help="只 dump 當前畫面的 AX tree，不按任何東西（零風險診斷）")
    ap.add_argument("--force", action="store_true", help="忽略版本快取，強制重新探索")
    ap.add_argument("--show", action="store_true", help="只印既有快取，完全不碰 App")
    ap.add_argument("--markdown", metavar="PATH", help="把 Guide 骨架寫到指定路徑")
    ap.add_argument("--max-depth", type=int, default=explore.MAX_DEPTH)
    ap.add_argument("--max-pages", type=int, default=explore.MAX_PAGES)
    args = ap.parse_args()

    if args.show:
        return show_cached()

    ark.check_platform(tool="ark-explore")
    import ax

    pid = ax.activate()
    if args.probe:
        return run_probe(ax, pid)

    print("讀取 App 版本號…", flush=True)
    version = explore.read_version(ax, pid)
    cached = explore.load_cache(version) if version else None
    action, why = explore.cache_decision(version, cached)
    print(f"  {why}")

    if action == explore.SKIP and cached and not args.force:
        print(f"\n跳過探索。已知 {len(cached.get('pages', []))} 頁：")
        for page in cached.get("pages", []):
            print(f"  {' › '.join(page['path'])}")
        print("\n加 --force 可強制重跑。")
        return 0

    print("\n開始探索（過程中請不要操作電腦——App 切到背景會被 iOS 暫停，AX 全部失效）\n",
          flush=True)
    pages, extras = explore.walk(ax, pid, args.max_depth, args.max_pages)

    now = dt.datetime.now().replace(microsecond=0).isoformat()
    app_map = explore.build_map(pages, version, now)
    path = explore.save_map(app_map)
    print(f"\n探索完成：{len(pages)} 頁，已存至 {path}")

    print_report(pages, extras)

    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(explore.to_markdown(app_map))
        print(f"\nGuide 骨架已寫入 {args.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
