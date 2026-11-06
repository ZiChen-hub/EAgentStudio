"""Windows GUI 操控工具：截图、鼠标、键盘、窗口。"""

from __future__ import annotations

import base64
import shutil
import subprocess
import time
from pathlib import Path

from .storage import prune

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WIN32 = """using System;
using System.Runtime.InteropServices;
public class EAgentWin32 {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, int extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte key, byte scan, uint flags, int extra);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
}"""


class GuiError(RuntimeError):
    pass


def _powershell() -> str:
    return shutil.which("powershell") or "powershell.exe"


def _run(script: str, timeout: float = 60.0) -> str:
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        raise GuiError(f"PowerShell 执行超时（{int(timeout)} 秒）") from error
    except OSError as error:
        raise GuiError(f"无法调用 PowerShell: {error}") from error

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:600]
        raise GuiError(detail or f"PowerShell 返回码 {completed.returncode}")
    return (completed.stdout or "").strip()


def _ps_path(path: Path) -> str:
    return str(path).replace("'", "''")


def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


GUI_SPECS = [
    {
        "name": "screen_capture",
        "description": "截取整个屏幕并保存为 PNG，返回文件路径与分辨率（需要确认）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mouse_move",
        "description": "把鼠标移动到屏幕坐标 (x, y)（需要确认）",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "mouse_click",
        "description": "在屏幕坐标 (x, y) 单击/双击，button 为 left/right/middle（需要确认）",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "double": {"type": "boolean"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "mouse_drag",
        "description": "按住鼠标左键从 (x1, y1) 拖到 (x2, y2)（需要确认）",
        "parameters": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                "x2": {"type": "integer"}, "y2": {"type": "integer"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "keyboard_type",
        "description": "向当前焦点窗口输入一段文字（通过剪贴板粘贴，支持中文）（需要确认）",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "keyboard_hotkey",
        "description": "按组合键，例如 ctrl+s、alt+f4、ctrl+shift+esc、win（需要确认）",
        "parameters": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
        },
    },
    {
        "name": "window_list",
        "description": "列出当前所有带标题的窗口（进程 ID + 标题）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "window_focus",
        "description": "按标题模糊匹配并切换到前台窗口（需要确认）",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
]

GUI_NAMES = {spec["name"] for spec in GUI_SPECS}


def execute_gui(name: str, args: dict, workspace: Path) -> str:
    if name == "screen_capture":
        return _screen_capture(workspace)
    if name == "mouse_move":
        return _mouse_move(args)
    if name == "mouse_click":
        return _mouse_click(args)
    if name == "mouse_drag":
        return _mouse_drag(args)
    if name == "keyboard_type":
        return _keyboard_type(args)
    if name == "keyboard_hotkey":
        return _keyboard_hotkey(args)
    if name == "window_list":
        return _window_list()
    if name == "window_focus":
        return _window_focus(args)
    raise GuiError(f"未知 GUI 工具: {name}")


def _screen_capture(workspace: Path) -> str:
    folder = workspace / ".eagent" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"shot-{int(time.time() * 1000)}.png"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
        f"$bmp.Save('{_ps_path(target)}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose(); "
        "Write-Output (\"{0}x{1}\" -f $b.Width, $b.Height)"
    )
    size = _run(script)
    prune(folder, 50, "*.png")
    return f"截图已保存：{target}（{size}）"


def _win32_define() -> str:
    return "Add-Type -TypeDefinition '" + WIN32.replace("'", "''") + "'"


def _mouse_script(body: str) -> str:
    return _win32_define() + "; " + body


def _mouse_move(args: dict) -> str:
    x = int(args.get("x") or 0)
    y = int(args.get("y") or 0)
    _run(_mouse_script(f"[EAgentWin32]::SetCursorPos({x}, {y})"))
    return f"鼠标已移动到 ({x}, {y})"


def _mouse_click(args: dict) -> str:
    x = int(args.get("x") or 0)
    y = int(args.get("y") or 0)
    button = str(args.get("button") or "left").lower()
    double = bool(args.get("double"))
    flags = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }.get(button, (0x0002, 0x0004))
    clicks = 2 if double else 1
    body = (
        f"[EAgentWin32]::SetCursorPos({x}, {y}); Start-Sleep -Milliseconds 60; "
        + " ".join(
            f"[EAgentWin32]::mouse_event(0x{flags[0]:04X},0,0,0,0); Start-Sleep -Milliseconds 40; "
            f"[EAgentWin32]::mouse_event(0x{flags[1]:04X},0,0,0,0); Start-Sleep -Milliseconds 80;"
            for _ in range(clicks)
        )
    )
    _run(_mouse_script(body))
    return f"已在 ({x}, {y}) 执行 {button} {'双击' if double else '单击'}"


