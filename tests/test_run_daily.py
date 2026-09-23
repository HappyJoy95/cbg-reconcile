"""日常流程编排（`src/run_daily.py`）的测试。

**为什么要单独钉**：这条流程是**定时任务唯一的入口**，它排错的时候没人盯着。
所以"哪一步失败、后面还跑不跑、最终退哪个码"这三件事必须有测试，
不能靠读注释确认。

用户 2026-09-16 定的规矩，逐个对应到下面的测试：

1. 第 1 步（抓华为）失败 ⇒ **跳过第 2、3 步，直接报错**
2. 第 2 步（报量对账）失败 ⇒ **第 3 步照跑**（POS 只读库，跟云商没关系）
3. 退出码取**第一个不成功的** —— 计划任务那边只看得到这一个数
4. 对账的 `EXIT_DIFF`(3) 算**跑通**（有差异是正常结果，不是故障）
"""

import datetime
import unittest
from pathlib import Path
from unittest import mock

from src import cli, mailer, run_daily
from src.app import pos as app_pos
from src.app.pos import PosRun


class _Rec:
    """记录调用顺序和参数，并按脚本返回退出码。"""

    def __init__(self, dump_rc=0, check_rc=0, pos_rc=0, pools_rc=0, attain_rc=0,
                 erp_dump_rc=0):
        # ⚠ 2026-09-20 加了 `erp-dump`（用户：「数据抓取再加个云商数据定时抓取吧」）——
        #   **必须一起挡**：不挡的话这条链会真去登云商拉销售明细（网络！），
        #   实测全量测试从 66 秒涨到 126 秒，而且依赖外网和账号。
        self.codes = {"dump": dump_rc, "check": check_rc, "pos": pos_rc,
                      "pools": pools_rc, "attain": attain_rc,
                      "erp-dump": erp_dump_rc}
        self.calls = []
        self.namespaces = {}

    def _mk(self, name):
        def fn(args):
            self.calls.append(name)
            self.namespaces[name] = args
            return self.codes[name]
        return fn

    def _mk_pos(self):
        """`app.pos.run` 的桩：收关键字、回 `PosRun`（新契约）。"""
        def fn(**kw):
            self.calls.append("pos")
            self.namespaces["pos"] = kw
            code = self.codes["pos"]
            return PosRun(ok=(code == 0), why="" if code == 0 else "桩：第 3 步没跑通")
        return fn

    def _mk_attain(self):
        """`features.sales.attain.run` 的桩 —— **必须挡**（同 `cmd_pools` 那段的理由）。

        ⚠ 第 5 步会**真去读腾讯文档**（网络！）：不挡的话这个文件里 20 多个调用点
          每次都发一次请求 —— 实测全量测试从 **59 秒涨到 165 秒**，而且测试依赖外网。
        """
        def fn(**kw):
            self.calls.append("attain")
            self.namespaces["attain"] = kw
            code = self.codes["attain"]
            return {"ok": code == 0, "why": "" if code == 0 else "桩：第 5 步没跑通"}
        return fn

    def _mk_report(self):
        """第 6 步（上报数据）的桩 —— 收 `**kw`，回 `{ok, skipped, rows}`。"""
        def fn(**kw):
            self.calls.append("report")
            self.namespaces["report"] = kw
            return {"ok": True, "rows": 0, "skipped": "", "why": ""}
        return fn

    def _mk_inbox(self):
        """第 7 步（收取门店上报）的桩 —— 默认给"没配收信，跳过"。"""
        def fn(**kw):
            self.calls.append("report-inbox")
            self.namespaces["report-inbox"] = kw
            return {"ok": True, "skipped": "这台机器没配收信（IMAP）", "problems": []}
        return fn

    def patch(self):
        return (
            mock.patch.object(cli, "cmd_dump", self._mk("dump")),
            mock.patch.object(cli, "cmd_check", self._mk("check")),
            # ⚠ 2026-09-19 起 POS 走**执行模块**（`app.pos.run`），不再经 CLI：
            #   桩要按**新的结果契约**（`PosRun`）来，不能返回一个整数。
            mock.patch.object(run_daily, "pos_run", self._mk_pos()),
            # ⚠ **`cmd_pools` 也得挡** —— 原来这里只有三个，漏了它 ⇒ `run()`
            #   每跑一次都**真算一遍四池**、往项目根的 `out/` 写
            #   `pools-2026.json` + `双平台数据对比-*.xlsx`（实测 **1.5 秒/次**，
            #   而 `run()` 在这个文件里有 20 多个调用点）。
            #   更要命的是它读**开发机上那个真库** —— 换台机器跑就是另一种行为，
            #   而测试不该依赖开发机的状态（这个文件上面已经为同一件事栽过一次）。
            #   ⇒ 2026-09-19 补上（四项文档 阶段 1.3「测试数据隔离」）。
            mock.patch.object(cli, "cmd_pools", self._mk("pools")),
            # ⚠ 第 2 步（抓云商数据）同样**必须挡**（理由见 `codes` 里那段）
            mock.patch.object(cli, "cmd_erp_dump", self._mk("erp-dump")),
            mock.patch.object(run_daily, "attain_run", self._mk_attain()),
            # ⚠ 2026-09-21（M18）第 6/7 步（上报数据 / 收取门店上报）同样**必须挡**：
            #   它们是执行模块（`app.report.run` / `app.report_inbox.run`）——
            #   不挡的话每条用例都会真去读本机库算差集、甚至去连邮箱（网络）。
            mock.patch.object(run_daily, "report_run", self._mk_report()),
            # ⚠ 第 6 步（M22 月度生意计划 `plan`）同理**必须挡**（2026-09-23 抓到的根因）：
            #   它 `root=None` ⇒ 每次全量测试都真写**项目根** `out/plan-2026.json`
            #   （4.5MB 真落盘被反复覆盖），且固定名 `plan-2026.json.tmp` 被三个头
            #   并行抢 ⇒ 后到的那个 `replace()` 报 `FileNotFoundError`（并行偶发红）。
            #   ⚠ 桩**不记 calls** —— 现有断言都是"五步"，plan 只进 done/退出码。
            mock.patch.object(run_daily, "plan_run", lambda **k: {"ok": True}),
        )


