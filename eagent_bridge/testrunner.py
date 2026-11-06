"""测试运行器：跑一次测试命令，并把失败用例解析成可点击的条目。"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

MAX_OUTPUT = 20000
TIMEOUT = 180

_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", re.M)
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")
_TB_LINE = re.compile(r'File "([^"]+)", line (\d+)')
_AT_LINE = re.compile(r"\(([^()\s]+):(\d+):\d+\)")


def detect_command(workspace: Path) -> str:
    package = workspace / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and str(scripts.get("test") or "").strip():
            return "npm test"
    return "python -m pytest -q --tb=short"


def _rel(workspace: Path, text: str) -> str:
    cleaned = text.strip().strip("\"'")
    try:
        return str(Path(cleaned).resolve().relative_to(workspace.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return cleaned.replace("\\", "/")


def _line_for(workspace: Path, output: str, file: str, test: str) -> int:
    """在 traceback 里找该用例的文件行号。"""
    if file:
        pattern = re.compile(re.escape(file) + r":(\d+):")
        if test:
            named = re.compile(re.escape(file) + r":(\d+): in " + re.escape(test))
            match = named.search(output)
            if match:
                return int(match.group(1))
        match = pattern.search(output)
        if match:
            return int(match.group(1))
    for pattern in (_TB_LINE, _AT_LINE):
        match = pattern.search(output)
        if match:
            candidate = _rel(workspace, match.group(1))
            if file and Path(candidate).name != Path(file).name:
                continue
            return int(match.group(2))
    return 1


def _parse_pytest(workspace: Path, output: str) -> list[dict]:
    failures = []
    for match in _PYTEST_FAILED.finditer(output):
        node = match.group(1)
        message = (match.group(2) or "").strip()
        parts = node.split("::")
        file = parts[0]
        test = parts[-1] if len(parts) > 1 else Path(file).stem
        failures.append(
            {
                "file": _rel(workspace, file),
                "line": _line_for(workspace, output, file, test),
                "test": test,
                "message": message[:300],
            }
        )
    if failures:
        return failures

    # 没跑到用例就报错（收集期错误），退回通用 traceback 解析
    match = _TB_LINE.search(output)
    if match:
        return [
            {
                "file": _rel(workspace, match.group(1)),
                "line": int(match.group(2)),
                "test": "收集或导入失败",
                "message": output.strip().splitlines()[-1][:300] if output.strip() else "",
            }
        ]
    return []


def _parse_generic(workspace: Path, output: str) -> list[dict]:
    seen: set[tuple[str, int]] = set()
    failures = []
    for pattern in (_AT_LINE, _TB_LINE):
        for match in pattern.finditer(output):
            path = _rel(workspace, match.group(1))
            line = int(match.group(2))
            key = (path, line)
            if key in seen:
                continue
            seen.add(key)
            failures.append({"file": path, "line": line, "test": "", "message": ""})
            if len(failures) >= 20:
                break
        if failures:
            break
    return failures


def _counts(output: str) -> tuple[int, int]:
    passed = failed = 0
    for value, kind in _PYTEST_COUNT.findall(output):
        if kind == "passed":
            passed = int(value)
        elif kind in ("failed", "error", "errors"):
            failed += int(value)
    return passed, failed


def run(workspace: Path, command: str | None = None) -> dict:
    cmd = (command or "").strip() or detect_command(workspace)
    started = time.time()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(workspace),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
        code = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired:
        code = -1
        output = f"测试超时（{TIMEOUT} 秒）"
    duration = round(time.time() - started, 1)
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n...（输出过长，已截断）"

    failures = _parse_pytest(workspace, output) if "pytest" in output or "FAILED" in output else []
    if not failures and code != 0:
        failures = _parse_generic(workspace, output)
    passed, failed = _counts(output)
    return {
        "command": cmd,
        "exit_code": code,
        "duration": duration,
        "passed": passed,
        "failed": failed or len(failures),
        "failures": failures,
        "output": output,
    }
