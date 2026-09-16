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
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "bootstrap.py").read_text(encoding="utf-8")



def _js_code() -> str:
    """app.js 去掉注释后的正文。

    ⚠ **两种注释都要剥**：只剥 `//` 的话，解释"别写 X"的**块注释**会被当成违规
    —— 真踩了，而且报错信息是把整个文件打出来，很难看出问题在哪。
    """
    import re
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)          # /* ... */
    return "\n".join(ln.split("//", 1)[0] for ln in js.splitlines())

def _load():
    ns = {"__name__": "boot_test", "__file__": str(ROOT / "bootstrap.py")}
    exec(compile(SRC, "bootstrap.py", "exec"), ns)
    # ⚠ 默认把 `ensure_layout` 换成空操作 —— 它是**真会往仓库根写文件**的
    #   （`.secrets/`、`out/`、`config/store-*.yaml`）。不换的话：
    #   1. 跑一趟测试就把工作区弄脏；
    #   2. 更坏的是**藏起顺序依赖** —— 某个测试调 `main()` 顺手把门店配置建出来，
    #      另一个正读这个文件的测试就"恰好"通过了，新克隆的仓库上却会挂。
    #      （这个坑真踩了：模拟新克隆时测试全绿，其实是测试自己把文件补回来的。）
    #   要测它本人的在 `TestEnsureLayout` 里换回 `_real_ensure_layout` 再调。
    ns["_real_ensure_layout"] = ns["ensure_layout"]
    ns["ensure_layout"] = lambda: None
    return ns


# 扫全项目 .py 时跳过的目录。这些里面全是**别人写的**代码（venv 里的第三方包、
# 缓存、打包产物），扫进来只会在旧解释器上误报。
_SKIP_PARTS = {".venv", ".pythons", ".uv-cache", "__pycache__", "dist", "node_modules"}
# ⚠ `.dsh/` **不能整个跳过**：它底下既有工具目录（venv / 缓存 / 备份），
#   也有 `.dsh/workspace/` —— 那是"验证通过就搬进项目根"的代码，
#   恰恰是最该被这些守卫看住的。所以只按前缀跳过工具那几个。
_SKIP_REL = (".dsh/uv-cache/", ".dsh/uv-python/", ".dsh/tasks/",
             ".dsh/backups/", ".dsh/archived/")


