"""macOS 原生對話視窗（osascript）。ark-setup 與 ark-collect 共用。

機密一律走 ask_hidden：輸入內容走「視窗 → Python 變數」，全程不經 stdout，
因此不會進入任何 AI 助理的對話上下文，也就沒有被注入或外洩的路徑。
"""
import subprocess

import source

TITLE = "ark-toolkit 設定"

FIELD_LABELS = {"code": "股票代號", "qty": "股數", "price": "成交均價"}


def _osascript(script):
    """執行 AppleScript；使用者按「取消」時回傳 None。"""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def _q(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def ask_buttons(message, buttons):
    joined = ", ".join(f'"{_q(b)}"' for b in buttons)
    return _osascript(
        f'button returned of (display dialog "{_q(message)}" '
        f'buttons {{{joined}}} default button 1 with title "{TITLE}")'
    )


def ask_hidden(message):
    """隱藏輸入視窗——輸入內容不回顯，也絕不印到 stdout。"""
    return _osascript(
        f'text returned of (display dialog "{_q(message)}" default answer "" '
        f'with hidden answer with title "{TITLE}")'
    )


def ask_text(message, default=""):
    """可見輸入視窗（非機密用，如帳戶名稱）。"""
    return _osascript(
        f'text returned of (display dialog "{_q(message)}" '
        f'default answer "{_q(default)}" with title "{TITLE}")'
    )


def choose_file(message):
    return _osascript(f'POSIX path of (choose file with prompt "{_q(message)}")')


def choose_from_list(message, items):
    joined = ", ".join(f'"{_q(i)}"' for i in items)
    got = _osascript(
        f'choose from list {{{joined}}} with prompt "{_q(message)}" with title "{TITLE}"'
    )
    return None if got in (None, "false") else got


def notify(message):
    ask_buttons(message, ["好"])


def pick_columns(path):
    """表頭自動對應，認不出的欄位以下拉選單詢問；使用者取消回傳 None。"""
    headers = source.sniff_headers(path)
    columns = source.auto_map_columns(headers)
    for key, header in columns.items():
        if header is None:
            picked = choose_from_list(
                f"認不出「{FIELD_LABELS[key]}」欄位，請從表頭中選擇：", headers)
            if picked is None:
                return None
            columns[key] = picked
    return columns
