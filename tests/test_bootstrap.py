"""bootstrap.py 的测试。

它的存在意义是**在依赖还没装的时候也能跑** —— `src/cli.py` 顶部就 import requests，
拿它去装依赖会先甩一个英文 ImportError 出来（全新门店电脑上必然踩到）。
所以这里的测试重点是：它不能 import 任何第三方包。
"""

import ast
import collections
import inspect
import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "bootstrap.py").read_text(encoding="utf-8")


def _load():
    ns = {"__name__": "boot_test", "__file__": str(ROOT / "bootstrap.py")}
    exec(compile(SRC, "bootstrap.py", "exec"), ns)
    return ns


class TestBootstrapIsStdlibOnly(unittest.TestCase):
    def test_only_stdlib_imports_at_module_level(self):
        """模块顶层**不能** import 第三方包 —— 否则装依赖前根本跑不起来。"""
        tree = ast.parse(SRC)
        tops = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                tops |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
        third_party = tops & {"requests", "yaml", "openpyxl", "src"}
        self.assertFalse(third_party,
                         f"bootstrap.py 顶层不能 import {third_party}（装依赖前它就得能跑）")

    def test_src_cli_is_imported_lazily_inside_a_function(self):
        tree = ast.parse(SRC)
        module_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any("src" in ast.dump(n) for n in module_level),
                         "src.cli 必须在函数里 import（依赖齐了才 import）")


class TestMissing(unittest.TestCase):
    def test_reports_missing_packages(self):
        ns = _load()
        real = importlib.util.find_spec
        with mock.patch.object(importlib.util, "find_spec",
                               lambda n, *a, **k: None if n == "requests" else real(n, *a, **k)):
            self.assertEqual(ns["missing"](), ["requests"])

    def test_all_present(self):
        ns = _load()
        self.assertEqual(ns["missing"](), [], "本机三个依赖都装了")

    def test_required_covers_what_the_code_imports(self):
        """别漏 —— 少写一个，门店电脑上就会以 ImportError 的形式暴露。"""
        ns = _load()
        self.assertEqual(set(ns["REQUIRED"]), {"requests", "yaml", "openpyxl"})


