"""EAgent Studio 界面自动化检查。

启动一个临时工作目录和本地服务，用无头 Edge 打开界面，执行 tests/ui_suite.js
里的检查项，然后把界面状态、布局尺寸、服务端副作用和日志汇总成报告。

用法：
    python tests/ui_check.py
    python tests/ui_check.py --keep --timeout 180
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eagent_bridge.cdp import (  # noqa: E402
    CDP,
    Page,
    create_target,
    find_site_target,
    launch_edge,
    wait_debug_port,
)
from eagent_bridge.proc import kill_tree  # noqa: E402
from eagent_bridge.http_api import create_app_server  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def make_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text(
        "def hello():\n"
        "    return 'hi'\n"
        "\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self):\n"
        "        return hello()\n",
        encoding="utf-8",
    )
    (workspace / "edit-me.txt").write_text("original\n", encoding="utf-8")
    notes = workspace / "notes"
    notes.mkdir()
    (notes / "中文 说明.txt").write_text("中文内容\n", encoding="utf-8")

    eagent = workspace / ".eagent"
    skills = eagent / "skills"
    skills.mkdir(parents=True)
    (skills / "demo.md").write_text(
        "---\nname: 演示技能\ndescription: UI 测试用\n---\n\n按步骤执行\n",
        encoding="utf-8",
    )
    rules = eagent / "rules"
    rules.mkdir()
    (rules / "demo.md").write_text(
        "---\nname: 演示规则\ndescription: UI 测试用\n---\n\n保持简洁\n",
        encoding="utf-8",
    )
    (eagent / "mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "echo", "args": ["hi"]}}}),
        encoding="utf-8",
    )
    (eagent / "memory.md").write_text("- 项目使用 Python\n", encoding="utf-8")

    snapshot = eagent / "backups" / "ui-demo"
    (snapshot / "files").mkdir(parents=True)
    (snapshot / "files" / "edit-me.txt").write_text("original\n", encoding="utf-8")
    (snapshot / "snapshot.json").write_text(
        json.dumps(
            {
                "id": "ui-demo",
                "task": "演示快照",
                "created": time.time(),
                "finished": time.time(),
                "summary": "",
                "files": [{"path": "edit-me.txt", "existed": True}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    sessions = eagent / "sessions"
    sessions.mkdir()
    (sessions / "ui-demo.json").write_text(
        json.dumps(
            {
                "id": "ui-demo",
                "task": "历史任务",
                "summary": "历史摘要",
                "created": time.time(),
                "events": [{"type": "agent", "kind": "assistant", "text": "历史回复"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return workspace


def wait_boot(page: Page, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    expression = (
        "!!(window.monaco && document.querySelector('#workspace')"
        " && document.querySelector('#workspace').value"
        " && document.querySelectorAll('#tree .node').length > 0)"
    )
    while time.time() < deadline:
        try:
            if page.evaluate(expression):
                return
        except Exception as error:  # noqa: BLE001
            last_error = error
        time.sleep(0.4)
    raise RuntimeError(f"等待界面启动超时：{last_error}")


def measure_layout(page: Page) -> dict:
    return page.evaluate(
        """(() => {
          const rect = (selector) => {
            const element = document.querySelector(selector);
            if (!element) return null;
            const box = element.getBoundingClientRect();
            return { width: box.width, height: box.height };
          };
          return {
            viewport: { width: window.innerWidth, height: window.innerHeight },
            topbar: rect(".topbar"),
            sidebar: rect(".sidebar"),
            chat: rect(".chat"),
            terminal: rect(".terminal"),
            overflow: document.documentElement.scrollWidth - window.innerWidth,
          };
        })()"""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="EAgent Studio 界面自动化检查")
    parser.add_argument("--timeout", type=float, default=180.0, help="界面套件最长执行秒数")
    parser.add_argument("--keep", action="store_true", help="保留临时工作目录和浏览器配置")
    parser.add_argument("--report", default="", help="报告输出目录，默认 .eagent/ui-test/<时间>")
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="eagent-ui-"))
    workspace = make_workspace(temp_root)
    profile = temp_root / "edge-profile"
    report_dir = (
        Path(args.report)
        if args.report
        else ROOT / ".eagent" / "ui-test" / time.strftime("%Y%m%d-%H%M%S")
    )
    report_dir.mkdir(parents=True, exist_ok=True)

    server = None
    state = None
    process = None
    page = None
    checks: list[dict] = []
    layout_results: dict = {}
    console_errors: list[str] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        server, state = create_app_server(0, workspace)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        token = server.RequestHandlerClass.token
        url = f"{base}/?token={token}"

        debug_port = free_port()
        process = launch_edge(debug_port, str(profile), url, headless=True)
        if not wait_debug_port(debug_port, 30):
            raise RuntimeError(f"无头 Edge 调试端口 {debug_port} 没有就绪")

        target = find_site_target(debug_port, base) or create_target(debug_port, url)
        page = Page(CDP(target["webSocketDebuggerUrl"]))
        page.wait_ready(20)
        page.cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        if base not in (page.url() or ""):
            page.navigate(url)
        wait_boot(page, 120)

        suite = (Path(__file__).with_name("ui_suite.js")).read_text(encoding="utf-8")
        page.evaluate("window.__uiDone = false; window.__uiResults = null;")
        page.evaluate("(function(){" + suite + "})()")

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if page.evaluate("window.__uiDone === true"):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"界面套件在 {args.timeout:.0f} 秒内没有执行完")

        ui_results = page.evaluate("window.__uiResults || []") or []
        console_errors = page.evaluate("window.__uiConsoleErrors || []") or []
        if not isinstance(ui_results, list) or not ui_results:
            raise RuntimeError("界面套件没有返回结果")

        # 两种窗口尺寸下的布局
        for width, height, label in ((1440, 900, "1440x900"), (1024, 700, "1024x700")):
            page.cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
            time.sleep(0.45)
            metrics = measure_layout(page)
            layout_results[label] = metrics
            record(
                f"布局 {label}：没有横向溢出",
                (metrics.get("overflow") or 0) <= 1,
                f"overflow={metrics.get('overflow')}",
            )
            sidebar = metrics.get("sidebar") or {}
            chat = metrics.get("chat") or {}
            terminal = metrics.get("terminal") or {}
            record(
                f"布局 {label}：三栏尺寸有效",
                (sidebar.get("width") or 0) >= 180
                and (chat.get("width") or 0) >= 280
                and (terminal.get("height") or 0) >= 80,
                f"sidebar={sidebar.get('width')} chat={chat.get('width')} terminal={terminal.get('height')}",
            )
            data = page.screenshot()
            (report_dir / f"layout-{label}.png").write_bytes(base64.b64decode(data))

        page.cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False
        })

        # 服务端副作用
        record(
            "UI 新建的文件已落盘",
            (workspace / "ui-created.txt").is_file(),
            "ui-created.txt",
        )
        saved = ""
        try:
            saved = (workspace / "edit-me.txt").read_text(encoding="utf-8")
        except OSError:
            pass
        record(
            "编辑器保存内容正确",
            saved == "edited by ui suite\n",
            repr(saved[:80]),
        )
        record("Git 仓库已初始化", (workspace / ".git").is_dir(), ".git")

        log_path = workspace / ".eagent" / "logs" / "app.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        record("测试期间没有 HTTP 500", " 500 " not in log_text, "app.log")
        record(
            "测试期间没有服务端异常",
            "Traceback" not in log_text and "[error]" not in log_text,
            "app.log",
        )
        request_count = sum(1 for line in log_text.splitlines() if "HTTP/1.1" in line)
        record("接口调用次数足够", request_count >= 20, f"{request_count} 次请求")

        def api_call(path: str, payload: dict | None = None) -> dict:
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                base + path + ("&" if "?" in path else "?") + "token=" + token,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST" if payload is not None else "GET",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")

        current = api_call("/api/workspace").get("path", "")
        record(
            "工作目录没有被测试切换",
            Path(current).resolve() == workspace.resolve(),
            current,
        )
        terminal = api_call("/api/terminal/exec", {"command": "Get-Location"})
        record(
            "终端目录已恢复",
            Path(str(terminal.get("cwd") or "")).resolve() == workspace.resolve(),
            str(terminal.get("cwd")),
        )

        summary = {
            "ui_ok": sum(1 for item in ui_results if item.get("ok")),
            "ui_failed": sum(1 for item in ui_results if not item.get("ok")),
            "backend_ok": sum(1 for item in checks if item["ok"]),
            "backend_failed": sum(1 for item in checks if not item["ok"]),
        }
        report = {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": str(workspace),
            "url": url.replace(token, "***"),
            "summary": summary,
            "ui": ui_results,
            "server": checks,
            "console_errors": console_errors,
            "layout": layout_results,
        }
        (report_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if log_path.is_file():
            shutil.copy2(log_path, report_dir / "app.log")

        print("=" * 72)
        print(f"界面检查：{summary['ui_ok']} 通过 / {summary['ui_failed']} 失败")
        print(f"服务端检查：{summary['backend_ok']} 通过 / {summary['backend_failed']} 失败")
        print(f"报告目录：{report_dir}")
        failures = [item for item in ui_results if not item.get("ok")]
        failures += [item for item in checks if not item["ok"]]
        for item in failures:
            print(f"  [FAIL] {item['name']} — {item.get('detail', '')}")
        if console_errors:
            print(f"  [INFO] console.error {len(console_errors)} 条，详见 report.json")
        print("=" * 72)
        return 1 if failures else 0
    finally:
        if page is not None:
            try:
                page.cdp.close()
            except Exception:
                pass
        if process is not None:
            kill_tree(process)
        if server is not None:
            server.shutdown()
            server.server_close()
        if state is not None:
            state.close()
        if not args.keep:
            shutil.rmtree(profile, ignore_errors=True)
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            print(f"临时目录：{temp_root}")


if __name__ == "__main__":
    raise SystemExit(main())
