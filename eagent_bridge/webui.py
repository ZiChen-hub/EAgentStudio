"""迷你 Trae：本地 Web UI + Monaco 编辑器 + 网页 AI Agent。

启动：
    python -m eagent_bridge.webui

默认地址 http://127.0.0.1:8766，会自动用 Edge 应用窗口打开。
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .adapters import AdapterError
from .backends import BackendError, build_backend
from .cdp import CDPError, find_edge
from .git_ops import (
    GitError,
    commit as git_commit,
    diff_pair as git_diff_pair,
    diff_text as git_diff_text,
    init as git_init,
    stage as git_stage,
    status as git_status,
    untracked_files as git_untracked,
    unstage as git_unstage,
)
from .index import (
    definition as find_definition,
    references as find_references,
    reset as reset_index,
    symbol_source,
    symbols,
)
from .gui_tools import execute_gui
from .memory import (
    append as memory_append,
    build_memory,
    load as load_memory,
    save as save_memory_content,
)
from .mcp_templates import install as install_mcp_template, list_templates as list_mcp_templates
from .runtime import AgentRuntime, StopRequested
from .rules import build_rules, delete_rule, list_rules, load_rule, save_rule
from .session import Session
from .snapshots import begin as snapshot_begin, finish as snapshot_finish
from .snapshots import list_snapshots, restore as snapshot_restore
from .skills import build_instructions, delete_skill, list_skills, load_skill, save_skill
from .proc import kill_tree
from .storage import prune as prune_folder
from .testrunner import detect_command as detect_test_command
from .testrunner import run as run_tests
from .tools import SKIP_DIRS, ToolError, preview_action, safe_path

UI_DIR = Path(__file__).parent / "ui"
DEFAULT_PORT = int(os.environ.get("EAGENT_BRIDGE_WEBUI_PORT", "8766"))
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
LOG_MAX_BYTES = 2 * 1024 * 1024
_TOKEN_IN_LOG = re.compile(r"(token=)[^&\s\"]+")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _append_log(workspace: Path, line: str) -> None:
    """把服务端日志落到工作目录，超过 2MB 轮转一次，方便事后排查。"""
    path = workspace / ".eagent" / "logs" / "app.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name("app.log.1"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")
    except OSError:
        pass



class RunState:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.task = ""
        self.events: list[dict] = []
        self.cv = threading.Condition()
        self.done = False
        self.cancelled = False
        self.runtime: AgentRuntime | None = None
        self.approvals: dict[str, dict] = {}

    def emit(self, event: dict) -> None:
        with self.cv:
            self.events.append(event)
            self.cv.notify_all()

    def finish(self) -> None:
        with self.cv:
            self.done = True
            self.cv.notify_all()


class QueuedTask:
    def __init__(self, payload: dict) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.payload = payload
        self.task = str(payload.get("task") or "")
        self.created = time.time()


class AppState:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.session = Session()
        self.term_cwd: Path = self.workspace
        self.runs: dict[str, RunState] = {}
        self.current: str | None = None
        self.lock = threading.Lock()
        self.queue: list[QueuedTask] = []
        self.queue_lock = threading.Lock()
        self.queue_failure: dict | None = None
        self.starting = False
        # 网页 AI 共用同一个浏览器会话：任务运行与审查/记忆/提交信息等旁路调用必须互斥
        self.model_lock = threading.Lock()

    def close(self) -> None:
        """退出钩子：当前没有需要显式释放的常驻资源。"""

    def add_run(self, run: RunState) -> None:
        with self.lock:
            self.runs[run.id] = run
            if len(self.runs) > 3:
                for key in list(self.runs)[:-3]:
                    self.runs.pop(key, None)
            self.current = run.id

    def busy_run(self) -> RunState | None:
        """worker 还没退出的任务（包括已经点了停止、正在收尾的）。"""
        run = self.runs.get(self.current or "")
        if run and not run.done:
            return run
        return None

    def active_run(self) -> RunState | None:
        run = self.busy_run()
        if run and not run.cancelled:
            return run
        return None


READ_ONLY_MCP_TOOLS = {"snapshot", "screenshot", "displayinventory", "controlstatus", "waitfor", "windowlist"}


def _is_read_only_mcp(name: str) -> bool:
    if "__" not in name:
        return False
    tool = name.split("__", 1)[1].strip().lower()
    return tool in READ_ONLY_MCP_TOOLS


def _request_approval(
    run: RunState,
    name: str,
    args: dict,
    workspace: Path,
    whitelist: list[str] | None = None,
    safe_tools: set[str] | None = None,
) -> bool:
    if name == "run_command" and _is_whitelisted(str(args.get("command") or ""), whitelist or []):
        run.emit({"type": "agent", "kind": "status", "text": f"白名单放行：{args.get('command')}"})
        return True

    if name in (safe_tools or set()) or _is_read_only_mcp(name):
        run.emit({"type": "agent", "kind": "status", "text": f"只读工具自动放行：{name}"})
        return True

    if name == "plan":
        preview = {"kind": "plan", "plan": str(args.get("plan") or "")}
    else:
        preview = preview_action(name, args, workspace)
    approval_id = uuid.uuid4().hex[:10]
    holder = {"event": threading.Event(), "allow": False}
    run.approvals[approval_id] = holder
    run.emit({"type": "approval", "id": approval_id, "tool": name, "preview": preview})
    holder["event"].wait(600)
    run.approvals.pop(approval_id, None)
    run.emit({"type": "approval_result", "id": approval_id, "allow": holder["allow"]})
    return bool(holder["allow"])


def _is_whitelisted(command: str, whitelist: list[str]) -> bool:
    text = command.strip().lower()
    for item in whitelist:
        prefix = item.strip().lower()
        if prefix and text.startswith(prefix):
            return True
    return False


COMMIT_PROMPT = """你是 Git 提交信息助手。根据下面的改动写一条中文提交信息。

