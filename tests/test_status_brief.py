"""右下角状态抽屉（`/api/status` → `App.status_brief()`）。

用户 2026-09-18：「点击后的悬窗显示目前门店的名称，编码，公司云商账号状态、
玲珑会话状态，推送哪个通道是开着的，下面是运行一次按钮和日志窗口」。

⚠ 为什么这些**判断**全在后端、而不是前端拼字符串：
"公司云商账号到底算不算配好了"是最容易写错、又最不容易看出来的那种判断 ——
`describe_credentials()` **只读指定的那个文件**，所以"这个文件里没有" ≠ "没账号"
（真账号在回落链的下一站）。散在前端就只能靠肉眼看，pytest 一条也测不到。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import web                                               # noqa: E402


def _js_code_only(src: str) -> str:
    """剥掉 JS 注释。

    ⚠ 不剥的话注释会把 `assertNotIn` 顶掉 —— 这个项目的注释专门写"这里以前
    是什么、为什么拿掉"，里面会**原样出现**那些词。HTML 那边为这个假红过两回。
    ⚠ `//` 要排除 `https://`，否则会把整行从协议头那儿切掉（正则用 `(?<!:)`）。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.splitlines():
        m = re.search(r"(?<!:)//", line)
        out.append(line[:m.start()] if m else line)
    return "\n".join(out)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        self.cfg = self.root / "config" / "store-X.yaml"
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.app = web.App(self.root, "config/store-X.yaml")

    def write_cfg(self, **kv):
        self.cfg.write_text("".join("%s: %s\n" % (k, v) for k, v in kv.items()),
                            encoding="utf-8")

    def rows(self):
        return {r["label"]: r for r in self.app.status_brief()["rows"]}


class Test门店那三行(_Base):
    def test_配好了就照原样显示(self):
        self.write_cfg(erp_store_name="青岛CBD万达店", store_code="SCN231409", marker="C")
        r = self.rows()
        self.assertEqual(r["门店名称"]["value"], "青岛CBD万达店")
        self.assertEqual(r["华为门店编码"]["value"], "SCN231409")
        self.assertEqual(r["串号标识（本店码）"]["value"], "C")
        self.assertEqual(r["门店名称"]["kind"], "")

    def test_没配要标红而不是留空(self):
        """⚠ 留空的话界面上那一行看着像"后端没给"，而它其实是**没配**。"""
        self.write_cfg()
        r = self.rows()
        self.assertEqual(r["门店名称"]["kind"], "bad")
        self.assertEqual(r["串号标识（本店码）"]["kind"], "bad")
        self.assertIn("未配", r["门店名称"]["value"])

    def test_门店编码留空是说会话默认不是报错(self):
        """留空是**合法**的（不传 storeCode，华为按会话自身门店查）—— 别标成红的。"""
        self.write_cfg(erp_store_name="X", marker="Y")
        r = self.rows()
        self.assertEqual(r["华为门店编码"]["kind"], "warn")
        self.assertIn("会话默认", r["华为门店编码"]["value"])


class Test公司云商账号那一行(_Base):
    """⚠ 这一行第一版写成两行（"没配置" + "密码实际来自 …"），自相矛盾。"""

    def _with_creds(self, creds):
        with mock.patch.object(web, "describe_credentials", lambda *a, **k: creds):
            return self.rows()["公司云商账号"]

    def test_内置账号(self):
        r = self._with_creds({"builtin": True, "username": "sL1", "has_password": False,
                              "has_token": False, "used_from": ""})
        self.assertEqual(r["kind"], "ok")
        self.assertIn("内置账号", r["value"])

    def test_这个文件里有密码(self):
        r = self._with_creds({"builtin": False, "username": "me", "has_password": True,
                              "has_token": True, "used_from": ""})
        self.assertEqual(r["kind"], "ok")
        self.assertIn("已配置", r["value"])

    def test_这个文件没有但别处有(self):
        """⚠ **不能说成"没配置"** —— 回落链上那份是能用的，说错了门店会白折腾。

        这正是第一版踩的坑：`describe_credentials` 按设计只读指定那个文件，
        所以它返回 `has_password=False`，但 `used_from` 指着真账号在的地方。
        """
        r = self._with_creds({"builtin": False, "username": "", "has_password": False,
                              "has_token": False, "used_from": "/home/x/.dsh/secrets/erp.env"})
        self.assertEqual(r["kind"], "ok", "别处有账号却说成不可用")
        self.assertNotIn("没配置", r["value"])
        self.assertIn("/home/x/.dsh/secrets/erp.env", r["value"])
        self.assertIn("可用", r["value"])

    def test_只有_token没密码(self):
        """现在能用，但 token 一过期就续不上 —— 得提醒，别报成"已配置"。"""
        r = self._with_creds({"builtin": False, "username": "me", "has_password": False,
                              "has_token": True, "used_from": ""})
        self.assertEqual(r["kind"], "warn")
        self.assertIn("token", r["value"])

    def test_真没配(self):
        r = self._with_creds({"builtin": False, "username": "", "has_password": False,
                              "has_token": False, "used_from": ""})
        self.assertEqual(r["kind"], "bad")


