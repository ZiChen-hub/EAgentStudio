"""OpenAI 兼容端点：把网页版 AI 伪装成一个本地模型服务。

用法：
    python -m eagent_bridge.server

可用端点：
    GET  /v1/models
    POST /v1/chat/completions   （支持 stream=true 与 tool_calls 模拟）

环境变量：
    EAGENT_BRIDGE_OPENAI_PORT   监听端口，默认 8765
    EAGENT_BRIDGE_API_KEY       可选；设置后要求 Authorization: Bearer <key>
    EAGENT_BRIDGE_MIN_INTERVAL  两次提问之间的最小冷却，默认 0
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .adapters import AdapterError
from .session import Session

MODEL_IDS = ("deepseek-web", "deepseek-web-free")
DEFAULT_TIMEOUT = float(os.environ.get("EAGENT_BRIDGE_TIMEOUT", "180"))
MAX_PROMPT_CHARS = 12000

_TOOL_PREAMBLE = """你在扮演一个工具调用规划器。可用工具如下：

%(tools)s

需要调用工具时，只输出这一种 JSON：
{"type":"tool_call","name":"工具名","arguments":{...}}

不需要调用工具时，只输出这一种 JSON：
{"type":"final","content":"最终回答"}

要求：只输出一个 JSON 对象，不要代码块标记，不要解释文字。"""


_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _repair_escapes(text: str) -> str:
    """模型常把 Windows 路径写成 D:\\FTAPI，\\F 不是合法 JSON 转义，这里补一个反斜杠。"""
    return _BAD_ESCAPE.sub(r"\\\\", text)


def _extract_json(text: str) -> dict | None:
    """从模型回复里尽力抠出一个 JSON 对象。"""
    text = text.strip()
    candidates = [text]

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidates.extend(part.strip() for part in fenced)
    candidates.extend(_repair_escapes(part) for part in list(candidates))

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for start in [index for index, char in enumerate(candidate) if char == "{"]:
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "\n".join(parts)
    return ""


def _normalize_message(message: dict) -> dict:
    """把 OpenAI 消息压成可比较、可拼接的纯文本形式。"""
    role = message.get("role") or "user"
    text = _content_text(message.get("content"))

    if role == "assistant" and message.get("tool_calls"):
        names = ", ".join(
            (call.get("function") or {}).get("name") or "?" for call in message["tool_calls"]
        )
        marker = f"[调用工具: {names}]"
        text = f"{text}\n{marker}".strip() if text else marker
    if role == "tool":
        text = f"[工具结果] {text}"
    return {"role": role, "content": text}


def _format_role(role: str) -> str:
    return {
        "system": "【系统要求】",
        "user": "【用户】",
        "assistant": "【助手】",
        "tool": "【工具结果】",
    }.get(role, f"【{role}】")


class WebModel:
    """一次进程内的网页模型会话，带多轮增量拼接。"""

    def __init__(self) -> None:
        self.session = Session()
        self.site = os.environ.get("EAGENT_BRIDGE_V1_SITE", "deepseek")
        self._lock = threading.Lock()
        self._history: list[dict] = []

    def complete(self, messages: list[dict], tools: list[dict] | None) -> dict:
        normalized = [_normalize_message(message) for message in messages]
        if not normalized:
            raise ValueError("messages 不能为空")

        with self._lock:
            delta, is_delta = self._take_delta(normalized)
            prompt = self._build_prompt(delta, tools, is_delta)
            reply = self.session.ask(prompt, self.site, timeout=DEFAULT_TIMEOUT)
            result = self._parse_reply(reply, tools)
            self._history = normalized + [result["history_entry"]]
            return result

    # ---- 内部 ----

    def _take_delta(self, messages: list[dict]) -> tuple[list[dict], bool]:
        if (
            self._history
            and len(messages) > len(self._history)
            and messages[: len(self._history)] == self._history
        ):
            return messages[len(self._history) :], True
        self._history = []
        return messages, False

    def _build_prompt(self, messages: list[dict], tools: list[dict] | None, is_delta: bool) -> str:
        blocks = []
        if tools:
            blocks.append(_TOOL_PREAMBLE % {"tools": self._describe_tools(tools)})

        lines = [f"{_format_role(m['role'])}{m['content']}" for m in messages]
        body = "\n".join(lines)
        if len(body) > MAX_PROMPT_CHARS:
            body = body[-MAX_PROMPT_CHARS:]
            body = "（上下文已截断，只保留最近部分）\n" + body
        blocks.append(body)

        if is_delta:
            blocks.append("请针对最后一条消息继续回答。")
        return "\n\n".join(blocks)

    @staticmethod
    def _describe_tools(tools: list[dict]) -> str:
        lines = []
        for tool in tools:
            function = tool.get("function") or {}
            name = function.get("name") or "unknown"
            description = function.get("description") or ""
            schema = json.dumps(function.get("parameters") or {}, ensure_ascii=False)
            lines.append(f"- {name}: {description}\n  参数结构: {schema}")
        return "\n".join(lines)

    def _parse_reply(self, reply: str, tools: list[dict] | None) -> dict:
        text = reply.strip()
        if not tools:
            return {
                "content": text,
                "tool_calls": None,
                "history_entry": {"role": "assistant", "content": text},
            }

        payload = _extract_json(text)
        allowed = {(tool.get("function") or {}).get("name") for tool in tools}

        if (
            isinstance(payload, dict)
            and payload.get("type") == "tool_call"
            and payload.get("name") in allowed
        ):
            name = payload["name"]
            arguments = payload.get("arguments") or {}
            if not isinstance(arguments, (dict, str)):
                arguments = {}
            arguments_json = (
                arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
            )
            call = {
                "id": f"call_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {"name": name, "arguments": arguments_json},
            }
            return {
                "content": None,
                "tool_calls": [call],
                "history_entry": {"role": "assistant", "content": f"[调用工具: {name}]"},
            }

        if isinstance(payload, dict) and payload.get("type") == "final":
            content = payload.get("content") or ""
            return {
                "content": content,
                "tool_calls": None,
                "history_entry": {"role": "assistant", "content": content},
            }

        # 模型没有按协议输出，降级为普通文本，避免宿主直接报错
        return {
            "content": text,
            "tool_calls": None,
            "history_entry": {"role": "assistant", "content": text},
        }

    def reset(self) -> None:
        with self._lock:
            self._history = []


class Handler(BaseHTTPRequestHandler):
    model: WebModel  # 由 create_server 注入
    api_key: str | None = None

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", file=sys.stderr)

    # ---- 路由 ----

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "owned_by": "eagent-bridge"}
                        for model in MODEL_IDS
                    ],
                },
            )
            return
        if self.path.rstrip("/") in ("/health", "/"):
            self._json(200, {"status": "ok", "models": list(MODEL_IDS)})
            return
        self._error(404, "not_found", f"未知路径: {self.path}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._error(404, "not_found", f"未知路径: {self.path}")
            return

        if not self._authorized():
            self._error(401, "invalid_api_key", "Authorization 校验失败")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            self._error(400, "invalid_request_error", f"请求体不是合法 JSON: {error}")
            return

        messages = body.get("messages") or []
        tools = body.get("tools") if isinstance(body.get("tools"), list) else None
        model = body.get("model") or MODEL_IDS[0]
        stream = bool(body.get("stream"))

        try:
            result = self.model.complete(messages, tools)
        except (AdapterError, TimeoutError, ValueError) as error:
            self._error(502, "upstream_error", str(error))
            return
        except Exception as error:  # noqa: BLE001
            self.log_error("调用网页 AI 失败: %s", error)
            self._error(500, "internal_error", str(error))
            return

        if stream:
            self._stream_result(model, result)
        else:
            self._completion(model, result)

    # ---- 响应 ----

    def _authorized(self) -> bool:
        if not self.api_key:
            return True
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {self.api_key}"

    def _completion(self, model: str, result: dict) -> None:
        message = {"role": "assistant", "content": result["content"]}
        finish_reason = "stop"
        if result["tool_calls"]:
            message["tool_calls"] = result["tool_calls"]
            finish_reason = "tool_calls"
        self._json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def _stream_result(self, model: str, result: dict) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(delta: dict, finish_reason: str | None = None) -> None:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            if result["tool_calls"]:
                call = result["tool_calls"][0]
                emit(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": call["function"]["name"], "arguments": ""},
                            }
                        ],
                    }
                )
                emit(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": call["function"]["arguments"]}}
                        ]
                    }
                )
                emit({}, "tool_calls")
            else:
                emit({"role": "assistant", "content": ""})
                text = result["content"] or ""
                for start in range(0, len(text), 48):
                    emit({"content": text[start : start + 48]})
                emit({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": code, "code": code}})

    def _cors(self) -> None:
        # 没配 API key 时不放开跨域，避免任意网页通过浏览器调用本机模型代理
        if not self.api_key:
            return
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def create_server(port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    model = WebModel()
    handler = type("EAgentHandler", (Handler,), {"model": model, "api_key": os.environ.get("EAGENT_BRIDGE_API_KEY")})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    port = int(os.environ.get("EAGENT_BRIDGE_OPENAI_PORT", "8765"))
    server = create_server(port)
    print(f"OpenAI 兼容端点已启动: http://127.0.0.1:{port}/v1", file=sys.stderr)
    print(f"模型名: {', '.join(MODEL_IDS)}", file=sys.stderr)
    if not os.environ.get("EAGENT_BRIDGE_API_KEY"):
        print(
            "提示：未设置 EAGENT_BRIDGE_API_KEY，已关闭跨域访问；本机命令行客户端仍可直接调用。",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止", file=sys.stderr)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
