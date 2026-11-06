"""Agent 运行时：把模型的每一步决策翻译成本地工具或 MCP 调用。"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .adapters import AdapterError, get_adapter
from .backends import BackendError, ChatBackend, WebBackend, build_backend
from .mcp_client import MCPClient, MCPError, describe_tools_for_prompt
from .plugins import describe as describe_plugins, load as load_plugins
from .server import _extract_json
from .session import Session
from .snapshots import backup as snapshot_backup
from .storage import prune
from .tools import ToolError, describe_tools, execute, safe_path, tool_specs

MAX_STEPS = 12
MAX_RETRIES = 2
# 长任务里对话会越滚越大，超过阈值就压缩成一份备忘再继续
COMPACT_THRESHOLD = 14000
COMPACT_MAX_TIMES = 6
COMPACT_PROMPT = (
    "请把下面这段 Agent 执行过程压缩成一份不超过 300 字的备忘。"
    "必须保留：任务目标、已完成的步骤、改动或读取过的文件路径、当前未完成的事。"
    "只输出备忘正文，不要解释。\n\n"
)
PLAN_PROMPT = (
    "先不要动手，也不要写任何代码。"
    "请给出一份执行计划：用编号列出 3-6 步，每一步说明要看哪个文件或执行什么命令、目的是什么。"
    "只输出计划正文。"
)
RULE_TEMPLATE = """你是运行在 Windows 上的编码 Agent，工作目录是：
{workspace}

可用工具：
{tools}

你每轮只能输出一个 JSON 对象，格式：
{{"thought":"这一步的想法","action":"工具名","args":{{...}}}}

任务完成时输出：
{{"thought":"收尾","action":"finish","args":{{"summary":"做了什么"}}}}

