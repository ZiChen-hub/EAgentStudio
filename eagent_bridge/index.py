"""代码索引：按行分块 + 关键词倒排，供 search_code 做相关度检索。

索引只存在进程内存里：首次搜索时构建，之后每次按 mtime / size 增量更新，
这样 Agent 改完文件立刻就能搜到新内容，也不需要往工作目录写缓存文件。
"""

from __future__ import annotations

import ast
import functools
import os
import re
import threading
import time
from pathlib import Path

SKIP_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "dist", "build", ".eagent", ".next", "target",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".less", ".json", ".md", ".txt",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".cs", ".php", ".rb", ".sh", ".bash", ".ps1", ".bat", ".sql", ".lua",
}
MAX_FILE_BYTES = 256_000
MAX_FILES = 2000
CHUNK_LINES = 40
PREVIEW_LINES = 8
PREVIEW_WIDTH = 140

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]+")

# 非 Python 语言只做轻量正则提取，覆盖常见定义写法
_SYMBOL_PATTERNS = (
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)"), "function"),
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*(?:public|private|protected|internal)\s+[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\("), "method"),
)
_REGEX_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".go", ".rs", ".java", ".cs", ".kt"}

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.RLock()


def _locked(func):
    """索引缓存在多线程里读写，统一串行化；RLock 允许内部函数重入。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _CACHE_LOCK:
            return func(*args, **kwargs)

    return wrapper


def _tokens(text: str) -> set[str]:
    """英文按标识符切，中文按 2-gram 切，保证中文短语也能命中。"""
    result: set[str] = set()
    for match in _TOKEN_RE.finditer(text):
        word = match.group(0)
        if word.isascii():
            result.add(word.lower())
            continue
        if len(word) <= 2:
            result.add(word)
        for index in range(len(word) - 1):
            result.add(word[index : index + 2])
    return result


def _scan(workspace: Path) -> list[tuple[str, Path, float, int]]:
    found: list[tuple[str, Path, float, int]] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not name.startswith("."))
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_FILE_BYTES or stat.st_size == 0:
                continue
            try:
                rel = str(path.relative_to(workspace)).replace("\\", "/")
            except ValueError:
                continue
            found.append((rel, path, stat.st_mtime, stat.st_size))
            if len(found) >= MAX_FILES:
                return found
    return found


def _python_symbols(path: Path, content: str) -> list[dict]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    symbols: list[dict] = []

    def walk(node, prefix: str) -> None:
        for child in getattr(node, "body", []) or []:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    {
                        "name": prefix + child.name,
                        "kind": "method" if prefix else "function",
                        "line": child.lineno,
                        "end": getattr(child, "end_lineno", child.lineno) or child.lineno,
                    }
                )
                walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                symbols.append(
                    {
                        "name": prefix + child.name,
                        "kind": "class",
                        "line": child.lineno,
                        "end": getattr(child, "end_lineno", child.lineno) or child.lineno,
                    }
                )
                walk(child, prefix + child.name + ".")

    walk(tree, "")
    return symbols


def _regex_symbols(path: Path, lines: list[str]) -> list[dict]:
    if path.suffix.lower() not in _REGEX_SUFFIXES:
        return []
    symbols: list[dict] = []
    for number, line in enumerate(lines, start=1):
        for pattern, kind in _SYMBOL_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            if name in ("if", "for", "while", "switch", "catch", "return", "new"):
                break
            symbols.append({"name": name, "kind": kind, "line": number, "end": number})
            break
    return symbols


def _index_file(path: Path) -> dict | None:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    lines = content.splitlines()
    chunks = []
    for start in range(0, len(lines), CHUNK_LINES):
        block = lines[start : start + CHUNK_LINES]
        tokens = _tokens("\n".join(block))
        if not tokens:
            continue
        chunks.append(
            {
                "start": start + 1,
                "end": min(start + CHUNK_LINES, len(lines)),
                "tokens": tokens,
            }
        )
    if not chunks:
        return None
    symbols = _python_symbols(path, content) if path.suffix.lower() == ".py" else _regex_symbols(path, lines)
    return {"chunks": chunks, "symbols": symbols}


def _build_postings(files: dict[str, dict]) -> dict[str, list[tuple[str, int]]]:
    postings: dict[str, list[tuple[str, int]]] = {}
    for rel, info in files.items():
        for index, chunk in enumerate(info["chunks"]):
            for token in chunk["tokens"]:
                postings.setdefault(token, []).append((rel, index))
    return postings


def _ensure(workspace: Path) -> dict:
    key = str(workspace)
    cache = _CACHE.get(key)
    if cache is None:
        cache = {"files": {}, "postings": {}, "built_at": 0.0, "scanned": 0}
        _CACHE[key] = cache

    scanned = _scan(workspace)
    cache["scanned"] = len(scanned)
    current: dict[str, dict] = {}
    changed = False
    for rel, path, mtime, size in scanned:
        cached = cache["files"].get(rel)
        if cached and cached.get("mtime") == mtime and cached.get("size") == size:
            current[rel] = cached
            continue
        indexed = _index_file(path)
        if indexed is None:
            continue
        indexed["mtime"] = mtime
        indexed["size"] = size
        current[rel] = indexed
        changed = True

    if changed or len(current) != len(cache["files"]):
        cache["files"] = current
        cache["postings"] = _build_postings(current)
        cache["built_at"] = time.time()
    return cache


def _preview(path: Path, start: int, end: int) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    lines = content.splitlines()
    picked = lines[start - 1 : min(end, start - 1 + PREVIEW_LINES)]
    return [f"{start + offset:>5}| {line[:PREVIEW_WIDTH]}" for offset, line in enumerate(picked)]


@_locked
def search(
    workspace: Path,
    query: str,
    max_results: int = 8,
    glob: str | None = None,
) -> list[dict]:
    """按相关度返回最相关的代码块。

    打分用 IDF 加权而不是简单计数：常见词（def、self、return）权重自然变低，
    稀有标识符权重变高；再叠加文件名命中与符号名命中。
    """
    import fnmatch
    import math

    query_tokens = _tokens(query or "")
    if not query_tokens:
        return []

    cache = _ensure(workspace)
    files = cache["files"]
    postings = cache["postings"]
    total_chunks = sum(len(info["chunks"]) for info in files.values()) or 1

    scores: dict[tuple[str, int], float] = {}
    coverage: dict[tuple[str, int], set[str]] = {}
    for token in query_tokens:
        hits = postings.get(token, [])
        if not hits:
            continue
        idf = math.log(1 + total_chunks / (1 + len(hits)))
        for rel, chunk_index in hits:
            key = (rel, chunk_index)
            scores[key] = scores.get(key, 0.0) + idf
            coverage.setdefault(key, set()).add(token)

    # 路径/文件名命中的文件整体加权，通常比正文命中更相关
    for rel in files:
        name = rel.lower()
        if glob and not fnmatch.fnmatch(rel, glob):
            continue
        hits = sum(1 for token in query_tokens if token in name)
        if not hits:
            continue
        for index in range(len(files[rel]["chunks"])):
            key = (rel, index)
            scores[key] = scores.get(key, 0.0) + 0.9 * hits

    # 查询命中符号名时，给定义所在的块加分
    for rel, info in files.items():
        for symbol in info.get("symbols") or []:
            lowered = symbol["name"].lower()
            short = lowered.rsplit(".", 1)[-1]
            if not any(token in (lowered, short) for token in query_tokens):
                continue
            for index, chunk in enumerate(info["chunks"]):
                if chunk["start"] <= symbol["line"] <= chunk["end"]:
                    key = (rel, index)
                    scores[key] = scores.get(key, 0.0) + 1.5
                    break

    # 命中更多查询词的块优先
    for key, tokens in coverage.items():
        if len(tokens) > 1:
            scores[key] *= 1 + 0.4 * (len(tokens) - 1) / len(query_tokens)

    if glob:
        scores = {
            key: value for key, value in scores.items()
            if fnmatch.fnmatch(key[0], glob) or fnmatch.fnmatch(Path(key[0]).name, glob)
        }

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    results: list[dict] = []
    used_lines: dict[str, int] = {}
    for (rel, chunk_index), score in ordered:
        if len(results) >= max_results:
            break
        chunks = files[rel]["chunks"]
        if chunk_index >= len(chunks):
            continue
        chunk = chunks[chunk_index]
        # 同一文件相邻块不重复输出
        if used_lines.get(rel) is not None and chunk["start"] <= used_lines[rel] + CHUNK_LINES:
            continue
        used_lines[rel] = chunk["end"]
        results.append(
            {
                "path": rel,
                "start": chunk["start"],
                "end": chunk["end"],
                "score": round(score, 1),
            }
        )
    return results


@_locked
def symbols(workspace: Path, query: str = "", limit: int = 40) -> list[dict]:
    """按名称模糊匹配符号，前缀命中优先。"""
    cache = _ensure(workspace)
    needle = (query or "").strip().lower()
    items: list[dict] = []
    for rel, info in cache["files"].items():
        for symbol in info.get("symbols") or []:
            name = symbol["name"]
            if needle and needle not in name.lower() and needle not in rel.lower():
                continue
            items.append(
                {
                    "name": name,
                    "kind": symbol["kind"],
                    "path": rel,
                    "line": symbol["line"],
                    "end": symbol["end"],
                }
            )
    if needle:
        items.sort(
            key=lambda item: (
                0 if item["name"].lower().startswith(needle) else 1,
                len(item["name"]),
                item["name"],
            )
        )
    else:
        items.sort(key=lambda item: (item["path"], item["line"]))
    return items[: max(1, min(limit, 200))]


def symbol_source(workspace: Path, path: str, line: int, end: int | None = None) -> str:
    """取符号定义处的源码，带行号，最多 80 行。"""
    try:
        root = workspace.resolve()
        target = (root / path).resolve()
    except (OSError, ValueError):
        return ""
    if target != root and root not in target.parents:
        return ""
    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    start = max(1, int(line))
    stop = int(end) if end and int(end) >= start else start
    stop = min(stop, start + 79, len(lines))
    picked = lines[start - 1 : stop]
    return "\n".join(f"{start + offset:>5}| {text[:120]}" for offset, text in enumerate(picked))


@_locked
def definition(workspace: Path, name: str, path: str | None = None, limit: int = 5) -> list[dict]:
    """按符号名找定义位置；同名时优先当前文件，其次按名称精确匹配。"""
    needle = (name or "").strip()
    if not needle:
        return []
    lowered = needle.lower()
    short = lowered.rsplit(".", 1)[-1]
    items = symbols(workspace, needle, 200)
    picked = [
        item
        for item in items
        if item["name"].lower() == lowered or item["name"].lower().rsplit(".", 1)[-1] == short
    ]
    if not picked:
        picked = items
    if path:
        current = path.replace("\\", "/").lstrip("/")
        picked.sort(key=lambda item: 0 if item["path"] == current else 1)
    return picked[: max(1, limit)]


@_locked
def references(workspace: Path, name: str, limit: int = 50) -> list[dict]:
    """按单词边界查找符号出现的位置，返回 文件 / 行号 / 该行内容。"""
    needle = (name or "").strip().rsplit(".", 1)[-1]
    if not needle:
        return []
    pattern = re.compile(r"(?<![\w$])" + re.escape(needle) + r"(?![\w$])")
    cache = _ensure(workspace)
    results: list[dict] = []
    for rel in sorted(cache["files"]):
        try:
            lines = (workspace / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if not pattern.search(line):
                continue
            results.append({"path": rel, "line": number, "text": line.strip()[:160]})
            if len(results) >= limit:
                return results
    return results


def format_results(workspace: Path, results: list[dict]) -> str:
    if not results:
        return "没有找到相关代码，可以改用 search_text 做精确匹配"
    blocks = []
    for item in results:
        path = workspace / item["path"]
        lines = _preview(path, item["start"], item["end"])
        header = f"{item['path']}:{item['start']}-{item['end']}（相关度 {item['score']}）"
        blocks.append(header + ("\n" + "\n".join(lines) if lines else ""))
    return "\n\n".join(blocks)


def search_code(workspace: Path, args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    limit = min(int(args.get("max_results") or 8), 20)
    glob = args.get("glob") or None
    results = search(workspace, query, max_results=limit, glob=glob)
    return format_results(workspace, results)


@_locked
def status(workspace: Path) -> dict:
    cache = _ensure(workspace)
    chunks = sum(len(info["chunks"]) for info in cache["files"].values())
    return {
        "files": len(cache["files"]),
        "chunks": chunks,
        "tokens": len(cache["postings"]),
        "scanned": cache["scanned"],
        "built_at": cache["built_at"],
    }


@_locked
def reset(workspace: Path | None = None) -> None:
    if workspace is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(workspace), None)
