"""ARK 伺服器的**唯讀**查詢層。

為什麼要有這一層：有些數字 UI 根本讀不到。運算頁的五指標與「持股水位建議」是
純圖形儀表板，AX tree 裡沒有任何文字；而伺服器的 `api/calculateRisk` 直接回數值。
另外庫存可以在這裡再讀一次，和 AX 解析出來的對帳——parser 被 App 改版弄壞時，
兩邊不一致會當場現形，而不是靜默讀成 0 檔。

**這個模組不寫入，也不該被改成會寫入。** 寫入端點存在、格式也已知，但走 API 寫入
是另一個決定：AX 寫入有 App 自算的總成本當獨立見證，API 只回一句「操作成功」，
而且繞過 UI 寫入後 App 端的建議是否跟著刷新並未驗證。`call()` 的白名單讓「不小心
打到寫入端點」變成不可能，而不只是「我們沒有呼叫它」。

**認證**：重用 App 自己拿到的 Bearer token（從它的 URL 快取裡讀），不自行向
`api/auth` 換發。App 沒登入、或 token 過期，這裡就明確失敗——那代表該去把 App
打開，而不是繞過它。token 只在記憶體裡流動，不寫檔、不記錄。
"""
import base64
import datetime as dt
import glob
import json
import os
import plistlib
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple, Optional

BASE = "https://ark-api.my-galaxy.com.tw/dashaWulinApi/"
PREFS_GLOB = "~/Library/Containers/*/Data/Library/Preferences/com.galaxy.ark.plist"
CACHE_REL = "Data/Library/Caches/com.galaxy.ark/Cache.db"

# 只有這些路徑送得出去。全部是查詢；任何會改變伺服器狀態的端點都不在此列。
READ_ONLY = {
    "api/calculateRisk",                 # 五指標 + 持股水位建議（AX 讀不到的儀表板）
    "api/calculateRisk/memberMoney",     # 台幣／美元現金
    "api/myStock/{member_id}",           # 庫存
    "api/st/ids",                        # 策略分區清單
}
EXPIRY_MARGIN = dt.timedelta(seconds=60)   # 剩這麼點時間就別開始了


class ArkApiError(RuntimeError):
    pass


class Session(NamedTuple):
    token: str
    member_id: str
    device_token: str
    expires: dt.datetime


# --- token ------------------------------------------------------------------

def token_expiry(token) -> Optional[dt.datetime]:
    """JWT 的 exp（本地時間）；形狀不對回 None。簽章不驗——我們不是發證方，
    只是要知道手上這張還能不能用。"""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return dt.datetime.fromtimestamp(claims["exp"])
    except Exception:                    # noqa: BLE001 - 任何解析失敗都只代表「不能用」
        return None


def token_valid(token, now=None) -> bool:
    exp = token_expiry(token)
    return exp is not None and exp - EXPIRY_MARGIN > (now or dt.datetime.now())


# --- 從 App 的本機檔案組出 session -------------------------------------------

def container():
    """ARK 的容器目錄。目錄名是每台機器不同的 UUID，只能由偏好設定檔反查——
    寫死 UUID 的話換一台機器就整個模組失效。"""
    hits = glob.glob(os.path.expanduser(PREFS_GLOB))
    if not hits:
        raise ArkApiError("找不到方舟運算的容器（App 可能沒安裝或從未開啟過）")
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(hits[0]))))


def _cached_credentials(cache_db):
    """從 App 的 URL 快取撈 (最新且未過期的 token, member_id)。

    先複製再讀：App 隨時在寫這個 SQLite，直接開它可能讀到半截交易，
    也可能干擾 App。WAL 與 SHM 要一起複製，否則看不到最近的寫入。
    """
    tmp = tempfile.mkdtemp(prefix="arkapi-")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = cache_db + suffix
            if os.path.exists(src):
                shutil.copy(src, os.path.join(tmp, "Cache.db" + suffix))
        db = sqlite3.connect(os.path.join(tmp, "Cache.db"))
        rows = db.execute(
            "select r.request_key, b.request_object "
            "from cfurl_cache_response r "
            "join cfurl_cache_blob_data b on r.entry_ID = b.entry_ID "
            "where r.request_key like '%dashaWulinApi%' "
            "order by r.time_stamp desc limit 200").fetchall()
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    member_id, best = None, None
    for key, blob in rows:
        if member_id is None and (m := re.search(r"/(\d{18,22})", key)):
            member_id = m.group(1)
        try:
            headers = next(x for x in plistlib.loads(blob)["Array"]
                           if isinstance(x, dict) and "Authorization" in x)
        except Exception:                         # noqa: BLE001 - 壞掉的那筆跳過就好
            continue
        token = headers["Authorization"].split(" ", 1)[-1]
        exp = token_expiry(token)
        if exp and (best is None or exp > best[1]):
            best = (token, exp)
    return best, member_id


