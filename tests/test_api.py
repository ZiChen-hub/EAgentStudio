"""接口行为：终端、符号检索、SSE 续传、模型检测。"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServerCase  # noqa: E402

from eagent_bridge.webui import QueuedTask, RunState, _load_mcp_servers, _start_next  # noqa: E402


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


class McpTemplateTests(ServerCase, unittest.TestCase):
    def test_templates_and_install(self):
        status, data = self.api("GET", "/api/mcp/templates")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["templates"]), 5)

        status, data = self.api("POST", "/api/mcp/install", {"id": "time"})
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "time")

        status, _ = self.api("POST", "/api/mcp/install", {"id": "time"})
        self.assertEqual(status, 400)


class ModelGuardTests(ServerCase, unittest.TestCase):
    def test_review_skips_while_model_busy(self):
        from eagent_bridge.webui import _review_changes

        self.state.model_lock.acquire()
        try:
            self.assertEqual(_review_changes(self.state, "run123", None), [])
        finally:
            self.state.model_lock.release()

    def test_model_check_reports_busy(self):
        self.state.model_lock.acquire()
        try:
            status, data = self.api("POST", "/api/model/check", {"model": {"provider": "nope"}})
        finally:
            self.state.model_lock.release()
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("正在处理", data["message"])

    def test_new_session_rejected_while_running(self):
        run = RunState()
        self.state.runs[run.id] = run
        self.state.current = run.id

        status, data = self.api("POST", "/api/agent/new_session", {})

        self.assertEqual(status, 409)
        self.assertIn("任务运行中", data["error"])


class McpConfigTests(ServerCase, unittest.TestCase):
    def test_string_flags_are_not_treated_as_true(self):
        path = self.workspace / ".eagent" / "mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "demo": {
                            "command": "demo",
                            "autoApprove": "false",
                            "args": "bad",
                            "safeTools": "Snapshot",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        servers = _load_mcp_servers(self.state)

        self.assertEqual(len(servers), 1)
        self.assertFalse(servers[0]["autoApprove"])
        self.assertEqual(servers[0]["args"], [])
        self.assertEqual(servers[0]["safeTools"], [])


class DesktopBridgeTests(unittest.TestCase):
    def test_choose_folder_accepts_string_and_list(self):
        from eagent_bridge.desktop import Bridge

        class Window:
            def __init__(self, result):
                self.result = result

            def create_file_dialog(self, *_args):
                return self.result

        bridge = Bridge()
        bridge._window = Window("D:\\Work")
        self.assertEqual(bridge.choose_folder(), "D:\\Work")

        bridge._window = Window(("D:\\Work",))
        self.assertEqual(bridge.choose_folder(), "D:\\Work")


class GitStatusParseTests(unittest.TestCase):
    def test_quoted_paths_are_unquoted(self):
        from eagent_bridge.git_ops import _parse_status

        branch, changes = _parse_status(
            '## main\n?? "a b.txt"\nR  "old name.txt" -> "new name.txt"\n'
        )

        self.assertEqual(branch, "main")
        self.assertEqual(
            [item["path"] for item in changes], ["a b.txt", "new name.txt"]
        )
        self.assertEqual(changes[0]["kind"], "untracked")
        self.assertEqual(changes[1]["kind"], "renamed")


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

    def test_workspace_switch_rejected_with_queued_tasks(self):
        other = self.workspace.parent / "other"
        other.mkdir()
        self.state.queue.append(QueuedTask({"task": "排队中"}))

        status, data = self.api("POST", "/api/workspace", {"path": str(other)})

        self.assertEqual(status, 409)
        self.assertIn("队列未清空", data["error"])
        self.assertEqual(self.state.workspace, self.workspace)

    def test_start_failure_releases_slot_and_advances_queue(self):
        from eagent_bridge.webui import _submit_run

        with mock.patch(
            "eagent_bridge.webui._start_run", side_effect=RuntimeError("bad config")
        ), mock.patch("eagent_bridge.webui._start_next") as next_mock:
            with self.assertRaises(RuntimeError):
                _submit_run(self.state, {"task": "x"})

        next_mock.assert_called_once_with(self.state)
        self.assertFalse(self.state.starting)

    def test_one_bad_queued_task_does_not_block_rest(self):
        self.state.queue.append(QueuedTask({"task": "坏任务"}))
        self.state.queue.append(QueuedTask({"task": "好任务"}))
        with mock.patch(
            "eagent_bridge.webui._start_run",
            side_effect=[RuntimeError("bad config"), RunState()],
        ) as start_mock:
            _start_next(self.state)

        self.assertEqual(start_mock.call_count, 2)
        self.assertEqual(self.state.queue, [])

    def test_failed_queued_task_is_reported(self):
        self.state.queue.append(QueuedTask({"task": "坏任务"}))
        with mock.patch(
            "eagent_bridge.webui._start_run", side_effect=RuntimeError("bad config")
        ):
            _start_next(self.state)

        status, data = self.api("POST", "/api/agent/queue", {})

        self.assertEqual(status, 200)
        self.assertEqual(data["failure"]["task"], "坏任务")
        self.assertEqual(data["failure"]["message"], "bad config")
        self.assertEqual(self.state.queue, [])


if __name__ == "__main__":
    unittest.main()
