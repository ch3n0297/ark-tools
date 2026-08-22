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
"""每日排程編排：順序寫死、不可跳過。

    daily.py decide    平日 10:00　事實 → 風控 → 判斷 → 紀律驗證 → 送單
    daily.py settle    平日 14:30　成交對回 → 淨值 → 同步庫存與現金欄
    daily.py check     隨選　　　　前置檢查（無副作用，只讀）

**LLM 只佔 decide 的第 3 步，而且不給 Bash 工具**——它產出 decision.json 之後
就沒有話語權，紀律驗證與送單由後面兩步負責。若讓 LLM 自己編排整條流程，它可能
「忘記」跑風控或驗證，硬規則就不再是硬的。

**為什麼是 Python 而不是 shell**：macOS 刻意封鎖系統二進位檔（`/bin/bash`）在
launchd 下對 `~/Documents` 等受保護目錄的存取，以防有人用腳本繞過 TCC。實測
`/bin/bash` 連讀取本檔都會 `Operation not permitted`，而 `uv` 這類第三方 binary
有自己的 TCC 身分、讀得到。所以 launchd 的進入點必須是 `uv run daily.py`。

送單環境由 ARK_BACKEND 決定，**預設 simulation**：切實單要明確改設定，
不會因為忘記帶參數就動到真錢。
"""
import argparse
import datetime as dt
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.ark-toolkit/agent")
LOG = os.path.join(STATE, "daily.log")
SYNC = os.path.abspath(os.path.join(HERE, "..", "ark-sync", "sync.py"))
# 無人值守：config 缺均價口徑時 sync 會開視窗詢問，排程沒人按會卡死，一定帶 --no-prompt
SYNC_ARGS = ("--allow-delete", "--with-cash", "--no-prompt")

PROCEED, SKIP, FAIL = "proceed", "skip", "fail"

# (排定時刻, 容許分鐘)。decide 窗窄——盤中判斷過了時間就失效；settle 窗寬——
# 那是對既成事實的記錄，晚幾小時做結果一樣。
WINDOWS = {"decide": ("10:00", 60), "settle": ("14:30", 240)}

# 決策模型。無人值守下要動真錢，判斷品質是最不該省的地方。
DECISION_MODEL = os.environ.get("ARK_DECISION_MODEL", "fable")

# 主模型撞到用量上限時的接手模型。額度是跟著模型走的，撞牆那天會整天不交易，
# 實驗數據就多一個與市場無關的缺口——換一個額度池把當天補回來。
FALLBACK_MODEL = os.environ.get("ARK_DECISION_FALLBACK", "opus")


# ---------------------------------------------------------------- 純邏輯

def is_trading_weekday(date):
    """週末不跑。國定假日不在這裡擋——settle 以 0050 當日有無行情判定休市，
    比維護一份會過期的假日表可靠。"""
    return dt.date.fromisoformat(date).weekday() < 5