规则：
1. 一次只做一件事，等我给你工具结果后再决定下一步
2. 不要臆造文件内容，改文件前先用 read_file 确认原文
3. 修改文件或执行命令前会有用户确认，被拒绝时换方案
4. MCP 工具名可能带 server__ 前缀，按给出的名字原样调用
5. 不要输出解释文字，只输出 JSON
6. Windows 路径里不要用单反斜杠转义，统一写成 D:/FTAPI 这种正斜杠
7. 截图里会包含本程序窗口，描述桌面时忽略 EAgent Studio 本身
"""

BUILTIN_TOOL_NAMES = {spec["name"] for spec in tool_specs()} - {"finish"}

CODE_TOOLS = {
    "list_dir", "read_file", "write_file", "replace_in_file", "search_text", "search_code",
    "find_files", "batch_files", "http_request", "office_document", "save_memory", "run_command",
}
COMPUTER_TOOLS = {
    "screen_capture", "mouse_move", "mouse_click", "mouse_drag",
    "keyboard_type", "keyboard_hotkey", "window_list", "window_focus", "run_command",
}
HYBRID_TOOLS = CODE_TOOLS | COMPUTER_TOOLS
MODE_TOOLS = {"code": CODE_TOOLS, "computer": COMPUTER_TOOLS, "hybrid": HYBRID_TOOLS}


class StopRequested(RuntimeError):
    """用户中止了任务。"""


def _clean_args(action: str, args) -> dict:
    if args is None:
        return {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {}
    return args if isinstance(args, dict) else {}


def parse_action(text: str) -> dict:
    payload = _extract_json(text) or {}
    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise ValueError("缺少 action 字段")
    return {
        "thought": str(payload.get("thought") or ""),
        "action": action,
        "args": _clean_args(action, payload.get("args")),
    }


class AgentRuntime:
    def __init__(
        self,
        workspace: Path,
        site: str = "deepseek",
        max_steps: int = MAX_STEPS,
        auto_allow: bool = False,
        approver=None,
        on_event=None,
        session: Session | None = None,
        backend: ChatBackend | None = None,
        mcp_servers: list[dict] | None = None,
        extra_instructions: str = "",
        project_rules: str = "",
        memory: str = "",
        model_config: dict | None = None,
        mode: str = "code",
        plan_first: bool = False,
        snapshot_id: str = "",
        fallback_config: dict | None = None,
        images: list | None = None,
    ):
        self.workspace = workspace.resolve()
        self.site = site
        mode = str(mode).lower()
        self.mode = mode if mode in MODE_TOOLS else "code"
        self.enabled_tools = MODE_TOOLS[self.mode]
        self.max_steps = max_steps
        self.auto_allow = auto_allow
        self.approver = approver
        self.on_event = on_event or (lambda kind, text: None)
        self.plugins, self.plugin_errors = load_plugins(self.workspace)
        if self.plugins:
            self.enabled_tools = self.enabled_tools | {tool.name for tool in self.plugins}
        self.session = session or Session()
        config = dict(model_config or {})
        if str(config.get("provider") or "webai").lower() in ("webai", "web") and not config.get("site"):
            config["site"] = site
        self.backend = backend or build_backend(
            config, self.session, on_status=lambda text: self._emit("status", text)
        )
        self.mcp_servers = mcp_servers or []
        self.extra_instructions = extra_instructions
        self.project_rules = project_rules
        self.memory = memory
        self.plan_first = plan_first
        self.snapshot_id = snapshot_id
        self.fallback_config = fallback_config if isinstance(fallback_config, dict) else None
        self._switched = False
        self._rules = ""
        self.images = list(images or [])
        self.stop_event = threading.Event()

        self._transcript: list[str] = []
        self._history_chars = 0
        self._compact_count = 0
        self.task = ""

        self._clients: list[MCPClient] = []
        self._mcp_tools: dict[str, dict] = {}
        self._mcp_descriptions: list[str] = []

    def _emit(self, kind: str, text: str) -> None:
        try:
            self.on_event(kind, text)
        except Exception:
            pass

    def run(self, task: str) -> str:
        # 排队期间用户可能已经点了停止，别再去重置网页会话或连接浏览器
        self._check_stop()
        self.task = task
        self._emit("status", self._backend_label())
        try:
            self._start_mcp()
            if self.plugins:
                self._emit("status", f"已加载 {len(self.plugins)} 个插件工具")
            for error in self.plugin_errors:
                self._emit("status", f"插件加载失败：{error}")

            rules = RULE_TEMPLATE.format(
                workspace=str(self.workspace),
                tools=self._tools_text(),
            )
            if self.project_rules.strip():
                rules += "\n\n" + self.project_rules.strip()
            if self.memory.strip():
                rules += "\n\n以下是长期记忆，供参考：\n" + self.memory.strip()
            if self.extra_instructions.strip():
                rules += "\n\n" + self.extra_instructions.strip()

            self.backend.start(rules, stop_event=self.stop_event)
            self._rules = rules
            self._attach_images(self.backend)

            if self.plan_first and self.approver:
                plan = self._ask(f"{PLAN_PROMPT}\n\n任务：{task}").strip()
                if not plan:
                    self._emit("status", "没拿到计划，直接开始执行")
                    reply = self._ask(f"任务：{task}")
                else:
                    self._emit("plan", plan)
                    if not self.approver("plan", {"plan": plan}, self.workspace):
                        self._emit("status", "已取消执行")
                        return "已取消执行"
                    reply = self._ask(
                        f"按下面的计划开始执行：\n{plan}\n\n"
                        f"任务：{task}\n请输出第一步的动作 JSON。"
                    )
            else:
                reply = self._ask(f"任务：{task}")

            for step in range(1, self.max_steps + 1):
                self._check_stop()
                action = self._parse_with_retry(reply)
                thought = action["thought"]
                if thought:
                    self._emit("status", thought[:120])

                if action["action"] == "finish":
                    summary = str(action["args"].get("summary") or "任务结束")
                    self._emit("finish", summary)
                    return summary

                self._emit(
                    "tool",
                    f"[{step}] {action['action']} {json.dumps(action['args'], ensure_ascii=False)}",
                )
                try:
                    result = self._execute_action(action["action"], action["args"])
                except (ToolError, MCPError) as error:
                    result = f"工具出错：{error}"
                    self._emit("status", f"工具出错：{error}")

                reply = self._ask(
                    f"【工具结果】\n{result}\n\n"
                    f"当前任务：{self.task[:300]}\n"
                    "请输出下一步 JSON。"
                )

            self._emit("status", f"已达到最大步数 {self.max_steps}，任务中止")
            return "已达到最大步数，任务中止"
        finally:
            for client in self._clients:
                try:
                    client.close()
                except Exception:
                    pass
            self._clients = []
            try:
                self.backend.close()
            except Exception:
                pass

    # ---- 内部 ----

    def _backend_label(self) -> str:
        if isinstance(self.backend, WebBackend):
            try:
                return f"正在连接 {get_adapter(self.site).label} ..."
            except Exception:
                return "正在连接网页 AI ..."
        return "正在连接模型接口 ..."

    def _start_mcp(self) -> None:
        if self.mode not in ("computer", "hybrid"):
            self._emit("status", "编程模式：不加载 MCP 桌面工具")
            return
        for config in self.mcp_servers:
            name = str(config.get("name") or "").strip()
            command = str(config.get("command") or "").strip()
            if not name or not command:
                self._emit("status", "MCP 配置缺少 name 或 command")
                continue
            try:
                timeout = float(config.get("timeout") or 30)
            except (TypeError, ValueError):
                timeout = 30.0
            client = MCPClient(
                name=name,
                command=command,
                args=config.get("args") if isinstance(config.get("args"), list) else [],
                env=config.get("env") if isinstance(config.get("env"), dict) else {},
                timeout=timeout,
            )
            try:
                client.start()
                tools = client.list_tools()
            except (MCPError, OSError) as error:
                self._emit("status", f"MCP [{name}] 启动失败：{error}")
                try:
                    client.close()
                except Exception:
                    pass
                continue

            self._clients.append(client)
            prefix = f"{name}__"
            for spec in tools:
                tool_name = str(spec.get("name") or "")
                if not tool_name:
                    continue
                qualified = prefix + tool_name
                self._mcp_tools[qualified] = {
                    "client": client,
                    "original": tool_name,
                    # 只有真正的布尔 true 才自动放行，字符串 "false" 不能当成真
                    "auto_approve": (
                        config.get("autoApprove") is True or config.get("auto_approve") is True
                    ),
                    "spec": spec,
                }
            description = describe_tools_for_prompt(tools, prefix)
            if description:
                self._mcp_descriptions.append(description)
            self._emit(
                "status",
                f"MCP [{name}] 已连接，加载 {len(tools)} 个工具"
                + (f"（{client.server_info.get('name')}）" if client.server_info else ""),
            )

    def _tools_text(self) -> str:
        specs = [spec for spec in tool_specs() if spec["name"] in self.enabled_tools]
        text = describe_tools(specs)
        if self.plugins:
            text += "\n\n插件工具（本地 .eagent/tools 扩展）：\n" + describe_plugins(self.plugins)
        if self._mcp_descriptions:
            text += "\n\nMCP 工具（名称带 server__ 前缀）：\n" + "\n".join(self._mcp_descriptions)
        return text

    def _execute_action(self, name: str, args: dict) -> str:
        if name in self.enabled_tools:
            try:
                result = execute(
                    name,
                    args,
                    self.workspace,
                    approver=self.approver,
                    auto_allow=self.auto_allow,
                    before_write=self._snapshot_write,
                    on_start=self._command_start,
                    on_output=self._command_output,
                    stop_event=self.stop_event,
                )
            except (ToolError, MCPError):
                if name == "run_command":
                    self._emit("command_end", "命令失败")
                raise
            if name == "run_command":
                self._emit("command_end", result.split("\n", 1)[0])
            if name == "screen_capture":
                match = re.search(r"截图已保存：(.+?)（", result)
                if match:
                    self._emit("image", self._relative_path(match.group(1)))
            return result

        info = self._mcp_tools.get(name)
        if not info:
            raise ToolError(f"当前模式下不可用的工具: {name}")

        if not self.auto_allow and not info["auto_approve"]:
            if not self.approver or not self.approver(name, args, self.workspace):
                raise ToolError("操作被用户拒绝，请换一种方式或询问用户")

        shots = self.workspace / ".eagent" / "screenshots"
        result = info["client"].call_tool(
            info["original"],
            args,
            image_dir=shots,
            on_image=lambda path: self._emit("image", self._relative_path(path)),
        )
        prune(shots, 50, "*.png")
        return result

    def _command_start(self, name: str, args: dict) -> None:
        if name == "run_command":
            self._emit("command_start", str(args.get("command") or ""))

    def _command_output(self, line: str) -> None:
        self._emit("command_output", line)

    def _snapshot_write(self, name: str, args: dict) -> None:
        """改文件前把原内容存进本次任务快照，便于一键回滚。"""
        if not self.snapshot_id:
            return
        if name == "office_document":
            if str(args.get("action") or "").lower() != "write":
                return
        elif name not in ("write_file", "replace_in_file"):
            return
        try:
            target = safe_path(self.workspace, str(args.get("path") or ""))
        except ToolError:
            return
        snapshot_backup(self.workspace, self.snapshot_id, target)

    def _relative_path(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.workspace)).replace("\\", "/")
        except (ValueError, OSError):
            return path

    def _ask(self, message: str) -> str:
        self._check_stop()
        self._maybe_compact()
        try:
            reply = self.backend.ask(message, stop_event=self.stop_event)
        except AdapterError as error:
            if self.stop_event.is_set():
                raise StopRequested() from error
            reply = self._retry_with_fallback(message, error, adapter=True)
        except BackendError as error:
            if self.stop_event.is_set():
                raise StopRequested() from error
            reply = self._retry_with_fallback(message, error, adapter=False)
        self._remember(message, reply)
        return reply

    def _attach_images(self, backend) -> None:
        if not self.images:
            return
        attach = getattr(backend, "attach_images", None)
        if attach is None or not attach(self.images):
            self._emit("status", "当前模型不支持读图，图片只在本地保留")

    def _retry_with_fallback(self, message: str, error: Exception, adapter: bool) -> str:
        """主模型失败时切到备用模型，把已有进展带过去继续。"""
        if not self._switch_backend():
            if adapter:
                raise AdapterError(
                    f"{error}\n请确认弹出的 Edge 窗口已登录 {get_adapter(self.site).label}"
                ) from error
            raise error

        progress = "\n\n".join(self._transcript)[-6000:]
        payload = message if not progress else f"【此前进展】\n{progress}\n\n{message}"
        try:
            self.backend.start(self._rules, stop_event=self.stop_event)
            self._attach_images(self.backend)
            return self.backend.ask(payload, stop_event=self.stop_event)
        except AdapterError as retry_error:
            if self.stop_event.is_set():
                raise StopRequested() from retry_error
            raise AdapterError(
                f"备用模型也不可用：{retry_error}\n请确认已登录备用站点或接口配置正确"
            ) from retry_error
        except BackendError as retry_error:
            if self.stop_event.is_set():
                raise StopRequested() from retry_error
            raise BackendError(f"备用模型也不可用：{retry_error}") from retry_error

    def _switch_backend(self) -> bool:
        if self._switched or not self.fallback_config:
            return False
        try:
            backend = build_backend(
                self.fallback_config,
                self.session,
                on_status=lambda text: self._emit("status", text),
            )
        except BackendError as error:
            self._emit("status", f"备用模型不可用：{error}")
            return False
        try:
            self.backend.close()
        except Exception:
            pass
        self.backend = backend
        self._switched = True
        config = self.fallback_config
        label = config.get("site") or config.get("model") or config.get("provider") or "备用模型"
        self._emit("status", f"主模型不可用，已切换到 {label}")
        return True

    # ---- 上下文压缩 ----

    def _remember(self, message: str, reply: str) -> None:
        self._transcript.append("我：" + message)
        self._transcript.append("AI：" + reply)
        self._history_chars += len(message) + len(reply)

    def _maybe_compact(self) -> None:
        if self._history_chars < COMPACT_THRESHOLD:
            return
        if self._compact_count >= COMPACT_MAX_TIMES:
            self._drop_oldest()
            return

        self._emit("status", "上下文较长，正在压缩历史…")
        snapshot = "\n\n".join(self._transcript)[-COMPACT_THRESHOLD * 2 :]
        try:
            summary = self.backend.ask(
                COMPACT_PROMPT + snapshot, stop_event=self.stop_event
            )
        except Exception:
            # 摘要失败就退化成丢弃更早的记录，避免上下文继续膨胀
            self._drop_oldest()
            return

        summary = (summary or "").strip()
        if not summary:
            self._drop_oldest()
            return

        try:
            self.backend.compact(summary)
        except Exception:
            pass
        self._compact_count += 1
        self._transcript = ["【前情摘要】\n" + summary]
        self._history_chars = len(summary)
        self._emit("status", "上下文已压缩，继续执行")

    def _drop_oldest(self) -> None:
        """兜底：只留最近几轮，保证还有上下文可推进任务。"""
        self._transcript = self._transcript[-6:]
        self._history_chars = sum(len(item) for item in self._transcript)

    def _parse_with_retry(self, reply: str) -> dict:
        for _ in range(MAX_RETRIES + 1):
            try:
                return parse_action(reply)
            except ValueError as error:
                self._emit("status", f"模型输出不是合法动作（{error}），重试中")
                reply = self._ask(
                    "你刚才的输出无法解析：\n"
                    + reply[:500]
                    + "\n请只输出一个 JSON 对象，包含 thought、action、args 三个字段。"
                )
        raise BackendError("模型连续输出非法 JSON，任务无法继续")

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested("用户已停止任务")
