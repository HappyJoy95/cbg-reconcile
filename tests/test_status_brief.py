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
        self.assertEqual(r["value"], "已配置")

    def test_这个文件里有密码(self):
        r = self._with_creds({"builtin": False, "username": "me", "has_password": True,
                              "has_token": True, "used_from": ""})
        self.assertEqual(r["kind"], "ok")
        self.assertEqual(r["value"], "已配置")

    def test_这个文件没有但别处有(self):
        """⚠ **不能说成"没配置"** —— 回落链上那份是能用的，说错了门店会白折腾。

        这正是第一版踩的坑：`describe_credentials` 按设计只读指定那个文件，
        所以它返回 `has_password=False`，但 `used_from` 指着真账号在的地方。
        """
        r = self._with_creds({"builtin": False, "username": "", "has_password": False,
                              "has_token": False, "used_from": "/home/x/.dsh/secrets/erp.env"})
        self.assertEqual(r["kind"], "ok", "别处有账号却说成不可用")
        self.assertEqual(r["value"], "已配置")

    def test_不露账号名也不露文件路径(self):
        """用户 2026-09-18：「（可）密码来自 /Users/ashui/.dsh/secrets/erp.env 这个不要」。

        两条都不露，理由不一样：
        * **路径**是本机内部实现 —— 门店那台上根本不是这个路径，
          摆出来只会让人以为"要去改那个文件"；
        * **账号名**是用户早先定过的「公司账号前端不显示」，
          在抽屉里印出来等于绕开那条规矩又露一遍。
        """
        cases = [
            {"builtin": True, "username": "sL18917405716", "has_password": False,
             "has_token": False, "used_from": ""},
            {"builtin": False, "username": "sL18917405716", "has_password": True,
             "has_token": True, "used_from": ""},
            {"builtin": False, "username": "", "has_password": False, "has_token": False,
             "used_from": "/Users/ashui/.dsh/secrets/erp.env"},
            {"builtin": False, "username": "me", "has_password": False, "has_token": True,
             "used_from": ""},
        ]
        for creds in cases:
            with self.subTest(creds=creds):
                v = self._with_creds(creds)["value"]
                self.assertNotIn("/", v, "把本机文件路径印出来了：%s" % v)
                self.assertNotIn(".env", v)
                self.assertNotIn(creds["username"] or "\x00", v, "把账号名印出来了")

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
        """⚠ 2026-09-18 起 `/api/*` 有**登录门禁**（`web.setup_state`）——
        这个测试得先把这台机器"配成能用的"，否则拿到的是 403。

        这里配成**合作店**（有门店名+编码、**没有串号标识**）：
        那种店不走玲珑，所以不需要玲珑会话也算就绪。
        """
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading

        self.write_cfg(erp_store_name="青岛永旺东部店", store_code="CNSCN162188")
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
        # ⚠ 结束锚点用**函数名**，不用文案 —— 文案会被改（「销售达成」这天就
        #   改成了「周度目标达成情况」），拿它当锚点等于给测试埋一颗定时炸弹。
        # ⚠ 锚点要写**带 `async function` 的完整形态**：光是 `loadAttain` 的话
        #   第一次出现在上面的 `SUBTABS` 表里（在 STATUS_KIND **之前**），
        #   切片会反过来、切出空串 —— 下面那句 assertTrue 就是拦这个的。
        block = js[js.index("const STATUS_KIND"):js.index("async function loadAttain")]
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


