"""Agent 工具集：目录、读写文件、文本搜索、命令执行。

所有路径都限制在工作目录内；危险操作通过 approver 交给上层确认。
"""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .gui_tools import GUI_NAMES, GUI_SPECS, GuiError, execute_gui
from .index import SKIP_DIRS, search_code
from .memory import save_tool as memory_save
from .office import read_docx, read_pptx, read_xlsx, write_docx, write_pptx, write_xlsx
from .plugins import find as find_plugin
from .proc import kill_tree

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
            "name": "find_files",
            "description": "按文件名或相对路径的通配符查找文件，例如 *.py、src/**/*.ts",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "通配符，例如 *.py"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "batch_files",
            "description": "批量复制、移动或删除文件/目录（需要确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["copy", "move", "delete"]},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "destination": {"type": "string", "description": "copy/move 的目标目录"},
                },
                "required": ["action", "paths"],
            },
        },
        {
            "name": "http_request",
            "description": "发起 HTTP 请求，可选把响应保存到工作目录（需要确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], "default": "GET"},
                    "url": {"type": "string"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                    "timeout": {"type": "integer", "description": "秒，默认 30，最大 120"},
                    "save_path": {"type": "string", "description": "可选，把响应写入这个相对路径"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "office_document",
            "description": "读写 Word/Excel/PPT 文档（docx/xlsx/pptx）；写入需要确认",
            "parameters": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["docx", "xlsx", "pptx"]},
                    "action": {"type": "string", "enum": ["read", "write"]},
                    "path": {"type": "string"},
                    "blocks": {
                        "type": "array",
                        "description": "docx 写入：heading/paragraph/table 块",
                        "items": {"type": "object"},
                    },
                    "sheets": {
                        "type": "array",
                        "description": "xlsx 写入：[{name, rows}]",
                        "items": {"type": "object"},
                    },
                    "slides": {
                        "type": "array",
                        "description": "pptx 写入：[{title, bullets}]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["format", "action", "path"],
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
        try:
            if item.is_dir():
                entries.append(f"{item.name}/")
            else:
                entries.append(f"{item.name}  ({item.stat().st_size} 字节)")
        except OSError:
            # 遍历期间文件被删除或没有权限，跳过即可
            continue
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
    if start > len(lines):
        raise ToolError(f"起始行 {start} 超过文件总行数 {len(lines)}")
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
                    kill_tree(process)
                    break
                if time.time() - started > COMMAND_TIMEOUT:
                    timed_out = True
                    kill_tree(process)
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
            kill_tree(process)
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


HTTP_TIMEOUT = 30
HTTP_MAX_TEXT = 20000
HTTP_MAX_FILE = 50 * 1024 * 1024


def _find_files(workspace: Path, args: dict) -> str:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        raise ToolError("pattern 不能为空")
    base = _resolve(workspace, str(args.get("path") or "."))
    if not base.is_dir():
        raise ToolError(f"不是目录: {args.get('path')}")
    try:
        limit = min(max(int(args.get("max_results") or 100), 1), 500)
    except (TypeError, ValueError):
        limit = 100
    needle = pattern.lower()
    results: list[str] = []
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(root) / name
            relative = str(path.relative_to(workspace)).replace("\\", "/")
            if fnmatch.fnmatch(name.lower(), needle) or fnmatch.fnmatch(relative.lower(), needle):
                results.append(relative)
                if len(results) >= limit:
                    return "\n".join(results)
    return "\n".join(results) if results else "没有找到匹配文件"


def _batch_files(workspace: Path, args: dict) -> str:
    action = str(args.get("action") or "").lower()
    if action not in ("copy", "move", "delete"):
        raise ToolError("action 只能是 copy / move / delete")
    raw_paths = args.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ToolError("paths 必须是非空数组")
    if len(raw_paths) > 200:
        raise ToolError("一次最多处理 200 个路径")

    sources: list[Path] = []
    for raw in raw_paths:
        source = _resolve(workspace, str(raw))
        if source == workspace:
            raise ToolError("不能操作工作目录本身")
        if not source.exists():
            raise ToolError(f"路径不存在: {raw}")
        sources.append(source)
    internal = workspace / ".eagent"
    if any(source == internal or internal in source.parents for source in sources):
        raise ToolError("不能批量操作 .eagent 内部文件")

    if action == "delete":
        try:
            for source in sources:
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
        except OSError as error:
            raise ToolError(f"删除失败：{error}") from error
        return f"已删除 {len(sources)} 个路径"

    raw_destination = str(args.get("destination") or "").strip()
    if not raw_destination:
        raise ToolError("destination 不能为空")
    destination = _resolve(workspace, raw_destination)
    if destination == internal or internal in destination.parents:
        raise ToolError("目标目录不能位于 .eagent 内部")
    if destination.exists() and not destination.is_dir():
        raise ToolError("destination 必须是目录")
    for source in sources:
        if source.is_dir() and (destination == source or source in destination.parents):
            raise ToolError(f"目标目录不能位于源目录内部: {source.name}")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ToolError(f"创建目标目录失败：{error}") from error

    targets = [destination / source.name for source in sources]
    if len({target.name for target in targets}) != len(targets):
        raise ToolError("多个路径同名，无法批量处理")
    for target in targets:
        if target.exists():
            raise ToolError(f"目标已存在: {target.relative_to(workspace)}")

    try:
        for source, target in zip(sources, targets):
            if action == "copy":
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
            else:
                shutil.move(str(source), str(target))
    except OSError as error:
        raise ToolError(f"{action} 失败：{error}") from error
    verb = "复制" if action == "copy" else "移动"
    return f"已{verb} {len(sources)} 个路径到 {destination.relative_to(workspace)}"


def _http_request(workspace: Path, args: dict) -> str:
    method = str(args.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise ToolError("不支持的 method")
    url = str(args.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError("url 必须是 http/https 地址")
    headers = args.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise ToolError("headers 必须是对象")
    header_map = {str(key): str(value) for key, value in (headers or {}).items()}
    body = args.get("body")
    data = None
    if body is not None and method != "HEAD":
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        data = text.encode("utf-8")
    try:
        timeout = min(max(int(args.get("timeout") or HTTP_TIMEOUT), 1), 120)
    except (TypeError, ValueError):
        timeout = HTTP_TIMEOUT

    request = urllib.request.Request(url, data=data, headers=header_map, method=method)
    save_path = str(args.get("save_path") or "").strip()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(HTTP_MAX_FILE + 1 if save_path else HTTP_MAX_TEXT + 1)
    except urllib.error.HTTPError as error:
        text = error.read(HTTP_MAX_TEXT).decode("utf-8", "replace")
        return f"HTTP {error.code}\n{text}".strip()
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ToolError(f"请求失败：{error}") from error

    if save_path:
        if len(raw) > HTTP_MAX_FILE:
            raise ToolError("响应超过 50MB，已中止保存")
        target = _resolve(workspace, save_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        except OSError as error:
            raise ToolError(f"保存响应失败：{error}") from error
        return f"HTTP {status} {content_type}\n已保存到 {target.relative_to(workspace)}（{len(raw)} 字节）"

    truncated = len(raw) > HTTP_MAX_TEXT
    raw = raw[:HTTP_MAX_TEXT]
    encoding = "utf-8"
    if "charset=" in content_type.lower():
        encoding = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
    try:
        text = raw.decode(encoding, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    suffix = "\n...（响应过长，已截断）" if truncated else ""
    return f"HTTP {status} {content_type}\n{text}{suffix}".strip()


def _office_document(workspace: Path, args: dict) -> str:
    file_format = str(args.get("format") or "").lower()
    action = str(args.get("action") or "").lower()
    if file_format not in ("docx", "xlsx", "pptx"):
        raise ToolError("format 只能是 docx / xlsx / pptx")
    if action not in ("read", "write"):
        raise ToolError("action 只能是 read / write")
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        raise ToolError("path 不能为空")
    path = _resolve(workspace, raw_path)

    if action == "read":
        if not path.is_file():
            raise ToolError(f"文件不存在: {raw_path}")
        try:
            if file_format == "docx":
                text = read_docx(path)
            elif file_format == "xlsx":
                sheets = read_xlsx(path)
                lines = []
                for sheet in sheets:
                    lines.append(f"【{sheet['name']}】")
                    for row in sheet["rows"]:
                        lines.append("\t".join(str(value) for value in row))
                text = "\n".join(lines)
            else:
                slides = read_pptx(path)
                text = "\n\n".join(
                    f"--- 第 {index} 页 ---\n{content}"
                    for index, content in enumerate(slides, start=1)
                )
        except ValueError as error:
            raise ToolError(str(error)) from error
        return _truncate(text or "（空文档）")

    if file_format == "docx":
        blocks = args.get("blocks")
        if not isinstance(blocks, list):
            raise ToolError("docx 写入需要 blocks 数组")
        try:
            write_docx(path, blocks)
        except OSError as error:
            raise ToolError(f"写入 Office 文档失败：{error}") from error
        return f"已生成 {path.relative_to(workspace)}（{len(blocks)} 个内容块）"
    if file_format == "xlsx":
        sheets = args.get("sheets")
        if not isinstance(sheets, list):
            raise ToolError("xlsx 写入需要 sheets 数组")
        try:
            write_xlsx(path, sheets)
        except OSError as error:
            raise ToolError(f"写入 Office 文档失败：{error}") from error
        return f"已生成 {path.relative_to(workspace)}（{len(sheets)} 个工作表）"
    slides = args.get("slides")
    if not isinstance(slides, list):
        raise ToolError("pptx 写入需要 slides 数组")
    try:
        write_pptx(path, slides)
    except ValueError as error:
        raise ToolError(str(error)) from error
    except OSError as error:
        raise ToolError(f"写入 Office 文档失败：{error}") from error
    count = max(1, len([item for item in slides if isinstance(item, dict)]))
    return f"已生成 {path.relative_to(workspace)}（{count} 页）"


def _needs_approval(name: str, args: dict) -> bool:
    if name == "office_document":
        return str(args.get("action") or "").lower() != "read"
    return name in _DANGEROUS


_HANDLERS = {
    "list_dir": _list_dir,
    "read_file": _read_file,
    "write_file": _write_file,
    "replace_in_file": _replace_in_file,
    "search_text": _search_text,
    "search_code": _search_code,
    "find_files": _find_files,
    "batch_files": _batch_files,
    "http_request": _http_request,
    "office_document": _office_document,
    "save_memory": _save_memory,
    "run_command": _run_command,
}

_DANGEROUS = {
    "write_file", "replace_in_file", "run_command", "batch_files", "http_request",
} | GUI_NAMES
# 只读的桌面动作不需要逐次确认，否则电脑模式几乎无法使用
READ_ONLY_GUI = {"screen_capture", "window_list"}


def _approval_detail(name: str, args: dict) -> str:
    if name == "run_command":
        return f"命令：{args.get('command')}\n目录：{args.get('cwd') or '.'}"
    if name == "batch_files":
        return (
            f"操作：{args.get('action')}\n"
            f"数量：{len(args.get('paths') or [])}\n"
            f"目标：{args.get('destination') or '（无）'}"
        )
    if name == "http_request":
        return f"请求：{str(args.get('method') or 'GET').upper()} {args.get('url')}"
    if name == "office_document":
        return (
            f"写入 {str(args.get('format') or '').upper()}：{args.get('path')}\n"
            f"操作：{args.get('action')}"
        )
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
    if name in ("batch_files", "http_request", "office_document"):
        return {"kind": "other", "detail": _approval_detail(name, args)}
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

    plugin = find_plugin(workspace, name)
    handler = _HANDLERS.get(name)
    if handler is None and name not in GUI_NAMES and plugin is None:
        raise ToolError(f"未知工具: {name}")

    if plugin is not None:
        if not plugin.read_only and not auto_allow:
            if approver is None:
                raise ToolError(f"{name} 需要人工确认，但当前没有确认通道")
            if not approver(name, args, workspace):
                raise ToolError("操作被用户拒绝，请换一种方式或询问用户")
        try:
            result = plugin.run(dict(args), workspace)
        except Exception as error:  # noqa: BLE001 - 插件异常统一转成工具错误
            raise ToolError(f"插件 {plugin.name} 执行失败：{error}") from error
        return _truncate(str(result))

    if _needs_approval(name, args) and name not in READ_ONLY_GUI and not auto_allow:
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
    except (ValueError, TypeError, OSError) as error:
        raise ToolError(str(error)) from error


def specs_json() -> str:
    return json.dumps(tool_specs(), ensure_ascii=False, indent=2)
