"""本地工具插件：从 <workspace>/.eagent/tools/*.py 自动加载。

插件文件需要提供：
    TOOL_SPEC = {"name": "word_count", "description": "...", "parameters": {...}}
    READ_ONLY = True   # 可选，默认 False（执行前需要确认）
    def run(args, workspace): ...

插件是受信任的本地 Python 代码，加载时即执行模块顶层语句。
"""

from __future__ import annotations

import importlib.util
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_PLUGIN_BYTES = 200_000
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

_CACHE: dict[str, tuple[tuple, list["PluginTool"], list[str]]] = {}
_LOCK = threading.RLock()


@dataclass(frozen=True)
class PluginTool:
    name: str
    declared: str
    description: str
    parameters: dict
    read_only: bool
    run: Callable


def _load_module(path: Path):
    module_name = f"eagent_plugin_{path.stem}_{abs(hash(str(path))) & 0xFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("无法创建模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signature(folder: Path) -> tuple:
    if not folder.is_dir():
        return ()
    try:
        return tuple(
            (path.name, path.stat().st_mtime_ns, path.stat().st_size)
            for path in sorted(folder.glob("*.py"))
            if path.is_file()
        )
    except OSError:
        return ()


def load(workspace: Path) -> tuple[list[PluginTool], list[str]]:
    """加载插件；返回 (工具列表, 错误列表)。文件没变时直接复用缓存。"""
    folder = workspace / ".eagent" / "tools"
    signature = _signature(folder)
    key = str(workspace.resolve())
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1], cached[2]

    tools: list[PluginTool] = []
    errors: list[str] = []
    used: set[str] = set()
    for path in sorted(folder.glob("*.py")) if folder.is_dir() else []:
        try:
            if path.stat().st_size > MAX_PLUGIN_BYTES:
                raise ValueError("插件文件超过 200KB")
            module = _load_module(path)
            spec = getattr(module, "TOOL_SPEC", None)
            runner = getattr(module, "run", None)
            if not isinstance(spec, dict):
                raise ValueError("缺少 TOOL_SPEC 字典")
            if not callable(runner):
                raise ValueError("缺少 run(args, workspace) 函数")
            declared = str(spec.get("name") or "").strip()
            if not _NAME_RE.match(declared):
                raise ValueError("TOOL_SPEC.name 必须是 2-41 位小写字母/数字/下划线")
            name = "ext__" + declared
            if name in used:
                raise ValueError(f"工具名重复: {declared}")
            parameters = spec.get("parameters") or {}
            if not isinstance(parameters, dict):
                raise ValueError("parameters 必须是对象")
            used.add(name)
            tools.append(
                PluginTool(
                    name=name,
                    declared=declared,
                    description=str(spec.get("description") or ""),
                    parameters=parameters,
                    read_only=bool(getattr(module, "READ_ONLY", False)),
                    run=runner,
                )
            )
        except Exception as error:  # noqa: BLE001 - 单个插件失败不影响其它插件
            errors.append(f"{path.name}: {error}")

    with _LOCK:
        _CACHE[key] = (signature, tools, errors)
    return tools, errors


def find(workspace: Path, name: str) -> PluginTool | None:
    if not name.startswith("ext__"):
        return None
    tools, _ = load(workspace)
    for tool in tools:
        if tool.name == name:
            return tool
    return None


def describe(tools: list[PluginTool]) -> str:
    lines = []
    for tool in tools:
        properties = tool.parameters.get("properties") or {}
        required = tool.parameters.get("required") or []
        params = ", ".join(
            f"{key}{'' if key in required else '?'}" for key in properties
        ) or "无"
        suffix = "（只读）" if tool.read_only else "（需要确认）"
        lines.append(f"- {tool.name}({params})：{tool.description}{suffix}")
    return "\n".join(lines)
