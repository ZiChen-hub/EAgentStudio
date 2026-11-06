"""本地运行数据清理：会话、截图、上传图片只保留最近若干份。"""

from __future__ import annotations

from pathlib import Path

DEFAULT_KEEP = 50


def ensure_data_dir(workspace: Path) -> Path:
    """确保 .eagent 存在，并用内部 .gitignore 挡住运行数据。

    不修改用户根目录的 .gitignore；目录内的忽略文件只影响 .eagent 自身，
    这样日志、会话、快照不会出现在 git status 里，也不会被误提交。
    """
    folder = workspace / ".eagent"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        ignore = folder / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    except OSError:
        pass
    return folder


def prune(folder: Path, keep: int = DEFAULT_KEEP, pattern: str = "*") -> int:
    """删除目录里最旧的多余文件，返回实际删除数量。"""
    if keep < 0:
        return 0
    try:
        if not folder.is_dir():
            return 0
        items = [item for item in folder.glob(pattern) if item.is_file()]
        items.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for item in items[keep:]:
        try:
            item.unlink()
            removed += 1
        except OSError:
            continue
    return removed
