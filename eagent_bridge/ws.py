"""极简 WebSocket 客户端。

只实现 CDP 需要的部分：握手、文本帧、分片重组、ping/pong、close。
不引入第三方依赖，避免安装环境和网络问题。
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse


class WebSocketError(RuntimeError):
    """WebSocket 层错误。"""


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不支持的 WebSocket 协议: {url}")

        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

        self._buffer = b""
        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        try:
            self._sock.sendall(request.encode("ascii"))
            header = self._read_until(b"\r\n\r\n")
            status_line = header.split(b"\r\n", 1)[0].decode("latin-1")
            if "101" not in status_line:
                raise WebSocketError(f"WebSocket 握手失败: {status_line}")
        except Exception:
            # 握手失败时把底层连接关掉，避免反复重连时累积半开 socket
            try:
                self._sock.close()
            except Exception:
                pass
            raise

    def settimeout(self, timeout: float) -> None:
        self._sock.settimeout(timeout)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        chunks = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise WebSocketError("服务端关闭了 WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            # 连接可能已经卡住，关闭时不要再用长超时等待
            self._sock.settimeout(1.0)
        except Exception:
            pass
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    # ---- 内部实现 ----

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)

        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + mask + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first = self._read_exact(2)
        fin = bool(first[0] & 0x80)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]

        mask_key = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise WebSocketError("WebSocket 连接已断开")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手阶段连接被关闭")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return head + marker
