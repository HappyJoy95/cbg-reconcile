"""安装时用的解释器记在哪、怎么读 —— 一台电脑上有两个 Python 时的保命绳。

**为什么必须有**：Win7 老机器只能装 3.8.10（3.9 以上不支持 Win7），新机器装 3.14，
有的机器上还躺着 Anaconda。依赖是装进**某一个** Python 的，而 .bat 每次是重新
去 PATH 上找的 —— 找到另一个就报 `No module named 'requests'`，
看着像"当初没装成功"，实际是"装到另一个 Python 去了"。
"""

import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from src import runtime


class TestRecordAndRead(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / ".secrets").mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def test_no_file_means_no_record(self):
        self.assertIsNone(runtime.pinned(self.root))
        self.assertEqual(runtime.pinned_version(self.root), "")

    def test_records_path_and_version(self):
        exe = self.root / "python.exe"
        exe.write_bytes(b"")
        p = runtime.record(self.root, exe=str(exe))
        self.assertTrue(p.exists())
        lines = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], f'"{exe}"', "路径要带引号存（bat 直接用）")
        self.assertRegex(lines[1], r"^\d+\.\d+\.\d+", "第二行是版本，给人看")

    def test_reads_back_without_quotes(self):
        exe = self.root / "python.exe"
        exe.write_bytes(b"")
        runtime.record(self.root, exe=str(exe))
        self.assertEqual(runtime.pinned(self.root), str(exe))

    def test_creates_the_secrets_dir(self):
        """`.secrets` 不存在也要能写 —— install 可能跑在任何时刻。"""
        exe = self.root / "python3"
        exe.write_bytes(b"")
        root2 = self.root / "fresh"
        root2.mkdir()
        self.assertIsNotNone(runtime.record(root2, exe=str(exe)))
        self.assertTrue((root2 / ".secrets" / "python.txt").exists())

    def test_path_with_spaces_is_quoted(self):
        """Windows 上 Python 常装在 `C:/Program Files/...` —— 必须带引号，
        否则 bat 里 `%PYBIN% bootstrap.py` 会在空格处断掉。"""
        d = self.root / "Program Files" / "Python38"
        d.mkdir(parents=True)
        exe = d / "python.exe"
        exe.write_bytes(b"")
        runtime.record(self.root, exe=str(exe))
        raw = (self.root / ".secrets" / "python.txt").read_text(encoding="utf-8")
        self.assertTrue(raw.startswith('"'), "带空格的路径一定要带引号")
        self.assertEqual(runtime.pinned(self.root), str(exe))

    def test_moved_python_is_ignored(self):
        """记录里的路径没了（Python 被卸载 / 挪走）→ 当作没记录，别硬用。

        硬用一个不存在的路径，表现是"双击 bat 闪一下就没了"，
        比退回 PATH 探测难查得多。
        """
        runtime.record(self.root, exe=str(self.root / "gone" / "python.exe"))
        self.assertIsNone(runtime.pinned(self.root))

    def test_stale_record_does_not_break_reading_the_version(self):
        runtime.record(self.root, exe=str(self.root / "gone" / "python.exe"))
        self.assertRegex(runtime.pinned_version(self.root), r"^\d+\.\d+\.\d+")

    def test_garbage_file_never_raises(self):
        """文件被写坏 / 是二进制 → 返回 None，**不能抛异常**。

        这个模块在启动路径上被调用：抛异常 = 双击 bat 直接闪退。
        """
        p = runtime.path_for(self.root)
        for junk in (b"\x00\xff\xfe\x01", b"", b"\n\n\n"):
            p.write_bytes(junk)
            self.assertIsNone(runtime.pinned(self.root))
            self.assertEqual(runtime.pinned_version(self.root), "")

    def test_directory_instead_of_file_is_rejected(self):
        """`Path.exists()` 对目录也为真 —— 所以校验必须用 `is_file()`。
        把一个目录当解释器传给 subprocess，报的错没人看得懂。"""
        d = self.root / "notpython"
        d.mkdir()
        runtime.record(self.root, exe=str(d))
        self.assertIsNone(runtime.pinned(self.root))


