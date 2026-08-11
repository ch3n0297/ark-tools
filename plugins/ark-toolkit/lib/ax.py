"""macOS Accessibility 操作層（僅供 ark-sync 使用）

只匯入 pyobjc，不含任何 ARK 業務邏輯。
"""
# pyobjc 的符號是執行期動態綁定，靜態檢查看不到
# pyright: reportAttributeAccessIssue=false
import re
import subprocess
import time

from AppKit import NSApplicationActivateIgnoringOtherApps, NSWorkspace
from ApplicationServices import (
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
)
from Quartz import (
    CGEventCreate,
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventGetLocation,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventPostToPid,
    CGEventSourceCreate,
    CGWarpMouseCursorPosition,
    CGWindowListCopyWindowInfo,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventSourceStateHIDSystemState,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGNullWindowID,
    kCGScrollEventUnitPixel,
    kCGWindowListOptionAll,
    kCGWindowName,
    kCGWindowNumber,
    kCGWindowOwnerPID,
)

BUNDLE_ID = "com.galaxy.ark"
_SRC = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)


class ArkNotRunning(RuntimeError):
    pass


def _running_app():
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if (app.bundleIdentifier() or "") == BUNDLE_ID:
            return app
    return None


def launch(timeout=20.0):
    """ARK 未執行時代為啟動，回傳 NSRunningApplication。

    先分清「未安裝」與「未開啟」——沒裝 App 的使用者被叫去「先開啟」是誤導。
    """
    if NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(BUNDLE_ID) is None:
        raise ArkNotRunning(
            "未偵測到方舟運算 App（com.galaxy.ark）。"
            "請先從 App Store 安裝——iOS App 僅 Apple Silicon Mac 可安裝。"
        )
    subprocess.run(["open", "-b", BUNDLE_ID], check=True, capture_output=True, timeout=15)
    deadline = time.time() + timeout
    while time.time() < deadline:
        app = _running_app()
        if app:
            return app
        time.sleep(0.5)
    raise ArkNotRunning("已代為啟動方舟運算但程序未出現，請手動開啟後重試")


def activate(timeout=10.0):
    """把 ARK 帶到前景並回傳 pid；未執行時代為啟動、未安裝則明講。

    必要步驟：App 在背景會被 iOS 生命週期 suspend，屆時所有 AX 查詢
    都回傳 kAXErrorCannotComplete(-25204)、AXWindows 為 0。
    輪詢等到 AXWindows 讀得到才算就緒——固定等待在切換慢時會讓
    第一個 AX 查詢直接失敗。
    """
    app = _running_app()
    launched = app is None
    if launched:
        app = launch()
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    pid = app.processIdentifier()
    deadline = time.time() + (30.0 if launched else timeout)   # 冷啟動要等過場畫面
    while time.time() < deadline:
        if attr(AXUIElementCreateApplication(pid), "AXWindows"):
            return pid
        time.sleep(0.3)
    raise ArkNotRunning("方舟運算已啟動但讀不到視窗"
                        "（可能被最小化、尚未完成前景切換，或停在登入頁）")


def screenshot(pid, path):
    """截 ARK 主視窗存到 path，回傳是否成功。

    AX 讀值與畫面可能不同步（AXValue 提交前不更新、AXPress 假成功），
    卡關時看畫面是最終手段；無人值守失敗也靠這個留現場。window id 每次
    動態解析——App 重啟後 id 會變，快取住的 id 只會截到空氣。
    """
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []:
        if w.get(kCGWindowOwnerPID) == pid and w.get(kCGWindowName):
            r = subprocess.run(["screencapture", "-o", "-x",
                                f"-l{w[kCGWindowNumber]}", path],
                               capture_output=True, check=False)
            return r.returncode == 0
    return False