def _scan_files():
    """项目里**我们自己写的** .py，按路径排序。"""
    out = []
    for p in ROOT.rglob("*.py"):
        if any(x in p.parts for x in _SKIP_PARTS):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(_SKIP_REL):
            continue
        out.append(p)
    return sorted(out)


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
    """⚠ **开机自启不再请求任何权限、不弹 UAC**（2026-09-16 改）。

    注册的是注册表 `HKCU\\...\\Run` —— 当前用户自己的分支，任何用户都能写。
    以前这里会用 UAC 提权去注册"以管理员身份启动"的计划任务，而管理员身份会让
    **「自动抓华为会话」彻底不可用**：浏览器拒绝以管理员运行，把命令行交棒出去
    就自己退 0，我们等不到调试端口。那次 UAC 除了一件坏事什么都换不来。

    这里钉住四件事：
    1. 非 Windows 上 `is_admin()` 返回 True —— 别拿 Windows 的概念拦别的平台；
    2. **装机这一步一次都不提权**（装依赖不提权、注册开机自启也不提权）；
    3. 已设过且是普通权限 → 直接跳过，不瞎折腾；
    4. 已设过但是**管理员**模式 → 提示它会弄坏抓会话，并问要不要改回普通权限；
    5. 提权零件 `relaunch_as_admin` 还在（按需提权时要用），只是不再被这条流程调用。
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

    def test_autostart_step_never_asks_for_uac(self):
        """⚠ 注册开机自启**不许提权、不许弹 UAC**。

        写的是 `HKCU\\...\\Run`，当前用户自己的注册表分支 —— 不需要管理员。
        而且这里有个**连锁伤害**：一旦提权注册成"以管理员身份启动"，
        服务以后就是管理员身份跑，它拉起来的 Edge / Chrome 也是管理员，
        而浏览器拒绝以管理员运行 —— 「自动抓华为会话」从此彻底不可用。
        """
        ns = _load()
        ns["os"] = types.SimpleNamespace(name="nt")
        ns["is_admin"] = lambda: False
        seen = {}
        ns["relaunch_as_admin"] = lambda cmd: seen.setdefault("cmd", cmd) or True
        self._fake_autostart()
        called = {"install": 0}
        ns["do_autostart"] = lambda elevated=False, result_file="": \
            called.__setitem__("install", called["install"] + 1) or 0

        old = sys.stdin
        sys.stdin = TTYInput("y\n")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ns["ask_autostart"]()
        finally:
            sys.stdin = old

        self.assertNotIn("cmd", seen, "注册开机自启不该提权")
        self.assertEqual(called["install"], 1, "应该就地用普通权限注册")
        self.assertIn("不需要管理员", buf.getvalue(), "要明说不弹 UAC")
        self.assertNotIn("UAC 窗口", buf.getvalue())

    def test_installed_but_elevated_is_offered_a_way_back(self):
        """已经注册成"以管理员身份"的机器 → 要**提示它会弄坏抓会话**，并问要不要改回来。

        ⚠ 这是升级路径上必然遇到的情况：老版本装出来的就是管理员模式，
        不提示的话用户永远不知道抓会话为什么坏。
        """
        ns = _load()
        self._fake_autostart(installed=True, elevated=True)
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = TTYInput("y\n")
        try:
            with contextlib.redirect_stdout(buf):
                ns["ask_autostart"]()
        finally:
            sys.stdin = old
        out = buf.getvalue()
        self.assertIn("以管理员身份", out)
        self.assertIn("自动抓华为会话", out, "要说清楚代价是什么")
        self.assertIn("普通权限", out, "要给出改回来的方向")

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
        ns["do_autostart"] = lambda elevated=False, result_file="": \
            called.__setitem__("n", called["n"] + 1) or 0
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
        ns["do_autostart"] = lambda elevated=False, result_file="": 0
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

    def test_already_installed_normal_is_left_alone(self):
        """已经装好了（普通权限）→ 直接跳过，别瞎折腾。

        ⚠ 以前这里做的是**反过来的事**：已设成普通权限的会被"顺手升级"成
        管理员模式。现在管理员模式会让抓会话不可用，所以那条逻辑整个删了。
        """
        ns = _load()
        self._fake_autostart(installed=True, elevated=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns["ask_autostart"]()
        out = buf.getvalue()
        self.assertIn("已经设过", out)
        self.assertIn("普通权限", out, "要说清现在是普通权限（一切正常）")


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

    def test_min_version_is_3_8(self):
        """底线是 **3.8** —— 不是"随便多旧都行"，也不是 3.9。

        为什么必须是 3.8：**Windows 7 只能装到 3.8.10**。3.9 起 CPython 用了
        `api-ms-win-core-path-l1-1.0.dll`，Win7 上没有这个 DLL，安装包直接起不来
        （bugs.python.org/issue40740）。门店还有 Win7 老电脑，卡在 3.9 就等于
        把那台机器判死刑。

        为什么要有一条测试钉这个数：门槛降下来之后很容易被"顺手"提回去
        （"都 2026 年了谁还用 3.8"）—— 一提回去 Win7 那台就静默失效。
        """
        ns = _load()
        self.assertEqual(tuple(ns["MIN_PYTHON"]), (3, 8))

    def test_too_old_is_rejected_with_a_next_step(self):
        ns = _load()
        # sys.version_info 是真 namedtuple（能切片、能取属性）—— 假的也要像
        vi = collections.namedtuple("version_info", "major minor micro")(3, 7, 9)
        ns["sys"] = types.SimpleNamespace(version_info=vi, executable="python")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ns["check_python"]()
        out = buf.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("3.7.9", out, "要说出用户现在是几")
        self.assertIn("3.8", out, "要说出需要几")
        self.assertIn("PATH", out, "要给出能照着做的下一步")

    def test_win7_gets_the_only_python_that_works_there(self):
        """Win7 上**只有 3.8.10 一个选择**，不能叫人家去装最新版。

        ⚠ 这是踩过的：原来一律叫人装 3.14，而 Win7 上 3.9 以上根本装不上 ——
        门店照着做会卡在安装包报错上，然后就没有下文了。
        """
        ns = _load()
        ns["is_win7"] = lambda: True
        out = ns["needs_python_help"]()
        self.assertIn("3.8.10", out, "Win7 必须点名 3.8.10")
        self.assertIn("python-3810", out, "要给 3.8.10 的下载页")
        self.assertNotIn("3.14", out, "Win7 上装不了 3.14，别给错地址")

    def test_non_win7_gets_the_latest(self):
        ns = _load()
        ns["is_win7"] = lambda: False
        out = ns["needs_python_help"]()
        self.assertIn("3.9", out)
        self.assertNotIn("python-3810", out, "别的系统不用去下 3.8.10")

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


class TestEnsureLayout(unittest.TestCase):
    """运行时目录**不进包**，由 `ensure_layout()` 按需生成。

    ⚠ 为什么不进包：`.secrets/`（云商账号）、`out/`（历史报告）、
    `config/store-*.yaml`（门店配置）都是**这台电脑自己的东西**。
    以前包里带着空模板（甚至某家店的真实配置），后果是**手工把新包拷到已有安装上
    会把门店的设置冲掉** —— 每次拷贝都得记着「跳过这三个目录」，迟早出错。
    现在包里一个都不带，整个目录直接覆盖就是安全的。

    ⚠ 而生成时必须**只补缺的**：`erp.env` 里是账号密码，盖掉等于把门店账号抹了。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        # 模板是生成门店配置的来源，必须放进去
        (self.root / "src").mkdir()
        (self.root / "src" / "store-config.default.yaml").write_text(
            (pathlib.Path(__file__).resolve().parent.parent
             / "src" / "store-config.default.yaml").read_text(encoding="utf-8"),
            encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def _ns(self):
        ns = _load()
        ns["ROOT"] = self.root
        # `_load()` 默认把它换成了空操作（见那里的说明）—— 这个类要测真的
        ns["ensure_layout"] = ns["_real_ensure_layout"]
        return ns

    def test_creates_the_three(self):
        self._ns()["ensure_layout"]()
        self.assertTrue((self.root / ".secrets").is_dir())
        self.assertTrue((self.root / "out").is_dir())
        self.assertTrue((self.root / ".secrets" / "erp.env").is_file())
        self.assertTrue((self.root / ".secrets" / "README.txt").is_file())
        self.assertTrue((self.root / "out" / "README.txt").is_file())
        self.assertTrue((self.root / "config" / "store-SCN231409.yaml").is_file())

    def test_never_overwrites_credentials(self):
        """⚠ 这是最要命的一条：`erp.env` 里是云商账号密码。"""
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / "erp.env").write_text("ERP_PASSWORD=真密码\n",
                                                         encoding="utf-8")
        self._ns()["ensure_layout"]()
        self.assertIn("真密码", (self.root / ".secrets" / "erp.env").read_text(
            encoding="utf-8"))

    def test_never_overwrites_the_store_config(self):
        (self.root / "config").mkdir()
        (self.root / "config" / "store-SCN231409.yaml").write_text(
            "store_code: 我的店\n", encoding="utf-8")
        self._ns()["ensure_layout"]()
        self.assertIn("我的店", (self.root / "config" / "store-SCN231409.yaml")
                      .read_text(encoding="utf-8"))

    def test_renamed_config_counts_as_present(self):
        """门店可能把配置改名成自己店的编码（界面上能改）——

        这时**不该**再补一个默认文件：多出一份没用的配置，还容易看错。
        """
        (self.root / "config").mkdir()
        (self.root / "config" / "store-OTHER001.yaml").write_text(
            "store_code: OTHER001\n", encoding="utf-8")
        self._ns()["ensure_layout"]()
        self.assertFalse((self.root / "config" / "store-SCN231409.yaml").exists(),
                         "已有门店配置就不该再补默认的")

    def test_missing_template_does_not_crash(self):
        """模板没了也只是打一行 —— 这是启动路径，绝不能抛。"""
        ((self.root / "src") / "store-config.default.yaml").unlink()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._ns()["ensure_layout"]()
        self.assertIn("门店配置要自己建", buf.getvalue(),
                      "模板没了要打一行说明，而且不能抛")

    def test_uninstall_does_not_recreate_dirs(self):
        """⚠ 卸载**不能**补目录 —— 用户正要删东西，我们却又建回来，很荒唐。"""
        ns = self._ns()
        ns["ensure_layout"] = lambda: done.append(1)
        done = []
        ns["do_uninstall"] = lambda yes=False, purge=False: 0
        self._call(ns, ["uninstall", "--yes"])
        self.assertEqual(done, [], "卸载路径不许补运行时目录")

    def _call(self, ns, argv):
        old = sys.argv
        sys.argv = ["bootstrap.py", *argv]
        try:
            return ns["main"]()
        finally:
            sys.argv = old