class TestEnvOverride(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(runtime.ENV_OVERRIDE, None)

    def test_env_wins(self):
        os.environ[runtime.ENV_OVERRIDE] = "/somewhere/else/python"
        self.assertEqual(runtime.pinned(), "/somewhere/else/python")

    def test_env_is_not_validated(self):
        """环境变量走"我说了算"：**故意不校验存在性**。

        排查时经常要指向一个还没建的路径试行为，卡在这儿反而碍事。
        """
        os.environ[runtime.ENV_OVERRIDE] = "/does/not/exist/python"
        self.assertEqual(runtime.pinned(), "/does/not/exist/python")

    def test_quoted_env_is_unquoted(self):
        os.environ[runtime.ENV_OVERRIDE] = '"C:/Program Files/Python38/python.exe"'
        self.assertEqual(runtime.pinned(), "C:/Program Files/Python38/python.exe")


class TestCurrent(unittest.TestCase):
    def test_falls_back_to_the_running_interpreter(self):
        """没有记录时用**当前进程自己** —— 第一次安装正是这个场景：
        双击 bat 探测出来的那个 Python，就是马上要装依赖的那个。"""
        with mock.patch.object(runtime, "pinned", lambda root=None: None):
            self.assertEqual(runtime.current(), runtime.sys.executable)

    def test_record_wins_over_running(self):
        with mock.patch.object(runtime, "pinned", lambda root=None: "/recorded/python"):
            self.assertEqual(runtime.current(), "/recorded/python")


class TestPythonw(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_prefers_pythonw(self):
        (self.root / "python.exe").write_bytes(b"")
        (self.root / "pythonw.exe").write_bytes(b"")
        self.assertTrue(
            runtime.pythonw_for(str(self.root / "python.exe")).endswith("pythonw.exe"))

    def test_falls_back_when_absent(self):
        """嵌入式发行版没有 pythonw.exe —— 原样返回，别造一个不存在的路径。"""
        (self.root / "python.exe").write_bytes(b"")
        exe = str(self.root / "python.exe")
        self.assertEqual(runtime.pythonw_for(exe), exe)

    def test_does_not_check_the_platform(self):
        """⚠ 平台判断留给调用方（autostart/schedule/cli 各有各的）。

        在这里判一次，等于同一个条件写两遍、还容易两边不一致。
        """
        (self.root / "python").write_bytes(b"")
        (self.root / "pythonw.exe").write_bytes(b"")
        self.assertTrue(
            runtime.pythonw_for(str(self.root / "python")).endswith("pythonw.exe"))


class TestSurvivesSelfUpdate(unittest.TestCase):
    def test_record_lives_under_secrets(self):
        """记录必须放在 `selfupdate` **永不覆盖**的目录里。

        放项目根 / src/ 里都会被"照仓库原样铺"的升级冲掉 ——
        而冲掉之后的表现就是"升个级，重启就起不来了"。
        """
        self.assertEqual(runtime.REL.parts[0], ".secrets")
        from src import selfupdate
        self.assertIn(".secrets", selfupdate.NEVER_TOUCH)


class TestCallersUseTheRecord(unittest.TestCase):
    """三个"拉起进程"的地方都得走 runtime —— 漏一个就还是会有沉默的失败。"""

    def test_autostart(self):
        from src import autostart
        with mock.patch.object(autostart.runtime, "current", lambda: "/x/python"):
            with mock.patch.object(autostart.platform, "system", lambda: "Linux"):
                self.assertEqual(autostart._python_exe(), "/x/python")

    def test_schedule(self):
        from src import schedule
        with mock.patch.object(schedule.runtime, "current", lambda: "/x/python"):
            self.assertEqual(schedule._python(), "/x/python")

    def test_cli_background(self):
        from src import cli
        with mock.patch.object(cli.runtime, "current", lambda: "/x/python"), \
                mock.patch.object(cli.platform, "system", lambda: "Linux"):
            self.assertEqual(cli._python_for_background(), "/x/python")

    def test_daily_task_uses_the_record(self):
        """计划任务必须用记下的解释器 —— 用错的表现是
        "任务计划程序说上次运行成功，而 out/ 里没有新报告"。"""
        from src import schedule
        with mock.patch.object(schedule.runtime, "current", lambda: "/x/pythonw.exe"), \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            self.assertEqual(schedule._python(), "/x/pythonw.exe")


class TestSameInstall(unittest.TestCase):
    """自检里"跟现在跑的不是同一个"这条警告，不能一上来就误报。

    ⚠ 记录里存的是 `python.exe`，而控制台/后台服务是 `pythonw.exe` 起来的 ——
    同一个 Python、两个 exe 名。比文件名的话，**每一台机器**都会显示这条警告，
    然后所有人学会无视它（真出问题时那条也就没用了）。

    ⚠ 两个平台的语义要**分别用 PureWindowsPath / PurePosixPath 钉**：
    在 POSIX 上跑 `Path(r"C:\\a\\b")` 时反斜杠**不是**分隔符，整串被当成一个文件名，
    `parent` 永远是 `.` —— 两边一比就相等，Windows 那几条会**假通过**。
    （反过来的坑见 AGENTS.md 坑 1。）
    测试还得在 Windows 上也跑得过：`selftest` 会跑它们。
    """

    def test_windows_pythonexe_and_pythonwexe_are_the_same_install(self):
        with mock.patch.object(runtime, "Path", PureWindowsPath):
            self.assertTrue(runtime.same_install(
                r"C:\Python38\python.exe", r"C:\Python38\pythonw.exe"))

    def test_windows_different_installations(self):
        with mock.patch.object(runtime, "Path", PureWindowsPath):
            self.assertFalse(runtime.same_install(
                r"C:\Python38\python.exe", r"C:\Python314\python.exe"))

    def test_posix_same_dir(self):
        with mock.patch.object(runtime, "Path", PurePosixPath):
            self.assertTrue(runtime.same_install(
                "/usr/bin/python3", "/usr/bin/python3.9"))

    def test_posix_different_dir(self):
        with mock.patch.object(runtime, "Path", PurePosixPath):
            self.assertFalse(runtime.same_install(
                "/venv/a/bin/python", "/usr/bin/python3"))

    def test_empty_never_matches(self):
        """`sys.executable` 在嵌入式/异常环境下可能是空的 —— 别因此报"不一样"。"""
        self.assertFalse(runtime.same_install("", "/usr/bin/python3"))
        self.assertFalse(runtime.same_install("/usr/bin/python3", None))


class TestDescribe(unittest.TestCase):
    def test_reports_both_recorded_and_running(self):
        d = runtime.describe()
        for k in ("pinned", "python", "version", "running", "running_version", "file"):
            self.assertIn(k, d)
        self.assertRegex(d["running_version"], r"^\d+\.\d+\.\d+$")
