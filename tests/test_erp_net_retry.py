"""云商 HTTP 层的**瞬时网络错误重试**（2026-09-21 用户报的 SSL 断连）。

钉三件事：

1. `SSLError` / `ConnectionError` / `Timeout` **会重试**，不是一次就炸；
2. 重试耗尽后抛的是 **`ErpError`** —— 不是 `requests.SSLError`；
   否则 `_fetch_erp_stock` 接不住，整步变成顶层「程序 bug」+ 退出码 9；
3. **业务错误不重试**（`ResponseID != 0` 的 `ErpError` 原样抛，只试一次）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from src import erp
from src.erp import ErpClient, ErpError


def _client():
    return ErpClient(
        {"username": "u", "password": "p", "company": "C", "token": "TK"},
        env_file=str(Path(tempfile.gettempdir()) / "x.env"),
    )


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.content = b""

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class TestNetRetry(unittest.TestCase):
    def test_ssl_then_success(self):
        """第一次 TLS 被掐、第二次成功 —— 要重试，不能直接抛。"""
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.side_effect = [
            requests.exceptions.SSLError(
                "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
            _Resp({"ResponseID": 0, "Data": {}}),
        ]
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()):
            j = c.call("https://api.yserp.cc/x", {"token": "TK"})
        self.assertEqual(j["ResponseID"], 0)
        self.assertEqual(c.s.post.call_count, 2, "第一次 SSL 失败后没有重试")

    def test_connection_error_retries(self):
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.side_effect = [
            requests.exceptions.ConnectionError("reset by peer"),
            _Resp({"ResponseID": 0}),
        ]
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()):
            j = c.call("https://api.yserp.cc/x", {})
        self.assertEqual(j["ResponseID"], 0)
        self.assertEqual(c.s.post.call_count, 2)

    def test_timeout_retries(self):
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.side_effect = [
            requests.exceptions.Timeout("read timed out"),
            _Resp({"ResponseID": 0}),
        ]
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()):
            c.call("https://api.yserp.cc/x", {})
        self.assertEqual(c.s.post.call_count, 2)

    def test_exhausted_raises_erp_error_not_ssl(self):
        """耗尽后必须是 `ErpError` —— 否则 daily 当「程序 bug」退出码 9。"""
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.side_effect = requests.exceptions.SSLError("EOF")
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()):
            with self.assertRaises(ErpError) as ctx:
                c.call("https://api.yserp.cc/x", {})
        self.assertNotIsInstance(ctx.exception, requests.exceptions.SSLError)
        self.assertIn("网络", str(ctx.exception))
        self.assertEqual(c.s.post.call_count, erp.NET_TRIES)

    def test_business_error_does_not_retry(self):
        """`ResponseID != 0` 是业务错 —— 重试没用，只许试一次。"""
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.return_value = _Resp({"ResponseID": 2, "Message": "暂无数据"})
        with mock.patch.object(erp.time, "sleep", lambda s: None):
            with self.assertRaises(ErpError) as ctx:
                c.call("https://api.yserp.cc/x", {})
        self.assertIn("暂无数据", str(ctx.exception))
        self.assertEqual(c.s.post.call_count, 1, "业务错被当成网络错重试了")

    def test_http_error_status_not_retried_as_net(self):
        """`raise_for_status` 的 4xx/5xx 不在「瞬时网络」名单里 —— 原样抛。"""
        c = _client()
        c.s = mock.MagicMock()
        resp = _Resp({})
        resp.raise_for_status = mock.Mock(
            side_effect=requests.exceptions.HTTPError("500 Server Error"))
        c.s.post.return_value = resp
        with self.assertRaises(requests.exceptions.HTTPError):
            c.call("https://api.yserp.cc/x", {})
        self.assertEqual(c.s.post.call_count, 1)

    def test_login_retries_ssl(self):
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.side_effect = [
            requests.exceptions.SSLError("EOF"),
            _Resp({"ResponseID": 0, "Data": {"token": "NEW"}}),
        ]
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()), \
                mock.patch.object(ErpClient, "_save_token", lambda self, tok: None):
            tok = c.login(save=False)
        self.assertEqual(tok, "NEW")
        self.assertEqual(c.s.post.call_count, 2)

    def test_inventory_download_retries(self):
        """导出 POST 成功、**下载 xlsx** SSL 掐断也要重试（两段是两次请求）。"""
        c = _client()
        c.s = mock.MagicMock()
        ok = _Resp({"ResponseID": 0, "Data": "/f/x.xlsx"})
        dl_fail = requests.exceptions.SSLError("EOF")
        dl_ok = _Resp({})
        dl_ok.content = b"PK\x03\x04fake"
        c.s.post.return_value = ok
        c.s.get.side_effect = [dl_fail, dl_ok]
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()), \
                mock.patch.object(erp, "_inventory_rows",
                                  lambda p: [{"Imei": "A"}]), \
                mock.patch.object(Path, "write_bytes", lambda self, b: None), \
                mock.patch.object(Path, "mkdir", lambda self, **k: None):
            rows = c.inventory_imei()
        self.assertEqual(rows, [{"Imei": "A"}])
        self.assertEqual(c.s.get.call_count, 2, "下载 SSL 失败后没有重试")


class TestFetchStockExitCode(unittest.TestCase):
    def test_ssl_becomes_exit_fetch_not_internal(self):
        """耗尽后的 SSL → `ErpError` → `_fetch_erp_stock` 返 2，不是 9。"""
        from src import cli
        c = _client()
        c.s = mock.MagicMock()
        c.s.post.side_effect = requests.exceptions.SSLError("EOF")
        ns = mock.Mock(start="", end="", date="", days_ago=0, config=None,
                       verbose=False, no_refresh=True, no_push=True, no_mail=True)
        with mock.patch.object(erp.time, "sleep", lambda s: None), \
                mock.patch.object(sys, "stderr", mock.MagicMock()), \
                mock.patch("src.erp.ErpClient", return_value=c):
            conn = mock.MagicMock()
            rc = cli._fetch_erp_stock(conn, ns)
        self.assertEqual(rc, cli.EXIT_FETCH)
        self.assertNotEqual(rc, cli.EXIT_INTERNAL)


if __name__ == "__main__":
    unittest.main()