class TestConfigTemplate(unittest.TestCase):
    """发出去的门店配置模板 —— 它决定新装的机器**开箱是什么状态**。"""

    TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "src" / "store-config.default.yaml"

    def _cfg(self):
        import yaml
        return yaml.safe_load(self.TEMPLATE.read_text(encoding="utf-8"))

    def test_three_fields_are_empty(self):
        """⚠ **三行必须是空的。**

        以前发的是**某一家店的真实配置**，别的店装上忘了改就会拿别家账来对，
        而且看起来一切正常、不报错 —— 这是这个项目最怕的失败模式。
        """
        cfg = self._cfg()
        for k in ("store_code", "marker", "erp_store_name"):
            self.assertIn(k, cfg)
            self.assertEqual(cfg[k], "", f"模板里的 {k} 必须是空的")

    def test_no_hardcoded_session_filename(self):
        """会话文件名让代码按 store_code 推 —— 写死的话改了门店还是旧文件名。"""
        self.assertNotIn("file", self._cfg().get("session") or {})

    def test_has_every_key_the_app_edits(self):
        """模板里必须有界面能编辑的**每一个**键，否则新机器一打开就是空的。"""
        from src import config_io
        cfg = self._cfg()
        missing = []
        for key in config_io.EDITABLE:
            node = cfg
            for part in key.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                missing.append(key)
        self.assertEqual(missing, [], f"模板缺这些键：{missing}")

    def test_check_defaults_are_zero(self):
        cfg = self._cfg()
        self.assertEqual(cfg["check"]["lookback_days"], 0)
        self.assertEqual(cfg["check"]["report_lookahead_days"], 0)


