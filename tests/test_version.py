"""版本号在关键位置保持一致。"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_versions_match(self):
        version = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]

        init = (ROOT / "eagent_bridge" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__ = "([^"]+)"', init)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), version)

        installer = (ROOT / "packaging" / "eagent.iss").read_text(encoding="utf-8-sig")
        self.assertIn(f'#define AppVersion "{version}"', installer)

        index = (ROOT / "eagent_bridge" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"{version} · 本地运行", index)


if __name__ == "__main__":
    unittest.main()
