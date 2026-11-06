"""内置工具：审批、自动放行与唯一替换。"""

from __future__ import annotations

import http.server
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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


class ReadOnlyGuiTests(unittest.TestCase):
    def test_read_only_gui_skips_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            calls: list[str] = []

            def approver(name, args, ws):
                calls.append(name)
                return False

            with mock.patch("eagent_bridge.tools.execute_gui", return_value="ok"):
                self.assertEqual(
                    execute("screen_capture", {}, Path(folder), approver=approver), "ok"
                )
                self.assertEqual(
                    execute("window_list", {}, Path(folder), approver=approver), "ok"
                )

            self.assertEqual(calls, [])

    def test_mouse_click_still_requires_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("eagent_bridge.tools.execute_gui", return_value="ok"):
                with self.assertRaises(ToolError):
                    execute(
                        "mouse_click",
                        {"x": 1, "y": 1},
                        Path(folder),
                        approver=lambda *args: False,
                    )


class FileToolsTests(unittest.TestCase):
    def test_find_files(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "a.py").write_text("", encoding="utf-8")
            (workspace / "b.txt").write_text("", encoding="utf-8")
            (workspace / "sub").mkdir()
            (workspace / "sub" / "c.py").write_text("", encoding="utf-8")

            result = execute("find_files", {"pattern": "*.py"}, workspace)

            self.assertIn("a.py", result)
            self.assertIn("sub/c.py", result)
            self.assertNotIn("b.txt", result)

    def test_find_files_no_match(self):
        with tempfile.TemporaryDirectory() as folder:
            result = execute("find_files", {"pattern": "*.rs"}, Path(folder))
            self.assertIn("没有找到", result)

    def test_batch_copy_and_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "a.txt").write_text("a", encoding="utf-8")
            (workspace / "b.txt").write_text("b", encoding="utf-8")

            execute(
                "batch_files",
                {"action": "copy", "paths": ["a.txt", "b.txt"], "destination": "out"},
                workspace,
                auto_allow=True,
            )
            self.assertTrue((workspace / "out" / "a.txt").is_file())
            self.assertTrue((workspace / "out" / "b.txt").is_file())

            execute(
                "batch_files",
                {"action": "delete", "paths": ["out/a.txt"]},
                workspace,
                auto_allow=True,
            )
            self.assertFalse((workspace / "out" / "a.txt").exists())

    def test_batch_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ToolError):
                execute(
                    "batch_files",
                    {"action": "delete", "paths": ["../outside.txt"]},
                    Path(folder),
                    auto_allow=True,
                )

    def test_batch_requires_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "a.txt").write_text("a", encoding="utf-8")
            with self.assertRaises(ToolError):
                execute(
                    "batch_files",
                    {"action": "delete", "paths": ["a.txt"]},
                    workspace,
                    approver=lambda *args: False,
                )
            self.assertTrue((workspace / "a.txt").is_file())

    def test_batch_rejects_copy_into_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / "src").mkdir()
            (workspace / "src" / "a.txt").write_text("a", encoding="utf-8")
            with self.assertRaises(ToolError):
                execute(
                    "batch_files",
                    {"action": "copy", "paths": ["src"], "destination": "src/backup"},
                    workspace,
                    auto_allow=True,
                )

    def test_batch_rejects_eagent_files(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            internal = workspace / ".eagent"
            internal.mkdir()
            (internal / "mcp.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ToolError):
                execute(
                    "batch_files",
                    {"action": "delete", "paths": [".eagent/mcp.json"]},
                    workspace,
                    auto_allow=True,
                )


class _HttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-http"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpToolTests(unittest.TestCase):
    def test_get_and_save(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                workspace = Path(folder)
                url = f"http://127.0.0.1:{server.server_address[1]}/x"

                result = execute("http_request", {"url": url}, workspace, auto_allow=True)
                self.assertIn("HTTP 200", result)
                self.assertIn("hello-http", result)

                saved = execute(
                    "http_request",
                    {"url": url, "save_path": "download.txt"},
                    workspace,
                    auto_allow=True,
                )
                self.assertIn("已保存", saved)
                self.assertEqual(
                    (workspace / "download.txt").read_text(encoding="utf-8"),
                    "hello-http",
                )
        finally:
            server.shutdown()
            server.server_close()

    def test_rejects_non_http_scheme(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ToolError):
                execute(
                    "http_request",
                    {"url": "file:///c:/windows/win.ini"},
                    Path(folder),
                    auto_allow=True,
                )

    def test_requires_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ToolError):
                execute(
                    "http_request",
                    {"url": "http://127.0.0.1:1/"},
                    Path(folder),
                    approver=lambda *args: False,
                )


if __name__ == "__main__":
    unittest.main()
