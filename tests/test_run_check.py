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


if __name__ == "__main__":
    unittest.main()
