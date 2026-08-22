"""ark-read 純邏輯測試（不連券商、不彈視窗，任何平台可跑）"""
import unittest

import read

FEES = {"include_dividends": False, "include_fees": True}
RAW = {"include_dividends": False, "include_fees": False}
SHIOAJI = {"type": "shioaji", "name": "永豐"}


class TestReadAll(unittest.TestCase):
    """對照 ARK 用的真實持倉要和 sync 寫進去的同一口徑，否則永遠顯示均價不符。"""

    def setUp(self):
        self.calls = []

    def fake_read(self, account, basis):
        self.calls.append((account["name"], basis))
        return {"2330": (18, 2285.78)}

    def test_Shioaji_帳戶帶_config_的口徑(self):
        cfg = {"version": 2, "accounts": [SHIOAJI], "cost_basis": FEES}
        by_account, _mtimes = read.read_all(cfg, self.fake_read)
        self.assertEqual(self.calls, [("永豐", FEES)])
        self.assertEqual(by_account, {"永豐": {"2330": (18, 2285.78)}})

    def test_config_沒有口徑時帶券商原值_不問(self):
        read.read_all({"version": 2, "accounts": [SHIOAJI]}, self.fake_read)
        self.assertEqual(self.calls, [("永豐", RAW)])


if __name__ == "__main__":
    unittest.main()
