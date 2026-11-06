"""OpenAI 兼容后端：图片上下文与历史裁剪。"""

from __future__ import annotations

import base64
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge import native_backends as nb  # noqa: E402
from eagent_bridge.backends import OpenAICompatBackend, build_backend  # noqa: E402
from eagent_bridge.native_backends import AnthropicBackend, GeminiBackend  # noqa: E402
from eagent_bridge.session import Session  # noqa: E402

ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def make_backend() -> OpenAICompatBackend:
    return OpenAICompatBackend("http://127.0.0.1:11434/v1", "", "test-model")


class HistoryTests(unittest.TestCase):
    def test_strip_images_replaces_base64(self):
        backend = make_backend()
        backend.messages = [
            {"role": "system", "content": "rules"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}},
                ],
            },
            {"role": "assistant", "content": "ok"},
        ]

        backend._strip_images()

        content = backend.messages[1]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("看这张图", content)
        self.assertIn("1 张图片已省略", content)
        self.assertNotIn("base64", content)

    def test_content_size_counts_image_url(self):
        size = OpenAICompatBackend._content_size(
            [
                {"type": "text", "text": "abc"},
                {"type": "image_url", "image_url": {"url": "x" * 100}},
            ]
        )
        self.assertGreaterEqual(size, 103)

    def test_trim_uses_real_content_size(self):
        backend = make_backend()
        backend.messages = [{"role": "system", "content": "rules"}]
        backend.messages += [{"role": "user", "content": "x" * 20000} for _ in range(3)]

        backend._trim()

        self.assertLessEqual(len(backend.messages), 3)

    def test_trim_starts_with_user(self):
        backend = make_backend()
        backend.messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "u" * 1000},
            {"role": "assistant", "content": "a" * 4000},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]

        with mock.patch("eagent_bridge.backends.MAX_HISTORY_CHARS", 4500):
            backend._trim()

        self.assertEqual(backend.messages[0]["role"], "system")
        self.assertEqual(backend.messages[1]["role"], "user")


    def test_attach_images_skips_oversized_files(self):
        backend = make_backend()
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "big.png"
            with image.open("wb") as handle:
                handle.seek(20 * 1024 * 1024 + 1)
                handle.write(b"x")

            self.assertFalse(backend.attach_images([str(image)]))
            self.assertEqual(backend._pending_images, [])


class _NativeHandler(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        if "generateContent" in self.path:
            data = {"candidates": [{"content": {"parts": [{"text": "gemini-ok"}]}}]}
        else:
            data = {"content": [{"type": "text", "text": "anthropic-ok"}]}
        raw = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class NativeBackendTests(unittest.TestCase):
    def setUp(self):
        _NativeHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NativeHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_anthropic_request_and_reply(self):
        backend = AnthropicBackend(base_url=self.base, api_key="k", model="claude-x")
        backend.start("系统规则")

        self.assertEqual(backend.ask("你好"), "anthropic-ok")

        record = _NativeHandler.requests[-1]
        self.assertEqual(record["path"], "/v1/messages")
        self.assertEqual(record["headers"].get("x-api-key"), "k")
        self.assertEqual(record["headers"].get("anthropic-version"), "2023-06-01")
        self.assertEqual(record["body"]["system"], "系统规则")
        self.assertEqual(record["body"]["messages"][0]["content"], "你好")

    def test_anthropic_images_are_only_sent_once(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "a.png"
            image.write_bytes(base64.b64decode(ONE_PIXEL_PNG))
            backend = AnthropicBackend(base_url=self.base, api_key="k", model="m")
            backend.start("")

            self.assertTrue(backend.attach_images([str(image)]))
            backend.ask("看图")
            content = _NativeHandler.requests[-1]["body"]["messages"][0]["content"]
            self.assertTrue(any(block.get("type") == "image" for block in content))

            backend.ask("继续")
            body = _NativeHandler.requests[-1]["body"]
            self.assertNotIn("image", json.dumps(body["messages"]))

    def test_gemini_request_and_reply(self):
        backend = GeminiBackend(
            base_url=self.base + "/v1beta", api_key="k", model="gemini-x"
        )
        backend.start("系统")

        self.assertEqual(backend.ask("你好"), "gemini-ok")

        record = _NativeHandler.requests[-1]
        self.assertEqual(record["path"], "/v1beta/models/gemini-x:generateContent")
        self.assertEqual(record["headers"].get("x-goog-api-key"), "k")
        self.assertEqual(
            record["body"]["systemInstruction"]["parts"][0]["text"], "系统"
        )
        self.assertEqual(record["body"]["contents"][0]["parts"][0]["text"], "你好")


class TrimTests(unittest.TestCase):
    def test_anthropic_trim_starts_with_user(self):
        messages = [
            {"role": "assistant", "content": "a" * 100},
            {"role": "user", "content": "u" * 100},
            {"role": "assistant", "content": "a2" * 100},
            {"role": "user", "content": "u2" * 100},
        ]
        with mock.patch.object(nb, "MAX_HISTORY_CHARS", 50):
            nb._trim_messages(messages)
        self.assertTrue(messages)
        self.assertEqual(messages[0]["role"], "user")

    def test_gemini_trim_starts_with_user(self):
        contents = [
            {"role": "model", "parts": [{"text": "a" * 100}]},
            {"role": "user", "parts": [{"text": "u" * 100}]},
            {"role": "model", "parts": [{"text": "a2" * 100}]},
            {"role": "user", "parts": [{"text": "u2" * 100}]},
        ]
        with mock.patch.object(nb, "MAX_HISTORY_CHARS", 50):
            nb._trim_contents(contents)
        self.assertTrue(contents)
        self.assertEqual(contents[0]["role"], "user")


class BuildBackendTests(unittest.TestCase):
    def test_native_providers(self):
        session = Session()
        anthropic = build_backend(
            {"provider": "anthropic", "apiKey": "k", "model": "m"}, session
        )
        gemini = build_backend(
            {"provider": "gemini", "apiKey": "k", "model": "m"}, session
        )
        self.assertIsInstance(anthropic, AnthropicBackend)
        self.assertIsInstance(gemini, GeminiBackend)

    def test_unknown_provider(self):
        with self.assertRaises(Exception):
            build_backend({"provider": "nope"}, Session())


if __name__ == "__main__":
    unittest.main()
