"""OpenAI 兼容后端：图片上下文与历史裁剪。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.backends import OpenAICompatBackend  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
