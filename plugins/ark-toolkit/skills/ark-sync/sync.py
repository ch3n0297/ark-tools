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
"""把方舟運算(ARK)的台股庫存同步成真實持倉。

以 ark-setup 設定的帳戶清單為準（永豐 Shioaji API＋任意個檔案帳戶），
ARK 向它對齊（修改／新增／刪除）。只有永豐時即時讀取；有檔案帳戶時只吃
ark-collect 確認過的當日快照——確認過的才是實際套用的。
讀取與解析共用 lib/ark.py；本檔只放同步專屬邏輯。
"""
import argparse
import datetime as dt
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark     # noqa: E402
import source  # noqa: E402

UnsupportedPlatform = ark.UnsupportedPlatform
ParseFailed = ark.ParseFailed
PRICE_TOL = ark.PRICE_TOL
parse_holding = ark.parse_holding
parse_edit_row = ark.parse_edit_row
check_parsed = ark.check_parsed


def check_platform(platform=None):
    ark.check_platform(platform, tool="ark-sync")


# ---------------------------------------------------------------- 差異計算

def plan_changes(current, target):
    """以 Shioaji 為準，算出 ARK 需要的動作。

    回傳 [(action, code, 目標股數, 目標均價, ARK 現況或 None)]，依代號排序。
    """
    plan = []
    for code in sorted(set(current) | set(target)):
        cur, want = current.get(code), target.get(code)
        if want is None:
            plan.append(("delete", code, 0, 0.0, cur))
        elif cur is None:
            plan.append(("add", code, want[0], want[1], None))
        elif cur[0] != want[0] or abs(cur[1] - want[1]) >= PRICE_TOL:
            plan.append(("update", code, want[0], want[1], cur))
    return plan


MAX_DELETE_RATIO = 0.5


def sync_is_safe(current, target, max_delete_ratio=MAX_DELETE_RATIO, allow_delete=True):
    """執行前的安全閘，回傳 (可否執行, 原因)。

    真實來源若因登入失敗回傳空 dict（Shioaji 就是如此），`plan_changes` 會照規則
    判定「ARK 全部刪除」—— 與 `check_parsed` 防的是同一類「失敗偽裝成成功」，
    差別在這次發生於寫入端：讀取端讀錯只是報告不準，寫入端寫錯是庫存不見了。

    因此空的目標一律拒絕——即使 ARK 也是空的（雙空看起來「一致」，但更可能是
    來源讀失敗，不能回報成功）；刪除量過半也拒絕（要真的清倉就自己加 --force），
    但只在刪除真的會執行時檢查（`allow_delete`），否則會連安全的新增／更新一起攔下。
    """
    if not target:
        return False, "真實持倉來源回傳 0 檔（可能是登入或讀取失敗），拒絕執行"
    if not allow_delete:
        return True, ""
    doomed = sorted(c for c in current if c not in target)
    if current and len(doomed) > len(current) * max_delete_ratio:
        return False, (f"計畫刪除 {len(doomed)}/{len(current)} 檔，超過半數，拒絕執行："
                       f"{', '.join(doomed)}")
    return True, ""


# ---------------------------------------------------------------- 編輯頁操作

def visible_rows(ax, pid):
    """編輯頁目前可見的列：{代號: (元素, 股數, 均價)}"""
    out = {}
    for el, d in ax.descs(ax.window(pid), "AXStaticText"):
        row = parse_edit_row(d)
        if row:
            out[row[0]] = (el, row[1], row[2])
    return out


def find_row_button(ax, el):
    """一列裡沒有 description 的那顆按鈕才是「點進編輯」"""
    row = ax.attr(el, "AXParent")
    btns = [k for k in (ax.attr(row, "AXChildren") or [])
            if ax.attr(k, "AXRole") == "AXButton" and not ax.attr(k, "AXDescription")]
    return btns[0] if btns else None


