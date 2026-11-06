"""ConPTY 终端会话：支持交互式命令、真实提示符和 ANSI 输出。"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import winpty
except ImportError:  # pragma: no cover
    winpty = None

try:
    import pyte
except ImportError:  # pragma: no cover
    pyte = None


class PtyError(RuntimeError):
    pass


class PtySession:
    def __init__(self, cwd: Path | str, cols: int = 120, rows: int = 30, history: int = 500):
        self.cwd = str(cwd)
        self.cols = cols
        self.rows = rows
        self.history = history
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._proc = None
        self._screen = None
        self._stream = None
        self._closed = False

    def reset(self, cwd: Path | str) -> None:
        self.close()
        self.cwd = str(cwd)
        self._closed = False

    def ensure_started(self) -> bool:
        with self._start_lock:
            if self._proc is not None and not self._closed:
                return True
            if winpty is None:
                raise PtyError("pywinpty 未安装，无法启动 ConPTY 终端")
            if pyte is None:
                raise PtyError("pyte 未安装，无法渲染终端")
            try:
                proc = winpty.PTY(self.cols, self.rows)
                proc.spawn("powershell.exe", "-NoLogo -NoProfile", cwd=self.cwd)
            except Exception as error:
                raise PtyError(f"ConPTY 启动失败: {error}") from error
            screen = pyte.HistoryScreen(self.cols, self.rows, history=self.history)
            stream = pyte.Stream(screen)
            with self._lock:
                self._proc = proc
                self._screen = screen
                self._stream = stream
                self._closed = False
            threading.Thread(target=self._read_loop, daemon=True).start()
            return True

    def write(self, data: str) -> None:
        self.ensure_started()
        try:
            self._proc.write(data)
        except Exception as error:
            raise PtyError(f"写入终端失败: {error}") from error

    def snapshot(self, since: int = 0) -> dict:
        self.ensure_started()
        with self._lock:
            text = self._render()
        total = len(text)
        # since 是上一次返回的 total，只回传新增部分，避免每次轮询重发整屏
        offset = min(max(0, int(since or 0)), total)
        return {"text": text[offset:], "total": total, "cwd": self.cwd}

    def close(self) -> None:
        # 与 ensure_started 共用 _start_lock，避免关闭过程中又启动出一个进程
        with self._start_lock:
            if self._closed and self._proc is None:
                return
            self._closed = True
            proc = self._proc
            self._proc = None
            self._screen = None
            self._stream = None
        if proc is None:
            return
        try:
            proc.cancel_io()
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                )
        except Exception:
            pass

    # ---- 内部 ----

    def _read_loop(self) -> None:
        proc = self._proc
        while proc is not None and not self._closed:
            try:
                chunk = proc.read(blocking=False)
            except Exception:
                break
            if chunk:
                stream = self._stream
                screen = self._screen
                if stream is not None and screen is not None:
                    with self._lock:
                        try:
                            stream.feed(chunk)
                        except Exception:
                            pass
                continue
            time.sleep(0.03)
            try:
                if not proc.isalive():
                    break
            except Exception:
                break

    def _render(self) -> str:
        screen = self._screen
        if screen is None:
            return ""
        lines = []
        for line in screen.history.top:
            lines.append(self._line_text(line))
        lines.extend(screen.display)
        return "\n".join(line.rstrip() for line in lines).rstrip()

    @staticmethod
    def _line_text(line) -> str:
        if isinstance(line, str):
            return line
        try:
            return "".join(char.data for _, char in sorted(line.items()))
        except Exception:
            return ""