class TestLeftoverTaskHelp(unittest.TestCase):
    """⚠ 旧提权任务删不掉时，提示必须**说到不可能被忽略**，而且要能照着做。

    实测就是这么坑了一轮：那条任务留着 → 每次登录还是以管理员拉起服务 →
    「自动抓华为会话」一直坏着、界面却写"普通权限"、连定时任务也建不了。
    三条症状一个原因，而原来的提示只混在成功消息里一句带过，用户没当回事。
    """

    def test_task_name_matches_the_real_one(self):
        """提示里拼进删除命令的那个名字，必须和 `autostart` 里的**同一个**。

        硬写两处迟早对不上 —— 改了一处，另一处就成了"删一个不存在的任务"，
        而用户会以为已经清干净了。
        """
        from src import autostart
        ns = _load()
        self.assertEqual(ns["AUTOSTART_TASK_NAME"], autostart.AUTOSTART_TASK)

    def test_help_contains_a_pasteable_command(self):
        ns = _load()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns["print_leftover_task_help"]()
        out = buf.getvalue()
        self.assertIn("schtasks /delete /tn", out, "要给能直接粘的命令")
        self.assertIn(ns["AUTOSTART_TASK_NAME"], out)
        self.assertIn("以管理员身份运行", out, "要说清得用管理员命令行")
        self.assertIn("start.bat", out, "要说清改完怎么重启")


