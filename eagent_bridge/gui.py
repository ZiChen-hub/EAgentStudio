"""桌面 Agent：聊天面板 + 本地工具执行。

启动：
    python -m eagent_bridge.gui
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .adapters import AdapterError
from .runtime import AgentRuntime, StopRequested
from .session import Session
from .tools import preview_action

SITES = ("deepseek", "doubao", "yuanbao")

TAG_STYLE = {
    "user": {"foreground": "#1F4E9C"},
    "assistant": {"foreground": "#1B1B1B"},
    "tool": {"foreground": "#5B5B5B"},
    "error": {"foreground": "#B3261E"},
    "status": {"foreground": "#7A7A7A"},
    "finish": {"foreground": "#0B7A3E", "font": ("Microsoft YaHei UI", 10, "bold")},
}

LABELS = {
    "user": "你",
    "assistant": "AI",
    "tool": "工具",
    "error": "错误",
    "status": "状态",
    "finish": "完成",
}


class AgentApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("EAgent Desktop · 网页 AI 驱动的编码 Agent")
        self.root.geometry("980x720")
        self.root.minsize(760, 560)

        self.queue: queue.Queue = queue.Queue()
        self.session = Session()
        self.runtime: AgentRuntime | None = None
        self.running = False

        self._build_ui()
        self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 界面 ----

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")

        ttk.Label(top, text="工作目录").pack(side="left")
        self.workspace_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(top, textvariable=self.workspace_var, width=52).pack(side="left", padx=(6, 6))
        ttk.Button(top, text="选择", command=self._choose_workspace).pack(side="left")

        ttk.Label(top, text="站点").pack(side="left", padx=(14, 4))
        self.site_var = tk.StringVar(value=SITES[0])
        ttk.Combobox(top, textvariable=self.site_var, values=SITES, width=10, state="readonly").pack(side="left")

        ttk.Label(top, text="最大步数").pack(side="left", padx=(14, 4))
        self.steps_var = tk.IntVar(value=12)
        ttk.Spinbox(top, from_=4, to=30, textvariable=self.steps_var, width=5).pack(side="left")

        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="自动允许修改与命令", variable=self.auto_var).pack(side="left", padx=(14, 0))

        body = ttk.Frame(self.root, padding=(12, 0))
        body.pack(fill="both", expand=True)

        self.log = tk.Text(body, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10), borderwidth=1, relief="solid")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for kind, style in TAG_STYLE.items():
            self.log.tag_configure(kind, **style)

        bottom = ttk.Frame(self.root, padding=(12, 10))
        bottom.pack(fill="x")

        self.input = tk.Text(bottom, height=4, font=("Microsoft YaHei UI", 10), borderwidth=1, relief="solid", wrap="word")
        self.input.pack(fill="x")
        self.input.bind("<Control-Return>", lambda event: (self._send(), "break")[1])

        actions = ttk.Frame(bottom)
        actions.pack(fill="x", pady=(8, 0))
        self.send_button = ttk.Button(actions, text="发送任务", command=self._send)
        self.send_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="Ctrl+Enter 发送").pack(side="left", padx=(10, 0))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, padding=(12, 4), foreground="#5B5B5B").pack(fill="x")

    # ---- 事件 ----

    def _choose_workspace(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.workspace_var.get() or str(Path.cwd()))
        if chosen:
            self.workspace_var.set(chosen)

    def _send(self) -> None:
        if self.running:
            return
        task = self.input.get("1.0", "end").strip()
        if not task:
            return
        workspace = Path(self.workspace_var.get()).expanduser()
        if not workspace.is_dir():
            messagebox.showerror("工作目录无效", f"目录不存在：{workspace}")
            return

        self.input.delete("1.0", "end")
        self._log("user", task)
        self.running = True
        self.send_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("运行中 ...")

        thread = threading.Thread(
            target=self._run_task,
            args=(task, workspace, self.site_var.get(), self.steps_var.get(), self.auto_var.get()),
            daemon=True,
        )
        thread.start()

    def _stop(self) -> None:
        if self.runtime:
            self.runtime.stop_event.set()
            self.status_var.set("正在停止 ...")

    def _run_task(self, task: str, workspace: Path, site: str, steps: int, auto_allow: bool) -> None:
        try:
            runtime = AgentRuntime(
                workspace=workspace,
                site=site,
                max_steps=steps,
                auto_allow=auto_allow,
                approver=self._approve,
                on_event=lambda kind, text: self.queue.put(("log", kind, text)),
                session=self.session,
            )
            self.runtime = runtime
            summary = runtime.run(task)
            self.queue.put(("done", "任务结束：" + summary))
        except StopRequested:
            self.queue.put(("done", "任务已被用户停止"))
        except AdapterError as error:
            self.queue.put(("error", str(error)))
        except Exception as error:  # noqa: BLE001
            self.queue.put(("error", f"{type(error).__name__}: {error}"))
        finally:
            self.runtime = None

    def _approve(self, name: str, args: dict, workspace: Path) -> bool:
        preview = preview_action(name, args, workspace)
        if preview["kind"] == "write":
            detail = f"{preview['path']}\n\n{preview['new'][:800]}"
        elif preview["kind"] == "command":
            detail = f"命令：{preview['command']}\n目录：{preview['cwd']}"
        else:
            detail = preview.get("detail", name)

        event = threading.Event()
        result = {"ok": False}

        def ask() -> None:
            result["ok"] = messagebox.askyesno("需要确认", f"Agent 请求执行：{name}\n\n{detail}")
            event.set()

        self.root.after(0, ask)
        event.wait()
        return result["ok"]

    # ---- 日志 ----

    def _log(self, kind: str, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{LABELS.get(kind, kind)}] {text}\n", kind)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
        except queue.Empty:
            item = None
        if item:
            if item[0] == "log":
                _, kind, text = item
                self._log(kind, text)
            elif item[0] == "done":
                self._finish(item[1])
            elif item[0] == "error":
                self._log("error", item[1])
                self._finish("运行失败")
        self.root.after(80, self._poll)

    def _finish(self, message: str) -> None:
        self.running = False
        self.send_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(message)

    def _on_close(self) -> None:
        if self.running and not messagebox.askyesno("任务仍在运行", "确定要退出吗？"):
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    AgentApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
