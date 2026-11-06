"""Agent 工具集：目录、读写文件、文本搜索、命令执行。

所有路径都限制在工作目录内；危险操作通过 approver 交给上层确认。
"""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .gui_tools import GUI_NAMES, GUI_SPECS, GuiError, execute_gui
from .index import SKIP_DIRS, search_code
from .memory import save_tool as memory_save

MAX_OUTPUT = 3000


class ToolError(RuntimeError):
    """工具执行失败。"""


def tool_specs() -> list[dict]:
    base = [
        {
            "name": "list_dir",
            "description": "列出目录内容，深度 1 层",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
        },
        {
            "name": "read_file",
            "description": "按行读取文本文件，返回带行号的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "写入或覆盖文本文件（需要确认）",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        {
            "name": "replace_in_file",
            "description": "把文件里唯一出现的 old 文本替换成 new（需要确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
        {
            "name": "search_text",
            "description": "精确匹配文本（已知确切字符串时用它），返回 文件:行号:内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "glob": {"type": "string", "default": "*.py"},
                    "max_results": {"type": "integer", "default": 40},
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_code",
            "description": "按相关度检索代码（不确定在哪时先用它定位），返回最相关的代码块与行号",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "glob": {"type": "string", "description": "可选，例如 *.py"},
                    "max_results": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "run_command",
            "description": "执行一条命令并返回输出（需要确认，默认超时 120 秒）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "default": "."},
                },
                "required": ["command"],
            },
        },
        {
            "name": "save_memory",
            "description": "把重要事实写进长期记忆（跨会话保留），例如项目约定、常用命令、踩过的坑",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "一条简洁的事实，一行一条"},
                    "scope": {"type": "string", "enum": ["project", "global"], "default": "project"},
                },
                "required": ["note"],
            },
        },
        {
            "name": "finish",
            "description": "任务完成时调用，summary 说明做了什么",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    ]
    return base[:-1] + GUI_SPECS + [base[-1]]


def describe_tools(specs: list[dict] | None = None) -> str:
    specs = specs if specs is not None else tool_specs()
    lines = []
    for spec in specs:
        params = spec["parameters"].get("properties") or {}
        required = spec["parameters"].get("required") or []
        param_text = ", ".join(
            f"{key}{'' if key in required else '?'}" for key in params
        ) or "无"
        lines.append(f"- {spec['name']}({param_text})：{spec['description']}")
    return "\n".join(lines)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n...（输出过长，已截断，共 {len(text)} 字符）"


def safe_path(workspace: Path, raw: str) -> Path:
    path = (workspace / (raw or ".")).resolve()
    if path != workspace and workspace not in path.parents:
        raise ToolError(f"路径越界，只能访问工作目录: {raw}")
    return path


def _resolve(workspace: Path, raw: str) -> Path:
    return safe_path(workspace, raw)


def _list_dir(workspace: Path, args: dict) -> str:
    path = _resolve(workspace, args.get("path", "."))
    if not path.exists():
        raise ToolError(f"目录不存在: {args.get('path')}")
    if not path.is_dir():
        raise ToolError(f"不是目录: {args.get('path')}")
    entries = []
    for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            entries.append(f"{item.name}/")
        else:
            entries.append(f"{item.name}  ({item.stat().st_size} 字节)")
    if not entries:
        return "（空目录）"
    return "\n".join(entries)


def _read_file(workspace: Path, args: dict) -> str:
    path = _resolve(workspace, args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"文件不存在: {args.get('path')}")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise ToolError(f"读取失败: {error}") from error
    start = max(int(args.get("start_line") or 1), 1)
    end = int(args.get("end_line") or len(lines))
    end = min(end, len(lines), start + 400)
    body = [f"{index:>4} | {lines[index - 1]}" for index in range(start, end + 1)]
    header = f"{path.relative_to(workspace)}（第 {start}-{end} 行，共 {len(lines)} 行）"
    return header + "\n" + "\n".join(body)


