"""`run_check.py` 的测试 —— 计划任务的兜底启动器。

**它存在的唯一理由是"不管出什么事都留得下证据"。**

门店实测踩到的坑：计划任务跑完，`out/run.log` 里**只有退出码、没有任何对账输出**
（用户报的是"退出码 120"）。原因是 `run.bat` 直接用 `pythonw.exe` 启动 ——
`pythonw` 是 GUI 子系统程序，**连 stderr 都没有**（`sys.stdout` / `sys.stderr` 是 `None`），
所以任何**启动阶段**的失败（缺依赖、路径不对、假 python）连 traceback 都留不下来。

所以这里**真的起子进程跑一遍**，而不是只查源码 —— 这一层要防的正是
"代码看着对、真跑起来什么都没有"。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "run_check.py"


def _run(args, **kw):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90, cwd=str(ROOT), **kw)


class TestLauncherAlwaysLeavesEvidence(unittest.TestCase):
    def setUp(self):
        self.log = ROOT / "out" / "run.log"
        self.before = self.log.read_text(encoding="utf-8") if self.log.exists() else None

    def tearDown(self):
        # 别把开发机上的日志搞乱
        if self.before is None:
            try:
                self.log.unlink()
            except OSError:
                pass
        else:
            self.log.write_text(self.before, encoding="utf-8")

    def _new_lines(self) -> str:
        now = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        if self.before is None:
            return now
        return now[len(self.before):]

    def test_import_failure_is_written_to_the_log(self):
        """**这就是这个文件存在的理由。**

        用 `-S`（跳过 site-packages）跑 —— Python 本身没问题，但任何第三方依赖
        都 import 不到，模拟门店电脑上"依赖没装 / python 是假的"。
        以前这种情况下日志**一个字都没有**。
        """
        r = subprocess.run([sys.executable, "-S", str(LAUNCHER),
                            "-c", "config/store-SCN231409.yaml",
                            "check", "--days-ago", "1"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90, cwd=str(ROOT))
        log = self._new_lines()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("开始", log, "连开始标记都没写 —— 启动器本身没跑起来")
        self.assertIn("Traceback", log,
                      "import 失败了但日志里没有 traceback —— 用户就只会看到一个退出码")
        self.assertIn("结束 exit=", log, "没有结束标记，界面会以为还在跑")

    def test_success_path_logs_start_and_finish(self):
        """正常路径：开始/结束标记都要有，而且退出码要对得上。"""
        r = _run(["-c", "config/no-such-store.yaml", "check", "--days-ago", "1"])
        log = self._new_lines()
        self.assertIn("开始", log)
        self.assertIn(f"结束 exit={r.returncode}", log,
                      f"日志里的退出码跟真实的不一致（真实 {r.returncode}）")

    def test_works_when_stdout_is_none(self):
        """`pythonw.exe` 下 `sys.stdout` / `sys.stderr` 是 **None**。

        直接 `sys.stdout.write` 会 AttributeError —— 那就又回到"什么都没有"了。
        """
        code = (
            "import sys, runpy;"
            "sys.stdout = None; sys.stderr = None;"
            "sys.argv = ['run_check.py', '-c', 'config/no-such-store.yaml',"
            "            'check', '--days-ago', '1'];"
            "runpy.run_path(r'" + str(LAUNCHER) + "', run_name='__main__')"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=90, cwd=str(ROOT))
        log = self._new_lines()
        self.assertIn("开始", log, "stdout 是 None 时启动器自己崩了")
        self.assertIn("结束 exit=", log)

    def test_log_is_flushed_line_by_line(self):
        """每行都要落盘 —— 进程被 kill / 卡死时缓冲区不能把证据带走。"""
        import re
        src = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("if s.endswith(\"\\n\")", src)
        self.assertIn("self.flush()", src)

    def test_reports_the_python_version(self):
        """日志第一行带 Python 版本 —— 门店电脑上装的是哪个一眼就知道。"""
        _run(["-c", "config/no-such-store.yaml", "check", "--days-ago", "1"])
        log = self._new_lines()
        self.assertRegex(log, r"python \d+\.\d+\.\d+")


class TestDiagnoseBat(unittest.TestCase):
    """`diagnose.bat` 是**唯一不需要 Python 就能跑**的排查工具。

    要查的恰恰可能是"Python 根本没装好"—— 那时候任何 Python 脚本都跑不起来。
    """

    def test_is_pure_ascii_and_crlf(self):
        raw = (ROOT / "diagnose.bat").read_bytes()
        bad = [b for b in raw if b > 127]
        self.assertFalse(bad, f"有 {len(bad)} 个非 ASCII 字节")
        self.assertIn(b"\r\n", raw)

    def test_covers_what_we_actually_need(self):
        src = (ROOT / "diagnose.bat").read_text(encoding="ascii")
        for needle, why in (
                ("where python", "python 到底在哪（还是要看是不是商店那个假的）"),
                ("run_check.py", "要真跑一次，把错误抓下来"),
                ("run.log", "日志内容"),
                ("schtasks", "计划任务注册成什么样"),
                ("deps OK", "依赖装了没"),
                ("diagnose-result.txt", "结果要落成文件，好发回来"),
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, src, f"诊断脚本没查：{why}")


class TestSystemExitLosesNothing(unittest.TestCase):
    """⚠ **预先存在的 bug，2026-09-16 才查出来。**

    `raise SystemExit("消息")` 的 `code` 是**字符串**，老写法
    `rc = e.code if isinstance(e.code, int) else 0` 把它变成 **0 = 成功**；
    而且因为异常被**接住**了，Python 也不会替我们去印那条消息 ——
    于是 `out/run.log` 里只剩一句 `结束 exit=0`。

    实测（配置路径写错）：**看着像跑成功了，其实什么都没干** ——
    这正是这个启动器要防的那类事故。
    """

    def setUp(self):
        self.log = ROOT / "out" / "run.log"
        self.before = self.log.read_text(encoding="utf-8") if self.log.exists() else None

    def tearDown(self):
        if self.before is None:
            try:
                self.log.unlink()
            except OSError:
                pass
        else:
            self.log.write_text(self.before, encoding="utf-8")

    def _new_lines(self) -> str:
        now = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return now if self.before is None else now[len(self.before):]

    def test_配置找不到时_消息要进日志且退出码非零(self):
        r = _run(["-c", "config/no-such-store.yaml", "check", "--days-ago", "1"])
        log = self._new_lines()
        self.assertNotEqual(r.returncode, 0, "找不到配置却报了成功")
        self.assertIn("找不到配置文件", log,
                      "SystemExit 的消息被吞了 —— 日志里就剩一个退出码，谁也查不出原因")
        self.assertIn(f"结束 exit={r.returncode}", log)

    def test_退出码语义和_python_自己那套一致(self):
        """`sys.exit("消息")` 不被接住时：消息进 stderr、退出码 **1**。

        我们接住了它，就得**自己把这两件事都补上**，不能只补一半。
        """
        import subprocess as sp
        ref = sp.run([sys.executable, "-c", 'raise SystemExit("boom")'],
                     capture_output=True, text=True)
        self.assertEqual(ref.returncode, 1)
        self.assertIn("boom", ref.stderr)


class TestRewriteLegacyCheck(unittest.TestCase):
    """⚠ **发版阻断的解法**：老 `run.bat` 里写的是 `check`，要改写成 `daily`。

    `run.bat` 是安装时生成的、不进版本库 → 自更新不重写它 →
    门店那份会一直写着 `check`。而 2.0.0 的 `check` **只从本地库读**，
    没人跑第 1 步（`dump`）库就永远是旧的 → **每天以「库不新鲜」失败**。
    `run_check.py` 在版本库里（自更新会覆盖），所以它是唯一能换掉命令的地方。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_rc_under_test", LAUNCHER)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        from src import cli
        cls.ap = cli.build_parser()

    def rw(self, argv):
        return self.mod.rewrite_legacy_check(list(argv), self.ap)

    def test_run_bat_里那条原样的命令会被改写(self):
        """这是门店 bat 里**逐字节**的样子（`schedule.write_runner_script` 生成的）。"""
        argv, changed = self.rw(["-c", "config/store-SCN231409.yaml",
                                 "check", "--days-ago", "1"])
        self.assertTrue(changed)
        self.assertEqual(argv, ["-c", "config/store-SCN231409.yaml",
                                "daily", "--days-ago", "1"])

    def test_不带任何选项也要能改(self):
        argv, changed = self.rw(["check"])
        self.assertTrue(changed)
        self.assertEqual(argv, ["daily"])

    def test_配置文件路径不能被当成子命令(self):
        """⚠ 扫描要**跳过 `-c` 的值** —— 否则会把路径当子命令，
        于是"什么都不改"，静默回到"每天失败"的老样子。"""
        argv, changed = self.rw(["-c", "check", "check"])
        self.assertTrue(changed)
        self.assertEqual(argv, ["-c", "check", "daily"])

    def test_等于号写法也要能跳过(self):
        argv, changed = self.rw(["--config=config/check.yaml", "check"])
        self.assertTrue(changed)
        self.assertEqual(argv, ["--config=config/check.yaml", "daily"])

    def test_已经是daily就不动(self):
        argv, changed = self.rw(["-c", "c.yaml", "daily", "--days-ago", "1"])
        self.assertFalse(changed)
        self.assertEqual(argv, ["-c", "c.yaml", "daily", "--days-ago", "1"])

    def test_别的子命令一律不动(self):
        for sub in ("auth", "ping", "selftest", "serve", "pos", "dump"):
            with self.subTest(sub=sub):
                argv, changed = self.rw(["-c", "c.yaml", sub])
                self.assertFalse(changed, "%s 不该被改写" % sub)

    def test_改写后daily必须真的认这些参数(self):
        """⚠ 光"改写了"不算数 —— 改写出来的东西得能被 `daily` 解析。

        这条就是 `daily ⊇ check` 那个包含关系在**迁移现场**的体现：
        少了任何一个参数，迁移那天就是 "unrecognized arguments"。
        """
        legacy = ["-c", "config/store-X.yaml", "check", "--days-ago", "1",
                  "--no-mail", "--no-push", "--log-file", "out/run.log"]
        argv, changed = self.rw(legacy)
        self.assertTrue(changed)
        # 把 `check` 的选项换成 `daily` 的选项表来解析 —— 不抛 SystemExit 就算过
        ns = self.ap.parse_args(argv)
        self.assertEqual(ns.cmd, "daily")
        self.assertEqual(ns.days_ago, 1)
        self.assertTrue(ns.no_mail)
        self.assertTrue(ns.no_push)
        self.assertEqual(ns.log_file, "out/run.log")

    def test_子命令前面的开关不会让扫描错位(self):
        """`-v` 这种不吃值的开关也得跳过，别把它后面的东西当子命令。"""
        argv, changed = self.rw(["-v", "-c", "c.yaml", "check", "--days-ago", "2"])
        self.assertTrue(changed)
        self.assertEqual(argv, ["-v", "-c", "c.yaml", "daily", "--days-ago", "2"])


if __name__ == "__main__":
    unittest.main()