#: 夹具默认点名的几步 —— **就是"每天那趟"**（注册表里 `default=True` 的那几个）。
#: ⚠ 2026-09-21 晚起 `daily` **必须 `--steps` 点名**（"整批"这个模式删了），
#:   所以默认值从"不带参数"换成了"点名这一套"。要少跑哪一步，用 `steps_except()`。
DEFAULT_STEPS = list(run_daily.MANUAL_STEPS)


def steps_except(*skip):
    """点名跑"每天那趟"里除了这几步之外的 —— 代替原来的 `--skip-xxx`。"""
    return ["--steps", ",".join(s for s in DEFAULT_STEPS if s not in skip)]


def run(argv=None, **codes):
    """跑一次 run_daily，返回 (退出码, 调用顺序, 各步收到的 Namespace)。"""
    r = _Rec(**codes)
    # ⚠ 本店必须是**要走玲珑**的那一类（名单里有串号标识），否则 `daily`
    #   会正确地早退成"没有可跑的步骤" —— 那是 2026-09-18 加的门店权限划分。
    #   以前这些测试读的是开发机上那份真配置，本店换成合作店之后集体变红；
    #   钉死一份配置，顺便让它们**不再依赖开发机的状态**。
    #   ⚠ 必须**并进 `ps` 一起 start()** —— 写成一句裸表达式的话，
    #     patch 对象造出来就没人 start，测试照样红（我自己踩了一次）。
    ps = r.patch() + (mock.patch.object(
        cli, "load_config",
        lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                         "store_code": "SCN328987", "marker": "C"}),)
    for p in ps:
        p.start()
    try:
        rc = run_daily.main(argv if argv is not None else steps_except())
    finally:
        for p in ps:
            p.stop()
    return rc, r.calls, r.namespaces


class Test按点名顺序跑(unittest.TestCase):
    """⚠⚠ 2026-09-21 用户：「**相同时间执行的任务，按照定时器这个列表从上到下执行**」。

    定时器到点派发的是**一条命令**（`daily --steps a,b,c`），所以"表里的顺序"
    必须真的决定"谁先跑" —— 以前这 8 个块是写死的代码顺序，
    用户在表里调了顺序也**一点用都没有**（而且日志顶上还按新顺序写着，
    看着像生效了，**静默不一致**）。
    """

    def test_点名的顺序就是跑的顺序(self):
        rc, calls, _ = run(["--steps", "attain,pos,dump,erp-dump,pools"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(calls, ["attain", "pos", "dump", "erp-dump", "pools"])

    def test_老顺序当然也照跑(self):
        rc, calls, _ = run(["--steps", "dump,erp-dump,pos,pools,attain"])
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])

    def test_抓数失败还是中止后面(self):
        """⚠⚠ 这条规矩原来是**隐式**的（写在前面的块直接 return）——
        改成按顺序跑之后必须显式写出来。反过来的顺序也一样：
        `dump` 排在最后、它失败了，**后面的**（这趟里它后面没别的了）不跑。
        """
        rc, calls, _ = run(["--steps", "attain,dump,pos"], dump_rc=99)
        self.assertEqual(rc, 99)
        self.assertEqual(calls, ["attain", "dump"], "dump 失败之后 pos 不该还跑")

    def test_云商那步失败也中止(self):
        """`erp-dump` 也是"读它写进去的库"的那一类 ⇒ 失败照样中止后面。"""
        rc, calls, _ = run(["--steps", "erp-dump,pools"], erp_dump_rc=cli.EXIT_FETCH)
        self.assertEqual(rc, cli.EXIT_FETCH)
        self.assertEqual(calls, ["erp-dump"], "云商失败之后 pools 不该还跑")

    def test_每一步都有它的块(self):
        """⚠ 注册表里加了步骤、`run_daily` 忘了接 ⇒ 定时器到点派发它，
        而 `main()` 里**没有对应的块**（这一步永远不跑，日志里只会有一行
        "还没有接进日常流程"）。结构钉子，别删。"""
        import inspect
        from src.features import registry
        src = inspect.getsource(run_daily.main)
        # ⚠ 别拿 `_ABORT_AFTER` 当结尾 —— 它在那段**说明注释**里先出现过一次
        #   （切片会变成空的，测试当场红；第一版就这么写的）。
        runners = src[src.index("_RUNNERS = {"):src.index("for _cmd in list(")]
        for s in registry.all_steps():
            with self.subTest(cmd=s.cmd):
                self.assertIn('"%s"' % s.cmd, runners,
                              "这一步没有对应的 `_step_*` 块")
                self.assertIn("_step_%s" % s.cmd.replace("-", "_"), runners)


