"""「跑什么」（`what`）这条线的测试 —— 界面四个按钮 / 定时任务勾选 / run.bat。

**为什么单独钉**：`what` 从界面一路穿到 `run.bat`，中间隔了四跳
（`/api/run` → `runner.start` → `daily` 子命令 → `run_daily.main`）。
任何一跳漏转发，表现都是**静默跑错东西** —— 勾了"只算 POS"结果把数据也抓了、
还顺手推了报量排查。这种错不会报错，只会让人某天觉得"怎么这么慢"。

用户 2026-09-16 定的：
* 运行页四个按钮：整个项目 / 抓华为数据 / 报量排查 / POS 合规；
* 单独执行的按钮**不顺手抓数据**（抓数据要一两分钟起）；
* 设置里勾「自动化跑什么」，两个都不勾是**错的**（不许静默当成"都跑"）。
"""

import unittest
from pathlib import Path
from unittest import mock

from src import cli, run_daily, runner, schedule

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


class TestWhatFlags(unittest.TestCase):
    def test_四个预设都在(self):
        self.assertEqual(set(run_daily.BUTTON_STEPS),
                         {"all", "dump", "reconcile", "pos"})

    def test_整个项目不加任何跳过开关(self):
        self.assertEqual(run_daily.flags_for("all"), [])

    def test_每个单独执行都跳过另外两件事(self):
        """⚠ 「单独执行」的语义就是**只做那一件** —— 不许顺手把别的也跑了。"""
        want = {
            "dump": {"--skip-check", "--skip-pos"},
            "reconcile": {"--skip-dump", "--skip-pos"},
            "pos": {"--skip-dump", "--skip-check"},
        }
        for what, flags in want.items():
            with self.subTest(what=what):
                self.assertEqual(set(run_daily.flags_for(what)), flags)

    def test_认不出来的_what_要抛而不是回落(self):
        """⚠ 回落成默认值的后果是"勾了只算 POS，结果数据也抓了、推送也发了"，
        而**日志里一个字都不会说**。宁可当场炸。"""
        with self.assertRaises(ValueError):
            run_daily.flags_for("nonsense")
        with self.assertRaises(ValueError):
            run_daily.flags_for("")

    def test_每个预设都有给人看的名字(self):
        for what in run_daily.BUTTON_STEPS:
            with self.subTest(what=what):
                self.assertTrue(run_daily.BUTTON_LABELS.get(what))


