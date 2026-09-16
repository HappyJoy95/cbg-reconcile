"""定时执行的测试。

**故意不真去动系统的 crontab / schtasks** —— 开发机上装个每天 9 点的任务
是很讨厌的副作用。这里把 subprocess 换成假的，只验证
「命令怎么拼、脚本怎么写、失败怎么报」，那才是真正会出错的地方。
"""

import datetime
import pathlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src import cli, schedule, winutil


class TestRunnerScript(unittest.TestCase):
    """生成的 run 脚本要满足两条**互相拉扯**的要求：

    1. 计划任务跑的时候，输出必须落到 `out\\run.log`（不然跑完什么都查不到）；
    2. 人**手动双击**的时候，屏幕上必须看得到东西。

    老版本用 `>> out\\run.log 2>&1` 只满足了第 1 条 —— 结果门店同事双击 run.bat
    看到的是"黑窗口、一分钟、自己关掉"，反馈就是**"什么都没发生"**。
    现在改成 Python 侧分流（`--log-file`），两边都写。
    """

    def test_unix_script(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(schedule, "kind", lambda: "unix"):
                p = schedule.write_runner_script(Path(d), "config/store-X.yaml", days_ago=1)
            body = p.read_text(encoding="utf-8")
            self.assertEqual(p.name, "run.sh")
            self.assertTrue(body.startswith("#!/bin/sh"))
            self.assertIn('cd "$(dirname "$0")"', body)
            self.assertIn("check --days-ago 1", body)
            self.assertIn('--log-file "out/run.log"', body)
            self.assertTrue(p.stat().st_mode & 0o111, "run.sh 必须可执行")

    def test_windows_script(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(schedule, "kind", lambda: "windows"):
                p = schedule.write_runner_script(Path(d), "config/store-X.yaml", days_ago=1)
            raw = p.read_bytes()
            body = raw.decode("ascii")           # ← 顺便断言纯 ASCII
            self.assertEqual(p.name, "run.bat")
            self.assertIn("chcp 65001", body)
            self.assertIn('cd /d "%~dp0"', body)
            self.assertIn("\r\n", body, "批处理要 CRLF")
            # ⚠ 跑对账**不是**在 bat 里拼 `python -m src.cli …`，而是交给
            #   run_check.py —— 它先开日志再 import，启动阶段的失败才留得下 traceback
            #   （pythonw 连 stderr 都没有）。
            self.assertIn("run_check.py", body)
            self.assertIn("check --days-ago 1", body, "参数还是要原样传过去")
            # 退出码**不由 bat 透出**：它用 start 起进程后立刻退出，拿不到。
            # 真实的退出码由启动器写进日志（"结束 exit=N"那行）。
            self.assertNotIn("%ERRORLEVEL%", body)
            self.assertIn("exit /b 0", body)
            # 但要留一条痕迹，好区分"bat 没跑"和"python 没起来"
            self.assertIn("launching", body)

    def test_generated_bats_never_redirect_stdout(self):
        """⚠ **不能**在 bat 里写 `>> out\\run.log 2>&1`。

        那样屏幕上什么都没有 —— 手动双击的人完全判断不了跑没跑。
        日志要交给 Python 分流。
        """
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(schedule, "kind", lambda: "windows"):
                p = schedule.write_runner_script(Path(d), "config/store-X.yaml")
            body = p.read_bytes().decode("ascii")
        check_lines = [ln for ln in body.splitlines() if "check --days-ago" in ln]
        self.assertTrue(check_lines, "没找到跑对账那一行")
        for ln in check_lines:
            self.assertNotIn(">>", ln, f"跑对账的输出被重定向走了：{ln.strip()}")
            self.assertNotIn("2>&1", ln, f"跑对账的输出被重定向走了：{ln.strip()}")
            self.assertIn("run_check.py", ln, "日志要交给启动器分流")

    def test_generated_bat_is_pure_ascii(self):
        """生成的 bat 里一个中文都不能有 —— 它的编码受控制台代码页摆布。

        （写这个功能时真踩到了：注释里写了中文，非 ASCII 字节 60 个。）
        """
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(schedule, "kind", lambda: "windows"):
                schedule.write_runner_script(Path(d), "config/store-X.yaml")
            for name in ("run.bat", "run-now.bat"):
                raw = (Path(d) / name).read_bytes()
                bad = [b for b in raw if b > 127]
                self.assertFalse(bad, f"{name} 里有 {len(bad)} 个非 ASCII 字节")

    def test_scheduled_bat_does_not_wait_for_the_process(self):
        """⚠ 计划任务的 bat **不能**同步等 python.exe。

        服务是 pythonw 跑的（没控制台），`schtasks` 起 cmd 跑 bat 时 Windows 会开一个
        **cmd 黑窗**；同步等的话那个黑窗会挂满整个对账过程（一两分钟），
        用户反馈"影响效果"。用 `start` 起 pythonw（自身无控制台）→ cmd 立刻退出。
        """
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_pythonw", lambda: r"C:\py\pythonw.exe"):
            p = schedule.write_runner_script(Path(d), "config/store-X.yaml")
            body = p.read_bytes().decode("ascii")
        run_lines = [ln for ln in body.splitlines() if "check --days-ago" in ln]
        self.assertTrue(run_lines)
        self.assertTrue(run_lines[0].startswith("start "),
                        f"计划任务那行没用 start，黑窗会挂满整个过程：{run_lines[0]}")
        self.assertIn("pythonw", run_lines[0], "要用 pythonw（它没有控制台）")

    def test_manual_bat_runs_synchronously(self):
        """手动那个**必须**同步跑。

        否则 run-now 会在对账还没跑完时就 pause —— 等于废了
        （第一版就是 `call run.bat`，而 run.bat 已经不等进程了）。
        """
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_python", lambda: r"C:\py\python.exe"):
            schedule.write_runner_script(Path(d), "config/store-X.yaml")
            body = schedule.manual_script_path(Path(d)).read_bytes().decode("ascii")
        run_lines = [ln for ln in body.splitlines() if "check --days-ago" in ln]
        self.assertTrue(run_lines, "run-now.bat 里没有跑对账那行")
        self.assertFalse(run_lines[0].startswith("start "),
                         "手动跑不该用 start —— 那样跑完立刻 pause，看不到结果")
        self.assertIn("python.exe", run_lines[0])
        self.assertIn("pause", body)

    def test_python_writes_its_own_exit_marker(self):
        """退出码改由 **Python** 写进日志 —— 而且是**真写**，不是只写在注释里。

        bat 用 start 起进程之后拿不到退出码了，而界面靠日志里这行判断"跑完了没"。
        """
        import io as _io
        import contextlib
        d = Path(tempfile.mkdtemp())
        log = d / "run.log"
        args = types.SimpleNamespace(log_file=str(log))

        # 真正跑一遍 cmd_check 的分流逻辑（把对账本身换成假的）
        with mock.patch.object(cli, "_cmd_check_locked", lambda a: 3), \
                contextlib.redirect_stdout(_io.StringIO()):
            rc = cli.cmd_check(args)

        self.assertEqual(rc, 3, "退出码要原样返回")
        text = log.read_text(encoding="utf-8")
        self.assertIn("开始", text)
        self.assertIn("exit=3", text, "日志里没有退出码标记，界面就永远以为还在跑")
        self.assertTrue(text.rstrip().endswith("==="), "结束标记要在最后")

    def test_no_log_file_means_no_markers(self):
        """不带 --log-file 时（界面点「运行」走的那条）不该多打标记。"""
        import io as _io
        import contextlib
        buf = _io.StringIO()
        args = types.SimpleNamespace(log_file=None)
        with mock.patch.object(cli, "_cmd_check_locked", lambda a: 0), \
                contextlib.redirect_stdout(buf):
            cli.cmd_check(args)
        self.assertEqual(buf.getvalue(), "")

    def test_also_writes_a_manual_script_that_pauses(self):
        """再生成一个给人双击的：跑完**停住**，不然来不及看结果。"""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(schedule, "kind", lambda: "unix"):
                schedule.write_runner_script(Path(d), "config/store-X.yaml")
            m = schedule.manual_script_path(Path(d))
            self.assertTrue(m.exists(), "没生成 run-now")
            # 手动那个是**自己同步跑**，不是转调 run.sh（run.sh 不等进程）
            self.assertIn("check --days-ago", m.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            schedule.write_runner_script(Path(d), "config/store-X.yaml")
            m = schedule.manual_script_path(Path(d))     # ← 要在补丁内，否则又按 unix 算
            self.assertEqual(m.name, "run-now.bat")
            self.assertIn("pause", m.read_bytes().decode("ascii"))

    def test_default_days_ago_is_yesterday(self):
        """次日早上跑昨天 —— 当天晚上跑的话晚班报量还没发生，必然全是差异。"""
        self.assertEqual(schedule.DEFAULT_DAYS_AGO, 1)


class TestRunnerScriptSelfHeals(unittest.TestCase):
    """⚠ `run.bat` 是**注册任务时**生成的，升级代码**不会**动它。

    门店电脑上因此很容易留着旧脚本 —— 用户就踩到了：日志格式还是旧的、
    黑窗没修掉、界面还认不出"跑完没"。所以要比对脚本里的版本标记，过时就重建。
    """

    def test_marks_the_generated_script(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            schedule.write_runner_script(Path(d), "config/store-X.yaml")
            body = (Path(d) / "run.bat").read_text(encoding="utf-8")
        self.assertIn(schedule.RUNNER_MARK, body)

    def test_detects_an_old_script(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            p = Path(d) / "run.bat"
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write("@echo off\r\nrem 老版本\r\n")
            self.assertTrue(schedule.runner_outdated(Path(d)))
            schedule.refresh_runner_scripts(Path(d), "config/store-X.yaml")
            self.assertFalse(schedule.runner_outdated(Path(d)))
            self.assertIn(schedule.RUNNER_MARK, p.read_text(encoding="utf-8"))

    def test_rebuild_keeps_the_users_days_ago(self):
        """**绝不能**把用户选的"今天/昨天"默默改回默认值 ——
        那等于悄悄换了对账的目标日，比不重建更糟。
        """
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            p = Path(d) / "run.bat"
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write("@echo off\r\nrem 老版本\r\n"
                        '"py" -m src.cli check --days-ago 0\r\n')
            schedule.refresh_runner_scripts(Path(d), "config/store-X.yaml")
            body = p.read_text(encoding="utf-8")
        self.assertIn("--days-ago 0", body, "用户的「今天」被改掉了")

    def test_missing_script_uses_the_default(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            self.assertIsNone(schedule.existing_days_ago(Path(d)))
            schedule.refresh_runner_scripts(Path(d), "config/store-X.yaml")
            body = (Path(d) / "run.bat").read_text(encoding="utf-8")
        self.assertIn(f"--days-ago {schedule.DEFAULT_DAYS_AGO}", body)

    def test_up_to_date_script_is_not_rewritten(self):
        """幂等：已经是最新版就只读一个文件，别每次刷页面都写盘。"""
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            schedule.write_runner_script(Path(d), "config/store-X.yaml")
            p = Path(d) / "run.bat"
            # ⚠ 先读出来再开写 —— `open(p, "w")` 会**立刻清空**文件，
            #   在参数里调 read_text 读到的是空的（第一版就这么写的）。
            original = p.read_text(encoding="utf-8")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(original + "rem 用户自己加的\r\n")
            self.assertFalse(schedule.refresh_runner_scripts(Path(d), "config/store-X.yaml"))
            self.assertIn("用户自己加的", p.read_text(encoding="utf-8"))


class TestExitCodeDiagnosis(unittest.TestCase):
    """用户报过"退出码 120" —— 120 不在我们任何约定里，光看退出码指不到方向。

    真正有用的是**日志里有没有对账的输出**：一行都没有，说明对账程序
    **根本没跑起来**（run.bat 里的路径不对 / 假 python），跟"对账失败"是两回事。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        from src import web
        q = mock.patch.object(web.service, "find_running", lambda *a, **k: None)
        q.start()
        self.addCleanup(q.stop)
        self.app = web.App(self.root, "config/store-X.yaml")

    def _read(self, text):
        from src import web
        (self.root / "out" / "run.log").write_text(text, encoding="utf-8")
        return web.read_run_log(self.app)

    def test_only_exit_lines_means_it_never_ran(self):
        d = self._read("[2026-09-15 14:23:36.91] exit=120 \n"
                       "[2026-09-15 14:24:11.83] exit=120 \n")
        self.assertEqual(d["exit"], 120)
        self.assertFalse(d["has_output"],
                         "只有退出码却当成'跑过了'，用户永远查不出原因")

    def test_real_output_marks_it_as_ran(self):
        d = self._read("=== 2026-09-15 21:00:01 开始 ===\n"
                       "=== 对账 青岛新业广场店 目标日 2026-09-14 ===\n"
                       "[1/6] 华为会话：✅ 会话有效\n"
                       "=== 2026-09-15 21:01:12 结束 exit=1 ===\n")
        self.assertTrue(d["has_output"])
        self.assertEqual(d["exit"], 1)

    def test_traceback_counts_as_output(self):
        """报错了但**跑起来了** —— 这两种情况给用户的建议完全不同。"""
        d = self._read("Traceback (most recent call last):\n"
                       "ModuleNotFoundError: No module named 'requests'\n"
                       "[2026-09-15 14:23:36] exit=1\n")
        self.assertTrue(d["has_output"])


class TestNoConsoleWindows(unittest.TestCase):
    """服务是 `pythonw.exe` 跑的（本身没有控制台）。

    这时再起一个**控制台子进程**（schtasks / tasklist / python -m src.cli），
    Windows 会给它**新开一个黑窗口** —— 用户看到的就是"操作时弹出来的黑窗"。
    任务列表每 30 秒刷一次、每次好几个 schtasks，不处理会一直闪。

    实测判断方式必须用 `os.name`：`platform.system()` 是模块属性，
    测试里一 patch 就全局生效，会在 macOS 上也塞上 creationflags 直接崩。
    """

    def test_quiet_kwargs_is_empty_off_windows(self):
        from src import winutil
        self.assertEqual(winutil.quiet_kwargs(), {})

    def test_quiet_kwargs_uses_os_name_not_platform_system(self):
        """回归：曾经用 platform.system()，被测试的 patch 误伤。

        ⚠ 要查**真实代码**，不能简单 grep 源码 —— 解释这件事的文档字符串里
        就写着 "platform.system()"，第一版就是这么误报的。
        """
        import ast as _ast
        import inspect
        from src import winutil
        src = inspect.getsource(winutil.quiet_kwargs)
        body = src[src.index('"""', src.index('"""') + 3) + 3:]   # 去掉文档字符串
        self.assertIn("os.name", body)
        self.assertNotIn("platform", body)
        self.assertNotIn("import platform", inspect.getsource(winutil),
                         "winutil 不该再依赖 platform 模块")

    def test_quiet_kwargs_sets_create_no_window_on_windows(self):
        from src import winutil
        with mock.patch.object(winutil.os, "name", "nt"):
            self.assertEqual(winutil.quiet_kwargs(),
                             {"creationflags": winutil.CREATE_NO_WINDOW})
        self.assertEqual(winutil.CREATE_NO_WINDOW, 0x08000000)

    def test_schtasks_and_tasklist_pass_it(self):
        """真正会频繁触发的那两个（任务列表 + 进程存活检查）必须带上。"""
        import inspect
        from src import service, winutil
        self.assertIn("quiet_kwargs", inspect.getsource(winutil.schtasks))
        self.assertIn("quiet_kwargs", inspect.getsource(service.pid_alive))

    def test_runner_hides_the_child_console(self):
        """界面上点「运行」起的是 python.exe —— 不处理就是一个满屏黑窗。"""
        import inspect
        from src import runner
        self.assertIn("quiet_kwargs", inspect.getsource(runner.RunManager.start))


class TestTimeValidation(unittest.TestCase):
    def test_rejects_bad_time(self):
        for bad in ("25:00", "9:0", "abc", "", "09-00", "9:60"):
            r = schedule.install(Path("."), bad)
            self.assertFalse(r.get("ok"), f"{bad!r} 应该被拒")
            self.assertIn("时间格式", r.get("message", ""))
            self.assertTrue(r.get("invalid"), "参数不合法要标出来，接口才好回 400")

    def test_normalizes_hour(self):
        with mock.patch.object(schedule, "_unix_install",
                               return_value={"ok": True}) as m, \
                mock.patch.object(schedule, "kind", lambda: "unix"):
            r = schedule.install(Path("."), "9:05")
        self.assertEqual(r["time"], "09:05")
        self.assertEqual(m.call_args[0][1], "09:05")


class TestCron(unittest.TestCase):
    def test_install_writes_expected_cron_line(self):
        captured = {}

        def fake_write(lines):
            captured["lines"] = lines
            return True, "ok"

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "unix"), \
                mock.patch.object(schedule, "_cron_lines", lambda: ["0 3 * * * /old/thing"]), \
                mock.patch.object(schedule, "_cron_write", fake_write):
            r = schedule.install(Path(d), "09:30", days_ago=1, config="config/store-X.yaml")

        self.assertTrue(r["ok"])
        lines = captured["lines"]
        self.assertIn("0 3 * * * /old/thing", lines, "别人的 cron 条目不能弄丢")
        mine = [x for x in lines if schedule.CRON_MARK in x]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0].startswith("30 9 * * * "), mine[0])
        self.assertIn(str(Path(d) / "run.sh"), mine[0])

    def test_reinstall_replaces_not_duplicates(self):
        """**同名**重复注册要替换，不是再追加一条。"""
        captured = {}
        existing = [f"30 9 * * * /old/run.sh {schedule.CRON_MARK} CBG报量对账-18点00"]

        def fake_write(lines):
            captured["lines"] = lines
            return True, "ok"

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "unix"), \
                mock.patch.object(schedule, "_cron_lines", lambda: existing), \
                mock.patch.object(schedule, "_cron_write", fake_write):
            schedule.install(Path(d), "18:00", days_ago=1)

        mine = [x for x in captured["lines"] if schedule.CRON_MARK in x]
        self.assertEqual(len(mine), 1, "重复注册不能留下两条")
        self.assertTrue(mine[0].startswith("0 18 * * * "))

    def test_cron_failure_is_reported_with_manual_command(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "unix"), \
                mock.patch.object(schedule, "_cron_lines", lambda: []), \
                mock.patch.object(schedule, "_cron_write", lambda lines: (False, "Operation not permitted")):
            r = schedule.install(Path(d), "09:00")
        self.assertFalse(r["ok"])
        self.assertIn("Operation not permitted", r["message"])
        self.assertIn("crontab -", r["manual"], "失败时要给出可以手动执行的命令")

    def test_status_parses_time(self):
        with mock.patch.object(schedule, "kind", lambda: "unix"), \
                mock.patch.object(schedule, "_cron_lines",
                                  lambda: [f"5 7 * * * /x/run.sh {schedule.CRON_MARK}"]):
            st = schedule.status(Path("."))
        self.assertTrue(st["installed"])
        self.assertEqual(st["time"], "07:05")


class TestWindows(unittest.TestCase):
    def setUp(self):
        # 任务列表缓存是**进程级**的（为了不让概览页每 30 秒 fork 一堆 schtasks），
        # 不清掉的话上一个用例的假数据会漏到下一个 —— 静悄悄地测出假绿。
        schedule.invalidate_cache()

    def test_schtasks_args(self):
        seen = {}

        def fake_run(args, **kw):
            seen["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            r = schedule.install(Path(d), "09:00", days_ago=1, config="config/store-X.yaml")

        self.assertTrue(r["ok"])
        a = seen["args"]
        self.assertEqual(a[0], "schtasks")
        self.assertIn("/create", a)
        self.assertIn("/sc", a)
        self.assertIn("daily", a)
        self.assertIn("09:00", a)
        self.assertTrue(any("run.bat" in str(x) for x in a), a)
        self.assertIn("/f", a, "没有 /f 的话任务已存在时会卡住等人确认")

    def test_tr_argument_has_no_manual_quotes(self):
        """⚠ Windows 上 subprocess 走 list2cmdline：手工加的引号会被转义成 \"…\"，
        而 schtasks 不认 C 运行时的转义，会把反斜杠当成路径的一部分。
        必须传裸路径，让 subprocess 自己决定要不要加引号。
        """
        seen = {}

        def fake_run(args, **kw):
            seen["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            schedule.install(Path(d), "09:00", days_ago=1)

        tr = seen["args"][seen["args"].index("/tr") + 1]
        self.assertFalse(tr.startswith('"'), f"/tr 不能自己带引号：{tr!r}")
        self.assertFalse(tr.endswith('"'), f"/tr 不能自己带引号：{tr!r}")
        self.assertTrue(tr.endswith("run.bat"), tr)

    def test_delete_uses_f(self):
        seen = {}

        def fake_run(args, **kw):
            seen["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            schedule.remove()
        self.assertEqual(seen["args"][:2], ["schtasks", "/delete"])
        self.assertIn("/f", seen["args"])

    def test_lists_all_our_tasks_from_csv_and_xml(self):
        """列表用 CSV（第一列就是任务名，跟系统语言无关），详情用 XML。

        列表里混着别人的任务 —— 只挑我们自己的，别的别碰。
        """
        csv_out = (
            '"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","2026/9/16 1:00:00","就绪"\n'
            '"\\CBG报量对账","2026/9/16 21:00:00","就绪"\n'
            '"\\CBG报量对账-中午","2026/9/16 12:00:00","就绪"\n'
            '"\\CBG报量对账-已停用","2026/9/16 9:00:00","已禁用"\n'
        )
        xmls = {
            "\\CBG报量对账": '<?xml version="1.0" encoding="UTF-16"?>\n'
                '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                "<Triggers><CalendarTrigger><StartBoundary>2026-09-16T21:00:00</StartBoundary>"
                "</CalendarTrigger></Triggers><Settings><Enabled>true</Enabled></Settings>"
                "<Actions><Exec><Command>D:\\cbg\\run.bat</Command></Exec></Actions></Task>",
            "\\CBG报量对账-中午": "<Task xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">"
                "<Triggers><CalendarTrigger><StartBoundary>2026-09-16T12:00:00</StartBoundary>"
                "</CalendarTrigger></Triggers><Actions><Exec><Command>D:\\cbg\\run.bat</Command>"
                "</Exec></Actions></Task>",
            "\\CBG报量对账-已停用": "<Task xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">"
                "<Triggers><CalendarTrigger><StartBoundary>2026-09-16T09:00:00</StartBoundary>"
                "</CalendarTrigger></Triggers><Settings><Enabled>false</Enabled></Settings>"
                "<Actions><Exec><Command>D:\\cbg\\run.bat</Command></Exec></Actions></Task>",
        }

        def fake_run(args, **kw):
            if "/xml" in args:
                tn = args[args.index("/tn") + 1]
                return types.SimpleNamespace(returncode=0, stdout=xmls.get(tn, ""), stderr="")
            return types.SimpleNamespace(returncode=0, stdout=csv_out, stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            st = schedule.status(Path("."))

        self.assertTrue(st["installed"])
        names = [x["name"] for x in st["tasks"]]
        self.assertEqual(names, ["CBG报量对账", "CBG报量对账-中午", "CBG报量对账-已停用"])
        self.assertNotIn("ScheduledDefrag", " ".join(names), "别人的任务不能出现")
        # 时间从 StartBoundary 里拿 —— 不走本地化字段名
        self.assertEqual(st["tasks"][0]["time"], "21:00")
        self.assertEqual(st["tasks"][1]["time"], "12:00")
        self.assertEqual(st["tasks"][2]["enabled"], False)
        self.assertEqual(st["time"], "21:00", "顶部显示第一个任务的时间")

    def test_xml_failure_falls_back_to_list_output(self):
        """老机器/权限问题导致 /xml 拿不到时，退回解析 `下次运行时间`（中英文都试）。"""
        out = "任务名:      \\CBG报量对账\n下次运行时间:  2026/9/16 9:00:00\n状态:   就绪\n"

        def fake_run(args, **kw):
            if "/xml" in args:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="拒绝访问")
            if "CSV" in args:
                return types.SimpleNamespace(
                    returncode=0, stdout='"\\CBG报量对账","2026/9/16 9:00:00","就绪"\n', stderr="")
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            st = schedule.status(Path("."))
        self.assertTrue(st["installed"])
        self.assertEqual(st["time"], "09:00")
        self.assertEqual(st["tasks"][0]["detail_source"], "list")

    def test_delete_removes_only_the_selected_task(self):
        """用户：'删除的话删除选中的' —— 删第二个不能顺手把第一个也删了。"""
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            r = schedule.remove("CBG报量对账-中午")

        self.assertTrue(r["ok"])
        self.assertEqual(len(calls), 1, f"只应发一条删除命令，实际 {calls}")
        tn = calls[0][calls[0].index("/tn") + 1]
        self.assertEqual(tn, "CBG报量对账-中午")
        self.assertIn("/f", calls[0])

    def test_install_with_custom_name_registers_a_second_task(self):
        seen = {}

        def fake_run(args, **kw):
            seen["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            r = schedule.install(Path(d), "12:00", days_ago=0,
                                 config="config/store-X.yaml", name="CBG报量对账-中午")

        self.assertTrue(r["ok"])
        self.assertEqual(r["task"], "CBG报量对账-中午")
        self.assertEqual(seen["args"][seen["args"].index("/tn") + 1], "CBG报量对账-中午")

    def test_default_name_carries_the_execution_time(self):
        """用户报的现象："注册两个定时任务只显示一个"。

        根因：两次都留空名字 → 名字一样 → 第二次的 `/f` 把第一次**覆盖**了。
        所以默认名必须带时间，两次注册才会是两个任务。
        """
        seen = []

        def fake_run(args, **kw):
            seen.append(args[args.index("/tn") + 1] if "/tn" in args else "")
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            schedule.install(Path(d), "12:00", days_ago=0)     # 中午那次，名字留空
            schedule.install(Path(d), "21:00", days_ago=1)     # 打烊那次，名字留空

        self.assertEqual(seen, ["CBG报量对账-12点00", "CBG报量对账-21点00"],
                         "两次留空注册必须是两个不同的任务名")

    def test_default_task_name_has_no_illegal_chars(self):
        """任务名在 Windows 上就是文件名 —— 冒号是非法的（schtasks 会直接拒绝）。

        所以是「21点00」不是「21:00」。
        """
        for tm in ("00:00", "09:05", "12:00", "21:00", "23:59"):
            with self.subTest(t=tm):
                name = schedule.default_task_name(tm)
                for ch in '\\/:*?"<>|':
                    self.assertNotIn(ch, name, f"{name!r} 里有非法字符 {ch!r}")

    def test_same_time_twice_overwrites_on_purpose(self):
        """同一时间重复注册是**故意覆盖**（幂等），别变成两个同名任务。"""
        seen = []

        def fake_run(args, **kw):
            seen.append(args[args.index("/tn") + 1])
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            schedule.install(Path(d), "21:00")
            schedule.install(Path(d), "21:00")
        self.assertEqual(seen, ["CBG报量对账-21点00"] * 2)

    def test_legacy_bare_name_is_kept_not_silently_deleted(self):
        """升级期：老的 `CBG报量对账`（没有时间后缀）会和新任务**并存**。

        我们**故意不悄悄删它** —— 悄悄删用户的任务比让他看见两行更糟。
        代价是两条都会跑（同一天对账两遍），所以界面上必须提示，
        并且那一行本来就能单独删掉。
        """
        self.assertTrue(schedule._is_ours("\\" + schedule.TASK_NAME),
                        "老任务必须还列在表里，用户才看得到、删得掉")
        self.assertNotEqual(schedule.TASK_NAME, schedule.default_task_name("21:00"),
                            "新默认名必须跟老名字区分开，否则又变回互相覆盖")

    def test_autostart_task_is_not_listed_as_a_daily_task(self):
        """开机自启那个任务**不能**出现在「定时执行」表里。

        混进去的话，用户在这个页面点「删除」会顺手把开机自启删掉，
        然后完全不知道服务为什么不再自启了。
        """
        self.assertFalse(schedule._is_ours("\\" + schedule.AUTOSTART_TASK),
                         "开机自启任务被当成定时任务了")
        self.assertFalse(schedule._is_ours(schedule.AUTOSTART_TASK))

    def test_daily_tasks_are_still_listed(self):
        for name in ("\\CBG报量对账", "\\CBG报量对账-21点00", "\\CBG报量对账-中午",
                     "\\我的CBG报量对账"):
            with self.subTest(n=name):
                self.assertTrue(schedule._is_ours(name), name)

    def test_other_peoples_tasks_are_never_listed(self):
        for name in ("\\Microsoft\\Windows\\Defrag\\ScheduledDefrag", "\\OneDrive"):
            with self.subTest(n=name):
                self.assertFalse(schedule._is_ours(name), name)

    def test_run_now_triggers_that_exact_task(self):
        """用户要的：'一个一个...执行'。

        用 `schtasks /run` 而不是直接调 check —— 这样验证的才是**任务本身**
        （路径、参数、权限对不对），而不是"代码没问题"。
        """
        seen = []

        def fake_run(args, **kw):
            seen.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            res = schedule.run_now("CBG报量对账-12点00")

        self.assertTrue(res["ok"])
        self.assertEqual(seen[0][:2], ["schtasks", "/run"])
        self.assertEqual(seen[0][seen[0].index("/tn") + 1], "CBG报量对账-12点00")
        self.assertIn("12点00", res["message"])

    def test_run_now_reports_failure(self):
        def fake_run(args, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="找不到任务")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            res = schedule.run_now("不存在的任务")
        self.assertFalse(res["ok"])
        self.assertIn("找不到任务", res["message"])

    def test_run_now_falls_back_to_the_default_name(self):
        seen = []

        def fake_run(args, **kw):
            seen.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            schedule.run_now()
        self.assertEqual(seen[0][seen[0].index("/tn") + 1], schedule.TASK_NAME)

    def test_install_name_with_illegal_chars_is_rejected(self):
        r"""任务名里有 \ / : 之类会让 schtasks 建出一个奇怪的路径 —— 提前拦掉。"""
        for bad in ("a\\b", "a/b", "a:b", "a*b", 'a?b', "a<b"):
            with self.subTest(bad=bad):
                r = schedule.install(Path("."), "09:00", name=bad)
                self.assertFalse(r["ok"])
                self.assertTrue(r.get("invalid"), bad)

    def test_utf16_output_is_decoded(self):
        """中文 Windows 的 schtasks 可能吐 UTF-16 —— 不处理就会读成乱码、时间解析不出来。"""
        xml = ('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               "<Triggers><CalendarTrigger><StartBoundary>2026-09-16T21:00:00</StartBoundary>"
               "</CalendarTrigger></Triggers></Task>")

        def fake_run(args, **kw):
            if "/xml" in args:
                return types.SimpleNamespace(returncode=0, stdout=xml.encode("utf-16"), stderr="")
            return types.SimpleNamespace(
                returncode=0, stdout='"\\CBG报量对账","2026/9/16 21:00:00","就绪"\n'.encode("utf-16"),
                stderr="")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            st = schedule.status(Path("."))
        self.assertTrue(st["installed"], "UTF-16 的 CSV 没解出来")
        self.assertEqual(st["time"], "21:00")

    def test_list_is_cached_but_install_busts_it(self):
        """缓存只是省进程，不能在用户点完「注册」之后还显示旧列表。"""
        calls = {"n": 0}
        rows = ['"\\CBG报量对账","2026/9/16 21:00:00","就绪"\n']
        xml = ('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               "<Triggers><CalendarTrigger><StartBoundary>2026-09-16T21:00:00</StartBoundary>"
               "</CalendarTrigger></Triggers></Task>")

        def fake_run(args, **kw):
            calls["n"] += 1
            if "/xml" in args:
                return types.SimpleNamespace(returncode=0, stdout=xml, stderr="")
            if "CSV" in args:
                return types.SimpleNamespace(returncode=0, stdout="".join(rows), stderr="")
            return types.SimpleNamespace(returncode=0, stdout="成功", stderr="")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            schedule.status(Path("."))
            after_first = calls["n"]
            self.assertGreater(after_first, 0)
            schedule.status(Path("."))
            self.assertEqual(calls["n"], after_first, "10 秒内应该走缓存，不再 fork schtasks")
            schedule.install(Path(d), "12:00", name="CBG报量对账-中午")   # ← 应作废缓存
            schedule.status(Path("."))
            self.assertGreater(calls["n"], after_first + 1, "注册完必须重新读，不能是旧列表")

    def test_not_installed(self):
        def fake_run(args, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="找不到")

        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(winutil.shutil, "which", lambda x: "schtasks"), \
                mock.patch.object(winutil.subprocess, "run", fake_run):
            st = schedule.status(Path("."))
        self.assertFalse(st["installed"])


if __name__ == "__main__":
    unittest.main()


class TestReconcileWindows(unittest.TestCase):
    """销售窗口 / 华为窗口的计算 —— **搞错就会误报未报量**，值得单独钉住。"""

    def setUp(self):
        import datetime
        from src.cli import compute_windows
        self.f = compute_windows
        self.d = datetime.date
        self.target = datetime.date(2026, 9, 14)

    def test_defaults_are_the_target_day_only(self):
        """默认 lookback=0 / lookahead=0 → 两个窗口都只有目标日当天。"""
        s1, s2, c1, c2 = self.f(self.target, 0, 0)
        self.assertEqual((s1, s2), (self.target, self.target))
        self.assertEqual((c1, c2), (self.target, self.target))

    def test_lookback_extends_sales_window(self):
        s1, s2, c1, c2 = self.f(self.target, 2, 0)
        self.assertEqual(s1, datetime.date(2026, 9, 12))
        self.assertEqual(s2, self.target)
        self.assertEqual(c1, s1, "华为窗口起点跟着销售窗口走")
        self.assertEqual(c2, self.target)

    def test_lookahead_extends_only_cbg_window(self):
        """lookahead 只往后拉华为窗口，销售窗口不动 —— 方向别搞反。"""
        s1, s2, c1, c2 = self.f(self.target, 0, 1)
        self.assertEqual((s1, s2), (self.target, self.target))
        self.assertEqual(c1, self.target)
        self.assertEqual(c2, datetime.date(2026, 9, 15))

    def test_negative_is_clamped(self):
        s1, s2, c1, c2 = self.f(self.target, -5, -5)
        self.assertEqual((s1, s2, c1, c2), (self.target,) * 4)

    def test_config_default_is_zero(self):
        """**发出去的那份模板**里 report_lookahead_days 默认必须是 0（用户要求的）。

        ⚠ 读的是 `src/store-config.default.yaml`，不是 `config/store-*.yaml`：
        后者是**这台电脑自己的配置**（已不进 git、不进包），新克隆的仓库上根本没有
        —— 读它的话这条测试在新机器上必挂。
        """
        import pathlib
        import yaml
        cfg = yaml.safe_load(
            (pathlib.Path(__file__).resolve().parent.parent
             / "src" / "store-config.default.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["check"]["report_lookahead_days"], 0)
        self.assertEqual(cfg["check"]["lookback_days"], 0)

    def test_missing_key_falls_back_to_zero(self):
        """老配置文件里没有这个键时，代码兜底也得是 0。"""
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "src" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('check.get("report_lookahead_days", 0)', src,
                      "兜底默认值必须和配置一致，否则老配置会静默用 1")


class TestNoShadowedClasses(unittest.TestCase):
    """防呆：同一个测试文件里**类名不能重复**。

    Python 里同名类会后者覆盖前者 —— 被覆盖的测试**静默消失**，套件照样全绿。
    写这条的时候真的踩到了：新加的 `TestWindows`（窗口计算）覆盖了原有的
    `TestWindows`（schtasks），一下丢了 5 个测试，而且 `unittest` 毫无提示。
    """

    def test_no_duplicate_class_names_per_file(self):
        import ast
        import collections
        import pathlib

        here = pathlib.Path(__file__).resolve().parent
        for f in sorted(here.glob("test_*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
            dup = [k for k, v in collections.Counter(names).items() if v > 1]
            self.assertFalse(dup, f"{f.name} 里有重名的测试类：{dup}（会静默覆盖，丢掉测试）")


class TestScheduleFailureAdvice(unittest.TestCase):
    """注册失败时给的**下一步**必须是能照着做的。

    ⚠ 原来写的是"试试右键 start.bat → 以管理员身份运行" —— **完全错的方向**：
    `start.bat` 是"启动服务"，跟建计划任务没有半点关系，照着做只会白跑一趟。
    （实测用户就卡在这儿，问"没有管理员权限了那定时任务怎么加"。）
    """

    def _fail_msg(self):
        import types
        from src import schedule
        r = types.SimpleNamespace(returncode=1, stdout=b"", stderr="错误: 拒绝访问。")
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "write_runner_script",
                                  lambda *a, **k: pathlib.Path("D:/x/run.bat")), \
                mock.patch.object(schedule, "_schtasks", lambda *a, **k: r):
            return schedule.install(pathlib.Path("D:/x"), "21:00", 1, "config/x.yaml")

    def test_does_not_tell_users_to_run_start_bat_as_admin(self):
        msg = self._fail_msg()["message"]
        self.assertNotIn("右键 start.bat", msg,
                         "start.bat 是启动服务，跟建计划任务无关 —— 别指错方向")

    def test_says_it_needs_admin_but_only_once(self):
        msg = self._fail_msg()["message"]
        self.assertIn("管理员权限", msg)
        self.assertIn("只弹这一次", msg, "要说明是一次性的，不是每次都要")

    def test_says_the_task_still_runs_unelevated(self):
        """⚠ 关键承诺：提权**建**出来的任务，照样是普通权限**运行**的。

        不然用户会以为"又变回管理员了"，而管理员身份会让自动抓会话失败。
        `schtasks` 的 `/rl` 默认就是 `Limited`（微软文档），所以这是真的。
        """
        msg = self._fail_msg()["message"]
        self.assertIn("普通权限运行", msg)

    def test_names_the_stale_task_cause(self):
        """第二种原因要点出来：以前用管理员身份建过同名任务 → 覆盖不了。"""
        msg = self._fail_msg()["message"]
        self.assertIn("覆盖", msg)

    def test_manual_command_is_still_offered(self):
        got = self._fail_msg()
        self.assertIn("schtasks", got["manual"], "手动那条路要留着当退路")


class TestUnreadableTaskIsNotMissing(unittest.TestCase):
    """⚠ **"读不到详情"和"没有这个任务"是两回事**，界面上的话也完全不同。

    实测：定时任务里只显示一个名字、时间和命令全空。原因是任务
    **以管理员身份建**的（所有者 `Administrators`），而服务现在是
    **普通权限**（过滤令牌）→ `schtasks /query /tn <名> /xml` 直接被拒。
    用户看到的就是"没有管理员权限就看不到定时执行的设置"。

    所以 `_win_task_info` 要把这种情况标出来（`unreadable=True`），
    界面才能说"是权限不够，不是没设"并给出修法。
    """

    def _info(self, query_results):
        """query_results: 按调用顺序给的返回值（`_schtasks` 会被调两次）。"""
        from src import schedule as sch
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return query_results[min(len(calls) - 1, len(query_results) - 1)]

        with mock.patch.object(sch, "_schtasks", fake):
            return sch._win_task_info(r"\CBG报量对账-21点00"), calls

    def test_both_queries_denied_marks_it_unreadable(self):
        r = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied")
        info, calls = self._info([r, r])
        self.assertTrue(info["unreadable"], "该标成「读不到」")
        self.assertEqual(info["time"], "")
        self.assertEqual(len(calls), 2, "两条路都要试过才认")

    def test_xml_success_is_not_unreadable(self):
        xml = (b'<?xml version="1.0" encoding="UTF-16"?>'
               b'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               b'<Triggers><CalendarTrigger><StartBoundary>2026-09-15T21:00:00</StartBoundary>'
               b'</CalendarTrigger></Triggers><Settings><Enabled>true</Enabled></Settings>'
               b'<Actions><Exec><Command>pythonw.exe</Command></Exec></Actions></Task>')
        ok = types.SimpleNamespace(returncode=0, stdout=xml, stderr=b"")
        info, _ = self._info([ok])
        self.assertFalse(info["unreadable"])
        self.assertEqual(info["time"], "21:00")

    def test_list_fallback_that_works_is_not_unreadable(self):
        """XML 读不到但 LIST 读得到 → 能拿到时间就算正常。"""
        bad = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        listing = ("\u4efb\u52a1\u540d:  x\r\n\u4e0b\u6b21\u8fd0\u884c\u65f6\u95f4: 2026-09-16 21:00:00\r\n").encode("utf-8")
        good = types.SimpleNamespace(returncode=0, stdout=listing, stderr=b"")
        info, _ = self._info([bad, good])
        self.assertFalse(info["unreadable"], "退路读到了就不算读不到")
        self.assertEqual(info["time"], "21:00")


class TestScheduleRecord(unittest.TestCase):
    """我们自己记的那份"注册参数"（`.secrets/schedule.json`）。

    ## 为什么非有它不可

    门店那台机器上**普通权限建不了计划任务**（`schtasks /create` 报
    `错误: 拒绝访问。`），只能提权建；而提权建出来的任务所有者是
    `Administrators` → 之后普通权限**连详情都读不到**
    （`schtasks /query /tn <名> /xml` 被拒）。

    用户看到的原话是「**没有管理员权限就看不到定时执行设置了**」。
    那就别去问 Windows —— 注册参数是我们自己传的，记下来就行。
    这份记录**不依赖任何 ACL 行为、任何系统语言**。

    ⚠ 所以它是"提权建任务"能成立的前提：**没有记录就别提权建**，
      否则又回到"界面上只有一个名字"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _record(self) -> dict:
        import json as _json
        f = self.root / schedule.RECORD_FILE
        return _json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    # ---- 读写
    def test_successful_install_remembers_the_parameters(self):
        """⚠ 注册成功就**立刻**记 —— 这是记录机制唯一的写入点。"""
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_win_install",
                                  return_value={"ok": True, "task": "T"}):
            r = schedule.install(self.root, "20:30", days_ago=2,
                                 config="config/store-X.yaml",
                                 name="CBG报量对账-20点30")
        self.assertTrue(r["ok"])
        rec = self._record()["CBG报量对账-20点30"]
        self.assertEqual(rec["time"], "20:30")
        self.assertEqual(rec["days_ago"], 2)
        self.assertEqual(rec["config"], "config/store-X.yaml")
        self.assertTrue(rec["at"], "要记下什么时候注册的")

    def test_failed_install_does_not_remember(self):
        """没建成的任务不许在界面上装作存在 —— 失败就**不记**。"""
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_win_install",
                                  return_value={"ok": False, "message": "拒绝访问"}):
            r = schedule.install(self.root, "20:30")
        self.assertFalse(r["ok"])
        self.assertEqual(self._record(), {})

    def test_record_lives_in_secrets_so_updates_cannot_wipe_it(self):
        """⚠ 放 `.secrets/` 是**有意的**：selfupdate 的 NEVER_TOUCH。

        它跟 `.secrets/` 里其它东西一样是"**这台电脑的**运行状态" ——
        自更新"照仓库原样铺"时不许碰，否则一次升级就把注册记录冲没了。
        """
        self.assertTrue(schedule.RECORD_FILE.startswith(".secrets/"),
                        "挪出 .secrets/ 就会被自更新冲掉")
        from src import selfupdate
        self.assertIn(".secrets", selfupdate.NEVER_TOUCH)

    def test_corrupt_or_missing_record_never_raises(self):
        """这是**显示用**的东西 —— 坏了最多是显示不出来，**绝不能把注册搞挂**。

        界面上"要不要注册"的判断全都会路过这里，抛一次就是整个页面白屏。
        """
        self.assertEqual(schedule._recall(self.root), {}, "文件不在 → 空")
        f = self.root / schedule.RECORD_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        for junk in ("{ 这不是 json", "[1,2,3]", "", "null"):
            f.write_text(junk, encoding="utf-8")
            self.assertEqual(schedule._recall(self.root), {}, f"{junk!r} 要给空 dict")

    def test_recall_without_a_root_is_empty_not_a_crash(self):
        """`root=None` 也要给空 dict（Path(None) 抛的是 TypeError）。"""
        self.assertEqual(schedule._recall(None), {})

    # ---- 读不到详情时拿记录兜底
    def test_record_fills_in_the_details_when_windows_refuses_to_read(self):
        """普通权限两条查询都被拒 → **记录顶上**，界面上照样有时间和命令。

        ⚠ 这时 `unreadable` 必须是 False：它表示"真的读不到、得让用户去修"，
        而我们有记录，用户不需要做任何事。
        """
        rec = {"CBG报量对账-21点20": {"time": "21:20", "days_ago": 0,
                                      "at": "2026-09-16 22:01:00"}}
        denied = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied")
        with mock.patch.object(schedule, "_schtasks", lambda *a, **k: denied), \
                mock.patch.object(schedule, "_win_list_names",
                                  lambda: [r"\CBG报量对账-21点20"]), \
                mock.patch.object(schedule, "_recall", lambda root: rec), \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            schedule.invalidate_cache()
            tasks = schedule._win_list(force=True, root=self.root)
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["time"], "21:20", "记录里的时间要顶上去")
        self.assertEqual(t["detail_source"], "record")
        self.assertFalse(t["unreadable"], "有记录就不算「读不到」")

    def test_without_a_record_it_still_says_unreadable(self):
        """**没有**记录 + 读不到 → 老实标 `unreadable`，界面才知道要提示修。"""
        denied = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied")
        with mock.patch.object(schedule, "_schtasks", lambda *a, **k: denied), \
                mock.patch.object(schedule, "_win_list_names",
                                  lambda: [r"\CBG报量对账-21点20"]), \
                mock.patch.object(schedule, "_recall", lambda root: {}), \
                mock.patch.object(schedule, "kind", lambda: "windows"):
            schedule.invalidate_cache()
            tasks = schedule._win_list(force=True, root=self.root)
        self.assertTrue(tasks[0]["unreadable"])
        self.assertEqual(tasks[0]["time"], "")

    # ---- 删掉就抹掉
    def test_removing_a_task_forgets_its_record(self):
        """删了还留着记录 → 界面上"任务没了、时间和命令还在"，看着像没删掉。"""
        schedule._remember(self.root, "CBG报量对账-中午", time_str="12:00")
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_win_remove",
                                  return_value={"ok": True, "task": "T"}):
            r = schedule.remove("CBG报量对账-中午", self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(self._record(), {})

    def test_failed_removal_keeps_the_record(self):
        """没删掉就不能抹记录 —— 任务还在，界面得继续显示它。"""
        schedule._remember(self.root, "CBG报量对账-中午", time_str="12:00")
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_win_remove",
                                  return_value={"ok": False, "message": "拒绝访问"}):
            schedule.remove("CBG报量对账-中午", self.root)
        self.assertIn("CBG报量对账-中午", self._record())

    def test_forget_accepts_the_full_name_the_ui_sends(self):
        """⚠ 界面上删除按钮发的是 `full_name`（带反斜杠），记录里的键是叶子名。

        只 `pop(task)` 的话**永远删不掉** —— 用户看到的是
        "删了之后时间和命令还挂在那儿"，会以为删除失败了。
        这个 bug 真出现过。
        """
        schedule._remember(self.root, "CBG报量对账-21点20", time_str="21:20")
        schedule._forget(self.root, "\\CBG报量对账-21点20")
        self.assertEqual(self._record(), {}, "带反斜杠的名字也要认得出来")

    def test_remove_all_clears_the_record_when_everything_is_gone(self):
        """卸载时整套撤干净，记录也别留 —— 否则下次安装会显示一个不存在的时间。"""
        # ⚠ 名字要通过 `_is_ours`（含 TASK_NAME），否则它压根不进删除列表，
        #   这条测试就"因为没有任务可删"而**假绿**
        schedule._remember(self.root, "CBG报量对账-21点00", time_str="21:00")
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_win_list_names",
                                  lambda: ["\\CBG报量对账-21点00"]), \
                mock.patch.object(schedule, "_schtasks",
                                  lambda *a, **k: types.SimpleNamespace(
                                      returncode=0, stdout=b"", stderr=b"")):
            r = schedule.remove_all(self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(self._record(), {})

    def test_remove_all_keeps_the_record_if_something_survived(self):
        """⚠ 还有任务没删掉时**不能**抹记录。

        那种情况下这份记录是界面上唯一还能显示它的东西 ——
        抹了就真成"看不见也删不掉"，只能去任务计划程序里瞎找。
        """
        schedule._remember(self.root, "CBG报量对账-21点00", time_str="21:00")
        with mock.patch.object(schedule, "kind", lambda: "windows"), \
                mock.patch.object(schedule, "_win_list_names",
                                  lambda: ["\\CBG报量对账-21点00"]), \
                mock.patch.object(schedule, "_schtasks",
                                  lambda *a, **k: types.SimpleNamespace(
                                      returncode=1, stdout=b"", stderr=b"denied")):
            r = schedule.remove_all(self.root)
        self.assertFalse(r["ok"])
        self.assertIn("CBG报量对账-21点00", self._record())

    def test_two_tasks_do_not_overwrite_each_other(self):
        """中午 + 打烊两条要各记各的 —— 后来的不能把先前的盖掉。"""
        schedule._remember(self.root, "A", time_str="12:00")
        schedule._remember(self.root, "B", time_str="21:00")
        self.assertEqual(self._record()["A"]["time"], "12:00")
        self.assertEqual(self._record()["B"]["time"], "21:00")
        schedule._forget(self.root, "A")
        self.assertNotIn("A", self._record())
        self.assertIn("B", self._record(), "抹掉一条不能连坐")


class TestScheduleSubcommandsExist(unittest.TestCase):
    """`bootstrap.py` 那两个子命令是界面上提权按钮的落点。

    ⚠ 名字写错了表现是"点了没反应" —— 门店完全无从下手，所以钉住。
    """

    def test_bootstrap_has_both_subcommands(self):
        import pathlib as _p
        src = (_p.Path(__file__).resolve().parent.parent / "bootstrap.py").read_text(encoding="utf-8")
        for sub in ("schedule-install", "schedule-remove"):
            self.assertIn(f'"{sub}"', src)
            self.assertIn(sub, src.split("STDLIB_ONLY = ")[1][:200],
                          f"{sub} 可能在依赖没装好时被提权调起 —— 要进 STDLIB_ONLY")
        self.assertIn("do_schedule_install", src)
        self.assertIn("do_schedule_remove", src)
