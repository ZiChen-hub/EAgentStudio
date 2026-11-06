"""会话管理：负责启动浏览器、复用标签页、串起适配器。"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from urllib.parse import urlparse

from .adapters import (
    AdapterError,
    WebAIAdapter,
    ask as adapter_ask,
    get_adapter,
    snapshot,
    start_new_chat,
    wait_input,
)
from .cdp import (
    CDP,
    CDPError,
    Page,
    create_target,
    debug_version,
    find_site_target,
    launch_edge,
    wait_debug_port,
)
from .ws import WebSocketError

_DEFAULT_PROFILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EAgentBridge", "edge-profile"
)

_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def _cookie_file(profile_dir: str) -> str:
    return os.path.join(profile_dir, "eagent-cookies.json")


def _domain_matches(domain: str, host: str) -> bool:
    """只保留目标站点相关的 Cookie，避免把其它站点的登录态也写进明文文件。"""
    cleaned = (domain or "").lstrip(".").lower()
    target = (host or "").lower()
    if not cleaned:
        return True
    return target == cleaned or target.endswith("." + cleaned)


def _locked(method):
    """同一个浏览器连接只允许一个调用方使用，避免 CDP 响应互相抢占。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def restore_cookies(page: Page, profile_dir: str) -> int:
    """把上次保存的登录态写回浏览器，避免每次重启都要重新登录。"""
    path = _cookie_file(profile_dir)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            cookies = json.load(handle)
    except (OSError, ValueError):
        return 0
    if not cookies:
        return 0
    try:
        page.cdp.call("Network.setCookies", {"cookies": cookies})
    except CDPError:
        return 0
    return len(cookies)


def save_cookies(page: Page, profile_dir: str, site_url: str = "") -> int:
    """保存目标站点的 Cookie。浏览器被强制结束时，磁盘上的会话 Cookie 可能来不及落盘。"""
    try:
        result = page.cdp.call("Network.getAllCookies")
    except CDPError:
        return 0
    host = urlparse(site_url).hostname if site_url else ""
    cookies = []
    for cookie in result.get("cookies") or []:
        if host and not _domain_matches(str(cookie.get("domain") or ""), host):
            continue
        item = {key: cookie[key] for key in _COOKIE_KEYS if key in cookie}
        if isinstance(item.get("expires"), (int, float)) and item["expires"] < 0:
            item.pop("expires")
        cookies.append(item)
    if not cookies:
        return 0
    try:
        os.makedirs(profile_dir, exist_ok=True)
        with open(_cookie_file(profile_dir), "w", encoding="utf-8") as handle:
            json.dump(cookies, handle, ensure_ascii=False)
    except OSError:
        return 0
    return len(cookies)


