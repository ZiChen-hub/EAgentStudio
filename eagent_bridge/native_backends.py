"""原生模型协议：Anthropic Messages 与 Google Gemini generateContent。"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from .backends import IMAGE_MIME, MAX_HISTORY_CHARS, BackendError, ChatBackend


def _content_length(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_content_length(item) for item in content)
    if isinstance(content, dict):
        return sum(_content_length(value) for value in content.values())
    return len(str(content or ""))


def _image_mime(path: Path) -> str:
    return IMAGE_MIME.get(path.suffix.lower(), "image/png")


def _trim_messages(messages: list[dict]) -> None:
    total = sum(_content_length(message.get("content")) for message in messages)
    while total > MAX_HISTORY_CHARS and len(messages) > 2:
        removed = messages.pop(0)
        total -= _content_length(removed.get("content"))
    # Anthropic 要求对话从 user 开始，裁掉开头可能剩下的 assistant
    while messages and messages[0].get("role") == "assistant":
        messages.pop(0)


def _trim_contents(contents: list[dict]) -> None:
    total = sum(_content_length(message.get("parts")) for message in contents)
    while total > MAX_HISTORY_CHARS and len(contents) > 2:
        removed = contents.pop(0)
        total -= _content_length(removed.get("parts"))
    # Gemini 同样要求从 user 开始
    while contents and contents[0].get("role") == "model":
        contents.pop(0)


def _strip_images(messages: list[dict], image_kind: str, text_kind: str = "text") -> None:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == text_kind
        ]
        images = sum(
            1
            for item in content
            if isinstance(item, dict) and item.get("type") == image_kind
        )
        text = "\n".join(part for part in texts if part)
        if images:
            text = (text + "\n" if text else "") + f"（{images} 张图片已省略）"
        message["content"] = text


class AnthropicBackend(ChatBackend):
    """Anthropic Messages API（Claude）。"""

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        api_key: str = "",
        model: str = "",
        timeout: float = 180.0,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ):
        if not base_url.strip():
            raise BackendError("base_url 不能为空")
        if not model.strip():
            raise BackendError("model 不能为空")
        base = base_url.rstrip("/")
        self.endpoint = base + "/messages" if base.endswith("/v1") else base + "/v1/messages"
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system = ""
        self.messages: list[dict] = []
        self._pending_images: list[dict] = []

    def start(self, rules: str, stop_event=None) -> None:
        self.system = rules
        self.messages = []
        self._pending_images = []

    def attach_images(self, paths) -> bool:
        blocks = []
        for raw in list(paths)[:4]:
            path = Path(raw)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _image_mime(path),
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        if not blocks:
            return False
        self._pending_images = blocks
        return True

    def ask(self, text: str, stop_event=None) -> str:
        if stop_event is not None and stop_event.is_set():
            raise BackendError("已被用户停止")
        if self._pending_images:
            content = [{"type": "text", "text": text}, *self._pending_images]
            self._pending_images = []
        else:
            content = text
        self.messages.append({"role": "user", "content": content})

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self.messages,
        }
        if self.system:
            payload["system"] = self.system
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:300]
            raise BackendError(f"Anthropic 接口返回 {error.code}: {body}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise BackendError(f"无法连接 Anthropic 接口 {self.endpoint}: {error}") from error

        blocks = data.get("content") or []
        reply = "\n".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not reply:
            raise BackendError(f"Anthropic 返回为空: {str(data)[:300]}")
        self.messages.append({"role": "assistant", "content": reply})
        _strip_images(self.messages, "image")
        _trim_messages(self.messages)
        return reply

    def compact(self, summary: str) -> None:
        self.messages = [{"role": "user", "content": "【前情摘要】\n" + summary}]


class GeminiBackend(ChatBackend):
    """Google Gemini generateContent API。"""

    def __init__(
        self,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str = "",
        model: str = "",
        timeout: float = 180.0,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ):
        if not base_url.strip():
            raise BackendError("base_url 不能为空")
        if not model.strip():
            raise BackendError("model 不能为空")
        self.api_key = api_key.strip()
        self.model = model.strip().removeprefix("models/")
        self.endpoint = base_url.rstrip("/") + "/models/" + self.model + ":generateContent"
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system = ""
        self.contents: list[dict] = []
        self._pending_images: list[dict] = []

    def start(self, rules: str, stop_event=None) -> None:
        self.system = rules
        self.contents = []
        self._pending_images = []

    def attach_images(self, paths) -> bool:
        parts = []
        for raw in list(paths)[:4]:
            path = Path(raw)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            parts.append(
                {
                    "inline_data": {
                        "mime_type": _image_mime(path),
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            )
        if not parts:
            return False
        self._pending_images = parts
        return True

    def ask(self, text: str, stop_event=None) -> str:
        if stop_event is not None and stop_event.is_set():
            raise BackendError("已被用户停止")
        parts = [{"text": text}, *self._pending_images]
        self._pending_images = []
        self.contents.append({"role": "user", "parts": parts})

        payload: dict = {"contents": self.contents}
        if self.system:
            payload["systemInstruction"] = {"parts": [{"text": self.system}]}
        generation: dict = {"maxOutputTokens": self.max_tokens}
        if self.temperature is not None:
            generation["temperature"] = self.temperature
        payload["generationConfig"] = generation

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:300]
            raise BackendError(f"Gemini 接口返回 {error.code}: {body}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise BackendError(f"无法连接 Gemini 接口 {self.endpoint}: {error}") from error

        candidates = data.get("candidates") or []
        reply = ""
        if candidates:
            parts_out = (candidates[0].get("content") or {}).get("parts") or []
            reply = "\n".join(
                str(part.get("text") or "")
                for part in parts_out
                if isinstance(part, dict)
            ).strip()
        if not reply:
            feedback = data.get("promptFeedback") or {}
            raise BackendError(
                "Gemini 没有返回内容"
                + (f"（{feedback.get('blockReason')}）" if feedback.get("blockReason") else "")
            )
        self.contents.append({"role": "model", "parts": [{"text": reply}]})

        # 图片只发送一次，之后用文字占位，避免每轮重复上传
        for message in self.contents:
            parts_in = message.get("parts")
            if not isinstance(parts_in, list):
                continue
            texts = [str(part.get("text") or "") for part in parts_in if "text" in part]
            images = sum(1 for part in parts_in if "inline_data" in part)
            if images:
                text_out = "\n".join(item for item in texts if item)
                message["parts"] = [
                    {"text": (text_out + "\n" if text_out else "") + f"（{images} 张图片已省略）"}
                ]
        _trim_contents(self.contents)
        return reply

    def compact(self, summary: str) -> None:
        self.contents = [{"role": "user", "parts": [{"text": "【前情摘要】\n" + summary}]}]