class Test玲珑会话那一行(_Base):
    def test_没导入是红的(self):
        r = self.rows()
        self.assertEqual(r["玲珑会话"]["kind"], "bad")
        self.assertIn("未导入", r["玲珑会话"]["value"])

    def _with_session(self, info):
        with mock.patch.object(self.app, "session_info", lambda: info):
            return self.rows()["玲珑会话"]

    def test_自检通过是绿的并带上时间(self):
        r = self._with_session({"exists": True, "check_ok": True, "checked_at": 1758181200})
        self.assertEqual(r["kind"], "ok")
        self.assertRegex(r["value"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_自检失败要把原因写出来(self):
        r = self._with_session({"exists": True, "check_ok": False, "checked_at": 1,
                                "check_message": "401 未授权"})
        self.assertEqual(r["kind"], "bad")
        self.assertIn("401 未授权", r["value"])

    def test_导入了但没自检过要提醒(self):
        r = self._with_session({"exists": True})
        self.assertEqual(r["kind"], "warn")


class Test推送通道那一行(_Base):
    def _with_channels(self, mail, wecom_):
        with mock.patch.object(web.mailer, "describe_mail", lambda *a, **k: mail):
            with mock.patch.object(web.wecom, "describe_wecom", lambda *a, **k: wecom_):
                return self.rows()["推送通道"]

    def test_都没开(self):
        r = self._with_channels({"enabled": False}, {"enabled": False})
        self.assertEqual(r["kind"], "warn")
        self.assertIn("都没开", r["value"])

    def test_只列开着的(self):
        r = self._with_channels({"enabled": True, "ready": True},
                                {"enabled": False, "ready": True})
        self.assertIn("邮件", r["value"])
        self.assertNotIn("企业微信", r["value"])
        self.assertEqual(r["kind"], "ok")

    def test_开着但缺配置要说清(self):
        """⚠ "开着"不等于"发得出去" —— 只写"邮件"会让门店以为配好了。"""
        r = self._with_channels({"enabled": True, "ready": False},
                                {"enabled": False})
        self.assertIn("邮件", r["value"])
        self.assertIn("缺配置", r["value"])


class Test接口和前端接线(_Base):
    def test_接口能通(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading

        self.write_cfg(erp_store_name="青岛CBD万达店", marker="C")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            web.Handler.app = self.app
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/api/status")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        d = json.loads(r.read().decode("utf-8"))
        self.assertTrue(any(x["label"] == "门店名称" for x in d["rows"]))

    def test_前端只画行不自己判断(self):
        """⚠ 抽屉里那句话**必须**来自后端。前端自己拼 = pytest 测不到。"""
        raw = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("'/api/status'", raw)
        self.assertIn("function renderStatus(", raw)
        # ⚠ **必须剥注释再查**：这个项目的注释是"记录踩过的坑"风格，会**原样提到**
        #   被禁的东西 —— 不剥的话注释自己就把 assertNotIn 顶掉了
        #   （HTML 那边为这个假红过两回，`_strip_html_comments` 就是那么来的）。
        js = _js_code_only(raw)
        # ⚠ **只查状态那一段**，不查整个 app.js —— 那几个词在别处是**合法**的
        #   （侧栏徽章写「会话未导入」、华为登录写「已配置」），
        #   全局查会连它们一起判红，然后你就会想去改本来没错的代码。
        block = js[js.index("const STATUS_KIND"):js.index("销售达成")]
        self.assertTrue(block.strip(), "没切到状态那段 —— 锚点漂了")
        for word in ("内置账号", "自检失败", "都没开", "未导入", "已配置", "没配置"):
            with self.subTest(word=word):
                self.assertNotIn(word, block, "这个判断该在后端，前端只画行")
        # 正面钉一下：值是从后端来的、原样画出去
        self.assertIn("esc(r.value", block)

    def test_把手是图标不是字按钮(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        i = html.index('class="drawer-fab"')
        blk = html[i:html.index("</button>", i)]
        self.assertIn("<svg", blk, "把手换成图标了，别又退回字按钮")
        self.assertNotIn("运行日志", blk)


if __name__ == "__main__":
    unittest.main()
