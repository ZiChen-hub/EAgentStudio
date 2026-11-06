"""任务快照：Agent 每次改文件前备份原内容，任务结束后可一键回滚。

快照放在 <workspace>/.eagent/backups/<run_id>/：
    snapshot.json   元数据（任务、时间、文件清单）
    files/<相对路径> 改动前的原文件
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

MAX_SNAPSHOTS = 20
# 超大文件只记录路径，不复制内容，避免快照目录被撑爆
MAX_FILE_BYTES = 5 * 1024 * 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _root(workspace: Path) -> Path:
    return workspace / ".eagent" / "backups"


def _check_id(run_id: str) -> str:
    value = str(run_id or "")
    if not _RUN_ID_RE.match(value):
        raise ValueError("快照 ID 非法")
    return value


def _dir(workspace: Path, run_id: str) -> Path:
    return _root(workspace) / _check_id(run_id)


def _manifest_path(workspace: Path, run_id: str) -> Path:
    return _dir(workspace, run_id) / "snapshot.json"


def _read_manifest(workspace: Path, run_id: str) -> dict | None:
    path = _manifest_path(workspace, run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_manifest(workspace: Path, run_id: str, data: dict) -> None:
    path = _manifest_path(workspace, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="")


def begin(workspace: Path, run_id: str, task: str = "") -> None:
    """任务开始时建立快照骨架。"""
    try:
        data = {
            "id": _check_id(run_id),
            "task": (task or "").strip()[:300],
            "created": time.time(),
            "finished": None,
            "summary": "",
            "files": [],
            "restored": None,
        }
        _write_manifest(workspace, run_id, data)
    except (OSError, ValueError):
        pass


def _relative_key(workspace: Path, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        return None
    key = rel.as_posix()
    if not key or key.startswith(".eagent/"):
        return None
    return key


def backup(workspace: Path, run_id: str, path: Path) -> None:
    """把文件当前内容存进快照；同一文件同一任务只记第一次。"""
    if not run_id:
        return
    key = _relative_key(workspace, path)
    if key is None:
        return
    data = _read_manifest(workspace, run_id)
    if data is None:
        begin(workspace, run_id)
        data = _read_manifest(workspace, run_id)
        if data is None:
            return
    files = data.setdefault("files", [])
    if not isinstance(files, list):
        files = []
        data["files"] = files
    if any(item.get("path") == key for item in files if isinstance(item, dict)):
        return

    existed = path.is_file()
    skipped = False
    if existed:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped = True
            else:
                target = _dir(workspace, run_id) / "files" / key
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        except OSError:
            return
    entry = {"path": key, "existed": existed}
    if skipped:
        entry["skipped"] = True
    files.append(entry)
    try:
        _write_manifest(workspace, run_id, data)
    except OSError:
        pass


def finish(workspace: Path, run_id: str, summary: str = "") -> None:
    """任务结束：写入摘要；没有改过文件就清掉空快照。"""
    data = _read_manifest(workspace, run_id)
    if data is None:
        return
    if not data.get("files"):
        shutil.rmtree(_dir(workspace, run_id), ignore_errors=True)
        return
    data["finished"] = time.time()
    data["summary"] = (summary or "").strip()[:300]
    try:
        _write_manifest(workspace, run_id, data)
    except OSError:
        return
    _prune(workspace)


def _prune(workspace: Path) -> None:
    items = list_snapshots(workspace)
    for item in items[MAX_SNAPSHOTS:]:
        shutil.rmtree(_dir(workspace, item["id"]), ignore_errors=True)


def list_snapshots(workspace: Path) -> list[dict]:
    base = _root(workspace)
    if not base.is_dir():
        return []
    items: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        try:
            data = _read_manifest(workspace, entry.name)
        except (OSError, ValueError):
            # 目录名不是合法快照 ID 时跳过，别让整个面板报错
            continue
        if not data:
            continue
        raw_files = data.get("files")
        files = (
            [item for item in raw_files if isinstance(item, dict)]
            if isinstance(raw_files, list)
            else []
        )
        try:
            created = float(data.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        items.append(
            {
                "id": str(data.get("id") or entry.name),
                "task": str(data.get("task") or ""),
                "summary": str(data.get("summary") or ""),
                "created": created,
                "finished": data.get("finished"),
                "restored": data.get("restored"),
                "count": len(files),
                "skipped": sum(1 for item in files if item.get("skipped")),
                "files": [str(item.get("path")) for item in files][:50],
            }
        )
    items.sort(key=lambda item: item["created"], reverse=True)
    return items


def _safe_target(workspace: Path, key: str) -> Path | None:
    """快照清单里的路径不可信，统一挡掉绝对路径和 .. 跳跃。"""
    candidate = Path(key)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    target = (workspace / candidate).resolve()
    if target != workspace and workspace not in target.parents:
        return None
    return target


def restore(workspace: Path, run_id: str) -> dict:
    """把快照里的文件还原到工作区，返回还原详情。"""
    data = _read_manifest(workspace, run_id)
    if data is None:
        raise ValueError("快照不存在")
    raw_files = data.get("files")
    files = (
        [item for item in raw_files if isinstance(item, dict)]
        if isinstance(raw_files, list)
        else []
    )
    if not files:
        raise ValueError("这个快照没有可还原的文件")

    restored: list[str] = []
    removed: list[str] = []
    missing: list[str] = []
    skipped: list[str] = []
    for item in reversed(files):
        key = str(item.get("path") or "")
        if not key:
            continue
        target = _safe_target(workspace, key)
        if target is None:
            missing.append(key)
            continue
        if item.get("skipped"):
            skipped.append(key)
            continue
        if item.get("existed"):
            source = _safe_target(_dir(workspace, run_id) / "files", key)
            if source is None or not source.is_file():
                missing.append(key)
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                restored.append(key)
            except OSError:
                missing.append(key)
        else:
            try:
                if target.exists():
                    target.unlink()
                    removed.append(key)
            except OSError:
                missing.append(key)

    data["restored"] = time.time()
    try:
        _write_manifest(workspace, run_id, data)
    except OSError:
        pass
    return {"restored": restored, "removed": removed, "missing": missing, "skipped": skipped}
