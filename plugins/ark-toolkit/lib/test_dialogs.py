"""dialogs 的純邏輯測試（視窗本身不測；ask／save 以注入取代）"""
import unittest

import dialogs

SHIOAJI_CFG = {"version": 2, "accounts": [{"type": "shioaji", "name": "永豐"}]}
FILE_CFG = {"version": 2, "accounts": [
    {"type": "file", "name": "國泰", "path": "p.csv", "columns": {}}]}
FEES = {"include_dividends": False, "include_fees": True}


class TestEnsureCostBasis(unittest.TestCase):
    """第一次 sync／collect 問一次口徑、存進 config 後永久沿用；排程（--no-prompt）不問。"""

    def setUp(self):
        self.asked = 0
        self.saved = []

    def ask(self, choice):
        def _ask():
            self.asked += 1
            return choice
        return _ask

    def save(self, cfg):
        self.saved.append(cfg)
        return "config.json"

    def test_已有口徑就不問(self):
        cfg = {**SHIOAJI_CFG, "cost_basis": FEES}
        got = dialogs.ensure_cost_basis(cfg, prompt=True, ask=self.ask(FEES), save=self.save)
        self.assertEqual(got, cfg)
        self.assertEqual((self.asked, self.saved), (0, []))

    def test_沒有_Shioaji_帳戶不問(self):
        got = dialogs.ensure_cost_basis(FILE_CFG, prompt=True, ask=self.ask(FEES), save=self.save)
        self.assertEqual(got, FILE_CFG)
        self.assertEqual(self.asked, 0)

    def test_no_prompt_時不問也不存_沿用券商原值(self):
        got = dialogs.ensure_cost_basis(SHIOAJI_CFG, prompt=False, ask=self.ask(FEES), save=self.save)
        self.assertEqual(got, SHIOAJI_CFG)
        self.assertEqual((self.asked, self.saved), (0, []))

    def test_第一次問到答案就寫進_config(self):
        got = dialogs.ensure_cost_basis(SHIOAJI_CFG, prompt=True, ask=self.ask(FEES), save=self.save)
        self.assertEqual(got["cost_basis"], FEES)
        self.assertEqual(got["accounts"], SHIOAJI_CFG["accounts"])
        self.assertEqual(self.saved, [got])

    def test_視窗按取消_這次用原值_不存_下次再問(self):
        got = dialogs.ensure_cost_basis(SHIOAJI_CFG, prompt=True, ask=self.ask(None), save=self.save)
        self.assertNotIn("cost_basis", got)
        self.assertEqual(self.saved, [])


if __name__ == "__main__":
    unittest.main()