class TestBuildStampForClonedTrees(unittest.TestCase):
    """clone 部署没有 `BUILD.txt`（它是未跟踪文件）。

    install 时从 `.git` 补一个 —— 否则界面和自检只能显示
    「源码运行（未打包）」，门店报问题时没人说得清跑的是哪一版。
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.ns = _load()
        self.ns["ROOT"] = self.tmp          # 所有路径都由 ROOT 派生，改它就够

    def _make_git(self, sha="b0250d232b2a1cdf2b47f888ab2e35b765657e88"):
        (self.tmp / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.tmp / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                                encoding="utf-8")
        (self.tmp / ".git" / "refs" / "heads" / "main").write_text(
            sha + "\n", encoding="utf-8")

    def test_writes_a_stamp_when_there_is_dot_git_and_no_BUILD_txt(self):
        self._make_git()
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.ns["write_build_stamp"]()
        stamp = (self.tmp / "BUILD.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(stamp, "git main@b0250d2")
        self.assertIn("git main@b0250d2", buf.getvalue(), "写没写要让人看见")

    def test_never_overwrites_an_existing_stamp(self):
        """打包写的时间戳、自更新写的 `GitHub main · v1.2.0` 都不能被冲掉。"""
        (self.tmp / "BUILD.txt").write_text("2026-09-15 15:14\n", encoding="utf-8")
        self._make_git()
        self.ns["write_build_stamp"]()
        self.assertEqual(
            (self.tmp / "BUILD.txt").read_text(encoding="utf-8").strip(),
            "2026-09-15 15:14")

    def test_does_nothing_without_dot_git(self):
        """解压出来的正式包 / 直接跑源码 —— 别凭空造个 fingerprint。"""
        self.ns["write_build_stamp"]()
        self.assertFalse((self.tmp / "BUILD.txt").exists())

    def test_is_called_by_install(self):
        """忘了接进 do_install 的话，这个功能等于没做。"""
        self.assertIn("write_build_stamp()", SRC)

    def test_failure_can_never_break_the_install(self):
        """写指纹失败绝不能把安装搞挂 —— 它只是锦上添花。"""
        self.ns["ROOT"] = self.tmp / "不存在的目录" / "更深一层"
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["write_build_stamp"]()          # 不抛异常就算过


class TestMainRouting(unittest.TestCase):
    def _ns(self, missing_list):
        ns = _load()
        ns["missing"] = lambda: missing_list
        return ns

    def test_refuses_to_run_commands_without_deps(self):
        import io
        import contextlib
        ns = self._ns(["requests"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ns["main"]()
        out = buf.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("install.bat", out, "要告诉人下一步做什么")
        self.assertIn("requests", out)

    def test_install_path_does_not_need_deps(self):
        """install 分支在依赖缺失时也必须能进去（它就是来装依赖的）。"""
        ns = self._ns(["requests", "yaml", "openpyxl"])
        called = {}

        def fake_install():
            called["yes"] = True
            return 0

        ns["do_install"] = fake_install
        ns["ask_autostart"] = lambda: None      # 别让它去调真的（会往 stdout 漏东西）
        import sys
        old = sys.argv
        sys.argv = ["bootstrap.py", "install"]
        try:
            code = ns["main"]()
        finally:
            sys.argv = old
        self.assertEqual(code, 0)
        self.assertTrue(called.get("yes"), "install 分支不能被依赖检查挡住")


class TestWindowsElevation(unittest.TestCase):
    """开机自启要「以管理员身份」，而注册这种东西**天生需要管理员权限**。

    这里钉住三件事：
    1. 非 Windows 上 `is_admin()` 返回 True —— 别拿 Windows 的概念拦别的平台；
    2. **只提权 autostart 这一步，不提权装依赖** —— 否则"标准用户 + 管理员密码"
       的机器上，pip 会装进管理员账号的 site-packages，普通用户 import 不到；
    3. UAC 被拒时**退回普通权限注册**，不能直接失败。
    """

    def setUp(self):
        self.ns = _load()
        self.src_pkg = __import__("src", fromlist=["src"])
        self.orig = getattr(self.src_pkg, "autostart", None)

    def tearDown(self):
        if self.orig is not None:
            self.src_pkg.autostart = self.orig
        elif hasattr(self.src_pkg, "autostart"):
            del self.src_pkg.autostart
        sys.modules.pop("src.autostart", None)

    def _fake_autostart(self, installed=False, elevated=True, result=None):
        fake = types.ModuleType("src.autostart")
        fake.status = lambda root: {"installed": installed, "elevated": elevated,
                                    "platform": "Windows"}
        fake.install = lambda root: result or {"ok": True, "message": "已注册"}
        sys.modules["src.autostart"] = fake
        self.src_pkg.autostart = fake
        return fake

    def _call(self, ns, argv):
        old = sys.argv
        sys.argv = ["bootstrap.py", *argv]
        try:
            return ns["main"]()
        finally:
            sys.argv = old

    def test_non_windows_is_admin(self):
        self.assertTrue(self.ns["is_admin"](), "macOS/Linux 上不该被管理员检查拦住")

    def test_autostart_step_is_elevated(self):
        """点「是」之后，Windows 上非管理员应该去请求 UAC，而不是直接注册。"""
        ns = _load()
        ns["os"] = types.SimpleNamespace(name="nt")
        ns["is_admin"] = lambda: False
        seen = {}

        def fake_relaunch(cmd):
            seen["cmd"] = cmd
            return True

        ns["relaunch_as_admin"] = fake_relaunch
        self._fake_autostart()
        called = {"install": 0}
        ns["do_autostart"] = lambda: called.__setitem__("install", called["install"] + 1) or 0

        old = sys.stdin
        sys.stdin = TTYInput("y\n")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ns["ask_autostart"]()
        finally:
            sys.stdin = old

        self.assertEqual(seen.get("cmd"), "autostart", "提权时只能重跑 autostart 子命令")
        self.assertEqual(called["install"], 0, "已经交给提权后的进程了，本进程不该再注册")
        self.assertIn("UAC", buf.getvalue(), "要提前告诉用户会弹 UAC")

    def test_uac_denied_still_registers_without_elevation(self):
        """用户点了"否"也不能就放弃 —— 退回普通权限注册，功能照样可用。"""
        ns = _load()
        ns["os"] = types.SimpleNamespace(name="nt")
        ns["is_admin"] = lambda: False
        ns["relaunch_as_admin"] = lambda cmd: False
        self._fake_autostart()
        called = {"n": 0}

        def fake_do():
            called["n"] += 1
            return 0

        ns["do_autostart"] = fake_do

        old = sys.stdin
        sys.stdin = TTYInput("y\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ns["ask_autostart"]()
        finally:
            sys.stdin = old
        self.assertEqual(called["n"], 1, "提权失败也要继续用普通权限注册")

    def test_install_step_is_never_relaunched(self):
        """⚠ 装依赖**绝不能**提权跑。

        如果是"标准用户 + 管理员账号密码"，UAC 后跑的是**另一个账号**，
        pip 会把包装进那个账号的 site-packages，当前用户 import 不到 ——
        现象是"装完了还是说缺依赖"，极难排查。
        """
        ns = _load()
        seen = []
        ns["do_install"] = lambda: 0
        ns["ask_autostart"] = lambda: None
        ns["relaunch_as_admin"] = lambda cmd: seen.append(cmd) or True
        self._call(ns, ["install"])
        self.assertEqual(seen, [], "install 路径不许提权")
        # 提权的目标只能是 autostart
        self.assertIn("autostart", ns["STDLIB_ONLY"])

    def test_relaunch_denied_is_reported(self):
        ns = _load()
        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = types.SimpleNamespace(
            shell32=types.SimpleNamespace(ShellExecuteW=lambda *a: 5))   # 5 = 拒绝访问
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            with contextlib.redirect_stdout(buf):
                ok = ns["relaunch_as_admin"]("autostart")
        self.assertFalse(ok)
        self.assertIn("管理员权限", buf.getvalue())

    def test_relaunch_success_returns_true(self):
        ns = _load()
        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = types.SimpleNamespace(
            shell32=types.SimpleNamespace(ShellExecuteW=lambda *a: 42))  # >32 = 成功
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(ns["relaunch_as_admin"]("autostart"))

    def test_relaunch_passes_pause_so_the_new_window_stays(self):
        """提权后会**另开一个窗口**，跑完就关的话用户根本看不到结果。"""
        ns = _load()
        got = {}
        fake_ctypes = types.ModuleType("ctypes")

        def spy(hwnd, verb, exe, params, cwd, show):
            got.update(verb=verb, exe=exe, params=params, cwd=cwd, show=show)
            return 42

        fake_ctypes.windll = types.SimpleNamespace(
            shell32=types.SimpleNamespace(ShellExecuteW=spy))
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            ns["relaunch_as_admin"]("autostart")
        self.assertEqual(got["verb"], "runas", "必须用 runas 才会触发 UAC 提权")
        self.assertIn("autostart", got["params"])
        self.assertIn("--pause", got["params"])
        self.assertIn("bootstrap.py", got["params"])
        self.assertTrue(got["params"].startswith('"'), "脚本路径要加引号（目录可能带空格）")

    def test_autostart_command_works_without_dependencies(self):
        """提权后那个窗口走的是 `bootstrap.py autostart` ——
        它**必须**在依赖缺失时也能跑，否则等于白提权。"""
        ns = _load()
        ns["missing"] = lambda: ["requests", "yaml", "openpyxl"]
        called = {"n": 0}
        ns["do_autostart"] = lambda: called.__setitem__("n", called["n"] + 1) or 0
        old = sys.argv
        sys.argv = ["bootstrap.py", "autostart"]
        try:
            code = ns["main"]()
        finally:
            sys.argv = old
        self.assertEqual(code, 0)
        self.assertEqual(called["n"], 1)

    def test_pause_flag_is_stripped_before_routing(self):
        """`--pause` 是给提权窗口用的，不能被当成子命令传下去。"""
        ns = _load()
        ns["do_autostart"] = lambda: 0
        old = sys.argv
        sys.argv = ["bootstrap.py", "autostart", "--pause"]
        try:
            import contextlib as _c
            with _c.redirect_stdout(io.StringIO()):
                with mock.patch("builtins.input", lambda *a: ""):
                    code = ns["main"]()
        finally:
            sys.argv = old
        self.assertEqual(code, 0)

    def test_already_elevated_does_not_ask_again(self):
        """已经设过**且是提权的** → 直接跳过，别啰嗦。"""
        ns = _load()
        self._fake_autostart(installed=True, elevated=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns["ask_autostart"]()
        self.assertIn("已经设过", buf.getvalue())

    def test_existing_non_elevated_install_offers_upgrade(self):
        """以前注册的是注册表项（普通权限）—— 再跑 install 要能升级成管理员。"""
        ns = _load()
        ns["os"] = types.SimpleNamespace(name="nt")     # 这是 Windows 才有的分支
        self._fake_autostart(installed=True, elevated=False)
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = TTYInput("n\n")           # 这次先拒绝，只看提示
        try:
            with contextlib.redirect_stdout(buf):
                ns["ask_autostart"]()
        finally:
            sys.stdin = old
        self.assertIn("普通权限", buf.getvalue())

    def test_non_windows_already_installed_is_left_alone(self):
        """macOS/Linux 上没有"提权注册"这回事 —— 已设过就直接跳过，别瞎升级。"""
        ns = _load()
        self._fake_autostart(installed=True, elevated=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns["ask_autostart"]()
        self.assertIn("已经设过", buf.getvalue())
        self.assertNotIn("普通权限", buf.getvalue())


class TestUninstall(unittest.TestCase):
    """卸载要撤干净，但**不能顺手删数据**。

    卸载脚本最危险的地方就是"太勤快" —— 用户想撤掉服务，结果报告和凭据
    一起没了，而且恢复不了。
    """

    def test_uninstall_bat_is_ascii_and_has_a_fallback(self):
        src = (ROOT / "uninstall.bat").read_text(encoding="ascii")
        self.assertIn(":nopython", src, "没装 Python 时要能告诉用户怎么手动卸")
        self.assertIn("Task Scheduler", src, "手动卸载步骤要写出来")

    def test_yes_alone_does_not_delete_data(self):
        """`--yes` 只跳过"确定吗"，**不代表要删数据**。

        删报告和凭据的破坏性太大，必须单独一个 `--purge`。
        （第一版就是 `--yes` 连数据一起删 —— 真跑的时候把自己的 out/ 删了。）
        """
        src = (ROOT / "bootstrap.py").read_text(encoding="utf-8")
        self.assertIn("wipe = purge", src, "--yes 不该等于删数据")
        self.assertIn('"--purge" in sys.argv', src, "要支持 --purge")

    def test_uninstall_reports_what_it_cannot_delete(self):
        """删不掉（有进程正在用）时要**报告出来**，不能假装成功。"""
        src = (ROOT / "bootstrap.py").read_text(encoding="utf-8")
        self.assertIn("problems.append", src)
        self.assertIn("还有程序在用", src, "要指出最可能的原因")

    def test_uninstall_is_stdlib_only(self):
        """卸载必须能在依赖坏了/没装的情况下跑起来 —— 那时才最需要卸载。"""
        ns = _load()
        self.assertIn("uninstall", ns["STDLIB_ONLY"])

    def test_uninstall_removes_tasks_and_autostart(self):
        from src import schedule, autostart
        self.assertTrue(hasattr(schedule, "remove_all"))
        self.assertTrue(hasattr(autostart, "remove"))
        # remove_all 要连开机自启那个任务一起删（它不在"定时任务"的过滤范围里）
        self.assertIn("AUTOSTART_TASK", inspect.getsource(schedule.remove_all))


class TestPythonVersionGuard(unittest.TestCase):
    """门店电脑装的是 3.14，开发机是 3.9 —— 代码按 **3.9+ 都能跑** 写，两头都测。

    这里钉住的是"版本不对时说人话"：与其后面报一堆看不懂的错，
    不如开头就说清楚现在是多少、要装哪个。
    """

    def test_current_python_is_supported(self):
        ns = _load()
        self.assertEqual(ns["check_python"](), 0,
                         f"本机 {ns['python_version']()} 应该被认作可用")

    def test_min_version_is_3_9(self):
        ns = _load()
        self.assertEqual(tuple(ns["MIN_PYTHON"]), (3, 9))

    def test_too_old_is_rejected_with_a_next_step(self):
        ns = _load()
        # sys.version_info 是真 namedtuple（能切片、能取属性）—— 假的也要像
        vi = collections.namedtuple("version_info", "major minor micro")(3, 8, 10)
        ns["sys"] = types.SimpleNamespace(version_info=vi, executable="python")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ns["check_python"]()
        out = buf.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("3.8.10", out, "要说出用户现在是几")
        self.assertIn("3.9", out, "要说出需要几")
        self.assertIn("PATH", out, "要给出能照着做的下一步")

    def test_main_refuses_to_run_anything_on_an_old_python(self):
        ns = _load()
        ns["check_python"] = lambda: 2
        called = {"n": 0}
        ns["do_install"] = lambda: called.__setitem__("n", called["n"] + 1) or 0
        old = sys.argv
        sys.argv = ["bootstrap.py", "install"]
        try:
            code = ns["main"]()
        finally:
            sys.argv = old
        self.assertEqual(code, 2)
        self.assertEqual(called["n"], 0, "版本不对就什么都别干")

    def test_version_string_is_reported(self):
        ns = _load()
        self.assertRegex(ns["python_version"](), r"^\d+\.\d+\.\d+$")


class TestNoForwardIncompatibleSyntax(unittest.TestCase):
    """别写"现在只是警告、以后会变成错误"的语法。

    Python 3.14 把非法转义序列从 DeprecationWarning 升级成了 **SyntaxWarning**，
    并明说"Such sequences will not work in the future"。等到真变 SyntaxError，
    门店电脑上双击 bat 会直接起不来。
    """

    def test_no_invalid_escape_sequences(self):
        import warnings
        bad = []
        for p in sorted(ROOT.rglob("*.py")):
            if any(x in p.parts for x in (".venv", ".pythons", ".uv-cache",
                                          "__pycache__", "dist")):
                continue
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                try:
                    compile(p.read_text(encoding="utf-8"), str(p), "exec")
                except SyntaxError as e:               # pragma: no cover
                    bad.append(f"{p}: SyntaxError {e}")
                    continue
                # ⚠ 3.9 上是 DeprecationWarning，3.13+ 才升级成 SyntaxWarning。
                #   只查 SyntaxWarning 的话，**开发机（3.9）上这条测试等于没写**。
                bad += [f"{p}:{x.lineno}: {x.message}" for x in w
                        if issubclass(x.category, (SyntaxWarning, DeprecationWarning))
                        and "invalid escape sequence" in str(x.message)]
        self.assertEqual(bad, [], "有会变成错误的语法：\n" + "\n".join(bad))

    def test_no_python310_only_stdlib_apis(self):
        """`Path.write_text(newline=)` / `str.removeprefix` 是 3.10+ 才有的。

        门店是 3.14 用不出问题，但**开发机是 3.9** —— 写了就在这儿炸，
        白白浪费一次"改了但没法验"。

        ⚠ 必须走 AST 找**真实调用**：直接 grep 会把解释这个坑的注释也算进去。
        """
        hits = []
        # ⚠ 连 tests/ 一起扫 —— 我自己就在测试里写下过 write_text(newline=)，
        #   而当时这个守卫只看 src/，是 3.9 上的 TypeError 才把它暴露出来的。
        files = [p for p in ROOT.rglob("*.py")
                 if not any(x in p.parts for x in
                            (".venv", ".pythons", ".uv-cache", "__pycache__", "dist"))]
        for p in sorted(files):
            tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not isinstance(f, ast.Attribute):
                    continue
                # ⚠ 名单要准：`str.removeprefix` 是 **3.9** 就有的（PEP 616），
                #   列进来只会误报。只列真正 3.10+ 的。
                if f.attr in ("write_text", "write_bytes") and any(
                        k.arg == "newline" for k in node.keywords):
                    hits.append(f"{p}:{node.lineno}: {f.attr}(newline=) 是 3.10+")
                elif f.attr == "bit_count":
                    hits.append(f"{p}:{node.lineno}: int.bit_count() 是 3.10+")
                elif f.attr == "pairwise":
                    hits.append(f"{p}:{node.lineno}: itertools.pairwise 是 3.10+")
                elif f.attr == "zip" and any(k.arg == "strict" for k in node.keywords):
                    hits.append(f"{p}:{node.lineno}: zip(strict=) 是 3.10+")
        self.assertEqual(hits, [], "用了 3.10+ 才有的 API（开发机是 3.9）：\n"
                                   + "\n".join(hits))


class TestBatsArePureAscii(unittest.TestCase):
    """bat 里一个中文都不能有 —— 编码受控制台代码页摆布，会显示成方块。

    bat 就在仓库根目录 —— **仓库布局和安装布局是同一套**，这样从 GitHub 更新
    时是照原样铺过去，不需要任何映射表。
    所以在解压出来的包里跑测试时这里要跳过 —— 否则 "包内单测全绿" 就成了假的。
    """

    HERE = ROOT


    def test_all_bats_ascii(self):
        bats = sorted(ROOT.glob("*.bat"))
        self.assertTrue(bats, "至少得有 install/start/stop/selftest/uninstall")
        for b in bats:
            raw = b.read_bytes()
            bad = [i for i, c in enumerate(raw) if c > 127]
            self.assertFalse(bad, f"{b.name} 里有 {len(bad)} 个非 ASCII 字节 —— "
                                  f"中文要交给 Python 打印")

    def test_entry_bats_survive_a_missing_python(self):
        """没装 Python（或点开的是 Microsoft Store 那个假 python）时，
        双击 bat **不能静默退出** —— 用户会以为"装好了但没反应"。

        ⚠ 这条消息只能是**英文 ASCII** —— 中文全靠 Python 打印，
        而这时候恰恰没有 Python。所以这是唯一的例外。

        `uninstall.bat` 的兜底内容不一样：它要告诉人**怎么手动卸**，
        而不是去哪装 Python（都要卸了还装什么）。
        """
        for name in ("install", "start"):
            with self.subTest(bat=name):
                src = (ROOT / f"{name}.bat").read_text(encoding="ascii")
                self.assertIn(":nopython", src, f"{name}.bat 没有 Python 缺失兜底")
                self.assertIn("import sys", src, "要先探一下 python 在不在")
                self.assertIn("python.org", src, "要给出下载地址")
                self.assertIn("PATH", src, "要提醒勾选 Add python.exe to PATH")
                self.assertIn("Microsoft Store", src,
                              "要点明商店那个是假的（门店最常见的坑）")

    def test_all_bats_probe_for_a_working_python(self):
        """每个 bat 都要先找一个**能用的** python，找不到就停下说清楚。

        门店实测：`python` 是 Microsoft Store 那个**假别名**，双击 bat 只弹个商店、
        什么都不发生 —— 用户完全不知道出了什么事。现在会停下来打一段英文说明
        （英文是没办法：中文全靠 Python 打印，而这时恰恰没有 Python）。
        """
        for name in ("install", "start", "stop", "selftest", "uninstall"):
            with self.subTest(bat=name):
                src = (ROOT / f"{name}.bat").read_text(encoding="ascii")
                self.assertIn("PYBIN", src, f"{name}.bat 没有探测可用的 python")
                self.assertIn("goto :nopython", src)
                self.assertIn("where python", src, "要打出来 python 到底在哪")
                self.assertIn("Microsoft Store", src, "要点明商店那个是假的")

    def test_bats_fall_back_to_the_py_launcher(self):
        """装了 Python 但没勾 "Add python.exe to PATH" 时，`py` 启动器还在。

        只认 `python` 的话会**误判成"没装 Python"** —— 明明装了的用户
        会被拦住，还告诉他去重装。
        """
        for name in ("install", "start", "stop", "selftest", "uninstall"):
            with self.subTest(bat=name):
                src = (ROOT / f"{name}.bat").read_text(encoding="ascii")
                self.assertIn("py -3 -c", src, f"{name}.bat 没有回退到 py 启动器")
                self.assertIn("if not defined PYBIN", src)

    def test_bats_use_the_probed_interpreter(self):
        """探测完了要**真的用它**跑 —— 不然白探。"""
        for name in ("install", "start", "stop", "selftest", "uninstall"):
            with self.subTest(bat=name):
                src = (ROOT / f"{name}.bat").read_text(encoding="ascii")
                self.assertIn("%PYBIN% bootstrap.py", src)
                self.assertNotIn("\npython bootstrap.py", src,
                                 "还有地方硬写 python，会绕过探测")

    def test_bats_never_fall_through_into_nopython(self):
        """⚠ 正常跑完**绝不能**穿透进 :nopython 块。

        穿透了的话：装成功了还会再打一遍"找不到 Python"，然后 pause ——
        用户看到的就是"明明成功了怎么还让我按回车"。
        """
        for name in ("install", "start", "stop", "selftest", "uninstall"):
            with self.subTest(bat=name):
                lines = [ln.strip() for ln in
                         (ROOT / f"{name}.bat").read_text(encoding="ascii").splitlines()]
                i = lines.index(":nopython")
                before = [ln for ln in lines[:i] if ln and not ln.startswith("rem")]
                self.assertTrue(before, f"{name}.bat 的 :nopython 前面什么都没有？")
                self.assertTrue(before[-1].lower().startswith("exit /b"),
                                f"{name}.bat 会穿透进 :nopython（前面一行是 {before[-1]!r}）")

    def test_install_bat_keeps_the_exit_code(self):
        """`pause` 会把 ERRORLEVEL 冲成 0 —— 必须先存下来再 pause。"""
        src = (ROOT / "install.bat").read_text(encoding="ascii")
        self.assertIn("set RC=%ERRORLEVEL%", src)
        self.assertIn("exit /b %RC%", src)
        self.assertNotIn("pause\r\nexit /b %ERRORLEVEL%", src.replace("\n", "\r\n"),
                         "pause 之后 ERRORLEVEL 已经不是 python 的了")

    def test_all_bats_go_through_bootstrap(self):
        """所有 bat 都走 bootstrap.py —— 否则依赖没装时又变成英文 ImportError。"""
        for b in sorted(ROOT.glob("*.bat")):
            text = b.read_text(encoding="ascii")
            self.assertIn("bootstrap.py", text, f"{b.name} 没走 bootstrap.py")
            self.assertNotIn("-m src.cli", text,
                             f"{b.name} 直接调 src.cli 了 —— 依赖没装时会 ImportError")


if __name__ == "__main__":
    unittest.main()


class TTYInput(io.StringIO):
    def isatty(self):
        return True


class TestAutostartPrompt(unittest.TestCase):
    """装完依赖问一句要不要开机自启。

    为什么是"问"而不是"直接装"：安装脚本静默改系统开机项会让人意外
    （"我就装个依赖，怎么开机多了个东西"）。但完全不做又会漏 ——
    门店电脑上最容易忘的就是这一步。
    """

    def setUp(self):
        self.ns = _load()
        import src as src_pkg
        self.src_pkg = src_pkg
        self.orig = getattr(src_pkg, "autostart", None)
        self.called = {}

    def tearDown(self):
        if self.orig is not None:
            self.src_pkg.autostart = self.orig
        elif hasattr(self.src_pkg, "autostart"):
            del self.src_pkg.autostart
        sys.modules.pop("src.autostart", None)

    def _fake(self, installed=False, result=None):
        import types
        fake = types.ModuleType("src.autostart")
        fake.status = lambda root: {"installed": installed, "platform": "TestOS"}
        fake.install = lambda root: (self.called.setdefault("root", root),
                                     result or {"ok": True, "message": "已加入开机启动"})[1]
        # ⚠ 两边都要塞：`from src import autostart` 走的是**包属性**，不只是 sys.modules
        sys.modules["src.autostart"] = fake
        self.src_pkg.autostart = fake

    def _run(self, stdin_text, tty=True):
        import contextlib
        import io as _io
        old = sys.stdin
        sys.stdin = TTYInput(stdin_text) if tty else _io.StringIO(stdin_text)
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.ns["ask_autostart"]()
        finally:
            sys.stdin = old
        return buf.getvalue()

    def test_enter_means_yes(self):
        self._fake()
        out = self._run("\n")
        self.assertTrue(self.called, "回车应当就是「开」")
        self.assertIn("已加入", out)

    def test_explicit_yes(self):
        self._fake()
        self._run("y\n")
        self.assertTrue(self.called)

    def test_no_skips_and_points_to_the_ui(self):
        self._fake()
        out = self._run("n\n")
        self.assertFalse(self.called, "回答 n 就不该注册")
        self.assertIn("后台服务", out, "要告诉人去哪开")

    def test_already_installed_does_not_ask(self):
        self._fake(installed=True)
        out = self._run("\n")
        self.assertNotIn("设为开机自动启动？", out, "已经设过就别再问")
        self.assertFalse(self.called, "不该重复注册")
        self.assertIn("已经设过", out)

    def test_non_interactive_does_not_block_or_register(self):
        """被别的脚本调起来时别卡在 input() 上。"""
        self._fake()
        out = self._run("y\n", tty=False)
        self.assertNotIn("设为开机自动启动？", out)
        self.assertFalse(self.called)
        self.assertIn("autostart --on", out, "要给出替代做法")

    def test_register_failure_gives_next_step(self):
        self._fake(result={"ok": False, "message": "拒绝访问"})
        out = self._run("y\n")
        self.assertIn("拒绝访问", out)
        self.assertIn("后台服务", out, "失败了也要告诉人去哪再试")

    def test_module_missing_does_not_crash(self):
        import sys as _s
        _s.modules["src.autostart"] = None          # 模拟 import 失败
        if hasattr(self.src_pkg, "autostart"):
            del self.src_pkg.autostart
        out = self._run("\n")
        self.assertTrue(out)                         # 只要不抛异常就行
        _s.modules.pop("src.autostart", None)


class TestInstallThenAsk(unittest.TestCase):
    def test_asks_only_after_successful_install(self):
        ns = _load()
        asked = {}
        ns["do_install"] = lambda: 2                  # 装失败
        ns["ask_autostart"] = lambda: asked.setdefault("x", True)
        old = sys.argv
        sys.argv = ["bootstrap.py", "install"]
        try:
            code = ns["main"]()
        finally:
            sys.argv = old
        self.assertEqual(code, 2)
        self.assertFalse(asked, "依赖都没装好，别去问开机自启")

    def test_asks_after_successful_install(self):
        ns = _load()
        asked = {}
        ns["do_install"] = lambda: 0
        ns["ask_autostart"] = lambda: asked.setdefault("x", True)
        old = sys.argv
        sys.argv = ["bootstrap.py", "install"]
        try:
            code = ns["main"]()
        finally:
            sys.argv = old
        self.assertEqual(code, 0)
        self.assertTrue(asked.get("x"), "装好了就该问一句")


class TestNoRealSideEffects(unittest.TestCase):
    def test_source_does_not_import_autostart_at_module_level(self):
        """ask_autostart 里才 import —— 顶层 import 会在依赖缺失时把它带崩。"""
        tree = ast.parse(SRC)
        tops = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any("autostart" in ast.dump(n) for n in tops))