def _write_file(workspace: Path, args: dict) -> str:
    raw = args.get("path", "")
    content = args.get("content")
    if not raw or not isinstance(content, str):
        raise ToolError("path 和 content 都必须提供")
    path = _resolve(workspace, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return f"已写入 {path.relative_to(workspace)}，{len(content)} 字符"


def _replace_in_file(workspace: Path, args: dict) -> str:
    raw = args.get("path", "")
    old = args.get("old")
    new = args.get("new")
    if not raw or not isinstance(old, str) or not isinstance(new, str) or not old:
        raise ToolError("path、old、new 都必须提供，且 old 不能为空")
    path = _resolve(workspace, raw)
    if not path.is_file():
        raise ToolError(f"文件不存在: {raw}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ToolError(f"文件不是 UTF-8 编码，已跳过以免写坏: {raw}") from error
    count = text.count(old)
    if count == 0:
        raise ToolError("要替换的内容没有出现，请先读文件确认原文")
    if count > 1:
        raise ToolError(f"要替换的内容出现了 {count} 次，请提供更长的唯一片段")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
    return f"已替换 {path.relative_to(workspace)} 中的 1 处内容"


def _search_text(workspace: Path, args: dict) -> str:
    query = args.get("query")
    if not query:
        raise ToolError("query 不能为空")
    pattern = args.get("glob") or "*"
    limit = min(int(args.get("max_results") or 40), 200)
    results = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            if not fnmatch.fnmatch(name, pattern):
                continue
            path = Path(root) / name
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if query in line:
                    results.append(f"{path.relative_to(workspace)}:{number}: {line.strip()[:160]}")
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    return "\n".join(results) if results else "没有找到匹配内容"


COMMAND_TIMEOUT = 120
MAX_COMMAND_OUTPUT = 20000


def _pump(stream, sink: "queue.Queue[str | None]") -> None:
    try:
        for line in stream:
            sink.put(line)
    finally:
        sink.put(None)


def _kill_tree(process: subprocess.Popen) -> None:
    """Windows 下 shell=True 只杀 cmd 会留下孤儿进程，需要连同子进程一起结束。"""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        else:
            process.kill()
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _run_command(workspace: Path, args: dict, on_output=None, stop_event=None) -> str:
    command = args.get("command")
    if not command:
        raise ToolError("command 不能为空")
    cwd = _resolve(workspace, args.get("cwd", "."))
    if not cwd.is_dir():
        raise ToolError(f"cwd 不是目录: {args.get('cwd')}")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise ToolError(f"命令启动失败：{error}") from error

    lines: list[str] = []
    total = 0
    truncated = False
    started = time.time()
    timed_out = False
    stopped = False
    sink: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_pump, args=(process.stdout, sink), daemon=True).start()

    try:
        while True:
            try:
                line = sink.get(timeout=0.2)
            except queue.Empty:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    _kill_tree(process)
                    break
                if time.time() - started > COMMAND_TIMEOUT:
                    timed_out = True
                    _kill_tree(process)
                    break
                continue
            if line is None:
                break
            if total < MAX_COMMAND_OUTPUT:
                lines.append(line)
                total += len(line)
            elif not truncated:
                truncated = True
                lines.append("...（输出过长，后续已省略）\n")
            if on_output is not None:
                try:
                    on_output(line.rstrip("\r\n"))
                except Exception:
                    pass
    finally:
        if process.poll() is None:
            _kill_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    if stopped:
        raise ToolError("命令被用户停止")
    if timed_out:
        raise ToolError(f"命令超时（{COMMAND_TIMEOUT} 秒）: {command}")

    output = "".join(lines).strip()
    parts = [f"exit_code={process.returncode}"]
    if output:
        parts.append("output:\n" + output)
    return _truncate("\n".join(parts))


def _search_code(workspace: Path, args: dict) -> str:
    try:
        return search_code(workspace, args)
    except ValueError as error:
        raise ToolError(str(error)) from error


def _save_memory(workspace: Path, args: dict) -> str:
    try:
        return memory_save(workspace, args)
    except ValueError as error:
        raise ToolError(str(error)) from error


_HANDLERS = {
    "list_dir": _list_dir,
    "read_file": _read_file,
    "write_file": _write_file,
    "replace_in_file": _replace_in_file,
    "search_text": _search_text,
    "search_code": _search_code,
    "save_memory": _save_memory,
    "run_command": _run_command,
}

_DANGEROUS = {"write_file", "replace_in_file", "run_command"} | GUI_NAMES


def _approval_detail(name: str, args: dict) -> str:
    if name == "run_command":
        return f"命令：{args.get('command')}\n目录：{args.get('cwd') or '.'}"
    if name == "write_file":
        content = args.get("content") or ""
        preview = content[:600] + ("..." if len(content) > 600 else "")
        return f"写入：{args.get('path')}\n\n{preview}"
    return f"文件：{args.get('path')}\n\n旧：\n{args.get('old')}\n\n新：\n{args.get('new')}"


def preview_action(name: str, args: dict, workspace: Path) -> dict:
    """给界面提供改文件前后的内容，用于渲染 diff。"""
    if name in ("write_file", "replace_in_file"):
        raw = args.get("path") or ""
        try:
            path = safe_path(workspace, raw)
        except ToolError:
            return {"kind": "other", "detail": f"路径非法: {raw}"}
        old = ""
        if path.is_file():
            try:
                old = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
        if name == "write_file":
            new = args.get("content") if isinstance(args.get("content"), str) else ""
        else:
            new = old
            old_fragment = args.get("old")
            new_fragment = args.get("new")
            if isinstance(old_fragment, str) and old_fragment and old.count(old_fragment) == 1:
                new = old.replace(old_fragment, new_fragment if isinstance(new_fragment, str) else "", 1)
        return {
            "kind": "write",
            "path": str(path.relative_to(workspace)),
            "old": old,
            "new": new,
        }
    if name == "run_command":
        return {
            "kind": "command",
            "command": args.get("command") or "",
            "cwd": args.get("cwd") or ".",
        }
    detail = json.dumps(args, ensure_ascii=False)[:600]
    return {"kind": "other", "detail": f"{name}\n{detail}"}


def execute(
    name: str,
    args: dict,
    workspace: Path,
    approver=None,
    auto_allow: bool = False,
    before_write=None,
    on_start=None,
    on_output=None,
    stop_event=None,
) -> str:
    if not isinstance(args, dict):
        raise ToolError("args 必须是对象")

    handler = _HANDLERS.get(name)
    if handler is None and name not in GUI_NAMES:
        raise ToolError(f"未知工具: {name}")

    if name in _DANGEROUS and not auto_allow:
        if approver is None:
            raise ToolError(f"{name} 需要人工确认，但当前没有确认通道")
        if not approver(name, args, workspace):
            raise ToolError("操作被用户拒绝，请换一种方式或询问用户")

    if before_write is not None:
        try:
            before_write(name, args)
        except Exception:
            pass

    if on_start is not None:
        try:
            on_start(name, args)
        except Exception:
            pass

    if name == "run_command":
        return _truncate(_run_command(workspace, args, on_output=on_output, stop_event=stop_event))

    if name in GUI_NAMES:
        try:
            return _truncate(execute_gui(name, args, workspace))
        except GuiError as error:
            raise ToolError(str(error)) from error

    try:
        return _truncate(handler(workspace, args))
    except ToolError:
        raise
    except (ValueError, TypeError) as error:
        raise ToolError(str(error)) from error


def specs_json() -> str:
    return json.dumps(tool_specs(), ensure_ascii=False, indent=2)