def _mouse_drag(args: dict) -> str:
    x1, y1 = int(args.get("x1") or 0), int(args.get("y1") or 0)
    x2, y2 = int(args.get("x2") or 0), int(args.get("y2") or 0)
    steps = 20
    moves = " ".join(
        f"[EAgentWin32]::SetCursorPos({x1 + (x2 - x1) * i // steps}, {y1 + (y2 - y1) * i // steps}); Start-Sleep -Milliseconds 15;"
        for i in range(1, steps + 1)
    )
    body = (
        f"[EAgentWin32]::SetCursorPos({x1}, {y1}); Start-Sleep -Milliseconds 60; "
        "[EAgentWin32]::mouse_event(0x0002,0,0,0,0); "
        f"{moves} [EAgentWin32]::mouse_event(0x0004,0,0,0,0);"
    )
    _run(_mouse_script(body))
    return f"已从 ({x1}, {y1}) 拖拽到 ({x2}, {y2})"


def _keyboard_type(args: dict) -> str:
    text = str(args.get("text") or "")
    if not text:
        raise GuiError("text 不能为空")
    payload = _b64_text(text)
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"$bytes = [Convert]::FromBase64String('{payload}'); "
        "$text = [Text.Encoding]::UTF8.GetString($bytes); "
        "$old = Get-Clipboard -Raw -ErrorAction SilentlyContinue; "
        "Set-Clipboard -Value $text; "
        "Start-Sleep -Milliseconds 150; "
        "[System.Windows.Forms.SendKeys]::SendWait('^v'); "
        "Start-Sleep -Milliseconds 350; "
        "if ($null -ne $old) { Set-Clipboard -Value $old }; "
        "Write-Output 'typed'"
    )
    _run(script)
    return f"已输入 {len(text)} 个字符（剪贴板已尝试恢复）"


def _keyboard_hotkey(args: dict) -> str:
    keys = str(args.get("keys") or "").strip()
    if not keys:
        raise GuiError("keys 不能为空")
    parts = [part.strip() for part in keys.split("+") if part.strip()]
    mapping = {
        "ctrl": 17, "control": 17, "alt": 18, "shift": 16, "win": 91, "cmd": 91,
        "enter": 13, "return": 13, "esc": 27, "escape": 27, "tab": 9, "space": 32,
        "backspace": 8, "delete": 46, "insert": 45, "up": 38, "down": 40,
        "left": 37, "right": 39, "home": 36, "end": 35, "pageup": 33, "pagedown": 34,
    }
    for index in range(1, 13):
        mapping[f"f{index}"] = 111 + index
    vk_parts = []
    for part in parts:
        low = part.lower()
        if low in mapping:
            vk_parts.append(mapping[low])
        elif len(low) == 1:
            vk_parts.append(ord(low.upper()))
        else:
            raise GuiError(f"不支持的按键: {part}")

    down = " ".join(f"[EAgentWin32]::keybd_event(0x{vk:02X},0,0,0); Start-Sleep -Milliseconds 40;" for vk in vk_parts)
    up = " ".join(
        f"[EAgentWin32]::keybd_event(0x{vk:02X},0,0x0002,0); Start-Sleep -Milliseconds 30;" for vk in reversed(vk_parts)
    )
    _run(_mouse_script(down + " " + up))
    return f"已按组合键：{keys}"


def _window_list() -> str:
    script = (
        "Get-Process | Where-Object { $_.MainWindowTitle } | "
        "ForEach-Object { \"$($_.Id)`t$($_.MainWindowTitle)\" }"
    )
    output = _run(script, timeout=30)
    return output or "没有找到带标题的窗口"


def _window_focus(args: dict) -> str:
    title = str(args.get("title") or "").strip()
    if not title:
        raise GuiError("title 不能为空")
    safe = title.replace("'", "''")
    script = _win32_define() + "; " + (
        f"$p = Get-Process | Where-Object {{ $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like '*{safe}*' }} | Select-Object -First 1; "
        "if (-not $p) { Write-Output 'NO_WINDOW' } else { "
        "[EAgentWin32]::ShowWindow($p.MainWindowHandle, 9) | Out-Null; "
        "[EAgentWin32]::SetForegroundWindow($p.MainWindowHandle) | Out-Null; "
        "Write-Output $p.MainWindowTitle }"
    )
    output = _run(script)
    if output == "NO_WINDOW":
        raise GuiError(f"没有找到标题包含「{title}」的窗口")
    return f"已切换到窗口：{output}"
