"""原生桌面窗口：用 WebView2 承载界面，不再依赖 Edge 应用模式。

启动：
    python -m eagent_bridge.desktop
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path

from .http_api import DEFAULT_PORT, create_app_server

try:
    import webview
except ImportError:  # pragma: no cover - 依赖缺失时给出可读提示
    webview = None


def _require_webview() -> None:
    if webview is not None:
        return
    message = (
        "缺少 pywebview，无法打开桌面窗口。\n\n"
        "请先执行：python -m pip install -r requirements.txt\n"
        "或改用浏览器模式：python -m eagent_bridge.webui"
    )
    print(message, file=sys.stderr)
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "EAgent Studio", 0x10)
    except Exception:
        pass
    raise SystemExit(1)


class Bridge:
    """暴露给前端的原生能力。"""

    _window = None

    def choose_folder(self) -> str:
        if not self._window:
            return ""
        try:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return ""
            # 单选时 pywebview 返回字符串，多选时才返回列表
            if isinstance(result, (list, tuple)):
                return str(result[0]) if result else ""
            return str(result)
        except Exception:
            return ""


def main() -> None:
    _require_webview()
    port = int(os.environ.get("EAGENT_BRIDGE_WEBUI_PORT", str(DEFAULT_PORT)))
    workspace = Path(os.environ.get("EAGENT_BRIDGE_WORKSPACE") or Path.cwd())

    try:
        server, state = create_app_server(port, workspace)
    except OSError as error:
        ctypes.windll.user32.MessageBoxW(
            0,
            "EAgent Studio 可能已经在运行。\n\n请关闭已有窗口，或设置 EAGENT_BRIDGE_WEBUI_PORT 换一个端口。\n\n"
            + str(error),
            "EAgent Studio",
            0x10,
        )
        raise SystemExit(1) from error

    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}/?token={getattr(server.RequestHandlerClass, 'token', '')}"
    print(f"EAgent Studio 已启动: {url}", file=sys.stderr)
    print(f"工作目录: {state.workspace}", file=sys.stderr)

    bridge = Bridge()
    window = webview.create_window(
        "EAgent Studio",
        url,
        width=1440,
        height=920,
        min_size=(1080, 700),
        background_color="#f5f5f7",
        js_api=bridge,
    )
    bridge._window = window
    try:
        webview.start(debug=bool(os.environ.get("EAGENT_BRIDGE_DEBUG")))
    finally:
        server.shutdown()
        server.server_close()
        state.close()


if __name__ == "__main__":
    main()
