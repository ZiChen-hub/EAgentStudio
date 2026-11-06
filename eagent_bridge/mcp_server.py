"""MCP stdio 服务：把网页 AI 会话暴露成工具供 Trae / WorkBuddy 等宿主调用。"""

from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .adapters import AdapterError
from .cdp import CDPError
from .session import Session

PROTOCOL_VERSION = "2024-11-05"

SITES = ["deepseek", "doubao", "yuanbao"]

TOOLS = [
    {
        "name": "webai_new_session",
        "description": "打开（或复用）一个网页 AI 对话页，例如 DeepSeek / 豆包 / 元宝。首次使用需要在弹出的 Edge 窗口里手动登录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "enum": SITES,
                    "default": "deepseek",
                    "description": "使用哪个网页版 AI",
                }
            },
        },
    },
    {
        "name": "webai_ask",
        "description": "把一条消息发送到当前网页 AI 对话框，并等待它回复完成，返回回复文本。适合让网页 AI 充当免费模型后端。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要发送给网页 AI 的内容"},
                "site": {"type": "string", "enum": SITES, "description": "可选，切换网页 AI 站点"},
                "timeout_seconds": {
                    "type": "number",
                    "default": 180,
                    "description": "等待回复的最长秒数",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "webai_new_chat",
        "description": "在当前网页 AI 站点开启一个全新对话，清空网页端累积的上下文。换话题时先调用它，避免旧对话干扰。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "enum": SITES,
                    "description": "可选，切换网页 AI 站点",
                }
            },
        },
    },
    {
        "name": "webai_status",
        "description": "查看当前浏览器会话状态：是否已连接、是否在目标站点、是否已登录（输入框是否存在）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_navigate",
        "description": "让当前受控标签页跳转到指定 URL，用于打开参考资料或验证产物。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "目标网址"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "截取当前标签页画面，返回 PNG 图片。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(message: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": is_error}


def _call_tool(name: str, args: dict, session: Session) -> dict:
    try:
        if name == "webai_new_session":
            info = session.new_session(args.get("site"))
            return _text(
                f"{info['site']} 已打开：{info['url']}\n{info['hint']}"
            )
        if name == "webai_ask":
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                return _text("参数 prompt 不能为空", True)
            timeout = float(args.get("timeout_seconds") or 180)
            reply = session.ask(prompt, args.get("site"), timeout=timeout)
            waited = getattr(session, "last_throttle_seconds", 0.0)
            if waited >= 1.0:
                reply = f"（为降低风控已等待 {round(waited)} 秒）\n{reply}"
            return _text(reply)
        if name == "webai_new_chat":
            info = session.new_chat(args.get("site"))
            return _text(
                f"{info['site']} 已开启新对话：{info['url']}\n"
                f"（方式：{'点击按钮' if info['clicked'] else '回到首页'}，当前消息数：{info['messages']}）"
            )
        if name == "webai_status":
            return _text(json.dumps(session.status(), ensure_ascii=False, indent=2))
        if name == "browser_navigate":
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                return _text("参数 url 不能为空", True)
            info = session.navigate(url)
            return _text(f"已打开：{info['url']}\n标题：{info['title']}")
        if name == "browser_screenshot":
            data = session.screenshot()
            return {"content": [{"type": "image", "data": data, "mimeType": "image/png"}], "isError": False}
        return _text(f"未知工具: {name}", True)
    except (AdapterError, CDPError) as error:
        return _text(str(error), True)
    except Exception as error:  # noqa: BLE001 - 兜底，避免服务崩溃
        traceback.print_exc(file=sys.stderr)
        return _text(f"工具执行失败: {error}", True)


def _dispatch(message: dict, session: Session) -> dict | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    if message_id is None:  # 通知，不需要响应
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "eagent-bridge", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        result = _call_tool(name, arguments, session)
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"不支持的方法: {method}"},
    }


def main() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    session = Session()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(f"忽略无法解析的行: {line[:80]}", file=sys.stderr)
            continue
        if not isinstance(message, dict):
            print(f"忽略非对象消息: {line[:80]}", file=sys.stderr)
            continue
        response = _dispatch(message, session)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