def page_edit_list(ax, pid, action, timeout=2.0):
    """對編輯庫存頁翻一頁。

    這一頁的滾輪、方向鍵、拖到邊緣自動捲動全部無效——reorder 手勢辨識器把
    **輸入事件**攔下了。但 AX action 不是輸入事件，`AXScrollDownByPage`
    由 UIKit 的 accessibility 層直接派給 scroll view，繞過手勢辨識器。
    """
    rows = visible_rows(ax, pid)
    if not rows:
        return False
    before = tuple(sorted(rows))
    ax.perform(next(iter(rows.values()))[0], action)
    # 輪詢可見列變化而非固定等待——渲染慢時固定等待會把「還沒畫完」誤判成
    # 「已到底」，讓 scroll_to_row 漏掉本來捲得到的列。到頂／到底時畫面不變，
    # 等滿 timeout 後交由呼叫端比對前後畫面收斂。
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tuple(sorted(visible_rows(ax, pid))) != before:
            break
        time.sleep(0.2)
    return True


def scroll_to_row(ax, pid, code, max_pages=30):
    """把目標列捲進畫面，找到回傳 True。

    離屏的列根本不在 AX tree 裡，所以無法對目標自己做 AXScrollToVisible——
    只能一頁一頁翻，每翻一頁重看畫面上有誰。先往上翻到頂再往下找，
    否則從中段開始會漏掉上方的列。
    """
    for action in ("AXScrollUpByPage", "AXScrollDownByPage"):
        previous = None
        for _ in range(max_pages):
            current = tuple(sorted(visible_rows(ax, pid)))
            if code in current:
                return True
            if current == previous:
                break               # 這個方向已到底，換方向
            previous = current
            if not page_edit_list(ax, pid, action):
                return False
    return code in visible_rows(ax, pid)


def posture_cash(balance, settlements, external_cash):
    """運算頁該填的台幣現金。

    官方定義：總資源＝現金＋緊急預備金＋**T+2 內可動用資金**＋必要時能動用的
    貸款額度，且輸入的是「所有的錢」（含薪水）。因此券商餘額只是其中一項：
    未交割款要加減（當日買了 1000 先扣 1000、賣得 1 萬先加 1 萬），帳戶外的
    現金由 `external_cash` 帶入——那部分系統看不到，只能由設定給。

    ARK 的欄位吃整數，且負數收不了，所以取整並夾在 0 以上。
    """
    total = (balance + settlements.get("today", 0.0) + settlements.get("t1", 0.0)
             + settlements.get("t2", 0.0) + external_cash)
    return float(max(0, round(total)))


def _fill(ax, pid, field_index, value):
    """相容包裝：欄位寫入已提升到 lib/ark.py 供運算頁現金欄共用。"""
    return ark.fill_field(ax, pid, field_index, value)


def _verify_and_save(ax, pid, qty, price, popup_label):
    """用 App 自算的「總成本」當獨立見證，通過才儲存。"""
    w = ax.window(pid)
    fields = ax.text_fields(w)
    got_qty = str(ax.attr(fields[-2], "AXValue") or "").replace(",", "")
    got_price = str(ax.attr(fields[-1], "AXValue") or "").replace(",", "")
    cost_text = next((d for _e, d in ax.descs(w, "AXStaticText") if d.startswith("總成本")), "")
    m = re.search(r"([\d,]+(?:\.\d+)?)", cost_text)
    actual = ark.num(m.group(1)) if m else None
    expect = float(qty) * float(price)

    ok = (got_qty == str(qty) and got_price == str(price)
          and actual is not None and abs(actual - expect) <= 2)
    print(f"    填入 {got_qty} 股 / {got_price} → {cost_text}（預期 {expect:.2f}）")
    if not ok:
        print("    ✗ 驗算不符，取消不儲存")
        _close_popup(ax, pid)         # 別直接 by_desc(...)[0]：X 可能不在 AX tree 裡
        return False

    ax.dismiss_keyboard(pid)          # 不點掉「完成」，儲存鈕會被蓋住
    ax.press(ax.by_desc(ax.window(pid), "儲存")[0])
    time.sleep(2.2)
    if ax.by_desc(ax.window(pid), popup_label):
        print("    ✗ 儲存後彈窗未關閉")
        _close_popup(ax, pid)         # 殘留的彈窗會害下一檔的操作全部失效
        return False
    return True


