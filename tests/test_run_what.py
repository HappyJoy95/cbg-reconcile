"""「跑什么」这条线的测试 —— **点名跑**（`--steps`）。

**为什么单独钉**：跑错东西是**静默**的 —— 少点一步，那天就少跑一件事，
日志里看着还挺正常；多点一步，可能真发了一封不该发的邮件（发出去收不回来）。

⭐ 这一段的口径被改过三次，现在是**第三版**（2026-09-21 晚，用户：
「**现在不需要 run daily 吧，按定时器运行就行了**」）：

| | 跑什么由谁定 |
|---|---|
| 1️⃣ 2026-09-16 | 界面四个按钮（整个项目 / 抓取玲珑数据 / 报量排查 / POS 合规）|
| 2️⃣ 2026-09-17 → 09-21 | 只剩「整个项目」；`/api/run` 的 `what` 预设 + `--skip-*` 组合出"整批" |
| 3️⃣ **现在** | **一律点名**：`daily --steps …`。到点由内置定时器按每一步自己的时刻派发，各页「刷新」按页点名，门店双击的 `run-now.bat` 也点名 |

* `BUTTON_STEPS` / `BUTTON_LABELS` / `flags_for` / `run_one` / `DEFAULT_WHAT`
  和 `runner.start()`、`POST /api/run` **全删了** —— "整批"这个模式不存在了；
* `--skip-*` 参数**还收**（老脚本兼容），但只剩"提醒一句它没用了"的作用；
* ⚠ 别再往回加"不给 `--steps` 就跑一大套"的默认：那正是"三处各说一套"的来源。
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from src import cli, run_daily, runner, schedule
from src.app.pos import PosRun

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


class Test预设层已经删掉(unittest.TestCase):
    """⚠ 2026-09-21 晚：**"手动跑整批"这一层整个删了**。

    它由三块拼出来：界面上那张「跑一次」的卡（先删）、`/api/run` 的 `what` 预设
    （`BUTTON_STEPS` / `BUTTON_LABELS` / `flags_for` / `run_one`），
    以及 `runner.start()`。三块是**一件事的三个入口** ——
    留一个没删，下一个人就会照着它把另外两个加回来。

    这条类就是那把锁：**谁把它们加回来，这里当场红**。
    """

    def test_run_daily_里的预设函数都没了(self):
        for gone in ("BUTTON_STEPS", "BUTTON_LABELS", "DEFAULT_WHAT",
                     "flags_for", "flags_for_steps", "run_one",
                     "with_always", "ALWAYS_STEPS", "AUTOMATION_DEFAULT_STEPS"):
            with self.subTest(gone=gone):
                self.assertFalse(hasattr(run_daily, gone),
                                 "%s 又回来了？" % gone)

    def test_runner_的预设入口也没了(self):
        self.assertFalse(hasattr(runner.RunManager, "start"),
                         "`runner.start(what=…)` 又回来了？（现在只有 start_steps / start_argv）")
        self.assertTrue(hasattr(runner.RunManager, "start_steps"))
        self.assertTrue(hasattr(runner.RunManager, "start_argv"))

    def test_手动那份脚本点名的是每天那趟(self):
        """⭐ `MANUAL_STEPS` = 注册表里 `default=True` 的那几步 ——
        "双击手动跑"跑的就是定时器到点会跑的那一套（不多不少）。
        """
        from src.features import registry
        self.assertEqual(tuple(run_daily.MANUAL_STEPS), registry.default_steps())
        self.assertNotIn("autoupdate", run_daily.MANUAL_STEPS,
                         "自动更新按小时自己跑，不该被手动那一份捎上")
        self.assertIn("dump", run_daily.MANUAL_STEPS,
                      "不抓数的话后面全是拿旧数据在算（用户定的红线）")


class TestDailySkipFlags(unittest.TestCase):
    """`daily --steps` —— **点名跑哪几件**（这是唯一的入口）。

    ⚠ 这一类的名字和历史都跟 `--skip-*` 有关（那时是"整批里跳过谁"）——
      口径 2026-09-21 晚改了：**点名**，没点名的就是不跑。
      老用例里"跳过某步"的意思现在写成"不点某步"，用 `_steps_except()`。
    """

    @staticmethod
    def _steps_except(*skip):
        """点名跑"每天那趟"里除了这几步之外的 —— 代替原来的 `--skip-xxx`。"""
        return ["--steps", ",".join(x for x in run_daily.MANUAL_STEPS if x not in skip)]

    def _run(self, extra):
        # ⚠ 2026-09-20 加了 `erp-dump`（抓云商数据）—— **也要有桩**，
        #   不挡的话这条链会真去登云商拉明细（网络！）。
        calls, codes = [], {"dump": 0, "check": 0, "pos": 0, "pools": 0, "attain": 0,
                            "erp-dump": 0}

        def mk(k):
            def f(_a):
                calls.append(k)
                return codes[k]
            return f
        import contextlib
        import io
        buf = io.StringIO()

        # ⚠ 2026-09-19 起 POS 走**执行模块**（`app.pos.run`），不再经 CLI ——
        #   桩要按新契约回 `PosRun`（返回整数会让 `res.ok` 直接炸）。
        #   ⚠ 这个定义**必须放在 with 之前**：`with A, \` 续行链里插注释是语法错。
        def mk_attain():
            def fn(**_kw):
                calls.append("attain")
                return {"ok": True}
            return fn

        def mk_pos():
            def fn(**_kw):
                calls.append("pos")
                return PosRun(ok=True)
            return fn

        def mk_kw(k, res):
            """给**执行模块**那几步用的桩（它们收关键字，不是 `args`）。"""
            def fn(**_kw):
                calls.append(k)
                return res
            return fn

        # ⚠ 本店必须是**要走玲珑**的那一类（名单里有串号标识），否则 `daily`
        #   会正确地早退成"没有可跑的步骤" —— 那是 2026-09-18 加的门店权限划分。
        #   以前这些测试读的是开发机上那份真配置，本店换成合作店之后集体变红。
        # ⚠ `plan_run` 也得挡（2026-09-23）：月度计划 `root=None` ⇒ 真写**项目根**
        #   `out/plan-2026.json`（固定名 tmp 三头并行抢 ⇒ FileNotFoundError 偶发红）。
        #   ⚠ 注释不能写进下面的 `\` 续行链 —— 桩不记 calls（断言是"五步"）。
        with mock.patch.object(cli, "load_config",
                               lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                                                "store_code": "SCN328987",
                                                "marker": "C"}), \
             mock.patch.object(cli, "cmd_dump", mk("dump")), \
             mock.patch.object(cli, "cmd_check", mk("check")), \
             mock.patch.object(run_daily, "pos_run", mk_pos()), \
             mock.patch.object(cli, "cmd_pools", mk("pools")), \
             mock.patch.object(cli, "cmd_erp_dump", mk("erp-dump")), \
             mock.patch.object(run_daily, "attain_run", mk_attain()), \
             mock.patch.object(run_daily, "report_run",
                               mk_kw("report", {"ok": True})), \
             mock.patch.object(run_daily, "inbox_run",
                               mk_kw("report-inbox", {"ok": True, "skipped": "没配收信"})), \
             mock.patch.object(run_daily, "plan_run", lambda **k: {"ok": True}), \
             contextlib.redirect_stdout(buf), \
             mock.patch.object(cli, "_find_pos_db",
                               return_value=Path("/tmp/cbg-2026.db")):
            rc = run_daily.main(extra)
        return rc, calls, buf.getvalue()

    def test_每天那趟五步都跑(self):
        rc, calls, _ = self._run(self._steps_except())
        # ⚠ `report`（上报数据）/`report-inbox`（收上报）**不在这一套里** ——
        #   它们各有自己的时刻（21:15 / 21:30，见 `registry.BUILTIN_STEPS`）。
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])
        self.assertEqual(rc, 0)

    def test_只抓数据(self):
        """点名只有 `dump` ⇒ **别的都不跑**（哪怕它们本来在"每天那趟"里）。"""
        rc, calls, _ = self._run(["--steps", "dump"])
        self.assertEqual(calls, ["dump"])

    def test_点名点空等于命令写错(self):
        """⚠ `--steps` 给了个空串 / 全是不认识的名字 —— **报错，别静默跑一大套**。"""
        rc, calls, _ = self._run(["--steps", " "])
        self.assertEqual(calls, [])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_只算_POS(self):
        rc, calls, _ = self._run(["--steps", "pos"])
        self.assertEqual(calls, ["pos"])

    def test_少点几步就只跑那几步(self):
        rc, calls, _ = self._run(self._steps_except("dump", "erp-dump"))
        self.assertEqual(calls, ["pos", "pools", "attain"])
        self.assertEqual(rc, 0)

    def test_表头写清这次要跑哪几件(self):
        _, _, out = self._run(self._steps_except("dump"))
        self.assertIn("双平台数据对比", out)
        self.assertIn("POS 合规", out)
        head = out.split("这趟跑的：")[1].split("\n")[0]
        self.assertNotIn("抓取玲珑数据", head,
                         "没点名的步骤不该出现在「这趟跑的」那一行里")

    # ------------------------------------------------ 退出码：跳过的步骤不许参与
    def test_只算_POS_时退出码只看_POS(self):
        """⚠ 这条是**重构的直接原因**：第一版固定拿 rc2/rc3 两个变量算退出码，
        于是"只算 POS"会把**根本没跑过**的报量排查那一步的默认值也算进去。
        """
        import contextlib
        import io
        seen = {}

        def fake_pos(**_kw):
            seen["pos"] = True
            return PosRun(ok=True)

        def boom(_a):
            raise AssertionError("只算 POS 时不该跑报量排查")

        buf = io.StringIO()
        with mock.patch.object(cli, "load_config",
                               lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                                                "store_code": "SCN328987",
                                                "marker": "C"}), \
             mock.patch.object(run_daily, "pos_run", fake_pos), \
             mock.patch.object(cli, "cmd_check", boom), \
             mock.patch.object(cli, "cmd_dump", boom), \
             mock.patch.object(cli, "cmd_pools", lambda a: 0), \
             mock.patch.object(run_daily, "attain_run", lambda **k: {"ok": True}), \
             mock.patch.object(run_daily, "report_run", lambda **k: {"ok": True}), \
             contextlib.redirect_stdout(buf):
            rc = run_daily.main(["--steps", "pos"])
        self.assertTrue(seen.get("pos"))
        self.assertEqual(rc, 0)

    def test_跳过的步骤失败也不影响退出码(self):
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config",
                               lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                                                "store_code": "SCN328987",
                                                "marker": "C"}), \
             mock.patch.object(cli, "cmd_dump", lambda a: 99), \
             mock.patch.object(cli, "cmd_erp_dump", lambda a: 0), \
             mock.patch.object(cli, "cmd_pools", lambda a: 0), \
             mock.patch.object(run_daily, "pos_run",
                               lambda **k: PosRun(ok=True)), \
             contextlib.redirect_stdout(buf):
            # ⚠ `dump` 会回 99（失败），但它**没被点名** ⇒ 不该把退出码带坏
            #   （⚠ `cmd_erp_dump` 必须挡：不挡的话这条会**真去登云商**拉数据）
            rc = run_daily.main(["--steps", "erp-dump,pos,pools"])
        self.assertEqual(rc, 0, "没点名的步骤不该把退出码带坏")

    def _argv(self, steps):
        m = runner.RunManager()
        j = m.start_steps(Path("/tmp/x"), "config/store-X.yaml", steps)
        j.kill()
        j.running = False
        return " ".join(j.argv[j.argv.index("daily") + 1:])

    def test_每条路都走_daily_这一个入口(self):
        """⚠ 各入口走不同命令的话，`daily` 那些行为（第 1 步失败就不发、
        库里没有就抓全量、会话失效先静默续期）在别处就全都享受不到了，
        而且"各页刷新跑的"和"定时任务跑的"迟早分叉。

        ⚠ 2026-09-21 晚起 `daily` 后面**必须**跟 `--steps`（点名）——
        这正好是这条测试能钉住的东西：漏了 `--steps` 的那条路会**当场报错**，
        而不是"悄悄跑了一大套"。
        """
        m = runner.RunManager()
        j = m.start_steps(Path("/tmp/x"), "config/store-X.yaml", ("erp-dump", "attain"))
        self.assertIn("daily", j.argv)
        self.assertIn("--steps", j.argv)
        self.assertIn("erp-dump,attain", j.argv)
        j.kill()
        j.running = False

    def test_argv_里点名的是那几步(self):
        # ⚠ 不拼 `--days-ago` / `--date`（2026-09-17 起就不拼了）：它们不影响
        #   任何一步，拼上去只会让日志里那条命令看着像"有个目标日可以调"。
        # ⚠ 也不拼 `--skip-*`（2026-09-21 晚）：那套"整批里跳过谁"的语义没了，
        #   点名就是全部信息。
        self.assertEqual(self._argv(("dump",)),
                         "--steps dump")
        self.assertEqual(self._argv(("dump", "erp-dump", "pos", "pools", "attain")),
                         "--steps dump,erp-dump,pos,pools,attain")
        self.assertNotIn("--skip-", self._argv(("pos",)))

    def test_定时器派发的那条命令也点名(self):
        """内置定时器那条路（`start_argv`）拿的就是 `daily --steps …` ——
        它跟各页「刷新」走**同一把锁、同一个抽屉、同一个退出码出口**。"""
        from src.modules import timer
        argv = timer.wake_argv("/tmp/x", "c.yaml", ["dump", "pos"], "2026-09-21 21:00")
        self.assertIn("daily", argv)
        self.assertIn("--steps", argv)
        self.assertIn("dump,pos", argv)


class Test跑哪几步已经不是设置(unittest.TestCase):
    """⚠⚠ 用户 2026-09-20：「**自动化跑什么 … 这些去掉吧，也不用设置了**」；
    2026-09-21 晚又删掉了"手动整批"。

    ⇒ 现在**没有任何地方**能限制"跑哪几步"：
      * 到点跑什么 = 每一步自己的唤醒时刻（注册表）；
      * 手动双击那份脚本 = `run_daily.MANUAL_STEPS`（也是注册表派生的）；
      * 各页「刷新」= `web.REFRESH_STEPS`。

    ⚠ 这条类的要害不是"少了个界面"，而是**别留下一个"设不了、却还在悄悄
      限制范围"的东西**：老门店的 `.secrets/schedule.json` 里记着 `["pos"]`
      那种老勾选，它**必须完全失效** —— 否则 pools / attain 会永远不跑，
      而界面上一个字都不说。
    """

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = mock.patch.object(schedule, "kind", lambda: "windows")
        p.start()
        self.addCleanup(p.stop)

    def test_那两个函数已经删了(self):
        for gone in ("automation_steps", "set_automation_steps", "existing_steps",
                     "existing_days_ago"):
            with self.subTest(gone=gone):
                self.assertFalse(hasattr(schedule, gone), "%s 又回来了？" % gone)

    def test_老记录里的勾选不再影响范围(self):
        """⚠ **升级路径**：老门店的 `.secrets/schedule.json` 里记着 `["pos"]`
        这种老勾选（那时界面还能勾）。它现在**读都不读** ——
        手动那份脚本跑哪几步由注册表派生（`MANUAL_STEPS`），
        跟那份记录一点关系都没有。"""
        p = schedule.record_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"t": {"steps": ["pos"], "at": "x"}}', encoding="utf-8")
        schedule.write_runner_script(self.root, "c.yaml")
        body = schedule.manual_script_path(self.root).read_text(encoding="utf-8")
        for step in run_daily.MANUAL_STEPS:
            with self.subTest(step=step):
                self.assertIn(step, body, "老记录把 %s 挤掉了？" % step)

    def test_生成的脚本里点名的是每天那趟(self):
        """手动那份脚本：`daily --steps dump,erp-dump,pos,pools,attain` ——
        **枚举出来**（不写"整批"那种隐式说法），而且不带任何 `--skip-*`。"""
        schedule.write_runner_script(self.root, "c.yaml")
        for path in (schedule.script_path(self.root),
                     schedule.manual_script_path(self.root)):
            body = path.read_text(encoding="utf-8")
            with self.subTest(f=path.name):
                for flag in ("--skip-dump", "--skip-pos", "--skip-pools",
                             "--skip-attain", "--skip-report", "--days-ago"):
                    self.assertNotIn(flag, body, "%s 里还有 %s" % (path.name, flag))
        manual = schedule.manual_script_path(self.root).read_text(encoding="utf-8")
        self.assertIn("--steps " + ",".join(run_daily.MANUAL_STEPS), manual)


class TestAutomationLabel(unittest.TestCase):
    """「跑什么」的文字 —— **列出执行项目**，不是给一句概括。

    写「整个项目」门店看不懂那指什么。
    """

    def test_列出每一天实际干的几件事(self):
        self.assertEqual(run_daily.steps_label(("dump", "pos")),
                         "抓取玲珑数据 + POS 合规")
        self.assertEqual(run_daily.steps_label(("pos", "pools")),
                         "POS 合规 + 双平台数据对比")
        self.assertEqual(run_daily.steps_label(("dump",)),
                         "抓取玲珑数据")

    def test_一件都不选要抛(self):
        with self.assertRaises(ValueError):
            run_daily.steps_label(())

    def test_每天那趟也说得出来(self):
        """`steps_label` 现在只给**各页「刷新」**用（`/api/refresh` 的回应里那句
        "正在抓：…"）—— 它仍然要能把任意一组步骤说成人话。"""
        from src.features import registry
        self.assertTrue(run_daily.steps_label(registry.default_steps()).strip())
        self.assertEqual(run_daily.steps_label(("erp-dump", "attain")),
                         "抓取云商数据 + 销售达成")


# ⚠ 这儿原来有个 `TestAutomationChoicesMatch`（"可选项 = 全部步骤 − 必做项"）。
#   2026-09-20 那个设置整个取消 —— `schedule.AUTOMATION_CHOICES` /
#   `steps_from_choices` / `choices_from_steps` 三个也一起删了（没有调用方了）。
#   "跑哪几步固定是全部"现在由 `Test跑哪几步不再是设置` 那几条钉着。


class TestSettingsCopy(unittest.TestCase):
    """设置页「定时执行」那段说明要跟可选项对得上。

    ⚠ 报量排查拿掉之后文案还写着「默认**四**件都勾」，而勾选框只有三个 ——
    门店会以为程序少跑了一件。**数字不要手写，从代码里的定义推。**
    """

    _CN = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}

    @staticmethod
    def _strip_html_comments(text):
        """⚠ 断言前**必须剥掉 HTML 注释** —— 我给这块写的注释里正引用着
        原文（「原来这儿有一组复选框（POS 合规 / 双平台数据对比）」），
        不剥的话 `assertNotIn` 会被**自己的注释**顶掉。
        （这个坑今天第五次了：断言要对着**页面**，不是整段源码。）"""
        import re
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)

    def _sched_card(self):
        return self._strip_html_comments(self._sched_card_raw())

    def _sched_card_raw(self):
        # ⚠ 2026-09-20：这块从「通用」页搬成了**独立一页「定时器设置」**
        #   （用户：「定时器设置单独出来一页」），标题后面还跟了一个
        #   `<span class="hint" id="timer-meta">`（显示"下次 …"）——
        #   所以按 `<h2>定时器设置` 找，带上 `</h2>` 就永远找不到了。
        i = INDEX_HTML.index("<h2>定时器设置")
        j = INDEX_HTML.index("</section>", i)
        return INDEX_HTML[i:j]

    def test_那几段说明也一起删了(self):
        """⚠⚠ 2026-09-20（用户）：「**自动化跑什么 … 这些去掉吧，也不用设置了**」。

        这一块原来是"复选框 + 保存 + 四段说明"（抓数据每次都会跑 / 双平台要勾 /
        POS 不勾就不算 / 两个都不勾也允许）。**整块取消**之后那几段说明也必须走 ——
        留着的话门店会满界面找一组**不存在的复选框**（这项目为"文案指着一个
        已经删掉的控件"栽过好几次）。
        """
        card = self._sched_card()
        for gone in ("自动化跑什么", "每次都会跑", "双平台数据对比", "不勾"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, card)
        # ⚠ 取消之后这页**不再解释"跑哪几步"** —— 没有可设的东西，就不用解释：
        #   （解释留在别的页面也没必要：`自动化跑什么` 这个词整份界面里都该消失）
        self.assertNotIn("自动化", card)

    def test_不再说不许一件都不勾(self):
        """⚠ 用户 2026-09-17 定的：两个都不勾 = 「只抓数据」，**允许**。
        文案里再留着"一件都不勾是不允许的"就是骗人。"""
        self.assertNotIn("不允许", self._sched_card())

    def test_勾选那两个名字不再出现在这页(self):
        """⚠ 2026-09-20 之前这条是反的（要求"说明里必须出现可选项的名字"）——
        设置取消之后，那些名字留在页面上只会让人满界面找复选框。"""
        card = self._sched_card()
        for s in ("pos", "pools"):
            with self.subTest(step=s):
                self.assertNotIn(run_daily.STEP_LABELS[s], card)

    def test_不再提已经删掉的目标日下拉(self):
        """⚠ 那句「选『昨天』零遗漏；选『今天』…」是对着一个**已经删掉的下拉框**
        说话（`sched-days-ago` 2026-09-17 拿掉）。留着门店会满页面找一个不存在的选项。"""
        card = self._sched_card()
        self.assertNotIn("选「昨天」", card)
        self.assertNotIn("目标日", card)


class TestTargetDateOnlyAffectsReconcile(unittest.TestCase):
    """⚠ 「目标日」和「高级（时间窗容差）」**只影响报量排查**（用户 2026-09-16 确认）。

    * **抓取玲珑数据**固定抓当月（第一次跑时补今年至今）—— 它没有"某一天"的概念；
    * **POS 合规**按**整月**算 —— 一个月一个分数，也没有"某一天"。

    这条不只是文案：要是哪天有人"顺手"把 `days_ago` 也转发给 dump/pos，
    用户会以为 POS 只算了今天 —— 而它其实算了整月，**而且不会有任何提示**。
    所以按"传进去的 Namespace 长什么样"钉死。

    ⚠ **2026-09-17**：报量排查整步没了 ⇒ 这两块**界面上直接删掉**、
    `/api/run` 也不再收那五个字段。`daily` 命令行上那四个参数**留着**
    （老 run.bat / 计划任务里写死着），所以下面几条照旧有效。
    """

    def _seen(self, extra=None):
        # ⚠ 2026-09-21 晚起 `daily` 必须点名 —— 不传就当"每天那趟"那几步
        #   （这些用例关心的是"目标日那几个参数传没传下去"，不是跑哪几步）。
        if extra is None:
            extra = ["--steps", ",".join(run_daily.MANUAL_STEPS)]
        import contextlib
        import io
        seen = {}
        calls = []          # ⚠ `mk_attain` 会往里记 —— 别删（删了就是 NameError）

        def mk(k):
            def f(a):
                seen[k] = a
                return 0
            return f
        buf = io.StringIO()

        # ⚠ 2026-09-19 起 POS 走**执行模块**（`app.pos.run`）：桩按新契约回 `PosRun`，
        #   而且它收的是**关键字**（不再是那个手工拼的 Namespace）。
        #   ⚠ 定义必须放在 `with` **之前** —— 续行链里插注释是语法错。
        def mk_attain():
            def fn(**_kw):
                calls.append("attain")
                return {"ok": True}
            return fn

        def mk_pos():
            def fn(**kw):
                seen["pos"] = kw
                return PosRun(ok=True)
            return fn

        # ⚠ 也得挡住 `cmd_pools` —— 不挡的话 `run_daily.main` 会**真算一遍四池**、
        #   往项目根的 `out/` 写 xlsx 和 json（实测 1.5 秒/次）。
        #   这属于「测试数据隔离」那一步（四项文档 阶段 1.3），本文件顺手先隔离掉。
        # ⚠ 注释**不能写在 `\` 续行链中间** —— 那是语法错误（这里刚踩了一次）。
        # ⚠ `plan_run` 同样必须挡（2026-09-23 抓到的根因）：月度计划 `root=None` ⇒
        #   真写**项目根** `out/plan-2026.json`（4.5MB 每次全量被覆盖），固定名
        #   `plan-2026.json.tmp` 被三个头并行抢 ⇒ `FileNotFoundError` 偶发红。
        with mock.patch.object(cli, "load_config",
                               lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                                                "store_code": "SCN328987",
                                                "marker": "C"}), \
             mock.patch.object(cli, "cmd_dump", mk("dump")), \
             mock.patch.object(cli, "cmd_check", mk("check")), \
             mock.patch.object(run_daily, "pos_run", mk_pos()), \
             mock.patch.object(cli, "cmd_pools", mk("pools")), \
             mock.patch.object(cli, "cmd_erp_dump", mk("erp-dump")), \
             mock.patch.object(run_daily, "attain_run", mk_attain()), \
             mock.patch.object(run_daily, "report_run",
                               lambda **k: {"ok": True}), \
             mock.patch.object(run_daily, "inbox_run",
                               lambda **k: {"ok": True, "skipped": "没配收信"}), \
             mock.patch.object(run_daily, "plan_run", lambda **k: {"ok": True}), \
             mock.patch.object(cli, "_find_pos_db",
                               return_value=Path("/tmp/cbg-2026.db")), \
             contextlib.redirect_stdout(buf):
            # ⚠⚠ **`cmd_erp_dump` 和 `attain_run` 必须一起挡**（2026-09-21 补）：
            #   漏了的话这条会**真去登云商抓数**（网络）并**真算一遍达成**
            #   —— 而达成会 `tmp + rename` 写**项目根**的 `out/attain-2026.json`。
            #   三个头并行跑时两个进程同时 rename 同一个 tmp ⇒
            #   `FileNotFoundError: out/attain-2026.json.tmp -> …`（实测在 3.8 那个头红了）。
            #   ⇒ 测试**不许碰项目根的 out/**（这是"测试数据隔离"的同一条规矩）。
            run_daily.main(extra)
        return seen

    def test_抓取玲珑数据收不到目标日(self):
        seen = self._seen(["--steps", ",".join(run_daily.MANUAL_STEPS),
                           "--date", "2026-09-10", "--lookback", "3", "--lookahead", "1"])
        for attr in ("date", "days_ago", "lookback", "lookahead"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(seen["dump"], attr),
                                 "dump 不该收到 %s —— 它固定抓当月" % attr)

    def test_抓取玲珑数据只看当月或全量(self):
        seen = self._seen(["--steps", ",".join(run_daily.MANUAL_STEPS),
                           "--date", "2026-09-10"])
        self.assertTrue(hasattr(seen["dump"], "month"))
        self.assertTrue(hasattr(seen["dump"], "all"))

    def test_POS_收不到目标日(self):
        seen = self._seen(["--steps", ",".join(run_daily.MANUAL_STEPS),
                           "--date", "2026-09-10", "--lookback", "3", "--lookahead", "1"])
        for attr in ("date", "days_ago", "lookback", "lookahead"):
            with self.subTest(attr=attr):
                self.assertNotIn(attr, seen["pos"],
                                 "pos 不该收到 %s —— 它按整月算" % attr)

    def test_POS_收到的字段和_pools_一样齐(self):
        """⚠ 这条**原来叫 `test_POS_只收到库路径`，断言的就是 `{"db": ""}`** ——
        它把**缺陷本身当成了规格**。

        真相（2026-09-19 架构审阅的隔离探针发现）：`cmd_pos` 会把
        `getattr(args, "config", None)` 交给 `_maybe_pos_push`，后者第一句是
        `if not config_path: return` ⇒ **`daily` 每天算了 POS，却从来没推过 POS**。
        而"POS 只收到库路径"这条断言，正好把这个缺口**焊死**了 ——
        谁要修它，先得把这条测试改绿。

        ⇒ 现在钉的是**该有的那份**：库路径 + 配置文件 + 两个通知开关，
          **和同一个函数里 `cmd_pools` 收到的一样齐**。
        """
        seen = self._seen()
        got = seen["pos"]
        # ⚠ 2026-09-19 起是**关键字**（不再是 Namespace）：这条断言跟着换挂点，
        #   但钉的东西一个字没变 —— 少一个参数就可能静默不推。
        self.assertEqual(sorted(got),
                         ["config_path", "db", "emit", "no_mail", "no_push"],
                         "POS 收到的参数变了 —— 少一个就可能静默不推")
        self.assertEqual(got["db"], "")
        self.assertFalse(got["no_mail"])
        self.assertFalse(got["no_push"])
        self.assertTrue(got["config_path"], "POS 拿不到配置文件 ⇒ 推送那边第一句就 return")
        self.assertTrue(callable(got["emit"]), "emit 没传 ⇒ daily 日志里没有分数那几行")


class TestRunPageOnlyOneButton(unittest.TestCase):
    """⚠ 2026-09-21（用户：「**右下角的跑一次可以去掉了**」）——
    那一整张「跑一次」的卡（按钮 + 停止 + 说明）**从 HTML 里删掉了**。

    留这条类的意义：**防止它被加回来**。手动跑还是可以的（命令行 `python -m src.cli daily`），
    只是界面上不再给这个入口 —— 「运行日志」那张卡留着（用户 2026-09-18：
    「运行日志要一直在、且好找」）。
    """

    def test_跑一次那张卡删了(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html"
                ).read_text(encoding="utf-8")
        for gone in ('<h2>跑一次</h2>', 'data-what="all"', 'id="btn-stop"',
                     'id="run-status"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html, "「跑一次」那张卡该删干净")

    def test_运行日志还在(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html"
                ).read_text(encoding="utf-8")
        self.assertIn('id="run-log"', html, "运行日志不能跟着删")


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


class Test旧的计划任务只剩清理(unittest.TestCase):
    """⚠ 2026-09-20（用户：「**把兜底去掉吧，不用系统的计划任务**」）：

    那块从"注册/管理一条 Windows 计划任务"改成**只做清理**：

    * **没有任务时什么都不显示** —— 以前会挂一条"还没注册定时任务，
      得手动跑才对账"，而那句话现在是**错的**（到点由服务里的定时器跑，
      服务靠开机自启常驻）；留着它门店会去建一条根本不需要的任务；
    * 有任务时给一句话 + 「删除」；**没有「添加」、也没有「执行」**
      （添加是产品不再提供的动作；执行只会让它去"确保服务在跑"）。
    """

    def _block(self):
        """⚠⚠ 取出来之后**必须剥掉 JS 注释** —— 那段注释里正好引用着
        "以前会挂一条『还没注册定时任务，得手动跑才对账』"，
        不剥的话 `assertNotIn` 会被**自己的注释**顶掉。
        （这个坑在这个项目里已经是第四次了：断言要对着**代码**，不是整段文本。）"""
        import re
        i = APP_JS.index("function renderSchedule(sch)")
        blk = APP_JS[i:APP_JS.index("\n}\n", i)]
        blk = re.sub(r"/\*.*?\*/", "", blk, flags=re.S)
        return re.sub(r"(?m)//[^\n]*$", "", blk)

    def test_没有任务时什么都不渲染(self):
        blk = self._block()
        self.assertIn("if (!tasks.length) {", blk)
        # 早退那一支里**不许**再出现"还没注册…得手动跑"那种话
        head = blk[:blk.index("const rows = tasks.map(")]
        self.assertNotIn("得手动跑", head)
        self.assertNotIn("注册计划任务", head)

    def test_界面不再提供注册入口(self):
        for gone in ("btn-sched-install", "sched-time", "sched-name",
                     "syncSchedPlaceholder"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, APP_JS, "注册入口又回来了？")
                self.assertNotIn(gone, INDEX_HTML)

    def test_也不再提供执行按钮(self):
        """那条任务现在只做"确保服务在跑" —— 点「执行」等于白跑一趟。"""
        self.assertNotIn("data-sched-run", APP_JS)

    def test_删除按钮带上两个名字(self):
        """发去后端的是 `full_name`（Windows 上带反斜杠），给人看的是叶子名 ——
        不然弹窗会写成「删除「\\TaskName」？」。"""
        blk = APP_JS[APP_JS.index("function schedDeleteButton(t)")
                     :APP_JS.index("function schedDeleteButton(t)") + 500]
        self.assertIn("data-sched-del=", blk)
        self.assertIn("data-sched-label=", blk)
        self.assertIn("full_name", blk)

    def test_删不掉的可以提权删(self):
        """管理员建的任务普通权限删不掉 —— 那只剩「以管理员身份删除」一条路，
        而且发的必须是**叶子名**（带反斜杠后端会 400，表现是"点了没反应"）。"""
        self.assertIn("data-sched-del-admin", APP_JS)
        i = APP_JS.index("data-sched-del-admin]")
        blk = APP_JS[i:i + 700]
        self.assertIn("schedule-remove", blk)
        self.assertIn("what: 'schedule-remove', name", blk)


if __name__ == "__main__":
    unittest.main()


class Test点名模式不打无关的日志(unittest.TestCase):
    """⭐ 用户 2026-09-21：「每个页面的刷新，刷**对应的数据**就行，比如销售达成
    就**没必要刷玲珑**，毕竟他不依赖于玲珑数据」。

    ⚠ 步骤映射本来就是对的（达成刷新 = `erp-dump,attain`，不碰玲珑）——
      但**日志**里原来会打一行「[1/6] 抓取玲珑数据：**已用 --skip-dump 跳过**」，
      用户看到"抓取玲珑数据"几个字就以为刷达成去动了玲珑。
    ⇒ 点名模式下：没点名的步骤**一律不吭声**（顶上已经列了"就这几步"）。
    """

    def _run(self, steps):
        import contextlib
        import io
        from unittest import mock
        from src import run_daily as rd
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target in ("src.cli.cmd_dump", "src.cli.cmd_erp_dump",
                           "src.cli.cmd_pools"):
                stack.enter_context(mock.patch(target, side_effect=lambda *a, **k: 0))
            stack.enter_context(mock.patch("src.run_daily.attain_run",
                                           side_effect=lambda **k: {"ok": True}))
            stack.enter_context(mock.patch(
                "src.run_daily.pos_run",
                side_effect=lambda **k: __import__("types").SimpleNamespace(
                    ok=True, why="")))
            stack.enter_context(mock.patch("src.modules.health.auto_update",
                                           side_effect=lambda *a, **k: {"ok": True}))
            stack.enter_context(mock.patch.object(
                cli, "load_config", lambda *a, **k: {
                    "erp_store_name": "青岛CBD万达店", "store_code": "SCN328987",
                    "marker": "C"}))
            stack.enter_context(mock.patch.object(
                cli, "_find_pos_db", return_value=Path("/tmp/cbg-2026.db")))
            stack.enter_context(contextlib.redirect_stdout(buf))
            rd.main(["--steps", steps])
        return buf.getvalue()

    def test_刷达成不提玲珑(self):
        out = self._run("erp-dump,attain")
        self.assertNotIn("玲珑", out, "刷达成的日志里不该出现玲珑")
        self.assertIn("抓取云商数据", out)

    def test_顶上写清就这几步(self):
        out = self._run("erp-dump,attain")
        self.assertIn("这趟跑的：", out)
        # ⚠ 步骤总数跟着注册表走（2026-09-22 加 film/benefit 后共 11 步）
        self.assertIn("其余 %d 步不跑" % (len(run_daily.STEPS) - 2), out)

    def test_明确用_skip_关掉的还是要说(self):
        """⚠ 静音只针对"没点名"；**自己拿 `--skip-*` 关掉的**仍然要报
        （那是人主动关的，不说就成了静默改行为）。"""
        out = self._run("dump,erp-dump,pos,pools,attain,autoupdate")
        self.assertIn("自动更新", out)
