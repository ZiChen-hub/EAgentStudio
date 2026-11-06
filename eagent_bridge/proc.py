"""子进程收尾：Windows 下连同子进程一起结束，避免残留孤儿进程。"""

from __future__ import annotations

import os
import subprocess

DEFAULT_TIMEOUT = 5


def kill_tree(process: subprocess.Popen, timeout: float = DEFAULT_TIMEOUT) -> None:
    """结束进程及其子进程；失败时退化为只杀直接子进程。"""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
            )
        else:
            process.kill()
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