class TestDailySkipFlags(unittest.TestCase):
    """`daily` 的三个跳过开关 —— 跑哪几件事由它们组合出来。"""

    def _run(self, extra):
        calls, codes = [], {"dump": 0, "check": 0, "pos": 0}

        def mk(k):
            def f(_a):
                calls.append(k)
                return codes[k]
            return f
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(cli, "cmd_dump", mk("dump")), \
             mock.patch.object(cli, "cmd_check", mk("check")), \
             mock.patch.object(cli, "cmd_pos", mk("pos")), \
             contextlib.redirect_stdout(buf), \
             mock.patch.object(cli, "_find_pos_db",
                               return_value=Path("/tmp/cbg-2026.db")):
            rc = run_daily.main(extra)
        return rc, calls, buf.getvalue()

    def test_整个项目三步都跑(self):
        rc, calls, _ = self._run([])
        self.assertEqual(calls, ["dump", "check", "pos"])
        self.assertEqual(rc, 0)

    def test_只抓数据(self):
        rc, calls, _ = self._run(["--skip-check", "--skip-pos"])
        self.assertEqual(calls, ["dump"])

    def test_只报量排查(self):
        rc, calls, _ = self._run(["--skip-dump", "--skip-pos"])
        self.assertEqual(calls, ["check"])

    def test_只算_POS(self):
        rc, calls, _ = self._run(["--skip-dump", "--skip-check"])
        self.assertEqual(calls, ["pos"])

    def test_全跳过什么都不做也不报错(self):
        rc, calls, _ = self._run(["--skip-dump", "--skip-check", "--skip-pos"])
        self.assertEqual(calls, [])
        self.assertEqual(rc, 0)

    def test_表头写清这次要跑哪几件(self):
        _, _, out = self._run(["--skip-dump"])
        self.assertIn("报量排查", out)
        self.assertIn("POS 合规", out)

    # ------------------------------------------------ 退出码：跳过的步骤不许参与
    def test_只算_POS_时退出码只看_POS(self):
        """⚠ 这条是**重构的直接原因**：第一版固定拿 rc2/rc3 两个变量算退出码，
        于是"只算 POS"会把**根本没跑过**的报量排查那一步的默认值也算进去。
        """
        import contextlib
        import io
        seen = {}

        def fake_pos(_a):
            seen["pos"] = True
            return 0

        def boom(_a):
            raise AssertionError("只算 POS 时不该跑报量排查")

        buf = io.StringIO()
        with mock.patch.object(cli, "cmd_pos", fake_pos), \
             mock.patch.object(cli, "cmd_check", boom), \
             mock.patch.object(cli, "cmd_dump", boom), \
             contextlib.redirect_stdout(buf):
            rc = run_daily.main(["--skip-dump", "--skip-check"])
        self.assertTrue(seen.get("pos"))
        self.assertEqual(rc, 0)

    def test_跳过的步骤失败也不影响退出码(self):
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(cli, "cmd_dump", lambda a: 99), \
             mock.patch.object(cli, "cmd_check", lambda a: 0), \
             mock.patch.object(cli, "cmd_pos", lambda a: 0), \
             contextlib.redirect_stdout(buf):
            rc = run_daily.main(["--skip-dump"])
        self.assertEqual(rc, 0, "跳过的步骤不该把退出码带坏")

    def test_报量排查有差异_POS_也要跑而且_3_要传出去(self):
        import contextlib
        import io
        calls = []
        buf = io.StringIO()
        with mock.patch.object(cli, "cmd_dump", lambda a: (calls.append("d"), 0)[1]), \
             mock.patch.object(cli, "cmd_check",
                               lambda a: (calls.append("c"), cli.EXIT_DIFF)[1]), \
             mock.patch.object(cli, "cmd_pos",
                               lambda a: (calls.append("p"), 0)[1]), \
             mock.patch.object(cli, "_find_pos_db",
                               return_value=Path("/tmp/cbg-2026.db")), \
             contextlib.redirect_stdout(buf):
            rc = run_daily.main([])
        self.assertEqual(calls, ["d", "c", "p"])
        self.assertEqual(rc, cli.EXIT_DIFF)


class TestRunnerWhat(unittest.TestCase):
    def _argv(self, what):
        m = runner.RunManager()
        d = Path("/tmp/x")
        j = m.start(d, "config/store-X.yaml", what=what)
        j.kill()
        j.running = False
        return " ".join(j.argv[j.argv.index("daily") + 1:])

    def test_四个_what_都走_daily_这一个入口(self):
        """⚠ 四个按钮走四条不同命令的话，`daily` 那些行为
        （第 1 步失败就不发、库里没有就抓全量、会话失效先静默续期）
        在界面上就全都享受不到了，而且"界面跑的"和"定时任务跑的"迟早分叉。"""
        for what in run_daily.BUTTON_STEPS:
            with self.subTest(what=what):
                m = runner.RunManager()
                j = m.start(Path("/tmp/x"), "c.yaml", what=what)
                self.assertIn("daily", j.argv)
                j.kill()
                j.running = False

    def test_argv_里的开关跟_WHAT_FLAGS_一致(self):
        self.assertEqual(self._argv("dump"), "--skip-check --skip-pos --days-ago 1")
        self.assertEqual(self._argv("pos"), "--skip-dump --skip-check --days-ago 1")
        self.assertEqual(self._argv("all"), "--days-ago 1")

    def test_乱传_what_不会留下半死的_job(self):
        """⚠ 必须在**起进程之前**炸 —— 起完再炸会留一个 running=True 的 job，
        界面上一直显示"在跑"，而实际上什么都没跑。"""
        m = runner.RunManager()
        with self.assertRaises(ValueError):
            m.start(Path("/tmp/x"), "c.yaml", what="bogus")
        self.assertEqual(len(m.jobs), 0)
        self.assertIsNone(m.current())

    def test_job_里带着_what_给界面用(self):
        m = runner.RunManager()
        j = m.start(Path("/tmp/x"), "c.yaml", what="pos")
        self.assertEqual(j.snapshot(0)["what"], "pos")
        self.assertEqual(j.snapshot(0)["what_label"], "POS 合规")
        j.kill()
        j.running = False