要求：
1. 只输出一行，不超过 50 个字
2. 用动词开头，说清做了什么，例如「修复登录后页面卡住的问题」
3. 不要引号、不要句号结尾、不要解释、不要分点

{payload}
"""


def _clean_commit_message(text: str) -> str:
    """模型常带引号、序号或「提交信息：」前缀，这里统一清洗。"""
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`").strip()
        if not line:
            continue
        line = re.sub(r"^(\d+[.、)]|[-*•])\s*", "", line)
        line = re.sub(r"^(提交信息|commit message|message)\s*[:：]\s*", "", line, flags=re.I)
        line = line.strip().strip("\"'“”「」").strip()
        if line:
            return line[:120]
    return ""


def _generate_commit_message(workspace: Path, config: dict | None, session: Session) -> str:
    diff = git_diff_text(workspace)
    untracked = git_untracked(workspace)
    if not diff and not untracked:
        raise ValueError("没有可提交的改动")
    parts = []
    if diff:
        parts.append("【改动 diff】\n" + diff)
    if untracked:
        parts.append("【新增未跟踪文件】\n" + "\n".join(untracked))

    backend = build_backend(config, session)
    try:
        backend.start("你是 Git 提交信息助手，只输出一条中文提交信息。")
        reply = backend.ask(COMMIT_PROMPT.format(payload="\n\n".join(parts)))
    finally:
        try:
            backend.close()
        except Exception:
            pass

    message = _clean_commit_message(reply)
    if not message:
        raise ValueError("模型没有返回有效的提交信息")
    return message


def _check_model(state: AppState, config: dict | None) -> dict:
    if not state.model_lock.acquire(blocking=False):
        return {"ok": False, "message": "模型正在处理其它任务，请稍后再试"}
    try:
        return _check_model_now(state, config)
    finally:
        state.model_lock.release()


def _check_model_now(state: AppState, config: dict | None) -> dict:
    """检测当前模型是否可用：网页 AI 看登录与输入框，接口模型发一条最小请求。"""
    config = config or {}
    provider = str(config.get("provider") or "webai").lower()
    if provider in ("webai", "web"):
        site = str(config.get("site") or "deepseek")
        try:
            state.session.ensure(site)
        except (AdapterError, CDPError, OSError) as error:
            return {"ok": False, "message": f"浏览器连接失败：{error}"}
        status = state.session.status()
        if status.get("ready"):
            return {"ok": True, "message": f"{status.get('site') or site} 已就绪"}
        return {
            "ok": False,
            "message": "页面已打开，但没有找到输入框。请在 Edge 窗口里完成登录后重试。",
        }

    try:
        backend = build_backend(config, state.session)
    except BackendError as error:
        return {"ok": False, "message": str(error)}
    try:
        backend.start("这是连通性检测，请只回复 ok")
        reply = backend.ask("请只回复 ok")
    except (BackendError, OSError) as error:
        return {"ok": False, "message": f"模型接口不可用：{error}"}
    finally:
        try:
            backend.close()
        except Exception:
            pass
    return {"ok": True, "message": "模型接口可用：" + (reply or "（无回复）")[:80]}


MEMORY_PROMPT = """你在帮一个编码 Agent 维护长期记忆。根据这次任务的记录，提炼 1-3 条值得下次任务直接复用的客观事实。

只写稳定的信息，例如：项目结构、入口文件、构建或测试命令、代码约定、踩过的坑。
不要写本次任务的临时进展，不要写客套话。

输出要求：每条一行，以「- 」开头，不超过 40 个字；没有值得记的就只输出「无」。

【任务】
{task}

【结果】
{summary}

【改动过的文件】
{files}
"""


def _parse_memory_items(text: str) -> list[str]:
    """优先取列表项；模型没加项目符号时才逐行取。"""
    bullets: list[str] = []
    plain: list[str] = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        is_bullet = bool(re.match(r"^([-*•]|\d+[.、)])\s*", stripped))
        line = stripped.lstrip("-*•").strip()
        line = re.sub(r"^\d+[.、)]\s*", "", line)
        line = line.strip().strip("`\"'“”「」").strip()
        if not line or line in ("无", "没有", "暂无") or line.endswith(("：", ":")):
            continue
        target = bullets if is_bullet else plain
        if line[:80] not in target:
            target.append(line[:80])
    items = bullets or plain
    return items[:3]


def _load_run_session(state: AppState, run_id: str) -> dict | None:
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        return None
    for file in _sessions_dir(state).glob(f"*-{run_id}.json"):
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def _suggest_memory(state: AppState, run_id: str, config: dict | None) -> list[str]:
    """任务结束后让模型提炼 1-3 条记忆候选，供用户确认。"""
    if state.active_run() or not state.model_lock.acquire(blocking=False):
        return []
    try:
        return _suggest_memory_now(state, run_id, config)
    finally:
        state.model_lock.release()


def _suggest_memory_now(state: AppState, run_id: str, config: dict | None) -> list[str]:
    record = _load_run_session(state, run_id)
    if not record:
        return []
    task = str(record.get("task") or "").strip()
    summary = str(record.get("summary") or "").strip()
    if not task:
        return []

    files = []
    for item in list_snapshots(state.workspace):
        if item["id"] == run_id:
            files = item["files"]
            break

    backend = build_backend(config, state.session)
    try:
        backend.start("你是记忆整理助手，只输出提炼后的事实列表。")
        reply = backend.ask(
            MEMORY_PROMPT.format(
                task=task[:1500],
                summary=summary[:800] or "（无摘要）",
                files="\n".join(files[:30]) or "（没有改动文件）",
            )
        )
    finally:
        try:
            backend.close()
        except Exception:
            pass
    return _parse_memory_items(reply)


REVIEW_PROMPT = """你是代码审查者。下面是一次改动的前后对比，请只从正确性角度挑出真正的问题。