def is_within_window(now, scheduled, tolerance_minutes):
    """現在時間是否落在排定時段的容許範圍內（"HH:MM" 格式，含端點）。

    launchd 對錯過的 StartCalendarInterval 會在系統喚醒後補跑一次——機器若在
    10:00 睡著，13:00 醒來就會執行，而那時盤中價格早已不同、限價全部失效。
    時間窗必須由程式自己守，不能寄望排程器的語意。
    """
    def minutes(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    return abs(minutes(now) - minutes(scheduled)) <= tolerance_minutes


def classify_risk_exit(code):
    """risk.py 的離開碼：0 繼續、3 今日完全不能動、其餘為故障。

    混為一談的話，全面熔斷會被誤報成系統故障，反過來也一樣危險。
    """
    return {0: PROCEED, 3: SKIP}.get(code, FAIL)


def render_prompt(template, packet, envelope, decision, date):
    """把提示模板的佔位符換成實際路徑。

    換完仍殘留 `*_PATH` 字樣就報錯：Agent 會照字面把決策寫到 'DECISION_PATH'
    這個檔名，排程接著找不到檔案，而那時已經浪費一次決策機會了。
    """
    out = (template.replace("PACKET_PATH", packet)
                   .replace("ENVELOPE_PATH", envelope)
                   .replace("DECISION_PATH", decision)
                   .replace("TODAY", date))
    left = re.findall(r"\b[A-Z_]+_PATH\b", out)
    if left:
        raise ValueError(f"提示模板有未取代的佔位符：{sorted(set(left))}")
    return out


def is_quota_exhausted(output):
    """claude CLI 是否因用量上限而離開。

    只認額度訊息獨有的 `/usage-credits` 指引——把所有非零離開都當額度問題的話，
    prompt 寫錯或網路斷線也會換個模型再燒一次決策。
    """
    return "/usage-credits" in output


def fallback_model(code, output, current, fallback):
    """該不該換模型重試；要換就回傳接手模型，否則 None。

    降級只在額度這一種失敗上成立：其餘故障換模型也是一樣的結果。
    接手模型與現用相同時不重試——那是拿同一個已耗盡的額度池再撞一次。
    """
    if code == 0 or not fallback or current == fallback:
        return None
    return fallback if is_quota_exhausted(output) else None


def settle_failures(results):
    """[(步驟名, 離開碼)] → 失敗的步驟名。

    結算每一步都要盡力做完再回報：成交對回失敗不該讓淨值記錄也跳過，
    否則熔斷基準會斷掉。
    """
    return [name for name, code in results if code != 0]


# ---------------------------------------------------------------- 副作用

def in_window(mode):
    """現在是否還在該模式的執行時窗內。逾時就放棄——補跑一個過期的決策
    比不跑危險得多。`ARK_FORCE_WINDOW=1` 可略過（供手動測試）。"""
    if os.environ.get("ARK_FORCE_WINDOW"):
        return True
    scheduled, tolerance = WINDOWS[mode]
    now = dt.datetime.now().strftime("%H:%M")
    if is_within_window(now, scheduled, tolerance):
        return True
    notify(mode, f"已逾時不執行：現在 {now}，排定 {scheduled}"
                 f"（容許 ±{tolerance} 分）")
    return False


def log(mode, message):
    os.makedirs(STATE, exist_ok=True)
    stamp = dt.datetime.now().replace(microsecond=0).isoformat(" ")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} [{mode}] {message}\n")


def notify(mode, message):
    log(mode, message)
    print(message, flush=True)
    subprocess.run(["osascript", "-e",
                    f'display notification {message.replace(chr(34), chr(39))!r} '
                    f'with title "ark-agent · {mode}"'],
                   capture_output=True, check=False)


def run(mode, *args):
    """跑一支子腳本，輸出接進 daily.log，回傳離開碼。

    用 sys.executable 而非巢狀 uv run：本檔的 PEP 723 已宣告全部依賴，
    子腳本共用同一個直譯器，省下每步重新解析環境的時間。
    """
    log(mode, "$ " + " ".join(str(a) for a in args))
    with open(LOG, "a", encoding="utf-8") as fh:
        return subprocess.run([sys.executable, *[str(a) for a in args]],
                              stdout=fh, stderr=subprocess.STDOUT,
                              check=False).returncode


