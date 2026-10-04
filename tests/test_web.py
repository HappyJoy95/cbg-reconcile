"""Web 控制台测试。

重点不是逐行测 HTML，而是钉住**接线**：
前端引用了一个不存在的 id、或者定时任务接口把"删选中的"退化成"删全部"，
这类错误在浏览器里才炸，跑测试是看不见的。
"""

import datetime
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import textwrap
import types
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from src import run_daily, runner, schedule, service, web
from src.storage import runlog
import bootstrap

ROOT = Path(__file__).resolve().parent.parent
APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js",
                              "features/valueadd/film/page.js",
                              "features/valueadd/benefit/page.js",
                              "features/sales/attain/page.js",
                              "features/cashier/page.js",
                              "features/tools/claim/page.js",
                              "features/distribution/page.js", "features/plan/monthly/page.js", "app.js"))
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _strip_js_comments(s: str) -> str:
    """剥掉 JS 注释，供"代码里不该再有 X"这类断言用。

    ⚠ **必须有这一步**：这个项目的注释是"记录踩过的坑"风格，删掉一段代码时
    往往把"以前这里是什么、为什么要删"写进注释 —— 而注释里就会带上那个
    刚被删掉的名字。直接 `assertNotIn("daysAgo", APP_JS)` 会被**自己的注释**顶掉。
    （2026-09-17 这一轮踩了两次：`至少勾一项` 和 `daysAgo`。）

    ⚠ 只剥**整行**注释（`^\\s*//`）和块注释，不做行尾 `//` 的剥离 ——
    那样会把 `https://…` 这类字符串里的斜杠也吃掉。
    """
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", s)