关注：明显的逻辑错误、遗漏的边界条件、被破坏的调用方、写错的路径或字段名。
不要提风格、命名、注释、格式化建议。没有明显问题就只输出「无」。

输出要求：每条一行，以「- 」开头，写成「文件:行 问题」，不超过 40 字，最多 5 条。

{payload}
"""


def _parse_review_items(text: str) -> list[str]:
    items: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        line = re.sub(r"^\d+[.、)]\s*", "", line)
        line = line.strip().strip("`\"'“”「」").strip()
        if not line or line in ("无", "没有", "暂无") or line.endswith(("：", ":")):
            continue
        if line not in items:
            items.append(line[:120])
        if len(items) >= 5:
            break
    return items


def _inside_dir(root: Path, target: Path) -> bool:
    """target 必须位于 root 之内，用于处理来自快照清单等不完全可信的路径。"""
    try:
        resolved_root = root.resolve()
        resolved = target.resolve()
    except (OSError, ValueError):
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


MAX_DIFF_FILE_BYTES = 2_000_000


def _run_diff(workspace: Path, run_id: str, max_chars: int = 3000) -> str:
    """把快照里改动过的文件拼成一段 unified diff，供审查角色阅读。"""
    from .snapshots import _dir as snapshot_dir  # 复用快照目录结构

    blocks: list[str] = []
    used = 0
    for item in list_snapshots(workspace):
        if item["id"] != run_id:
            continue
        for rel in item["files"][:6]:
            old = ""
            files_root = snapshot_dir(workspace, run_id) / "files"
            backup = files_root / rel
            try:
                if (
                    _inside_dir(files_root, backup)
                    and backup.is_file()
                    and backup.stat().st_size <= MAX_DIFF_FILE_BYTES
                ):
                    old = backup.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
            target = workspace / rel
            new = ""
            try:
                if (
                    _inside_dir(workspace, target)
                    and target.is_file()
                    and target.stat().st_size <= MAX_DIFF_FILE_BYTES
                ):
                    new = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                new = ""
            diff = "\n".join(
                difflib.unified_diff(
                    old.splitlines(), new.splitlines(), fromfile=rel + "（改前）", tofile=rel, lineterm="", n=2
                )
            )
            if not diff.strip():
                continue
            block = diff[: max(0, max_chars - used)]
            blocks.append(block)
            used += len(block)
            if used >= max_chars:
                break
        break
    return "\n\n".join(blocks)


def _review_changes(state: AppState, run_id: str, config: dict | None) -> list[str]:
    if state.active_run() or not state.model_lock.acquire(blocking=False):
        return []
    try:
        return _review_changes_now(state, run_id, config)
    finally:
        state.model_lock.release()


def _review_changes_now(state: AppState, run_id: str, config: dict | None) -> list[str]:
    diff = _run_diff(state.workspace, run_id)
    if not diff.strip():
        return []
    backend = build_backend(config, state.session)
    try:
        backend.start("你是代码审查者，只输出问题清单。")
        reply = backend.ask(REVIEW_PROMPT.format(payload=diff))
    finally:
        try:
            backend.close()
        except Exception:
            pass
    return _parse_review_items(reply)


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


def _reference_instructions(workspace: Path, references: dict) -> str:
    """把聊天里 @ 的文件、// 的 MCP 服务转成额外指令。"""
    parts = []
    used = 0
    for raw in references.get("files") or []:
        try:
            path = safe_path(workspace, str(raw))
        except Exception:
            continue
        if path.is_dir():
            try:
                entries = sorted(item.name for item in path.iterdir())[:40]
            except OSError:
                continue
            block = f"【目录：{raw}】\n" + "\n".join(entries)
        elif path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            block = f"【文件：{raw}】\n{content}"
        else:
            continue
        if used + len(block) > 6000:
            break
        parts.append(block)
        used += len(block)

    servers = [str(name) for name in (references.get("mcp") or [])]
    if servers:
        parts.append(
            "本次优先使用这些 MCP 服务："
            + "、".join(servers)
            + "（对应工具名以「服务名__」开头）"
        )

    # & 引用的代码符号：取定义处源码，预算单独控制
    symbol_budget = 4000
    used_symbols = 0
    for raw in references.get("symbols") or []:
        fields = str(raw).split("|")
        if len(fields) == 1:
            # 只给了名字（用户手输 &name），取第一个匹配
            found = symbols(workspace, fields[0], 1)
            if not found:
                continue
            path_text = found[0]["path"]
            line = found[0]["line"]
            end = found[0]["end"]
            name = found[0]["name"]
        elif len(fields) == 3:
            path_text, line_text, name = fields
            try:
                line = int(line_text)
            except ValueError:
                continue
            end = None
            for candidate in symbols(workspace, name, 100):
                if candidate["path"] == path_text and candidate["line"] == line:
                    end = candidate["end"]
                    break
        else:
            continue
        snippet = symbol_source(workspace, path_text, line, end)
        if not snippet:
            continue
        block = f"【符号：{name}（{path_text}:{line}）】\n{snippet}"
        if used_symbols + len(block) > symbol_budget:
            break
        parts.append(block)
        used_symbols += len(block)

    return "\n\n".join(parts)


def _reserve_slot(state: AppState) -> bool:
    """抢占“启动中”名额，避免 stop 与排队线程同时拉起两个任务。"""
    with state.queue_lock:
        if state.starting or state.busy_run() is not None or state.queue:
            return False
        state.starting = True
        return True


def _release_slot(state: AppState) -> None:
    with state.queue_lock:
        state.starting = False


