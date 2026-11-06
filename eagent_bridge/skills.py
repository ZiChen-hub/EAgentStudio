"""技能系统：读取 .eagent/skills 下的 Markdown 指令包。"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

MAX_SKILL_CHARS = 1500
MAX_TOTAL_CHARS = 4000


def skills_dir(workspace: Path) -> Path:
    return workspace / ".eagent" / "skills"


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


def _candidates(workspace: Path, skill_id: str) -> list[Path]:
    base = skills_dir(workspace)
    return [base / f"{skill_id}.md", base / skill_id / "SKILL.md"]


def _check_id(skill_id: str) -> str:
    if not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id:
        raise ValueError("技能 ID 非法")
    return skill_id


def _description(meta: dict, body: str) -> str:
    if meta.get("description"):
        return meta["description"]
    lines = body.strip().splitlines()
    return lines[0][:80] if lines else ""


def list_skills(workspace: Path) -> list[dict]:
    base = skills_dir(workspace)
    if not base.is_dir():
        return []
    items = []
    for file in sorted(base.glob("*.md")):
        try:
            meta, body = _parse(file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            # 文件被删除或没有权限时跳过，别让整个技能面板报错
            continue
        items.append(
            {
                "id": file.stem,
                "name": meta.get("name") or file.stem,
                "description": _description(meta, body),
            }
        )
    try:
        folders = sorted(base.iterdir())
    except OSError:
        return items
    for folder in folders:
        if not folder.is_dir():
            continue
        skill_file = folder / "SKILL.md"
        try:
            if not skill_file.is_file():
                continue
            meta, body = _parse(skill_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        items.append(
            {
                "id": folder.name,
                "name": meta.get("name") or folder.name,
                "description": _description(meta, body),
            }
        )
    return items


def load_skill(workspace: Path, skill_id: str) -> tuple[str, str]:
    _check_id(skill_id)
    for path in _candidates(workspace, skill_id):
        if path.is_file():
            meta, body = _parse(path.read_text(encoding="utf-8", errors="replace"))
            name = meta.get("name") or path.stem
            return name, body.strip()[:MAX_SKILL_CHARS]
    raise ValueError(f"技能不存在: {skill_id}")


def build_instructions(workspace: Path, skill_ids: list[str]) -> str:
    """把启用的技能拼成额外规则，总量有上限，防止撑爆上下文。"""
    parts = []
    used = 0
    for skill_id in skill_ids or []:
        try:
            name, body = load_skill(workspace, skill_id)
        except (ValueError, OSError):
            continue
        block = f"【技能：{name}】\n{body}"
        if used + len(block) > MAX_TOTAL_CHARS:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def _slug(workspace: Path, name: str, current_id: str | None = None) -> str:
    base = re.sub(r'[\\/:*?"<>|]+', "-", name).strip().strip(".")
    base = base or f"skill-{int(time.time())}"
    candidate = base
    index = 2
    base_dir = skills_dir(workspace)
    while candidate != current_id and (
        (base_dir / f"{candidate}.md").exists() or (base_dir / candidate).is_dir()
    ):
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def save_skill(
    workspace: Path,
    name: str,
    description: str,
    instructions: str,
    skill_id: str | None = None,
) -> str:
    """新建或更新技能，返回技能 ID。"""
    if not name.strip():
        raise ValueError("技能名称不能为空")
    if not instructions.strip():
        raise ValueError("技能指令不能为空")

    base_dir = skills_dir(workspace)
    base_dir.mkdir(parents=True, exist_ok=True)

    target: Path | None = None
    if skill_id:
        _check_id(skill_id)
        for path in _candidates(workspace, skill_id):
            if path.is_file():
                target = path
                break
        if target is None:
            raise ValueError(f"技能不存在: {skill_id}")
        resolved_id = skill_id
    else:
        resolved_id = _slug(workspace, name)
        target = base_dir / f"{resolved_id}.md"

    text = (
        "---\n"
        f"name: {name.strip()}\n"
        f"description: {description.strip()}\n"
        "---\n\n"
        f"{instructions.strip()}\n"
    )
    target.write_text(text, encoding="utf-8", newline="")
    return resolved_id


def delete_skill(workspace: Path, skill_id: str) -> None:
    _check_id(skill_id)
    for path in _candidates(workspace, skill_id):
        if path.is_file():
            if path.parent.name == skill_id:  # <id>/SKILL.md
                shutil.rmtree(path.parent, ignore_errors=True)
            else:
                path.unlink()
            return
    raise ValueError(f"技能不存在: {skill_id}")
