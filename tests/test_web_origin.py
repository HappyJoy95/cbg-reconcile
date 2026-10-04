"""本机控制台写接口：浏览器来源必须属于当前本机地址。"""

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src import web


class TestWriteOrigin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "store-X.yaml").write_text(
            "erp_store_name: 青岛CBD万达店\n"
            "store_code: SCN328987\n"
            "marker: C\n", encoding="utf-8")
        web.Handler.app = web.App(self.root, "config/store-X.yaml")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def post(self, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/setup/preview", body='{"on":true}',
                     headers=headers or {})
        response = conn.getresponse()
        status = response.status
        response.read()
        conn.close()
        return status

    def test_跨站简单请求不能写本机设置(self):
        status = self.post({"Origin": "https://outside.example",
                            "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertFalse((self.root / web.PREVIEW_REL).exists())

    def test_跨站_fetch_site_即使没有_origin_也拒绝(self):
        self.assertEqual(self.post({"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertFalse((self.root / web.PREVIEW_REL).exists())

    def test_null来源和不同端口也拒绝(self):
        self.assertEqual(self.post({"Origin": "null"}), 403)
        self.assertEqual(self.post({"Origin": "http://127.0.0.1:1"}), 403)

    def test_无请求体的停止服务接口也拒绝跨站来源(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/shutdown", headers={"Origin": "https://outside.example"})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        conn.close()

    def test_同源浏览器和无来源头的本机脚本都能写(self):
        origin = "http://127.0.0.1:%d" % self.port
        self.assertEqual(self.post({"Origin": origin,
                                    "Sec-Fetch-Site": "same-origin"}), 200)
        self.assertEqual(self.post(), 200)
        self.assertTrue(json.loads((self.root / web.PREVIEW_REL)
                                   .read_text(encoding="utf-8"))["on"])