class Session:
    """一个 MCP 进程内维护的浏览器会话。"""

    def __init__(self) -> None:
        self.port = int(os.environ.get("EAGENT_BRIDGE_PORT", "9222"))
        self.profile_dir = os.environ.get("EAGENT_BRIDGE_PROFILE", _DEFAULT_PROFILE)
        self.min_interval = float(os.environ.get("EAGENT_BRIDGE_MIN_INTERVAL", "0") or 0)
        self._last_ask_end = 0.0
        self.last_throttle_seconds = 0.0
        self._adapter: WebAIAdapter | None = None
        self._page: Page | None = None
        self._lock = threading.RLock()

    # ---- 对外接口 ----

    def _alive(self) -> bool:
        """缓存的页面是否还能用：浏览器被关掉或标签页被刷掉时返回 False。"""
        if self._page is None:
            return False
        try:
            return self._page.evaluate("1") == 1
        except Exception:
            self._page = None
            return False

    @_locked
    def ensure(self, site: str | None = None) -> tuple[WebAIAdapter, Page]:
        adapter = get_adapter(site)
        if self._adapter is not None and self._adapter.key == adapter.key and self._alive():
            return self._adapter, self._page

        if not debug_version(self.port):
            launch_edge(self.port, self.profile_dir, adapter.url)
            if not wait_debug_port(self.port, 20.0):
                raise CDPError(
                    "Edge 已启动但调试端口未就绪，请检查是否有其它程序占用端口 "
                    f"{self.port}，或设置环境变量 EAGENT_BRIDGE_PORT"
                )

        target = find_site_target(self.port, adapter.url) or create_target(self.port, adapter.url)
        page = Page(CDP(target["webSocketDebuggerUrl"]))
        page.wait_ready(timeout=15.0)
        if restore_cookies(page, self.profile_dir):
            # Cookie 写回后需要重新加载，单页应用才会读到登录态
            page.navigate(adapter.url)
        self._adapter, self._page = adapter, page
        return adapter, page

    @_locked
    def ask(
        self,
        prompt: str,
        site: str | None = None,
        timeout: float = 180.0,
        on_status=None,
        stop_event=None,
    ) -> str:
        self.last_throttle_seconds = self._throttle()
        try:
            adapter, page = self.ensure(site)
            try:
                reply = adapter_ask(
                    page, adapter, prompt, timeout=timeout,
                    on_status=on_status, stop_event=stop_event,
                )
            except (WebSocketError, OSError, CDPError) as error:
                # 浏览器可能被关掉或标签页被刷掉，重建一次再试
                self._page = None
                adapter, page = self.ensure(site)
                try:
                    reply = adapter_ask(
                        page, adapter, prompt, timeout=timeout,
                        on_status=on_status, stop_event=stop_event,
                    )
                except Exception:
                    raise AdapterError(f"与浏览器通信失败，重试也未成功: {error}") from error
            save_cookies(page, self.profile_dir, adapter.url)
            return reply
        finally:
            self._last_ask_end = time.monotonic()

    @_locked
    def new_chat(self, site: str | None = None, stop_event=None) -> dict:
        try:
            adapter, page = self.ensure(site)
            info = start_new_chat(page, adapter, stop_event=stop_event)
        except (WebSocketError, OSError, CDPError) as error:
            # 浏览器刚被关掉或标签页失效：重建一次再试
            self._page = None
            adapter, page = self.ensure(site)
            try:
                info = start_new_chat(page, adapter, stop_event=stop_event)
            except Exception:
                raise AdapterError(f"与浏览器通信失败，重试也未成功: {error}") from error
        save_cookies(page, self.profile_dir, adapter.url)
        return {"site": adapter.label, **info}

    def _throttle(self) -> float:
        """上一轮提问结束后再计算间隔；默认 0，不强制等待。"""
        if self.min_interval <= 0:
            return 0.0
        remaining = self._last_ask_end + self.min_interval - time.monotonic()
        if remaining <= 0:
            return 0.0
        time.sleep(remaining)
        return remaining

    @_locked
    def new_session(self, site: str | None = None, wait_login: float = 30.0) -> dict:
        adapter, page = self.ensure(site)
        ready = wait_input(page, adapter, wait_login)
        if ready:
            save_cookies(page, self.profile_dir, adapter.url)
        state = snapshot(page, adapter)
        return {
            "site": adapter.label,
            "url": state.get("url") or adapter.url,
            "ready": ready,
            "hint": (
                "页面已就绪"
                if ready
                else "未检测到输入框：请在打开的浏览器窗口完成登录后再次调用 webai_ask"
            ),
        }

    @_locked
    def status(self) -> dict:
        if self._page is None or self._adapter is None:
            return {"session": False, "port": self.port, "hint": "还没有连接浏览器，先调用 webai_new_session"}
        try:
            state = snapshot(self._page, self._adapter)
        except Exception as error:
            return {"session": False, "port": self.port, "error": str(error)}
        return {
            "session": True,
            "port": self.port,
            "site": self._adapter.label,
            "url": state.get("url"),
            "ready": bool(state.get("input")),
        }

    def navigate(self, url: str) -> dict:
        adapter, page = self.ensure(None)
        page.navigate(url)
        return {"url": page.url(), "title": page.title()}

    @_locked
    def screenshot(self) -> str:
        _, page = self.ensure(None)
        return page.screenshot()