class TestScheduleAutomation(unittest.TestCase):
    """设置里「自动化跑什么」—— 存 `.secrets/schedule.json` + 重写 run.bat。

    ⚠ **三项**：抓华为数据 / 报量排查 / POS 合规，**默认三件都勾**（用户定的）。
    复选框列表必须和旁边那列「跑什么」对得上 —— 否则用户看到"只勾了两项"、
    实际跑了三件，会以为程序乱来。
    """

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = mock.patch.object(schedule, "kind", lambda: "windows")
        p.start()
        self.addCleanup(p.stop)

    def test_三项可选_默认三件都做(self):
        self.assertEqual(tuple(schedule.AUTOMATION_CHOICES),
                         ("dump", "reconcile", "pos"))
        self.assertEqual(tuple(run_daily.AUTOMATION_DEFAULT_STEPS),
                         ("dump", "reconcile", "pos"))

    def test_抓数据默认要执行(self):
        """⚠ 用户 2026-09-16 明确要求："定时任务自动化跑什么默认执行抓数据"。

        不抓的话库永远是旧的，报量排查每天以「库不新鲜」失败，
        POS 也永远算老数据 —— 而日志只会说"失败"。
        """
        self.assertIn("dump", run_daily.AUTOMATION_DEFAULT_STEPS)
        self.assertNotIn("--skip-dump",
                         run_daily.flags_for_steps(run_daily.AUTOMATION_DEFAULT_STEPS))

    def test_一个都不勾要抛(self):
        """⚠ 静默当成"都跑"的话，用户取消了勾选、结果每天照样推送，
        而日志里一个字都不会说。"""
        with self.assertRaises(ValueError):
            schedule.steps_from_choices([])
        with self.assertRaises(ValueError):
            schedule.steps_from_choices(None)

    def test_乱勾要抛(self):
        with self.assertRaises(ValueError):
            schedule.steps_from_choices(["nonsense"])

    def test_顺序不同算同一种(self):
        self.assertEqual(schedule.steps_from_choices(["pos", "dump"]),
                         ("dump", "pos"))

    def test_七种组合生成的开关都对(self):
        want = {
            ("dump", "reconcile", "pos"): "",
            ("dump", "reconcile"): "--skip-pos",
            ("dump", "pos"): "--skip-check",
            ("reconcile", "pos"): "--skip-dump",
            ("dump",): "--skip-check --skip-pos",
            ("reconcile",): "--skip-dump --skip-pos",
            ("pos",): "--skip-dump --skip-check",
        }
        for picked, flags in want.items():
            with self.subTest(picked=picked):
                schedule.write_runner_script(self.root, "c.yaml", 1, steps=picked)
                body = schedule.script_path(self.root).read_text(encoding="utf-8")
                line = [x for x in body.splitlines() if "daily" in x][0]
                rest = line.split("daily", 1)[1]
                if flags:
                    self.assertIn(flags, rest)
                else:
                    self.assertNotIn("--skip", rest)

    def test_反推能读回来(self):
        """重建脚本时要**保住勾选** —— 跟 `--days-ago` 一个道理。
        读不回来的话，一次界面自愈就把勾选悄悄改回默认。"""
        for steps in (("dump",), ("dump", "reconcile"), ("reconcile", "pos"),
                      ("pos",), ("dump", "reconcile", "pos")):
            with self.subTest(steps=steps):
                schedule.write_runner_script(self.root, "c.yaml", 1, steps=steps)
                self.assertEqual(schedule.existing_steps(self.root), steps)

    def test_手工脚本也要跟着改(self):
        """run-now.bat 是给人手动双击的 —— 不改的话手动跑的还是老范围。"""
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("pos",))
        now = schedule.manual_script_path(self.root).read_text(encoding="utf-8")
        self.assertIn("--skip-dump", now)

    def test_保存勾选_不重新注册任务(self):
        """⚠ 改勾选**不用碰 Windows 任务** —— 任务跑的是 run.bat，
        改的是它的内容。重新注册有可能弹 UAC，为了改个勾选弹框不值得。"""
        with mock.patch.object(schedule, "install") as inst:
            res = schedule.set_automation_steps(self.root, "c.yaml", ["pos"])
        self.assertEqual(inst.call_count, 0)
        self.assertTrue(res["ok"])
        self.assertIn("不用重新注册", res["message"])

    def test_勾选存进了_schedule_json(self):
        schedule.set_automation_steps(self.root, "c.yaml", ["dump", "reconcile"])
        raw = schedule.record_path(self.root).read_text(encoding="utf-8")
        self.assertIn("reconcile", raw)

    def test_记录优先于从脚本反推(self):
        """提权注册的任务普通权限读不到，界面靠这份记录 ——
        所以记录和脚本不一致时**信记录**。"""
        schedule.set_automation_steps(self.root, "c.yaml", ["pos"])
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("dump",))
        self.assertEqual(schedule.automation_steps(self.root), ("pos",))

    def test_没记录时从脚本反推(self):
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("reconcile",))
        self.assertEqual(schedule.automation_steps(self.root), ("reconcile",))

    def test_老格式的_what_也读得出来(self):
        """升级期：记录里可能还留着 `what: "all"` 这种单值格式。
        读不出来的话界面会显示成默认值 —— 用户改的勾选"自己变回去了"。"""
        from src import schedule as sc
        p = sc.record_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"t": {"what": "reconcile", "at": "x"}}', encoding="utf-8")
        self.assertEqual(schedule.automation_steps(self.root),
                         run_daily.BUTTON_STEPS["reconcile"])

    def test_记录丢了就当默认全跑(self):
        self.assertEqual(schedule.automation_steps(self.root),
                         tuple(run_daily.AUTOMATION_DEFAULT_STEPS))

    def test_改勾选保住目标日(self):
        schedule.write_runner_script(self.root, "c.yaml", 3, steps=("dump",))
        schedule.set_automation_steps(self.root, "c.yaml", ["pos"])
        body = schedule.script_path(self.root).read_text(encoding="utf-8")
        self.assertIn("--days-ago 3", body)

    def test_自愈重建也要保住勾选(self):
        """`refresh_runner_scripts` 是概览页触发的 —— 它要是把勾选冲掉，
        表现就是"打开一次界面，设置自己变回默认了"。"""
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("pos",))
        p = schedule.script_path(self.root)
        p.write_text(p.read_text(encoding="utf-8").replace(schedule.RUNNER_MARK,
                                                           "rem cbg-runner v0"),
                     encoding="utf-8")
        self.assertTrue(schedule.refresh_runner_scripts(self.root, "c.yaml"))
        self.assertEqual(schedule.existing_steps(self.root), ("pos",))

    def test_定时任务不勾抓数据就不抓(self):
        """⚠ 这是**用户明确要的**：抓数据是能取消的选项（默认勾上）。

        取消之后库不会更新 —— 那是用户的选择，程序**照做**；
        库过期时报量排查会以「库不新鲜」**明确失败**，不会静默算错。
        """
        schedule.write_runner_script(self.root, "c.yaml", 1,
                                     steps=("reconcile", "pos"))
        body = schedule.script_path(self.root).read_text(encoding="utf-8")
        self.assertIn("--skip-dump", body)
        self.assertNotIn("--skip-check", body)


