"""「跑什么」（`what`）这条线的测试 —— 界面按钮 / 定时任务勾选 / run.bat。

**为什么单独钉**：`what` 从界面一路穿到 `run.bat`，中间隔了四跳
（`/api/run` → `runner.start` → `daily` 子命令 → `run_daily.main`）。
任何一跳漏转发，表现都是**静默跑错东西** —— 勾了"只算 POS"结果把数据也抓了。
这种错不会报错，只会让人某天觉得"怎么这么慢"。

用户 2026-09-16 定的：
* 运行页四个按钮：整个项目 / 抓四池数据 / 报量排查 / POS 合规；
* 单独执行的按钮**不顺手抓数据**（抓数据要一两分钟起）；
* 设置里勾「自动化跑什么」，两个都不勾是**错的**（不许静默当成"都跑"）。

⚠ 用户 2026-09-17 实测后又定：**运行页只留「整个项目」一个按钮**，
「目标日」「高级（时间窗容差）」整块删掉（`dump` / `pos` 两个预设的定义留着，
`/api/run` 和老记录还认它们）。
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from src import cli, run_daily, runner, schedule

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


class TestWhatFlags(unittest.TestCase):
    def test_三个预设都在(self):
        # ⚠ 四池**故意不给单独按钮** —— 前端 index.html 里没有 data-what="pools"，
        #   它是「整个项目」的第 3 步（`STEPS` 里有，`BUTTON_STEPS` 里没有）。
        #   `dump` / `pos` 也没有按钮了，但**预设本身留着** ——
        #   `/api/run` 还认，老 `.secrets/schedule.json` 里 `what: "pos"` 也靠它。
        self.assertEqual(set(run_daily.BUTTON_STEPS), {"all", "dump", "pos"})

    def test_整个项目不加任何跳过开关(self):
        self.assertEqual(run_daily.flags_for("all"), [])

    def test_每个单独执行都跳过另外两件事(self):
        """⚠ 「单独执行」的语义就是**只做那一件** —— 不许顺手把别的也跑了。"""
        want = {
            "dump": {"--skip-pos", "--skip-pools"},
            "pos": {"--skip-dump", "--skip-pools"},
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
        self.assertEqual(calls, ["dump", "pos"])
        self.assertEqual(rc, 0)

    def test_只抓数据(self):
        rc, calls, _ = self._run(["--skip-check", "--skip-pos", "--skip-pools"])
        self.assertEqual(calls, ["dump"])

    def test_只报量排查(self):
        rc, calls, _ = self._run(["--skip-dump", "--skip-pos", "--skip-pools"])
        self.assertEqual(calls, [])

    def test_只算_POS(self):
        rc, calls, _ = self._run(["--skip-dump", "--skip-check"])
        self.assertEqual(calls, ["pos"])

    def test_全跳过什么都不做也不报错(self):
        rc, calls, _ = self._run(["--skip-dump", "--skip-check", "--skip-pos", "--skip-pools"])
        self.assertEqual(calls, [])
        self.assertEqual(rc, 0)

    def test_表头写清这次要跑哪几件(self):
        _, _, out = self._run(["--skip-dump"])
        self.assertIn("四池对账", out)
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

    def _argv(self, what):
        m = runner.RunManager()
        d = Path("/tmp/x")
        j = m.start(d, "config/store-X.yaml", what=what)
        j.kill()
        j.running = False
        return " ".join(j.argv[j.argv.index("daily") + 1:])

    def test_每个_what_都走_daily_这一个入口(self):
        """⚠ 各按钮走不同命令的话，`daily` 那些行为
        （第 1 步失败就不发、库里没有就抓全量、会话失效先静默续期）
        在界面上就全都享受不到了，而且"界面跑的"和"定时任务跑的"迟早分叉。"""
        for what in run_daily.BUTTON_STEPS:
            with self.subTest(what=what):
                m = runner.RunManager()
                j = m.start(Path("/tmp/x"), "c.yaml", what=what)
                self.assertIn("daily", j.argv)
                j.kill()
                j.running = False

    def test_argv_里的开关跟_BUTTON_STEPS_一致(self):
        # ⚠ `--skip-check` 没了：报量排查整步拿掉，那个开关已废弃。
        # ⚠ `--days-ago` 也不再拼了（2026-09-17）：它不影响任何一步，
        #   拼上去只会让日志里那条命令看着像"界面上有个目标日"。
        self.assertEqual(self._argv("dump"), "--skip-pos --skip-pools")
        self.assertEqual(self._argv("pos"), "--skip-dump --skip-pools")
        self.assertEqual(self._argv("all"), "")

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

    ⚠ **可选项只有两项**：POS 合规 / 四池对账 —— 用户 2026-09-17 把
    「抓四池数据」的复选框**拿掉了**（「默认执行这个。不可选」），
    它由 `run_daily.ALWAYS_STEPS` 无条件补上，谁取消都补回来。

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

    def test_两项可选_抓数据是必做(self):
        self.assertEqual(tuple(schedule.AUTOMATION_CHOICES), ("pos", "pools"))
        self.assertIn("dump", run_daily.ALWAYS_STEPS)
        self.assertEqual(tuple(run_daily.AUTOMATION_DEFAULT_STEPS),
                         ("dump", "pos", "pools"))

    def test_抓数据任何组合下都要执行(self):
        """⚠ 用户 2026-09-16 要求"默认执行抓数据"，2026-09-17 更进一步：**不可选**。

        不抓的话库永远是旧的，POS 和四池对账都只是**拿旧数据在算** ——
        而日志只会说"跑完了"，这是最难发现的一类错。

        遍历**所有**可选项组合（含一个都不勾），断言生成的 run.bat 里
        **永远不出现 `--skip-dump`**。
        """
        import itertools
        picks = [()]
        for n in range(1, len(schedule.AUTOMATION_CHOICES) + 1):
            picks += list(itertools.combinations(schedule.AUTOMATION_CHOICES, n))
        for picked in picks:
            with self.subTest(picked=picked):
                schedule.write_runner_script(self.root, "c.yaml", 1, steps=picked)
                body = schedule.script_path(self.root).read_text(encoding="utf-8")
                self.assertNotIn("--skip-dump", body,
                                 "抓数据被跳过了（picked=%r）" % (picked,))

    def test_两个都不勾就是只抓数据(self):
        """⚠ 用户 2026-09-17 定的：**允许**两个都不勾 ——
        含义是「每天只抓数据，不算也不推」。

        原来那条"至少勾一项"防的是"取消了勾选、结果照样推"；
        现在的行为严格照着勾选走（**空 + 必做**），不会静默扩大。
        """
        self.assertEqual(schedule.steps_from_choices([]), ("dump",))
        self.assertEqual(schedule.steps_from_choices(None), ("dump",))
        # 界面上两个框都不点亮 —— 这是对的，不是 bug
        self.assertEqual(schedule.choices_from_steps(("dump",)), [])

    def test_乱勾要抛(self):
        with self.assertRaises(ValueError):
            schedule.steps_from_choices(["nonsense"])

    def test_顺序不同算同一种(self):
        self.assertEqual(schedule.steps_from_choices(["pos", "dump"]),
                         ("dump", "pos"))

    def test_所有组合生成的开关都对(self):
        """遍历**全部可选项**子集，断言写进脚本的开关 = `flags_for_steps` 的输出。

        ⚠ 原来手写 7 个组合 —— 加一件就得回来补一倍的行，迟早漏。
        ⚠ 遍历的是 `AUTOMATION_CHOICES`（**可选项**）而不是 `STEPS`：
          `dump` 取消不掉（`ALWAYS_STEPS`），把它算进组合里去试
          `--skip-dump` 是在测一个**不存在**的状态。
        """
        import itertools
        for n in range(0, len(schedule.AUTOMATION_CHOICES) + 1):
            for picked in itertools.combinations(schedule.AUTOMATION_CHOICES, n):
                with self.subTest(picked=picked):
                    schedule.write_runner_script(self.root, "c.yaml", 1, steps=picked)
                    body = schedule.script_path(self.root).read_text(encoding="utf-8")
                    line = [x for x in body.splitlines() if "daily" in x][0]
                    rest = line.split("daily", 1)[1]
                    want = run_daily.flags_for_steps(run_daily.with_always(picked))
                    for flag in want:
                        self.assertIn(flag, rest)
                    for flag in ("--skip-dump", "--skip-check",
                                 "--skip-pos", "--skip-pools"):
                        if flag not in want:
                            self.assertNotIn(flag, rest)

    def test_步骤定义被钉住(self):
        """步骤是**全项目唯一的定义**，加一件必须是有意识的决定。

        这条**故意硬编码** —— 改它就意味着"确实要加一步"。
        """
        self.assertEqual(run_daily.STEPS, ("dump", "pos", "pools"))

    def test_反推能读回来(self):
        """重建脚本时要**保住勾选** —— 跟 `--days-ago` 一个道理。
        读不回来的话，一次界面自愈就把勾选悄悄改回默认。

        ⚠ 读回来的**一定带着 `dump`**（它必做）：写进去 `("pos",)`，
          读出来是 `("dump", "pos")` —— 这是特性不是 bug。
        """
        for picked, want in ((("dump",), ("dump",)),
                             ((), ("dump",)),
                             (("pos",), ("dump", "pos")),
                             (("pools",), ("dump", "pools")),
                             (("pos", "pools"), ("dump", "pos", "pools"))):
            with self.subTest(picked=picked):
                schedule.write_runner_script(self.root, "c.yaml", 1, steps=picked)
                self.assertEqual(schedule.existing_steps(self.root), want)

    def test_老脚本里的_skip_dump_要补回来(self):
        """⚠ **升级路径**：抓数据以前是能取消的，门店的 run.bat 里可能
        真带着 `--skip-dump`。不补的话那份脚本会一直生效，库永远不更新。

        造一份带 `--skip-dump` 的老脚本 → `existing_steps` 必须补回 `dump`。
        """
        p = schedule.script_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(schedule.RUNNER_MARK + "\n"
                     '"py" -m src.cli -c "c.yaml" daily --skip-dump --skip-pools\n',
                     encoding="utf-8")
        self.assertIn("dump", schedule.existing_steps(self.root))

    def test_手工脚本也要跟着改(self):
        """run-now.bat 是给人手动双击的 —— 不改的话手动跑的还是老范围。"""
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("pools",))
        now = schedule.manual_script_path(self.root).read_text(encoding="utf-8")
        self.assertIn("--skip-pos", now)

    def test_保存勾选_不重新注册任务(self):
        """⚠ 改勾选**不用碰 Windows 任务** —— 任务跑的是 run.bat，
        改的是它的内容。重新注册有可能弹 UAC，为了改个勾选弹框不值得。"""
        with mock.patch.object(schedule, "install") as inst:
            res = schedule.set_automation_steps(self.root, "c.yaml", ["pos"])
        self.assertEqual(inst.call_count, 0)
        self.assertTrue(res["ok"])
        self.assertIn("不用重新注册", res["message"])

    def test_勾选存进了_schedule_json(self):
        schedule.set_automation_steps(self.root, "c.yaml", ["pos"])
        raw = schedule.record_path(self.root).read_text(encoding="utf-8")
        self.assertIn("pos", raw)
        # ⚠ 报量排查拿掉之后再写 reconcile 进去就是错的
        self.assertNotIn("reconcile", raw)

    def test_记录优先于从脚本反推(self):
        """提权注册的任务普通权限读不到，界面靠这份记录 ——
        所以记录和脚本不一致时**信记录**。"""
        schedule.set_automation_steps(self.root, "c.yaml", ["pos"])
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("pools",))
        self.assertEqual(schedule.automation_steps(self.root), ("dump", "pos"))

    def test_没记录时从脚本反推(self):
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("pos",))
        self.assertEqual(schedule.automation_steps(self.root), ("dump", "pos"))

    def test_老格式的_what_也读得出来(self):
        """升级期：记录里可能还留着 `what: "all"` 这种单值格式。
        读不出来的话界面会显示成默认值 —— 用户改的勾选"自己变回去了"。

        ⚠ 顺带**补上 `dump`**：那些 `what` 是界面按钮时代的产物
          （那时能只跑一件事），现在的定时任务必须抓数据。
        """
        from src import schedule as sc
        p = sc.record_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"t": {"what": "pos", "at": "x"}}', encoding="utf-8")
        self.assertEqual(schedule.automation_steps(self.root), ("dump", "pos"))

    def test_老记录里没有_dump_也要补回来(self):
        """⚠ 抓数据以前是**可取消**的，老 `.secrets/schedule.json` 里
        真可能存着 `["pos"]`。不补的话那份记录会一直生效，库永远不更新。"""
        from src import schedule as sc
        p = sc.record_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"t": {"steps": ["pos", "pools"], "at": "x"}}',
                     encoding="utf-8")
        self.assertEqual(schedule.automation_steps(self.root),
                         ("dump", "pos", "pools"))

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
        self.assertEqual(schedule.existing_steps(self.root), ("dump", "pos"))

    def test_定时任务永远不跳过抓数据(self):
        """⚠ 这是**用户 2026-09-17 明确要的**：抓数据不可选。

        （原来那条 `test_定时任务不勾抓数据就不抓` 是反过来的 ——
         那时抓数据是能取消的选项。现在取消不掉了，那条规矩作废。）
        """
        schedule.write_runner_script(self.root, "c.yaml", 1, steps=("pos",))
        body = schedule.script_path(self.root).read_text(encoding="utf-8")
        self.assertNotIn("--skip-dump", body)
        self.assertNotIn("--skip-check", body)
        self.assertIn("--skip-pools", body)


class TestAutomationLabel(unittest.TestCase):
    """「跑什么」的文字 —— **列出执行项目**，不是给一句概括。

    写「整个项目」门店看不懂那指什么。
    """

    def test_列出每一天实际干的几件事(self):
        self.assertEqual(run_daily.steps_label(("dump", "pos")),
                         "抓四池数据 + POS 合规")
        self.assertEqual(run_daily.steps_label(("pos", "pools")),
                         "POS 合规 + 四池对账")
        self.assertEqual(run_daily.steps_label(("dump",)),
                         "抓四池数据")

    def test_一件都不选要抛(self):
        with self.assertRaises(ValueError):
            run_daily.steps_label(())

    def test_按钮的四个预设都说得出来(self):
        for what in run_daily.BUTTON_STEPS:
            with self.subTest(what=what):
                self.assertTrue(run_daily.steps_label(run_daily.BUTTON_STEPS[what]).strip())


class TestAutomationChoicesMatch(unittest.TestCase):
    """`schedule.AUTOMATION_CHOICES` 和 `run_daily` 是两份 ——
    有测试盯着，免得改了一处另一处静默用旧的。

    ⚠ 2026-09-17 起这两份**不再相等**：可选项 = 全部步骤 − 必做项
    （「抓四池数据」的复选框被用户拿掉了）。所以要盯的是**这个式子**，
    不是"两份字符串一样"。
    """

    def test_可选项等于全部步骤减去必做项(self):
        want = tuple(s for s in run_daily.AUTOMATION_DEFAULT_STEPS
                     if s not in run_daily.ALWAYS_STEPS)
        self.assertEqual(tuple(schedule.AUTOMATION_CHOICES), want)

    def test_必做项本身也在默认里(self):
        """⚠ `dump` 必须同时是"默认跑"和"必做" ——
        哪天有人把它从 `AUTOMATION_DEFAULT_STEPS` 里删了，
        补回来的那一步会**排在最后**（`with_always` 按 `STEPS` 排序），
        命令字符串就跟预期不一样了。"""
        for s in run_daily.ALWAYS_STEPS:
            with self.subTest(step=s):
                self.assertIn(s, run_daily.AUTOMATION_DEFAULT_STEPS)


class TestSettingsCopy(unittest.TestCase):
    """设置页「定时执行」那段说明要跟可选项对得上。

    ⚠ 报量排查拿掉之后文案还写着「默认**四**件都勾」，而勾选框只有三个 ——
    门店会以为程序少跑了一件。**数字不要手写，从代码里的定义推。**
    """

    _CN = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}

    def _sched_card(self):
        i = INDEX_HTML.index("<h2>定时执行</h2>")
        j = INDEX_HTML.index("</section>", i)
        return INDEX_HTML[i:j]

    def test_说明写清了抓数据不用选(self):
        """⚠ 抓数据的复选框被用户 2026-09-17 拿掉了 ——
        说明里必须写清"每次都跑、取消不掉"，否则用户会满界面找那个不存在的框。"""
        card = self._sched_card()
        self.assertIn("抓四池数据", card)
        self.assertIn("每次都会跑", card)

    def test_不再说不许一件都不勾(self):
        """⚠ 用户 2026-09-17 定的：两个都不勾 = 「只抓数据」，**允许**。
        文案里再留着"一件都不勾是不允许的"就是骗人。"""
        self.assertNotIn("不允许", self._sched_card())

    def test_可选项的名字都在说明里出现过(self):
        card = self._sched_card()
        for s in schedule.AUTOMATION_CHOICES:
            with self.subTest(step=s):
                self.assertIn(run_daily.STEP_LABELS[s], card)

    def test_不再提已经删掉的目标日下拉(self):
        """⚠ 那句「选『昨天』零遗漏；选『今天』…」是对着一个**已经删掉的下拉框**
        说话（`sched-days-ago` 2026-09-17 拿掉）。留着门店会满页面找一个不存在的选项。"""
        card = self._sched_card()
        self.assertNotIn("选「昨天」", card)
        self.assertNotIn("目标日", card)


class TestTargetDateOnlyAffectsReconcile(unittest.TestCase):
    """⚠ 「目标日」和「高级（时间窗容差）」**只影响报量排查**（用户 2026-09-16 确认）。

    * **抓四池数据**固定抓当月（第一次跑时补今年至今）—— 它没有"某一天"的概念；
    * **POS 合规**按**整月**算 —— 一个月一个分数，也没有"某一天"。

    这条不只是文案：要是哪天有人"顺手"把 `days_ago` 也转发给 dump/pos，
    用户会以为 POS 只算了今天 —— 而它其实算了整月，**而且不会有任何提示**。
    所以按"传进去的 Namespace 长什么样"钉死。

    ⚠ **2026-09-17**：报量排查整步没了 ⇒ 这两块**界面上直接删掉**、
    `/api/run` 也不再收那五个字段。`daily` 命令行上那四个参数**留着**
    （老 run.bat / 计划任务里写死着），所以下面几条照旧有效。
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

    def test_抓四池数据收不到目标日(self):
        seen = self._seen(["--date", "2026-09-10", "--lookback", "3", "--lookahead", "1"])
        for attr in ("date", "days_ago", "lookback", "lookahead"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(seen["dump"], attr),
                                 "dump 不该收到 %s —— 它固定抓当月" % attr)

    def test_抓四池数据只看当月或全量(self):
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


