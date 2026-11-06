"""本地 HTTP 服务：Web UI 静态资源、工作区 API 与 SSE。"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .adapters import AdapterError
from .backends import BackendError
from .agent_service import (
    AppState,
    _check_model,
    _generate_commit_message,
    _list_sessions,
    _load_session,
    _review_changes,
    _submit_run,
    _suggest_memory,
)
from .cdp import find_edge
from .git_ops import (
    GitError,
    commit as git_commit,
    diff_pair as git_diff_pair,
    init as git_init,
    stage as git_stage,
    status as git_status,
    unstage as git_unstage,
)
from .gui_tools import execute_gui
from .index import (
    definition as find_definition,
    references as find_references,
    reset as reset_index,
    symbols,
)
from .mcp_templates import install as install_mcp_template, list_templates as list_mcp_templates
from .memory import (
    append as memory_append,
    load as load_memory,
    save as save_memory_content,
)
from .rules import delete_rule, list_rules, load_rule, save_rule
from .skills import delete_skill, list_skills, load_skill, save_skill
from .snapshots import list_snapshots, restore as snapshot_restore
from .testrunner import detect_command as detect_test_command
from .testrunner import run as run_tests
from .tools import ToolError, safe_path
from .workspace_service import (
    _describe_mcp_servers,
    _list_files,
    _mcp_file,
    _read_text,
    _save_upload,
    _search_text,
    _terminal_exec,
    _tree,
)

UI_DIR = Path(__file__).parent / "ui"
DEFAULT_PORT = int(os.environ.get("EAGENT_BRIDGE_WEBUI_PORT", "8766"))
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
LOG_MAX_BYTES = 2 * 1024 * 1024
_TOKEN_IN_LOG = re.compile(r"(token=)[^&\s\"]+")


def default_workspace() -> Path:
    """默认工作目录；打包后不要落在安装目录（通常不可写）。"""
    configured = os.environ.get("EAGENT_BRIDGE_WORKSPACE")
    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False):
        documents = Path.home() / "Documents"
        return documents if documents.is_dir() else Path.home()
    return Path.cwd()


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
            ".map": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ttf": "font/ttf",
            ".woff": "font/woff",
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