def update_holding(ax, pid, code, qty, price):
    rows = visible_rows(ax, pid)
    if code not in rows:
        if not scroll_to_row(ax, pid, code):
            print(f"    ✗ {code} 捲遍整份列表仍找不到")
            return False
        rows = visible_rows(ax, pid)
    btn = find_row_button(ax, rows[code][0])
    if btn is None:
        print(f"    ✗ {code} 找不到編輯入口")
        return False
    ax.press(btn)
    time.sleep(1.8)
    if not ax.by_desc(ax.window(pid), "編輯持股"):
        print(f"    ✗ {code} 未進入編輯彈窗（元素可能被遮擋）")
        return False
    _fill(ax, pid, 0, str(qty))
    _fill(ax, pid, 1, str(price))
    return _verify_and_save(ax, pid, qty, price, "編輯持股")


def _close_popup(ax, pid, tries=3):
    """關掉新增／編輯彈窗，回到編輯庫存頁。

    「新增台股持股」右上角的 X **不在 AX tree 裡**（同 dismiss_keyboard 的問題），
    只能用座標點；而搜尋下拉開著時要點兩次（第一次只收下拉）。
    原本直接 `by_desc(w, "popup close")[0]`，找不到就拋 IndexError，
    把「已處理的失敗」變成 traceback。
    """
    for _ in range(tries):
        w = ax.window(pid)
        if ax.by_desc(w, "新增持股"):
            return True
        found = ax.by_desc(w, "popup close") or ax.by_desc(w, "close")
        if found:
            ax.press(found[0])
        else:
            pos = ax.point(w)
            if pos is None:
                return False
            ax.click(pos[0] + 345, pos[1] + 74)
        time.sleep(1.2)
    return bool(ax.by_desc(ax.window(pid), "新增持股"))


def add_holding(ax, pid, code, qty, price):
    ax.press(ax.by_desc(ax.window(pid), "新增持股")[0])
    time.sleep(1.8)
    w = ax.window(pid)
    ax.press(ax.text_fields(w)[0])
    time.sleep(0.5)
    ax.keystroke(pid, code)
    time.sleep(2.2)

    w = ax.window(pid)
    hits = [e for e, d in ax.descs(w, "AXStaticText") if d.startswith(code + ",")]
    if not hits:
        print(f"    ✗ 搜尋不到 {code}")
        _close_popup(ax, pid)
        return False
    row = ax.attr(hits[0], "AXParent")
    btn = [k for k in (ax.attr(row, "AXChildren") or []) if ax.attr(k, "AXRole") == "AXButton"][0]
    ax.press(btn)
    time.sleep(2.0)

    w = ax.window(pid)
    if [d for _e, d in ax.descs(w) if "已存在" in d]:
        print(f"    ✗ {code} 已存在於 ARK，新增表單無法更新既有部位")
        _close_popup(ax, pid)
        return False
    _fill(ax, pid, 1, str(qty))
    _fill(ax, pid, 2, str(price))
    return _verify_and_save(ax, pid, qty, price, "新增台股持股")


def delete_holding(ax, pid, code):
    rows = visible_rows(ax, pid)
    if code not in rows:
        if not scroll_to_row(ax, pid, code):
            print(f"    ✗ {code} 捲遍整份列表仍找不到")
            return False
        rows = visible_rows(ax, pid)
    row = ax.attr(rows[code][0], "AXParent")
    btns = [k for k in (ax.attr(row, "AXChildren") or [])
            if ax.attr(k, "AXDescription") == "watchlist delete"]
    if not btns:
        print(f"    ✗ {code} 找不到刪除鈕")
        return False
    ax.press(btns[0])
    time.sleep(2.0)
    return code not in visible_rows(ax, pid)


# ---------------------------------------------------------------- 主流程

