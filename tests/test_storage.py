"""运行数据清理。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.storage import ensure_data_dir, prune  # noqa: E402


class DataDirTests(unittest.TestCase):
    def test_creates_gitignore_for_runtime_data(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            data = ensure_data_dir(workspace)

            self.assertTrue(data.is_dir())
            self.assertEqual(
                (data / ".gitignore").read_text(encoding="utf-8").strip(), "*"
            )

    def test_does_not_overwrite_existing_gitignore(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            data = workspace / ".eagent"
            data.mkdir()
            (data / ".gitignore").write_text("custom\n", encoding="utf-8")

            ensure_data_dir(workspace)

            self.assertEqual(
                (data / ".gitignore").read_text(encoding="utf-8"), "custom\n"
            )


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