def restart_app(timeout=40.0):
    """終止並重啟 ARK，回傳新 pid（登入態保留）。

    App 從最小化喚醒後可能進入殭屍態：AX 讀得到、內部計時器在走，但
    AXPress／座標點擊／activate 全部無效（press 照樣回成功）。實測唯一
    解法是重啟（2026-08-11）。呼叫端應先以 ark.ensure_responsive 探測，
    確認殭屍才走到這裡。
    """
    app = _running_app()
    if app is not None:
        app.terminate()
        deadline = time.time() + 10.0
        while _running_app() is not None and time.time() < deadline:
            time.sleep(0.5)
        if _running_app() is not None:
            app.forceTerminate()
            time.sleep(1.0)
    return activate(timeout)


def ensure_ready(timeout=10.0):
    """讓 ARK 可被 AX 操作但**不搶焦點**；未執行時代為啟動。

    失焦（背景但視窗可見）不會觸發 iOS 生命週期 suspend——隱藏／最小化才會
    （約 20 秒後 AX 全回 -25204）。所以平常不必 activate，使用者可以同時用
    電腦；讀不到視窗時才 unhide＋activate 喚醒一次。
    """
    app = _running_app()
    if app is None:
        launch()
        return activate()          # 冷啟動本來就在前景，沿用既有的等待邏輯
    pid = app.processIdentifier()
    deadline = time.time() + timeout
    woken = False
    while time.time() < deadline:
        try:
            window(pid)
            return pid
        except ArkNotRunning:
            if not woken:
                woken = True
                app.unhide()
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.3)
    raise ArkNotRunning("方舟運算在執行中但讀不到視窗（可能停在登入頁）")


def attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def window(pid):
    app = AXUIElementCreateApplication(pid)
    wins = attr(app, "AXWindows")
    if wins:
        return wins[0]
    # 失焦時 AXWindows 列舉回空（err=0）但 AXMainWindow 仍讀得到——這不是
    # App 被暫停（那會回 -25204），只是列舉被藏起來。靠這個 fallback，
    # 整條操作鏈才能在 ARK 不在前景時運作。
    win = attr(app, "AXMainWindow")
    if win is not None:
        return win
    raise ArkNotRunning("讀不到 ARK 視窗（App 可能被隱藏／最小化而暫停）")


def find(el, pred, out=None):
    if out is None:
        out = []
    if pred(el):
        out.append(el)
    for kid in attr(el, "AXChildren") or []:
        find(kid, pred, out)
    return out


def by_desc(root, text):
    return find(root, lambda e: attr(e, "AXDescription") == text)


def descs(root, role=None):
    """回傳所有 (元素, description)；role 可過濾"""
    return [(e, attr(e, "AXDescription") or "")
            for e in find(root, lambda e: role is None or attr(e, "AXRole") == role)]


def press(el):
    """AXPress。注意：被遮擋的元素會回傳 0 但實際無效（見 SKILL.md 陷阱）"""
    return AXUIElementPerformAction(el, "AXPress")


def perform(el, action):
    """執行任意 AXAction。

    **AX action 不是輸入事件**，手勢辨識器攔不到它——這是繞開多個限制的關鍵：
    編輯庫存頁滾輪無效但 `AXScrollDownByPage` 有效；離屏元素 AXPress 靜默失效，
    但可先用 `AXScrollToVisible` 捲進畫面再按。
    """
    return AXUIElementPerformAction(el, action)


def actions(el):
    """元素支援的 AXAction 名稱。

    判斷「能不能按」唯一不需要製造副作用的方法——探索時不能靠試按來分辨。
    """
    err, names = AXUIElementCopyActionNames(el, None)
    return list(names) if err == 0 else []


def text_fields(root):
    return find(root, lambda e: attr(e, "AXRole") == "AXTextField")


def point(el, key="AXPosition"):
    m = re.search(r"x:([\d.-]+)\s+y:([\d.-]+)", str(attr(el, key)))
    return (float(m.group(1)), float(m.group(2))) if m else None


def size(el):
    m = re.search(r"w:([\d.-]+)\s+h:([\d.-]+)", str(attr(el, "AXSize")))
    return (float(m.group(1)), float(m.group(2))) if m else None