class TestHappyPath(unittest.TestCase):
    def test_五步都跑_按顺序(self):
        """⚠ 2026-09-20 起第 2 步是**抓云商数据**（从第 1 步里拆出来的）——
        顺序必须是"先玲珑、再云商、最后分析"：分析那几步读的正是这两步写进去的数。"""
        rc, calls, _ = run()
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_第一步抓的是当月不是全量(self):
        """⚠ 用户定过：**拉整月再取并集** —— 因为 21:00 拉当天的话，
        后面还有更新，第二天就对不上了。

        ⚠ 必须自己把库探测钉成"有库" —— 开发机上 `out/cbg-2026.db` 是真的存在的，
        不钉的话这条测的是"这台机器有没有库"。**在包里跑就露馅了**：
        包里没有 out/，于是走了"第一次跑 → 抓全量"那条路，测试失败。
        """
        with mock.patch.object(cli, "_find_pos_db", return_value=Path("/tmp/cbg-2026.db")):
            _, _, ns = run()
        self.assertEqual(ns["dump"].month, "")
        self.assertFalse(ns["dump"].all)

    def test_第三步只读库(self):
        _, _, ns = run()
        self.assertEqual(ns["pos"]["db"], "")


class TestStep1FailureAbortsEverything(unittest.TestCase):
    """⚠ 最重要的一组：第 1 步失败**什么都不许发**。

    理由（写在 `run_daily` 模块注释里）：对账的华为侧现在从库读。
    库要是没抓到今天的单，差集会把当天**所有**销售都算成「未报量」——
    一份完全错误的清单，而且**看着很合理**，门店会照着去补报一批假的。
    """

    def test_失败就跳过第二步(self):
        rc, calls, _ = run(dump_rc=cli.EXIT_AUTH)
        self.assertEqual(calls, ["dump"])
        self.assertEqual(rc, cli.EXIT_AUTH)

    def test_失败也跳过第三步(self):
        _, calls, _ = run(dump_rc=cli.EXIT_FETCH)
        self.assertNotIn("pos", calls)

    def test_任何非零退出码都算失败(self):
        for code in (1, 2, 9, 42):
            rc, calls, _ = run(dump_rc=code)
            self.assertEqual(calls, ["dump"], "退出码 %d 时不该继续" % code)
            self.assertEqual(rc, code)

    def test_失败原因写进_stderr_而不是只留个码(self):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run(dump_rc=1)
        msg = err.getvalue()
        self.assertIn("跳过第 2、3 步", msg)
        self.assertIn("玲珑无但云商有", msg)   # 说清**为什么**不能接着跑（口径名同报表）
        self.assertIn("先解决第 1 步", msg)    # 以及下一步怎么办

    def test_失败时不发邮件不发推送(self):
        """第 1 步失败 ⇒ 连 `check` 都不进 —— 也就没有邮件可发。

        这条是**结构保证**（不是靠某个 flag），所以直接验调用列表。
        """
        _, calls, _ = run(dump_rc=1)
        self.assertEqual(calls, ["dump"])


class TestOnlyStep1CanAbort(unittest.TestCase):
    """⚠ 第 2 步（报量排查）拿掉之后，**只有第 1 步失败会中止整条流程**。

    原来是"第 2 步失败也照跑第 3 步"（POS 只读库，跟云商没关系）——
    那个场景随第 2 步一起没了。留下这条是为了钉住"中止规则现在只剩一条"。
    """

    def test_第1步失败就什么都不发(self):
        rc, calls, _ = run(dump_rc=cli.EXIT_FETCH)
        self.assertEqual(calls, ["dump"])
        self.assertEqual(rc, cli.EXIT_FETCH)

    def test_第1步成功就一路跑完(self):
        rc, calls, _ = run()
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])
        self.assertEqual(rc, cli.EXIT_OK)


class TestDiffIsNotFailure(unittest.TestCase):
    """`EXIT_DIFF`(3) = **跑通了，只是有差异** —— 每天都在发生，不是故障。"""

    def test_有差异时第三步照跑(self):
        rc, calls, _ = run(check_rc=cli.EXIT_DIFF)
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])

    def test_有差异但POS挂了_退POS的码(self):
        rc, _, _ = run(check_rc=cli.EXIT_DIFF, pos_rc=cli.EXIT_FETCH)
        self.assertEqual(rc, cli.EXIT_FETCH)


