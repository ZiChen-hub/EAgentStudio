"""运行数据清理。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.storage import prune  # noqa: E402


class PruneTests(unittest.TestCase):
    def test_keeps_newest_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for number in range(5):
                path = base / f"f{number}.txt"
                path.write_text(str(number), encoding="utf-8")
                os.utime(path, (1000 + number, 1000 + number))

            removed = prune(base, keep=2, pattern="*.txt")

            self.assertEqual(removed, 3)
            self.assertEqual(
                sorted(item.name for item in base.glob("*.txt")),
                ["f3.txt", "f4.txt"],
            )

    def test_missing_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(prune(Path(folder) / "nope", keep=2), 0)


if __name__ == "__main__":
    unittest.main()
