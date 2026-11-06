"""工作区侧能力：终端、上传、MCP 配置与文件树 / 搜索。"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .proc import kill_tree
from .storage import prune as prune_folder
from .tools import SKIP_DIRS, safe_path

if TYPE_CHECKING:
    from .agent_service import AppState

TERM_TIMEOUT = 120


def _terminal_exec(state: AppState, command: str) -> dict:
    """在终端里执行一条命令；cd 单独处理，保证前缀里的目录真的生效。"""
    text = (command or "").strip()
    cwd = state.term_cwd
    if not text:
        return {"output": "", "exit_code": 0, "cwd": str(cwd)}

    match = re.match(r"^cd(?:\s+(.*))?$", text, re.IGNORECASE)
    if match:
        raw = (match.group(1) or "").strip().strip('"').strip("'")
        target = Path(raw).expanduser() if raw else state.workspace
        if not target.is_absolute():
            target = cwd / target
        try:
            target = target.resolve()
        except OSError:
            return {"output": f"cd: 路径无效: {raw}\n", "exit_code": 1, "cwd": str(cwd)}
        if not target.is_dir():
            return {"output": f"cd: 目录不存在: {raw}\n", "exit_code": 1, "cwd": str(cwd)}
        state.term_cwd = target
        return {"output": "", "exit_code": 0, "cwd": str(target)}

    try:
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + text],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return {"output": f"无法启动 PowerShell：{error}\n", "exit_code": 1, "cwd": str(cwd)}

    try:
        output, _ = process.communicate(timeout=TERM_TIMEOUT)
        code = process.returncode
    except subprocess.TimeoutExpired:
        # 只杀 powershell 会留下它启动的子进程，必须按进程树清理
        kill_tree(process)
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return {"output": f"命令超时（{TERM_TIMEOUT} 秒）\n", "exit_code": -1, "cwd": str(cwd)}

    output = (output or "").lstrip("\r\n")
    if not output.endswith("\n") and output:
        output += "\n"
    return {"output": output, "exit_code": code, "cwd": str(cwd)}


def _mcp_file(state: AppState) -> Path:
    return state.workspace / ".eagent" / "mcp.json"


UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _save_upload(workspace: Path, name: str, data_b64: str) -> str:
    if not data_b64:
        raise ValueError("图片数据为空")
    try:
        raw = base64.b64decode(data_b64, validate=False)
    except Exception as error:  # noqa: BLE001
        raise ValueError("图片数据不是合法 base64") from error
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("图片太大（超过 12MB）")
    suffix = Path(name).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        suffix = ".png"
    folder = workspace / ".eagent" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"img-{int(time.time() * 1000)}{suffix}"
    target.write_bytes(raw)
    prune_folder(folder, 50, "img-*")
    return str(target.relative_to(workspace)).replace("\\", "/")


def _load_mcp_servers(state: AppState) -> list[dict]:
    path = _mcp_file(state)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    items = []
    for name, config in servers.items():
        if not isinstance(config, dict) or not config.get("command"):
            continue
        if config.get("enabled") is False:
            continue
        raw_args = config.get("args")
        raw_safe = config.get("safeTools") or config.get("safe_tools")
        items.append(
            {
                "name": name,
                "command": str(config["command"]),
                "args": [str(item) for item in raw_args] if isinstance(raw_args, list) else [],
                "env": config.get("env") if isinstance(config.get("env"), dict) else {},
                # 只有真正的布尔 true 才自动放行，字符串 "false" 不能当成真
                "autoApprove": (
                    config.get("autoApprove") is True or config.get("auto_approve") is True
                ),
                "enabled": config.get("enabled", True) is not False,
                "safeTools": [str(item) for item in raw_safe] if isinstance(raw_safe, list) else [],
            }
        )
    return items


def _describe_mcp_servers(state: AppState) -> list[dict]:
    path = _mcp_file(state)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    items = []
    for name, config in servers.items():
        if not isinstance(config, dict):
            continue
        items.append(
            {
                "name": name,
                "command": config.get("command") or "",
                "args": config.get("args") if isinstance(config.get("args"), list) else [],
                "enabled": config.get("enabled", True) is not False,
                "autoApprove": (
                    config.get("autoApprove") is True or config.get("auto_approve") is True
                ),
            }
        )
    return items


def _list_files(state: AppState, limit: int = 8000) -> list[str]:
    files = []
    for root, dirs, names in os.walk(state.workspace):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in names:
            files.append(str((Path(root) / name).relative_to(state.workspace)).replace("\\", "/"))
            if len(files) >= limit:
                return sorted(files)
    return sorted(files)


def _search_text(state: AppState, query: str, limit: int = 80) -> list[dict]:
    if not query.strip():
        return []
    needle = query.lower()
    results = []
    for root, dirs, names in os.walk(state.workspace):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in names:
            path = Path(root) / name
            try:
                if path.stat().st_size > 1_500_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower():
                    results.append(
                        {
                            "path": str(path.relative_to(state.workspace)).replace("\\", "/"),
                            "line": number,
                            "text": line.strip()[:160],
                        }
                    )
                    if len(results) >= limit:
                        return results
    return results


def _tree(state: AppState, raw_path: str) -> list[dict]:
    path = safe_path(state.workspace, raw_path or ".")
    if not path.is_dir():
        raise ValueError(f"不是目录: {raw_path}")
    entries = []
    for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name in SKIP_DIRS:
            continue
        try:
            is_dir = item.is_dir()
            size = item.stat().st_size if not is_dir else 0
        except OSError:
            # 遍历期间文件被删除或没有权限，跳过即可
            continue
        relative = str(item.relative_to(state.workspace))
        entries.append(
            {
                "name": item.name,
                "path": relative,
                "type": "dir" if is_dir else "file",
                "size": size,
            }
        )
    return entries


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("这是二进制文件，编辑器无法打开") from error