def ask_model(mode, prompt, model):
    """LLM 插槽：只給讀檔／寫檔／查新聞，**不給 Bash**——它無法自行送單或改紀錄。
    回傳 (離開碼, 輸出)，輸出同時進 daily.log。

    先收進記憶體再寫檔而非直接導向 log：額度耗盡與其他故障都是非零離開，
    要分辨只能看輸出內容。
    """
    log(mode, f"呼叫 claude（{model}）產生決策…")
    proc = subprocess.run(
        ["claude", "-p", prompt,
         "--model", model,
         "--allowedTools", "Read", "Write", "WebSearch", "WebFetch",
         "--permission-mode", "acceptEdits",
         "--output-format", "text"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=STATE, check=False)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(proc.stdout)
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------- 模式

def decide(date, backend):
    mode = "decide"
    if not is_trading_weekday(date):
        log(mode, "週末，不執行")
        return 0
    if not in_window(mode):
        return 0
    if run(mode, os.path.join(HERE, "packet.py")) != 0:
        notify(mode, "🛑 決策包產生失敗")
        return 1

    verdict = classify_risk_exit(run(mode, os.path.join(HERE, "risk.py")))
    if verdict == SKIP:
        notify(mode, "今日不交易：執行邊界全面阻擋（詳見 daily.log）")
        return 0
    if verdict == FAIL:
        notify(mode, "🛑 風控邊界產生失敗")
        return 1

    packet = os.path.join(STATE, "packets", f"{date}.json")
    envelope = os.path.join(STATE, "envelopes", f"{date}.json")
    decision = os.path.join(STATE, "decisions", f"{date}.json")
    if not (os.path.exists(packet) and os.path.exists(envelope)):
        notify(mode, "🛑 決策包或執行邊界不存在")
        return 1
    os.makedirs(os.path.dirname(decision), exist_ok=True)

    with open(os.path.join(HERE, "prompts", "decide.md"), encoding="utf-8") as fh:
        prompt = render_prompt(fh.read(), packet, envelope, decision, date)

    rc, out = ask_model(mode, prompt, DECISION_MODEL)
    successor = fallback_model(rc, out, DECISION_MODEL, FALLBACK_MODEL)
    if successor:
        notify(mode, f"⚠️ {DECISION_MODEL} 額度耗盡，改用 {successor} 重試")
        rc, out = ask_model(mode, prompt, successor)
    if rc != 0:
        notify(mode, "🛑 決策產生失敗（claude 非零離開）")
        return 1
    if not os.path.exists(decision):
        notify(mode, f"🛑 決策檔未產生：{decision}")
        return 1

    if run(mode, os.path.join(HERE, "journal.py"), "record", decision) != 0:
        notify(mode, "🛑 決策違反 ARK 紀律，已拒絕寫入（未送出任何委託）")
        return 1
    if run(mode, os.path.join(HERE, "execute.py"), "--backend", backend) != 0:
        notify(mode, f"🛑 送單未全部成功（{backend}）——請查 daily.log 與 journal")
        return 1

    notify(mode, f"✅ {date} 決策完成並送出（{backend}）")
    return 0


def settle(date):
    mode = "settle"
    if not is_trading_weekday(date):
        log(mode, "週末，不執行")
        return 0
    if not in_window(mode):
        return 0
    # 兩個寫 ARK App 的步驟排在最後且相鄰：AX 操作最脆弱，先把純資料的記錄
    # 做完，App 出問題也不會連累成交對回與淨值——那兩者是熔斷基準的來源。
    results = [
        ("成交對回", run(mode, os.path.join(HERE, "journal.py"), "settle")),
        ("淨值記錄", run(mode, os.path.join(HERE, "equity.py"))),
        # 除權息預告表只含尚未發生的事件，除息日一過就從表上消失——必須每天
        # 抓、累積存檔，等前瞻報酬到期時資料才在。放在寫 App 之前：純網路取數，
        # 失敗也不影響後面兩步。
        ("除權息資料", run(mode, os.path.join(HERE, "dividends.py"))),
        ("庫存/現金同步", run(mode, SYNC, *SYNC_ARGS)),
        ("實現收益記錄", run(mode, os.path.join(HERE, "record_return.py"))),
    ]
    failed = settle_failures(results)
    if failed:
        notify(mode, f"⚠️ {date} 結算部分失敗：{'、'.join(failed)}")
        return 1
    notify(mode, f"✅ {date} 盤後結算完成")
    return 0


def check(backend):
    mode = "check"
    bad = []
    if subprocess.run(["pgrep", "-x", "Arkeration"],
                      capture_output=True, check=False).returncode != 0:
        bad.append("方舟運算 App 未執行")
    if subprocess.run(["which", "claude"],
                      capture_output=True, check=False).returncode != 0:
        bad.append("找不到 claude")
    if run(mode, os.path.join(HERE, "preflight.py")) != 0:
        bad.append("preflight 未過（詳見 daily.log）")
    if bad:
        notify(mode, f"🛑 前置檢查未過：{'、'.join(bad)}")
        return 1
    notify(mode, f"✅ 前置檢查全過（backend={backend}）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="ark-agent 每日排程編排")
    ap.add_argument("mode", choices=("decide", "settle", "check"))
    ap.add_argument("--date", help="覆寫日期（預設今天）")
    args = ap.parse_args()

    date = args.date or dt.date.today().isoformat()
    backend = os.environ.get("ARK_BACKEND", "simulation")
    if args.mode == "decide":
        return decide(date, backend)
    if args.mode == "settle":
        return settle(date)
    return check(backend)


if __name__ == "__main__":
    sys.exit(main())
