"""长期记忆：跨会话保存下来的项目事实与偏好，任务开始时注入提示词。

项目记忆放在 <workspace>/.eagent/memory.md；
全局记忆放在 ~/.eagent/memory.md（可用 EAGENT_HOME 改根目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_MEMORY_CHARS = 4000
MAX_TOTAL_CHARS = 6000
SCOPES = ("project", "global")


def _base_dir(workspace: Path, scope: str) -> Path:
    if scope == "global":
        base = os.environ.get("EAGENT_HOME") or os.path.join(os.path.expanduser("~"), ".eagent")
        return Path(base)
    return workspace / ".eagent"


def memory_path(workspace: Path, scope: str = "project") -> Path:
    scope = _check_scope(scope)
    return _base_dir(workspace, scope) / "memory.md"


def _check_scope(scope: str) -> str:
    value = str(scope or "project").lower()
    if value not in SCOPES:
        raise ValueError(f"未知的记忆作用域: {scope}")
    return value


def load(workspace: Path, scope: str = "project") -> str:
    path = memory_path(workspace, scope)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_MEMORY_CHARS]
    except OSError:
        return ""


def save(workspace: Path, content: str, scope: str = "project") -> None:
    scope = _check_scope(scope)
    path = memory_path(workspace, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (content or "").strip()
    path.write_text(text + ("\n" if text else ""), encoding="utf-8", newline="")


def append(workspace: Path, note: str, scope: str = "project") -> str:
    """追加一条记忆，返回追加后的完整内容。"""
    scope = _check_scope(scope)
    text = (note or "").strip()
    if not text:
        raise ValueError("记忆内容不能为空")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    current = load(workspace, scope).rstrip()
    existing = {line.strip() for line in current.splitlines()}
    fresh = [
        bullet
        for bullet in (line if line.startswith("- ") else f"- {line}" for line in lines)
        if bullet.strip() not in existing
    ]
    if not fresh:
        return current
    extra = "\n".join(fresh)
    merged = (current + "\n" + extra).strip() if current else extra
    if len(merged) > MAX_MEMORY_CHARS:
        merged = merged[-MAX_MEMORY_CHARS:]
    save(workspace, merged, scope)
    return merged


def build_memory(workspace: Path) -> str:
    """把项目记忆与全局记忆拼成提示词片段。"""
    parts: list[str] = []
    used = 0
    for scope, label in (("project", "项目记忆"), ("global", "全局记忆")):
        text = load(workspace, scope).strip()
        if not text:
            continue
        block = f"【{label}】\n{text}"
        if used + len(block) > MAX_TOTAL_CHARS:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def save_tool(workspace: Path, args: dict) -> str:
    """save_memory 工具的实现。"""
    note = str(args.get("note") or "").strip()
    scope = str(args.get("scope") or "project")
    if not note:
        raise ValueError("记忆内容不能为空")
    merged = append(workspace, note, scope)
    label = "项目记忆" if scope == "project" else "全局记忆"
    return f"已写入{label}（当前 {len(merged)} 字）"