def describe(plan):
    labels = {"update": "修改", "add": "新增", "delete": "刪除"}
    lines = []
    for action, code, qty, price, cur in plan:
        now = f"{cur[0]} / {cur[1]}" if cur else "（無）"
        want = "（移除）" if action == "delete" else f"{qty} / {price}"
        lines.append(f"  {labels[action]}  {code:<8} {now:>18}  →  {want}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="把 ARK 台股庫存同步成真實持倉（來源由 ark-setup 設定）")
    ap.add_argument("--dry-run", action="store_true", help="只顯示差異，不做任何寫入")
    ap.add_argument("--allow-delete", action="store_true",
                    help="允許刪除 ARK 有而 Shioaji 沒有的部位（預設略過）")
    ap.add_argument("--force", action="store_true",
                    help="略過安全閘（來源讀空、或刪除量過半時仍執行）")
    ap.add_argument("--with-cash", action="store_true",
                    help="一併把運算頁的台幣現金欄同步成券商可動用資金"
                         "（＋config.json 的 external_cash）")
    args = ap.parse_args()

    check_platform()
    try:
        cfg = source.load_config()
    except source.SetupRequired as e:
        print(f"⚠️  {e}")
        return 2
    if source.is_pure_ark(cfg):
        print("⚠️  目前為純 ARK 模式（不對帳）。ark-sync 需要真實持倉來源，"
              "請執行 ark-setup 變更設定。")
        return 2
    if source.needs_staging(cfg):
        # 有檔案帳戶：只吃 ark-collect 確認過的當日快照——確認過的才是實際套用的
        try:
            staging = source.load_staging()
        except source.StagingRequired as e:
            print(f"⚠️  {e}")
            return 2
        counts = "＋".join(f"{n} {len(p)} 檔" for n, p in staging["accounts"].items())
        print(f"讀取收集快照（{counts}，{staging['created_at']}）…", flush=True)
        target = staging["merged"]
    else:
        print(f"讀取真實持倉（{source.describe(cfg)}）…", flush=True)
        target = source.read_positions(cfg) or {}     # 純 ARK 已在上面擋掉，None 不會到這
    print(f"  合併 {len(target)} 檔", flush=True)

    import ax

    pid = ax.ensure_ready()       # 不搶焦點：ARK 視窗可見即可，使用者可繼續用電腦
    pid = ark.ensure_responsive(ax, pid)   # 殭屍態（事件層死掉）在這裡自癒
    print("讀取 ARK 庫存…")
    holdings = ark.read_holdings(ax, pid)
    declared = ark.read_declared_count(ax, pid)
    check_parsed(holdings, declared)                 # 與 App 宣告的檔數交叉驗證
    print(f"  ARK {len(holdings)} 檔" + (f"（App 宣告 {declared} 檔）" if declared else ""))

    current = {code: (h.qty, h.price) for code, h in holdings.items()}
    plan = plan_changes(current, target)
    if plan:
        print(f"\n需要 {len(plan)} 項變更：")
        print(describe(plan))

    # 安全閘要在「無變更即成功」之前——雙空（來源讀失敗＋ARK 空）看起來一致，
    # 其實是失敗偽裝成成功
    safe, reason = sync_is_safe(current, target, allow_delete=args.allow_delete)
    if not safe:
        print(f"\n🛑 安全閘攔下：{reason}")
        if not args.force:
            print("    確認 Shioaji 登入正常後再執行；真的要照做請加 --force。")
            return 2
        print("    --force 已指定，繼續執行。")

    if not plan:
        print("\n✅ 庫存已完全一致，無需變更")
        if not cash_step_needed(args.with_cash, args.dry_run):
            return 0
        return 0 if sync_cash(ax, pid, cfg) else 1

    if args.dry_run:
        print("\n（dry-run，未做任何寫入）")
        return 0

    skipped = [c for a, c, *_ in plan if a == "delete" and not args.allow_delete]
    if skipped:
        print(f"\n略過刪除 {len(skipped)} 檔（需要 --allow-delete）：{', '.join(skipped)}")

    print("\n開始執行…")
    ark.enter_edit_page(ax, pid)
    ok = 0
    applied = []
    todo = [(a, c, q, p) for a, c, q, p, _cur in plan
            if not (a == "delete" and not args.allow_delete)]
    for attempt in (1, 2):
        # 失敗項重試一輪：AX 操作偶發失手（實例：2026-08-11 的 00635U 刪除），
        # 同樣的操作重跑一次多半就過；仍失敗才回報，交給收尾的 --dry-run 複驗
        failed = []
        for action, code, qty, price in todo:
            print(f"  {action} {code}" + ("（重試）" if attempt == 2 else ""))
            if action == "update":
                done = update_holding(ax, pid, code, qty, price)
            elif action == "add":
                done = add_holding(ax, pid, code, qty, price)
            else:
                done = delete_holding(ax, pid, code)
            if done:
                ok += 1
                applied.append([action, code])
                print(f"    ✅ {code} 完成")
            else:
                failed.append((action, code, qty, price))
            ark.enter_edit_page(ax, pid)  # 每檔後重置列表位置，避免遮擋
        todo = failed
        if not todo:
            break
    fail = len(todo)

    after = ark.read_declared_count(ax, pid)     # 用 App 自己宣告的檔數收尾，不用推算的
    entry = {
        "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
        "ark_count_before": len(current),
        "shioaji_count": len(target),
        "applied": applied,               # 只記實際成功的動作，失敗的看 fail
        "ok": ok, "fail": fail,
    }
    if after is not None:                 # 讀不到就不寫，別拿執行前的檔數充數
        entry["ark_count"] = after
    ark.append_sync_log(entry)

    print(f"\n完成 {ok} 項，失敗 {fail} 項")
    if args.with_cash and not sync_cash(ax, pid, cfg):
        fail += 1
    print("建議執行 --dry-run 再確認一次結果。")
    return 1 if fail else 0


def cash_step_needed(with_cash, dry_run):
    """庫存無變更時仍要不要同步現金。

    現金與庫存是兩件獨立的事：庫存一致不代表現金欄也對。實例（2026-08-14）
    ——換股當天庫存同步成功、現金欄寫入失敗，重跑時因為庫存已一致就提早收工，
    現金永遠補不上，欄位停在舊值直到有人察覺。
    """
    return bool(with_cash) and not dry_run


def sync_cash(ax, pid, cfg):
    """把運算頁台幣現金欄同步成券商可動用資金（＋帳戶外現金）。回傳是否成功。

    ARK 用這個數字算建議比例與參考調節，官方教學：「現金輸錯，整個方向都反」。
    帳戶外的錢系統看不到，由 config.json 的 `external_cash` 帶入——沒設就是 0，
    此時填進去的只有券商那部分，若你實際還有別的錢，建議去設定。
    """
    import shioaji as sj

    source.load_credentials()
    api = sj.Shioaji(simulation=False)
    api.login(api_key=os.environ["SHIOAJI_API_KEY"],
              secret_key=os.environ["SHIOAJI_SECRET_KEY"])
    try:
        balance = float(api.account_balance().acc_balance)
        s = api.list_settlements(api.stock_account)
        settlements = {"today": float(s.t_money), "t1": float(s.t1_money),
                       "t2": float(s.t2_money)}
    finally:
        api.logout()

    external = float(cfg.get("external_cash", 0.0))
    target = posture_cash(balance, settlements, external)
    print(f"\n同步運算頁現金欄 → {target:,.0f}"
          f"（券商 {balance:,.0f}＋未交割 {sum(settlements.values()):,.0f}"
          f"＋帳戶外 {external:,.0f}）")
    if ark.write_posture_cash(ax, pid, target):
        print("  ✅ 已寫入")
        return True
    print("  ✗ 寫入失敗，欄位維持原值——後續決策應視為失效保護條件，不交易")
    return False


if __name__ == "__main__":
    sys.exit(main())
