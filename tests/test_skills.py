"""技能：ID 校验与正常读写。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.skills import load_skill, save_skill  # noqa: E402


class SkillIdTests(unittest.TestCase):
    def test_save_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "work"
            (workspace / ".eagent" / "skills").mkdir(parents=True)
            outside = workspace / "outside.md"
            outside.write_text("ORIGINAL", encoding="utf-8")

            with self.assertRaises(ValueError):
                save_skill(workspace, "名字", "", "HACKED", "../../outside")

            self.assertEqual(outside.read_text(encoding="utf-8"), "ORIGINAL")

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "work"
            workspace.mkdir()
            skill_id = save_skill(workspace, "测试技能", "说明", "第一条\n第二条")
            self.assertTrue(skill_id)
            name, body = load_skill(workspace, skill_id)
            self.assertEqual(name, "测试技能")
            self.assertIn("第一条", body)


if __name__ == "__main__":
    unittest.main()