class TestExitCodeIsFirstNonSuccess(unittest.TestCase):
    def test_全好退0(self):
        self.assertEqual(run()[0], cli.EXIT_OK)

    def test_只有第三步坏(self):
        self.assertEqual(run(pos_rc=cli.EXIT_FETCH)[0], cli.EXIT_FETCH)

    def test_跳过抓取(self):
        rc, calls, _ = run(steps_except("dump"))
        self.assertEqual(calls, ["erp-dump", "pos", "pools", "attain"])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_跳过云商那一步(self):
        """不点名 `erp-dump` 就只少那**一步**（云商在库 + 销售明细），别的照跑。"""
        rc, calls, _ = run(steps_except("erp-dump"))
        self.assertEqual(calls, ["dump", "pos", "pools", "attain"])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_跳过POS(self):
        rc, calls, _ = run(steps_except("pos"))
        self.assertEqual(calls, ["dump", "erp-dump", "pools", "attain"])

    def test_少点两步就只剩别的(self):
        """⚠ 点名是**精确的**：不点 `dump`/`pos` 就只有那三步会跑 ——
        不像原来的 `--skip-*`（那条路会被 `ALWAYS_STEPS` 补回来，见 2026-09-20 那条坑）。"""
        rc, calls, _ = run(steps_except("dump", "pos"))
        self.assertEqual(calls, ["erp-dump", "pools", "attain"])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_跳过抓取时不校验第一步的成败(self):
        """不点 `dump` 是**调试模式**（人在旁边看着），不是"库坏了也放行"。"""
        rc, calls, _ = run(steps_except("dump"), dump_rc=99)
        self.assertEqual(calls, ["erp-dump", "pos", "pools", "attain"])


