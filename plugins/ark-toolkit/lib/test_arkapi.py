"""arkapi 的純邏輯測試（不連網、不需要 ARK 執行中，任何平台可跑）

執行：cd plugin/plugins/ark-toolkit/lib && uv run --no-project --python 3.13 python -m unittest test_arkapi -v
"""
import base64
import datetime as dt
import json
import unittest

import arkapi


def jwt(exp):
    """組一個只有 exp／jti 的假 JWT（與 ARK 實際簽發的形狀相同，簽章不驗）"""
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'HS512'})}.{seg({'exp': int(exp.timestamp()), 'jti': 'x'})}.sig"


NOW = dt.datetime(2026, 9, 2, 20, 0)


class TestTokenExpiry(unittest.TestCase):
    """token 會過期（實測壽命從數天到數週）。過期就必須停手——拿過期的 token 去打
    只會拿到 401，而呼叫端多半會把它誤讀成「這個欄位沒有資料」。"""

    def test_讀得出到期時間(self):
        exp = NOW + dt.timedelta(days=1)
        self.assertEqual(arkapi.token_expiry(jwt(exp)), exp.replace(microsecond=0))

    def test_過期的_token_視為無效(self):
        self.assertFalse(arkapi.token_valid(jwt(NOW - dt.timedelta(minutes=1)), now=NOW))

    def test_快到期的也視為無效_留安全邊際(self):
        # 剩不到 60 秒就別用了：一輪決策包要跑好幾分鐘，中途過期比一開始就失敗更難查
        self.assertFalse(arkapi.token_valid(jwt(NOW + dt.timedelta(seconds=30)), now=NOW))

    def test_還有效的可以用(self):
        self.assertTrue(arkapi.token_valid(jwt(NOW + dt.timedelta(hours=2)), now=NOW))

    def test_形狀不對的_token_不當成有效(self):
        self.assertFalse(arkapi.token_valid("not-a-jwt", now=NOW))


class TestRows(unittest.TestCase):
    """ARK 的回應一律是 {code, message, title, data}：title 是欄位名、data 是列。"""

    def test_title_與_data_併成_dict(self):
        payload = {"code": "0000", "title": ["股票代號", "總股數"],
                   "data": [["0050", "53"], ["0056", "106"]]}
        self.assertEqual(arkapi.rows(payload),
                         [{"股票代號": "0050", "總股數": "53"},
                          {"股票代號": "0056", "總股數": "106"}])

    def test_錯誤碼直接拋_不回半套資料(self):
        with self.assertRaises(arkapi.ArkApiError):
            arkapi.rows({"code": "9999", "message": "登入逾時"})

    def test_少欄位的列以_None_補齊_而不是靜默丟掉(self):
        payload = {"code": "0000", "title": ["a", "b", "c"], "data": [["1", "2"]]}
        self.assertEqual(arkapi.rows(payload), [{"a": "1", "b": "2", "c": None}])


class TestParsers(unittest.TestCase):
    def test_五指標與持股水位建議(self):
        payload = {"code": "0000",
                   "title": ["全球散戶", "外資情緒", "位階增溫", "社交反指", "量能暴衝", "持股水位建議"],
                   "data": [["45", "26.27000", "80.4036", "87.5288", "62.0681", "69.9"]]}
        got = arkapi.parse_indicators(payload)
        self.assertEqual(got["持股水位建議"], 69.9)
        self.assertEqual(got["全球散戶"], 45.0)

    def test_庫存只取台股_股數與均價轉數值(self):
        payload = {"code": "0000",
                   "title": ["股票代號", "種類", "總股數", "成交均價", "排序", "建立時間"],
                   "data": {"usStock": [], "twStock": [["0056", "1", "106.0", "53.6", "0", "t"],
                                                      ["0050", "1", "53.0", "106.28", "1", "t"]]}}
        self.assertEqual(arkapi.parse_positions(payload),
                         {"0056": (106, 53.6), "0050": (53, 106.28)})

    def test_現金台幣美元(self):
        payload = {"code": "0000", "title": ["台幣金額", "美元金額"], "data": ["75443", "0"]}
        self.assertEqual(arkapi.parse_cash(payload), (75443.0, 0.0))


class TestReadOnlyGuard(unittest.TestCase):
    """這個模組是唯讀的。寫入端點存在且格式已知，但**不從這裡走**——
    要走得經過使用者同意、另外設計驗證（AX 寫入有 App 自算總成本當獨立見證，
    API 只回一句「操作成功」）。白名單讓「不小心打到寫入端點」變成不可能。"""

    def test_白名單外的路徑一律拒絕(self):
        sess = arkapi.Session("t", "m", "d", NOW + dt.timedelta(hours=1))
        for path in ("api/myStock/update/1/0050/1/10/100", "api/calculateRisk/memberMoney/update",
                     "api/dailyReward/upsert", "api/watchlistUpdate"):
            with self.assertRaises(ValueError, msg=path):
                arkapi.call(sess, path, fetch=lambda *a, **k: {})

    def test_白名單內的路徑放行(self):
        token = jwt(NOW + dt.timedelta(hours=1))
        sess = arkapi.Session(token, "m", "d", NOW + dt.timedelta(hours=1))
        seen = {}

        def fetch(url, headers, body):
            seen["url"], seen["headers"] = url, headers
            return {"code": "0000", "title": ["a"], "data": [["1"]]}

        arkapi.call(sess, "api/calculateRisk", fetch=fetch, now=NOW)
        self.assertIn("api/calculateRisk?", seen["url"])
        self.assertEqual(seen["headers"]["Authorization"], f"Bearer {token}")

    def test_帶_memberId_的庫存路徑也在白名單內(self):
        token = jwt(NOW + dt.timedelta(hours=1))
        sess = arkapi.Session(token, "20260313220729978000", "d", NOW + dt.timedelta(hours=1))
        arkapi.call(sess, f"api/myStock/{sess.member_id}", body={},
                    fetch=lambda *a, **k: {"code": "0000"}, now=NOW)

    def test_token_過期就不送出請求(self):
        sess = arkapi.Session("t", "m", "d", NOW - dt.timedelta(hours=1))

        def fetch(*a, **k):
            raise AssertionError("過期還送出請求了")

        with self.assertRaises(arkapi.ArkApiError):
            arkapi.call(sess, "api/calculateRisk", fetch=fetch, now=NOW)


if __name__ == "__main__":
    unittest.main()
