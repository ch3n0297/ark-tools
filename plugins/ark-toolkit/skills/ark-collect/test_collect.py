"""ark-collect 純邏輯測試（不開視窗、不連券商，任何平台可跑）"""
import unittest

import collect

FEES = {"include_dividends": False, "include_fees": True}
RAW = {"include_dividends": False, "include_fees": False}
SHIOAJI = {"type": "shioaji", "name": "永豐"}
FILE = {"type": "file", "name": "國泰", "path": "p.csv", "columns": {}}


class TestReadAccounts(unittest.TestCase):
    """1.3.0 的回報：collect 讀永豐時沒帶 config 的均價口徑，快照永遠是券商原值。"""

    def setUp(self):
        self.calls = []

    def read_shioaji(self, account, basis):
        self.calls.append((account["name"], basis))
        return {"2330": (18, 2285.78)}

    def read_file(self, account):
        return {"0050": (12, 104.67)}, account

    def test_Shioaji_帳戶帶_config_的口徑(self):
        cfg = {"version": 2, "accounts": [SHIOAJI], "cost_basis": FEES}
        by_account, accounts = collect.read_accounts(cfg, self.read_shioaji, self.read_file)
        self.assertEqual(self.calls, [("永豐", FEES)])
        self.assertEqual(by_account, {"永豐": {"2330": (18, 2285.78)}})
        self.assertEqual(accounts, [SHIOAJI])

    def test_config_沒有口徑時帶券商原值(self):
        cfg = {"version": 2, "accounts": [SHIOAJI]}
        collect.read_accounts(cfg, self.read_shioaji, self.read_file)
        self.assertEqual(self.calls, [("永豐", RAW)])

    def test_混合帳戶各走各的讀法(self):
        cfg = {"version": 2, "accounts": [SHIOAJI, FILE], "cost_basis": FEES}
        by_account, accounts = collect.read_accounts(cfg, self.read_shioaji, self.read_file)
        self.assertEqual(set(by_account), {"永豐", "國泰"})
        self.assertEqual(accounts, [SHIOAJI, FILE])

    def test_檔案帳戶取消時整體取消(self):
        cfg = {"version": 2, "accounts": [FILE]}
        self.assertEqual(collect.read_accounts(cfg, self.read_shioaji, lambda a: (None, None)),
                         (None, None))

    def test_Shioaji_讀取失敗指名帳戶(self):
        def boom(account, basis):
            raise ConnectionError("login failed")
        cfg = {"version": 2, "accounts": [SHIOAJI]}
        with self.assertRaisesRegex(RuntimeError, "永豐.*ConnectionError"):
            collect.read_accounts(cfg, boom, self.read_file)


if __name__ == "__main__":
    unittest.main()
