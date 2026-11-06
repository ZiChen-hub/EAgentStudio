"""Chrome DevTools Protocol 客户端与 Edge 启动器。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .ws import WebSocket


class CDPError(RuntimeError):
    """CDP 调用错误。"""


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_edge() -> str | None:
    for path in EDGE_PATHS:
        if os.path.exists(path):
            return path
    return shutil.which("msedge")


def _http(port: int, path: str, method: str = "GET", timeout: float = 3.0) -> str:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def debug_version(port: int) -> dict | None:
    """调试端口是否可用，可用时返回 /json/version 信息。"""
    try:
        return json.loads(_http(port, "/json/version", timeout=2.0))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def list_targets(port: int) -> list[dict]:
    try:
        return json.loads(_http(port, "/json/list")) or []
    except (urllib.error.URLError, OSError, ValueError):
        return []


def create_target(port: int, url: str) -> dict:
    quoted = urllib.parse.quote(url, safe="")
    last_error: Exception | None = None
    for method in ("PUT", "GET"):
        try:
            return json.loads(_http(port, f"/json/new?{quoted}", method=method))
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code in (405, 501):
                continue
            raise CDPError(f"创建标签页失败: {error}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise CDPError(f"创建标签页失败: {error}") from error
    raise CDPError(f"创建标签页失败: {last_error}")


def find_site_target(port: int, site_url: str) -> dict | None:
    host = urllib.parse.urlparse(site_url).netloc
    for target in list_targets(port):
        if target.get("type") != "page":
            continue
        if host and host in (target.get("url") or ""):
            return target
    return None


def launch_edge(port: int, profile_dir: str, url: str, headless: bool = False) -> subprocess.Popen:
    executable = find_edge()
    if not executable:
        raise CDPError("未找到 Edge，请确认已安装 Microsoft Edge")
    os.makedirs(profile_dir, exist_ok=True)
    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # 网页 AI 依赖页面里的 JS 定时器（例如 PoW 计算），
        # 窗口在后台时 Chrome 会把定时器节流到分钟级，表现为“发了消息一直没回复”。
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)

    # 浏览器必须比启动它的脚本活得更久：脱离父控制台，并尝试脱离作业对象，
    # 否则命令行窗口结束时 Edge 会被一起终止。
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

    try:
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError:
        # 作业对象不允许 breakaway 时退回普通分离模式
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )


def wait_debug_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if debug_version(port):
            return True
        time.sleep(0.3)
    return False


class CDP:
    """单标签页的 CDP 连接，同步请求/响应。"""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self._ws = WebSocket(ws_url, timeout=timeout)
        self._next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._next_id += 1
        message_id = self._next_id
        self._ws.settimeout(timeout)
        self._ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._ws.recv_text())
            if message.get("id") != message_id:
                # 事件或其它响应，直接丢弃
                continue
            if "error" in message:
                error = message["error"]
                raise CDPError(f"{method} 失败: {error.get('message', error)}")
            return message.get("result", {})

    def close(self) -> None:
        self._ws.close()


class Page:
    """标签页操作封装。"""

    def __init__(self, cdp: CDP):
        self.cdp = cdp

    def evaluate(self, expression: str, await_promise: bool = False):
        result = self.cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        details = result.get("exceptionDetails")
        if details:
            exception = details.get("exception") or {}
            description = exception.get("description") or details.get("text")
            raise CDPError(f"页面脚本执行出错: {description}")
        return (result.get("result") or {}).get("value")

    def wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.evaluate("document.readyState")
            if state in ("interactive", "complete"):
                return True
            time.sleep(0.3)
        return False

    def navigate(self, url: str, wait: bool = True) -> None:
        self.cdp.call("Page.navigate", {"url": url})
        if wait:
            self.wait_ready()

    def insert_text(self, text: str) -> None:
        self.cdp.call("Input.insertText", {"text": text})

    def press_enter(self) -> None:
        self.cdp.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
                "text": "\r",
                "unmodifiedText": "\r",
            },
        )
        self.cdp.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )

    def click(self, x: int, y: int) -> None:
        """在页面坐标处发送一次真实的鼠标左键点击。"""
        self.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
        )
        self.cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1},
        )

    def screenshot(self, image_format: str = "png") -> str:
        """返回 base64 图片数据。"""
        result = self.cdp.call("Page.captureScreenshot", {"format": image_format}, timeout=60)
        return result["data"]

    def title(self) -> str:
        return self.evaluate("document.title") or ""

    def url(self) -> str:
        return self.evaluate("location.href") or ""