def keystroke(pid, s):
    """把文字送給 ARK（`CGEventPostToPid`，不經全域 HID）。

    事件只投遞給 ARK 程序：ARK 不必在前景，使用者同時打字互不干擾。
    **不能用 osascript 的 keystroke** —— 它會經過當前輸入來源，中文輸入法在全形模式下
    會把 "2308" 打成 "２３０８"，搜尋於是一無所獲、股數與均價也會寫進全形數字。
    `CGEventKeyboardSetUnicodeString` 直接指定字元，不經過輸入法。
    注意：keycode 事件（Esc、Tab、Cmd 組合鍵）與滑鼠事件走 PostToPid 都進不了
    wrapped iOS App，只有這條 unicode 文字路徑有效；打字前欄位必須已是
    first responder（AXPress 欄位即可建立，AXFocused 屬性不算數）。
    """
    for ch in s:
        for down in (True, False):
            event = CGEventCreateKeyboardEvent(_SRC, 0, down)
            CGEventKeyboardSetUnicodeString(event, len(ch), ch)
            CGEventPostToPid(pid, event)
            time.sleep(0.03)
    time.sleep(0.35)


def backspace(pid, n):
    """退格 n 次。\\x08 走 keystroke 的 unicode 文字路徑。

    取代原本 osascript System Events 的 key code 51——那是全域按鍵，
    需要 ARK 在前景，且會跟使用者的輸入打架。
    """
    keystroke(pid, "\x08" * n)


def click(x, y):
    """真實滑鼠點擊。用於不在 ARK AX tree 裡的系統輸入法元素。"""
    orig = CGEventGetLocation(CGEventCreate(None))
    CGWarpMouseCursorPosition((x, y))
    time.sleep(0.3)
    for kind in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
        CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(_SRC, kind, (x, y), kCGMouseButtonLeft))
        time.sleep(0.1)
    time.sleep(0.6)
    CGWarpMouseCursorPosition(orig)


def scroll_page(pid, action="AXScrollDownByPage", tries=4):
    """對主視窗執行 AX 捲動 action——不依賴游標位置，失焦也有效。

    iOS 依「當下可捲的方向」動態暴露這些 action：到頂/到底時該方向會消失，
    而且剛到端點後反方向的出現**有延遲**（實測約一秒）。因此找不到元素時
    先短暫重試吸收延遲，仍沒有就視為端點 no-op 回傳 None——收斂迴圈會
    因畫面不變而自然終止，單頁列表也因此不需特判。
    調節庫存頁支援（翻頁自帶重疊，不會跳列）；布局自選頁沒有任何
    可捲動的 AX 元素，那裡仍得走 scroll() 滾輪。
    """
    for _ in range(tries):
        els = find(window(pid), lambda e: action in actions(e))
        if els:
            return perform(els[0], action)
        time.sleep(0.5)
    return None


def scroll(x, y, dy, times):
    """滾輪捲動。只在非編輯的調節庫存頁有效（編輯頁被 reorder 手勢吃掉）。"""
    CGWarpMouseCursorPosition((x, y))
    time.sleep(0.3)
    for _ in range(times):
        CGEventPost(kCGHIDEventTap,
                    CGEventCreateScrollWheelEvent(_SRC, kCGScrollEventUnitPixel, 1, dy))
        time.sleep(0.2)
    time.sleep(0.8)


def dismiss_keyboard(pid):
    """收掉輸入 session，讓被蓋住的儲存鈕可按。

    打字後 ARK 在前景時會出現整條「完成」輔助列，失焦時則是右下角的
    「完成」浮鈕——兩者都會蓋住儲存鈕讓 AXPress 靜默失效，也都屬於
    系統輸入法層、不在 ARK 的 AX tree 裡。
    失焦時送 \\r 讓欄位 resign first responder（截圖驗證浮鈕即消失）；
    前景時維持點「完成」座標——需要實體滑鼠，但只剩這條路徑在用。
    """
    app = _running_app()
    if app is not None and not app.isActive():
        keystroke(pid, "\r")
        time.sleep(0.4)
        return
    pos = point(window(pid))
    if pos is None:
        raise RuntimeError("讀不到 ARK 視窗位置，無法定位鍵盤的「完成」按鈕")
    click(pos[0] + 325.5, pos[1] + 631.0)
