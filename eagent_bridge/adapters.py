"""网页 AI 站点适配器。

每个站点只需要三样东西：输入框选择器、回复节点选择器、生成中标志选择器。
回复是否结束不依赖站点特定标志，主要靠"文本停止变化"判断，兼容性更好。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .cdp import Page


class AdapterError(RuntimeError):
    """适配器错误：未登录、页面结构变化、等待超时等。"""


@dataclass(frozen=True)
class WebAIAdapter:
    key: str
    label: str
    url: str
    input_selectors: tuple[str, ...]
    reply_selectors: tuple[str, ...]
    busy_selectors: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "url": self.url}


ADAPTERS: dict[str, WebAIAdapter] = {
    "deepseek": WebAIAdapter(
        key="deepseek",
        label="DeepSeek 网页版",
        url="https://chat.deepseek.com/",
        input_selectors=(
            "textarea#chat-input",
            "textarea",
            "div[contenteditable='true']",
            "[contenteditable='true']",
        ),
        reply_selectors=(
            ".ds-markdown",
            "[class*='ds-markdown']",
            "[class*='markdown']",
        ),
        busy_selectors=(
            "button[aria-label*='停止']",
            "button[aria-label*='Stop']",
            "[class*='stop-btn']",
            "[class*='typing']",
            "[class*='loading']",
            "[class*='thinking']",
        ),
    ),
    "doubao": WebAIAdapter(
        key="doubao",
        label="豆包网页版",
        url="https://www.doubao.com/chat/",
        input_selectors=(
            "textarea",
            "div[contenteditable='true']",
            "[contenteditable='true']",
        ),
        reply_selectors=(
            ".flow-markdown-body",
            "[class*='message-content']",
            "[class*='markdown']",
        ),
        busy_selectors=(
            "[class*='loading']",
            "[class*='typing']",
            "button[aria-label*='停止']",
        ),
    ),
    "yuanbao": WebAIAdapter(
        key="yuanbao",
        label="腾讯元宝网页版",
        url="https://yuanbao.tencent.com/chat",
        input_selectors=(
            "textarea",
            "div[contenteditable='true']",
            "[contenteditable='true']",
        ),
        reply_selectors=(
            ".hyc-content-md",
            "[class*='markdown']",
            "[class*='message']",
        ),
        busy_selectors=(
            "[class*='loading']",
            "[class*='stop']",
        ),
    ),
}

DEFAULT_SITE = "deepseek"


def get_adapter(key: str | None) -> WebAIAdapter:
    adapter = ADAPTERS.get((key or DEFAULT_SITE).lower())
    if not adapter:
        supported = "、".join(ADAPTERS)
        raise AdapterError(f"不支持的站点: {key}，可选: {supported}")
    return adapter


_VISIBLE_HELPER = """
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const pick = (selectors) => {
    for (const selector of selectors) {
      const list = Array.from(document.querySelectorAll(selector)).filter(visible);
      if (list.length) return list;
    }
    return [];
  };
