"""Agent 运行时：完成、降级、压缩与工具调用。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.adapters import AdapterError  # noqa: E402
from eagent_bridge.backends import ChatBackend  # noqa: E402
from eagent_bridge.runtime import COMPACT_THRESHOLD, AgentRuntime  # noqa: E402


def finish(summary: str = "done") -> str:
    return json.dumps({"thought": "", "action": "finish", "args": {"summary": summary}})


class FakeBackend(ChatBackend):
    def __init__(self, replies):
        self.replies = list(replies)
        self.started = False
        self.closed = False
        self.compacted = None
        self.prompts: list[str] = []

    def start(self, rules, stop_event=None):
        self.started = True

    def ask(self, text, stop_event=None):
        self.prompts.append(text)
        return self.replies.pop(0)

    def compact(self, summary):
        self.compacted = summary

    def close(self):
        self.closed = True


class FailingBackend(FakeBackend):
    def ask(self, text, stop_event=None):
        raise AdapterError("主模型不可用")


class RuntimeTests(unittest.TestCase):
    def test_finish_action_ends_run(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = FakeBackend([finish()])
            runtime = AgentRuntime(workspace=Path(folder), backend=backend)

            self.assertEqual(runtime.run("任务"), "done")
            self.assertTrue(backend.started)
            self.assertTrue(backend.closed)

    def test_switches_to_fallback_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            fallback = FakeBackend([finish("fallback-done")])
            primary = FailingBackend([])
            config = {"provider": "openai", "baseUrl": "http://127.0.0.1/v1", "model": "m"}
            with mock.patch("eagent_bridge.runtime.build_backend", return_value=fallback):
                runtime = AgentRuntime(
                    workspace=Path(folder), backend=primary, fallback_config=config
                )
                self.assertEqual(runtime.run("任务"), "fallback-done")
            self.assertTrue(primary.closed)
            self.assertTrue(fallback.prompts)
            self.assertIn("任务", fallback.prompts[0])

    def test_compaction_replaces_history(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = FakeBackend(["摘要内容"])
            runtime = AgentRuntime(workspace=Path(folder), backend=backend)
            runtime._transcript = ["x" * COMPACT_THRESHOLD]
            runtime._history_chars = COMPACT_THRESHOLD

            runtime._maybe_compact()

            self.assertEqual(backend.compacted, "摘要内容")
            self.assertEqual(runtime._history_chars, len("摘要内容"))

    def test_tool_action_writes_file(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            replies = [
                json.dumps(
                    {"thought": "", "action": "write_file", "args": {"path": "a.txt", "content": "hi"}}
                ),
                finish("written"),
            ]
            runtime = AgentRuntime(
                workspace=workspace, backend=FakeBackend(replies), auto_allow=True
            )

            self.assertEqual(runtime.run("写文件"), "written")
            self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "hi")


if __name__ == "__main__":
    unittest.main()
