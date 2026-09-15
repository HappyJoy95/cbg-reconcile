"""极简 CDP 客户端（纯标准库）。

为什么自己写 WebSocket：项目的原则是"门店电脑能少装一个包就少一个"。
CDP 只需要文本帧，够用就行 —— 但**必须写对**：掩码、分片、长度三档、ping/pong。

只实现用到的东西：
    http_json(port, path)      读 /json/version、/json/list
    Cdp.connect(ws_url)        连上浏览器
    cdp.call(method, params)   发一条命令，等它的回复（自动跳过事件通知）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import urllib.error
import urllib.request
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# 单帧上限：CDP 的命令很小，但浏览器回的 cookie 列表可能不小
_MAX_FRAME = 32 * 1024 * 1024


class CdpError(RuntimeError):
    pass


# ------------------------------------------------------------------ HTTP 侧
def http_json(port: int, path: str, timeout: float = 5.0, host: str = "127.0.0.1"):
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CdpError(f"读 {url} 失败：{e}") from e


# ------------------------------------------------------------- WebSocket 侧
class WebSocket:
    """够 CDP 用就行：文本帧、分片、ping/pong、close。"""

    def __init__(self, url: str, timeout: float = 20.0):
        u = urlparse(url)
        if u.scheme != "ws":
            raise CdpError(f"只支持 ws://，收到 {url!r}")
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 80
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake()

    # ---- 握手 ----
    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket 握手时连接被关闭")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest
        text = head.decode("latin-1")
        if "101" not in text.split("\r\n")[0]:
            raise CdpError(f"WebSocket 握手失败：{text.splitlines()[0] if text else '空响应'}")
        expect = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if expect.lower() not in text.lower():
            raise CdpError("WebSocket 握手校验失败（Sec-WebSocket-Accept 不对）")

    # ---- 收 ----
    def _read_exact(self, n: int) -> bytes:
        out = b""
        if self._buf:
            out, self._buf = self._buf[:n], self._buf[n:]
        while len(out) < n:
            chunk = self.sock.recv(min(65536, n - len(out)))
            if not chunk:
                raise CdpError("连接被关闭")
            out += chunk
        return out

    def _read_frame(self) -> tuple[int, bool, bytes]:
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if length > _MAX_FRAME:
            raise CdpError(f"帧太大：{length} 字节")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, fin, payload

    def recv_text(self, deadline: float | None = None) -> str:
        """收一条完整文本消息（自动拼分片、自动回 pong）。"""
        import time
        end = time.monotonic() + (deadline if deadline is not None else self.timeout)
        data = b""
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise CdpError("等 CDP 回复超时")
            self.sock.settimeout(left)
            opcode, fin, payload = self._read_frame()
            if opcode == 0x9:                       # ping → pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode == 0x8:
                raise CdpError("浏览器关闭了 CDP 连接")
            if opcode in (0x1, 0x2):
                data = payload
            elif opcode == 0x0:
                data += payload
            else:
                continue
            if fin:
                return data.decode("utf-8", "replace")

    # ---- 发 ----
    def _send_frame(self, opcode: int, payload: bytes):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)                        # 客户端必须掩码
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------- CDP 层
class Cdp:
    """一条到浏览器（或某个标签页）的 CDP 连接。"""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self._id = 0
        self.events: list[dict] = []               # 攒着事件，需要的人自己捞

    @classmethod
    def connect(cls, port: int, timeout: float = 20.0) -> "Cdp":
        v = http_json(port, "/json/version")
        url = v.get("webSocketDebuggerUrl")
        if not url:
            raise CdpError("浏览器没给出 webSocketDebuggerUrl")
        return cls(url, timeout=timeout)

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        import time
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.monotonic() + (timeout or self.ws.timeout)
        while True:
            msg = json.loads(self.ws.recv_text(deadline=max(end - time.monotonic(), 0.1)))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError(f"{method} 失败：{msg['error'].get('message')}")
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)

    def close(self):
        self.ws.close()


def page_targets(port: int) -> list[dict]:
    return [t for t in http_json(port, "/json/list") if t.get("type") == "page"]
