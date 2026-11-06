"""MCP 模板：列表、安装与冲突处理。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.mcp_templates import install, list_templates  # noqa: E402


class McpTemplateTests(unittest.TestCase):
    def test_list_templates(self):
        with tempfile.TemporaryDirectory() as folder:
            templates = list_templates(Path(folder))
            self.assertGreaterEqual(len(templates), 5)
            ids = [item["id"] for item in templates]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(any(item["id"] == "windows-mcp" for item in templates))

    def test_install_preserves_existing_and_replaces_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            config = workspace / ".eagent" / "mcp.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"mcpServers": {"existing": {"command": "x"}}}),
                encoding="utf-8",
            )

            result = install(workspace, "filesystem")

            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(result["servers"], 2)
            self.assertIn("existing", data["mcpServers"])
            args = data["mcpServers"]["filesystem"]["args"]
            self.assertIn(str(workspace.resolve()), args)

    def test_duplicate_install_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            install(workspace, "time")
            with self.assertRaises(ValueError):
                install(workspace, "time")

    def test_unknown_template_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                install(Path(folder), "nope")

    def test_invalid_existing_config_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            config = workspace / ".eagent" / "mcp.json"
            config.parent.mkdir(parents=True)
            config.write_text("not json", encoding="utf-8")

            with self.assertRaises(ValueError):
                install(workspace, "time")

            self.assertEqual(config.read_text(encoding="utf-8"), "not json")

    def test_invalid_mcp_servers_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            config = workspace / ".eagent" / "mcp.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"mcpServers": []}', encoding="utf-8")

            with self.assertRaises(ValueError):
                install(workspace, "time")

            self.assertEqual(config.read_text(encoding="utf-8"), '{"mcpServers": []}')


if __name__ == "__main__":
    unittest.main()
