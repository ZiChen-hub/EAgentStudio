"""本地工具插件：加载、审批与运行时集成。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.backends import ChatBackend  # noqa: E402
from eagent_bridge.plugins import load  # noqa: E402
from eagent_bridge.runtime import AgentRuntime  # noqa: E402
from eagent_bridge.tools import ToolError, execute  # noqa: E402

ECHO_PLUGIN = """
TOOL_SPEC = {
    "name": "echo_args",
    "description": "回显参数",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}
READ_ONLY = True

def run(args, workspace):
    return "echo:" + str(args.get("text"))
"""

WRITER_PLUGIN = """
TOOL_SPEC = {"name": "writer", "description": "写一个文件", "parameters": {"type": "object", "properties": {}}}

def run(args, workspace):
    (workspace / "plugin-created.txt").write_text("ok", encoding="utf-8")
    return "written"
"""


def make_workspace(folder: str) -> Path:
    workspace = Path(folder)
    (workspace / ".eagent" / "tools").mkdir(parents=True)
    return workspace


def write_plugin(workspace: Path, name: str, content: str) -> None:
    (workspace / ".eagent" / "tools" / name).write_text(content, encoding="utf-8")


class _Backend(ChatBackend):
    def __init__(self, replies):
        self.replies = list(replies)

    def start(self, rules, stop_event=None):
        pass

    def ask(self, text, stop_event=None):
        return self.replies.pop(0)

    def close(self):
        pass


class PluginTests(unittest.TestCase):
    def test_read_only_plugin_runs_without_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = make_workspace(folder)
            write_plugin(workspace, "echo.py", ECHO_PLUGIN)

            tools, errors = load(workspace)
            self.assertEqual(errors, [])
            self.assertEqual(tools[0].name, "ext__echo_args")

            def approver(*args):
                raise AssertionError("只读插件不应该请求确认")

            result = execute("ext__echo_args", {"text": "hi"}, workspace, approver=approver)
            self.assertEqual(result, "echo:hi")

    def test_write_plugin_requires_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = make_workspace(folder)
            write_plugin(workspace, "writer.py", WRITER_PLUGIN)

            with self.assertRaises(ToolError):
                execute("ext__writer", {}, workspace, approver=lambda *args: False)
            self.assertFalse((workspace / "plugin-created.txt").exists())

            execute("ext__writer", {}, workspace, auto_allow=True)
            self.assertEqual(
                (workspace / "plugin-created.txt").read_text(encoding="utf-8"), "ok"
            )

    def test_broken_plugin_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = make_workspace(folder)
            write_plugin(workspace, "echo.py", ECHO_PLUGIN)
            write_plugin(workspace, "broken.py", "def run(:\n")

            tools, errors = load(workspace)

            self.assertEqual(len(tools), 1)
            self.assertTrue(any("broken.py" in error for error in errors))

    def test_invalid_tool_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = make_workspace(folder)
            write_plugin(
                workspace,
                "bad.py",
                'TOOL_SPEC = {"name": "Bad-Name", "description": "", "parameters": {}}\n'
                "def run(args, workspace):\n    return ''\n",
            )

            tools, errors = load(workspace)

            self.assertEqual(tools, [])
            self.assertTrue(errors)

    def test_runtime_exposes_plugin_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = make_workspace(folder)
            write_plugin(workspace, "echo.py", ECHO_PLUGIN)
            replies = [
                json.dumps(
                    {"thought": "", "action": "ext__echo_args", "args": {"text": "hi"}}
                ),
                json.dumps({"thought": "", "action": "finish", "args": {"summary": "done"}}),
            ]
            runtime = AgentRuntime(
                workspace=workspace, backend=_Backend(replies), auto_allow=True
            )

            self.assertIn("ext__echo_args", runtime.enabled_tools)
            self.assertEqual(runtime.run("任务"), "done")
            self.assertIn("echo:hi", "".join(runtime._transcript))


if __name__ == "__main__":
    unittest.main()
