# -*- coding: utf-8 -*-
"""CDP 层的异常契约：**socket 错误一律折成 `CdpError`，不许漏裸异常**。

2026-09-29 实测真因：抓取循环读 csrf 时页面正在跳转，`sock.recv` 到期抛出
**裸 `TimeoutError`** —— 而抓取循环、`csrf_from_page`、`try_auto_login`
所有兜底都是 `except CdpError`，接不住 → 整个抓取当场崩、
会话根本没走到保存。用户看到的就是「手动登上了，还是抓不到」。

调用方按"CdpError 是唯一货币"写的，这里就是铸币厂 —— 漏一个类型出去
就是一轮线上崩溃。
"""

import socket
import unittest
from unittest import mock

from src import cdp
from src.cdp import Cdp, CdpError, WebSocket


class _DeadSock:
    """连接"活着但什么都不回"或"抛各种 socket 错"的假 socket。"""

    def __init__(self, recv_exc=None, send_exc=None):
        self.recv_exc = recv_exc
        self.send_exc = send_exc
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def recv(self, n):
        if self.recv_exc is not None:
            raise self.recv_exc
        return b""

    def sendall(self, data):
        if self.send_exc is not None:
            raise self.send_exc

    def close(self):
        pass


def _ws(sock) -> WebSocket:
    """跳过 __init__（那会真连网），直接装一个带假 socket 的 WebSocket。"""
    ws = object.__new__(WebSocket)
    ws.host, ws.port, ws.path = "127.0.0.1", 9222, "/devtools"
    ws.timeout = 1.0
    ws.sock = sock
    ws._buf = b""
    return ws


class TestRecvNeverLeaksRawExceptions(unittest.TestCase):
    """收的方向 —— 崩溃就出在这条路上。"""

    def test_socket_timeout_is_cdp_error_not_timeout_error(self):
        """socket.recv 超时抛的 TimeoutError 必须折成 CdpError。

        这就是 2026-09-29 抓取崩溃的原始堆栈（csrf_from_page → call →
        recv_text → sock.recv → TimeoutError: timed out）。
        """
        ws = _ws(_DeadSock(recv_exc=TimeoutError("timed out")))
        with self.assertRaises(CdpError) as ctx:
            ws.recv_text(deadline=0.5)
        self.assertIn("超时", str(ctx.exception))

    def test_socket_timeout_alias_is_also_folded(self):
        """3.8/3.9 上 socket.timeout 是独立类 —— 一样要折。"""
        ws = _ws(_DeadSock(recv_exc=socket.timeout("timed out")))
        with self.assertRaises(CdpError):
            ws.recv_text(deadline=0.5)

    def test_connection_reset_is_cdp_error(self):
        """浏览器中途崩掉（ConnectionReset/BrokenPipe）也是 CdpError。"""
        ws = _ws(_DeadSock(recv_exc=ConnectionResetError(104, "reset")))
        with self.assertRaises(CdpError) as ctx:
            ws.recv_text(deadline=0.5)
        self.assertIn("连接中断", str(ctx.exception))

    def test_close_frame_still_reports_as_cdp_error(self):
        """浏览器主动关连接（opcode 0x8）的既有语义不能被改坏。"""
        # fin+close 帧，无 payload：0x88 0x80(masked, len 0) + 4 字节 mask
        frame = bytes([0x88, 0x80, 0, 0, 0, 0])
        sock = _DeadSock()
        buf = {"b": frame}

        def recv(n):
            # 真 socket 一次最多还 n 字节 —— 假 socket 也要守这个契约
            out, buf["b"] = buf["b"][:n], buf["b"][n:]
            return out

        sock.recv = recv
        ws = _ws(sock)
        with self.assertRaises(CdpError) as ctx:
            ws.recv_text(deadline=0.5)
        self.assertIn("浏览器关闭", str(ctx.exception))


class TestSendNeverLeaksRawExceptions(unittest.TestCase):
    def test_send_failure_is_cdp_error(self):
        """sendall 撞上 BrokenPipe（浏览器刚退出）也要折。"""
        ws = _ws(_DeadSock(send_exc=BrokenPipeError(32, "broken")))
        with self.assertRaises(CdpError):
            ws.send_text("{}")

    def test_close_swallows_send_failure(self):
        """close() 是清理路径 —— 发不出去就算了，绝不许抛。"""
        ws = _ws(_DeadSock(send_exc=BrokenPipeError(32, "broken")))
        ws.close()                                   # 不抛就是通过


class TestConnectNeverLeaksRawExceptions(unittest.TestCase):
    def test_connection_refused_is_cdp_error(self):
        """端口刚没了（浏览器正在退出）→ CdpError，调用方按 CdpError 兜。"""
        with mock.patch.object(cdp.socket, "create_connection",
                               side_effect=ConnectionRefusedError(111, "refused")):
            with self.assertRaises(CdpError) as ctx:
                WebSocket("ws://127.0.0.1:9/devtools/browser/x")
        self.assertIn("连不上", str(ctx.exception))

    def test_connect_timeout_is_cdp_error(self):
        with mock.patch.object(cdp.socket, "create_connection",
                               side_effect=TimeoutError("timed out")):
            with self.assertRaises(CdpError):
                WebSocket("ws://127.0.0.1:9/devtools/browser/x")

    def test_handshake_recv_failure_is_cdp_error(self):
        """握手时对端把连接关了 —— 同样折成 CdpError。"""
        sock = _DeadSock(recv_exc=ConnectionResetError(104, "reset"))
        with mock.patch.object(cdp.socket, "create_connection", return_value=sock):
            with self.assertRaises(CdpError):
                WebSocket("ws://127.0.0.1:9/devtools/browser/x")


class TestCallFoldsRecvTimeout(unittest.TestCase):
    def test_call_timeout_surfaces_as_cdp_error(self):
        """Cdp.call 等不到回复 → CdpError（csrf_from_page 就是靠它兜住的）。"""
        sock = _DeadSock(recv_exc=TimeoutError("timed out"))
        ws = _ws(sock)
        cdp_obj = object.__new__(Cdp)
        cdp_obj.ws = ws
        cdp_obj._id = 0
        cdp_obj.events = []
        with self.assertRaises(CdpError):
            cdp_obj.call("Runtime.evaluate", {}, timeout=0.5)


if __name__ == "__main__":
    unittest.main()