"""

_TEXTAREA_INPUTS = ("textarea", "div[contenteditable='true']", "[contenteditable='true']")

# 用原生 value setter 写入并派发 input 事件：React 受控组件才能同步 state，
# 否则发送按钮会一直处于禁用态（DeepSeek 新版就是这样）。
_TYPE_JS = (
    "(() => {"
    + _VISIBLE_HELPER
    + """
  const inputs = pick(%(inputs)s);
  const el = inputs[inputs.length - 1];
  if (!el) return {ok: false, error: 'no input'};
  el.focus();
  const text = %(text)s;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, text);
    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  } else {
    el.textContent = text;
    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
  }
  return {ok: true, value: (el.value !== undefined ? el.value : el.innerText) || ''};
})()"""
)

# 找输入框同一容器里的“发送”按钮并点击：
# 优先文字/aria 命中，否则退到主色圆形图标按钮（DeepSeek 新版只有图标）。
# 用元素 click 而不是坐标点击——坐标点击落在 SVG 图标上时站点不响应。
_CLICK_SEND_JS = (
    "(() => {"
    + _VISIBLE_HELPER
    + """
  const inputs = pick(%(inputs)s);
  const input = inputs[inputs.length - 1];
  if (!input) return {ok: false, error: 'no input'};
  const keywords = ['发送', 'Send', '提交'];
  let node = input;
  for (let level = 0; node && level < 6; level++) {
    const buttons = Array.from(node.querySelectorAll('button, [role="button"], .ds-button')).filter(visible);
    if (buttons.length) {
      const named = buttons.find((el) => {
        const label = (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.innerText || '');
        return keywords.some((keyword) => label.includes(keyword));
      });
      const primary = buttons.find((el) => {
        const cls = String(el.className || '');
        return /ds-button--primary|ds-button--filled|ds-button--circle|send|submit/i.test(cls);
      });
      const target = named || primary || buttons[buttons.length - 1];
      target.click();
      return {ok: true, level: level, opacity: getComputedStyle(target).opacity};
    }
    node = node.parentElement;
  }
  return {ok: false, error: 'no send button'};
})()"""
)

# 读取回复：站点的 markdown 容器；同时统计“思考中”等忙碌标志。
_SNAPSHOT_JS = (
    "(() => {"
    + _VISIBLE_HELPER
    + """
  const inputs = pick(%(inputs)s);
  const replies = pick(%(replies)s);
  const busy = %(busy)s.some((selector) => Array.from(document.querySelectorAll(selector)).some(visible));
  const input = inputs[inputs.length - 1];
  const inputText = input ? ((input.value !== undefined ? input.value : input.innerText) || '') : '';
  return {
    input: inputs.length > 0,
    inputText: inputText,
    count: replies.length,
    messages: replies.slice(-8).map((node) => (node.innerText || '').trim()).filter(Boolean),
    busy: busy,
    url: location.href,
  };
})()"""
)

_NEW_CHAT_JS = (
    "(() => {"
    + _VISIBLE_HELPER
    + """
  const keywords = ['新对话', '新建对话', '开启新对话', '新的对话', '新聊天', 'New chat', 'New Chat'];
  const candidates = Array.from(
    document.querySelectorAll('button, [role="button"], a, [class*="new-chat"], [class*="newChat"]')
  ).filter(visible);
  for (const el of candidates) {
    const label = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || ''));
    if (keywords.some((keyword) => label.includes(keyword))) {
      el.click();
      return true;
    }
  }
  return false;
})()"""
)


def _selectors(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def snapshot(page: Page, adapter: WebAIAdapter) -> dict:
    return page.evaluate(
        _SNAPSHOT_JS
        % {
            "inputs": _selectors(adapter.input_selectors),
            "replies": _selectors(adapter.reply_selectors),
            "busy": _selectors(adapter.busy_selectors),
        }
    ) or {}


def type_prompt(page: Page, adapter: WebAIAdapter, prompt: str) -> bool:
    """把提示词写进输入框，确保 React 受控组件的 state 同步更新。"""
    result = page.evaluate(
        _TYPE_JS
        % {
            "inputs": _selectors(adapter.input_selectors),
            "text": json.dumps(prompt, ensure_ascii=False),
        }
    )
    return bool((result or {}).get("ok"))


def click_send(page: Page, adapter: WebAIAdapter) -> bool:
    result = page.evaluate(_CLICK_SEND_JS % {"inputs": _selectors(adapter.input_selectors)})
    return bool((result or {}).get("ok"))


def wait_input(page: Page, adapter: WebAIAdapter, timeout: float = 30.0) -> bool:
    """等待输入框出现，用来判断页面是否加载完成 / 是否已登录。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = snapshot(page, adapter)
        if current.get("input"):
            return True
        time.sleep(0.5)
    return False


def start_new_chat(page: Page, adapter: WebAIAdapter, timeout: float = 20.0, stop_event=None) -> dict:
    """开启一个全新的网页对话，清空网页端累积的上下文。"""
    before = snapshot(page, adapter)
    before_count = int(before.get("count") or 0)

    clicked = bool(page.evaluate(_NEW_CHAT_JS))
    if not clicked:
        # 找不到按钮就退回首页，多数站点回首页即新对话
        page.navigate(adapter.url)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise AdapterError("已被用户停止")
        time.sleep(0.6)
        current = snapshot(page, adapter)
        count = int(current.get("count") or 0)
        if count < before_count or (count == 0 and before_count == 0):
            if wait_input(page, adapter, 8.0):
                return {
                    "ok": True,
                    "clicked": clicked,
                    "url": current.get("url"),
                    "messages": count,
                }
    raise AdapterError("没能开启新对话，可能是按钮文案或页面结构已变化")