class TestRunPageOnlyOneButton(unittest.TestCase):
    """运行页现在只有「整个项目」一个按钮（用户 2026-09-17 定的）。

    ⚠ 「目标日」和「高级（时间窗容差）」是**删掉**的，不是藏起来 ——
    它们对应的五个字段一个都不生效，留着就是"能选、选了没用"的旋钮。
    这条按源码钉：前端**没有构建步骤、没有 lint**，`$('#run-mode')` 这种
    引用留着只会在浏览器控制台里报一行 TypeError，跑测试和跑服务都看不见。
    """

    def _panel(self):
        i = INDEX_HTML.index('id="panel-run"')
        j = INDEX_HTML.index('id="panel-sessions"', i) if 'id="panel-sessions"' in INDEX_HTML \
            else INDEX_HTML.index('id="panel-session"', i)
        # ⚠ **先去掉 HTML 注释再断言"没有 X"** —— 那块删掉的说明我们留在了注释里
        #   （"为什么删"比"删了什么"更值钱），不去掉的话注释自己会把断言顶掉。
        return re.sub(r"<!--.*?-->", "", INDEX_HTML[i:j], flags=re.S)

    def test_只剩整个项目一个按钮(self):
        panel = self._panel()
        self.assertEqual(re.findall(r'data-what="([^"]+)"', panel), ["all"])

    def test_按钮的行内说明还在(self):
        """按钮少了，但"这一下到底跑什么"必须写在旁边 ——
        用户点到的是一个笼统的「整个项目」。"""
        panel = self._panel()
        for word in ("抓四池数据", "POS 合规", "四池对账"):
            with self.subTest(word=word):
                self.assertIn(word, panel)

    def test_目标日和高级整块没了(self):
        panel = self._panel()
        for gone in ("目标日", "时间窗容差", "run-mode", "run-date",
                     "run-lookback", "run-lookahead"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, panel, "「%s」还在运行页上" % gone)

    def test_app_js_里也不许再引用这些_id(self):
        """⚠ 引用一个不存在的 id，`$('#run-mode').value` 会在**加载时**抛
        TypeError —— 后面的绑定全部不执行，整个界面变哑巴。"""
        for gone in ("#run-mode", "#run-date", "#run-lookback", "#run-lookahead"):
            with self.subTest(gone=gone):
                self.assertNotIn("'%s'" % gone, APP_JS)

    def test_请求体里只有_what(self):
        """⚠ 那五个字段以前是**前端拼好、后端拿去拼命令**的。现在两边都不碰 ——
        老页面（刷新前还在跑旧 JS）多传也不会 400，后端直接忽略。"""
        i = APP_JS.index("async function startRun")
        j = APP_JS.index("function appendLog", i)
        seg = APP_JS[i:j]
        self.assertIn("body: { what }", seg)
        for gone in ("#run-mode", "#run-date", "#run-lookback", "#run-lookahead",
                     "body.lookback", "body.lookahead", "body.mode", "body.date"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, seg)


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
