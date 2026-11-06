"""Git 集成：状态、暂存、提交、差异预览。

只调用固定参数形式（列表传参），不拼 shell 字符串，避免命令注入。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(workspace: Path, args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise GitError("没有找到 git，请先安装 Git for Windows") from error
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git 命令超时：{' '.join(args)}") from error
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _ensure_repo(workspace: Path) -> None:
    code, out, _ = _run(workspace, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out.strip() != "true":
        raise GitError("当前工作目录不是 Git 仓库")


def is_repo(workspace: Path) -> bool:
    try:
        _ensure_repo(workspace)
        return True
    except GitError:
        return False


def init(workspace: Path) -> str:
    code, out, err = _run(workspace, ["init"])
    if code != 0:
        raise GitError(err.strip() or "初始化仓库失败")
    return out.strip()


def _parse_status(text: str) -> tuple[str, list[dict]]:
    branch = ""
    changes: list[dict] = []
    for line in text.splitlines():
        if line.startswith("##"):
            header = line[2:].strip()
            if header.startswith("No commits yet on "):
                header = header[len("No commits yet on "):]
            branch = header.split("...")[0].split(" ")[0] or header
            continue
        if len(line) < 3:
            continue
        code = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        staged = code[0] not in (" ", "?")
        untracked = code == "??"
        if untracked:
            kind = "untracked"
        elif "D" in code:
            kind = "deleted"
        elif "A" in code:
            kind = "added"
        elif "R" in code:
            kind = "renamed"
        else:
            kind = "modified"
        changes.append({"path": path, "code": code, "staged": staged, "kind": kind})
    return branch, changes


def status(workspace: Path) -> dict:
    if not is_repo(workspace):
        return {"repo": False, "branch": "", "changes": []}
    code, out, err = _run(workspace, ["status", "--porcelain=v1", "--branch"])
    if code != 0:
        raise GitError(err.strip() or "git status 失败")
    branch, changes = _parse_status(out)
    return {"repo": True, "branch": branch, "changes": changes}


def stage(workspace: Path, paths: list[str]) -> None:
    _ensure_repo(workspace)
    if not paths:
        raise GitError("没有要暂存的文件")
    code, _, err = _run(workspace, ["add", "--", *paths])
    if code != 0:
        raise GitError(err.strip() or "暂存失败")


def unstage(workspace: Path, paths: list[str]) -> None:
    _ensure_repo(workspace)
    if not paths:
        raise GitError("没有要取消暂存的文件")
    code, _, err = _run(workspace, ["restore", "--staged", "--", *paths])
    if code != 0:
        # 老版本 git 没有 restore，退回 reset
        code, _, err = _run(workspace, ["reset", "HEAD", "--", *paths])
        if code != 0:
            raise GitError(err.strip() or "取消暂存失败")


def _identity_args(workspace: Path) -> list[str]:
    """没配置 user.name / user.email 时给一次性的兜底身份，避免提交失败。"""
    args: list[str] = []
    code, out, _ = _run(workspace, ["config", "user.name"])
    if code != 0 or not out.strip():
        args += ["-c", "user.name=EAgent"]
    code, out, _ = _run(workspace, ["config", "user.email"])
    if code != 0 or not out.strip():
        args += ["-c", "user.email=eagent@localhost"]
    return args


def commit(workspace: Path, message: str) -> str:
    _ensure_repo(workspace)
    text = (message or "").strip()
    if not text:
        raise GitError("提交信息不能为空")
    code, out, err = _run(workspace, [*_identity_args(workspace), "commit", "-m", text], timeout=60.0)
    if code != 0:
        raise GitError(err.strip() or out.strip() or "提交失败")
    return out.strip()


def _show(workspace: Path, spec: str) -> str:
    code, out, _ = _run(workspace, ["show", spec])
    return out if code == 0 else ""


def diff_text(workspace: Path, max_chars: int = 6000) -> str:
    """取待提交改动的文本 diff：优先暂存区，没有再看工作区。"""
    _ensure_repo(workspace)
    code, out, _ = _run(workspace, ["diff", "--cached", "--no-color"])
    if code != 0 or not out.strip():
        code, out, _ = _run(workspace, ["diff", "--no-color"])
    text = out.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...（diff 过长，已截断）"
    return text


def untracked_files(workspace: Path) -> list[str]:
    _ensure_repo(workspace)
    code, out, _ = _run(workspace, ["ls-files", "--others", "--exclude-standard"])
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()][:50]


def diff_pair(workspace: Path, path: str, staged: bool = False) -> dict:
    """返回可用于并排 diff 的 old / new 文本。"""
    _ensure_repo(workspace)
    if not path or ".." in path or Path(path).is_absolute():
        raise GitError("文件路径非法")
    root = workspace.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise GitError("文件路径非法")
    head = _show(workspace, f"HEAD:{path}")
    index = _show(workspace, f":{path}")
    try:
        working = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except OSError:
        working = ""
    if staged:
        return {"path": path, "old": head, "new": index, "staged": True}
    return {"path": path, "old": index or head, "new": working, "staged": False}
