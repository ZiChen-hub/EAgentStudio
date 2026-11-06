"""内置工具：审批、自动放行与唯一替换。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.tools import ToolError, execute  # noqa: E402


class ApprovalTests(unittest.TestCase):
    def test_write_is_rejected_without_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            target = workspace / "a.txt"
            with self.assertRaises(ToolError):
                execute(
                    "write_file",
                    {"path": "a.txt", "content": "x"},
                    workspace,
                    approver=lambda *args: False,
                )
            self.assertFalse(target.exists())

    def test_write_runs_after_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            execute(
                "write_file",
                {"path": "a.txt", "content": "hello"},
                workspace,
                approver=lambda *args: True,
            )
            self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "hello")

    def test_auto_allow_skips_approver(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            calls: list[str] = []

            def approver(name, args, ws):
                calls.append(name)
                return False

            execute(
                "write_file",
                {"path": "a.txt", "content": "auto"},
                workspace,
                approver=approver,
                auto_allow=True,
            )
            self.assertEqual(calls, [])
            self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "auto")


class ReplaceTests(unittest.TestCase):
    def test_requires_unique_fragment(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "a.txt").write_text("x x", encoding="utf-8")
            with self.assertRaises(ToolError):
                execute(
                    "replace_in_file",
                    {"path": "a.txt", "old": "x", "new": "y"},
                    workspace,
                    auto_allow=True,
                )

    def test_replaces_once(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "a.txt").write_text("alpha beta", encoding="utf-8")
            execute(
                "replace_in_file",
                {"path": "a.txt", "old": "beta", "new": "gamma"},
                workspace,
                auto_allow=True,
            )
            self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "alpha gamma")


if __name__ == "__main__":
    unittest.main()
