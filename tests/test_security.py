"""安全边界：鉴权、Host、路径穿越、请求体上限、快照回滚。"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServerCase  # noqa: E402

from eagent_bridge import index  # noqa: E402
from eagent_bridge.snapshots import restore  # noqa: E402
from eagent_bridge.tools import ToolError, safe_path  # noqa: E402
from eagent_bridge.http_api import MAX_BODY_BYTES  # noqa: E402


class AuthTests(ServerCase, unittest.TestCase):
    def test_health_is_public(self):
        status, data = self.api("GET", "/api/health", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_other_endpoints_require_token(self):
        status, _ = self.api("GET", "/api/workspace", token=False)
        self.assertEqual(status, 401)

    def test_bad_host_is_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request(
                "GET",
                "/api/workspace?token=" + self.token,
                headers={"Host": "evil.example.com"},
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            conn.close()


class PathTests(ServerCase, unittest.TestCase):
    def test_file_api_blocks_traversal(self):
        status, data = self.api("GET", "/api/file?path=../secret.txt")
        self.assertEqual(status, 400)
        self.assertIn("越界", data["error"])

    def test_safe_path_rejects_escape(self):
        with self.assertRaises(ToolError):
            safe_path(self.workspace, "../outside.txt")

    def test_snapshot_restore_ignores_traversal(self):
        snapshot_dir = self.workspace / ".eagent" / "backups" / "run1"
        snapshot_dir.mkdir(parents=True)
        manifest = {
            "id": "run1",
            "task": "x",
            "created": 0,
            "finished": 0,
            "summary": "",
            "files": [{"path": "../evil.txt", "existed": True}],
        }
        (snapshot_dir / "snapshot.json").write_text(json.dumps(manifest), encoding="utf-8")

        outside = (self.workspace.parent / "evil.txt").resolve()
        result = restore(self.workspace, "run1")
        self.assertIn("../evil.txt", result["missing"])
        self.assertFalse(outside.exists())

    def test_symbol_source_rejects_escape(self):
        self.assertEqual(index.symbol_source(self.workspace, "../outside.py", 1), "")

    def test_body_limit_is_enforced(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.putrequest("POST", "/api/agent/queue?token=" + self.token)
            conn.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            conn.close()


class StaticAssetTests(ServerCase, unittest.TestCase):
    def test_index_references_split_assets(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request("GET", "/?token=" + self.token)
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"/ui/styles.css", body)
            self.assertIn(b"/ui/js/core.js", body)
            # Monaco 必须走本地资源，不能再依赖 CDN
            self.assertIn(b"/ui/vendor/monaco/vs/loader.js", body)
            self.assertNotIn(b"registry.npmmirror.com", body)
            self.assertNotIn(b"unpkg.com", body)
        finally:
            conn.close()

    def test_css_and_js_are_served_without_token(self):
        for path, expected in (
            ("/ui/styles.css", "text/css"),
            ("/ui/js/core.js", "text/javascript"),
            ("/ui/vendor/monaco/vs/loader.js", "text/javascript"),
            ("/ui/vendor/monaco/vs/base/browser/ui/codicons/codicon/codicon.ttf", "font/ttf"),
        ):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.server.server_address[1], timeout=10
            )
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 200, path)
                self.assertIn(expected, response.getheader("Content-Type") or "")
            finally:
                conn.close()

    def test_asset_traversal_is_blocked(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request("GET", "/ui/%2e%2e/webui.py")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 404)
        finally:
            conn.close()


class OpenAIServerTests(unittest.TestCase):
    def test_cors_disabled_without_api_key(self):
        import os

        from eagent_bridge import server as openai_server

        previous = os.environ.pop("EAGENT_BRIDGE_API_KEY", None)
        server = openai_server.create_server(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10
            )
            conn.request("OPTIONS", "/v1/chat/completions")
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, 204)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        finally:
            server.shutdown()
            server.server_close()
            if previous is not None:
                os.environ["EAGENT_BRIDGE_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