def ask(
    page: Page,
    adapter: WebAIAdapter,
    prompt: str,
    timeout: float = 180.0,
    poll: float = 0.4,
    ready_timeout: float = 20.0,
    on_status=None,
    stop_event=None,
    no_reply_timeout: float = 45.0,
) -> str:
    """发送一条消息并等待回复文本。"""
    if not prompt.strip():
        raise AdapterError("提示词不能为空")

    def notify(text: str) -> None:
        if on_status:
            try:
                on_status(text)
            except Exception:
                pass

    before = snapshot(page, adapter)
    if not before.get("input"):
        # 刚打开的页面还在渲染，给单页应用一点时间
        if wait_input(page, adapter, ready_timeout):
            before = snapshot(page, adapter)
    if not before.get("input"):
        raise AdapterError(
            f"在 {adapter.label} 页面上没有找到输入框：可能尚未登录，或页面结构已变化"
        )

    baseline_count = int(before.get("count") or 0)
    baseline_messages = list(before.get("messages") or [])
    baseline_last = baseline_messages[-1] if baseline_messages else ""

    # 写入：必须走原生 setter，网页端的受控组件才会把文本记进 state
    notify(f"正在向 {adapter.label} 输入任务…")
    if not type_prompt(page, adapter, prompt):
        raise AdapterError(f"{adapter.label} 输入框无法写入")

    time.sleep(0.4)
    typed = snapshot(page, adapter)
    if not (typed.get("inputText") or "").strip():
        raise AdapterError(f"{adapter.label} 输入框没有接受文本，页面结构可能已变化")

    notify("正在发送…")
    sent = False
    attempts = 0
    started = time.time()
    while time.time() - started < 8.0 and not sent:
        if stop_event is not None and stop_event.is_set():
            raise AdapterError("已被用户停止")
        attempts += 1
        if attempts == 1:
            page.press_enter()
        else:
            click_send(page, adapter)
        time.sleep(1.2)
        current = snapshot(page, adapter)
        if not (current.get("inputText") or "").strip():
            sent = True
            break
        if int(current.get("count") or 0) > baseline_count and current.get("busy"):
            sent = True
            break
    if not sent:
        raise AdapterError(
            f"消息没能发到 {adapter.label}：发送按钮可能是禁用状态。"
            "请在弹出的浏览器窗口里确认已登录，并手动试发一条。"
        )

    notify(f"已发送，等待 {adapter.label} 回复…")
    started = time.time()
    last_text = ""
    unchanged = 0
    while time.time() - started < timeout:
        if stop_event is not None and stop_event.is_set():
            raise AdapterError("已被用户停止")
        time.sleep(poll)
        current = snapshot(page, adapter)
        messages = current.get("messages") or []

        current_count = int(current.get("count") or 0)
        if current_count <= baseline_count and not current.get("busy"):
            # 只有“最后一条回复变了”才算新回复（列表可能被虚拟化截断）
            if not messages or messages[-1] == baseline_last:
                if time.time() - started > no_reply_timeout:
                    raise AdapterError(
                        f"{adapter.label} 已接收消息，但 {int(no_reply_timeout)} 秒内没有返回内容。"
                        "网页版可能触发了风控或不稳定：请稍后重试、切换其它网页模型，"
                        "或在设置里改用 API 模型。"
                    )
                continue

        text = messages[-1] if messages else ""
        if not text:
            continue

        if text == last_text:
            unchanged += 1
        else:
            last_text = text
            unchanged = 0

        # 文本稳定 + 没有“生成中”标志 → 认为回复结束；无标志时用更长的稳定窗口兜底
        if (unchanged >= 3 and not current.get("busy")) or unchanged >= 9:
            return text

    if last_text:
        return last_text
    raise AdapterError(f"等待 {adapter.label} 回复超时（{int(timeout)} 秒），且未拿到任何内容")
