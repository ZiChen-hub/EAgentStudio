"""快照：超大文件跳过、非法目录容错、Cookie 域名过滤。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.session import _domain_matches  # noqa: E402
from eagent_bridge.snapshots import (  # noqa: E402
    MAX_FILE_BYTES,
    backup,
    begin,
    list_snapshots,
)


class SnapshotTests(unittest.TestCase):
    def test_backup_skips_large_file(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "work"
            workspace.mkdir()
            big = workspace / "big.bin"
            with big.open("wb") as handle:
                handle.seek(MAX_FILE_BYTES + 10)
                handle.write(b"x")

            begin(workspace, "run1")
            backup(workspace, "run1", big)

            item = next(item for item in list_snapshots(workspace) if item["id"] == "run1")
            self.assertIn("big.bin", item["files"])
            self.assertFalse(
                (workspace / ".eagent" / "backups" / "run1" / "files" / "big.bin").exists()
            )

    def test_list_snapshots_skips_invalid_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "work"
            bad = workspace / ".eagent" / "backups" / "bad name"
            bad.mkdir(parents=True)
            (bad / "snapshot.json").write_text("{}", encoding="utf-8")
            self.assertEqual(list_snapshots(workspace), [])


class CookieDomainTests(unittest.TestCase):
    def test_matches_site_and_subdomain(self):
        self.assertTrue(_domain_matches(".deepseek.com", "chat.deepseek.com"))
        self.assertTrue(_domain_matches("deepseek.com", "deepseek.com"))
        self.assertTrue(_domain_matches("", "chat.deepseek.com"))

    def test_rejects_other_sites(self):
        self.assertFalse(_domain_matches(".example.com", "chat.deepseek.com"))


if __name__ == "__main__":
    unittest.main()
