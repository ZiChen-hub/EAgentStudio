"""常驻 PowerShell 会话：支持 cd、变量和连续命令。"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DONE_MARKER = "__EAGENT_DONE__"
CWD_MARKER = "__EAGENT_CWD__"

_SCRIPT = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "while ($true) { "
    "$line = [Console]::ReadLine(); "
    "if ($null -eq $line) { break }; "
    "if ($line -eq '__EAGENT_EXIT__') { break }; "
    "try { Invoke-Expression $line 2>&1 | Out-String -Stream } catch { $_.Exception.Message }; "
    "Write-Output ('__EAGENT_CWD__' + (Get-Location).Path); "
    "Write-Output '__EAGENT_DONE__' }"
)


class ShellError(RuntimeError):
    pass


class PowerShellSession:
    def __init__(self, cwd: Path | str, timeout: float = 120.0):
        self.cwd = str(cwd)
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue | None = None
        self._lock = threading.Lock()

    def reset(self, cwd: Path | str) -> None:
        with self._lock:
            self.close()
            self.cwd = str(cwd)

    def run(self, command: str) -> dict:
        with self._lock:
            if not self._ensure_started():
                return {"stdout": "PowerShell 启动失败", "cwd": self.cwd, "error": True}

            line = command.replace("\r\n", "; ").replace("\n", "; ").strip()
            if not line:
                return {"stdout": "", "cwd": self.cwd}

            assert self._proc and self._proc.stdin and self._queue
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.close()
                return {"stdout": "PowerShell 会话已断开，请重试", "cwd": self.cwd, "error": True}

            output: list[str] = []
            new_cwd = self.cwd
            deadline = time.time() + self.timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.close()
                    return {
                        "stdout": "\n".join(output) + f"\n命令超时（{int(self.timeout)} 秒）",
                        "cwd": self.cwd,
                        "error": True,
                    }
                try:
                    item = self._queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if item is None:
                    self.close()
                    return {
                        "stdout": "\n".join(output) + "\nPowerShell 进程已退出",
                        "cwd": self.cwd,
                        "error": True,
                    }
                if item.startswith(CWD_MARKER):
                    new_cwd = item[len(CWD_MARKER) :].strip()
                    continue
                if item == DONE_MARKER:
                    break
                output.append(item)

            self.cwd = new_cwd or self.cwd
            return {"stdout": "\n".join(output), "cwd": self.cwd}

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        self._queue = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.write("__EAGENT_EXIT__\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                )
                proc.wait(timeout=3)
            else:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---- 内部 ----

    def _ensure_started(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        executable = shutil.which("powershell") or "powershell.exe"
        try:
            self._proc = subprocess.Popen(
                [executable, "-NoLogo", "-NoProfile", "-Command", _SCRIPT],
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError:
            return False

        self._queue = queue.Queue()

        def reader() -> None:
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                if self._queue is not None:
                    self._queue.put(line.rstrip("\r\n"))
            if self._queue is not None:
                self._queue.put(None)

        threading.Thread(target=reader, daemon=True).start()
        return True