def _submit_run(state: AppState, payload: dict) -> tuple[RunState | None, QueuedTask | None]:
    """空闲就直接跑，否则排队。返回 (run, queued)。"""
    if _reserve_slot(state):
        try:
            run = _start_run(state, payload)
        except Exception:
            # 启动失败时释放名额，并让已经排队的任务有机会启动，避免永久卡住
            _release_slot(state)
            _start_next(state)
            raise
        _release_slot(state)
        return run, None
    with state.queue_lock:
        item = QueuedTask(payload)
        state.queue.append(item)
        return None, item


def _start_next(state: AppState) -> None:
    with state.queue_lock:
        if state.starting or state.busy_run() is not None or not state.queue:
            return
        state.starting = True
        item = state.queue.pop(0)
    failed = False
    try:
        _start_run(state, item.payload)
    except Exception as error:  # noqa: BLE001
        failed = True
        # 启动失败也要让界面看到，否则排队任务会无声消失
        print(f"启动排队任务失败: {error}", file=sys.stderr)
        state.queue_failure = {
            "id": item.id,
            "task": item.task,
            "message": str(error) or type(error).__name__,
            "created": time.time(),
        }
    finally:
        _release_slot(state)
    if failed:
        # 一个坏任务不能把后面的队列永久堵死
        _start_next(state)


def _start_run(state: AppState, payload: dict) -> RunState:
    task = payload["task"]
    try:
        max_steps = int(payload.get("max_steps") or 12)
    except (TypeError, ValueError) as error:
        raise ValueError("max_steps 必须是整数") from error
    max_steps = max(1, min(max_steps, 100))
    raw_whitelist = payload.get("whitelist")
    whitelist = (
        [item for item in raw_whitelist if isinstance(item, str) and item.strip()]
        if isinstance(raw_whitelist, list)
        else []
    )
    references = payload.get("references") if isinstance(payload.get("references"), dict) else {}
    skill_ids = list(payload.get("skills") or []) + list(references.get("skills") or [])
    model_config = payload.get("model") if isinstance(payload.get("model"), dict) else None
    fallback_config = payload.get("fallback") if isinstance(payload.get("fallback"), dict) else None
    images = []
    for raw in references.get("images") or []:
        try:
            path = safe_path(state.workspace, str(raw))
        except Exception:
            continue
        if path.is_file():
            images.append(str(path))

    extra = build_instructions(state.workspace, [str(item) for item in skill_ids])
    reference_text = _reference_instructions(state.workspace, references)
    if reference_text:
        extra = (extra + "\n\n" + reference_text).strip()

    raw_rules = payload.get("rules")
    rule_keys = [str(item) for item in raw_rules] if isinstance(raw_rules, list) else None
    project_rules = build_rules(state.workspace, rule_keys)

    run = RunState()
    run.task = task
    state.add_run(run)
    mcp_servers = _load_mcp_servers(state)
    safe_tools = {
        f"{server['name']}__{tool}"
        for server in mcp_servers
        for tool in server.get("safeTools", [])
    }
    try:
        runtime = AgentRuntime(
            workspace=state.workspace,
            site=payload.get("site") or "deepseek",
            max_steps=max_steps,
            auto_allow=bool(payload.get("auto_allow")),
            approver=lambda name, args, workspace: _request_approval(
                run, name, args, workspace, whitelist, safe_tools
            ),
            on_event=lambda kind, text: run.emit({"type": "agent", "kind": kind, "text": text}),
            session=state.session,
            model_config=model_config,
            mcp_servers=mcp_servers,
            extra_instructions=extra,
            project_rules=project_rules,
            memory=build_memory(state.workspace),
            mode=str(payload.get("mode") or "code"),
            plan_first=bool(payload.get("plan_first")),
            snapshot_id=run.id,
            fallback_config=fallback_config,
            images=images,
        )
    except Exception:
        # 模型配置不合法等情况：别让这个 run 永久占住“当前任务”
        run.finish()
        state.runs.pop(run.id, None)
        with state.lock:
            if state.current == run.id:
                state.current = None
        raise
    run.runtime = runtime
    snapshot_begin(state.workspace, run.id, task)
    run.emit({"type": "run_started", "run": run.id, "workspace": str(state.workspace)})

    def worker() -> None:
        summary = ""
        error: str | None = None
        try:
            # 和审查/记忆/提交信息等旁路模型调用互斥，避免同时操作网页会话
            with state.model_lock:
                summary = runtime.run(task)
            run.emit({"type": "run_done", "summary": summary})
        except StopRequested:
            summary = "已被用户停止"
            run.emit({"type": "run_done", "summary": summary})
        except AdapterError as exception:
            error = str(exception)
            run.emit({"type": "run_error", "message": error})
        except BackendError as exception:
            error = str(exception)
            run.emit({"type": "run_error", "message": error})
        except Exception as exception:  # noqa: BLE001
            error = f"{type(exception).__name__}: {exception}"
            run.emit({"type": "run_error", "message": error})
        finally:
            snapshot_finish(state.workspace, run.id, summary or (error or ""))
            _save_session(state, run, task, summary, error)
            run.finish()
            _start_next(state)

    threading.Thread(target=worker, daemon=True).start()
    return run


def _sessions_dir(state: AppState) -> Path:
    path = state.workspace / ".eagent" / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_session(state: AppState, run: RunState, task: str, summary: str, error: str | None) -> None:
    events = []
    for event in run.events:
        kind = event.get("type")
        if kind == "approval":
            preview = event.get("preview") or {}
            events.append(
                {
                    "type": "approval",
                    "tool": event.get("tool"),
                    "preview": {
                        "kind": preview.get("kind"),
                        "path": preview.get("path"),
                        "command": preview.get("command"),
                    },
                }
            )
        elif kind == "agent":
            events.append({"type": "agent", "kind": event.get("kind"), "text": (event.get("text") or "")[:2000]})
        else:
            events.append({key: event.get(key) for key in ("type", "summary", "message") if key in event})
    payload = {
        "id": run.id,
        "task": task,
        "summary": summary,
        "error": error,
        "created": time.time(),
        "events": events,
    }
    try:
        target = _sessions_dir(state) / f"{int(payload['created'])}-{run.id}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        prune_folder(_sessions_dir(state), 50, "*.json")
    except OSError as exception:
        print(f"保存会话失败: {exception}", file=sys.stderr)


