"""Agent 运行编排：任务状态、审批、队列、会话记录与旁路模型调用。"""

from __future__ import annotations

import difflib
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from .adapters import AdapterError
from .backends import BackendError, build_backend
from .cdp import CDPError
from .git_ops import diff_text as git_diff_text, untracked_files as git_untracked
from .index import symbol_source, symbols
from .memory import build_memory
from .runtime import AgentRuntime, StopRequested
from .rules import build_rules
from .session import Session
from .snapshots import begin as snapshot_begin, finish as snapshot_finish, list_snapshots
from .skills import build_instructions
from .storage import ensure_data_dir, prune as prune_folder
from .tools import preview_action, safe_path
from .workspace_service import _load_mcp_servers

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


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
        ensure_data_dir(self.workspace)
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
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
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
        if not isinstance(data, dict):
            continue
        items.append(
            {
                "id": file.stem,
                "task": data.get("task") or "",
                "summary": data.get("summary") or "",
                "error": data.get("error"),
                "created": (
                    data.get("created")
                    if isinstance(data.get("created"), (int, float))
                    else 0
                ),
            }
        )
    items.sort(key=lambda item: item["created"], reverse=True)
    return items[:30]


def _load_session(state: AppState, session_id: str) -> dict:
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        raise ValueError("会话 ID 非法")
    path = _sessions_dir(state) / f"{session_id}.json"
    if not path.is_file():
        raise ValueError("会话不存在")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("会话记录格式异常")
    return data
