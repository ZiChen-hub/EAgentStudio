"""命令行入口：手动试跑与自检。

用法：
    python -m eagent_bridge.cli selftest
    python -m eagent_bridge.cli ask "你好"
    python -m eagent_bridge.cli status
    python -m eagent_bridge.cli serve
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from .cdp import CDP, Page, create_target, debug_version, find_edge, launch_edge, wait_debug_port
from .session import Session


def cmd_ask(prompt: str, site: str, timeout: float) -> int:
    session = Session()
    print(f"正在连接 {site} ...", file=sys.stderr)
    reply = session.ask(prompt, site, timeout=timeout)
    print(reply)
    return 0


def cmd_status(site: str) -> int:
    session = Session()
    session.ensure(site)
    time.sleep(1.0)
    print(json.dumps(session.status(), ensure_ascii=False, indent=2))
    return 0


def selftest(port: int = 9333) -> int:
    """用一次性的无头 Edge 验证：端口发现 → 建标签页 → 导航 → 执行 JS → 注入文本。"""
    executable = find_edge()
    if not executable:
        print("FAIL: 未找到 Edge", file=sys.stderr)
        return 1

    profile = tempfile.mkdtemp(prefix="eagent-selftest-")
    process = launch_edge(port, profile, "about:blank", headless=True)
    try:
        if not wait_debug_port(port, 20.0):
            print("FAIL: 调试端口未就绪", file=sys.stderr)
            return 1

        target = create_target(port, "about:blank")
        page = Page(CDP(target["webSocketDebuggerUrl"]))
        page.navigate("data:text/html,<title>selftest</title><input id='i'>")
        time.sleep(0.4)

        title = page.title()
        page.evaluate("document.getElementById('i').focus()")
        page.insert_text("hello-bridge")
        value = page.evaluate("document.getElementById('i').value")
        arithmetic = page.evaluate("1 + 1")
        payload_size = page.evaluate("'x'.repeat(200000).length")
        image = page.screenshot()

        checks = {
            "navigate": title == "selftest",
            "evaluate": arithmetic == 2,
            "insert_text": value == "hello-bridge",
            "large_frame": payload_size == 200000,
            "screenshot": len(image) > 1000,
        }
        for name, ok in checks.items():
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
        return 0 if all(checks.values()) else 1
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="eagent-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="向网页 AI 发送一条消息并打印回复")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--site", default="deepseek", choices=["deepseek", "doubao", "yuanbao"])
    ask_parser.add_argument("--timeout", type=float, default=180.0)

    status_parser = subparsers.add_parser("status", help="查看会话状态")
    status_parser.add_argument("--site", default="deepseek", choices=["deepseek", "doubao", "yuanbao"])

    subparsers.add_parser("selftest", help="无头 Edge 自检 CDP 链路")
    subparsers.add_parser("serve", help="以 MCP stdio 方式启动服务")

    args = parser.parse_args()

    if args.command == "selftest":
        return selftest()
    if args.command == "ask":
        return cmd_ask(args.prompt, args.site, args.timeout)
    if args.command == "status":
        return cmd_status(args.site)
    if args.command == "serve":
        from .mcp_server import main as serve
        serve()
        return 0
    return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
