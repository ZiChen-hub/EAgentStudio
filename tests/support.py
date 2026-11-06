"""测试支撑：临时工作目录 + 真实本地服务，不依赖浏览器或模型。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eagent_bridge.webui import create_app_server  # noqa: E402


class ServerCase:
    """用标准库起一个真实 HTTP 服务，测试结束自动关闭。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "sample.py").write_text(
            "def hello():\n"
            "    return 'hi'\n"
            "\n"
            "\n"
            "class Greeter:\n"
            "    def greet(self):\n"
            "        return hello()\n",
            encoding="utf-8",
        )
        self.server, self.state = create_app_server(0, self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.server.RequestHandlerClass.token

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.state.close()
        self._tmp.cleanup()

    def api(self, method: str, path: str, payload=None, token: bool = True, headers=None):
        url = self.base + path
        if token:
            url += ("&" if "?" in path else "?") + "token=" + self.token
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        request = urllib.request.Request(url, data=data, method=method, headers=merged)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return response.status, json.loads(body or b"{}")
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                return error.code, json.loads(body or b"{}")
            except ValueError:
                return error.code, {"raw": body.decode("utf-8", "replace")}