class TestDeprecatedFlags(unittest.TestCase):
    """⚠ 报量排查拿掉后，`--date` / `--days-ago` / `--lookback` / `--lookahead`
    **都不生效了** —— 它们原来只喂 `cmd_check`。

    这些参数**故意保留**（老门店的 run.bat / 计划任务里可能还带着），
    但绝不能变成"死参数"：传了要能跑通、不报错，**而且不能静默当没看见**。
    """

    def test_传了也不报错(self):
        rc, calls, _ = run(steps_except() + ["--date", "2026-09-10", "--days-ago", "3",
                                             "--lookback", "2", "--lookahead", "1"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])

    def test_老脚本带来的_skip_要提醒一句(self):
        """⚠ 老门店的 `run-now.bat` 里写死了 `--skip-*` —— 那些开关现在**没用了**
        （跑什么一律由 `--steps` 点名）。

        收下它们只是为了让老脚本不炸，但**必须说出来**：不说的话它们看着像还在生效，
        下一个人会照着一个不存在的行为去调它（AGENTS 里那条"死参数要吭声"）。
        """
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc, calls, _ = run(steps_except() + ["--skip-pos"])
        out = buf.getvalue()
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("--skip-pos", out, "老开关没被点出来")
        self.assertIn("已经不用了", out)
        # ⚠ 说了就要**真的按点名跑** —— `pos` 在点名的名单里，照样得跑
        self.assertIn("pos", calls)

    def test_skip_check_还在但没用(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc, calls, _ = run(steps_except() + ["--skip-check"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(calls, ["dump", "erp-dump", "pos", "pools", "attain"])
        self.assertIn("没用了", buf.getvalue(),
                      "老脚本还在传 --skip-check，得让人知道它不再起作用了")


class TestCliWiring(unittest.TestCase):
    """`python -m src.cli daily` 这条路必须通。"""

    def test_有daily子命令(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        self.assertIn("daily", buf.getvalue())

    def test_daily路由到run_daily(self):
        with mock.patch.object(cli, "cmd_daily", return_value=7) as m:
            with mock.patch("sys.argv", ["x"]):
                rc = cli.main(["daily", "--days-ago", "2"])
        self.assertEqual(rc, 7)
        self.assertEqual(m.call_args[0][0].days_ago, 2)

    def _argv_for(self, cli_argv):
        """走**真解析器**（`cli.main`）跑一次 `daily`，截下交给 `run_daily` 的 argv。

        ⚠ 别用 `mock.Mock()` 当 Namespace —— Mock 的任意属性都返回 Mock，
        `val not in ("", None)` 恒真，于是翻译逻辑测的是"Mock 会怎样"，
        不是"真参数会怎样"。第一版就是这么写的。
        """
        seen = {}

        def fake(argv=None):
            seen["argv"] = argv
            return 0

        import src.run_daily as rd
        with mock.patch.object(rd, "main", fake), \
             mock.patch.object(cli, "cmd_daily", cli.cmd_daily):
            rc = cli.main(cli_argv)
        self.assertEqual(rc, 0)
        return seen["argv"]

    def test_cmd_daily翻译成run_daily的argv(self):
        argv = self._argv_for(["daily", "--days-ago", "2", "--skip-dump"])
        self.assertEqual(argv, ["-c", cli.DEFAULT_CONFIG, "--days-ago", "2", "--skip-dump"])

    def test_cmd_daily_把log_file和verbose带过去(self):
        # ⚠ `-v` 是**全局**参数，得写在子命令**前面**（`check` 也是这规矩）：
        #   `cli -v daily` ✅   `cli daily -v` ❌ unrecognized arguments
        argv = self._argv_for(["-v", "daily", "--log-file", "out/run.log"])
        self.assertIn("--log-file", argv)
        self.assertIn("out/run.log", argv)
        self.assertIn("-v", argv)

    def test_verbose写在子命令后面会被拒(self):
        """把上面那条规矩钉住 —— 免得哪天有人"顺手"改了位置还以为能用。"""
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["daily", "-v"])

    def test_不给可选项时一个都不塞(self):
        """⚠ 别把 argparse 的默认值当"用户意图"硬塞过去 ——

        塞了就会覆盖 `run_daily` 自己的默认值，两处默认值迟早对不上。
        """
        argv = self._argv_for(["daily"])
        self.assertEqual(argv, ["-c", cli.DEFAULT_CONFIG, "--days-ago", "1"])

    def test_daily认得check的每一个参数(self):
        """⚠ 这条是**迁移的保险丝**：老门店 `run.bat` 里写的是 `check`。

        把那种 bat 平滑迁到 `daily`，前提是 `daily` 认 `check` 认得的
        **每一个**参数 —— 少一个，迁移那天就是 "unrecognized arguments"。

        直接从 `build_parser()` **内省**参数集合来比 —— 不手抄清单
        （手抄的迟早和真的对不上），也不正则猜源码。
        `build_parser` 就是为这条测试从 `main` 里抽出来的。

        ⚠ **豁免集合现在是空的。** 曾经豁免过 `--no-refresh`（那时它是个死参数：
        华为那套搬去第 1 步后没人读它）—— 现在它**搬到了 `dump`/`daily` 上**，
        在第 1 步恢复静默续期之后重新有了意义，于是豁免可以取消了。
        **集合为空 = 保险丝最紧**，别往里加东西。
        """
        ap = cli.build_parser()
        sub = None
        for a in ap._actions:
            if hasattr(a, "choices") and a.choices and "check" in a.choices:
                sub = a
        self.assertIsNotNone(sub, "找不到子命令表")

        def opts(name):
            return {o for a in sub.choices[name]._actions for o in a.option_strings
                    if o.startswith("--")}

        missing = opts("check") - opts("daily")
        self.assertEqual(missing, set(),
                         "daily 少了 check 有的参数：%s" % sorted(missing))

    def test_豁免清单必须一直是空的(self):
        """免得以后有人"加个参数顺便加条豁免"，把保险丝泡软。"""
        ap = cli.build_parser()
        sub = [a for a in ap._actions
               if hasattr(a, "choices") and a.choices and "check" in a.choices][0]

        def opts(name):
            return {o for a in sub.choices[name]._actions for o in a.option_strings
                    if o.startswith("--")}
        self.assertEqual(opts("check") - opts("daily"), set())

    def test_no_refresh搬到了第一步才有意义的地方(self):
        """⚠ `--no-refresh` 控的是"华为会话失效时要不要静默续期"。

        华为只在第 1 步（`dump`）被登录 —— 所以它该在 `dump`/`daily` 上，
        **不该**留在 `check` 上（留着的那些天它是个死参数：
        `--help` 里描述着一段已经不存在的行为了）。
        """
        ap = cli.build_parser()
        sub = [a for a in ap._actions
               if hasattr(a, "choices") and a.choices and "check" in a.choices][0]

        def opts(name):
            return {o for a in sub.choices[name]._actions for o in a.option_strings
                    if o.startswith("--")}
        self.assertNotIn("--no-refresh", opts("check"), "check 已经不碰华为了")
        self.assertIn("--no-refresh", opts("dump"))
        self.assertIn("--no-refresh", opts("daily"))

    def test_no_refresh一路传到cmd_dump(self):
        """⚠ 光"解析器里有这个参数"不算数 —— 得真能传到执行的地方。

        中间隔着 `cmd_daily` → `run_daily.main` → `cmd_dump` 三跳，
        任何一跳漏转发，参数就静默丢了（表现是"加了没用"，最难查）。
        """
        seen = {}

        def fake_dump(args):
            seen["no_refresh"] = getattr(args, "no_refresh", "缺失")
            return 0
        # ⚠ **`attain_run` 这个桩不能漏**（2026-09-19 实测踩到）：漏了的话第 5 步
        #   `root=None` ⇒ 读**真腾讯文档**、往**项目根**写一份只有本店一行的
        #   `out/attain-2026.json` —— 开发机上那份真落盘就这么被覆盖过。
        #   ⚠ 这段注释必须在 `with` **之前**：续行链里插注释是语法错（AGENTS.md 坑）。
        # ⚠ `cmd_erp_dump` / `plan_run` 也得挡（2026-09-23）：真 erp-dump 看机器状态
        #   （网络/凭据）决定成败 ⇒ 走到哪一步都不确定（实测两条兄弟用例一条写
        #   项目根一条不写）；plan `root=None` ⇒ 真写 `out/plan-2026.json`
        #   （固定名 tmp 三头并行抢 ⇒ FileNotFoundError 偶发红）。
        with mock.patch.object(cli, "load_config",
                               lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                                                "store_code": "SCN328987",
                                                "marker": "C"}), \
             mock.patch.object(cli, "cmd_dump", fake_dump), \
             mock.patch.object(cli, "cmd_check", lambda a: 0), \
             mock.patch.object(run_daily, "pos_run", lambda **k: PosRun(ok=True)), \
             mock.patch.object(cli, "cmd_pools", lambda a: 0), \
             mock.patch.object(cli, "cmd_erp_dump", lambda a: 0), \
             mock.patch.object(run_daily, "attain_run", lambda **k: {"ok": True}), \
             mock.patch.object(run_daily, "report_run", lambda **k: {"ok": True}), \
             mock.patch.object(run_daily, "inbox_run",
                               lambda **k: {"ok": True, "skipped": "没配收信"}), \
             mock.patch.object(run_daily, "plan_run", lambda **k: {"ok": True}):
            cli.main(["daily"] + steps_except() + ["--no-refresh"])
        self.assertIs(seen["no_refresh"], True)

    def test_不给no_refresh时是False而不是缺失(self):
        """⚠ 用 `getattr(args, "no_refresh", False)` 读它 —— 缺这个属性时
        默认值必须是 False（= 允许续期，也就是原来的行为），不能是 True。"""
        seen = {}

        def fake_dump(args):
            seen["no_refresh"] = getattr(args, "no_refresh", "缺失")
            return 0
        # ⚠ **`attain_run` 这个桩不能漏**（2026-09-19 实测踩到）：漏了的话第 5 步
        #   `root=None` ⇒ 读**真腾讯文档**、往**项目根**写一份只有本店一行的
        #   `out/attain-2026.json` —— 开发机上那份真落盘就这么被覆盖过。
        #   ⚠ 这段注释必须在 `with` **之前**：续行链里插注释是语法错（AGENTS.md 坑）。
        # ⚠ `cmd_erp_dump` / `plan_run` 同上必须挡（2026-09-23，理由见上一条）。
        with mock.patch.object(cli, "load_config",
                               lambda *a, **k: {"erp_store_name": "青岛CBD万达店",
                                                "store_code": "SCN328987",
                                                "marker": "C"}), \
             mock.patch.object(cli, "cmd_dump", fake_dump), \
             mock.patch.object(cli, "cmd_check", lambda a: 0), \
             mock.patch.object(run_daily, "pos_run", lambda **k: PosRun(ok=True)), \
             mock.patch.object(cli, "cmd_pools", lambda a: 0), \
             mock.patch.object(cli, "cmd_erp_dump", lambda a: 0), \
             mock.patch.object(run_daily, "attain_run", lambda **k: {"ok": True}), \
             mock.patch.object(run_daily, "report_run", lambda **k: {"ok": True}), \
             mock.patch.object(run_daily, "inbox_run",
                               lambda **k: {"ok": True, "skipped": "没配收信"}), \
             mock.patch.object(run_daily, "plan_run", lambda **k: {"ok": True}):
            cli.main(["daily"] + steps_except())
        self.assertIs(seen["no_refresh"], False)


class TestNoHuaweiLoginInSteps23(unittest.TestCase):
    """结构断言：第 2、3 步的代码里**不许出现华为登录**。"""

    def test_check步骤读库不读华为(self):
        import inspect
        src = inspect.getsource(cli._run_check)
        self.assertIn("reported_sns_from_db", src)
        self.assertNotIn("cbg.CbgClient", src)
        self.assertNotIn("reported_sns(", src)

    def test_pos步骤不碰网络(self):
        import inspect
        src = inspect.getsource(cli.cmd_pos)
        for banned in ("requests", "CbgClient", "session"):
            self.assertNotIn(banned, src, "cmd_pos 里不该有 %s" % banned)


if __name__ == "__main__":
    unittest.main()


class TestFirstRunGrabsFullHistory(unittest.TestCase):
    """⚠ 库里一片空白 ⇒ 第一次跑 ⇒ 抓**全量**，不是当月。

    为什么：只抓当月的话，**POS 页只有当月一个数、前面几个月全是空的** ——
    而"历史几个月的合规率"恰恰是那个看板最要紧的东西。
    刚装完 / 刚从 1.x 升上来的门店，第一次定时任务就落在这一档。
    """

    def test_没有库时抓全量(self):
        # ⚠ 必须把库探测**钉成"没有"** —— 开发机上 out/cbg-2026.db 是真存在的，
        #   不钉的话这条测的是"开发机有没有库"，不是"没库时会怎样"。
        with mock.patch.object(cli, "_find_pos_db", return_value=None):
            _, _, ns = run()
        # ⚠ 是"**今年**至今"不是"全部历史"（用户 2026-09-17 定：
        #   「跨年不重要，就拉当年的全量就行」）。
        #   原来传 `--all`，而 `dump.py` 拿到跨年数据会**直接报错**要求按年分次抓 ——
        #   老店第一次跑正好卡在这儿。
        self.assertEqual(ns["dump"].year, datetime.date.today().year,
                         "第一次跑没抓今年全量 —— 历史月份会是空的")
        self.assertFalse(ns["dump"].all, "别再用 --all：跨年会被 dump.py 顶回来")

    def test_有库时只抓当月(self):
        with mock.patch.object(cli, "_find_pos_db", return_value=Path("/tmp/cbg-2026.db")):
            _, _, ns = run()
        self.assertFalse(ns["dump"].all)
        self.assertFalse(ns["dump"].year, "有库了就别再抓整年")
        self.assertEqual(ns["dump"].month, "")

    def test_抓全量时也要说一句(self):
        """第一次跑会比较久（要拉全部历史），得先告诉人一声。"""
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(cli, "_find_pos_db", return_value=None):
            with contextlib.redirect_stdout(buf):
                run()
        out = buf.getvalue()
        self.assertIn("第一次跑", out)
        self.assertIn("今年", out)

    def test_有库时不说第一次(self):
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(cli, "_find_pos_db", return_value=Path("/tmp/cbg-2026.db")):
            with contextlib.redirect_stdout(buf):
                run()
        self.assertNotIn("第一次跑", buf.getvalue())

    def test_skip_dump_时不碰库探测(self):
        """`--skip-dump` 是调试模式，不该因为探测库而改变行为。"""
        with mock.patch.object(cli, "_find_pos_db",
                               side_effect=AssertionError("不该探测")):
            rc, calls, _ = run(steps_except("dump", "pools"))
        self.assertEqual(calls, ["erp-dump", "pos", "attain"])


class TestDaily要把POS推送跑起来(unittest.TestCase):
    """⚠ 2026-09-19 修的**一条静默缺口**（架构审阅的隔离探针发现、我复核过）。

        run_daily.py:280   cli.cmd_pos(argparse.Namespace(db=""))      ← 只传了 db
        run_daily.py:290   cli.cmd_pools(Namespace(config=…, no_mail=…, no_push=…))

    而 `_maybe_pos_push()` 的第一句是 `if not config_path: return`。

    ⇒ **`daily` 每天算了 POS，却从来没推过 POS。**
      门店只有手动跑 `python -m src.cli pos` 才收得到那条推送。
      **1410 条测试一条都没抓到** —— 因为下面 `_Rec.patch` 把 `cmd_pos`
      **整个 mock 掉了**，"只传了 db"这件事根本没人看见。

    ⇒ 所以这一组**不 mock `cmd_pos`**，只把**外部发送端**换掉。
      这正是架构审阅 §5-B 的验收要求：
      「只替换外部发送端，**不只断言顶层 mock 被调用**」。
    """

    def _run_isolated(self, argv):
        """用模块级 `run()` 跑，但**额外把 `cmd_pools` 挡住**。

        ⚠ `run()` 里的 `_Rec.patch()` **只挡了 dump / check / pos，没挡 pools** ——
        所以它每跑一次都会**真算一遍四池**、往项目根的 `out/` 写
        `pools-2026.json` + `双平台数据对比-*.xlsx`（实测 **1.5 秒/次**，
        跑全套时这十几条就占了半分钟）。而且它读的是**开发机那个真库**。

        ⚠ **这里不顺手修 `run()`** —— 那要动十几条 `calls` 断言，
          属于「测试数据隔离」那一步（四项文档 阶段 1.3），别混进这一步。
          本组测试自己隔离就行。
        """
        with mock.patch.object(cli, "cmd_pools", lambda a: cli.EXIT_OK):
            return run(argv)

    def test_config_和通知开关要传下去(self):
        """契约层：`daily` 交给 POS 执行模块的关键字必须带 `config_path` + 两个开关。

        ⚠ 这条钉的是**那个缺陷本身**（daily 每天算 POS 却从来不推）。
        2026-09-19 起挂点从"手工拼的 Namespace"换成了**关键字参数** ——
        约束没变，只是现在漏一个在调用点就看得出来。
        """
        _, _, ns = self._run_isolated(steps_except() + ["-c", "config/store-X.yaml"])
        self.assertEqual(ns["pos"]["config_path"], "config/store-X.yaml",
                         "POS 拿不到配置 ⇒ 推送那边第一句就 return")
        self.assertFalse(ns["pos"]["no_mail"])
        self.assertFalse(ns["pos"]["no_push"])

    def test_开关也要跟着传(self):
        _, _, ns = self._run_isolated(steps_except() + ["--no-mail", "--no-push"])
        self.assertTrue(ns["pos"]["no_mail"], "--no-mail 没传到 POS")
        self.assertTrue(ns["pos"]["no_push"], "--no-push 没传到 POS")

    def test_还要把日志交下去(self):
        """⚠ `emit` 必须传 —— 不传的话 `daily` 的日志里就没有那几行分数了
        （以前那些行是 `cmd_pos` 自己 print 的）。"""
        _, _, ns = self._run_isolated(steps_except())
        self.assertTrue(callable(ns["pos"].get("emit")), "emit 没传下去，日志里会缺 POS 那几行")

    def setUp(self):
        """造一个**像样的临时安装目录**。

        ⚠ 2026-09-19 起 POS 走**执行模块**（`app.pos`），它自己按 root 找库、自己读配置 ⇒
        不能再像以前那样 patch `cli.ROOT` / `cli._find_pos_db` 去骗它。
        顺带这条测试比以前更真：**库和配置都是真文件**。
        """
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        (self.root / "out" / "cbg-2026.db").write_bytes(b"")   # 只要存在；load 被换掉
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.cfg_file = self.root / "config" / "store-X.yaml"
        self.cfg_file.write_text("erp_store_name: 青岛CBD万达店\nstore_code: SCN328987\n"
                                 "marker: C\n", encoding="utf-8")

    def _run_real(self, argv):
        """跑一次 daily —— **不 mock 执行模块**，只换外部发送端。

        返回 `(退出码, 记录到的发送调用)`。
        """
        from src import mailer, wecom
        from src.features.compliance.pos import pos_report

        sent = []
        root = self.root
        db = root / "out" / "cbg-2026.db"
        store_cfg = {"erp_store_name": "青岛CBD万达店", "store_code": "SCN328987",
                     "marker": "C", "_path": str(self.cfg_file)}

        def fake_send(mc, subject, body, attachments=(), prefix=None):
            sent.append({"subject": subject, "prefix": prefix, "to": list(mc.recipients)})

        def fake_push(wc, ctx, lines, head):
            sent.append({"wecom": head})
            return "已发送"

        #: 一封**配置齐全**的邮件配置 —— 让 `mailer.should_send` **真跑**，
        #: 而不是把它也 mock 掉（那样就验不出"开关传下去没有"了）。
        mc_ok = mailer.MailConfig(enabled=True, host="smtp.example.com", port=465,
                                  username="store@example.com", password="x",
                                  recipients=["boss@example.com"], when="always")

        ps = [
            mock.patch.object(cli, "cmd_dump", lambda a: cli.EXIT_OK),
            mock.patch.object(cli, "cmd_pools", lambda a: cli.EXIT_OK),
            mock.patch.object(cli, "load_config", lambda *a, **k: store_cfg),
            # ⚠ 落盘的 JSON 和"找库"都归 `app.pos` 管了 ⇒ 直接把它那份 ROOT
            #   指到临时目录（`cli.ROOT` 已经管不到这条路）。
            mock.patch.object(app_pos, "ROOT", root),
            mock.patch.object(pos_report, "load", lambda conn: ([], [])),
            mock.patch.object(mailer, "load_mail_config", lambda c, r: mc_ok),
            mock.patch.object(mailer, "send", fake_send),
            mock.patch.object(wecom, "load_wecom_config",
                              lambda c, r: wecom.WecomConfig(enabled=False)),
            mock.patch.object(wecom, "push_pos", fake_push),
            # ⚠ 第 5 步也要挡：它 `root=None` ⇒ 会去读**真腾讯文档**、
            #   并往**项目根**写 `out/attain-2026.json`（开发机那份真落盘被覆盖过）。
            mock.patch.object(run_daily, "attain_run", lambda **k: {"ok": True}),
            # ⚠ 2026-09-21（M18）第 6/7 步也**必须挡**（它们 `root=None` ⇒ 会去读
            #   **项目根**那个真库、并往真 `out/report/` 写包与指纹 ——
            #   实测污染过一次：`out/report/pending/cbg-*.db` + 300KB 的 state.db）。
            mock.patch.object(run_daily, "report_run", lambda **k: {"ok": True}),
            mock.patch.object(run_daily, "inbox_run",
                              lambda **k: {"ok": True, "skipped": "没配收信"}),
            # ⚠ plan 同理必须挡（同上：root=None ⇒ 真写项目根 out/plan-2026.json，
            #   固定名 tmp 三头并行抢 ⇒ FileNotFoundError 偶发红）
            mock.patch.object(run_daily, "plan_run", lambda **k: {"ok": True}),
        ]
        for p in ps:
            p.start()
        try:
            rc = run_daily.main(argv)
        finally:
            for p in ps:
                p.stop()
        return rc, sent

    def test_daily_会真的推_POS(self):
        """行为层：**不 mock `cmd_pos`**，只换发送端 —— 推送必须真的被尝试。

        ⚠ 修之前这条是红的：`_maybe_pos_push` 拿到 `config=None`，第一句就 return，
          `mailer.send` 一次都不会被调到。
        ⚠ 顺带钉住**主题前缀**：不传 `prefix=` 的话，POS 那封会顶着配置里的
          `[报量对账]` 发出去，门店会以为发重了（`mailer.py:236` 的注释）。
        """
        rc, sent = self._run_real(steps_except() + ["-c", str(self.cfg_file)])
        self.assertEqual(rc, cli.EXIT_OK)
        mails = [s for s in sent if "subject" in s]
        self.assertEqual(len(mails), 1, "daily 没有推 POS 邮件 —— 缺口又回来了？")
        self.assertEqual(mails[0]["prefix"], mailer.POS_SUBJECT_PREFIX)
        self.assertEqual(mails[0]["to"], ["boss@example.com"])

    def test_两个开关都给了就不许发(self):
        """`--no-push --no-mail` 是"只算不发" —— 一个都不许发出去。

        ⚠ 这条**单独看是假绿的** —— 修那个缺口之前它也是绿的（因为**什么都不发**）。
          所以要配着 `test_daily_会真的推_POS` 一起看：
          **那条证明"不给开关就会发"，这条证明"给了就不发"。**
        """
        rc, sent = self._run_real(steps_except()
                                  + ["-c", str(self.cfg_file), "--no-push", "--no-mail"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(sent, [], "给了 --no-push --no-mail 还发出去了")
