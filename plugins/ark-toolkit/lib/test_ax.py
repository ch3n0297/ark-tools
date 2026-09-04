"""ax.find 的純邏輯測試（AX 樹以假物件注入；需要 pyobjc 才 import 得了 ax，僅 macOS 可跑）

執行：cd plugin/plugins/ark-toolkit && PYTHONPATH=lib uv run python -m unittest test_ax -v
"""
import unittest
from unittest import mock

import ax


class Node:
    """假 AX 元素：以身份雜湊，children 可含祖先以模擬環。"""
    def __init__(self, name):
        self.name = name
        self.children = []

    def __repr__(self):
        return f"Node({self.name})"


def fake_attr(el, name):
    if name == "AXChildren":
        return el.children
    if name == "AXDescription":
        return el.name
    return None


class TestFindTerminatesOnCycle(unittest.TestCase):
    """2026-09-02 14:43 settle 實錄：App 剛被喚醒時 AXChildren 出現環，
    find 遞迴 990 層後 RecursionError，整段 sync 掛掉且沒留下 sync-log。"""

    def test_child_points_back_to_ancestor(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        a.children = [b, c]
        b.children = [a]                    # 環：b → a
        with mock.patch.object(ax, "attr", fake_attr):
            found = ax.find(a, lambda e: True)
        self.assertEqual(found, [a, b, c])  # 每個元素只回一次，前序

    def test_self_cycle(self):
        a = Node("a")
        a.children = [a]
        with mock.patch.object(ax, "attr", fake_attr):
            self.assertEqual(ax.find(a, lambda e: True), [a])

    def test_by_desc_through_cycle(self):
        a, b = Node("自選"), Node("b")
        a.children, b.children = [b], [a]
        with mock.patch.object(ax, "attr", fake_attr):
            self.assertEqual(ax.by_desc(b, "自選"), [a])

    def test_plain_tree_unchanged(self):
        a, b, c, d = Node("a"), Node("b"), Node("c"), Node("d")
        a.children, b.children = [b, c], [d]
        with mock.patch.object(ax, "attr", fake_attr):
            self.assertEqual(ax.find(a, lambda e: e.name in ("c", "d")), [d, c])


if __name__ == "__main__":
    unittest.main()
