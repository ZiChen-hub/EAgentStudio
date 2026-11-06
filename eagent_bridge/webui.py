"""迷你 Trae：本地 Web UI + Monaco 编辑器 + 网页 AI Agent。

启动：
    python -m eagent_bridge.webui

默认地址 http://127.0.0.1:8766，会自动用 Edge 应用窗口打开。
"""

from __future__ import annotations

import os
import sys

from .http_api import DEFAULT_PORT, create_app_server, default_workspace, open_app_window


def main() -> None:
    workspace = default_workspace()
    try:
        server, state = create_app_server(DEFAULT_PORT, workspace)
    except OSError as error:
        print(
            f"启动失败：端口 {DEFAULT_PORT} 可能已被占用（EAgent Studio 可能已在运行）。"
            f"\n{error}",
            file=sys.stderr,
        )
        return
    token = getattr(server.RequestHandlerClass, "token", "")
    url = f"http://127.0.0.1:{DEFAULT_PORT}/?token={token}"
    print(f"迷你 Trae 已启动: {url}", file=sys.stderr)
    print(f"工作目录: {state.workspace}", file=sys.stderr)

    if not os.environ.get("EAGENT_BRIDGE_NO_WINDOW"):
        if not open_app_window(url):
            print("未找到 Edge，请手动在浏览器打开上面的地址", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止", file=sys.stderr)
    finally:
        server.server_close()
        state.close()


if __name__ == "__main__":
    main()