class TestFrontendWiring(unittest.TestCase):
    def test_loaded_scripts_do_not_repeat_top_level_bindings(self):
        """Page scripts share one global scope; duplicate bindings cause parse errors or replace APIs."""
        scripts = re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)', INDEX_HTML)
        bindings = {}
        for src in scripts:
            if not src.startswith("/"):
                continue
            path = ROOT / "web" / src.lstrip("/").split("?", 1)[0]
            self.assertTrue(path.is_file(), f"index.html 引用了不存在的脚本：{src}")
            code = _strip_js_comments(path.read_text(encoding="utf-8"))
            for line_no, line in enumerate(code.splitlines(), 1):
                match = re.match(
                    r"^(?:(?:async\s+)?function|const|let|class|var)\s+([A-Za-z_$][\w$]*)\b",
                    line,
                )
                if match:
                    bindings.setdefault(match.group(1), []).append(f"{path.relative_to(ROOT)}:{line_no}")
        duplicates = {name: locations for name, locations in bindings.items()
                      if len(locations) > 1}
        self.assertFalse(duplicates, f"classic scripts repeat global declarations: {duplicates}")

    def test_every_referenced_id_exists_in_html(self):
        """`$('#foo')` 里的 foo 必须在 index.html 里有 id="foo"。

        漏了就是 `Cannot read properties of null` —— 整个页签白屏，
        而 Python 测试全绿。改 HTML 时最容易犯这个错。
        """
        used = set(re.findall(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""", APP_JS))
        defined = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", INDEX_HTML))
        # 有些元素是**前端自己拼出来的**（比如跑日志那个 <pre>）——
        # 只要 app.js 里也写了 id="x"，就算定义过
        defined |= set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", APP_JS))
        missing = sorted(used - defined)
        self.assertFalse(missing, f"app.js 引用了不存在的 id：{missing}")

    def test_schedule_table_has_per_row_delete(self):
        """删按钮得绑在**每一行**上，并且把任务名带去后端。"""
        self.assertIn("data-sched-del", APP_JS)
        self.assertIn("/api/schedule?name=", APP_JS)

    def test_手输店码仅生活馆展示并接到保存接口(self):
        self.assertIn("/api/session/store-code", APP_JS)
        i = APP_JS.index("function storeCodeBox()")
        block = APP_JS[i:APP_JS.index("function bindStoreCodeBtn()", i)]
        self.assertIn("setupState.runtime_lifehall", block)

    def test_生活馆编码只在选择流程和玲珑设置页(self):
        self.assertIn('id="entry-store-code"', INDEX_HTML)
        self.assertIn('id="store-code-settings"', INDEX_HTML)
        self.assertIn("data-store-code-input", APP_JS)
        self.assertIn("data-save-store-code", APP_JS)
        self.assertNotIn('id="setup-store-code-host"', INDEX_HTML)
        self.assertNotIn("setup-store-code-host", APP_JS)
        self.assertIn("store-code-settings", APP_JS)

    def test_店码设置卡住在玲珑授权页(self):
        """⭐ 用户 2026-09-29：「把门店编码设置放到玲珑授权里面吧」——
        它管的是"认哪家店、抓会话怎么自检"，跟会话同去向。
        ⚠ 两个方向都钉：进了玲珑授权、**不在**通用设置 —— 少一边，
        下次谁"顺手挪回去"这里当场红。"""
        ll = INDEX_HTML.split('id="subpanel-linglong"', 1)[1]
        ll = ll.split('</section>', 1)[0]
        self.assertIn('id="store-code-settings-card"', ll,
                      "店码设置卡不在玲珑授权页里")
        # ⚠ 截界用**下一个 subpanel**，不能用 </section> —— general 和 linglong
        #   同在一个 panel-settings 里，按 section 切会把玲珑段也圈进 general。
        gen = INDEX_HTML.split('id="subpanel-general"', 1)[1]
        gen = gen.split('id="subpanel-', 1)[0]
        self.assertNotIn('id="store-code-settings-card"', gen,
                         "店码设置卡怎么又回到了通用设置")

    def test_登录门禁不在概览加载前误画会话状态(self):
        i = APP_JS.index("async function checkSetup()")
        j = APP_JS.index("/* ⚠ 原来这里有个", i)
        block = APP_JS[i:j]
        self.assertIn("renderStoreCodeForms();", block)
        self.assertNotIn("renderSession();", block)

    def test_生活馆设置页保留邮件企微配置和业务推送空状态(self):
        self.assertIn('data-foot="general"', INDEX_HTML)
        self.assertIn('id="mail-paths"', INDEX_HTML)
        self.assertIn('id="wecom-paths"', INDEX_HTML)
        self.assertIn("生活馆版当前没有单独配置的业务推送项", APP_JS)

    def test_生活馆设置页不加载云商账号或后台服务(self):
        i = APP_JS.index("async function loadConfig()")
        j = APP_JS.index("// 收集设置页所有可改字段", i)
        block = APP_JS[i:j]
        self.assertIn("const lifehall = !!(setupState && setupState.runtime_lifehall)", block)
        self.assertIn("if (!lifehall)", block)
        self.assertIn("loadStoreAccount();", block)
        self.assertIn("loadService();", block)


class TestLifehallNotifyPreferences(unittest.TestCase):
    """生活馆推送偏好只含真实通道；旧版业务开关不显示也不可写。"""

    def setUp(self):
        from src import edition
        self._old_edition = os.environ.get("CBG_EDITION")
        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(self._restore_edition)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def _restore_edition(self):
        from src import edition
        if self._old_edition is None:
            os.environ.pop("CBG_EDITION", None)
        else:
            os.environ["CBG_EDITION"] = self._old_edition
        edition.reload()

    def _request_ready(self, method, path, body=None):
        with mock.patch.object(web, "setup_state",
                               return_value={"ready": True, "need": ""}):
            return self.srv.request(method, path, body)

    def test_GET只返回通道偏好不返回full业务项(self):
        status, body = self._request_ready("GET", "/api/notify-pref")
        self.assertEqual(status, 200, body)
        prefs = body["prefs"]
        self.assertEqual(set(prefs), {"plat:mail", "plat:wecom"})
        self.assertFalse(any(p.get("kind") == "feature" for p in prefs.values()))

    def test_PUT拒绝旧版业务开关且不落盘(self):
        status, body = self._request_ready(
            "PUT", "/api/notify-pref", {"key": "attain", "enabled": False})
        self.assertEqual(status, 400, body)
        self.assertFalse((self.root / ".secrets" / "notify-prefs.json").exists())

    def test_full版仍允许原业务推送偏好(self):
        from src import edition
        os.environ["CBG_EDITION"] = "full"
        edition.reload()
        try:
            status, body = self._request_ready("GET", "/api/notify-pref")
            self.assertEqual(status, 200, body)
            self.assertIn("attain", body["prefs"])
            status, body = self._request_ready(
                "PUT", "/api/notify-pref", {"key": "attain", "enabled": False})
            self.assertEqual(status, 200, body)
            self.assertFalse(body["prefs"]["attain"]["enabled"])
        finally:
            os.environ["CBG_EDITION"] = "lifehall"
            edition.reload()

    def test_提权删发的是叶子名(self):
        """⚠ 提权删那条发的是 **叶子名**（`t.name`），不是 `full_name`。

        `full_name` 带反斜杠（`\\CBG报量对账-21点20`），而 `schedule.remove`
        会拒收带反斜杠的名字（防路径注入）→ 必然 400，表现是"点了完全没反应"。
        行内「删除」那条发 full_name 没问题（那条路不做名字校验），
        两条的差别就在这儿。

        ⚠ 2026-09-20：原来是「以管理员身份**修复**」（重新注册一遍），
          兜底去掉之后改成「以管理员身份**删除**」——
          按钮换了，规矩不变：**发叶子名**。
        """
        i = APP_JS.index("data-sched-del-admin]")
        blk = APP_JS[i:i + 800]
        self.assertIn("what: 'schedule-remove', name", blk)
        self.assertNotIn("full_name", blk, "提权这条不能发带反斜杠的名字")
        # 名字是从 `data-sched-del-admin` 上取的（渲染时给的是 t.name）
        self.assertIn("data-sched-del-admin=\"${esc((tasks.find((t) => t.unreadable) || {}).name", APP_JS)

    def test_captcha_state_has_its_own_banner(self):
        """⚠ 状态映射漏了 `need_captcha` 的话，`cls`/`title` 都取到 `undefined`，
        横幅会渲染成空的 —— 用户看到的还是"什么都没说"，正是这次要修的毛病。
        """
        self.assertIn("need_captcha", APP_JS)
        # ⚠ 必须**精确切片**，不能取个 300 字符的窗口就断言 ——
        #   紧挨着的 `title` 映射里也有 `need_captcha`，窗口会把它框进去，
        #   于是"从 cls 里删掉 need_captcha"这个改动**测试照样绿**。
        #   （第一版就是这么写的，故意改坏验证时才发现。）
        i = APP_JS.index("const cls = {")
        cls = APP_JS[i:APP_JS.index("const title = {", i)]
        self.assertIn("need_captcha", cls, "cls 映射缺 need_captcha → 横幅会是空的")
        j = APP_JS.index("const title = {")
        title = APP_JS[j:APP_JS.index("|| job.state", j)]
        self.assertIn("需要验证码", title, "title 映射缺文案")

    def test_running_captcha_shows_a_banner_not_only_the_log(self):
        """⚠ 运行期间就要提示，别等结束了才说 —— 用户正在等的时候最需要知道
        "现在轮到你操作了"。只写进 `#auto-log` 那个滚动日志是不够的。
        """
        i = APP_JS.index("function pollAuto")
        block = APP_JS[i:i + 1500]
        self.assertIn("job.need", block, "运行期横幅要看 need 字段")
        self.assertIn("#auto-result", block, "要写进醒目横幅，不是日志")

    def test_no_leftover_single_delete_button(self):
        """老的单删按钮已经拆成行内按钮，别再被谁加回来。"""
        self.assertNotIn("btn-sched-remove", APP_JS)
        self.assertNotIn('id="btn-sched-remove"', INDEX_HTML)

    def test_no_bare_html_strings_in_table_cells(self):
        """⚠ 表格单元格里的 HTML 必须包成 `{html: ...}`。

        实测踩到（门店就是这么看到它的）：历史版本列表里把 `` `<b>v1.4.6</b>` ``
        这个**字符串**直接当单元格传进去，而 `table()` 对字符串默认 `esc()`
        —— 页面上于是原样显示出 `<b>v1.4.6</b>` 这串标签。

        这种错**跑测试不会红、控制台也不报**，只有肉眼能发现。所以在这里钉死。

        只看**数组字面量内部、且以字符串开头**的元素 —— 普通赋值里的 HTML
        （`el.innerHTML = '<div>…'`）是正常写法，不该误报。
        """
        bad = []
        in_arr = False
        for i, line in enumerate(APP_JS.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            if re.match(r"^(return\s+)?\[$", s) or re.match(r"^[\w.$]+\s*=\s*\[$", s):
                in_arr = True
                continue
            if in_arr and re.match(r"^\];?$", s):
                in_arr = False
                continue
            if not in_arr:
                continue
            if "{ html:" in s or "{html:" in s:
                continue                      # 已经是正确的写法
            if not re.match(r"^[`'\"]", s):
                continue                      # 元素不是字符串起头（对象/变量）
            if re.search(r"<(\w+)[^>]*>", s):
                bad.append(f"第 {i} 行：{s[:70]}")
        self.assertFalse(bad, "这些单元格带 HTML 但没包 {html:}，会被原样转义显示：\n"
                              + "\n".join(bad))

    def test_schedule_html_cells_are_wrapped(self):
        """`table()` 的单元格默认 **esc 转义** —— 想塞原生 HTML 必须包成 `{html: ...}`。

        （截图时真踩到过：页面上直接显示 `<span class="hint">` 的源码。
        不算崩，但很难看，而且测试全绿、只有肉眼能发现。）
        """
        # ⚠ 切片要**先锚到 `function renderSchedule`**：2026-09-20 前面多了
        #   一张定时器任务表（`renderTimer` / `timerRow`），不锚的话
        #   匹到的是那一张 —— 而它里面确实有不含 `{html:}` 的 `<span>`（是模板片段，不是单元格）。
        seg = APP_JS[APP_JS.index("function renderSchedule("):]
        seg = seg[:seg.index("\n}\n", seg.index("function renderSchedule("))]
        m = re.search(r"const rows = tasks\.map\(.*?\n  \]\);", seg, re.S)
        self.assertIsNotNone(m, "找不到 renderSchedule 里的 rows 定义，测试要跟着改")
        block = m.group(0)
        html_lines = [ln for ln in block.splitlines() if "<span" in ln or "<button" in ln]
        self.assertTrue(html_lines, "rows 里应该有几处带 HTML 的单元格")
        for ln in html_lines:
            self.assertIn("{ html:", ln, f"含 HTML 的单元格没包 {{html:}}：{ln.strip()}")

    def test_delete_button_carries_both_names(self):
        """发去后端的名字（带 \\ 前缀）和给人看的名字分开 ——
        不然弹窗会写成「删除「\\CBG报量对账-中午」？」。
        """
        self.assertIn("data-sched-del=", APP_JS)
        self.assertIn("data-sched-label=", APP_JS)
        self.assertIn("btn.dataset.schedLabel", APP_JS)


class _Server:
    """真起一个 HTTP 服务 —— 测的是路由和状态码，不是 mock 出来的路由。"""

    def __init__(self, root: Path):
        # ⚠ 2026-09-18 起 `/api/*` 有**登录门禁**（`web.setup_state`）——
        #   不先把这台机器配成"能用的"，下面所有请求都是 403。
        #   这里配成**合作店**（有门店名 + 编码、**没有串号标识**）：
        #   那种店不走玲珑，所以不需要玲珑会话也算就绪。
        #   ⚠ 必须写进**这个 root**（`app.root`）—— 门店账号文件是按 `app.root`
        #     解析的，写到别处的话会去读**开发机上那份真凭据**（实测踩到）。
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛永旺东部店"\nstore_code: "CNSCN162188"\n',
            encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def request(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestAutoUpdateReachesUI(unittest.TestCase):
    """后台每天自动查一次更新 —— 查到了得**真的送到界面**才算数。

    光写进缓存没用：前端渲染用的是 `/api/overview` 里那个 `update` 字段。
    这条链断在哪一环，界面上都不会有任何提示，而门店同事**不会**去点按钮 ——
    于是这个功能等于不存在。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_overview_carries_the_cached_update(self):
        """⚠ 必须走 `cached()`，不能走 `check()` —— 概览页 30 秒刷一次，
        每次都去戳 GitHub 的话，门店那点网络全被它占了。"""
        cache = {"at": 1, "latest": "1.3.0", "checked_at": 2, "has_update": True}

        def no_network(*a, **kw):
            raise AssertionError("概览不能发网络请求")

        with mock.patch.object(web.selfupdate, "cached", lambda r, c: cache), \
                mock.patch.object(web.selfupdate, "remote_version", no_network):
            status, body = self.srv.request("GET", "/api/overview")

        self.assertEqual(status, 200)
        self.assertEqual(body["update"]["latest"], "1.3.0")
        self.assertTrue(body["update"]["has_update"], "有新版本没送到界面")
        self.assertEqual(body["update"]["checked_at"], 2,
                         "界面要说得出上次查是什么时候")

    def test_cached_endpoint_returns_what_the_watcher_wrote(self):
        """后台线程写完缓存，前端刷新时读的就是这个接口。"""
        cache = {"at": 1, "latest": "1.3.0", "checked_at": 2, "has_update": True}
        with mock.patch.object(web.selfupdate, "cached", lambda r, c: cache):
            status, body = self.srv.request("GET", "/api/update?cached=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["latest"], "1.3.0")
        self.assertTrue(body["has_update"])

    def test_the_watcher_is_actually_started(self):
        """`daily_watcher` 没人启动的话，一切都白搭。"""
        import inspect
        src = inspect.getsource(web.serve)
        self.assertIn("daily_watcher", src, "serve() 没启动后台更新检查")
        self.assertIn("daemon=True", src, "必须守护线程，不能拦住进程退出")

    def test_ui_says_when_it_last_checked(self):
        """报错时要能说出"上次成功查到是什么时候" —— 否则人不知道该不该信它。"""
        self.assertIn("checked_at", APP_JS)
        self.assertIn("formatAgo", APP_JS)


class TestUpdateApi(unittest.TestCase):
    """升级 / 修复接口（历史版本回退已去掉，用户 2026-09-23）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_改到一半失败时绝不重启服务(self):
        """阶段 1.4a：`PartialUpdate` ⇒ 界面拿得到现场，而且**不重启**。"""
        def boom(root, *, current=""):
            raise web.selfupdate.PartialUpdate("铺到一半失败了（3/40 个文件）：磁盘满了", {
                "from": current, "to": "1.5.0", "changed": ["src/a.py"],
                "backup": "/tmp/backup-x", "journal": "/tmp/journal.json"})

        with mock.patch.object(web.selfupdate, "apply_update", boom), \
                mock.patch.object(web.selfupdate, "restart_later",
                                  lambda r: self.fail("部分失败**不许**重启服务")):
            status, body = self.srv.request("POST", "/api/update", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertTrue(body["partial"], "要标明这是「改到一半」，不是干净失败")
        self.assertFalse(body["restarting"])
        self.assertIn("磁盘满了", body["message"])
        self.assertEqual(body["changed"], ["src/a.py"], "现场要带给界面")
        self.assertIn("backup", body)

    def test_修复接口走_repair_并且会重启(self):
        seen = {}

        def fake_repair(root, *, current="", mode="auto"):
            seen["mode"] = mode
            return {"ok": True, "to": "1.5.0", "count": 3}

        with mock.patch.object(web.selfupdate, "repair", fake_repair), \
                mock.patch.object(web.selfupdate, "restart_later", lambda r: True):
            status, body = self.srv.request("POST", "/api/update", {"repair": "rerun"})
        self.assertEqual(seen["mode"], "auto")
        self.assertTrue(body["ok"])
        self.assertTrue(body["restarting"])
        self.assertIn("1.5.0", body["message"])

    def test_修复接口_restore_走备份(self):
        seen = {}

        def fake_repair(root, *, current="", mode="auto"):
            seen["mode"] = mode
            return {"ok": True, "message": "已恢复 12 个文件"}

        with mock.patch.object(web.selfupdate, "repair", fake_repair), \
                mock.patch.object(web.selfupdate, "restart_later", lambda r: False):
            status, body = self.srv.request("POST", "/api/update", {"repair": "restore"})
        self.assertEqual(seen["mode"], "restore")
        self.assertIn("已恢复", body["message"])

    def test_修复失败时不重启(self):
        def fake_repair(root, *, current="", mode="auto"):
            return {"ok": False, "message": "找不到备份目录"}

        with mock.patch.object(web.selfupdate, "repair", fake_repair), \
                mock.patch.object(web.selfupdate, "restart_later",
                                  lambda r: self.fail("修复失败不该重启")):
            status, body = self.srv.request("POST", "/api/update", {"repair": "restore"})
        self.assertFalse(body["ok"])
        self.assertIn("找不到备份", body["message"])

    def test_概览页带出没走完的升级(self):
        with mock.patch.object(web.selfupdate, "pending",
                               lambda root: {"state": "failed", "to": "1.5.0"}):
            status, body = self.srv.request("GET", "/api/overview")
        self.assertEqual(body["update_pending"]["state"], "failed")

    def test_post_without_ref_still_upgrades(self):
        """不带 ref 的老行为不能变（升级）。"""
        called = {}

        def fake_apply(root, *, current=""):
            called["upgrade"] = True
            return {"ok": True, "to": "1.5.0", "changed": [], "count": 0}

        with mock.patch.object(web.selfupdate, "apply_update", fake_apply), \
                mock.patch.object(web.selfupdate, "restart_later", lambda r: False):
            status, body = self.srv.request("POST", "/api/update", {})
        self.assertTrue(called.get("upgrade"))
        self.assertIn("更新到", body["message"])
        self.assertFalse(body.get("rollback"))

    def test_界面不再有历史回退入口(self):
        self.assertNotIn("btn-history-load", Path("web/index.html").read_text(encoding="utf-8"))
        self.assertNotIn("data-rollback", APP_JS)


class TestElevationWarning(unittest.TestCase):
    """以管理员身份跑服务时，**启动就要说**。

    管理员身份是浏览器自动化的已知杀手：Edge / Chrome 拒绝以管理员运行，
    会在跑到一半时把自己降权重启。门店真踩过，而且是**抓会话失败之后**
    才发现是权限问题 —— 那时候人已经在对着"退出码 None"猜了。
    """

    def test_serve_warns_when_elevated(self):
        import inspect
        src = inspect.getsource(web.serve)
        self.assertIn("is_elevated", src, "启动时没检查权限身份")
        self.assertIn("管理员", src, "要让门店一眼看懂现在是什么身份")

    def test_the_warning_says_how_to_fix_it(self):
        """光说"你是管理员"没用 —— 得给出能照着做的下一步。"""
        import inspect
        src = inspect.getsource(web.serve)
        self.assertIn("start.bat", src, "要告诉人怎么改用普通权限启动")
        self.assertIn("不受影响", src, "要说清对账本身没事，免得门店以为废了")


class TestServeStartupCannotBeBlocked(unittest.TestCase):
    """**服务启动绝不能被这类东西挡住。**

    门店实测：v1.3.1 之后，原来用管理员权限能跑的那台机器，服务**起不来了**。
    根因是启动路径上的输出：后台服务由 `pythonw.exe` 起（没有控制台），
    或 stdout 被重定向到文件时，Python 按 locale 编码写输出（中文 Windows 是
    GBK）—— 打印 `⚠`（U+26A0）这类符号会抛 `UnicodeEncodeError`，
    而它就在 `serve_forever()` 前面，一抛服务就永远起不来。

    教训：**启动路径上的输出只能"尽力而为"，不能有失败模式。**
    """

    def test_the_emoji_that_broke_the_store_is_gone_from_serve(self):
        """`⚠` 编不进 GBK —— **真正会打印的字符串里**不许再有它。

        只看 print() 的字面量，不看注释：注释不输出，留在那里当教训正合适。
        """
        import ast
        import inspect
        src = textwrap.dedent(inspect.getsource(web.serve))
        printed = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        printed.append(arg.value)
        self.assertTrue(printed, "没找到任何 print 字面量，测试要跟着改")
        for text in printed:
            for bad in ("⚠", "✅", "❌", "🆕", "→"):
                self.assertNotIn(bad, text,
                                 f"serve() 会打印 {bad!r} —— GBK 编不出来会炸：{text[:40]}")

    def test_gbk_really_cannot_encode_it(self):
        """把这条钉死：这个字符确实编不进 GBK（否则上面那条测试就是无病呻吟）。"""
        with self.assertRaises(UnicodeEncodeError):
            "⚠".encode("gbk")

    def test_the_elevation_hint_is_wrapped_and_cannot_throw(self):
        """权限提示必须包在 try 里 —— 它不是个"必须成功"的操作。"""
        import inspect
        src = inspect.getsource(web.serve)
        self.assertIn("try:", src)
        # 提示块之后紧跟着兜底，不能再让异常往外走
        idx = src.index("is_elevated")
        tail = src[idx:]
        self.assertIn("except Exception", tail[:1200],
                      "权限提示抛出去就会挡住整个服务启动")

    def test_the_update_watcher_is_wrapped_too(self):
        """更新检查线程同理 —— 它跟对账一点关系都没有，不能挡住启动。"""
        import inspect
        src = inspect.getsource(web.serve)
        self.assertIn("更新检查线程没起来", src, "线程起不来要降级，不能抛出去")


class TestStdoutHardening(unittest.TestCase):
    """进程级保险：**任何**字符编不出来时都不该抛异常。

    这条修的是一个反复踩的坑 —— 提示文案里加个符号，门店的服务就起不来。
    与其每次靠人记得"别用 emoji"，不如让输出层直接兜住。
    """

    def test_harden_stdout_never_raises(self):
        from src import cli
        cli._harden_stdout()          # 不该抛

    def test_harden_stdout_is_wired_into_main(self):
        """没接进 main() 的话，等于没做。"""
        import inspect
        from src import cli
        self.assertIn("_harden_stdout()", inspect.getsource(cli.main),
                      "main() 开头必须先加固 stdout")

    def test_replace_errors_swallow_unencodable_chars(self):
        """`errors="replace"` 的实际效果：编不出来变 `?`，不抛。"""
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="gbk", errors="replace")
        stream.write("  ⚠ 管理员\n")
        stream.flush()
        self.assertIn(b"?", buf.getvalue(), "编不出来的字符应该退化成 ?")


class TestServiceStartWait(unittest.TestCase):
    """`start.bat` 等后台服务就绪时，**超时不等于失败**。

    门店实测：窗口提示"要按回车"，回车之后服务其实是好的 ——
    因为门店电脑第一次冷启动慢（杀毒实时扫描 pythonw.exe、机械盘），
    超过了原来的 25 秒等待，于是被误报成"启动失败"。

    教训：这里的判断标准是"服务能不能用"，不是"它在 25 秒内起没起来"。
    """

    def _run(self, ready_after, *, running=False):
        """ready_after：第二次确认（多给 10 秒）时服务起来了吗。"""
        import io as _io
        import contextlib
        from src import cli

        calls = {"n": 0}

        def fake_wait(root, timeout=60):
            calls["n"] += 1
            return {"host": "127.0.0.1", "port": 8787} if ready_after else None

        buf = _io.StringIO()
        with mock.patch.object(cli.service, "find_running",
                               lambda root, *a, **k: ({"host": "127.0.0.1", "port": 8787}
                                                     if running else None)), \
                mock.patch.object(cli.service, "wait_ready", fake_wait), \
                mock.patch.object(cli, "_spawn_detached", lambda argv: None), \
                mock.patch.object(cli, "webbrowser", types.SimpleNamespace(
                    open=lambda u: None), create=True), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_service_start(types.SimpleNamespace(timeout=60))
        return code, buf.getvalue(), calls

    def test_slow_start_that_eventually_comes_up_is_a_success(self):
        """慢了点但起来了 → 报成功，别吓唬门店。"""
        code, out, calls = self._run(ready_after=True)
        self.assertEqual(code, 0, "服务最后起来了，不该报失败")
        self.assertIn("启动成功", out)
        self.assertEqual(calls["n"], 1, "没超时就不该多等一轮")

    def test_timeout_message_does_not_pretend_it_is_a_hard_failure(self):
        """真超时了也要说清：窗口关掉不影响服务，先等等再试。"""
        code, out, calls = self._run(ready_after=False)
        self.assertEqual(code, 2)
        self.assertIn("等待超时", out)
        self.assertIn("还在启动", out, "要说清服务可能只是慢，不是坏了")
        self.assertIn("关掉", out, "窗口关掉不影响后台服务 —— 门店最容易误会的点")
        self.assertIn("127.0.0.1:8787", out, "给个能直接打开的地址")
        self.assertEqual(calls["n"], 2, "超时之后必须再确认一次才报故障")

    def test_the_default_wait_is_long_enough_for_a_cold_start(self):
        """默认等待别退回 25 秒 —— 门店冷启动会超。"""
        # ⚠ 原来的写法是 `inspect.getsource(cli.main)` 里找 'default=60' ——
        #   解析器搬进 `build_parser()` 之后那个取样点就空了。
        #   改成**内省解析器本身**：源码怎么挪都不影响，而且量的是真值不是文本。
        from src import cli
        ap = cli.build_parser()
        sub = [a for a in ap._actions
               if hasattr(a, "choices") and a.choices and "service-start" in a.choices][0]
        opt = [a for a in sub.choices["service-start"]._actions
               if "--timeout" in a.option_strings][0]
        self.assertGreaterEqual(opt.default, 60,
                                "service-start 的等待时间不该短于 60 秒")


class TestRunLogFeed(unittest.TestCase):
    """计划任务跑的时候是个**黑盒** —— 用户点完「执行」什么都看不到。

    反馈原话就是"点了也不推送"。其实日志里写得清清楚楚
    （`[6/6] 企微：跳过（配置的是「仅有差异时发」，本次无差异）`），
    只是没人看得到。这个接口就是把日志捞出来给界面。
    """

    LOG = """=== 2026-09-15 21:00:01 开始 ===
=== 对账 青岛新业广场店（标识 Y）目标日 2026-09-14 ===
[1/6] 华为会话：✅ 会话有效
[2/6] 云商销量：446 行
[5/6] 邮件：跳过（配置的是「仅有差异时发」，本次无差异）
[6/6] 企微：✅ 已推送到群
[2026-09-15 21:01:12] exit=0
"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.app = web.App(self.root, "config/store-X.yaml")

    def _write(self, text):
        (self.root / "out" / "run.log").write_text(text, encoding="utf-8")

    def test_missing_log_is_not_an_error(self):
        d = web.read_run_log(self.app)
        self.assertFalse(d["exists"])
        self.assertIsNone(d["exit"])

    def test_reads_lines_and_exit_code(self):
        self._write(self.LOG)
        d = web.read_run_log(self.app)
        self.assertTrue(d["exists"])
        self.assertEqual(d["exit"], 0)
        self.assertTrue(any("云商销量" in x for x in d["lines"]))

    def test_surfaces_the_push_outcome(self):
        """推没推、为什么没推 —— 单独挑出来，别埋在 150 行里。"""
        self._write(self.LOG)
        d = web.read_run_log(self.app)
        self.assertIn("✅", d["wecom"])
        self.assertIn("跳过", d["mail"])
        self.assertIn("无差异", d["mail"])

    def test_takes_the_last_exit_code(self):
        """日志是**追加**的 —— 要拿最后一次的退出码，不是第一次的。"""
        self._write(self.LOG + self.LOG.replace("exit=0", "exit=3"))
        self.assertEqual(web.read_run_log(self.app)["exit"], 3)

    def test_tail_limit_and_flag(self):
        self._write("\n".join(f"第 {i} 行" for i in range(300)))
        d = web.read_run_log(self.app, tail=10)
        self.assertEqual(len(d["lines"]), 10)
        self.assertTrue(d["truncated"])

    def test_endpoint_is_wired(self):
        self._write(self.LOG)
        srv = _Server(self.root)
        self.addCleanup(srv.close)
        st, body = srv.request("GET", "/api/runlog")
        self.assertEqual(st, 200)
        self.assertEqual(body["exit"], 0)


class TestSessionSelfCheck(unittest.TestCase):
    """会话页那个"没验证过"的横幅。

    用户报的现象：文件在、提示未验证 → 点自检 → **提示还在**。
    两个原因叠在一起：
      1. 那句提示是前端**写死的字符串**，自检只更新了 toast 和右上角徽章；
      2. 自检结果**根本没存下来**，刷新页面当然还是"未验证"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        q = mock.patch.object(service, "find_running", lambda *a, **k: None)
        q.start()
        self.addCleanup(q.stop)
        self.app = web.App(self.root, "config/store-X.yaml")

    def _save_session(self, cookies="JSESSIONID=a; hwssot3=b"):
        from src.session import CbgSession
        s = CbgSession(cookies=cookies, csrf="CSRFTOKEN123456", source="test")
        # ⚠ 存到 **App 认的那个路径**（配置文件缺失时是 cbg-default.json），
        #   自己拼一个 cbg-SCN231409.json 的话 session_info() 根本看不到
        s.save(self.app.session_path())
        return s

    def test_records_and_reports_the_check(self):
        s = self._save_session()
        self.app.record_check(s, True, "门店可访问")
        info = self.app.session_info()
        self.assertTrue(info["check_ok"])
        self.assertIn("checked_at", info)
        self.assertEqual(info["check_message"], "门店可访问")

    def test_reports_a_failed_check(self):
        s = self._save_session()
        self.app.record_check(s, False, "会话已过期（401）")
        info = self.app.session_info()
        self.assertFalse(info["check_ok"])
        self.assertIn("401", info["check_message"])

    def test_never_checked_has_no_timestamp(self):
        self._save_session()
        info = self.app.session_info()
        self.assertNotIn("checked_at", info, "没自检过就不该有时间")

    def test_a_new_session_invalidates_the_old_check(self):
        """⚠ 换过会话之后，旧的自检记录**必须作废**。

        否则界面会拿旧会话的"✅ 通过"给新会话背书 —— 比什么都不显示更糟。
        """
        old = self._save_session("JSESSIONID=old; hwssot3=old")
        self.app.record_check(old, True, "老的通过了")

        self._save_session("JSESSIONID=brand-new; hwssot3=new")   # 重新抓了一份
        info = self.app.session_info()
        self.assertNotIn("check_ok", info, "新会话被旧的'通过'背书了")
        self.assertTrue(info.get("check_stale"),
                        "要告诉前端'记录作废了'，它自己判断不出来")

    def test_changing_the_store_invalidates_the_check(self):
        """⚠ 门店也是自检的一部分。

        自检 = "拿这份会话去查**配置里那个门店**"。改了门店（设置页那三行，
        改完**立刻生效、不用重启**）之后，同一个会话的有效性就完全变了：

        * 从"没权限的店"改成"有权限的店" → 旧记录说没过，其实现在能过
        * 反过来 → 旧记录说过了，**其实现在根本查不了**

        这里固定住 session.file，单独验指纹这一层。
        """
        s = self._save_session()
        (self.root / "config").mkdir(exist_ok=True)
        cfg = self.root / "config" / "store-X.yaml"
        fixed = "session:\n  file: .secrets/session.json\n"

        cfg.write_text("store_code: AAA\n" + fixed, encoding="utf-8")
        s.save(self.app.session_path())              # 固定路径，换店不会换文件
        self.app.record_check(s, True, "A 店能查")
        self.assertTrue(self.app.last_check(s), "同一个店、同一份会话，记录该有效")

        cfg.write_text("store_code: BBB\n" + fixed, encoding="utf-8")
        self.assertFalse(self.app.last_check(s),
                         "换了门店，旧的自检记录必须作废 —— 它验的是另一个店")
        info = self.app.session_info()
        self.assertNotIn("check_ok", info)
        self.assertTrue(info.get("check_stale"), "要告诉前端'要重新自检'")

    def test_changing_the_store_switches_the_session_file(self):
        """⚠ 没固定 session.file 时，**改门店 = 换一份会话文件**。

        会话文件名里嵌着门店码（`.secrets/cbg-<门店码>.json`），因为华为会话
        本来就是**按店**的。所以改完门店，会话页会显示"还没有会话" ——
        这不是 bug，是要求你**用那个店的账号重新登录一次**。

        这条测试是为了钉住这个行为：哪天有人想改成"共用一份会话"，
        会先在这儿红，然后被迫想清楚跨店复用会话到底对不对。
        """
        s = self._save_session()                     # 存到 store_code 对应的名字
        (self.root / "config").mkdir(exist_ok=True)
        cfg = self.root / "config" / "store-X.yaml"
        cfg.write_text("store_code: AAA\n", encoding="utf-8")
        s.save(self.app.session_path())
        self.assertTrue(self.app.session_info()["exists"])

        cfg.write_text("store_code: BBB\n", encoding="utf-8")
        info = self.app.session_info()
        self.assertFalse(info["exists"],
                         "换了门店该去找那个店自己的会话文件")
        self.assertIn("BBB", self.app.session_path().name)

    def test_record_survives_a_page_refresh(self):
        """自检结果**要落盘** —— 不然刷新一下又变回"未验证"。"""
        s = self._save_session()
        self.app.record_check(s, True, "ok")
        fresh = web.App(self.root, "config/store-X.yaml")       # 相当于重启服务
        self.assertTrue(fresh.session_info()["check_ok"])

    def test_corrupt_check_file_does_not_break_the_page(self):
        s = self._save_session()
        (self.root / ".secrets" / "session-check.json").write_text("{坏了", encoding="utf-8")
        info = self.app.session_info()                          # 不该抛
        self.assertNotIn("check_ok", info)

    def test_ping_endpoint_records_the_result(self):
        """自检按钮走的是这个接口 —— 结果必须被记下来。"""
        s = self._save_session()
        client = mock.Mock()
        client.session = s
        client.ping.return_value = (True, "门店可访问")
        with mock.patch.object(web.App, "cbg_client", lambda self: client):
            res = web.Handler._ping_result(self.app)
        self.assertTrue(res["ping"]["ok"])
        self.assertTrue(self.app.session_info()["check_ok"])


class Test手动跑的反馈在抽屉里(unittest.TestCase):
    """⚠ 2026-09-20（用户：「把兜底去掉吧，不用系统的计划任务」）之后，
    **手动跑**是唯一"点了要看到过程"的入口 —— 它走 `runner` + 右下角那个抽屉
    （`/api/run` 轮询 + `setRunDrawer`）。

    以前还有一条"点计划任务的「执行」、再盯 out/run.log"的路
    （`watchRunLog` + `runlog-progress`）—— 那条**随兜底一起删了**：
    旧任务现在只做"确保服务在跑"，点它等于白跑一趟。

    ⚠ 这里钉的是"**反馈不能是黑盒**"这条规矩本身：
      它当年的教训是「点了也不推送」——其实推送只是被跳过了，日志里写得清清楚楚，
      而界面上什么都看不到。换个入口，规矩不变。
    """

    def test_抽屉会轮询运行日志(self):
        self.assertIn("setRunDrawer", APP_JS)
        self.assertIn("/api/run", APP_JS)

    def test_旧的那条看日志的路撤干净了(self):
        for gone in ("watchRunLog", "runlog-progress", "runlog-box"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, APP_JS, "兜底那套的看日志又回来了？")

    def test_日志是逐行原样显示的(self):
        """✅ / 跳过 **逐行保留自己的记号**，不要整块染成绿的 ——
        整块绿的话"邮件跳过"也会跟着变绿，反而误导。

        ⚠ 2026-09-20：原来这条盯的是 `watchRunLog` 里那几行
          （它自己解析 `d.wecom` / `d.mail` 再拼成一块 banner）——
          那条路随兜底一起删了。现在**抽屉直接把日志行原样贴出来**
          （`textContent += job.lines.join('\n')`），记号本来就跟着行走的，
          比对着一份解析结果更不容易撒谎。
        """
        i = APP_JS.index("function appendLog(job)")
        blk = APP_JS[i:i + 400]
        self.assertIn("textContent", blk, "日志要按文本贴，别当 HTML 拼")
        self.assertIn("job.lines.join", blk)


class TestSessionBannerIsNotHardcoded(unittest.TestCase):
    def test_banner_branches_on_the_check_result(self):
        """回归：那句"没验证过"曾经是**写死的**，自检完也不变。"""
        self.assertIn("s.check_ok === true", APP_JS)
        self.assertIn("s.check_ok === false", APP_JS)
        self.assertIn("s.checked_at", APP_JS, "要显示上次自检时间")
        self.assertIn("上次自检", APP_JS)

    def test_ping_refreshes_the_page_state(self):
        """自检完必须重新拉一次数据，否则横幅还是老的。"""
        seg = APP_JS[APP_JS.index("$('#btn-ping')"):]
        seg = seg[:seg.index("$('#btn-import')")]       # 这个 handler 的完整范围
        self.assertIn("loadOverview()", seg,
                      "自检完没重新拉状态 —— 横幅不会更新")


class TestScheduleApiRegistersMultipleTasks(unittest.TestCase):
    """**走真 HTTP 接口**注册多次，看后端到底发出了什么 schtasks 命令。

    单测直接调 `schedule.install` 是测不到"接口有没有把名字弄丢"的 ——
    用户报过"注册两个只显示一个"，光看返回值会以为没问题，实际是 `/tn` 全一样。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.calls = []

        def fake_schtasks(args, timeout=20):
            self.calls.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        for target, attr, val in (
                (schedule, "_schtasks", fake_schtasks),
                (schedule, "kind", lambda: "windows"),
                (service, "find_running", lambda *a, **k: None)):
            q = mock.patch.object(target, attr, val)
            q.start()
            self.addCleanup(q.stop)
        self.srv = _Server(self.tmp)
        self.addCleanup(self.srv.close)

    def _created(self):
        return [c[c.index("/tn") + 1] for c in self.calls if "/create" in c]

    def test_two_registrations_produce_two_distinct_tasks(self):
        self.srv.request("POST", "/api/schedule", {"time": "12:00", "days_ago": 0})
        self.srv.request("POST", "/api/schedule", {"time": "21:00", "days_ago": 1})
        self.assertEqual(self._created(),
                         ["门店数据拉取与计算-12点00", "门店数据拉取与计算-21点00"],
                         "两次注册必须落到两个不同的任务名，否则就是互相覆盖")

    def test_same_time_twice_is_an_intentional_overwrite(self):
        for _ in range(2):
            self.srv.request("POST", "/api/schedule", {"time": "21:00"})
        self.assertEqual(self._created(),
                         ["门店数据拉取与计算-21点00", "门店数据拉取与计算-21点00"])

    def test_explicit_name_wins_over_the_time_default(self):
        self.srv.request("POST", "/api/schedule", {"time": "21:00", "name": "打烊那次"})
        self.assertEqual(self._created(), ["打烊那次"])


class TestRunLogIsFlushed(unittest.TestCase):
    """日志必须**每行刷盘**。

    stdout 重定向到文件时是块缓冲 —— 进程被 kill / 卡住 / 崩了，缓冲区整个丢掉。
    而计划任务日志的全部意义就是"出事之后还能看"。实测踩到：跑了一分钟，
    `out/run.log` 是 **0 字节**。
    """

    def test_tee_flushes_on_every_line(self):
        # ⚠ 必须用**真文件**：StringIO 不缓冲，测不出"没刷盘"这个 bug
        #   （第一版就是这么写的，把 flush 删掉测试照样绿）。
        import io
        from src.cli import _Tee
        d = Path(tempfile.mkdtemp())
        f = d / "run.log"
        with open(f, "a", encoding="utf-8") as fp:      # 块缓冲，跟真实情况一样
            _Tee(io.StringIO(), fp).write("第一行\n")
            # 不 close、不 flush —— 模拟"进程正好在这一刻被杀"
            self.assertIn("第一行", f.read_text(encoding="utf-8"),
                          "换行之后没刷盘 —— 进程被杀就什么都留不下")

    def test_tee_mirrors_to_both_streams(self):
        import io
        from src.cli import _Tee
        a, b = io.StringIO(), io.StringIO()
        tee = _Tee(a, b)
        tee.write("x")
        self.assertEqual(a.getvalue(), "x")
        self.assertEqual(b.getvalue(), "x")


class TestHiddenAttributeActuallyHides(unittest.TestCase):
    """⚠ `hidden` 属性必须**真的**能藏住元素。

    HTML 的 `hidden` 靠浏览器内置样式 `[hidden] { display: none }` 生效，
    而那条内置规则**优先级最低** —— 任何 `.form-row { display: flex }`、
    `.btn { display: inline-block }` 都能压过它。

    真踩过：后台服务那个「以管理员身份修复」按钮**一直显示着**，
    不管条件满不满足 —— 用户连着两轮截图里都有它，还去点了；
    `#btn-update-apply`（立即更新）也一样，哪怕根本没有新版本。
    这类 bug 的特点是：**JS 逻辑全对、测试全绿，只有肉眼看得出来**。
    """

    CSS = Path(__file__).resolve().parent.parent / "web" / "style.css"
    HTML = Path(__file__).resolve().parent.parent / "web" / "index.html"

    def _css(self) -> str:
        """style.css **去掉注释**后的正文。

        ⚠ 必须剥注释：解释这条规则的注释里**也写着 `[hidden]`**，
        直接 `split("[hidden]")` 会先命中注释那一处，
        然后拿到一段没有 `!important` 的文字 —— 测试假红。
        （这个坑在 JS 那边已经踩过一次了，见 `_js_code()`。）
        """
        import re
        css = self.CSS.read_text(encoding="utf-8")
        return re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def test_style_sheet_has_a_hidden_fallback(self):
        """⚠ 别用 `css.split("[hidden]")[1]` 找那条兜底 —— **它假定兜底是全文第一处
        `[hidden]`**。2026-09-19 加了下左角的设置浮层（`.side-menu[hidden]`），
        它排在那条兜底**前面**，于是切出来的是浮层那条
        （浮层自己那条本来就不该有 `!important`，它的类没设 display），假红。
        → 按**行首的裸选择器**锚，那才是全局兜底那一条。"""
        css = self._css()
        self.assertIn("[hidden]", css,
                      "style.css 要有 [hidden] 兜底，否则类选择器会压过它")
        m = re.search(r"(?m)^\[hidden\]\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "找不到裸的 `[hidden] { … }` 兜底规则")
        self.assertIn("!important", m.group(1),
                      "不加 !important 压不过 .form-row / .btn 那些规则")

    def test_every_hidden_element_has_a_display_rule_covered(self):
        """逐个核对：**带 `hidden` 的元素，它的类有没有设 display**。

        这条比上一条更实在 —— 上一条只证明"兜底在"，
        这条证明"兜底是必要的、而且覆盖到了真实元素"。
        """
        import re
        html = self.HTML.read_text(encoding="utf-8")
        css = self._css()
        # 抽出所有 <tag ... hidden ...>，拿到它的 class
        tags = re.findall(r"<[a-z][^>]*\bhidden\b[^>]*>", html)
        self.assertTrue(tags, "一个 hidden 元素都没有？那这条测试没意义了")
        at_risk = []
        for tag in tags:
            m = re.search(r'class="([^"]*)"', tag)
            if not m:
                continue                      # 没类 → 内置规则就够
            for cls in m.group(1).split():
                # 精确找 `.cls {` 这种规则里有没有 display
                for body in re.findall(r"(?:^|[},])\s*\." + re.escape(cls) + r"\s*\{([^}]*)\}",
                                       css):
                    if "display" in body:
                        at_risk.append((cls, tag[:60]))
        # 有兜底规则在，这些就都不怕了 —— 所以断言的是"兜底必须在"
        self.assertTrue(at_risk, "这条测试的前提变了（没有危险的类了）—— 复核一下")
        self.assertIn("[hidden]", css, f"这些类会压过 hidden：{at_risk}")


class TestElevateApi(unittest.TestCase):
    """按需提权那个端点（`/api/elevate`）—— **只把需要管理员的那一步**弹一次 UAC。

    ⚠ 这条路由在 Windows 上的提权行为**没法在本机验**（`ShellExecuteW` 那一套），
    但路由本身、参数传递、失败分支**必须**是真的 —— 界面上那两个按钮
    （「以管理员身份修复」「以管理员身份重试」）全靠它，
    写错了表现是"点了没反应"，门店完全无从下手。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.srv = _Server(self.tmp)
        self.addCleanup(self.srv.close)

    def _post(self, body, runner):
        """打这个端点，把 `run_elevated` 换掉 —— 本机没有 UAC 可弹。"""
        with mock.patch.object(web.elevate, "run_elevated", runner), \
                mock.patch.object(web.elevate, "is_admin", lambda: False):
            return self.srv.request("POST", "/api/elevate", body)

    def test_autostart_repair_runs_the_autostart_subcommand(self):
        seen = {}

        def runner(script, args, timeout=90):
            seen["script"] = script
            seen["args"] = list(args)
            return {"ok": True, "message": "已注册"}

        st, body = self._post({"what": "autostart"}, runner)
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(str(seen["script"]).endswith("bootstrap.py"))
        self.assertEqual(seen["args"], ["autostart"],
                         "修复开机自启＝提权跑普通权限那条 autostart（顺带删掉旧提权任务）")

    def _fake_schedule(self, existing=()):
        """把 schedule 那层换掉，记下**提权子命令**收到了什么。

        ⚠ 这里**故意不去 mock `schedule.install`** —— 因为这条路根本不调它：
        提权那一步是"另起一个进程跑 `bootstrap.py`"，参数只能从命令行过去。
        mock 掉 install 会让测试"看起来很对"，实际什么都没验到。
        """
        seen = {}
        patches = [
            mock.patch.object(web.schedule, "status",
                              lambda root: {"installed": bool(existing),
                                            "tasks": list(existing)}),
            mock.patch.object(web.schedule, "DEFAULT_TIME", "21:00"),
            mock.patch.object(web.schedule, "DEFAULT_DAYS_AGO", 1),
        ]
        for q in patches:
            q.start()
            self.addCleanup(q.stop)
        return seen

    @staticmethod
    def _arg(args, flag, default=None):
        """从命令行参数里取值 —— 替掉"直接看函数参数"的断言。"""
        return args[args.index(flag) + 1] if flag in args and \
            args.index(flag) + 1 < len(args) else default

    def test_schedule_repair_elevates_the_create(self):
        """⚠ **这个按钮必须真的去"建"**，不能只是删旧的。

        门店实测过：普通权限点注册，`schtasks /create` 直接报
        `错误: 拒绝访问。`（旧任务被管理员建过时 `/f` 覆盖不了；账户被 UAC
        过滤时更是压根建不了）。所以"提权只删、建还是普通权限建"那条路
        **在那台机器上走不通** —— 用户点「以管理员身份重试」会毫无反应。

        → 提权那一步直接跑 `schedule-install`（`/create` 自带 `/f`，
          旧同名任务一并覆盖），参数全部从命令行过去。
        """
        self._fake_schedule(existing=[{"name": "CBG报量对账-21点00"}])
        seen = {}

        def runner(script, args, timeout=90):
            seen["args"] = list(args)
            return {"ok": True, "message": "已注册"}

        st, body = self._post({"what": "schedule", "time": "20:30",
                               "days_ago": 2, "name": "CBG报量对账-20点30"}, runner)
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        a = seen["args"]
        self.assertEqual(a[0], "schedule-install",
                         "要跑的是「注册」，不是「删」")
        self.assertEqual(self._arg(a, "--time"), "20:30")
        self.assertEqual(self._arg(a, "--days-ago"), "2")
        self.assertEqual(self._arg(a, "--name"), "CBG报量对账-20点30")

    def test_schedule_repair_rejects_a_foreign_task_name_before_uac(self):
        """同名任务已属于 Windows 时，不能先弹 UAC 再尝试 /create /f 覆盖它。"""
        self._fake_schedule()
        elevated = mock.Mock(return_value={"ok": True})
        with mock.patch.object(web.schedule, "install_conflict", return_value=True) as conflict:
            status, body = self._post(
                {"what": "schedule", "time": "12:00", "name": "OneDrive"}, elevated)

        self.assertEqual(status, 409)
        self.assertIn("不属于本程序", body["error"])
        conflict.assert_called_once_with(self.tmp, "12:00", "OneDrive")
        elevated.assert_not_called()

    def test_repair_button_accepts_the_full_name_the_ui_sends(self):
        """⚠ 修复按钮手上只有 `full_name`（`\\CBG报量对账-21点20`）。

        而 `schedule.install` **拒收带 `\\` 的名字**（防路径注入）→
        不收敛成叶子名的话这个按钮**必然 400**，表现还是"点了没反应"。
        """
        self._fake_schedule()
        seen = {}
        st, body = self._post({"what": "schedule", "name": "\\CBG报量对账-21点20"},
                              lambda script, args, timeout=90:
                              seen.update(args=list(args)) or {"ok": True})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self._arg(seen["args"], "--name"), "CBG报量对账-21点20",
                         "带反斜杠的全名要收敛成叶子名")

    def test_schedule_remove_is_its_own_action(self):
        """删也要能提权 —— 管理员建的任务，普通权限连 `/delete` 都会被拒。"""
        self._fake_schedule(existing=[{"name": "CBG报量对账-中午",
                                      "full_name": "\\CBG报量对账-中午"}])
        seen = {}
        st, body = self._post({"what": "schedule-remove", "name": "CBG报量对账-中午"},
                              lambda script, args, timeout=90:
                              seen.update(args=list(args)) or {"ok": True})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(seen["args"][0], "schedule-remove")
        self.assertEqual(self._arg(seen["args"], "--name"), "CBG报量对账-中午")

    def test_schedule_uses_defaults_when_not_given(self):
        """不传参数就用默认值 —— 别把 `None` 或空串一路传下去。"""
        self._fake_schedule()
        seen = {}
        self._post({"what": "schedule"},
                   lambda script, args, timeout=90:
                   seen.update(args=list(args)) or {})
        self.assertEqual(self._arg(seen["args"], "--time"), "21:00")
        self.assertEqual(self._arg(seen["args"], "--days-ago"), "1")
        self.assertNotIn("--name", seen["args"],
                         "没给名字就别传空串，让 bootstrap 用它的默认命名")

    def test_denied_or_timed_out_uac_explains_itself(self):
        """用户点了「否」/ 忘了关那个窗口 → **要说清是哪一种**，别只报个失败。

        提权子进程的窗口会停住等回车；用户不关它，父进程就等不到结果。
        这条提示是唯一能让他反应过来"哦我还开着个窗口"的东西。
        """
        st, body = self._post({"what": "autostart"},
                              lambda script, args, timeout=90: None)
        self.assertEqual(st, 200)
        self.assertFalse(body["ok"])
        self.assertIsNone(body["elevated"], "拿不到结果要如实标出来")
        self.assertIn("UAC", body["message"])
        self.assertIn("窗口", body["message"], "要点明可能有个窗口还开着")

    def test_unknown_action_is_rejected(self):
        st, body = self._post({"what": "rm-rf"}, lambda *a, **k: {})
        self.assertEqual(st, 400)
        self.assertIn("不认识", body["message"])

    def test_refuses_when_already_elevated(self):
        """⚠ 服务本身就是管理员时**不许再弹 UAC** —— 那只会让人更糊涂。

        管理员身份本身就是要修掉的问题；再提一次权什么都解决不了。
        这条分支以前没有，是"怎么点都没反应"那类投诉的常见来源。
        """
        with mock.patch.object(web.elevate, "is_admin", lambda: True), \
                mock.patch.object(web.elevate, "run_elevated",
                                  mock.Mock(side_effect=AssertionError("不该走到提权"))):
            st, body = self.srv.request("POST", "/api/elevate", {"what": "autostart"})
        self.assertEqual(st, 200)
        self.assertFalse(body["ok"])
        self.assertIn("管理员", body["message"])
        self.assertIn("普通权限", body["message"], "要给出改回去的方向")


class TestScheduleApi(unittest.TestCase):
    def setUp(self):
        # ⚠ 不能用 enterContext —— 那是 Python 3.11+，门店电脑上是 3.9
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.srv = _Server(self.tmp)
        self.addCleanup(self.srv.close)

    def test_delete_forwards_the_selected_name(self):
        """用户明确要的：'删除的话删除选中的'。

        接口必须把 ?name= 传到 schedule.remove —— 传丢了就变成删默认任务，
        用户以为删的是第二个，其实删的是第一个。
        """
        seen = {}

        def fake_remove(name=None, root=None):
            seen["name"] = name
            seen["root"] = root
            return {"ok": True, "task": name or "默认", "message": "已删除"}

        listed = {"installed": True, "tasks": [{
            "name": "CBG报量对账-中午", "full_name": "\\CBG报量对账-中午"}]}
        with mock.patch.object(web.schedule, "remove", fake_remove), \
                mock.patch.object(web.schedule, "status", lambda root: listed):
            st, body = self.srv.request(
                "DELETE", "/api/schedule?name=" + quote("CBG报量对账-中午"))

        self.assertEqual(st, 200)
        self.assertEqual(seen.get("name"), "CBG报量对账-中午")
        self.assertEqual(body["task"], "CBG报量对账-中午")

    def test_delete_without_name_only_operates_when_default_task_is_listed(self):
        """省略名字仍指向默认任务，但清单里没有本程序任务时不能盲删。"""
        remove = mock.Mock(return_value={"ok": True})
        with mock.patch.object(web.schedule, "remove", remove), \
                mock.patch.object(web.schedule, "status", return_value={
                    "installed": False, "tasks": []}):
            status, body = self.srv.request("DELETE", "/api/schedule")

        self.assertEqual(status, 404)
        self.assertIn("已登记", body["error"])
        remove.assert_not_called()

    def test_delete_without_name_can_still_remove_a_listed_default_task(self):
        """有真实清单证明时，旧客户端省略名字仍能维护默认任务。"""
        remove = mock.Mock(return_value={"ok": True})
        listed = {"installed": True, "tasks": [{
            "name": schedule.TASK_NAME, "full_name": "\\" + schedule.TASK_NAME}]}
        with mock.patch.object(web.schedule, "remove", remove), \
                mock.patch.object(web.schedule, "status", return_value=listed):
            status, _body = self.srv.request("DELETE", "/api/schedule")

        self.assertEqual(status, 200)
        remove.assert_called_once_with(None, self.tmp)

    def test_install_rejects_bad_time_with_400(self):
        st, body = self.srv.request("POST", "/api/schedule", {"time": "25:99"})
        self.assertEqual(st, 400, "参数不合法要 400，前端才知道是输入问题")
        self.assertFalse(body["ok"])

    def test_status_shape_has_tasks_list(self):
        sch = schedule.status(self.tmp)
        self.assertIn("tasks", sch)
        self.assertIsInstance(sch["tasks"], list)

    def test_install_returns_task_and_status(self):
        def fake_install(root, time_str, days_ago, config, name=None):
            return {"ok": True, "task": name or schedule.TASK_NAME, "time": time_str}

        with mock.patch.object(web.schedule, "install", fake_install), \
                mock.patch.object(web.schedule, "status",
                                  lambda root: {"installed": True, "tasks": [{"name": "X"}]}):
            st, body = self.srv.request(
                "POST", "/api/schedule", {"time": "12:30", "name": "CBG报量对账-中午"})

        self.assertEqual(st, 200)
        self.assertEqual(body["task"], "CBG报量对账-中午")
        self.assertIn("status", body, "前端注册完要立刻刷新任务表")


if __name__ == "__main__":
    unittest.main()


class TestCaptchaInCaptureWorker(unittest.TestCase):
    """⚠ 删 profile 的**唯一**地方在这里 —— 只有它知道 `app.root`。

    而且必须在这儿删而不是 `browser.py`：`capture_session` 的 `finally`
    已经把浏览器关了，此时删才删得掉（Windows 有句柄就删不掉）。
    """

    def setUp(self):
        # ⚠ `capture_job` 是**模块级单例** —— 跑完必须复位，
        #   否则 `need_captcha` / `need` 会粘给后面的测试（这种"单跑绿、全量红"
        #   最难查，本项目踩过）。
        self.addCleanup(web.capture_job.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir()
        self.profile = self.root / ".secrets" / "browser-profile"
        (self.profile / "Default").mkdir(parents=True)
        (self.profile / "Local State").write_text("{}", encoding="utf-8")

    def _patches(self, capture):
        app = mock.Mock()
        app.root = self.root
        app.config_path = self.root / "config" / "store-x.yaml"
        app.session_path = lambda cfg: self.root / "sess.json"
        app.record_check = lambda *a, **k: None
        return app, [
            mock.patch.object(web.config_io, "load_raw", lambda p: {}),
            mock.patch.object(web.browser, "find_browser", lambda cfg: ("edge", "/x")),
            mock.patch.object(web.browser, "profile_path", lambda cfg, r: self.profile),
            mock.patch.object(web.browser, "login_url", lambda cfg: "http://x"),
            mock.patch.object(web.browser, "load_login_credentials",
                              lambda c, r: ("u", "p")),
            mock.patch.object(web.browser, "capture_session", capture),
        ]

    def _run(self, capture):
        app, patches = self._patches(capture)
        for q in patches:
            q.start()
            self.addCleanup(q.stop)
        web.capture_job.reset()
        web.capture_job.running = True
        web._capture_worker(app, headless=False)
        return dict(web.capture_job.snapshot())

    def test_captcha_abort_deletes_the_profile_and_sets_the_state(self):
        def boom(profile, **kw):
            raise web.browser.CbgCaptchaRequired(profile)

        snap = self._run(boom)
        self.assertEqual(snap["state"], "need_captcha")
        self.assertFalse(self.profile.exists(), "半成品 profile 要删掉")
        self.assertIn("验证码", snap["message"])

    def test_it_says_so_when_the_profile_survives(self):
        """删不掉要**写进消息**，不能假装删干净了。"""
        def boom(profile, **kw):
            raise web.browser.CbgCaptchaRequired(profile)

        with mock.patch.object(web.browser, "delete_profile",
                               lambda d: (False, "删不掉 profile：being used")):
            snap = self._run(boom)
        self.assertEqual(snap["state"], "need_captcha")
        self.assertIn("删不掉", snap["message"])

    def test_marked_state_means_no_credentials_are_passed(self):
        """⚠ 死循环的另一半：标记还在时，**不许**把凭据递给 capture_session。

        （`capture_session` 自己也会看 `state_root` 再挡一道，但那是双保险；
        这里钉的是 worker 这一层也要挡。）
        """
        web.browser.mark_captcha(self.root)
        seen = {}

        def fake_capture(profile, **kw):
            seen.update(kw)
            raise web.browser.CbgCaptchaRequired(profile)

        self._run(fake_capture)
        self.assertIsNone(seen.get("credentials"),
                          "上次撞了验证码，这次不许再自动填账号密码")
        self.assertEqual(seen.get("state_root"), self.root,
                         "state_root 没传下去的话，标记永远清不掉")

    def test_a_normal_failure_is_still_a_plain_error(self):
        """别的失败**不许**被当成验证码 —— 状态和文案都不一样。"""
        def other(profile, **kw):
            raise web.browser.CbgAuthError("超时了")

        snap = self._run(other)
        self.assertEqual(snap["state"], "error")
        self.assertTrue(self.profile.exists(), "不是验证码就别删 profile")


class TestLifehall保存点自检(unittest.TestCase):
    """0 订单新店（认不出店码）：保存点要显示「已保存」。

    2026-09-27 毛刺：店码空着时保存点照样 `ping()`，`store_detail` 必报
    「接口异常：没给 storeCode，无法查门店详情」→ 界面把成功显示成
    「已保存，但自检没过」。判据只许走 `store_identity.check_after_save`。
    """

    def setUp(self):
        self.addCleanup(web.capture_job.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir()

    def test_没店码时保存点放行并说清(self):
        from src import store_identity

        app = mock.Mock()
        app.root = self.root
        app.config_path = self.root / "config" / "store-x.yaml"
        app.session_path = lambda cfg: self.root / "sess.json"
        app.record_check = lambda *a, **k: None

        class _Sess:
            def save(self, p):
                Path(p).write_text("{}", encoding="utf-8")
                return Path(p)

        with mock.patch.object(web._edition, "is_lifehall", return_value=True), \
                mock.patch.object(store_identity, "identify",
                                  return_value={"ok": False, "probe": "empty",
                                                "why": "这 30 天没有查到本店订单，"
                                                       "认不出店码（不影响使用）"}), \
                mock.patch.object(web.config_io, "load_raw", lambda p: {}), \
                mock.patch.object(web.browser, "find_browser",
                                  lambda cfg: ("edge", "/x")), \
                mock.patch.object(web.browser, "profile_path",
                                  lambda cfg, r: self.root / "prof"), \
                mock.patch.object(web.browser, "login_url", lambda cfg: "http://x"), \
                mock.patch.object(web.browser, "load_login_credentials",
                                  lambda c, r: ("u", "p")), \
                mock.patch.object(web.browser, "capture_session",
                                  lambda profile, **kw: _Sess()):
            web.capture_job.reset()
            web.capture_job.running = True
            web._capture_worker(app, headless=False)
            snap = dict(web.capture_job.snapshot())

        self.assertEqual(snap["state"], "ok",
                         "empty 是真会话 —— 不该被 ping 打成没过")
        self.assertIn("店码还没认出来", snap["message"])
        self.assertNotIn("没给 storeCode", snap["message"])


class Test手输店码接口(unittest.TestCase):
    """手输店码从真实 HTTP 路由写入本机配置，并在登录前可用。"""

    def setUp(self):
        from src import edition
        self._old_edition = os.environ.get("CBG_EDITION")
        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(self._restore_edition)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir(parents=True)
        shutil.copy(ROOT / "config" / "stores.yaml",
                    self.root / "config" / "stores.yaml")
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def _restore_edition(self):
        from src import edition
        if self._old_edition is None:
            os.environ.pop("CBG_EDITION", None)
        else:
            os.environ["CBG_EDITION"] = self._old_edition
        edition.reload()

    def test_PUT成功且配置文件真实写入(self):
        from src import config_io
        status, body = self.srv.request(
            "PUT", "/api/session/store-code", {"store_code": "SCN328987"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["found"])
        self.assertEqual(body["store_name"], "青岛CBD万达店")
        v = config_io.pick(config_io.load_raw(self.root / "config" / "store-X.yaml"))
        self.assertEqual(v["store_code"], "SCN328987")
        self.assertEqual(v["erp_store_name"], "青岛CBD万达店")

    def test_非法输入返回400(self):
        status, body = self.srv.request(
            "PUT", "/api/session/store-code", {"store_code": "A B"})
        self.assertEqual(status, 400, body)
        self.assertIn("error", body)

    def test_登录尚未就绪时仍可保存店码(self):
        # 生活馆现在凭编码进入；通用服务夹具已有编码，要先清空才是未就绪。
        from src import config_io
        config_io.update(self.root / "config" / "store-X.yaml", {"store_code": ""})
        status, setup = self.srv.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertFalse(setup["ready"], "夹具必须处于未登录状态")
        status, body = self.srv.request(
            "PUT", "/api/session/store-code", {"store_code": "SCN328987"})
        self.assertEqual(status, 200, body)

    def test_full版不开放生活馆手输接口(self):
        from src import edition
        config_path = self.root / "config" / "store-X.yaml"
        before = config_path.read_bytes()
        os.environ["CBG_EDITION"] = "full"
        edition.reload()
        try:
            status, body = self.srv.request(
                "PUT", "/api/session/store-code", {"store_code": "SCN328987"})
        finally:
            os.environ["CBG_EDITION"] = "lifehall"
            edition.reload()
        self.assertEqual(status, 404, body)
        self.assertEqual(config_path.read_bytes(), before,
                         "full 版拒绝接口时不得改门店配置")


class TestRunnerScriptSelfHeal(unittest.TestCase):
    """⚠ **发版阻断的解法**（方案 B）：概览页顺手自愈 `run.bat`。

    `run.bat` / `run-now.bat` 是**安装时生成、不进版本库**的，
    自更新**不会重写它们**。而 2.0.0 的日常流程换了命令
    （`check` → `daily`）—— 门店那份 bat 不改的话，`check` 会每天
    以「库不新鲜」失败。

    `schedule.refresh_runner_scripts()` 本来就是干这个的，**但它从来没跑过**：
    它挂在 `GET /api/schedule` 上，而前端**从不 GET 那个路径**
    （只有 POST 注册 / POST 运行 / DELETE 删除）。
    改挂到概览页 —— 概览是每次开界面都会拉的。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        # ⚠ `script_path()` 按平台给 `run.bat` 还是 `run.sh` ——
        #   整类都得当成 Windows，否则 fixture 写的是 run.sh、
        #   而重建去找的是 run.bat，两边对不上（第一版就这么错的）。
        p = mock.patch.object(schedule, "kind", lambda: "windows")
        p.start()
        self.addCleanup(p.stop)
        self.app = web.App(self.root, "config/store-X.yaml")

    def _legacy_bat(self):
        """写一份老门店那样的 v4 脚本（命令是 check）。"""
        p = schedule.script_path(self.root)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("@echo off\r\nrem cbg-runner v4\r\n"
                    '"py" -m src.cli -c "config/store-X.yaml" check --days-ago 1\r\n')
        return p

    def test_过时的脚本会被重建(self):
        p = self._legacy_bat()
        self.app.overview()
        body = p.read_text(encoding="utf-8")
        # ⚠ v6（2026-09-20）：计划任务那份**不再跑 daily**，它只确保服务在跑；
        #   真正跑日常流程的是手动那份（`run-now.bat`）。
        self.assertIn("ensure-service", body)
        self.assertIn(schedule.RUNNER_MARK, body)
        now = schedule.manual_script_path(self.root).read_text(encoding="utf-8")
        # ⚠ v7（2026-09-21 晚）：手动那份**点名**跑（`daily --steps …`）——
        #   `daily` 不给 `--steps` 会直接报错，所以自愈必须把它写进去。
        self.assertIn("daily --steps " + ",".join(run_daily.MANUAL_STEPS), now,
                      "手动那份要点名跑「每天那趟」")

    def test_重建过就在返回里说一声(self):
        """界面可以据此提一句 —— 免得门店发现命令悄悄变了会懵。"""
        self._legacy_bat()
        self.assertTrue(self.app.overview()["runner_rebuilt"])

    def test_没改动就不说重建了(self):
        schedule.write_runner_script(self.root, "config/store-X.yaml")
        self.assertFalse(self.app.overview()["runner_rebuilt"])

    def test_每进程只试一次(self):
        """⚠ 概览 30 秒轮询一次。不收敛的话每次都要读一遍 run.bat，
        而且升级那一刻可能**正跑着任务去覆盖 bat**
        （Windows 上 cmd 正在执行的 .bat 未必能覆盖掉）。
        """
        self._legacy_bat()
        with mock.patch.object(schedule, "refresh_runner_scripts",
                               wraps=schedule.refresh_runner_scripts) as m:
            for _ in range(5):
                self.app.overview()
        self.assertEqual(m.call_count, 1, "概览轮询时反复重建了启动脚本")

    def test_自愈炸了也不能拖垮概览(self):
        """概览是最重要的接口 —— 它挂了整个界面就白屏。

        （真失败也不怕：`run_check.py` 里那个垫片还兜着。）
        """
        with mock.patch.object(schedule, "refresh_runner_scripts",
                               side_effect=OSError("磁盘只读")):
            ov = self.app.overview()          # 不许抛
        self.assertIn("version", ov)
        self.assertFalse(ov["runner_rebuilt"])

    def test_失败之后不再重试(self):
        """失败也把标志置上 —— 否则每次轮询都去撞同一堵墙。"""
        with mock.patch.object(schedule, "refresh_runner_scripts",
                               side_effect=OSError("磁盘只读")) as m:
            self.app.overview()
            self.app.overview()
        self.assertEqual(m.call_count, 1)


class Test跑一次那个接口已经删了(unittest.TestCase):
    """⚠ 2026-09-21 晚（用户：「**现在不需要 run daily 吧，按定时器运行就行了**」）——
    `POST /api/run`（`what` 预设那套）**删了**：界面上那张「跑一次」的卡先删的，
    后端这套"手动跑一整趟"的入口跟着一起走。

    ⚠ 留着一条**说人话的 410**，不静默 404 —— 老页面缓存里的 JS 还可能打过来，
      而 404 只会让那个按钮"点了没反应"（这个项目最怕的一种失败）。
    ⚠ `GET /api/run`（看日志/进度）**留着** —— 右下角抽屉和各页「刷新」还在用它。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_打过来是_410_而且说人话(self):
        """老页面缓存里的「跑一次」按钮打过来：**别起进程**，回一句人话。"""
        with mock.patch.object(runner.RunManager, "start_steps") as m:
            code, body = self.srv.request("POST", "/api/run", {"what": "all"})
        self.assertEqual(code, 410)
        self.assertIn("已经去掉", body["error"])
        self.assertIn("刷新", body["error"], "要告诉人现在该怎么补这一趟")
        self.assertEqual(m.call_count, 0, "入口删了就不许再起进程")

    def test_什么_what_都不认了(self):
        for what in ("all", "pos", "dump", "bogus", ""):
            with self.subTest(what=what):
                code, _ = self.srv.request("POST", "/api/run", {"what": what})
                self.assertEqual(code, 410)

    def test_读日志那条路还在(self):
        """⚠ 删的是 POST（起一趟），不是 GET（看进度/日志）——
        右下角那个抽屉和各页「刷新」的反馈全靠它。"""
        code, body = self.srv.request("GET", "/api/run?id=&since=0")
        self.assertEqual(code, 200)
        self.assertIn("running", body)


class Test旧停止入口已退役(unittest.TestCase):
    """没有页面会调用全局 stop；旧客户端不能借此终止任意当前任务。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_旧停止请求返回_410_且不触碰当前任务(self):
        job = mock.Mock()
        with mock.patch.object(web.manager, "current", return_value=job) as current:
            code, body = self.srv.request("POST", "/api/run/stop", {})

        self.assertEqual(code, 410)
        self.assertIn("停止", body["error"])
        current.assert_not_called()
        job.kill.assert_not_called()


class Test自动化设置的接口只回一句取消(unittest.TestCase):
    """⚠ 2026-09-20（用户）：「**自动化跑什么 … 这些去掉吧，也不用设置了**」，
    2026-09-21 晚连"手动整批"也删了。

    ⇒ `/api/schedule/automation` 这条路由**不再改任何东西**（脚本跑什么是注册表派生的），
      只回一句"这个设置取消了"。
    ⚠ 回的是**说人话的 410**（不是 404）：老页面缓存里那个「保存」按钮打过来时，
      用户要看到的是"设置没了"，而不是一个看不懂的 404、更不是"点了没反应"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        p = mock.patch.object(schedule, "kind", lambda: "windows")
        p.start()
        self.addCleanup(p.stop)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_传什么都不改_脚本也不重写(self):
        with mock.patch.object(schedule, "write_runner_script") as w:
            code, body = self.srv.request("POST", "/api/schedule/automation",
                                          {"steps": ["pos"]})
        self.assertEqual(code, 410)
        self.assertFalse(body["ok"])
        self.assertTrue(body.get("cancelled"))
        self.assertIn("取消", body["message"])
        self.assertEqual(w.call_count, 0, "设置没了就别再去动门店那份脚本")


class TestRunPageWiring(unittest.TestCase):
    """前端接线 —— 「运行」页的按钮 + 自动化勾选。

    ⚠ 前端**没有构建步骤、没有 lint**，引用了一个不存在的 id 只会在浏览器
    控制台里报一行，跑测试和跑服务都看不见。所以这里按源码钉。

    ⚠ 2026-09-17：界面上只剩「整个项目」一个按钮（用户定），
    `dump` / `pos` 两个预设后端还认，只是**不许再有按钮**。
    """

    def test_跑一次那张卡整个删了(self):
        """⚠ 2026-09-21（用户：「**右下角的跑一次可以去掉了**」）——
        那一整张卡（按钮 + 停止 + 状态）从 HTML 里删了。手动跑仍可以走命令行。
        ⚠ 这条同时保证**别再加回来**（旧写法是"只剩「整个项目」一个按钮"）。
        """
        for gone in ('data-what="all"', 'id="btn-stop"', 'id="run-status"',
                     '<h2>跑一次</h2>'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, INDEX_HTML)
        # ⚠ app.js 里对这两个 id 的引用**必须判空**（元素没了，裸取会在加载时抛）
        import re as _re
        self.assertFalse(_re.findall(r"\$\('#(?:btn-stop|run-status)'\)\.", APP_JS),
                         "app.js 里还有对已删元素的裸引用")

    def test_老的单按钮已经彻底拿掉(self):
        """⚠ 拆成 data-what 之后 `#btn-run` 就不存在了 —— app.js 里那句
        `$('#btn-run').addEventListener` 会在**加载时**抛 TypeError，
        后面的绑定全部不执行（整个界面变哑巴）。"""
        self.assertNotIn('id="btn-run"', INDEX_HTML)
        self.assertNotIn("$('#btn-run')", APP_JS)

    def test_退出码说法跟着那张卡一起走了(self):
        """⚠ 2026-09-21（用户：「**右下角的跑一次可以去掉了**」）——
        `EXIT_LABELS`（"退出码 → 人话"那张表）只有那张卡在用：
        卡删了、`pollRun` 也删了，它就成了死代码。

        ⚠ 这里钉的是**"删干净"**：留着那张表本身无害，但留着它的兄弟
        （`startRun` / `pollRun` / `setRunButtons`）就有害 ——
        里面每一句对 `btn-stop` / `run-status` 取元素的写法都会在加载时拿到 null，
        而 `TestFrontendWiring::test_every_referenced_id_exists_in_html` 是按源码扫的。
        ⚠ 手动跑整趟改走命令行 `python -m src.cli daily`；后端 `POST /api/run`
        **还在**（`Test手动跑的反馈在抽屉里` 那几条钉着 `/api/run` 的读取侧）。
        """
        for gone in ("function startRun", "function pollRun", "function setRunButtons",
                     "const EXIT_LABELS"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, APP_JS, "那张卡删了，这套接线也该走")
        # ⚠ 锚**定义**（`function x` / `const x =`）而不是裸名字：app.js 里那段
        #   说明为什么删的注释会把这些名字写进去，锚裸名字会匹到注释上（当场踩到）。
        # 「运行日志」抽屉的喂法（各页「刷新」用）**留着** —— 别删过头
        self.assertIn("watchJob", APP_JS)
        self.assertIn("appendLog", APP_JS)

    def test_自动化勾选那三个_id_都不在了(self):
        """⚠ 2026-09-20（用户）：「自动化跑什么 … 这些去掉吧，也不用设置了」——
        复选框、保存按钮、提示位**一起撤**。
        ⚠ 撤了 HTML 但 JS 还留着 `$('#automation-box')` 那种写法是最阴的：
          拿到 null 不报错，看着一切正常、其实什么都不干
          （`test_自动化勾选不再有接线` 盯着那个方向）。"""
        for i in ("automation-box", "btn-automation-save", "automation-msg"):
            with self.subTest(id=i):
                self.assertNotIn('id="%s"' % i, INDEX_HTML)

    def test_自动化勾选不再有接线(self):
        """⚠ 2026-09-20：这个设置**整个取消** ⇒ 前端那套接线也要跟着走。

        ⚠ 最阴的失败是"HTML 删了、JS 留着"：`$$('#automation-box [data-auto]')`
          拿到空数组、`$('#btn-automation-save')` 拿到 null —— **不报错、
          看着一切正常**，其实什么都不干。所以这条按**函数名和事件**查，
          不是只查 id。
        """
        # ⚠⚠ 断言前**剥掉 JS 注释** —— 我为了说明"删了什么"在注释里原样写了这些名字，
        #   不剥的话 `assertNotIn` 会被**自己的注释**顶掉（今天第六次了）。
        import re as _re
        code = _re.sub(r"/\*.*?\*/", "", APP_JS, flags=_re.S)
        code = _re.sub(r"(?m)//[^\n]*$", "", code)
        for gone in ("renderAutomation", "pickedAutomation",
                     "btn-automation-save", "automation-box", "automation-msg"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, code)
        # 定时器页那个加载器只剩「旧的系统计划任务」那块
        i = APP_JS.index("async function loadSchedulerBits()")
        blk = APP_JS[i:i + 320]
        self.assertIn("renderSchedule(o.schedule", blk)
        self.assertNotIn("renderAutomation", blk)

    def test_两个都不勾前端不再拦(self):
        """⚠ 用户 2026-09-17 定的：两个都不勾是合法的（「只抓数据」）。
        前端那道"至少勾一项"的拦截必须撤掉，否则点了保存什么都不发生。

        ⚠ 断言前先**剥掉 JS 注释** —— 注释里正好写着"以前这里有一道拦截"，
        不剥的话 `assertNotIn` 会被自己的注释顶掉（这一轮踩了两次）。
        """
        code = _strip_js_comments(APP_JS)
        self.assertNotIn("if (!what.length)", code)
        self.assertNotIn("至少勾一项", code)

    def test_没有指向已删下拉框的残留变量(self):
        """⚠ `daysAgo` 是「昨天/今天」那个下拉框的变量，框 2026-09-17 删了 ——
        而「以管理员身份重试」那条路上还写着 `days_ago: daysAgo` ⇒
        点下去直接 `ReferenceError`，**UAC 一次都不会弹**。
        而那正是"普通权限建不了定时任务"时唯一的退路（门店真实场景）。

        前端没有 lint，这种错只在浏览器控制台里露一行。所以按源码钉，
        并且**剥掉注释**再查（`node --check` 只查语法，查不出未定义变量）。
        """
        code = _strip_js_comments(APP_JS)
        self.assertNotIn("daysAgo", code,
                         "还有指向已删下拉框的残留变量 —— 点下去会 ReferenceError")

    def test_报量排查_tab_改好名了(self):
        """⚠ 这个名字改过两轮：2026-09-17「报量排查」→「报量查询」
        （判据被证伪，那页换成报量查询的记录），2026-09-18 又改名成「报量查询」（用户）。
        页签 id 始终是 `pools`。"""
        self.assertIn(">报量查询<", INDEX_HTML)
        for dead in (">报告<", ">报量排查<"):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, INDEX_HTML)


class Test不再注册系统计划任务(unittest.TestCase):
    """⚠ 用户 2026-09-20：「**把兜底去掉吧，不用系统的计划任务**」。

    这块原来有一整套"注册计划任务"的界面（时间框 + 任务名 + 添加按钮，
    连 placeholder 都要按**后端的** `schedule.TASK_NAME` 现算）。

    现在**整套撤掉**：到点由服务里的定时器跑，服务靠**开机自启**常驻
    （注册表 Run 项 —— 不需要管理员，也不会把服务变成管理员）。
    界面上只剩"旧的还能删"（见 `Test旧的计划任务只剩清理`）。
    """

    def test_注册入口全撤了(self):
        for gone in ("btn-sched-install", "sched-time", "sched-name",
                     "syncSchedPlaceholder", "添加兜底任务"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, APP_JS)
                self.assertNotIn(gone, INDEX_HTML)

    def test_连老名字一起没了(self):
        """写死的 `CBG报量对账-21点00` 是 2026-09-16 就改掉的老名字 ——
        那个 placeholder 撤了之后，一个字都不该再出现。"""
        self.assertNotIn("CBG报量对账-21点00", INDEX_HTML)
        self.assertNotIn("CBG报量对账-21点00", APP_JS)

    def test_后台那套还在_命令行还能用(self):
        """⚠ 撤的是**产品界面上的入口**，不是能力本身。

        留着它有三个用处：删旧任务（普通权限删不掉时提权删）、
        给"服务起不来"的机器手动兜一条、以及老门店升级期的兼容。
        """
        self.assertIn("schedule-install", str(bootstrap.STDLIB_ONLY))
        self.assertIn("schedule-remove", str(bootstrap.STDLIB_ONLY))


class Test跑哪几步没有可设的东西(unittest.TestCase):
    """⚠⚠ 2026-09-20（用户）：「**自动化跑什么 … 这些去掉吧，也不用设置了**」，
    2026-09-21 晚"手动整批"也删了。

    ⇒ 现在"跑哪几步"**没有任何可设的余地**：
      * 到点 = 每一步自己的唤醒时刻（注册表）；
      * 手动双击 = `run_daily.MANUAL_STEPS`（也是注册表派生的）；
      * 各页「刷新」= `web.REFRESH_STEPS`。

    ⚠ 这条线当年守的是"**勾了什么就得跑什么**"。设置和手动入口都没了之后，
      要守的换成了它的反面：**别再冒出第二个"跑什么"的来源** ——
      `overview.automation` 那个字段已经删了（留着只会让下一个人以为还能配）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        p = mock.patch.object(schedule, "kind", lambda: "windows")
        p.start()
        self.addCleanup(p.stop)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_概览里不再有那个字段(self):
        _, ov = self.srv.request("GET", "/api/overview")
        self.assertNotIn("automation", ov,
                         "`overview.automation` 又回来了？那说明「跑什么」又要变成可配的了")

    def test_计划任务那列说的是实话(self):
        """⚠ 系统计划任务 v6 起**不跑对账**了（只确保服务在跑）——
        那一列以前填的是一串步骤名，那是**假信息**（那些步骤根本不是它跑的）。"""
        _, ov = self.srv.request("GET", "/api/overview")
        for t in (ov["schedule"].get("tasks") or []):
            with self.subTest(task=t.get("name")):
                self.assertEqual(t.get("steps"), [])
                self.assertEqual(t.get("what_label"), schedule.WHAT_LABEL_OF_TASK)

    def test_老记录取消不掉任何一步(self):
        """⚠ 老门店的 `.secrets/schedule.json` 里记着 `["pos","pools"]` 那种老勾选 ——
        它现在**读都不读**：手动那份脚本跑哪几步由注册表派生，跟记录无关
        （否则就成了"设不了的设置还在限范围"：`dump`/`attain` 永远跑不到，
         而界面上一个字都不会说）。"""
        p = schedule.record_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"t": {"steps": ["pos", "pools"], "at": "x"}}', encoding="utf-8")
        schedule.refresh_runner_scripts(self.root, "config/store-X.yaml")
        body = schedule.manual_script_path(self.root).read_text(encoding="utf-8")
        for step in run_daily.MANUAL_STEPS:
            with self.subTest(step=step):
                self.assertIn(step, body)


class TestWhatsNewPopup(unittest.TestCase):
    """更新弹窗「只弹一次」—— 以及「看这一版的更新说明」那个翻回来的入口。

    ⚠ 用户 2026-09-18 实测报的：**升级到 2.1.0 之后那个窗不止弹一次**。
    根因在前端：`seen` 只在点正中那个「知道了」时记，另外三条关掉的路
    （点灰底 / 点待办里的「去运行」/ 弹窗开着直接刷新）**都不记**。
    ⚠ 第二条最容易被踩 —— 门店看到的第一条待办旁边就挂着「去运行」。

    修法两步：**弹出来就记** + **补一个能翻回来的入口**（不然没细看就关掉
    就再也见不着了）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_概览里给的就是要弹的那一份(self):
        _, ov = self.srv.request("GET", "/api/overview")
        wn = ov["whatsnew"]
        self.assertIsNotNone(wn, "升级后第一次打开控制台该弹")
        self.assertTrue(wn["todo"], "待办不能是空的")

    def test_记过之后概览里就没有了(self):
        self.srv.request("POST", "/api/whatsnew/seen", {"version": web.version.VERSION})
        _, ov = self.srv.request("GET", "/api/overview")
        self.assertIsNone(ov["whatsnew"], "看过了还弹")

    def test_记的时候把待办一起存下来(self):
        """⚠⚠ **顺序不能反**：先算 digest、**再**记 `seen`。

        反过来的话 `seen` 已经是当前版本，算出来的 `todo` 是**空的** ——
        正是 AGENTS.md 坑 13「看过 ≠ 做完了」（那条升级推送踩过一次，
        这里差点又踩）。
        """
        self.srv.request("POST", "/api/whatsnew/seen", {"version": web.version.VERSION})
        code, r = self.srv.request("GET", "/api/whatsnew")
        self.assertEqual(code, 200)
        self.assertTrue(r["body"]["todo"], "存档里的待办是空的 —— 顺序反了？")

    def test_随时能翻回来重看(self):
        self.srv.request("POST", "/api/whatsnew/seen", {"version": web.version.VERSION})
        _, r = self.srv.request("GET", "/api/whatsnew")
        self.assertEqual(r["body"]["version"], web.version.VERSION)
        self.assertTrue(r["body"]["highlights"])

    def test_前端是弹出来就记_不是点了才记(self):
        """⚠ **这条是本轮修复的核心**：记 `seen` 的动作挂在"显示"上，
        不是挂在「知道了」的点击上。前端没有 lint、跑起来也看不见，
        所以按源码钉。
        """
        i = APP_JS.index("function renderWhatsNew")
        j = APP_JS.index("function closeWhatsNew", i)
        self.assertIn("if (!force) markWhatsNewSeen(wn.version)", APP_JS[i:j],
                      "弹出时没记 seen —— 就是这个 bug")

    def test_重看不重复记(self):
        """从设置翻回来看的那一次**不再记一次**（`seen` 早就是这一版了），
        而且要能绕过 `wnShowing` —— 它就是用来"关掉之后还想再看一眼"的。"""
        i = APP_JS.index("function renderWhatsNew")
        j = APP_JS.index("function closeWhatsNew", i)
        seg = APP_JS[i:j]
        self.assertIn("force", seg)
        self.assertIn("(!force && wnShowing === wn.version)", seg,
                      "force 没能绕过「已经弹过」那道闸")

    def test_入口按钮在设置里(self):
        self.assertIn('id="btn-whatsnew-show"', INDEX_HTML)
        self.assertIn("$('#btn-whatsnew-show')", APP_JS)

    def test_两个弹窗不会同时弹(self):
        """⚠ 「已更新」和「老任务要处理」这两个框的**触发时机完全一样**
        （都是"升级后第一次打开控制台"），而 `.modal-mask` 两个都是
        `position: fixed; inset: 0; z-index: 200` ——

        同时显示 = **两层遮罩叠在一起**（背景发黑），而且 DOM 靠后的
        「已更新」压在上面，用户**根本不知道底下还压着一个**。

        所以只能一个先弹、另一个排队。事件触发的东西浏览器里才看得见，
        按源码钉。
        """
        i = APP_JS.index("function renderLegacyPrompt")
        j = APP_JS.index("function closeLegacyPrompt", i)
        self.assertIn("$('#whatsnew-mask').hidden", APP_JS[i:j],
                      "老任务弹窗没给「已更新」让路 —— 两个会叠在一起")
        k = APP_JS.index("function closeWhatsNew")
        m = APP_JS.index("async function ackWhatsNew", k)
        self.assertIn("lgWaiting", APP_JS[k:m],
                      "关掉「已更新」之后没把排队的老任务提示放出来")


class TestReportBugApi(unittest.TestCase):
    """`/api/report-bug` —— 「定时执行」下面那个「上报 bug」按钮。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        (self.root / ".secrets" / "erp.env").write_text(
            "ERP_PASSWORD=hunter2\n", encoding="utf-8")
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "store-X.yaml").write_text(
            "store_code: SCN1\n", encoding="utf-8")
        # ⚠ `load_config` 会读 config/stores.yaml（随包发的门店映射表）——
        #   夹具里不建的话，真实路径上会 FileNotFoundError
        (self.root / "config" / "stores.yaml").write_text(
            "stores: []\ndoc_types: {}\n", encoding="utf-8")
        (self.root / "out" / "run.log").write_text("=== 跑了一次 ===\n", encoding="utf-8")
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_点了就打包并返回路径(self):
        code, body = self.srv.request("POST", "/api/report-bug", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(Path(body["path"]).is_file())
        self.assertIn("执行日志.txt", body["entries"])

    def test_返回里带邮件和企业微信各自的结果(self):
        """⚠ 两条**各自独立** —— 一条挂了另一条照发，界面要能分开显示。"""
        _, body = self.srv.request("POST", "/api/report-bug", {})
        self.assertIn("mail", body)
        self.assertIn("wecom", body)

    def test_推送失败时_ok_仍然是真的并且给得出路径(self):
        """⚠ 用户自己问的"日志会不会推不出去" —— **会**，而且那正是最常见的 bug。
        推送失败不该把上报判成失败，包必须留着、路径必须说出来。"""
        with mock.patch("src.cli.report_bug") as m:
            m.return_value = {"ok": True, "path": "/tmp/x.zip", "size_kb": 1.0,
                              "entries": [], "mail": "❌ 挂了", "wecom": "❌ 挂了",
                              "sent": [], "message": "包在这：/tmp/x.zip"}
            _, body = self.srv.request("POST", "/api/report-bug", {})
        self.assertTrue(body["ok"])
        self.assertIn("/tmp/x.zip", body["message"])

    def test_包里没有凭据(self):
        """⚠ 端到端也验一遍 —— 光单测不够，这里走的是真路由。"""
        import zipfile
        _, body = self.srv.request("POST", "/api/report-bug", {})
        with zipfile.ZipFile(body["path"]) as z:
            names = z.namelist()
            text = "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
        self.assertFalse([n for n in names if ".env" in n])
        self.assertNotIn("hunter2", text)
        self.assertIn("=== 跑了一次 ===", text)

    def test_区长错误上报不附带辖区外业务日志和报告摘要(self):
        """区长能上报环境问题，但这台机器上的全区业务明细不能随支持包外发。"""
        import zipfile
        outside_serial = "OUTSIDE-SERIAL-ONLY-SYNTHETIC"
        (self.root / "out" / "run.log").write_text(
            "区外店库存串号：%s\n" % outside_serial, encoding="utf-8")
        (self.root / "out" / "run.log.1").write_text(
            "区外店上一份日志串号：OLD-OUTSIDE-SYNTHETIC\n", encoding="utf-8")
        (self.root / "out" / "attain-2026.json").write_text(
            json.dumps({"store": "区外店", "serial": outside_serial}),
            encoding="utf-8")
        manager_scope = web._with_pages({
            "role": web.ROLE_MANAGER, "stores": {"授权区门店"},
            "entry_kind": "erp", "runtime_lifehall": False,
            "who": "测试区长", "label": "区长（授权区）",
            "needs_linglong": False,
        }, self.root, [])
        with mock.patch.object(web, "role_scope", return_value=manager_scope):
            code, body = self.srv.request("POST", "/api/report-bug", {})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        with zipfile.ZipFile(body["path"]) as z:
            names = z.namelist()
            contents = "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
        self.assertIn("环境.txt", names)
        self.assertNotIn("报告摘要.txt", names)
        self.assertNotIn("执行日志.上一份.txt", names)
        self.assertNotIn(outside_serial, contents)
        self.assertNotIn("OLD-OUTSIDE-SYNTHETIC", contents)
        self.assertIn("区长身份的支持包会省略业务运行日志", contents)


class TestReportBugButtonWiring(unittest.TestCase):
    def test_上报_bug_在右下角抽屉里(self):
        """⚠ 2026-09-17 定的：这两个按钮原来挂在「定时执行」下面 ——
        「出问题了？」跟"每天几点跑"完全没关系。

        ⚠⚠ **2026-09-21 又挪了一次**（用户：「**出问题上报 bug 做到右下角的状态
        悬浮窗里**」）—— 出问题时人第一反应是点右下角那个悬浮窗看状态，
        而不是翻到设置页里找按钮。⇒ 这条测试也跟着改成盯抽屉。

        老规矩保留：`btn-report-bug` **不许**再出现在「定时器设置」页里。

        ⚠ 锚 `<h2>定时执行`（**不带 `</h2>`**）—— 光找"定时执行"会匹到顶部那个
        小药丸的 title（第一版就是这么错的，取到的区间是空的）；
        而 2026-09-20 起这个标题后面跟了个 `<span class="hint" id="timer-meta">`，
        带上 `</h2>` 就再也找不到了。
        """
        i = INDEX_HTML.index("<h2>定时器设置")
        j = INDEX_HTML.index("</section>", i)
        for gone in ("btn-report-bug", "btn-clear-pools-notify"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, INDEX_HTML[i:j],
                                 "「%s」不该再挂在「定时执行」下面了" % gone)

        # 「上报 bug」在**右下角那个状态抽屉**里（`#run-drawer`）
        d0 = INDEX_HTML.index('id="run-drawer"')
        d1 = INDEX_HTML.index("</aside>", d0)
        self.assertIn("btn-report-bug", INDEX_HTML[d0:d1],
                      "「上报 bug」该在右下角状态悬浮窗里")
        # ⚠ 2026-09-21 又挪了一次（用户：「**清除推送记忆这个放在报量查询那个
        #   设置里面吧**，合作店用不上」）—— 它是**双平台数据对比**那条推送自己的事。
        k = INDEX_HTML.index('id="subpanel-compliance-settings"')
        m = INDEX_HTML.index("</section>", k)
        self.assertIn("btn-clear-pools-notify", INDEX_HTML[k:m],
                      "「清除推送记忆」该在「报量查询 › 设置」里")
        # 而且**只有一份**（两份 id 会撞，而且用户明确说"放那儿")
        self.assertEqual(INDEX_HTML.count('id="btn-clear-pools-notify"'), 1)
        # 合作店看不到这一页
        # ⚠ 2026-09-21（M17 甲方案）：标签上不再写 `data-types="experience platform"`
        #   —— 改由后端 `web.PAGE_RULES` 说了算。这里断言**表里那一行管着它**：
        #   `compliance-settings` 必须挂在"走玲珑才看得见"那一档。
        from src import web as _web
        self.assertTrue(_web.PAGE_RULES.get("compliance-settings"),
                        "这一页得只给走玲珑的店看（合作店用不上）")
        self.assertIn("compliance-settings",
                      _web.pages_for({"role": _web.ROLE_STORE, "needs_linglong": True}))
        self.assertNotIn("compliance-settings",
                         _web.pages_for({"role": _web.ROLE_STORE,
                                         "needs_linglong": False}))

    def test_结果区几个_id_都在(self):
        for i in ("btn-report-bug", "report-bug-msg", "report-bug-result"):
            with self.subTest(id=i):
                self.assertIn('id="%s"' % i, INDEX_HTML)

    def test_前端会渲染出包的路径(self):
        """⚠ 界面**必须**把路径显示出来 —— 自动发送失败是常态，
        那时候用户得能自己把文件发出去。"""
        self.assertIn("r.path", APP_JS)
        self.assertIn("$('#report-bug-result')", APP_JS)

    def test_界面写明了包里没有凭据但有业务数据(self):
        seg = INDEX_HTML[INDEX_HTML.index("btn-report-bug"):]
        self.assertIn("凭据", seg[:2500])
        self.assertIn("业务数据", seg[:2500])


class Test更新没走完的横幅(unittest.TestCase):
    """阶段 1.4d：中断的升级必须**在哪个页面都看得见**。

    后端把 `overview.update_pending` 给出来了还不够 —— 前端不渲染就等于没有，
    而"静默跑在混合版本上"正是这一整套要消灭的东西。
    """

    def test_横幅和两个按钮都在页面上(self):
        self.assertIn('id="update-broken"', INDEX_HTML)
        self.assertIn('id="btn-update-repair"', INDEX_HTML)
        self.assertIn('id="btn-update-restore"', INDEX_HTML)

    def test_默认是藏着的(self):
        """没断在半路时不能一直挂条红横幅。"""
        self.assertRegex(INDEX_HTML, r'id="update-broken"[^>]*\shidden')

    def test_前端真的会渲染它(self):
        self.assertIn("renderUpdateBroken", APP_JS)
        # ⚠ 必须挂在**总览**那趟轮询上（30 秒一次、跟当前在哪一页无关），
        #   只挂在设置页的话，门店不进设置就永远看不到。
        self.assertIn("renderUpdateBroken(o.update_pending)", APP_JS)

    def test_两个按钮真的会去打后端(self):
        self.assertIn("repair: how", APP_JS)
        self.assertIn("api/update", APP_JS)
        self.assertIn("'restore'", APP_JS)


class Test数据没到位的横幅(unittest.TestCase):
    """M14 / 阶段 3.3：**五态要在界面上分得开**。

    ⚠ 这条的价值在于：以前"ERP 挂了抓取一直失败"和"今天确实没卖"在页面上
    是同一个画面（"还是昨天那份"），门店会把前者的账算到"没生意"头上。
    """

    def test_横幅和文案位都在(self):
        self.assertIn('id="data-broken"', INDEX_HTML)
        self.assertIn('id="ds-text"', INDEX_HTML)

    def test_默认是藏着的(self):
        """没事别老挂一条黄条 —— 只在 `ok=false` 时才出现。"""
        self.assertRegex(INDEX_HTML, r'id="data-broken"[^>]*\shidden')

    def test_前端真的会渲染它(self):
        self.assertIn("renderDataState", APP_JS)
        # ⚠ 挂在**总览**那趟轮询上（跟"更新没走完"那条横幅一个位置）
        self.assertIn("renderDataState(o.data_state)", APP_JS)

    def test_全_ok_时不显示(self):
        """逻辑层：`ok=true` ⇒ 藏起来（用 node 太贵，这里查源码里的那个分支）。"""
        seg = APP_JS[APP_JS.index("function renderDataState"):]
        seg = seg[:seg.index("\n}")]
        self.assertIn("ds.ok", seg)
        self.assertIn("hidden = true", seg)

    def test_确实为零和没有数据在文案上不是一回事(self):
        """⚠ M2M5 §9.4 的红线：真 0 不许写成"没有数据"。"""
        from src.app import data_state as ds
        self.assertNotEqual(ds.LABELS[ds.ZERO], ds.LABELS[ds.MISSING])
        self.assertNotIn("没有数据", ds.LABELS[ds.ZERO])


class Test表头也认_html(unittest.TestCase):
    """2026-09-19：达成表要在表头里换行（产品名 + 占比两行），
    而 `table()` 的表头原来只 `esc(h)` ⇒ 传 `{html:…}` 进去渲染成
    `[object Object]`，**整个表头都看不见了**（用户当场发现）。

    ⚠ 这条同时钉住"**字符串照样转义**"—— 开这个口子不等于放宽安全性。
    """

    def setUp(self):
        self.js = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js", "features/plan/monthly/page.js", "app.js"))

    def test_表头认_html(self):
        i = self.js.index("function table(header")
        blk = self.js[i:i + 700]
        self.assertIn("'html' in h", blk, "表头不认 {html:…} ⇒ 换行的表头渲染不出来")
        self.assertIn("esc(h)", blk, "普通字符串还是得转义")

    def test_表体的规矩没变(self):
        """⚠ 2026-09-19：`cell()` 多了一个"单元格自带 class"的能力（达成率标色要用，
        颜色得打在 `td` 上）—— 但**转义那条规矩没变**：字符串照样 `esc`。"""
        i = self.js.index("function cell(")
        blk = self.js[i:i + 500]
        self.assertIn("raw ? c.html : esc(c)", blk, "字符串还是要转义")
        self.assertIn("const k = (raw && c.cls) || cls;", blk, "自带 class 只认 {html:…} 那种")


class Test静态文件取得到(unittest.TestCase):
    """⚠⚠ 2026-09-19 用真金白银换的：我给静态文件加缓存头时多传了一个 kwarg
    （`_send()` 没有 `extra` 参数）⇒ **每个 .js / .css 请求都 500** ⇒
    浏览器拿不到前端，用户看到的是"改完了、刷新了、**还是老样子**"。

    ⚠ 当时 `py_compile` 过得去、**全量测试也全绿** —— 因为没有任何一条测试
      请求过静态文件。所以这一条补的就是那个洞：**交付链路本身要有测试**。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)

    def tearDown(self):
        self.srv.close()

    def test_四个前端文件都取得到(self):
        for name, least in (("index.html", 1000), ("app.js", 10000),
                            ("style.css", 5000), ("theme.css", 1000)):
            code, body = self.srv.request("GET", "/" + name)
            with self.subTest(name=name):
                self.assertEqual(code, 200, "%s 取不到（前端会停在旧的那份上）" % name)
                self.assertGreaterEqual(len(body), least)

    def test_不带缓存头的话前端会停在旧版本(self):
        """⚠ 前端**没有构建步骤**，改完就是刷新一下 —— 所以静态文件不能缓存。"""
        c = HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("GET", "/app.js")
        r = c.getresponse()
        hdr = r.getheader("Cache-Control") or ""
        r.read()
        c.close()
        self.assertIn("no-store", hdr)


class Test历史记录接口(unittest.TestCase):
    """`/api/attain/history` —— 「历史记录」页读的就是它。

    ⚠ 这一页**只读**：存档是"锁住"的那份，接口里**没有**写入口（唯一的写
      发生在 `attain.run()` 里换周那一下）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        # ⚠ **别依赖开发机那个配置**：达成页 2026-09-21 起按**画像**过滤，
        #   而开发机的 `config/store-*.yaml` 可能写着某家真店 ⇒ 过滤掉所有行。
        #   这里钉成"平台岗"（看全区），测的才是这一页本身。
        #   ⚠ **只打 `store_profile`**，别打 `load_raw` —— 后者被门禁读（登录/授权），
        #     打成空配置会让请求先被门禁挡掉（400），测的就不是这一页了。
        from src import config_io
        #   ⚠ 画像要**给全**：门禁查 `needs_linglong`，缺键会被兜成
        #     `参数不对：'needs_linglong'`（400）—— 那是 mock 不完整，不是产品问题。
        pr = mock.patch.object(
            config_io, "store_profile",
            lambda *a, **k: {"erp_name": "平台岗", "huawei_code": "", "marker": "",
                             "kind": "平台岗", "huawei_name": "", "in_roster": True,
                             "platform": True, "show_all": True,
                             "needs_linglong": False, "type": "platform"})
        pr.start()
        self.addCleanup(pr.stop)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def _archive(self, period, stores=1, total=0.5):
        from src.features.sales.attain import attain as A
        d = A.archive_dir(self.root)
        d.mkdir(parents=True, exist_ok=True)
        A.archive_path(self.root, period).write_text(json.dumps({
            "exists": True, "period": period, "start": "2026-09-14", "end": "2026-09-20",
            "locked_at": "2026-09-21 08:00:00", "columns": ["A"], "weights": [1.0],
            "rows": [{"store": "甲%d" % i, "total": total, "targets": [2], "actuals": [1],
                      "rates": [0.5], "people": []} for i in range(stores)],
        }, ensure_ascii=False), encoding="utf-8")

    def test_没存档时给空列表_不是报错(self):
        code, body = self.srv.request("GET", "/api/attain/history")
        self.assertEqual(code, 200)
        self.assertEqual(body["items"], [])

    def test_列表带上周数和平均达成(self):
        self._archive("2026-W38", stores=2, total=0.6)
        self._archive("2026-W39", stores=4, total=0.4)
        _, body = self.srv.request("GET", "/api/attain/history")
        self.assertEqual([x["period"] for x in body["items"]], ["2026-W39", "2026-W38"])
        self.assertEqual(body["items"][0]["stores"], 4)
        self.assertAlmostEqual(body["items"][0]["avg"], 0.4)
        self.assertTrue(body["items"][0]["locked_at"])

    def test_按周取那一份_并标明是锁住的(self):
        self._archive("2026-W38", stores=3)
        _, body = self.srv.request("GET", "/api/attain/history?period=2026-W38")
        self.assertTrue(body["exists"])
        self.assertTrue(body["locked"], "历史是锁住的 —— 前端要能这么说")
        self.assertEqual(len(body["rows"]), 3)

    def test_取不存在的周给_exists_false(self):
        code, body = self.srv.request("GET", "/api/attain/history?period=2026-W01")
        self.assertEqual(code, 200)
        self.assertFalse(body["exists"])

    def test_历史这页只读_没有写入口(self):
        """⚠ 存档就是"锁住"的那一份 —— 接口里**不能**有写入口。

        唯一的写发生在 `attain.run()` 里换周那一下（`archive_rolled`）。
        这里给个 PUT/POST 的话，"锁住"就不成立了，而复盘引用的正是它。
        """
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                code, _ = self.srv.request(method, "/api/attain/history",
                                           {"period": "2026-W38"})
                self.assertNotEqual(code, 200, "%s 竟然被受理了" % method)


class _FrozenTimerClock:
    """`src.modules.timer` 眼里的 `datetime` —— `now()` 钉在 `FROZEN`（上午 10:00）。

    ⚠ 为什么要有它：`/api/timer` 的 `next_run` **走真实时钟**（那层没有 now 注入口，
      `timer.next_run()` 自己 `datetime.datetime.now()`），而「下一次」的断言写的是
      "21:00 那一批（dump…）"。每天 **21:00–21:30** 这半小时，最近的一趟是
      「数据交换」（21:15 上报 / 21:30 收取）⇒ 断言**必红** —— 2026-09-29 21:07
      跑三头当场抓到（main 同时段一样红：是断言跟钟点耦合，不是功能坏了）。
    ⚠ 只换 `src.modules.timer` 包里 `import datetime` 那**一个绑定**：
      `when.py` / `once.py` 各有自己的 import（when 在这条路径上不读 now、
      once 根本不在 GET /api/timer 上）—— 不动全局 `datetime` 模块，
      `runlog` 和测试自己照常用真实时间。
    """

    FROZEN = datetime.datetime(2026, 9, 29, 10, 0, 0)

    class _DT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            at = _FrozenTimerClock.FROZEN
            return at if tz is None else at.replace(tzinfo=tz)

    def __getattr__(self, name):
        if name == "datetime":
            return _FrozenTimerClock._DT
        return getattr(datetime, name)


def _freeze_timer_clock():
    """把 `src.modules.timer.datetime` 换成 `_FrozenTimerClock`（`addCleanup` 停）。"""
    return mock.patch("src.modules.timer.datetime", _FrozenTimerClock())


class Test定时器的那一屏(unittest.TestCase):
    """用户 2026-09-20：「加一个**定时器执行日志**，记录什么时间唤醒了什么，成功了没。
    然后定时器设置页面**最上面大字**写着**下一次执行的是啥，什么时间**。
    **右下角的控制板也加上这个**」。

    三个落点，各测一条：

    | 落点 | 数据 |
    |---|---|
    | 定时器页顶上那行大字 | `/api/timer` 的 `next_run` |
    | 执行日志那张表 | `/api/timer` 的 `history` |
    | 右下角「本机状态」 | `/api/status` 的 `上次自动跑`（⚠ **"下次"那一行 2026-09-21 删了**，见下）|

    ⚠ 2026-09-21 用户：「**这个悬浮窗就别显示下次时间了**」—— 悬浮窗是"读一眼状态"
      的地方，"下一次什么时候跑"只留在「定时器设置」页（那行大字，还能点进去改）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        db = self.root / "out" / "cbg-2026.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        # ⚠ 「下一次」的断言跟钟点耦合（21:00–21:30 那半小时必红，见 _FrozenTimerClock）
        p = _freeze_timer_clock()
        p.start()
        self.addCleanup(p.stop)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_接口给下一次和日志(self):
        code, body = self.srv.request("GET", "/api/timer")
        self.assertEqual(code, 200)
        nxt = body["next_run"]
        self.assertTrue(nxt["at"], "得说清下一次什么时候")
        self.assertTrue(nxt["label"], "还得说清跑什么")
        # ⚠ 2026-09-21 起「下一次」= **每天干活那趟**（自动更新不进前端了，见
        #   `Test自动更新不进前端`）—— 所以白天问也是五步那一批，不是 :17。
        self.assertNotIn("autoupdate", nxt["cmds"], "「下一次」是每天那趟，不含内部步骤")
        self.assertIn("dump", nxt["cmds"])
        self.assertEqual(body["history"], [], "还没跑过 ⇒ 空列表，不是报错")

    def test_顶上那行按实际排的时间算_不按default筛(self):
        """⚠⚠ 2026-09-21 用户：「我把**数据交换改到 21:00**，**这个位置不加上啊**，
        要注意以后这个地方」。

        `上报数据` / `收取门店上报` 是 `Step.default=False`（各有自己的时刻），
        原来那行字按 `default=True` 筛 ⇒ 用户把它们调到 21:00 之后，
        **那一刻真会跑**，可这行字照样不列它们。
        ⇒ 判据改成"是不是**内部步骤**"（只排自动更新那种每小时跑的）。
        """
        from src.modules import timer
        timer.set_whens(self.root, "report",
                        [{"kind": "daily", "time": "21:00"}])
        _, body = self.srv.request("GET", "/api/timer")
        nxt = body["next_run"]
        self.assertIn("上报数据", nxt["labels"], "调到同一刻了，这行字还是不带它")
        # ⚠ 时钟被钉在上午 10:00（_FrozenTimerClock）⇒ 下一个 21:00 恒是"今天"。
        #   原来这段按真实 now 推"今天/明天"：21:00 一过就变"明天"，可 next_run
        #   那时挑中的根本不是 21:00 那批（是 21:15/21:30 的数据交换）⇒ 推了也红
        #   （2026-09-29 21:07 跑三头实测）。冻结时钟后两种红一起没了。
        self.assertEqual(nxt["at_text"], "今天 21:00")
        # 再调回自己的 21:15 ⇒ 它不该再挤进 21:00 那一趟
        timer.set_whens(self.root, "report",
                        [{"kind": "daily", "time": "21:15"}])
        _, body = self.srv.request("GET", "/api/timer")
        self.assertNotIn("上报数据", body["next_run"]["labels"])
        self.assertNotIn("自动更新", body["next_run"]["labels"],
                         "内部步骤（每小时那趟）永远不许进这行字")

    def test_跑过之后日志里有那一条(self):
        runlog.record("wake", True, note="内置定时器：销售达成",
                      detail={"slot": "2026-09-20 21:00", "steps": ["attain"],
                              "exit_code": 0, "seconds": 3.5}, root=self.root)
        _, body = self.srv.request("GET", "/api/timer")
        one = body["history"][0]
        self.assertEqual(one["slot"], "2026-09-20 21:00")
        self.assertEqual(one["label"], "销售达成")
        self.assertTrue(one["ok"])
        self.assertEqual(one["seconds"], 3.5)

    def test_控制板只留上次_不显示下次时间(self):
        """⚠ 2026-09-21（用户：「**这个悬浮窗就别显示下次时间了**」）。

        "下一次什么时候跑"挪回「定时器设置」页（那行大字还能点进去改）；
        悬浮窗只留**发生过的事**（不会因为看的时间而变）。
        """
        runlog.record("wake", True, detail={"slot": "2026-09-20 21:00",
                                            "steps": ["attain"]}, root=self.root)
        _, body = self.srv.request("GET", "/api/status")
        rows = {r["label"]: r for r in body["rows"]}
        self.assertNotIn("下次自动跑", rows, "悬浮窗里又显示下次时间了")
        self.assertIn("上次自动跑", rows)
        self.assertIn("成功", rows["上次自动跑"]["value"])

    def test_没跑过也有一行_不是崩(self):
        _, body = self.srv.request("GET", "/api/status")
        rows = {r["label"]: r for r in body["rows"]}
        self.assertIn("上次自动跑", rows)
        self.assertIn("还没跑过", rows["上次自动跑"]["value"])

    def test_开关滑块走_PUT_enabled(self):
        """⭐ 用户 2026-09-20：「开了就注册到定时器，不开就不注册」——
        界面上那个滑块拨一下 = `PUT /api/timer {cmd, enabled}`。"""
        code, body = self.srv.request("PUT", "/api/timer",
                                      {"cmd": "pools", "enabled": False})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["enabled"])
        self.assertIn("不再注册", body["message"])
        # 关掉之后：任务表里它是关的、下一次里没有它
        _, t = self.srv.request("GET", "/api/timer")
        row = [x for x in t["tasks"] if x["cmd"] == "pools"][0]
        self.assertFalse(row["enabled"])
        self.assertNotIn("pools", t["next_run"]["cmds"])

    def test_开关拨回去就恢复(self):
        self.srv.request("PUT", "/api/timer", {"cmd": "pools", "enabled": False})
        _, body = self.srv.request("PUT", "/api/timer",
                                   {"cmd": "pools", "enabled": True})
        self.assertTrue(body["enabled"])
        self.assertIn("已注册到定时器", body["message"])
        _, t = self.srv.request("GET", "/api/timer")
        # ⚠ 别断言"下一次里有它"：白天最近的一趟通常是**每小时的自动更新**，
        #   要看的是"它注册回来了"（开关是开的）。
        row = [x for x in t["tasks"] if x["cmd"] == "pools"][0]
        self.assertTrue(row["enabled"])

    def test_只改时间点不带开关_别把开关掀了(self):
        """⚠ `PUT` 一个入口两件事：只传 `whens` 时**不能**顺手把 `enabled` 也改了
        （`whens` 缺省 = "恢复默认时间"，跟"关掉"是两回事）。"""
        self.srv.request("PUT", "/api/timer", {"cmd": "pools", "enabled": False})
        # ⚠ 后面的步骤不能早于数据抓取 ⇒ 先把抓取挪到 08:00（两个抓取一起同步）
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "dump",
                          "whens": [{"kind": "daily", "time": "08:00"}]})
        code, _ = self.srv.request("PUT", "/api/timer",
                                   {"cmd": "pools",
                                    "whens": [{"kind": "daily", "time": "08:15"}]})
        self.assertEqual(code, 200)
        _, t = self.srv.request("GET", "/api/timer")
        row = [x for x in t["tasks"] if x["cmd"] == "pools"][0]
        self.assertFalse(row["enabled"], "改时间把开关掀开了？")
        self.assertEqual(row["when_text"], "每天 08:15")

    def test_改时间的提示语只说这一步的下一趟(self):
        """⚠⚠ 2026-09-21 用户（看着定时器页）：

        「『**dump**』改成：每天 21:00（下一趟 **2026-09-21 15:17**）。
         这个下一趟是自动更新的，**不要显示自动更新的**」

        15:17 是**自动更新**那趟（每小时 :17 跑一次）—— 原来提示语里那个"下一趟"
        是**全局**最近的一趟（`timer.next_at` / `next_run` 把所有步骤放一起取最小），
        白天任何时候都是自动更新。⇒ 现在一律算**这一步自己的**下一次。
        """
        code, body = self.srv.request("PUT", "/api/timer",
                                      {"cmd": "dump",
                                       "whens": [{"kind": "daily", "time": "21:00"}]})
        self.assertEqual(code, 200)
        # ⚠ 提示语里**不带下次时间**了（那句就贴在行里，旁边那列写着下次，
        #   重复一遍只会长到换行）——"下一次"当**数据**回：`next_at`。
        self.assertNotIn(":17", body["message"], "又把自动更新那趟写进去了")
        self.assertNotIn(":17", body["next_at"])
        self.assertTrue(body["next_at"].endswith("21:00"), body["next_at"])
        self.assertIn("21:00", body["message"])

    def test_一个字没动就说没改动(self):
        """⚠ 点「保存」但值没变时，说"改成…"本身就怪（用户：「单击保存会弹出来
        一些奇怪的东西」）——该说"没改动"。"""
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "dump", "whens": [{"kind": "daily", "time": "08:15"}]})
        _, body = self.srv.request("PUT", "/api/timer",
                                   {"cmd": "dump",
                                    "whens": [{"kind": "daily", "time": "08:15"}]})
        self.assertFalse(body["changed"])
        self.assertIn("没改动", body["message"])
        self.assertNotIn("已保存", body["message"])

    def test_点默认要说是恢复默认(self):
        """⚠ 用户 2026-09-21：「默认不是恢复默认 21:00 时间嘛，怎么提示是
        『attain』改成：**只手动跑**」——「默认」按钮发的是 `whens: []`，
        那是"跟随模块声明的默认值"，不是"改成只手动跑"。"""
        # 先把抓取挪早，否则 attain 08:30 会撞上「不能早于数据抓取」
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "dump",
                          "whens": [{"kind": "daily", "time": "08:00"}]})
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "attain",
                          "whens": [{"kind": "weekly", "weekdays": [1],
                                     "time": "08:30"}]})
        code, body = self.srv.request("PUT", "/api/timer",
                                      {"cmd": "attain", "whens": []})
        self.assertEqual(code, 200)
        self.assertIn("恢复默认", body["message"])
        self.assertIn("每天 21:00", body["message"])
        self.assertNotIn("只手动跑", body["message"])
        self.assertTrue(body["changed"], "从 08:30 改回默认 21:00，这是**真改了**")
        _, t = self.srv.request("GET", "/api/timer")
        row = [x for x in t["tasks"] if x["cmd"] == "attain"][0]
        self.assertEqual(row["when_text"], "每天 21:00")
        self.assertFalse(row["overridden"])

    def test_关着的步骤要说它没在跑(self):
        """⚠ 开关关着 ⇒ 它现在**根本不会跑**。这时候只说"已保存"会让人以为到点会跑。"""
        self.srv.request("PUT", "/api/timer", {"cmd": "pools", "enabled": False})
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "dump",
                          "whens": [{"kind": "daily", "time": "08:00"}]})
        code, body = self.srv.request("PUT", "/api/timer",
                                      {"cmd": "pools",
                                       "whens": [{"kind": "daily", "time": "08:15"}]})
        self.assertEqual(code, 200, body.get("error") or body.get("message"))
        self.assertIn("没在跑", body["message"])
        self.assertEqual(body["next_at"], "")

    def test_调顺序走_PUT_order(self):
        """⭐ 2026-09-21 用户：「相同时间执行的任务，**按照定时器这个列表从上到下执行**，
        然后定时器列表给个调顺序的功能」。

        界面把**整张表的顺序**发过来（不是"把某一步上移一格"）——
        后端不用猜"现在什么顺序"，也不会因为两次点击之间别人改过而错位。
        ⭐ 2026-09-23：两个抓取**钉回最前**（界面怎么排都一样）。
        """
        order = ["attain", "pos", "dump", "erp-dump", "film", "benefit", "pools",
                 "report", "report-inbox", "plan"]
        code, body = self.srv.request("PUT", "/api/timer", {"order": order})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("执行顺序已更新", body["message"])
        want = ["dump", "erp-dump"] + [c for c in order
                                       if c not in ("dump", "erp-dump")]
        self.assertEqual([t["cmd"] for t in body["tasks"]], want)
        # 重新读一遍也是这个顺序（真存下来了，不是只在这一次的返回里）
        _, t = self.srv.request("GET", "/api/timer")
        self.assertEqual([x["cmd"] for x in t["tasks"]], want)

    def test_调顺序时时间和开关一个字都不动(self):
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "dump",
                          "whens": [{"kind": "daily", "time": "08:00"}]})
        self.srv.request("PUT", "/api/timer",
                         {"cmd": "pos", "whens": [{"kind": "daily", "time": "08:15"}]})
        self.srv.request("PUT", "/api/timer", {"cmd": "pools", "enabled": False})
        self.srv.request("PUT", "/api/timer",
                         {"order": ["attain", "dump", "erp-dump", "pos", "pools",
                                    "report", "report-inbox"]})
        _, t = self.srv.request("GET", "/api/timer")
        rows = {x["cmd"]: x for x in t["tasks"]}
        self.assertEqual(rows["pos"]["when_text"], "每天 08:15")
        self.assertFalse(rows["pools"]["enabled"])
        self.assertEqual(rows["dump"]["when_text"], "每天 08:00")
        self.assertEqual(rows["erp-dump"]["when_text"], "每天 08:00")

    def test_顺序名单里认不出来的步骤要_400(self):
        """⚠ 落回的后果是"界面调了顺序、实际按老顺序跑"（界面说一套、跑另一套）。"""
        code, body = self.srv.request("PUT", "/api/timer",
                                      {"order": ["dump", "没有这一步"]})
        self.assertEqual(code, 400)
        self.assertIn("没有这一步", body["error"])

    def test_两样都不给就_400(self):
        code, body = self.srv.request("PUT", "/api/timer", {"cmd": "pools"})
        self.assertEqual(code, 400)
        self.assertIn("enabled", body["error"])


class Test刷新按钮会先抓新数据(unittest.TestCase):
    """⭐ 用户 2026-09-20：「周度重点的刷新按钮，还有 pos 合规和报量查询的刷新按钮
    **需要单独调用一次抓取新数据**」。

    ⚠ 原来那三个「刷新」只是**重读**已经算好的落盘 —— 想看新数据得自己跑去别处
      （或者等定时任务）先抓一遍，界面上完全看不出来这件事。
    ⇒ 现在它们 = **抓新数据 + 重新读这一页**（起后台任务，跟手动「跑一次」同一条路）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_三页各要哪几步(self):
        """⚠ 一个都不能少、也一个都不能多 —— 各页读的是不同的数据源：
        达成读云商销售明细 / POS 读玲珑单据 / 双平台两边都要。"""
        self.assertEqual(web.REFRESH_STEPS["attain"], ("erp-dump", "attain"))
        self.assertEqual(web.REFRESH_STEPS["pos"], ("dump", "pos"))
        self.assertEqual(web.REFRESH_STEPS["pools"], ("dump", "erp-dump", "pools"))
        for page, steps in web.REFRESH_STEPS.items():
            with self.subTest(page=page):
                for s in steps:
                    self.assertIn(s, run_daily.STEPS, "步骤名写错了：%s" % s)

    def test_起的是后台任务_不是同步等(self):
        """⚠ 抓数要一两分钟 —— 挂在 HTTP 请求里只会让页面转圈、还看不出卡在哪。"""
        with mock.patch.object(web.manager, "start_steps") as m:
            m.return_value = mock.Mock(snapshot=lambda _n: {"id": "abc", "running": True})
            code, body = self.srv.request("POST", "/api/refresh", {"page": "attain"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(m.call_count, 1)
        _, args, kwargs = m.mock_calls[0]
        self.assertEqual(args[2], ("erp-dump", "attain"))

    def test_命令是_steps_不是_skip(self):
        """⚠ 必须用 `--steps`（**就这几步**）：用 `--skip-*` 的话会被
        `ALWAYS_STEPS`（dump/attain）补回来，于是"只抓云商 + 算达成"会变成整批都跑，
        而界面上写着"正在抓新数据"。"""
        m = runner.RunManager()
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            popen.return_value.stdout = iter([])
            popen.return_value.wait.return_value = 0
            job = m.start_steps(self.root, "c.yaml", ("erp-dump", "attain"))
        cmd = " ".join(job.argv)
        self.assertIn("--steps erp-dump,attain", cmd)
        self.assertNotIn("--skip-", cmd)
        self.assertEqual(job.what, "refresh")

    def test_已经在跑就_409_并说人话(self):
        with mock.patch.object(web.manager, "current", return_value=object()):
            code, body = self.srv.request("POST", "/api/refresh", {"page": "attain"})
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        self.assertIn("一趟在跑", body["error"])

    def test_不认识的页面_400(self):
        code, body = self.srv.request("POST", "/api/refresh", {"page": "没有这页"})
        self.assertEqual(code, 400)
        self.assertIn("不认识的页面", body["error"])

    def test_半小时内拉过就跳过抓取步(self):
        """⭐ 2026-09-22 用户：「手动拉取时半个小时内不重复拉取云商和玲珑」。

        跳过的只是**抓库**那几步；算的那几步（attain/plan/film）照跑。
        ⚠ 定时器**不过这道闸** —— 闸只装在 `/api/refresh`（手动）上。
        """
        with mock.patch.object(web, "_cool_skip",
                               return_value=(("attain",), ("erp-dump",))), \
                mock.patch.object(web.manager, "start_steps") as m:
            m.return_value = mock.Mock(snapshot=lambda _n: {"id": "x", "running": True})
            code, body = self.srv.request("POST", "/api/refresh", {"page": "attain"})
        self.assertEqual(code, 200)
        self.assertEqual(body["skipped"], ["erp-dump"])
        self.assertEqual(body["steps"], ["attain"])
        self.assertIn("跳过", body["message"])
        self.assertEqual(m.call_args[0][2], ("attain",))

    def test_全被冷却挡住时不硬跑(self):
        with mock.patch.object(web, "_cool_skip",
                               return_value=((), ("erp-dump",))), \
                mock.patch.object(web.manager, "start_steps") as m:
            code, body = self.srv.request("POST", "/api/refresh", {"page": "film"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(m.call_count, 0, "只该重读，不该再起抓取")
        self.assertIn("不用再抓", body["message"])

    def test_冷却只认成功的那次(self):
        """拉失败了**不算**「刚拉过」—— 失败当然该再拉。"""
        from src.app import data_state as ds
        db = self.root / "out" / "cbg-2026.db"
        import sqlite3
        conn = sqlite3.connect(str(db))
        try:
            ds.ensure_attempt_table(conn)
            ds.record_attempt(conn, "erp-sales", False, why="超时")
            self.assertFalse(ds.fetched_within(self.root, ("erp-sales",), 30))
            ds.record_attempt(conn, "erp-sales", True, rows=1)
            self.assertTrue(ds.fetched_within(self.root, ("erp-sales",), 30))
        finally:
            conn.close()


class Test自动更新不进前端(unittest.TestCase):
    """⭐ 用户 2026-09-21：「自动更新**不进入计时器前端显示，前端日志也不显示**」。

    自动更新每小时跑一趟 ⇒ 摆在任务表里既占地方、又关不掉（`required`），
    日志里更会把真活淹掉（实测一晚上攒了 8 条"已经是最新版"，
    而对账那趟只有 1 条）。

    ⚠ 它**照样记进 runlog**（排查要用）—— 这条也一起钉住：
      "不显示" ≠ "不记录"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        # ⚠ **得有库**：`runlog.record()` 在没库时是**静默不写**的
        #   （`Test定时器的那一屏` 那儿的注释写过）。不建库的话这些断言
        #   全成了"记录本来就没有"，测的是空气。
        db = self.root / "out" / "cbg-2026.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        # ⚠ `test_下一次是每天那趟` 的断言跟钟点耦合（21:00–21:30 必红，见 _FrozenTimerClock）
        p = _freeze_timer_clock()
        p.start()
        self.addCleanup(p.stop)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def _wake(self, slot, steps, ok=True):
        runlog.record("wake", ok, detail={"slot": slot, "steps": steps}, root=self.root)

    def test_任务表里没有它(self):
        _, body = self.srv.request("GET", "/api/timer")
        cmds = [t["cmd"] for t in body["tasks"]]
        self.assertNotIn("autoupdate", cmds)
        # ⚠ 只断言"这几步都在、`autoupdate` 不在"，**不写死全部**
        #   （注册表里加一步就红的测试，守的不是这件事）
        self.assertNotIn("autoupdate", cmds)
        for want in ("dump", "erp-dump", "pos", "pools", "attain"):
            self.assertIn(want, cmds)

    def test_下一次是每天那趟_不是每小时的更新(self):
        _, body = self.srv.request("GET", "/api/timer")
        self.assertNotIn("autoupdate", body["next_run"]["cmds"])
        self.assertIn("dump", body["next_run"]["cmds"])

    def test_执行日志里没有它(self):
        self._wake("2026-09-20 21:00", ["dump", "attain"])
        for hh in range(3):                     # 三趟自动更新
            self._wake("2026-09-21 0%d:17" % hh, ["autoupdate"])
        _, body = self.srv.request("GET", "/api/timer")
        labels = [w["label"] for w in body["history"]]
        self.assertEqual(len(labels), 1, "只该剩那趟真活")
        self.assertNotIn("自动更新", labels)

    def test_但_runlog_里照样记着(self):
        """⚠ 「不显示」≠「不记录」—— 排查时要能查到它到底跑没跑。"""
        self._wake("2026-09-21 05:17", ["autoupdate"])
        rows = runlog.recent(self.root, limit=5, kind="wake")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].get("detail") or {}).get("steps"), ["autoupdate"])

    def test_控制板那行也跳过它(self):
        """⚠ 悬浮窗里剩下的那一行（「上次自动跑」）也要跳过"只有自动更新"的那几趟 ——
        每小时一趟会把真活淹掉（那正是当初加这条过滤的原因）。"""
        self._wake("2026-09-20 21:00", ["dump", "attain"])
        self._wake("2026-09-21 05:17", ["autoupdate"])
        _, body = self.srv.request("GET", "/api/status")
        rows = {r["label"]: r["value"] for r in body["rows"]}
        self.assertIn("抓取玲珑数据", rows["上次自动跑"])
        self.assertNotIn("自动更新", rows["上次自动跑"])


class Test运行日志跳过内部步骤(unittest.TestCase):
    """抽屉里的「运行日志」不该被自动更新顶掉（它每小时一趟）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_认得内部步骤(self):
        from src import runner as R

        class J:
            def __init__(self, argv):
                self.argv = argv
        auto = J(["python", "-m", "src.cli", "daily", "--steps", "autoupdate"])
        self.assertTrue(R.is_internal_job(auto))
        normal = J(["python", "-m", "src.cli", "daily", "--steps", "dump,attain"])
        self.assertFalse(R.is_internal_job(normal))
        both = J(["python", "-m", "src.cli", "daily", "--steps", "autoupdate,attain"])
        self.assertFalse(R.is_internal_job(both), "掺了真活就不是内部任务")
        self.assertFalse(R.is_internal_job(J(["python", "-m", "src.cli", "daily"])),
                         "没有 --steps 不算")

    def test_最近一趟跳过它(self):
        from src import runner as R
        m = R.RunManager()
        def add(job_id, steps):
            job = R.RunJob(job_id, ["python", "-m", "src.cli", "daily",
                                    "--steps", steps], str(self.root))
            m.jobs[job.id] = job
            m.order.append(job.id)

        add("j0", "autoupdate")
        add("j1", "dump,attain")
        self.assertEqual(m.latest().id, "j1")
        # 再来一趟自动更新（最新的），"给人看的"那条仍然是对账那趟
        add("j2", "autoupdate")
        self.assertEqual(m.latest().id, "j2")
        self.assertEqual(m.latest_visible().id, "j1", "抽屉要显示真活那趟")


class Test数据告警的收起来(unittest.TestCase):
    """`POST /api/data-state/dismiss` —— 只关**当前这一条**。

    ⚠ 指纹写 `.secrets/ui-dismissed.json`（这台机器自己的界面状态）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_关掉之后就藏起来(self):
        _, ov = self.srv.request("GET", "/api/overview")
        # 指纹保留在服务端供 dismiss 精确匹配，不再下发给门店角色。
        fp = self.srv.app._data_state_with_dismiss()["fingerprint"]
        if not fp:                       # 这台机器数据全好 ⇒ 没什么可关的
            self.skipTest("这份 fixture 里没有要关的告警")
        self.assertFalse(ov["data_state"]["dismissed"])
        self.assertNotIn("fingerprint", ov["data_state"])
        code, body = self.srv.request("POST", "/api/data-state/dismiss")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("再出新的", body["message"])
        _, ov2 = self.srv.request("GET", "/api/overview")
        self.assertTrue(ov2["data_state"]["dismissed"])

    def test_指纹变了就重新露出来(self):
        """⚠ "关掉"必须**只关这一条** —— 否则就是把告警永久关掉了。"""
        from src.app import data_state as DS
        app = self.srv.app if hasattr(self.srv, "app") else None
        # 直接测文件那一层：写一个**别的**指纹进去 ⇒ 当前这条不算被关
        p = self.root / ".secrets" / "ui-dismissed.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"data_state": "failed|别的源|failed|2020-01-01 00:00:00"}',
                     encoding="utf-8")
        _, ov = self.srv.request("GET", "/api/overview")
        self.assertFalse(ov["data_state"]["dismissed"], "指纹不一样就该露出来")


class Test达成页按登录身份过滤(unittest.TestCase):
    """⭐ 用户 2026-09-21 报的 bug：「我登录了个门店的账号，他的**周度重点达成情况
    显示的是全部的**，不是他们店的」。

    根因：落盘那份 `out/attain-<年>.json` 是**算的时候**按当时的门店过滤的，
    而页面读的是**别人（或上一次）**算好的那一份 ⇒ 门店账号下看到全区。
    ⇒ 读的时候**再按当前画像过一遍**（`show_all` 的平台岗照旧看全部）。
    """

    ROWS = [{"store": "青岛城阳万象汇店", "erp_name": "青岛城阳万象汇店"},
            {"store": "城阳大润发店", "erp_name": "城阳大润发店"}]

    def _app(self, profile):
        from src import web as W
        # 只测内存 scope 过滤；项目根指向会让服务健康探针碰到 out/ 的开发数据。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = W.App(root=tmp.name, config="x.yaml")
        with mock.patch.object(W.config_io, "load_raw", lambda *a, **k: {}), \
             mock.patch.object(W.config_io, "store_profile", lambda *a, **k: profile):
            return app

    def test_门店账号只看自己那一行(self):
        from src import web as W
        app = self._app({"show_all": False, "erp_name": "城阳大润发店"})
        with mock.patch.object(W, "role_scope", return_value={
                "role": "store", "stores": {"城阳大润发店"}, "label": "门店"}):
            d = app.filter_attain_rows({"exists": True, "rows": list(self.ROWS)})
        self.assertEqual([r["store"] for r in d["rows"]], ["城阳大润发店"])
        self.assertEqual(d["store_filter"], "城阳大润发店")

    def test_平台岗照旧看全部(self):
        from src import web as W
        app = self._app({"show_all": True, "erp_name": "平台岗"})
        with mock.patch.object(W, "role_scope", return_value={
                "role": "platform", "stores": None, "label": "平台"}):
            d = app.filter_attain_rows({"exists": True, "rows": list(self.ROWS)})
        self.assertEqual(len(d["rows"]), 2)
        self.assertEqual(d["store_filter"], "")

    def test_过滤完没剩_要说清而不是空白(self):
        """⚠ "看着很合理的空"最坑 —— 得说清是"这份数据里没有本店"。"""
        from src import web as W
        app = self._app({"show_all": False, "erp_name": "别的店"})
        with mock.patch.object(W, "role_scope", return_value={
                "role": "store", "stores": {"别的店"}, "label": "门店"}):
            d = app.filter_attain_rows({"exists": True, "rows": list(self.ROWS)})
        self.assertEqual(d["rows"], [])
        self.assertIn("没有「别的店」那一行", d["error"])


class Test展开门店时列全部在职人员(unittest.TestCase):
    """⭐ 用户 2026-09-21：「点开门店名称时下面的人员名单应该是**门店在职全部的**，
    不是谁有数据才显示谁」。

    ⚠ 原来那份名单是"这周卖过东西的人"推出来的 ⇒ **没开单的人根本分不了目标**。
    ⇒ 后端自己补：`store.staff.rosters_by_store()`（组织架构树 + 240 个账号按机构归堆，
      两次调用拿全区、缓存 12 小时）。前端那条 `roster=` 参数仍然认，但不再依赖它。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        # ⚠ 达成那一份现在按**画像**过滤（门店账号只看本店）——
        #   测试里钉成"平台岗"，否则请求会被滤成"这份数据里没有这家店"。
        from src import config_io
        pr = mock.patch.object(
            config_io, "store_profile",
            lambda *a, **k: {"erp_name": "平台岗", "huawei_code": "", "marker": "",
                             "kind": "平台岗", "huawei_name": "", "in_roster": True,
                             "platform": True, "show_all": True,
                             "needs_linglong": False, "type": "platform"})
        pr.start()
        self.addCleanup(pr.stop)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_在册名单由后端补上(self):
        import json as _json
        from src.features.sales.attain import attain as A
        # 造一份达成数据：这家店这周**只有一个人**有数据
        (self.root / "out" / "attain-2026.json").write_text(_json.dumps({
            "exists": True, "period": "2026-W38", "start": "2026-09-14", "end": "2026-09-20",
            "columns": ["X"], "weights": [1.0], "data_until": "2026-09-20",
            "rows": [{"store": "甲店", "erp_name": "甲店", "matched": True,
                      "targets": [2], "actuals": [1], "rates": [0.5], "total": 0.5,
                      "people": [[["张三", 1, ["X"]]]]}]}, ensure_ascii=False),
            encoding="utf-8")
        with mock.patch("src.features.store.staff.rosters_by_store",
                        lambda *a, **k: {"甲店": ["张三", "李四", "王五"]}):
            # ⚠ 中文要 URL 编码：`http.client` 的请求行只收 ASCII
            code, d = self.srv.request(
                "GET", "/api/attain/split?store=%E7%94%B2%E5%BA%97&period=2026-W38")
        self.assertEqual(code, 200)
        self.assertEqual(d["roster_source"], "erp")
        self.assertEqual([m["name"] for m in d["members"]], ["张三", "李四", "王五"],
                         "没开单的人也要在名单里（不然没法给他分目标）")

    def test_读不到在册名单时不炸_但说清(self):
        import json as _json
        (self.root / "out" / "attain-2026.json").write_text(_json.dumps({
            "exists": True, "period": "2026-W38", "start": "2026-09-14", "end": "2026-09-20",
            "columns": ["X"], "weights": [1.0], "data_until": "2026-09-20",
            "rows": [{"store": "甲店", "erp_name": "甲店", "matched": True,
                      "targets": [2], "actuals": [1], "rates": [0.5], "total": 0.5,
                      "people": [[["张三", 1, ["X"]]]]}]}, ensure_ascii=False),
            encoding="utf-8")
        with mock.patch("src.features.store.staff.rosters_by_store",
                        side_effect=RuntimeError("云商连不上")):
            # ⚠ 中文要 URL 编码：`http.client` 的请求行只收 ASCII
            code, d = self.srv.request(
                "GET", "/api/attain/split?store=%E7%94%B2%E5%BA%97&period=2026-W38")
        self.assertEqual(code, 200)
        self.assertEqual(d["roster_source"], "none")
        self.assertIn("云商连不上", d["roster_error"])
        self.assertEqual([m["name"] for m in d["members"]], ["张三"], "至少还有有数据的人")


class Test壁纸接口(unittest.TestCase):
    """GET/POST/DELETE /api/wallpaper —— **只管文件**；选中态在浏览器，不在这儿。

    ⚠ 不提供 /api/theme（执行规范 7.2）：主题切换仍是前端 localStorage。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def _raw(self, method, path, data: bytes, ctype="image/png"):
        from http.client import HTTPConnection
        c = HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request(method, path, body=data, headers={"Content-Type": ctype})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def test_get_list_empty_then_after_upload(self):
        code, d = self.srv.request("GET", "/api/wallpaper")
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["items"], [])
        self.assertEqual(set(d["exts"]), {".png", ".jpg", ".jpeg", ".webp", ".gif"})

        code, d = self._raw("POST", "/api/wallpaper?name=%E5%BA%97%E5%BA%86.png",
                            b"\x89PNG\r\n\x1a\n")
        self.assertEqual(code, 200, d)
        self.assertEqual(d["name"], "店庆.png")
        self.assertTrue((self.root / "web" / "wallpaper" / "店庆.png").is_file())

        code, d = self.srv.request("GET", "/api/wallpaper")
        self.assertEqual(code, 200)
        self.assertEqual([w["name"] for w in d["items"]], ["店庆.png"])

    def test_post_rejects_bad_extension_and_traversal(self):
        for name in ("x.svg", "x.exe", "noext", "..%2F..%2Fevil.png"):
            with self.subTest(name=name):
                code, d = self._raw("POST", "/api/wallpaper?name=" + name, b"xx")
                self.assertEqual(code, 400, d)
                self.assertIn("error", d)
        # 没写进 web 外
        self.assertFalse((self.root / "evil.png").exists())
        self.assertFalse((self.root / "web" / "evil.png").exists())

    def test_post_rejects_empty_and_oversize(self):
        code, d = self._raw("POST", "/api/wallpaper?name=a.png", b"")
        self.assertEqual(code, 400, d)
        code, d = self._raw("POST", "/api/wallpaper?name=a.png",
                            b"x" * (5 * 1024 * 1024 + 1))
        self.assertEqual(code, 400, d)

    def test_delete_roundtrip(self):
        self._raw("POST", "/api/wallpaper?name=a.png", b"png")
        code, d = self.srv.request("DELETE", "/api/wallpaper?name=a.png")
        self.assertEqual(code, 200, d)
        self.assertTrue(d["ok"])
        self.assertFalse((self.root / "web" / "wallpaper" / "a.png").exists())
        code, d = self.srv.request("DELETE", "/api/wallpaper?name=a.png")
        self.assertEqual(code, 404)


    def test_不提供_theme_接口(self):
        """主题切换不许有后端接口 —— 有就多一处要和 localStorage 对账的状态。"""
        code, _d = self.srv.request("GET", "/api/theme")
        self.assertEqual(code, 404)
        code, _d = self.srv.request("POST", "/api/theme", {"name": "dark"})
        self.assertEqual(code, 404)
        # 路由源码里也不该出现这个 path 分支
        src = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('path == "/api/theme"', src)


class Test静态文件目录边界(unittest.TestCase):
    """静态文件只许从 web/ 下发，目录名相同前缀不能扩大范围。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.web_dir = base / "web"
        self.web_dir.mkdir()
        sibling = base / "web-private"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("private", encoding="utf-8")
        self.root = base / "app"
        self.root.mkdir()
        (self.root / "out").mkdir()
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_static_route_does_not_serve_sibling_with_web_prefix(self):
        with mock.patch.object(web, "WEB_DIR", self.web_dir):
            code, body = self.srv.request("GET", "/%2e%2e/web-private/secret.txt")

        self.assertEqual(code, 404, body)
        self.assertNotEqual(body, "private")


class Test增值日期窗口(unittest.TestCase):
    """日期窗口（2026-09-29）—— 用户：「加个日期选择窗口吧，
    每个月 1 号可以手动拉取上个月全月数据」。

    形态（用户三选一里选的）：**月份 select + 该月内截止日**，**不跨月**；
    契约 `?end=YYYY-MM-DD`，窗口 = **该月 1 号 ～ 这天**；不给 = 今天（原行为不变）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_parse_end_day_合法与非法(self):
        self.assertIsNone(web.parse_end_day(""))
        self.assertIsNone(web.parse_end_day(None))
        self.assertIsNone(web.parse_end_day("   "))
        self.assertEqual(web.parse_end_day("2026-08-31"), datetime.date(2026, 8, 31))
        for bad in ("2026/08/31", "08-31", "2026-13-01", "昨天"):
            with self.assertRaises(ValueError, msg=bad) as cm:
                web.parse_end_day(bad)
            self.assertIn("YYYY-MM-DD", str(cm.exception),
                          "报错要写清格式，别只说 invalid date")

    def test_窗口是该月1号到截止日(self):
        code, d = self.srv.request("GET", "/api/film?end=2026-08-31")
        self.assertEqual(code, 200, d)
        self.assertEqual(d.get("start"), "2026-08-01")
        self.assertEqual(d.get("end"), "2026-08-31")

    def test_不给end就是今天(self):
        code, d = self.srv.request("GET", "/api/film")
        self.assertEqual(code, 200, d)
        today = datetime.date.today()
        self.assertEqual(d.get("start"), today.replace(day=1).isoformat())
        self.assertEqual(d.get("end"), today.isoformat())

    def test_权益页也吃同一个end(self):
        from src.features.valueadd.benefit import compute as bcomp
        # 名册走系统人店表（会联网）—— 集成测试一律 patch 掉（同 test_foreign_note）
        with mock.patch.object(bcomp, "load_people",
                               lambda *a, **k: ({}, "empty", "")):
            code, d = self.srv.request("GET", "/api/benefit?end=2026-08-31")
        self.assertEqual(code, 200, d)
        self.assertEqual(d.get("start"), "2026-08-01")
        self.assertEqual(d.get("end"), "2026-08-31")

    def test_非法end回400带error(self):
        """前端读的是 `error` —— 只回 400 的话用户看不到「为什么失败」。"""
        code, d = self.srv.request("GET", "/api/film?end=2026%2F08%2F31")
        self.assertEqual(code, 400, d)
        self.assertIn("error", d)
        self.assertIn("YYYY-MM-DD", d["error"])

    def test_前端两页都有窗口控件且带end(self):
        for i in ("film-month", "film-day", "film-prev", "film-today",
                  "benefit-month", "benefit-day", "benefit-prev", "benefit-today"):
            self.assertIn('id="%s"' % i, INDEX_HTML, "index.html 缺 id=" + i)
        self.assertIn("/api/film?end=", APP_JS)
        self.assertIn("/api/benefit?end=", APP_JS)
        self.assertIn("winEnd('film')", APP_JS)
        self.assertIn("winEnd('benefit')", APP_JS)
        # 导出必须带同一个 end —— 否则「页面看 8 月、导出 9 月」会静默对不上
        self.assertIn("body: { end: winEnd('film') }", APP_JS)
        self.assertIn("body: { end: winEnd('benefit') }", APP_JS)


class Test明细下钻接口(unittest.TestCase):
    """增值两页 · 点门店行上**每个能下钻的数字** → 明细弹窗背后的两条接口。

    用户 2026-09-29：「每个门店新机销量那个数字，点开要有纳入统计的门店对应的
    销售单号、商品名称和数量，以及销售时间，包含退货。」
    同日追加：「几个具体达成的，点开也显示一下对应的销售单据？比如钢化膜的展示
    钢化膜的销售单」—— 于是接口带 `?kind=`（新机 / 贴膜达成 / 礼包达成 /
    无忧 / Care+ / 各份毛利·利润）。

    钉五件事：
    * **越权 403**（坑 18：菜单里能点到 ≠ 有权限 —— 门店只能查本店）；
    * 没给店 / 没给指标 / 日期写错 / 指标打错 → **400 + `error`**
      （前端读的是 `error`，不是干巴巴的状态码）；
    * 本店那条真走到数据层（没库时说的是「找不到订单库」，不是 404 路由没配）；
    * **前端接线在**（漏了就是「点了没反应」，这个项目最怕的失败）。
    """

    STORE = "青岛永旺东部店"          # _Server 里配的那家店
    OTHER = "青岛城阳万达店"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_查别家店_403两页都拦(self):
        q = "kind=new&store=" + quote(self.OTHER)
        for path in ("/api/film/drill?" + q, "/api/benefit/drill?" + q):
            code, d = self.srv.request("GET", path)
            self.assertEqual(code, 403, (path, d))
            self.assertTrue(d.get("forbidden"), d)
            self.assertIn("没有权限", d.get("error") or "")

    def test_没给门店_400带error(self):
        for path in ("/api/film/drill?kind=new", "/api/benefit/drill?kind=new"):
            code, d = self.srv.request("GET", path)
            self.assertEqual(code, 400, (path, d))
            self.assertIn("门店", d.get("error") or "")

    def test_没给指标_400带error(self):
        """`kind` 决定看哪个指标 —— 漏了要报，别默认成"随便哪个"。"""
        code, d = self.srv.request(
            "GET", "/api/film/drill?store=" + quote(self.STORE))
        self.assertEqual(code, 400, d)
        self.assertIn("指标", d.get("error") or "")

    def test_指标打错_400带指标名(self):
        code, d = self.srv.request(
            "GET", "/api/film/drill?store=" + quote(self.STORE) + "&kind=nope")
        self.assertEqual(code, 400, d)
        self.assertIn("nope", d.get("error") or "")

    def test_日期写错_400带格式说明(self):
        code, d = self.srv.request(
            "GET", "/api/film/drill?store=" + quote(self.STORE)
            + "&kind=new&end=2026/09/30")
        self.assertEqual(code, 400, d)
        self.assertIn("YYYY-MM-DD", d.get("error") or "")

    def test_本店走到数据层_没库说的是找不到订单库(self):
        code, d = self.srv.request(
            "GET", "/api/film/drill?store=" + quote(self.STORE) + "&kind=film")
        self.assertEqual(code, 400, d)
        self.assertIn("订单库", d.get("error") or "",
                      "范围过了、到数据层了 —— 别把「没配库」说成 404/403")

    def test_前端接线在(self):
        self.assertIn("/drill?store=", APP_JS)
        self.assertIn("data-drill", APP_JS)
        self.assertIn("data-kind", APP_JS)
        self.assertIn("openDrill", APP_JS)
        # 每个能下钻的 kind 都要真的拼进格子（漏一个 = 那个数字点了没反应）
        for kind in ("new", "film", "gift", "film_profit", "gift_profit",
                     "total_profit"):
            self.assertIn("'" + kind + "'", APP_JS)
        self.assertIn('id="drill-mask"', INDEX_HTML)
        self.assertIn('id="drill-body"', INDEX_HTML)
        self.assertIn("毛利", APP_JS,
                      "明细表要有毛利列（用户：带上毛利吧）—— 表头是 JS 拼的")
        css = Path("web/style.css").read_text(encoding="utf-8")
        self.assertIn(".drill", css, "可点的那格要看得出来（指针 + hover）")


class Test设置强制刷新(unittest.TestCase):
    """设置 · 强制刷新（2026-09-29 用户：「设置里面加个强制刷新按钮吧，
    按照新规则全部重写数据库」）—— 起一趟
    `pools --fetch erp-sales --start 年初 --end 今天 --rewrite`。

    钉三件事：
    * **argv 必须带 `--rewrite` + 年初窗口**（不带就退化成"又写一遍"，
      老口径的行永远删不掉 —— 那正是这次要修的东西）；
    * **已经在跑 ⇒ 409**（同一把运行锁，绝不并行写同一个库）；
    * 按钮 / 接口的接线在（漏了就是"点了没反应"，这个项目最怕的失败）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    @staticmethod
    def _job():
        class _Job:
            def snapshot(self, _n):
                return {"id": "job-rewrite", "running": True}
        return _Job()

    def test_起跑的命令带_rewrite和年初窗口(self):
        seen = {}

        def fake_start(root, argv, what="", what_label=""):
            seen.update(argv=list(argv), what=what, label=what_label)
            return self._job()

        with mock.patch.object(web.manager, "current", lambda: None), \
             mock.patch.object(web.manager, "start_argv", fake_start):
            code, d = self.srv.request("POST", "/api/sales-rewrite", {})
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("ok"))
        argv = seen["argv"]
        self.assertIn("--rewrite", argv, "没有 --rewrite 就不是重写，是又写一遍")
        self.assertIn("erp-sales", argv)
        y = datetime.date.today().year
        self.assertIn("%d-01-01" % y, argv, "本年度窗口的起点要写死到年初")
        self.assertIn(datetime.date.today().isoformat(), argv)
        self.assertIn("--no-push", argv, "强制刷新不许顺手推送")
        self.assertEqual(seen["what"], "rewrite")

    def test_已经在跑回409(self):
        with mock.patch.object(web.manager, "current", lambda: object()):
            code, d = self.srv.request("POST", "/api/sales-rewrite", {})
        self.assertEqual(code, 409, d)
        self.assertIn("已经有一趟在跑", d.get("error", ""))

    def test_按钮和接口接线在且有二次确认(self):
        self.assertIn('id="btn-sales-rewrite"', INDEX_HTML)
        self.assertIn('id="rewrite-msg"', INDEX_HTML)
        self.assertIn("/api/sales-rewrite", APP_JS)
        # 危险动作必须二次确认（点了就跑 = 手一抖清库）
        i = APP_JS.index("/api/sales-rewrite")
        self.assertIn("confirm(", APP_JS[max(0, i - 700):i])
