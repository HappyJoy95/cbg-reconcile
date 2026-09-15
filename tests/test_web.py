"""Web 控制台测试。

重点不是逐行测 HTML，而是钉住**接线**：
前端引用了一个不存在的 id、或者定时任务接口把"删选中的"退化成"删全部"，
这类错误在浏览器里才炸，跑测试是看不见的。
"""

import io
import json
import re
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

from src import schedule, service, web

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


class TestFrontendWiring(unittest.TestCase):
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

    def test_history_row_builds_html_cells_as_objects(self):
        """历史版本那一行的「版本」单元格**必须**是 `{html: ...}`。

        它踩过一次，而且形态是"在数组外面先把 HTML 算成字符串、再当单元格传"——
        所以上面那条按数组扫描的检查**抓不到它**。这里单独钉一次。

        症状（门店看到的）：页面上原样显示 `<b>v1.4.6</b>` 这串标签。
        """
        m = re.search(r"function renderHistory\(.*?\n\}", APP_JS, re.S)
        self.assertIsNotNone(m, "找不到 renderHistory，测试要跟着改")
        block = m.group(0)
        self.assertIn("{ html:", block,
                      "版本单元格要用 {html: ...}，字符串会被 table() 转义")
        self.assertNotIn("const label = isCur", block,
                         "const label 直接赋 HTML 字符串 = 会被转义（踩过）")

    def test_schedule_html_cells_are_wrapped(self):
        """`table()` 的单元格默认 **esc 转义** —— 想塞原生 HTML 必须包成 `{html: ...}`。

        （截图时真踩到过：页面上直接显示 `<span class="hint">` 的源码。
        不算崩，但很难看，而且测试全绿、只有肉眼能发现。）
        """
        m = re.search(r"const rows = tasks\.map\(.*?\n  \]\);", APP_JS, re.S)
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


