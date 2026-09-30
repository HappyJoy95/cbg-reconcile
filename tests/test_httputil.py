# -*- coding: utf-8 -*-
"""业务接口**不吃代理**（`src/httputil.py`）。

钉的是 2026-09-29 那次故障的修法：代理软件关了、环境变量还留着 ⇒
云商/华为全部 `ProxyError` ⇒ 退出码 2 ⇒ 界面上「最近拉数据一直报错」。
去掉代理变量重跑当场成功（15833 行落库），所以修法就是**让业务接口不看代理**。

⚠ `selfupdate`（GitHub）**故意仍然吃代理** —— 有的网络出不去，
   别顺手把它也改了（这条有测试盯着）。
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests

from src import httputil
from src.paths import ROOT

#: 死代理（这个端口没人听）—— 谁走代理谁挂。
DEAD = "http://127.0.0.1:1"


class _OK(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *a):                          # 静音
        pass


class _Local:
    """一个只回 200 的本地服务器（线程里跑，测试结束就关）。"""

    def __enter__(self):
        self.srv = socketserver.TCPServer(("127.0.0.1", 0), _OK)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/" % self.port
        return self.url

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()


def _dead_proxy_env():
    """把代理指到死端口、并清掉 no_proxy（否则 127.0.0.1 会被绕过去、测不出东西）。"""
    return mock.patch.dict(os.environ, {
        "http_proxy": DEAD, "https_proxy": DEAD, "all_proxy": "socks5://127.0.0.1:1",
        "HTTP_PROXY": DEAD, "HTTPS_PROXY": DEAD, "ALL_PROXY": "socks5://127.0.0.1:1",
        "no_proxy": "", "NO_PROXY": "",
    })


class Test死代理下业务接口照样通(unittest.TestCase):
    def test_裸requests会被死代理掐断_业务Session不会(self):
        """**先证明环境真的在起作用** —— 不然这条测试绿了也没说明什么。"""
        with _Local() as url, _dead_proxy_env():
            with self.assertRaises(requests.RequestException) as ctx:
                requests.get(url, timeout=5)
            msg = str(ctx.exception)
            self.assertIn("ProxyError", msg,
                          "裸调用必须真的走了代理（不是这条路的话这条测试是空的）")
            self.assertIn("port=1", msg, "而且得是那个死端口 1")
            r = httputil.session().get(url, timeout=5)
            self.assertEqual(r.status_code, 200,
                             "业务 Session 要直连 —— 代理死活与它无关")

    def test_NO_PROXY关键字也能绕开死代理(self):
        """裸调那几处（wecom / browser / dump）走的是这个键。"""
        with _Local() as url, _dead_proxy_env():
            r = requests.get(url, timeout=5, proxies=httputil.NO_PROXY)
            self.assertEqual(r.status_code, 200)


class Test各业务模块都接上了(unittest.TestCase):
    def test_带登录态的三处Session都不吃代理(self):
        from src.cbg import CbgClient
        from src.erp import ErpClient
        from src import tdoc
        erp = ErpClient({"username": "u", "password": "p", "company": "c",
                         "token": "t"})
        self.assertFalse(erp.s.trust_env, "云商 Session 要 trust_env=False")
        cbg = CbgClient(session=mock.Mock())
        self.assertFalse(cbg.http.trust_env, "华为 Session 要 trust_env=False")
        self.assertFalse(tdoc._SESS.trust_env, "腾讯文档默认 Session 同上")

    def test_裸调那几处都带了NO_PROXY(self):
        for rel, needle in (("src/wecom.py", "proxies=httputil.NO_PROXY"),
                            ("src/browser.py", "proxies=httputil.NO_PROXY"),
                            ("src/dump.py", "proxies=httputil.NO_PROXY")):
            s = (Path(ROOT) / rel).read_text(encoding="utf-8")
            with self.subTest(f=rel):
                self.assertIn(needle, s,
                              "%s 的裸 requests.post 必须显式给 proxies" % rel)

    def test_selfupdate默认仍吃代理_只有兜底那次关掉(self):
        """GitHub 那条**要**能走代理（有的网络出不去）—— 别把主路径统一掉。

        2026-09-29 加了「代理不通就直连」的兜底，但那**只关那一次请求的代理**：
        主 Session 不许 `trust_env = False`，兜底那次也只置 `proxies`、不动
        `trust_env`（自定义 CA / netrc 还得照常生效）。
        """
        import inspect
        from src import selfupdate
        main = inspect.getsource(selfupdate._get)
        # ⚠ 认 `trust_env =`（赋值），别裸认 `trust_env` —— 注释里写"不动 trust_env"
        #   也会被算进来（这条今天就是这么红的）
        self.assertNotIn("trust_env =", main,
                         "主路径的 Session 不许关 trust_env —— 它默认要吃代理")
        self.assertIn("_get_direct", main, "代理不通要能换直连（兜底）")
        direct = inspect.getsource(selfupdate._get_direct)
        self.assertIn("httputil.NO_PROXY", direct, "直连那次要显式不走代理")
        self.assertNotIn("trust_env =", direct, "兜底也别动 trust_env")


if __name__ == "__main__":
    unittest.main()