class Test悬浮窗(_Base):
    """用户 2026-09-18：「能不能做成悬浮窗。悬浮窗打开的话，这个图标还有，
    再点一下图标就关掉了。点旁边非悬浮窗的空白区域也可以关掉」。

    ⚠ 三个出口缺一不可，而且**它们会互相打架**：点把手时如果"点外面关闭"
    那个 handler 也生效，就会「先被关、紧接着又被开」—— 看着是"点了没反应"。
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        # ⚠ 令牌（`--fab-*` 那些）2026-09-18 搬去 `theme.css` 了 ——
        #   只读 style.css 的话，下面那些"几何对得上吗"的断言会全部找不到锚点。
        self.tokens = (ROOT / "web" / "theme.css").read_text(encoding="utf-8")

    def test_打开时把手不藏(self):
        """⚠ 原来写的是 `fab.hidden = open` —— 那样就没法"再点一下关掉"，
        而用户明确要求"悬浮窗打开的话，这个图标还有"。"""
        i = self.js.index("function setRunDrawer(")
        blk = self.js[i:i + 700]
        self.assertNotIn("fab.hidden", blk,
                         "又把把手藏了？那用户就没法点它关闭")

    def test_把手是开关不是单向打开(self):
        i = self.js.index("$('#btn-drawer')?.addEventListener")
        blk = self.js[i:i + 500]
        self.assertIn("setRunDrawer(opening)", blk)
        # ⚠ 判据要带上 `.open`：收起动画还在播的时候 `hidden` 仍是 false，
        #   只看 `hidden` 的话这时候点一下会"再关一次"，等于点了没反应。
        self.assertIn("classList.contains('open')", blk, "没读当前状态 —— 那就只能开不能关")

    def test_有过渡动画(self):
        """用户 2026-09-18：「这个从图标里冒出来的气泡能加上过渡动画吗」。"""
        i = self.css.index("\n.drawer {")
        blk = self.css[i:self.css.index("}", i)]
        self.assertIn("transition:", blk)
        # ⚠ 原点必须在**右下角** —— 那儿正是把手圆心，气泡才是"从图标里长出来"
        self.assertIn("transform-origin: 100% 100%", blk)
        self.assertIn("opacity: 0", blk, "没有初始态，就没有可过渡的东西")
        j = self.css.index(".drawer.open {")
        opened = self.css[j:self.css.index("}", j)]
        self.assertIn("opacity: 1", opened)
        self.assertIn("transform: none", opened)
        # 收起动画期间别让它抢点击
        self.assertIn("pointer-events: none", blk)
        self.assertIn("pointer-events: auto", opened)

    def test_动画时长两处对得上(self):
        """⚠ JS 里那个 `DRAWER_ANIM_MS` 和 CSS 的 transition 时长是**同一件事两份定义**。

        JS 短了 → 收起动画还没播完就 `hidden`，看着"啪"地消失；
        JS 长了 → 藏得晚，中间那段窗子看不见却还能点（幽灵点击）。
        """
        m = re.search(r"DRAWER_ANIM_MS\s*=\s*(\d+)", self.js)
        self.assertIsNotNone(m, "找不到 DRAWER_ANIM_MS")
        ms = int(m.group(1))
        i = self.css.index("\n.drawer {")
        blk = self.css[i:self.css.index("}", i)]
        decl = blk.split("transition:")[1].split(";")[0]
        # ⚠ 时长现在是**令牌**（`var(--motion-normal)`），得回 theme.css 解出来 ——
        #   直接在这段里找 `0.2s` 是找不到的
        names = re.findall(r"var\((--motion-[\w-]+)\)", decl)
        self.assertTrue(names, "过渡没用 --motion-* 令牌")
        secs = []
        for nm in names:
            mm = re.search(re.escape(nm) + r":\s*([\d.]+)s", self.tokens)
            self.assertIsNotNone(mm, "theme.css 里没有 %s" % nm)
            secs.append(float(mm.group(1)))
        self.assertEqual(ms, round(max(secs) * 1000),
                         "JS 的 DRAWER_ANIM_MS 和 theme.css 里的时长对不上")

    def test_动画靠类不靠_hidden(self):
        """⚠ `display: none` 过渡不了 —— 所以动画必须挂在 `.open` 类上，
        而 `hidden` 只在收起动画**播完之后**才设。

        ⚠ 这段逻辑 2026-09-18 抽成了共用的 `setVisible()`（二级菜单也要用同一套），
        所以断言从"抽屉里写了什么"改成"共用的那个函数做对了什么" +
        "抽屉确实用了它"。**抽走之后测试要跟着改锚点，别把断言留在原地当假绿。**
        """
        i = self.js.index("function setVisible(")
        blk = self.js[i:i + 1200]
        self.assertIn("requestAnimationFrame", blk, "没等帧 —— 过渡不会触发")
        self.assertIn("el.hidden = true", blk, "收起动画播完要真的藏起来")
        self.assertIn("clearTimeout(el._animTimer)", blk,
                      "关到一半又点开时，上一发的定时器会把它藏了")
        # ⚠ 计时器要挂在元素自己身上 —— 菜单有好几个，共用一个变量会互相取消
        self.assertIn("_animTimer", blk)
        j = self.js.index("function setRunDrawer(")
        self.assertIn("setVisible(drawer, open, DRAWER_ANIM_MS)", self.js[j:j + 300],
                      "抽屉没用上共用的那个 —— 两边动画行为会各走各的")

    def test_点窗外空白关闭(self):
        i = self.js.index("document.addEventListener('click'")
        # 找到"点外面关闭"那个 handler（文件里不止一个 document click）
        found = False
        pos = 0
        while True:
            pos = self.js.find("document.addEventListener('click'", pos + 1)
            if pos < 0:
                break
            blk = self.js[pos:pos + 700]
            if "#run-drawer" in blk and "setRunDrawer(false)" in blk:
                found = True
                # ⚠ 必须**同时**排掉窗内和把手
                self.assertIn("closest('#run-drawer')", blk)
                self.assertIn("closest('#btn-drawer')", blk,
                              "没排掉把手 —— 点把手会「先关再开」，看着像没反应")
                # ⚠ 点纯文本节点时 e.target 没有 closest
                self.assertIn("t.closest", blk)
                break
        self.assertTrue(found, "没有「点窗外空白关闭」的 handler")

    def test_样式是右下角浮层不是整屏抽屉(self):
        """⚠ 整屏抽屉是 `top: 0; bottom: 0` —— 那样把手会被压在窗子下面点不着。"""
        i = self.css.index("\n.drawer {")
        blk = self.css[i:self.css.index("}", i)]
        self.assertNotIn("top: 0", blk, "还是整屏抽屉？")
        self.assertIn("border-radius", blk)

    def test_窗的右下角顶点钉在把手圆心上(self):
        """用户 2026-09-18：「这个悬浮窗右下角的顶点和我们状态图标的圆心设置到一起吧」。

        ⚠ `--fab-center` 必须是**算出来的**（gap + size/2），不能写死一个 41px ——
        把手尺寸或边距一改，那个点就跟着走；写死的话两边立刻对不上。
        """
        # ⚠ 令牌在 `theme.css` 里（2026-09-18 从 style.css 搬过去的）
        root = self.tokens
        self.assertIn("--fab-center: calc(var(--fab-gap) + var(--fab-size) / 2)", root,
                      "--fab-center 没按 gap + size/2 算")
        i = self.css.index("\n.drawer {")
        blk = self.css[i:self.css.index("}", i)]
        self.assertIn("right: var(--fab-center)", blk)
        self.assertIn("bottom: var(--fab-center)", blk)

    def test_图标压在窗子上面(self):
        """用户 2026-09-18：「状态图标显示在上层」。

        ⚠ 窗角就在图标圆心上，压不到上面的话**半个图标会被窗子盖住**。
        """
        def z(sel):
            i = self.css.index(sel)
            blk = self.css[i:self.css.index("}", i)]
            m = re.search(r"z-index:\s*(\d+)", blk)
            self.assertIsNotNone(m, "%s 没有 z-index" % sel)
            return int(m.group(1))

        fab = z(".drawer-fab {")
        layer = z("\n.drawer {")
        self.assertGreater(fab, layer, "把手的 z-index 不比窗子高，图标会被盖住")
        # 仍要低于 toast(99) / 弹窗(200)，否则提示和弹窗会被图标压住
        self.assertLess(fab, 99)

if __name__ == "__main__":
    unittest.main()
