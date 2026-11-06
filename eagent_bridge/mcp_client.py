"""MCP stdio 客户端：启动外部 server，列出工具并调用。"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    """MCP 服务调用失败。"""


class MCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
        timeout: float = 30.0,
    ):
        self.name = name
        self.command = command
        self.args = [str(item) for item in args] if isinstance(args, list) else []
        self.extra_env = env if isinstance(env, dict) else {}
        self.timeout = timeout
        self.server_info: dict = {}

        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._stderr_lines: list[str] = []
        self._closed = False

    # ---- 生命周期 ----

    def start(self) -> None:
        import os

        environment = {**os.environ, **self.extra_env}
        command_line = subprocess.list2cmdline([self.command, *self.args])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                command_line,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=creationflags,
            )
        except OSError as error:
            raise MCPError(f"[{self.name}] 启动失败：{error}；请确认命令已安装") from error

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "eagent-studio", "version": "0.1.0"},
            },
            timeout=self.timeout,
        )
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._proc
        if not process:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                )
                process.wait(timeout=3)
            else:
                process.terminate()
                process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ---- 能力 ----

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list", {}, timeout=self.timeout)
        tools = result.get("tools")
        return [item for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        timeout: float = 120.0,
        image_dir: Path | None = None,
        on_image=None,
    ) -> str:
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=timeout,
        )
        parts = []
        raw_content = result.get("content")
        content = raw_content if isinstance(raw_content, list) else []
        for index, item in enumerate(content):
            if not isinstance(item, dict):
                parts.append(str(item)[:200])
                continue
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text") or "")
            elif kind == "image":
                data = item.get("data")
                mime = item.get("mimeType") or "image/png"
                if image_dir is not None and data:
                    try:
                        image_dir.mkdir(parents=True, exist_ok=True)
                        ext = "jpg" if "jpeg" in mime or "jpg" in mime else "png"
                        target = image_dir / f"mcp-{int(time.time() * 1000)}-{index}.{ext}"
                        target.write_bytes(base64.b64decode(data))
                        parts.append(f"[截图已保存：{target}]")
                        if on_image:
                            on_image(str(target))
                    except (OSError, ValueError):
                        parts.append("[图片内容已省略]")
                else:
                    parts.append("[图片内容已省略]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:200])
        text = "\n".join(parts).strip() or "（无输出）"
        if result.get("isError"):
            text = "工具返回错误：" + text
        return text

    # ---- 内部 ----

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._cv:
            self._next_id += 1
            request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        deadline = time.time() + timeout
        with self._cv:
            try:
                while request_id not in self._responses:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        tail = self._stderr_tail()
                        raise MCPError(
                            f"[{self.name}] {method} 超时（{int(timeout)} 秒）"
                            + (f"；stderr: {tail}" if tail else "")
                        )
                    self._cv.wait(min(remaining, 1.0))
                message = self._responses.pop(request_id)
            finally:
                # 超时后迟到的响应也要丢掉，避免 _responses 无限增长
                self._responses.pop(request_id, None)

        if "error" in message:
            error = message["error"]
            raise MCPError(f"[{self.name}] {method} 失败：{error.get('message', error)}")
        return message.get("result") or {}

    def _write(self, message: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPError(f"[{self.name}] 进程未启动")
        line = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise MCPError(f"[{self.name}] 写入失败：{error}") from error

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                with self._cv:
                    self._responses[message["id"]] = message
                    self._cv.notify_all()

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr_lines.append(line.strip())
            del self._stderr_lines[:-20]

    def _stderr_tail(self) -> str:
        return " | ".join(self._stderr_lines[-5:])[:600]


def describe_tools_for_prompt(tools: list[dict], prefix: str) -> str:
    """把 MCP 工具压成提示词里的短描述。"""
    lines = []
    for tool in tools[:40]:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or "unknown"
        description = str(tool.get("description") or "").replace("\n", " ")[:80]
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        required = schema.get("required")
        if not isinstance(required, list):
            required = []
        params = ", ".join(
            f"{key}{'' if key in required else '?'}" for key in properties
        ) or "无"
        lines.append(f"- {prefix}{name}({params})：{description}")
    return "\n".join(lines)
