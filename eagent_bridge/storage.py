"""本地运行数据清理：会话、截图、上传图片只保留最近若干份。"""

from __future__ import annotations

from pathlib import Path

DEFAULT_KEEP = 50


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