class TestAutomationLabel(unittest.TestCase):
    """「跑什么」的文字 —— **列出执行项目**，不是给一句概括。

    写「整个项目」门店看不懂那指什么。
    """

    def test_列出每一天实际干的几件事(self):
        self.assertEqual(run_daily.steps_label(("dump", "reconcile", "pos")),
                         "抓华为数据 + 报量排查 + POS 合规")
        self.assertEqual(run_daily.steps_label(("dump", "reconcile")),
                         "抓华为数据 + 报量排查")
        self.assertEqual(run_daily.steps_label(("dump",)),
                         "抓华为数据")

    def test_一件都不选要抛(self):
        with self.assertRaises(ValueError):
            run_daily.steps_label(())

    def test_按钮的四个预设都说得出来(self):
        for what in run_daily.BUTTON_STEPS:
            with self.subTest(what=what):
                self.assertTrue(run_daily.steps_label(run_daily.BUTTON_STEPS[what]).strip())


class TestAutomationChoicesMatch(unittest.TestCase):
    """`schedule.AUTOMATION_CHOICES` 和 `run_daily` 是两份 ——
    有测试盯着，免得改了一处另一处静默用旧的。"""

    def test_两边一致(self):
        self.assertEqual(tuple(schedule.AUTOMATION_CHOICES),
                         tuple(run_daily.AUTOMATION_DEFAULT_STEPS))


