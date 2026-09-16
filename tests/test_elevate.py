"""按需提权 —— **只把真正需要管理员的那一步**弹一次 UAC。

这两件事只有管理员能做，而且都是一次性的：
  1. 删掉老版本留下的"以管理员身份启动"计划任务（留着的话每次登录还是以管理员
     拉起服务 → 自动抓会话永远坏着，而界面写着"普通权限"）；
  2. 覆盖一条由管理员创建过的定时任务。

逼用户"右键 install.bat 以管理员身份运行"是错的 —— 那会把 pip install
也一起提权跑掉（标准用户 + 管理员密码的机器上，包会装进**另一个账号**）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import elevate


class TestIsAdmin(unittest.TestCase):
    def test_non_windows_is_never_admin(self):
        """非 Windows 恒为 False —— 那边没有"提权"这回事，别拿它拦事情。"""
        if os.name == "nt":
            self.skipTest("这条只在非 Windows 上有意义")
        self.assertFalse(elevate.is_admin())

    def test_not_fooled_by_a_global_platform_patch(self):
        """⚠ 判断系统不能靠 `platform.system()`。"""
        with mock.patch("platform.system", lambda: "Windows"):
            if os.name != "nt":
                self.assertFalse(elevate.is_admin(), "被 platform patch 带跑了")

    def test_is_windows_is_patchable_on_its_own(self):
        """⚠ 抽成函数就是为了**能 patch 它**，而不是去 patch 全局的 `os.name`。

        `mock.patch.object(os, "name", "nt")` 会让 `ctypes/__init__.py`
        也以为自己在 Windows 上 → 在 macOS 上 `import ctypes` 直接 ImportError
        （真踩了，五条测试一起崩）。
        """
        with mock.patch.object(elevate, "is_windows", lambda: True):
            self.assertTrue(elevate.is_windows())


class TestConsolePython(unittest.TestCase):
    """提权要用**带控制台**的 python.exe —— 否则用户点完「是」什么都看不到。"""

    def test_prefers_python_exe_next_to_the_running_interpreter(self):
        d = tempfile.mkdtemp()
        (Path(d) / "python.exe").write_bytes(b"")
        with mock.patch.object(elevate.sys, "executable", str(Path(d) / "pythonw.exe")), \
                mock.patch.object(elevate, "is_windows", lambda: True):
            self.assertTrue(elevate.console_python().endswith("python.exe"))
            self.assertFalse(elevate.console_python().endswith("pythonw.exe"))

    def test_falls_back_to_the_running_interpreter(self):
        """嵌入式发行版没有 python.exe —— 原样返回，别造一个不存在的路径。"""
        d = tempfile.mkdtemp()
        exe = str(Path(d) / "pythonw.exe")
        with mock.patch.object(elevate.sys, "executable", exe), \
                mock.patch.object(elevate, "is_windows", lambda: True):
            self.assertEqual(elevate.console_python(), exe)


class TestReadJson(unittest.TestCase):
    """结果文件是**另一个进程**在写 —— 一定会撞上"读到一半"。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "r.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file(self):
        self.assertIsNone(elevate._read_json(str(self.path)))

    def test_empty_file_is_not_a_result(self):
        """⚠ 子进程是"先创建再写"的，中间有一瞬间是**空文件**。

        把空文件当成"结果"的话，会偶发地丢掉一次成功（父进程读到空就返回了）。
        """
        self.path.write_text("", encoding="utf-8")
        self.assertIsNone(elevate._read_json(str(self.path)))

    def test_half_written_json(self):
        self.path.write_text('{"ok": true, "mess', encoding="utf-8")
        self.assertIsNone(elevate._read_json(str(self.path)))

    def test_valid(self):
        self.path.write_text(json.dumps({"ok": True, "message": "好了"}),
                             encoding="utf-8")
        self.assertEqual(elevate._read_json(str(self.path))["message"], "好了")

    def test_non_dict_is_rejected(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(elevate._read_json(str(self.path)))


class TestRunElevated(unittest.TestCase):
    def test_non_windows_returns_none(self):
        if os.name == "nt":
            self.skipTest("这条只在非 Windows 上有意义")
        self.assertIsNone(elevate.run_elevated(Path("bootstrap.py"), ["autostart"]))

    def test_denied_uac_returns_none_instead_of_raising(self):
        """⚠ 用户点「否」**不能抛异常** —— 这条路是在 HTTP 请求里走的，
        抛出去界面上就是一个 500，而真实情况只是"他没点「是」"。
        """
        seen = {}

        class _Shell:
            @staticmethod
            def ShellExecuteW(*a):
                seen["params"] = a[3]
                return 5                      # 5 = 拒绝访问（约定的"失败"）
        fake = type("W", (), {"shell32": _Shell})
        with mock.patch.object(elevate, "is_windows", lambda: True), \
                mock.patch("ctypes.windll", fake, create=True):
            self.assertIsNone(elevate.run_elevated(Path("bootstrap.py"), ["autostart"]))

    def test_params_carry_the_subcommand_and_a_result_file(self):
        """约定必须对上：子命令 + `--result-file`（不然父进程永远等不到结果）。"""
        seen = {}

        class _Shell:
            @staticmethod
            def ShellExecuteW(*a):
                seen["params"] = a[3]
                return 5
        fake = type("W", (), {"shell32": _Shell})
        with mock.patch.object(elevate, "is_windows", lambda: True), \
                mock.patch("ctypes.windll", fake, create=True):
            elevate.run_elevated(Path("bootstrap.py"), ["autostart"])
        self.assertIn("autostart", seen["params"])
        self.assertIn("--result-file", seen["params"])

    def test_pause_is_forwarded(self):
        """提权窗口是一闪而过的 —— `--pause` 让它停住，用户才看得到结果。"""
        seen = {}

        class _Shell:
            @staticmethod
            def ShellExecuteW(*a):
                seen["params"] = a[3]
                return 5
        fake = type("W", (), {"shell32": _Shell})
        with mock.patch.object(elevate, "is_windows", lambda: True), \
                mock.patch("ctypes.windll", fake, create=True):
            elevate.run_elevated(Path("bootstrap.py"), ["autostart"], pause=True)
        self.assertIn("--pause", seen["params"])


class TestCleanup(unittest.TestCase):
    def test_missing_file_is_fine(self):
        elevate.cleanup_result_file("/definitely/not/here.json")   # 不该抛

    def test_removes_it(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "x.json"
        p.write_text("{}", encoding="utf-8")
        elevate.cleanup_result_file(p)
        self.assertFalse(p.exists())


class TestModuleStaysStdlibOnly(unittest.TestCase):
    def test_no_third_party_imports(self):
        """`bootstrap.py` 会在**装依赖之前**用到它，所以不能 import 第三方包。"""
        import ast
        src = (Path(elevate.__file__)).read_text(encoding="utf-8")
        tops = set()
        for node in ast.parse(src).body:
            if isinstance(node, ast.Import):
                tops |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
        self.assertFalse(tops & {"requests", "yaml", "openpyxl"},
                         f"src/elevate.py 顶层 import 了第三方包：{tops}")


class TestAlwaysAdminReason(unittest.TestCase):
    r"""⚠ **为什么这台电脑上每个程序都是管理员、而且不弹授权框。**

    实测踩过：一台门店电脑上，**任何**程序选「以管理员身份运行」都不弹 UAC，
    服务不管怎么启动都是管理员 → Edge 拒绝运行 → 抓会话永远失败。
    用户照着提示改了「启动方式」、还把 UAC 滑块拉到最高，**全都没用**。

    原因在 Windows 账户本身：用的是**内置 Administrator**（RID 500），
    它的"管理员批准模式"默认关着（`FilterAdministratorToken` 默认 0）——
    这个设置下它启动的每个进程都直接拿完整管理员令牌，**永远不弹 UAC**。
    **拉高滑块对它没有任何影响**（滑块管的是普通管理员账户）。

    所以程序必须**自己说出这件事**，并且别再说"改成普通权限"——
    那台机器上改了也没用。
    """

    def _reason(self, flags, env=None, windows=True):
        with mock.patch.object(elevate, "is_windows", lambda: windows), \
                mock.patch.object(elevate, "_uac_flags", lambda: flags), \
                mock.patch.dict(elevate.os.environ, env or {}, clear=True):
            return elevate.always_admin_reason()

    def test_message_is_plain_text_not_markdown(self):
        """⚠ **不能带 Markdown。**

        这段话是被前端 `esc()` 之后原样打进 `innerHTML` 的 —— `esc()` 只转义
        HTML，**不渲染 Markdown**。写成 `**内置 Administrator 账户**` 的话，
        用户看到的就是一坨带星号的原文，又长又难读（实测就是这么被看到的）。
        """
        msg = self._reason({"EnableLUA": 1, "FilterAdministratorToken": 0},
                           {"USERNAME": "Administrator"})
        self.assertNotIn("**", msg, "前端不渲染 Markdown，星号会原样显示")
        self.assertNotIn("`", msg, "反引号同理")

    def test_message_keeps_its_line_breaks(self):
        """换行要留着（前端用 `white-space: pre-line` 显示）——
        挤成一整段的话这段话没法读。"""
        msg = self._reason({"EnableLUA": 1, "FilterAdministratorToken": 0},
                           {"USERNAME": "Administrator"})
        self.assertGreaterEqual(len(msg.splitlines()), 6)

    def test_builtin_admin_is_explained(self):
        msg = self._reason({"EnableLUA": 1, "FilterAdministratorToken": 0},
                           {"USERNAME": "Administrator"})
        self.assertIn("内置 Administrator", msg)
        self.assertIn("永远", msg, "要说清是「永远不弹」，不是「偶尔不弹」")
        self.assertIn("滑块", msg, "⚠ 必须点破：拉高 UAC 滑块对它没用（用户真试过）")

    def test_builtin_admin_message_says_it_cannot_be_changed_here(self):
        msg = self._reason({"EnableLUA": 1, "FilterAdministratorToken": 0},
                           {"USERNAME": "Administrator"})
        self.assertIn("改", msg)
        self.assertIn("普通用户", msg, "解法②之外还要给最省事的那个：换个普通账户")
        self.assertIn("FilterAdministratorToken", msg, "解法①要给出能直接粘的注册表命令")

    def test_userprofile_also_counts(self):
        """账户名可能和环境变量不一致 —— 用户目录也是线索。"""
        msg = self._reason({"EnableLUA": 1, "FilterAdministratorToken": 0},
                           {"USERPROFILE": r"C:\Users\Administrator"})
        self.assertIn("内置 Administrator", msg)

    def test_filtered_builtin_admin_is_fine(self):
        """已经把内置 Administrator 也纳入 UAC 管了 → 没问题，别乱报。"""
        self.assertEqual(
            self._reason({"EnableLUA": 1, "FilterAdministratorToken": 1},
                         {"USERNAME": "Administrator"}), "")

    def test_normal_admin_account_is_not_flagged(self):
        """普通管理员账户（UAC 正常过滤）→ 不该报这个，那是另一回事。"""
        self.assertEqual(
            self._reason({"EnableLUA": 1, "FilterAdministratorToken": 0},
                         {"USERNAME": "shop-pc"}), "")

    def test_uac_off_entirely(self):
        msg = self._reason({"EnableLUA": 0}, {"USERNAME": "shop-pc"})
        self.assertIn("UAC 是关掉的", msg)
        self.assertIn("重启", msg, "改完 UAC 要重启，得说")

    def test_unreadable_registry_is_silent(self):
        """读不到注册表 → 空串，**不能猜**（猜错会把用户指去改没用的东西）。"""
        self.assertEqual(self._reason({}, {"USERNAME": "Administrator"}), "")

    def test_non_windows_is_silent(self):
        self.assertEqual(self._reason({}, {"USERNAME": "Administrator"},
                                      windows=False), "")