def session(now=None) -> Session:
    """重用 App 自己的登入。App 沒登入過、或最新的 token 已過期，就明確失敗。"""
    root = container()
    prefs_path = os.path.join(root, "Data/Library/Preferences/com.galaxy.ark.plist")
    with open(prefs_path, "rb") as f:
        prefs = plistlib.load(f)
    device_token = prefs.get("deviceToken")
    if not device_token:
        raise ArkApiError("方舟運算的偏好設定裡沒有 deviceToken，App 可能還沒登入過")

    cache_db = os.path.join(root, CACHE_REL)
    if not os.path.exists(cache_db):
        raise ArkApiError("找不到方舟運算的 URL 快取，無法取得登入憑證")
    best, member_id = _cached_credentials(cache_db)
    if best is None or member_id is None:
        raise ArkApiError("快取裡沒有可用的登入憑證，請先開啟方舟運算並登入")
    token, exp = best
    if not token_valid(token, now=now):
        raise ArkApiError(f"快取裡的登入憑證已於 {exp:%Y-%m-%d %H:%M} 過期，"
                          "請開啟方舟運算讓它重新登入")
    return Session(token=token, member_id=member_id,
                   device_token=device_token, expires=exp)


# --- 呼叫 -------------------------------------------------------------------

def _fetch(url, headers, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ArkApiError(f"{url.rsplit('/', 1)[-1]} 回應 HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ArkApiError(f"連不上 ARK 伺服器：{e.reason}") from e


def call(sess, path, body=None, query=None, fetch=_fetch, now=None):
    """送出一次**查詢**。path 必須在 READ_ONLY 白名單裡，否則拒絕。"""
    template = re.sub(r"/\d{18,22}(?=/|$)", "/{member_id}", path)
    if template not in READ_ONLY:
        raise ValueError(f"arkapi 是唯讀的，不送 {path}（白名單：{sorted(READ_ONLY)}）")
    if not token_valid(sess.token, now=now):
        raise ArkApiError(f"登入憑證已於 {sess.expires:%Y-%m-%d %H:%M} 過期，"
                          "請開啟方舟運算讓它重新登入")
    params = {"deviceType": "I", "deviceToken": sess.device_token, **(query or {})}
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {sess.token}", "Accept": "*/*"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return fetch(url, headers, body)


def rows(payload):
    """{code, message, title, data} → [{欄位名: 值}]。列比 title 短就補 None，
    不靜默丟掉——欄位變動要看得出來，這是 parser 壞掉時的第一個線索。"""
    if payload.get("code") != "0000":
        raise ArkApiError(f"ARK 回應 code={payload.get('code')}：{payload.get('message')}")
    title = payload.get("title") or []
    return [{name: row[i] if i < len(row) else None for i, name in enumerate(title)}
            for row in payload.get("data") or []]


def _num(v):
    return float(str(v).replace(",", "")) if v not in (None, "") else 0.0


def parse_indicators(payload):
    """五指標＋持股水位建議 → {名稱: 數值}。這些在 AX tree 裡完全讀不到。"""
    got = rows(payload)
    return {k: _num(v) for k, v in got[0].items()} if got else {}


def parse_positions(payload):
    """庫存 → {代號: (股數, 成交均價)}。只取台股；美股另有帳戶模型，這裡不混。"""
    if payload.get("code") != "0000":
        raise ArkApiError(f"ARK 回應 code={payload.get('code')}：{payload.get('message')}")
    title = payload.get("title") or []
    tw = (payload.get("data") or {}).get("twStock") or []
    idx = {name: i for i, name in enumerate(title)}
    return {row[idx["股票代號"]]: (int(_num(row[idx["總股數"]])), _num(row[idx["成交均價"]]))
            for row in tw}


def parse_cash(payload):
    """現金 → (台幣, 美元)。這個端點的 data 是單層陣列，不是列的陣列。"""
    if payload.get("code") != "0000":
        raise ArkApiError(f"ARK 回應 code={payload.get('code')}：{payload.get('message')}")
    data = payload.get("data") or []
    return (_num(data[0] if len(data) > 0 else 0), _num(data[1] if len(data) > 1 else 0))


# --- 對外的查詢 --------------------------------------------------------------

def indicators(sess, **kw):
    return parse_indicators(call(sess, "api/calculateRisk",
                                 body={"deviceType": "I", "deviceToken": sess.device_token}, **kw))


def cash(sess, **kw):
    return parse_cash(call(sess, "api/calculateRisk/memberMoney",
                           query={"memberId": sess.member_id}, **kw))


def positions(sess, **kw):
    return parse_positions(call(sess, f"api/myStock/{sess.member_id}", body={}, **kw))