class TestTargetDateOnlyAffectsReconcile(unittest.TestCase):
    """⚠ 「目标日」和「高级（时间窗容差）」**只影响报量排查**（用户 2026-09-16 确认）。

    * **抓华为数据**固定抓**当月**（没有本地库时补全部历史）—— 它没有"某一天"的概念；
    * **POS 合规**按**整月**算 —— 一个月一个分数，也没有"某一天"。

    这条不只是文案：界面上的下拉框是**三个按钮共用**的，
    要是哪天有人"顺手"把 `days_ago` 也转发给 dump/pos，
    用户选了"今天"再点 POS，会以为 POS 只算了今天 —— 而它其实算了整月，
    **而且不会有任何提示**。所以按"传进去的 Namespace 长什么样"钉死。
    """

    def _seen(self, extra):
        import contextlib
        import io
        seen = {}

        def mk(k):
            def f(a):
                seen[k] = a
                return 0
            return f
        buf = io.StringIO()
        with mock.patch.object(cli, "cmd_dump", mk("dump")), \
             mock.patch.object(cli, "cmd_check", mk("check")), \
             mock.patch.object(cli, "cmd_pos", mk("pos")), \
             mock.patch.object(cli, "_find_pos_db",
                               return_value=Path("/tmp/cbg-2026.db")), \
             contextlib.redirect_stdout(buf):
            run_daily.main(extra)
        return seen

    def test_抓华为数据收不到目标日(self):
        seen = self._seen(["--date", "2026-09-10", "--lookback", "3", "--lookahead", "1"])
        for attr in ("date", "days_ago", "lookback", "lookahead"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(seen["dump"], attr),
                                 "dump 不该收到 %s —— 它固定抓当月" % attr)

    def test_抓华为数据只看当月或全量(self):
        seen = self._seen(["--date", "2026-09-10"])
        self.assertTrue(hasattr(seen["dump"], "month"))
        self.assertTrue(hasattr(seen["dump"], "all"))

    def test_POS_收不到目标日(self):
        seen = self._seen(["--date", "2026-09-10", "--lookback", "3", "--lookahead", "1"])
        for attr in ("date", "days_ago", "lookback", "lookahead"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(seen["pos"], attr),
                                 "pos 不该收到 %s —— 它按整月算" % attr)

    def test_POS_只收到库路径(self):
        seen = self._seen([])
        self.assertEqual(vars(seen["pos"]), {"db": ""})

    def test_报量排查照单全收(self):
        seen = self._seen(["--date", "2026-09-10", "--lookback", "3", "--lookahead", "1"])
        self.assertEqual(seen["check"].date, "2026-09-10")
        self.assertEqual(seen["check"].lookback, 3)
        self.assertEqual(seen["check"].lookahead, 1)

    def test_显式日期时不给_days_ago(self):
        """两个都给的话谁赢不确定 —— 显式日期优先。"""
        seen = self._seen(["--date", "2026-09-10"])
        self.assertIsNone(seen["check"].days_ago)


class TestRunPanelSaysTargetDateScope(unittest.TestCase):
    """界面得**在「目标日」旁边**说清它只管报量排查（用户提的）。

    塞在卡片标题里等于没说 —— 用户盯着那个下拉框的时候，
    视线不会跑到标题上去。
    """

    def _panel(self):
        i = INDEX_HTML.index('id="panel-run"')
        j = INDEX_HTML.index('id="panel-sessions"', i) if 'id="panel-sessions"' in INDEX_HTML \
            else INDEX_HTML.index('id="panel-session"', i)
        return INDEX_HTML[i:j]

    def test_运行页说清了目标日只管报量排查(self):
        panel = self._panel()
        self.assertIn("只影响", panel)
        self.assertIn("报量排查", panel)
        self.assertIn("当月", panel)      # 抓华为数据是固定当月
        self.assertIn("整月", panel)      # POS 是按整月

    def test_提示紧跟在目标日下拉框后面(self):
        """⚠ 位置也是需求的一部分 —— 隔着一整个 <details> 就等于没说。"""
        i = INDEX_HTML.index('id="run-date"')
        j = INDEX_HTML.index('id="run-lookback"')
        between = INDEX_HTML[i:j]
        self.assertIn("只影响", between,
                      "「目标日」和「高级」之间没有那句说明 —— 提示又跑到拿不到的地方去了")

    def test_高级项也说清了同理(self):
        panel = self._panel()
        i = panel.index("时间窗容差")
        self.assertIn("只影响", panel[i:])


