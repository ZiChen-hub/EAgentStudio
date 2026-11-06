"""模型后端：网页 AI、OpenAI 兼容接口、Ollama 统一成同一个接口。"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from .adapters import AdapterError
from .session import Session

MAX_HISTORY_CHARS = 24000
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class BackendError(RuntimeError):
    """模型后端调用失败。"""


class ChatBackend:
    def start(self, rules: str, stop_event=None) -> None:
        raise NotImplementedError

    def ask(self, text: str, stop_event=None) -> str:
        raise NotImplementedError

    def compact(self, summary: str) -> None:
        """用摘要替换已有上下文，给长任务腾出空间。"""
        pass

    def attach_images(self, paths) -> bool:
        """把图片挂到下一条用户消息上；不支持图片的后端返回 False。"""
        return False

    def close(self) -> None:
        pass


class WebBackend(ChatBackend):
    """网页版 AI：规则在第一条消息里附上，因为网页端没有 system role。"""

    def __init__(self, session: Session, site: str = "deepseek", on_status=None) -> None:
        self.session = session
        self.site = site
        self.rules = ""
        self._started = False
        self.on_status = on_status

    def start(self, rules: str, stop_event=None) -> None:
        self.rules = rules
        self._started = False
        try:
            self.session.new_chat(self.site, stop_event=stop_event)
        except AdapterError:
            # 登录态或页面异常交给随后的 ask 报错
            pass

    def ask(self, text: str, stop_event=None) -> str:
        message = text if self._started else f"{self.rules}\n\n{text}"
        self._started = True
        return self.session.ask(
            message, self.site, on_status=self.on_status, stop_event=stop_event
        )

    def compact(self, summary: str) -> None:
        # 网页端没有可编辑的历史，只能开新对话并把摘要塞回规则里
        self.rules = (self.rules + "\n\n【前情摘要】\n" + summary).strip()
        self._started = False
        try:
            self.session.new_chat(self.site)
        except AdapterError:
            pass


class OpenAICompatBackend(ChatBackend):
    """任何 OpenAI 兼容接口，包括 Ollama 的 /v1。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = 180.0,
        temperature: float | None = None,
    ):
        if not base_url.strip():
            raise BackendError("base_url 不能为空")
        if not model.strip():
            raise BackendError("model 不能为空")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        self.temperature = temperature
        self.messages: list[dict] = []
        self._pending_images: list[str] = []

    def start(self, rules: str, stop_event=None) -> None:
        self.messages = [{"role": "system", "content": rules}]

    def attach_images(self, paths) -> bool:
        urls = []
        for raw in list(paths)[:4]:
            path = Path(raw)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            mime = IMAGE_MIME.get(path.suffix.lower(), "image/png")
            urls.append(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
        if not urls:
            return False
        self._pending_images = urls
        return True

    def ask(self, text: str, stop_event=None) -> str:
        if stop_event is not None and stop_event.is_set():
            raise BackendError("已被用户停止")
        if self._pending_images:
            content: list[dict] = [{"type": "text", "text": text}]
            content.extend(
                {"type": "image_url", "image_url": {"url": url}} for url in self._pending_images
            )
            self.messages.append({"role": "user", "content": content})
            self._pending_images = []
        else:
            self.messages.append({"role": "user", "content": text})
        payload = {"model": self.model, "messages": self.messages, "stream": False}
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key or 'none'}",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:300]
            raise BackendError(f"模型接口返回 {error.code}: {body}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise BackendError(f"无法连接模型接口 {self.endpoint}: {error}") from error

        try:
            content = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as error:
            raise BackendError(f"模型接口返回格式异常: {str(data)[:300]}") from error

        self.messages.append({"role": "assistant", "content": content})
        # 图片只在本轮请求里发送，之后换成占位文本，避免后续每轮重复上传 base64
        self._strip_images()
        self._trim()
        return content

    @staticmethod
    def _content_size(content) -> int:
        if isinstance(content, list):
            total = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                total += len(str(part.get("text") or ""))
                image = part.get("image_url") or {}
                total += len(str(image.get("url") or ""))
            return total
        return len(str(content or ""))

    def _strip_images(self) -> None:
        for message in self.messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            texts = [str(part.get("text") or "") for part in content if part.get("type") == "text"]
            images = sum(1 for part in content if part.get("type") == "image_url")
            text = "\n".join(item for item in texts if item)
            if images:
                text = (text + "\n" if text else "") + f"（{images} 张图片已省略）"
            message["content"] = text

    def _trim(self) -> None:
        total = sum(self._content_size(message.get("content")) for message in self.messages)
        while total > MAX_HISTORY_CHARS and len(self.messages) > 3:
            removed = self.messages.pop(1)
            total -= self._content_size(removed.get("content"))

    def compact(self, summary: str) -> None:
        system = self.messages[0] if self.messages else {"role": "system", "content": ""}
        self.messages = [
            system,
            {"role": "user", "content": "【前情摘要】\n" + summary},
        ]


def build_backend(config: dict | None, session: Session, on_status=None) -> ChatBackend:
    """按前端传入的配置构造模型后端。"""
    config = config or {}
    provider = str(config.get("provider") or "webai").lower()

    if provider in ("webai", "web", "webai"):
        return WebBackend(session, str(config.get("site") or "deepseek"), on_status=on_status)

    if provider == "ollama":
        return OpenAICompatBackend(
            base_url=str(config.get("baseUrl") or "http://127.0.0.1:11434/v1"),
            api_key=str(config.get("apiKey") or "ollama"),
            model=str(config.get("model") or "llama3"),
            temperature=config.get("temperature"),
        )

    if provider in ("openai", "api"):
        return OpenAICompatBackend(
            base_url=str(config.get("baseUrl") or ""),
            api_key=str(config.get("apiKey") or ""),
            model=str(config.get("model") or ""),
            temperature=config.get("temperature"),
        )

    raise BackendError(f"未知的模型类型: {provider}")