class TestRollbackApi(unittest.TestCase):
    """回退接口的接线。

    回退按钮是**动态渲染**出来的，报错时页面上只留一句"回退失败"，
    所以参数有没有正确传到后端、走的哪条路，必须在测试里钉住。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_history_endpoint_returns_versions(self):
        fake = [{"version": "1.4.1", "sha": "a" * 40, "short": "a" * 7,
                 "date": "2026-09-15T09:44:00Z", "message": "release: v1.4.1"}]
        with mock.patch.object(web.selfupdate, "history", lambda *a, **k: fake):
            status, body = self.srv.request("GET", "/api/update?history=1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["versions"][0]["version"], "1.4.1")
        self.assertIn("current", body, "界面要能标出哪个是当前版本")

    def test_history_endpoint_reports_failure_without_crashing(self):
        """GitHub 连不上 / 限流时，接口要如实说，不能 500。"""
        def boom(*a, **k):
            raise web.selfupdate.UpdateError("连不上 GitHub（试了 2 次）")
        with mock.patch.object(web.selfupdate, "history", boom):
            status, body = self.srv.request("GET", "/api/update?history=1")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("连不上", body["message"])

    def test_post_with_ref_goes_through_rollback(self):
        """带 ref → 走 rollback（**不是** upgrade），且 sha 要原样传过去。"""
        seen = {}

        def fake_rollback(root, *, ref, current=""):
            seen["ref"] = ref
            return {"ok": True, "from": current, "to": "1.3.6",
                    "changed": ["src/cli.py"], "count": 1}

        with mock.patch.object(web.selfupdate, "rollback", fake_rollback), \
                mock.patch.object(web.selfupdate, "restart_later", lambda r: False):
            status, body = self.srv.request("POST", "/api/update",
                                            {"ref": "ddfb39d" + "0" * 33})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(seen["ref"], "ddfb39d" + "0" * 33)
        self.assertTrue(body.get("rollback"), "要标明这是一次回退，不是升级")
        self.assertIn("回退到", body["message"])

    def test_post_without_ref_still_upgrades(self):
        """不带 ref 的老行为不能变（升级）。"""
        called = {}

        def fake_apply(root, *, current=""):
            called["upgrade"] = True
            return {"ok": True, "to": "1.5.0", "changed": [], "count": 0}

        with mock.patch.object(web.selfupdate, "apply_update", fake_apply), \
                mock.patch.object(web.selfupdate, "rollback",
                                  lambda *a, **k: self.fail("不该走回退")), \
                mock.patch.object(web.selfupdate, "restart_later", lambda r: False):
            status, body = self.srv.request("POST", "/api/update", {})
        self.assertTrue(called.get("upgrade"))
        self.assertIn("更新到", body["message"])
        self.assertFalse(body.get("rollback"))

    def test_ui_wires_the_history_controls(self):
        """前端得真的去读、真的去回退 —— 光有后端没人调也一样。"""
        self.assertIn("/api/update?history=1", APP_JS)
        self.assertIn("data-rollback", APP_JS)
        self.assertIn("btn-history-load", APP_JS)


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
        import inspect
        from src import cli
        src = inspect.getsource(cli.main)
        self.assertIn('default=60', src, "service-start 的等待时间不该短于 60 秒")


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


class TestRunButtonShowsFeedback(unittest.TestCase):
    """「执行」按钮不能是个黑盒。

    用户原话："点了也不推送" —— 其实推送被跳过了（配置的是"仅有差异时发"），
    日志里写得清清楚楚，但界面上什么都看不到。
    """

    def test_watches_the_run_log(self):
        self.assertIn("/api/runlog", APP_JS)
        self.assertIn("watchRunLog", APP_JS)

    def test_removes_the_progress_banner_when_done(self):
        """跑完了"正在跑…"那条要撤掉 —— 挂着会让人以为还在跑。"""
        self.assertIn("runlog-progress", APP_JS)
        self.assertIn(".remove()", APP_JS)

    def test_push_outcome_is_shown_line_by_line(self):
        """✅ / 跳过 **逐行保留自己的记号**，不要整块染成绿的。

        整块绿的话"邮件跳过"也会跟着变绿，反而误导。
        """
        self.assertIn("banner info", APP_JS, "推送结果那块的样式")
        self.assertIn("d.wecom", APP_JS)
        self.assertIn("d.mail", APP_JS)

    def test_exit_code_is_translated(self):
        """退出码要翻译成人话 + 下一步做什么。"""
        seg = APP_JS[APP_JS.index("async function watchRunLog"):]
        self.assertIn("会话过期", seg)
        self.assertIn("取数失败", seg)


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
                         ["CBG报量对账-12点00", "CBG报量对账-21点00"],
                         "两次注册必须落到两个不同的任务名，否则就是互相覆盖")

    def test_same_time_twice_is_an_intentional_overwrite(self):
        for _ in range(2):
            self.srv.request("POST", "/api/schedule", {"time": "21:00"})
        self.assertEqual(self._created(),
                         ["CBG报量对账-21点00", "CBG报量对账-21点00"])

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

        def fake_remove(name=None):
            seen["name"] = name
            return {"ok": True, "task": name or "默认", "message": "已删除"}

        with mock.patch.object(web.schedule, "remove", fake_remove), \
                mock.patch.object(web.schedule, "status", lambda root: {"installed": False, "tasks": []}):
            st, body = self.srv.request(
                "DELETE", "/api/schedule?name=" + quote("CBG报量对账-中午"))

        self.assertEqual(st, 200)
        self.assertEqual(seen.get("name"), "CBG报量对账-中午")
        self.assertEqual(body["task"], "CBG报量对账-中午")

    def test_delete_without_name_falls_back_to_default(self):
        """不传名字 → None → schedule.remove 用默认任务名（兼容老前端）。"""
        seen = {}

        def fake_remove(name=None):
            seen["name"] = name
            return {"ok": True}

        with mock.patch.object(web.schedule, "remove", fake_remove), \
                mock.patch.object(web.schedule, "status", lambda root: {"installed": False, "tasks": []}):
            st, _ = self.srv.request("DELETE", "/api/schedule")

        self.assertEqual(st, 200)
        self.assertIsNone(seen.get("name"))

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