class TestTaskNameAndLegacy(unittest.TestCase):
    """定时任务**改过名**（用户 2026-09-16 提的）。

    以前叫 `CBG报量对账`，现在叫 `门店数据拉取与计算` —— 因为它现在每天干三件事
    （抓数据 → 报量排查 → POS 合规），还叫"报量对账"名不副实。

    ⚠ 改名的代价：Windows 那边**不同名就是并存，不是覆盖** ——
    老门店升级后表里会有两条，**两条都会每天跑一遍**。
    所以必须能认出老名字（否则界面上那句提醒根本不会出现）。
    """

    def test_新名字(self):
        self.assertEqual(schedule.TASK_NAME, "门店数据拉取与计算")

    def test_默认任务名带执行时间(self):
        self.assertEqual(schedule.default_task_name("21:00"), "门店数据拉取与计算-21点00")
        self.assertEqual(schedule.default_task_name("09:05"), "门店数据拉取与计算-9点05")

    def test_还认得出老名字(self):
        """⚠ 认不出 = 那条旧任务在界面上是个"外人"：删不掉、
        也拿不到"你还有一条旧任务在跑"的提醒，于是一天跑两遍。"""
        for old in ("CBG报量对账", "CBG报量对账-21点00", "CBG报量对账-中午"):
            with self.subTest(old=old):
                self.assertTrue(schedule._is_ours(old), "%s 认不出来了" % old)
        self.assertFalse(schedule._is_ours("别的软件的任务"))

    def test_开机自启那个不算我们的定时任务(self):
        from src import autostart
        self.assertFalse(schedule._is_ours(autostart.AUTOSTART_TASK))

    def test_给界面带上了老名字清单(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(schedule, "kind", lambda: "windows"), \
                 mock.patch.object(schedule, "_win_status",
                                   lambda root: {"tasks": [{"name": "x", "time": "21:00"}]}):
                st = schedule.status(Path(d))
        self.assertIn("CBG报量对账", st["legacy_names"])

    def test_crontab_里老名字也不会重复(self):
        """Windows 那边是"并存"，crontab 这边是**替换** ——
        判据得认老前缀，否则同一件事在 crontab 里躺两份。"""
        self.assertTrue(schedule._same_task("CBG报量对账-18点00",
                                            "门店数据拉取与计算-18点00"))
        self.assertTrue(schedule._same_task("门店数据拉取与计算-18点00",
                                            "门店数据拉取与计算-18点00"))
        self.assertFalse(schedule._same_task("CBG报量对账-18点00",
                                             "门店数据拉取与计算-12点00"))
        self.assertFalse(schedule._same_task("别的-18点00",
                                             "门店数据拉取与计算-18点00"))


class TestScheduleTableColumns(unittest.TestCase):
    """表列：「跑什么」放的是命令（里面有 run.bat 的**路径**）—— 名不副实。

    用户点出来了，所以：那一列改名「**路径**」，另起一列「**跑什么**」
    写**执行项目**。
    """

    def _cols(self):
        """定时任务那张表的列标题。

        ⚠ 必须**锚定到含「任务名」的那一处** —— app.js 里有好几处 `table([...])`，
        `re.search` 找的是**第一个**，第一版就匹到了别的表上，
        于是断言"找不到「路径」"（而它其实在）。
        """
        import re
        i = APP_JS.index("'任务名'")
        j = APP_JS.index("]", i)
        return re.findall(r"'([^']*)'", APP_JS[i:j])

    def test_有路径这一列(self):
        self.assertIn("路径", self._cols())

    def test_有跑什么这一列(self):
        self.assertIn("跑什么", self._cols())

    def test_跑什么排在路径后面(self):
        cols = self._cols()
        self.assertLess(cols.index("路径"), cols.index("跑什么"))

    def test_表格用的是后端的文案(self):
        """「跑什么」那一格的内容来自 `t.what_label`（后端 `automation_label`），
        **前端不另写一份** —— 各写一份必然有一天对不上。"""
        self.assertIn("t.what_label", APP_JS)

    def test_跑什么那格不被转义成源码(self):
        """⚠ `table()` 对**字符串**单元格默认 esc()，要放 HTML 得包 `{html: ...}`
        （这个坑踩过三次）。这一格是纯文字，所以直接给字符串就行 ——
        但不能写成 `{ html: ... }` 里再套 esc（那样会双重转义）。"""
        import re
        seg = APP_JS[APP_JS.index("t.what_label") - 120:APP_JS.index("t.what_label") + 60]
        self.assertIn("{ html:", seg)


if __name__ == "__main__":
    unittest.main()
