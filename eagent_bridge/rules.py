"""规则系统：项目级与全局的 Markdown 约定，任务开始时注入提示词。

项目规则放在 <workspace>/.eagent/rules/*.md；
全局规则放在 ~/.eagent/rules/*.md（可用 EAGENT_HOME 改根目录）。
文件名即规则 ID，正文就是约定内容，可选 front matter 提供 name / description。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

MAX_RULE_CHARS = 1500
MAX_TOTAL_CHARS = 4000
SCOPES = ("project", "global")


def project_rules_dir(workspace: Path) -> Path:
    return workspace / ".eagent" / "rules"


def global_rules_dir() -> Path:
    base = os.environ.get("EAGENT_HOME") or os.path.join(os.path.expanduser("~"), ".eagent")
    return Path(base) / "rules"


def rules_dir(workspace: Path, scope: str) -> Path:
    return global_rules_dir() if scope == "global" else project_rules_dir(workspace)


def _parse(text: str) -> tuple[dict, str]:
    """解析极简 front matter：--- 之间的 key: value。"""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            header = text[3:end]
            body = text[end + 4 :]
            for line in header.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip()
    return meta, body.lstrip("\n")


def _description(meta: dict, body: str) -> str:
    if meta.get("description"):
        return meta["description"]
    for line in body.strip().splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:80]
    return ""


def _check_scope(scope: str) -> str:
    value = str(scope or "project").lower()
    if value not in SCOPES:
        raise ValueError(f"未知的规则作用域: {scope}")
    return value


def _check_id(rule_id: str) -> str:
    if not rule_id or "/" in rule_id or "\\" in rule_id or ".." in rule_id:
        raise ValueError("规则 ID 非法")
    return rule_id


def list_rules(workspace: Path) -> list[dict]:
    items: list[dict] = []
    for scope in SCOPES:
        base = rules_dir(workspace, scope)
        if not base.is_dir():
            continue
        for file in sorted(base.glob("*.md")):
            try:
                meta, body = _parse(file.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            items.append(
                {
                    "id": file.stem,
                    "name": meta.get("name") or file.stem,
                    "description": _description(meta, body),
                    "scope": scope,
                    "key": f"{scope}:{file.stem}",
                }
            )
    return items


def load_rule(workspace: Path, rule_id: str, scope: str = "project") -> dict:
    scope = _check_scope(scope)
    _check_id(rule_id)
    path = rules_dir(workspace, scope) / f"{rule_id}.md"
    if not path.is_file():
        raise ValueError(f"规则不存在: {rule_id}")
    meta, body = _parse(path.read_text(encoding="utf-8", errors="replace"))
    return {
        "id": rule_id,
        "name": meta.get("name") or rule_id,
        "description": _description(meta, body),
        "scope": scope,
        "key": f"{scope}:{rule_id}",
        "content": body.strip()[:MAX_RULE_CHARS],
    }


def _slug(workspace: Path, scope: str, name: str, current_id: str | None = None) -> str:
    base = re.sub(r'[\\/:*?"<>|]+', "-", name).strip().strip(".")
    base = base[:60]
    if not base.strip("-"):
        base = f"rule-{int(time.time())}"
    candidate = base
    index = 2
    base_dir = rules_dir(workspace, scope)
    while candidate != current_id and (base_dir / f"{candidate}.md").exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def save_rule(
    workspace: Path,
    name: str,
    description: str,
    content: str,
    rule_id: str | None = None,
    scope: str = "project",
) -> str:
    """新建或更新规则，返回规则 ID。"""
    scope = _check_scope(scope)
    if not name.strip():
        raise ValueError("规则名称不能为空")
    if not content.strip():
        raise ValueError("规则内容不能为空")

    base_dir = rules_dir(workspace, scope)
    base_dir.mkdir(parents=True, exist_ok=True)

    if rule_id:
        _check_id(rule_id)
        target = base_dir / f"{rule_id}.md"
        if not target.is_file():
            raise ValueError(f"规则不存在: {rule_id}")
        resolved = rule_id
    else:
        resolved = _slug(workspace, scope, name)
        target = base_dir / f"{resolved}.md"

    text = (
        "---\n"
        f"name: {name.strip()}\n"
        f"description: {description.strip()}\n"
        "---\n\n"
        f"{content.strip()}\n"
    )
    target.write_text(text, encoding="utf-8", newline="")
    return resolved


def delete_rule(workspace: Path, rule_id: str, scope: str = "project") -> None:
    scope = _check_scope(scope)
    _check_id(rule_id)
    path = rules_dir(workspace, scope) / f"{rule_id}.md"
    if not path.is_file():
        raise ValueError(f"规则不存在: {rule_id}")
    path.unlink()


def build_rules(workspace: Path, enabled_keys: list[str] | None = None) -> str:
    """把启用的规则拼成提示词片段；enabled_keys 为 None 时表示全部启用。"""
    items = list_rules(workspace)
    allowed = set(enabled_keys) if enabled_keys is not None else None
    parts: list[str] = []
    used = 0
    for item in items:
        if allowed is not None and item["key"] not in allowed:
            continue
        try:
            rule = load_rule(workspace, item["id"], item["scope"])
        except (ValueError, OSError):
            continue
        label = "项目规则" if item["scope"] == "project" else "全局规则"
        block = f"【{label}：{rule['name']}】\n{rule['content']}"
        if used + len(block) > MAX_TOTAL_CHARS:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
