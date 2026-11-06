"""接口行为：终端、符号检索、SSE 续传、模型检测。"""

from __future__ import annotations

import sys
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServerCase  # noqa: E402

from eagent_bridge.webui import RunState  # noqa: E402


class TerminalTests(ServerCase, unittest.TestCase):
    def test_echo(self):
        status, data = self.api("POST", "/api/terminal/exec", {"command": "Write-Output hi-eagent"})
        self.assertEqual(status, 200)
        self.assertEqual(data["exit_code"], 0)
        self.assertIn("hi-eagent", data["output"])

    def test_cd_changes_cwd(self):
        status, data = self.api("POST", "/api/terminal/exec", {"command": "cd ."})
        self.assertEqual(status, 200)
        self.assertTrue(data["cwd"].endswith("workspace"))


class SymbolTests(ServerCase, unittest.TestCase):
    def test_symbols_and_definition(self):
        status, data = self.api("GET", "/api/symbols?q=hello")
        self.assertEqual(status, 200)
        self.assertIn("hello", [item["name"] for item in data["symbols"]])

        status, data = self.api("GET", "/api/definition?symbol=hello")
        self.assertEqual(status, 200)
        self.assertEqual(data["definitions"][0]["name"], "hello")

    def test_references_find_usage(self):
        status, data = self.api("GET", "/api/references?symbol=hello")
        self.assertEqual(status, 200)
        self.assertTrue(any("hello" in item["text"] for item in data["references"]))


class StreamTests(ServerCase, unittest.TestCase):
    def test_unknown_run_returns_404(self):
        status, _ = self.api("GET", "/api/agent/events?run=nope")
        self.assertEqual(status, 404)

    def test_finished_run_only_replays_final_event(self):
        run = RunState()
        run.emit({"type": "agent", "kind": "assistant", "text": "hello"})
        run.emit({"type": "run_done", "summary": "ok"})
        run.finish()
        self.state.runs[run.id] = run

        url = self.base + "/api/agent/events?run=" + run.id + "&token=" + self.token
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(body.count("data:"), 1)
        self.assertIn("run_done", body)
        self.assertNotIn("hello", body)

        with urllib.request.urlopen(url + "&since=2", timeout=10) as response:
            self.assertEqual(response.read(), b"")


class ModelCheckTests(ServerCase, unittest.TestCase):
    def test_unknown_provider(self):
        status, data = self.api("POST", "/api/model/check", {"model": {"provider": "nope"}})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])


class QueueTests(ServerCase, unittest.TestCase):
    def test_reports_active_event_count(self):
        run = RunState()
        run.emit({"type": "agent", "kind": "assistant", "text": "x"})
        self.state.runs[run.id] = run
        self.state.current = run.id

        status, data = self.api("POST", "/api/agent/queue", {})

        self.assertEqual(status, 200)
        self.assertEqual(data["active"]["id"], run.id)
        self.assertEqual(data["active"]["events"], 1)


if __name__ == "__main__":
    unittest.main()