def _list_sessions(state: AppState) -> list[dict]:
    items = []
    for file in _sessions_dir(state).glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append(
            {
                "id": file.stem,
                "task": data.get("task") or "",
                "summary": data.get("summary") or "",
                "error": data.get("error"),
                "created": data.get("created") or 0,
            }
        )
    items.sort(key=lambda item: item["created"], reverse=True)
    return items[:30]


def _load_session(state: AppState, session_id: str) -> dict:
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise ValueError("会话 ID 非法")
    path = _sessions_dir(state) / f"{session_id}.json"
    if not path.is_file():
        raise ValueError("会话不存在")
    return json.loads(path.read_text(encoding="utf-8"))


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


class Handler(BaseHTTPRequestHandler):
    state: AppState
    token: str = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        line = fmt % args
        line = _TOKEN_IN_LOG.sub(r"\1***", line)
        print(f"[webui] {line}", file=sys.stderr)
        _append_log(self.state.workspace, line)

    # ---- GET ----

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if not self._host_ok():
            self.close_connection = True
            self._json(403, {"error": "forbidden host"})
            return
        if not self._authorized():
            self.close_connection = True
            self._json(401, {"error": "unauthorized"})
            return

        try:
            if parsed.path in ("/", "/index.html"):
                self._serve_ui()
                return
            if parsed.path.startswith("/ui/"):
                self._serve_asset(unquote(parsed.path[len("/ui/"):]))
                return
            if parsed.path == "/api/health":
                self._json(200, {"status": "ok"})
                return
            if parsed.path == "/api/workspace":
                self._json(200, {"path": str(self.state.workspace)})
                return
            if parsed.path == "/api/tree":
                self._json(200, {"entries": _tree(self.state, (params.get("path") or ["."])[0])})
                return
            if parsed.path == "/api/file":
                path = safe_path(self.state.workspace, (params.get("path") or [""])[0])
                if not path.is_file():
                    raise ValueError("文件不存在")
                if path.stat().st_size > 2_000_000:
                    raise ValueError("文件过大（超过 2MB），请用搜索或分段读取")
                self._json(200, {"path": str(path.relative_to(self.state.workspace)), "content": _read_text(path)})
                return
            if parsed.path == "/api/image":
                raw_path = (params.get("path") or [""])[0]
                path = safe_path(self.state.workspace, raw_path)
                if not path.is_file():
                    raise ValueError("图片不存在")
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError("图片过大（超过 20MB）")
                suffix = path.suffix.lower()
                content_type = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                }.get(suffix, "application/octet-stream")
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/api/agent/events":
                self._stream(params.get("run", [""])[0])
                return
            if parsed.path == "/api/files":
                self._json(200, {"files": _list_files(self.state)})
                return
            if parsed.path == "/api/symbols":
                query = (params.get("q") or [""])[0]
                try:
                    limit = int((params.get("limit") or ["40"])[0] or 40)
                except ValueError:
                    limit = 40
                self._json(200, {"symbols": symbols(self.state.workspace, query, limit)})
                return
            if parsed.path == "/api/git":
                self._json(200, git_status(self.state.workspace))
                return
            if parsed.path == "/api/snapshots":
                self._json(200, {"snapshots": list_snapshots(self.state.workspace)})
                return
            if parsed.path == "/api/definition":
                name = (params.get("symbol") or [""])[0]
                path = (params.get("path") or [""])[0] or None
                self._json(200, {"definitions": find_definition(self.state.workspace, name, path)})
                return
            if parsed.path == "/api/references":
                name = (params.get("symbol") or [""])[0]
                self._json(200, {"references": find_references(self.state.workspace, name)})
                return
            if parsed.path == "/api/tests/detect":
                self._json(200, {"command": detect_test_command(self.state.workspace)})
                return
            if parsed.path == "/api/memory":
                self._json(
                    200,
                    {
                        "project": load_memory(self.state.workspace, "project"),
                        "global": load_memory(self.state.workspace, "global"),
                    },
                )
                return
            if parsed.path == "/api/git/diff":
                path = (params.get("path") or [""])[0]
                staged = (params.get("staged") or ["0"])[0] in ("1", "true")
                self._json(200, git_diff_pair(self.state.workspace, path, staged))
                return
            if parsed.path == "/api/search":
                query = (params.get("q") or [""])[0]
                self._json(200, {"results": _search_text(self.state, query)})
                return
            if parsed.path == "/api/sessions":
                self._json(200, {"sessions": _list_sessions(self.state)})
                return
            if parsed.path.startswith("/api/sessions/"):
                session_id = parsed.path.rsplit("/", 1)[-1]
                self._json(200, _load_session(self.state, session_id))
                return
            if parsed.path == "/api/mcp":
                path = _mcp_file(self.state)
                text = path.read_text(encoding="utf-8") if path.is_file() else '{\n  "mcpServers": {}\n}'
                self._json(200, {"text": text, "servers": _describe_mcp_servers(self.state)})
                return
            if parsed.path == "/api/mcp/templates":
                self._json(200, {"templates": list_mcp_templates(self.state.workspace)})
                return
            if parsed.path == "/api/skills":
                self._json(200, {"skills": list_skills(self.state.workspace)})
                return
            if parsed.path == "/api/rules":
                self._json(200, {"rules": list_rules(self.state.workspace)})
                return
            if parsed.path.startswith("/api/rules/"):
                rule_id = unquote(parsed.path.rsplit("/", 1)[-1])
                scope = (params.get("scope") or ["project"])[0]
                self._json(200, load_rule(self.state.workspace, rule_id, scope))
                return
            if parsed.path.startswith("/api/skills/"):
                skill_id = unquote(parsed.path.rsplit("/", 1)[-1])
                name, body = load_skill(self.state.workspace, skill_id)
                self._json(200, {"id": skill_id, "name": name, "content": body})
                return
            self._json(404, {"error": f"未知路径: {parsed.path}"})
        except (ValueError, OSError, GitError, ToolError) as error:
            self._json(400, {"error": str(error)})

    # ---- POST ----

    def do_POST(self) -> None:
        if not self._host_ok():
            self.close_connection = True
            self._json(403, {"error": "forbidden host"})
            return
        if not self._authorized():
            # 不读 body 直接断开，避免 keep-alive 下残留字节被当成下一条请求
            self.close_connection = True
            self._json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        try:
            payload = self._body()
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return

        try:
            if parsed.path == "/api/workspace":
                target = Path(str(payload.get("path") or "")).expanduser().resolve()
                if not target.is_dir():
                    raise ValueError(f"目录不存在: {target}")
                # 和启动/排队线程共用一把锁，避免刚切完目录就有旧队列任务开跑
                with self.state.queue_lock:
                    if self.state.starting or self.state.busy_run() or self.state.queue:
                        raise RuntimeError("任务运行中或队列未清空，不能切换工作目录，请先停止任务")
                    old_workspace = self.state.workspace
                    self.state.workspace = target
                    self.state.term_cwd = target
                reset_index(old_workspace)
                self._json(200, {"path": str(target)})
                return
            if parsed.path == "/api/file":
                path = safe_path(self.state.workspace, str(payload.get("path") or ""))
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("content 必须是字符串")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="")
                self._json(200, {"ok": True, "path": str(path.relative_to(self.state.workspace))})
                return
            if parsed.path == "/api/agent/start":
                task = str(payload.get("task") or "").strip()
                if not task:
                    raise ValueError("任务不能为空")
                run, queued = _submit_run(self.state, {**payload, "task": task})
                if queued is not None:
                    with self.state.queue_lock:
                        position = [item.id for item in self.state.queue].index(queued.id) + 1
                    self._json(200, {"queued": True, "id": queued.id, "position": position})
                    return
                self._json(200, {"run": run.id})
                return
            if parsed.path == "/api/agent/queue":
                active = self.state.active_run()
                with self.state.queue_lock:
                    items = [
                        {"id": item.id, "task": item.task, "created": item.created}
                        for item in self.state.queue
                    ]
                self._json(
                    200,
                    {
                        "active": (
                            {"id": active.id, "task": active.task, "events": len(active.events)}
                            if active
                            else None
                        ),
                        "items": items,
                        "failure": self.state.queue_failure,
                    },
                )
                return
            if parsed.path == "/api/agent/queue/remove":
                target = str(payload.get("id") or "")
                removed = False
                with self.state.queue_lock:
                    for index, item in enumerate(self.state.queue):
                        if item.id == target:
                            self.state.queue.pop(index)
                            removed = True
                            break
                self._json(200, {"ok": True, "removed": removed})
                return
            if parsed.path == "/api/agent/stop":
                run = self.state.active_run()
                if run:
                    run.cancelled = True
                    if run.runtime:
                        run.runtime.stop_event.set()
                    for holder in list(run.approvals.values()):
                        holder["allow"] = False
                        holder["event"].set()
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/agent/new_session":
                if self.state.busy_run():
                    raise RuntimeError("任务运行中，不能新建会话，请先停止任务")
                model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
                provider = str(model.get("provider") or "webai").lower()
                if provider in ("webai", "web"):
                    if not self.state.model_lock.acquire(blocking=False):
                        raise RuntimeError("模型正在使用中，请稍后再试")
                    try:
                        self.state.session.new_chat(str(model.get("site") or "deepseek"))
                    except AdapterError as error:
                        raise ValueError(str(error)) from error
                    finally:
                        self.state.model_lock.release()
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/approval":
                run = self.state.runs.get(str(payload.get("run") or self.state.current or ""))
                holder = run.approvals.get(str(payload.get("id") or "")) if run else None
                if not holder:
                    raise ValueError("确认请求已失效")
                holder["allow"] = bool(payload.get("allow"))
                holder["event"].set()
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/mcp":
                text = str(payload.get("text") or "")
                try:
                    data = json.loads(text or "{}")
                except json.JSONDecodeError as error:
                    raise ValueError(f"MCP 配置不是合法 JSON: {error}") from error
                if not isinstance(data, dict):
                    raise ValueError("MCP 配置必须是 JSON 对象")
                path = _mcp_file(self.state)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
                self._json(200, {"ok": True, "servers": len(servers)})
                return
            if parsed.path == "/api/mcp/install":
                result = install_mcp_template(
                    self.state.workspace, str(payload.get("id") or "")
                )
                self._json(200, {"ok": True, **result})
                return
            if parsed.path == "/api/mcp/toggle":
                name = str(payload.get("name") or "")
                enabled = bool(payload.get("enabled"))
                path = _mcp_file(self.state)
                if not path.is_file():
                    raise ValueError("还没有 MCP 配置")
                data = json.loads(path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers") if isinstance(data, dict) else None
                if not isinstance(servers, dict) or name not in servers:
                    raise ValueError(f"MCP 服务不存在: {name}")
                if not isinstance(servers[name], dict):
                    raise ValueError(f"MCP 服务配置不是对象: {name}")
                servers[name]["enabled"] = enabled
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/skills":
                name = str(payload.get("name") or "").strip()
                description = str(payload.get("description") or "").strip()
                instructions = str(payload.get("instructions") or "")
                skill_id = payload.get("id")
                resolved = save_skill(
                    self.state.workspace,
                    name,
                    description,
                    instructions,
                    str(skill_id) if skill_id else None,
                )
                self._json(200, {"ok": True, "id": resolved})
                return
            if parsed.path == "/api/skills/delete":
                delete_skill(self.state.workspace, str(payload.get("id") or ""))
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/rules":
                resolved = save_rule(
                    self.state.workspace,
                    str(payload.get("name") or "").strip(),
                    str(payload.get("description") or "").strip(),
                    str(payload.get("content") or ""),
                    str(payload.get("id")) if payload.get("id") else None,
                    str(payload.get("scope") or "project"),
                )
                self._json(200, {"ok": True, "id": resolved, "scope": str(payload.get("scope") or "project")})
                return
            if parsed.path == "/api/rules/delete":
                delete_rule(
                    self.state.workspace,
                    str(payload.get("id") or ""),
                    str(payload.get("scope") or "project"),
                )
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/review":
                run_id = str(payload.get("run") or "")
                if not run_id:
                    raise ValueError("缺少任务 ID")
                config = payload.get("model") if isinstance(payload.get("model"), dict) else None
                self._json(200, {"ok": True, "items": _review_changes(self.state, run_id, config)})
                return
            if parsed.path == "/api/memory/suggest":
                run_id = str(payload.get("run") or "")
                if not run_id:
                    raise ValueError("缺少任务 ID")
                config = payload.get("model") if isinstance(payload.get("model"), dict) else None
                self._json(200, {"ok": True, "items": _suggest_memory(self.state, run_id, config)})
                return
            if parsed.path == "/api/memory/append":
                items = [str(item) for item in (payload.get("items") or []) if str(item).strip()][:10]
                if not items:
                    raise ValueError("没有要保存的内容")
                scope = str(payload.get("scope") or "project")
                for item in items:
                    memory_append(self.state.workspace, item, scope)
                self._json(200, {"ok": True, "saved": len(items)})
                return
            if parsed.path == "/api/memory":
                save_memory_content(
                    self.state.workspace,
                    str(payload.get("content") or ""),
                    str(payload.get("scope") or "project"),
                )
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/snapshots/restore":
                active = self.state.active_run()
                target = str(payload.get("id") or "")
                if active and active.id == target:
                    raise RuntimeError("任务还在运行，先停止再回滚")
                result = snapshot_restore(self.state.workspace, target)
                self._json(200, {"ok": True, **result})
                return
            if parsed.path == "/api/git/init":
                self._json(200, {"ok": True, "output": git_init(self.state.workspace)})
                return
            if parsed.path == "/api/git/stage":
                git_stage(self.state.workspace, [str(item) for item in (payload.get("paths") or [])])
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/git/unstage":
                git_unstage(self.state.workspace, [str(item) for item in (payload.get("paths") or [])])
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/terminal/exec":
                self._json(200, _terminal_exec(self.state, str(payload.get("command") or "")))
                return
            if parsed.path == "/api/upload":
                path = _save_upload(
                    self.state.workspace,
                    str(payload.get("name") or "image.png"),
                    str(payload.get("data") or ""),
                )
                self._json(200, {"ok": True, "path": path})
                return
            if parsed.path == "/api/screenshot":
                output = execute_gui("screen_capture", {}, self.state.workspace)
                # 贪婪匹配到最后一个全角括号，避免工作目录本身带「（」时截断路径
                match = re.search(r"截图已保存：(.+)（", output)
                if not match:
                    raise ValueError(output)
                target = Path(match.group(1))
                self._json(
                    200,
                    {"ok": True, "path": str(target.relative_to(self.state.workspace)).replace("\\", "/")},
                )
                return
            if parsed.path == "/api/tests/run":
                command = str(payload.get("command") or "")
                self._json(200, run_tests(self.state.workspace, command))
                return
            if parsed.path == "/api/git/message":
                if self.state.busy_run():
                    raise RuntimeError("任务运行中，先停止再生成提交信息")
                if not self.state.model_lock.acquire(blocking=False):
                    raise RuntimeError("模型正在使用中，请稍后再试")
                try:
                    config = payload.get("model") if isinstance(payload.get("model"), dict) else None
                    message = _generate_commit_message(self.state.workspace, config, self.state.session)
                finally:
                    self.state.model_lock.release()
                self._json(200, {"ok": True, "message": message})
                return
            if parsed.path == "/api/model/check":
                if self.state.busy_run() is not None:
                    raise RuntimeError("任务运行中，先停止再检测模型")
                config = payload.get("model") if isinstance(payload.get("model"), dict) else None
                self._json(200, _check_model(self.state, config))
                return
            if parsed.path == "/api/git/commit":
                output = git_commit(self.state.workspace, str(payload.get("message") or ""))
                self._json(200, {"ok": True, "output": output})
                return
            if parsed.path == "/api/fs/file":
                path = safe_path(self.state.workspace, str(payload.get("path") or ""))
                if path.exists():
                    raise ValueError("文件已存在")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(payload.get("content") or ""), encoding="utf-8", newline="")
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/fs/mkdir":
                path = safe_path(self.state.workspace, str(payload.get("path") or ""))
                if path.exists():
                    raise ValueError("目录已存在")
                path.mkdir(parents=True, exist_ok=True)
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/fs/rename":
                source = safe_path(self.state.workspace, str(payload.get("path") or ""))
                target = safe_path(self.state.workspace, str(payload.get("to") or ""))
                if source == self.state.workspace or (
                    source.name == ".eagent" and source.parent == self.state.workspace
                ):
                    raise ValueError("不能重命名工作目录或 .eagent 内部目录")
                if target.name == ".eagent" and target.parent == self.state.workspace:
                    raise ValueError("不能把路径重命名为 .eagent")
                if not source.exists():
                    raise ValueError("源路径不存在")
                if target.exists():
                    raise ValueError("目标路径已存在")
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
                self._json(200, {"ok": True, "path": str(target.relative_to(self.state.workspace))})
                return
            if parsed.path == "/api/fs/delete":
                path = safe_path(self.state.workspace, str(payload.get("path") or ""))
                if path == self.state.workspace or (
                    path.name == ".eagent" and path.parent == self.state.workspace
                ):
                    raise ValueError("不能删除工作目录或 .eagent 内部目录")
                if not path.exists():
                    raise ValueError("路径不存在")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                self._json(200, {"ok": True})
                return
            self._json(404, {"error": f"未知路径: {parsed.path}"})
        except subprocess.TimeoutExpired:
            self._json(400, {"error": "命令超时（120 秒）"})
        except BackendError as error:
            self._json(400, {"error": str(error)})
        except GitError as error:
            self._json(400, {"error": str(error)})
        except (ValueError, OSError, ToolError) as error:
            self._json(400, {"error": str(error)})
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    # ---- 内部 ----

    def _host_ok(self) -> bool:
        """只接受本机回环地址，挡掉 DNS rebinding。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name = host.split(":")[0]
        return name in ("127.0.0.1", "localhost", "[::1]")

    def _authorized(self) -> bool:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health" or parsed.path.startswith("/ui/"):
            return True
        token = self.headers.get("X-EAgent-Token") or ""
        if not token:
            token = (parse_qs(parsed.query).get("token") or [""])[0]
        return bool(self.token) and token == self.token

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            self.close_connection = True
            raise ValueError("Content-Length 不合法") from error
        if length < 0 or length > MAX_BODY_BYTES:
            # 不读取超限请求体，保持连接会导致后续请求错位
            self.close_connection = True
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as error:
            raise ValueError(f"请求体不是合法 JSON: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _serve_asset(self, relative: str) -> None:
        """提供 ui 目录下的样式和脚本；只允许读取 ui 内部文件。"""
        root = UI_DIR.resolve()
        try:
            target = (root / relative).resolve()
        except (OSError, ValueError):
            self._json(404, {"error": "not found"})
            return
        if target != root and root not in target.parents:
            self._json(404, {"error": "not found"})
            return
        if not target.is_file():
            self._json(404, {"error": "not found"})
            return
        content_type = {
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
        }.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _serve_ui(self) -> None:
        index = UI_DIR / "index.html"
        if not index.is_file():
            self._json(500, {"error": "缺少 ui/index.html"})
            return
        html = index.read_text(encoding="utf-8").replace("__EAGENT_TOKEN__", self.token)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, run_id: str) -> None:
        run = self.state.runs.get(run_id)
        if not run:
            self._json(404, {"error": "找不到运行中的任务"})
            return

        try:
            index = max(0, int(self.headers.get("Last-Event-ID") or 0))
        except ValueError:
            index = 0
        try:
            since = max(0, int((parse_qs(urlparse(self.path).query).get("since") or ["0"])[0] or 0))
        except ValueError:
            since = 0
        index = max(index, since)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        # 已结束的任务不重放整套历史，只补一条终态事件，避免刷新后重复渲染
        if run.done and index == 0:
            final = next(
                (
                    event
                    for event in reversed(run.events)
                    if event.get("type") in ("run_done", "run_error")
                ),
                {"type": "run_done", "summary": "任务已结束"},
            )
            try:
                self.wfile.write(
                    f"id: 1\ndata: {json.dumps(final, ensure_ascii=False)}\n\n".encode("utf-8")
                )
                self.wfile.flush()
            except OSError:
                pass
            return

        try:
            while True:
                with run.cv:
                    while index >= len(run.events) and not run.done:
                        run.cv.wait(1.0)
                    pending = run.events[index:]
                    index = len(run.events)
                    done = run.done
                for offset, event in enumerate(pending):
                    event_id = index - len(pending) + offset + 1
                    self.wfile.write(
                        f"id: {event_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                if done and not pending:
                    break
        except OSError:
            # 浏览器断开、连接中止等都在这里收尾
            pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")


def open_app_window(url: str) -> bool:
    executable = find_edge()
    if not executable:
        return False
    profile = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EAgentBridge", "ui-profile"
    )
    os.makedirs(profile, exist_ok=True)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    args = [
        executable,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
    ]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return True


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[0]
        if error in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        try:
            _append_log(
                self.RequestHandlerClass.state.workspace,
                f"[error] {client_address} {sys.exc_info()[1]!r}",
            )
        except Exception:
            pass
        super().handle_error(request, client_address)


def create_app_server(port: int, workspace: Path) -> tuple[AppServer, AppState]:
    state = AppState(workspace)
    token = secrets.token_urlsafe(24)
    handler = type(
        "TraeLiteHandler",
        (Handler,),
        {"state": state, "token": token},
    )
    return AppServer(("127.0.0.1", port), handler), state


def main() -> None:
    workspace = Path(os.environ.get("EAGENT_BRIDGE_WORKSPACE") or Path.cwd())
    try:
        server, state = create_app_server(DEFAULT_PORT, workspace)
    except OSError as error:
        print(
            f"启动失败：端口 {DEFAULT_PORT} 可能已被占用（EAgent Studio 可能已在运行）。"
            f"\n{error}",
            file=sys.stderr,
        )
        return
    token = getattr(server.RequestHandlerClass, "token", "")
    url = f"http://127.0.0.1:{DEFAULT_PORT}/?token={token}"
    print(f"迷你 Trae 已启动: {url}", file=sys.stderr)
    print(f"工作目录: {state.workspace}", file=sys.stderr)

    if not os.environ.get("EAGENT_BRIDGE_NO_WINDOW"):
        if not open_app_window(url):
            print("未找到 Edge，请手动在浏览器打开上面的地址", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止", file=sys.stderr)
    finally:
        server.server_close()
        state.close()


if __name__ == "__main__":
    main()