class TestScheduleInstallSubcommand(unittest.TestCase):
    """`schedule-install` 存在的唯一理由：**能提权重跑一次**。

    那条定时任务以前可能是管理员身份的服务建的，普通权限覆盖不了
    （`schtasks ... /f` 拒绝访问）。所以界面失败时用它弹一次 UAC 重试。
    """

    def setUp(self):
        self.ns = _load()
        import src as src_pkg
        self.src_pkg = src_pkg
        self.orig = getattr(src_pkg, "schedule", None)

    def tearDown(self):
        # ⚠ 两边都要还原：`from src import schedule` 走的是**包属性**，
        #   光清 sys.modules 不够 —— 别的测试会拿到我这个假模块。
        if self.orig is not None:
            self.src_pkg.schedule = self.orig
        elif hasattr(self.src_pkg, "schedule"):
            del self.src_pkg.schedule
        sys.modules.pop("src.schedule", None)

    def _fake(self, result):
        """⚠ 两边都要塞：`from src import schedule` 走的是**包属性**，
        不只是 sys.modules —— 只塞 sys.modules 的话假模块根本不会被用到，
        测试会跑到真的 `schtasks` 上去（这里真踩了一次，而且是
        "单跑绿、全量跑红"那种最难查的样子）。"""
        import types as _t
        fake = _t.ModuleType("src.schedule")
        fake.DEFAULT_TIME = "21:00"
        fake.DEFAULT_DAYS_AGO = 1

        def _install(root, time_str, days_ago, config, name=None):
            self.seen = dict(time=time_str, days=days_ago, config=config, name=name)
            return result

        fake.install = _install
        sys.modules["src.schedule"] = fake
        self.src_pkg.schedule = fake

    def test_is_stdlib_only(self):
        """它可能在依赖没装好的状态下被提权调起 —— 不能先去 import 第三方包。"""
        self.assertIn("schedule-install", self.ns["STDLIB_ONLY"])

    def test_parses_options(self):
        self._fake({"ok": True, "message": "已注册"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.ns["do_schedule_install"](
                ["schedule-install", "--time", "20:30", "--days-ago", "2",
                 "--config", "config/store-X.yaml", "--name", "我的任务"])
        self.assertEqual(code, 0)
        self.assertEqual(self.seen["time"], "20:30")
        self.assertEqual(self.seen["days"], 2)
        self.assertEqual(self.seen["config"], "config/store-X.yaml")
        self.assertEqual(self.seen["name"], "我的任务")

    def test_defaults_when_no_options_given(self):
        self._fake({"ok": True, "message": "已注册"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["do_schedule_install"](["schedule-install"])
        self.assertEqual(self.seen["days"], 1)

    def test_failure_prints_the_manual_command(self):
        """失败要给一条能拿管理员权限直接粘的命令当退路。"""
        self._fake({"ok": False, "message": "拒绝访问",
                    "manual": "schtasks /create ..."})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.ns["do_schedule_install"](["schedule-install"])
        self.assertEqual(code, 2)
        self.assertIn("schtasks /create", buf.getvalue())


class TestElevateArgvReachesBootstrap(unittest.TestCase):
    """⚠ **两边的接口**：`/api/elevate` 拼出来的命令行，得被 bootstrap 正确读走。

    提权是"另起一个进程"，参数**只走命令行** —— 两边靠字符串约定，
    而这个约定**没有任何类型检查兜着**：一边写 `--days-ago`、另一边读 `--days`，
    各自单测都是绿的，合起来就是**静默用默认值**（用户改了时间、注册出来还是 21:00）。
    所以这里让 web.py **真的拼一遍**，再把拼出来的 argv 喂给 bootstrap 读。
    """

    def test_flags_round_trip_through_the_real_command_line(self):
        import sys as _sys
        _sys.path.insert(0, str(ROOT))
        from tests.test_web import _Server              # noqa: E402
        from src import web as _web                     # noqa: E402
        from unittest import mock as _mock              # noqa: E402
        import tempfile as _tf

        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srv = _Server(pathlib.Path(tmp.name))
        self.addCleanup(srv.close)

        seen = {}

        def runner(script, args, timeout=90):
            seen["args"] = list(args)
            return {"ok": True, "message": "已注册"}

        with _mock.patch.object(_web.elevate, "run_elevated", runner), \
                _mock.patch.object(_web.elevate, "is_admin", lambda: False), \
                _mock.patch.object(_web.schedule, "status",
                                   lambda root: {"installed": False, "tasks": []}):
            srv.request("POST", "/api/elevate",
                        {"what": "schedule", "time": "20:30", "days_ago": 3,
                         "name": "CBG报量对账-20点30"})

        argv = seen["args"]
        self.assertEqual(argv[0], "schedule-install")

        ns = _load()
        fake = types.ModuleType("src.schedule")
        fake.DEFAULT_TIME = "21:00"
        fake.DEFAULT_DAYS_AGO = 1
        fake.install = lambda root, time_str, days_ago, config, name=None: (
            seen.update(got_time=time_str, got_days=days_ago, got_name=name)
            or {"ok": True, "message": "已注册"})
        sys.modules["src.schedule"] = fake
        self.addCleanup(lambda: sys.modules.pop("src.schedule", None))
        # ⚠ 只塞 sys.modules 不够 —— `from src import schedule` 走的是**包属性**，
        #   两个都要换（这个坑本项目踩过，见上面 `_fake` 的注释）
        import src as _srcpkg
        old_attr = getattr(_srcpkg, "schedule", None)
        _srcpkg.schedule = fake
        self.addCleanup(lambda: setattr(_srcpkg, "schedule", old_attr))

        with contextlib.redirect_stdout(io.StringIO()):
            code = ns["do_schedule_install"](argv)
        self.assertEqual(code, 0)
        # ⚠ 这三条才是重点：用户在界面上填的值，真的走到了注册那一步
        self.assertEqual(seen.get("got_time"), "20:30", "时间丢在命令行上了")
        self.assertEqual(seen.get("got_days"), 3, "天数丢在命令行上了")
        self.assertEqual(seen.get("got_name"), "CBG报量对账-20点30", "任务名丢了")


class TestResultFile(unittest.TestCase):
    """提权子进程靠这个文件把结果回传给父进程 —— 写不成**不能反过来报错**。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_writes_json(self):
        ns = _load()
        p = pathlib.Path(self.dir.name) / "r.json"
        ns["_write_result"](str(p), {"ok": True, "message": "好了"})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["message"], "好了")

    def test_empty_path_is_a_noop(self):
        ns = _load()
        ns["_write_result"]("", {"ok": True})      # 不该抛

    def test_bad_path_does_not_raise(self):
        """写不进去最多是"界面看不到结果"，不该让**已经做成的操作**报错。"""
        ns = _load()
        ns["_write_result"]("/definitely/not/a/dir/x.json", {"ok": True})

    def test_main_strips_result_file_from_argv(self):
        """⚠ `--result-file <路径>` 必须像 `--pause` 一样先摘掉。

        摘漏了的话它会被当成子命令参数传下去 —— 路径一长就出各种怪问题，
        而真正的原因是"路由把参数吃错了"。
        """
        ns = _load()
        seen = {}
        ns["do_autostart"] = lambda elevated=False, result_file="": \
            seen.update(elevated=elevated, result_file=result_file) or 0
        self._call(ns, ["autostart", "--result-file", "/tmp/x.json"])
        self.assertEqual(seen.get("result_file"), "/tmp/x.json")

    def _call(self, ns, argv):
        old = sys.argv
        sys.argv = ["bootstrap.py", *argv]
        try:
            return ns["main"]()
        finally:
            sys.argv = old


class TestServicePanelHasNoPrivilegeChoice(unittest.TestCase):
    """后台服务面板**不许再出现「启动方式」下拉**。

    ⚠ 那个下拉是个**死选项**：选「以管理员身份」只是让当前这个（普通权限的）
    进程去执行 `schtasks /create ... /rl HIGHEST`，**必然失败**，
    而且**永远不会弹 UAC** —— 那条路上根本没有提权代码。
    实测就是这么被卡住的：用户选了、保存了、什么都没发生，
    然后开始怀疑"是不是没管理员权限所以不行"。

    而且**就算成功了也是坏的**：服务以管理员跑 → 它拉起的 Edge / Chrome 也是
    管理员 → 浏览器拒绝以管理员运行 →「自动抓会话」必坏。

    真需要管理员的两件事（删旧的提权任务、建定时任务）走**按需提权**，
    只在那一下弹一次 UAC（`src/elevate.py`）。
    """

    def test_no_privilege_dropdown(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="svc-mode"', html,
                         "别把「启动方式」下拉加回来 —— 它永远弹不出 UAC")

    def test_frontend_never_asks_for_elevated(self):
        """前端**永远不许**带 `elevated` 去注册开机自启。

        ⚠ 用词边界匹配，别用 `assertNotIn("elevated")` —— 接口返回里有个字段叫
        `self_elevated`（读它是**对的**，那是显示服务当前权限用的），
        子串断言会把它一起判违规，然后你得去解释为什么"读"和"写"不一样。
        `\belevated\b` 不会匹配 `self_elevated`（`_` 算单词字符）。
        """
        import re
        code = _js_code()
        self.assertNotIn("svc-mode", code)
        self.assertIsNone(re.search(r"\belevated\b", code),
                          "前端不该再往 /api/autostart 传 elevated")
        self.assertIn("self_elevated", code, "但**读**它是对的，别一起删了")

    def test_save_only_sends_enabled(self):
        """保存开机自启时**只发 enabled** —— 让后端用它的默认（普通权限）。"""
        self.assertIn("body: { enabled }", _js_code())

    def test_app_js_does_not_claim_capture_still_works(self):
        """⚠ 那句「自动抓会话不受影响」是**错的**，别再写回去。

        真踩过：界面上写着"不受影响"，用户就照着这句话排除了权限这条线，
        去试 profile、试沙箱，白折腾一轮。实际是**必坏**。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        # ⚠ 先把 `//` 行注释剥掉再查 —— 不然**解释这条规则的注释本身**会被判违规
        #   （真踩了：这条测试第一次跑就红在"别再说 X"那行注释上）。
        code = "\n".join(ln.split("//", 1)[0] for ln in js.splitlines())
        self.assertNotIn("自动抓会话不受影响", code)
        self.assertNotIn("自动抓会话可能不稳定", code, "别用「可能」淡化它")

    def test_repair_button_still_exists_for_legacy_machines(self):
        """老机器上可能还留着提权任务 —— 那个「以管理员身份修复」按钮要留着。"""
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="btn-svc-repair"', html)
        self.assertIn("/api/elevate", js)


class TestInstallRecordsTheInterpreter(unittest.TestCase):
    """安装成功时要把"这次用的解释器"记下来（`.secrets/python.txt`）。

    少了这一步，几个 `.bat` 下次双击还是去 PATH 上碰运气 ——
    而 PATH 上排第一的那个未必是装过依赖的那个。
    """

    def _ns(self, missing=(), returncode=0):
        ns = _load()
        ns["missing"] = lambda: list(missing)
        ns["write_build_stamp"] = lambda: None
        ns["subprocess"] = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(returncode=returncode))
        return ns

    def _run(self, ns):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return ns["do_install"](), buf.getvalue()

    def test_success_records_it(self):
        ns = self._ns()
        called = []
        ns["record_runtime"] = lambda: called.append(1)
        code, _ = self._run(ns)
        self.assertEqual(code, 0)
        self.assertEqual(called, [1], "装成功了就要把解释器记下来")

    def test_pip_failure_does_not_record(self):
        """装失败就别记 —— 记了等于把一个"装不上依赖的 Python"钉成以后要用的那个，
        而且从"能退回探测"变成"每次都拉错的那个"。"""
        ns = self._ns(returncode=1)
        called = []
        ns["record_runtime"] = lambda: called.append(1)
        code, _ = self._run(ns)
        self.assertEqual(code, 2)
        self.assertEqual(called, [], "装失败了不该记")

    def test_still_missing_after_install_does_not_record(self):
        ns = self._ns(missing=("requests",))
        called = []
        ns["record_runtime"] = lambda: called.append(1)
        code, _ = self._run(ns)
        self.assertEqual(code, 2)
        self.assertEqual(called, [],
                         "装完还找不到依赖 = 装到别的环境去了，这时候记下来只会更乱")


class TestNoForwardIncompatibleSyntax(unittest.TestCase):
    """别写"现在只是警告、以后会变成错误"的语法。

    Python 3.14 把非法转义序列从 DeprecationWarning 升级成了 **SyntaxWarning**，
    并明说"Such sequences will not work in the future"。等到真变 SyntaxError，
    门店电脑上双击 bat 会直接起不来。
    """

    def test_no_invalid_escape_sequences(self):
        import warnings
        bad = []
        for p in _scan_files():
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
        for p in _scan_files():
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

    def test_bats_prefer_the_recorded_interpreter(self):
        """安装时记下的解释器优先 —— 一台电脑上有两个 Python 时的保命绳。

        Win7 老机器只能装 3.8.10、新机器装 3.14、有的机器还有 Anaconda：
        依赖是装进**某一个**的，而 bat 每次是重新去 PATH 上找的。
        找到另一个就报 `No module named 'requests'`，看着像"当初没装成功"。
        """
        for name in ("install", "start", "stop", "selftest", "uninstall"):
            with self.subTest(bat=name):
                src = (ROOT / f"{name}.bat").read_text(encoding="ascii")
                self.assertIn(".secrets\\python.txt", src)
                self.assertIn("set /p PYBIN=", src, "要用 set /p 读那一行")
                self.assertIn("goto :probepython", src, "没记录/记录坏了要能退回去")
                self.assertIn(":probepython", src)
                self.assertIn(":havepython", src)

    def test_bat_pin_read_is_guarded_by_if_exist(self):
        """⚠ 读记录**必须**包在 `if exist` 里。

        第一次安装时还没有这个文件，而那是**最不能出错**的一步：
        门店新机器上双击的就是 install.bat。`set /p` 指向不存在的文件会报错。
        """
        for name in ("install", "start", "stop", "selftest", "uninstall"):
            with self.subTest(bat=name):
                src = (ROOT / f"{name}.bat").read_text(encoding="ascii")
                self.assertIn('if exist ".secrets\\python.txt" set /p PYBIN=', src)

    def test_bats_do_not_name_a_specific_python_version(self):
        """别再写死"装 3.14" —— **Win7 上 3.9 以上根本装不上**。

        原文是 `Install Python 3.14 from https://www.python.org/downloads/`，
        Win7 门店照着做会卡在安装包报错上，然后就没有下文了。
        （3.9 起 CPython 依赖 api-ms-win-core-path-l1-1.0.dll，Win7 没有。）
        """
        for b in sorted(ROOT.glob("*.bat")):
            with self.subTest(bat=b.name):
                src = b.read_text(encoding="ascii")
                self.assertNotIn("Python 3.14", src,
                                 f"{b.name} 里写死了 3.14 —— Win7 装不了它")
                self.assertNotIn("Python 3.9", src, f"{b.name} 里写死了版本号")

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
        fake.status = lambda root: {"installed": installed, "platform": "TestOS",
                                    "elevated": False}
        # ⚠ install 现在有第二个参数 `elevated`（默认 None=普通权限）——
        #   替身必须收下它，否则 `TypeError` 会被当成"注册失败"，测试假过/假挂
        def _install(root, elevated=None):
            self.called["root"] = root
            self.called["elevated"] = elevated
            return result or {"ok": True, "message": "已加入开机启动"}
        fake.install = _install
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
