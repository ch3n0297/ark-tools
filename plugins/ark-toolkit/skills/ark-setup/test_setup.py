"""ark-setup 純邏輯測試（視窗流程不在此；任何平台可跑）"""
import unittest

import setup

SHIOAJI_CFG = {"version": 2, "accounts": [{"type": "shioaji", "name": "永豐"}]}
FEES = {"include_dividends": False, "include_fees": True}


class TestApplyCostBasis(unittest.TestCase):
    def test_選了就寫進設定(self):
        self.assertEqual(setup.apply_cost_basis(SHIOAJI_CFG, FEES)["cost_basis"], FEES)

    def test_取消視窗時設定不變(self):
        cfg = {**SHIOAJI_CFG, "cost_basis": FEES}
        self.assertEqual(setup.apply_cost_basis(cfg, None), cfg)
        self.assertNotIn("cost_basis", setup.apply_cost_basis(SHIOAJI_CFG, None))


if __name__ == "__main__":
    unittest.main()
