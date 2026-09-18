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

from src import cli, run_daily


class _Rec:
    """记录调用顺序和参数，并按脚本返回退出码。"""

    def __init__(self, dump_rc=0, check_rc=0, pos_rc=0):
        self.codes = {"dump": dump_rc, "check": check_rc, "pos": pos_rc}
        self.calls = []
        self.namespaces = {}

    def _mk(self, name):
        def fn(args):
            self.calls.append(name)
            self.namespaces[name] = args
            return self.codes[name]
        return fn

    def patch(self):
        return (
            mock.patch.object(cli, "cmd_dump", self._mk("dump")),
            mock.patch.object(cli, "cmd_check", self._mk("check")),
            mock.patch.object(cli, "cmd_pos", self._mk("pos")),
        )


def run(argv=None, **codes):
    """跑一次 run_daily，返回 (退出码, 调用顺序, 各步收到的 Namespace)。"""
    r = _Rec(**codes)
    ps = r.patch()
    for p in ps:
        p.start()
    try:
        rc = run_daily.main(argv if argv is not None else [])
    finally:
        for p in ps:
            p.stop()
    return rc, r.calls, r.namespaces


class TestHappyPath(unittest.TestCase):
    def test_三步都跑_按顺序(self):
        rc, calls, _ = run()
        self.assertEqual(calls, ["dump", "pos"])
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
        self.assertEqual(ns["pos"].db, "")


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
        self.assertEqual(calls, ["dump", "pos"])
        self.assertEqual(rc, cli.EXIT_OK)


class TestDiffIsNotFailure(unittest.TestCase):
    """`EXIT_DIFF`(3) = **跑通了，只是有差异** —— 每天都在发生，不是故障。"""

    def test_有差异时第三步照跑(self):
        rc, calls, _ = run(check_rc=cli.EXIT_DIFF)
        self.assertEqual(calls, ["dump", "pos"])

    def test_有差异但POS挂了_退POS的码(self):
        rc, _, _ = run(check_rc=cli.EXIT_DIFF, pos_rc=cli.EXIT_FETCH)
        self.assertEqual(rc, cli.EXIT_FETCH)


class TestExitCodeIsFirstNonSuccess(unittest.TestCase):
    def test_全好退0(self):
        self.assertEqual(run()[0], cli.EXIT_OK)

    def test_只有第三步坏(self):
        self.assertEqual(run(pos_rc=cli.EXIT_FETCH)[0], cli.EXIT_FETCH)

    def test_跳过抓取(self):
        rc, calls, _ = run(["--skip-dump"])
        self.assertEqual(calls, ["pos"])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_跳过POS(self):
        rc, calls, _ = run(["--skip-pos"])
        self.assertEqual(calls, ["dump"])

    def test_两个skip合起来只剩对账(self):
        """⚠ `--skip-dump`/`--skip-pos` 只关**第 1、3 步** —— 对账是这条流程的本体，
        没有开关能关掉它（想只对账就直接跑 `python -m src.cli check`）。"""
        rc, calls, _ = run(["--skip-dump", "--skip-pos"])
        self.assertEqual(calls, [])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_跳过抓取时不校验第一步的成败(self):
        """`--skip-dump` 是**调试模式**（人在旁边看着），不是"库坏了也放行"。"""
        rc, calls, _ = run(["--skip-dump"], dump_rc=99)
        self.assertEqual(calls, ["pos"])


class TestDeprecatedFlags(unittest.TestCase):
    """⚠ 报量排查拿掉后，`--date` / `--days-ago` / `--lookback` / `--lookahead`
    **都不生效了** —— 它们原来只喂 `cmd_check`。

    这些参数**故意保留**（老门店的 run.bat / 计划任务里可能还带着），
    但绝不能变成"死参数"：传了要能跑通、不报错，**而且不能静默当没看见**。
    """

    def test_传了也不报错(self):
        rc, calls, _ = run(["--date", "2026-09-10", "--days-ago", "3",
                            "--lookback", "2", "--lookahead", "1"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(calls, ["dump", "pos"])

    def test_skip_check_还在但没用(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc, calls, _ = run(["--skip-check"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(calls, ["dump", "pos"])
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
        with mock.patch.object(cli, "cmd_dump", fake_dump), \
             mock.patch.object(cli, "cmd_check", lambda a: 0), \
             mock.patch.object(cli, "cmd_pos", lambda a: 0):
            cli.main(["daily", "--no-refresh"])
        self.assertIs(seen["no_refresh"], True)

    def test_不给no_refresh时是False而不是缺失(self):
        """⚠ 用 `getattr(args, "no_refresh", False)` 读它 —— 缺这个属性时
        默认值必须是 False（= 允许续期，也就是原来的行为），不能是 True。"""
        seen = {}

        def fake_dump(args):
            seen["no_refresh"] = getattr(args, "no_refresh", "缺失")
            return 0
        with mock.patch.object(cli, "cmd_dump", fake_dump), \
             mock.patch.object(cli, "cmd_check", lambda a: 0), \
             mock.patch.object(cli, "cmd_pos", lambda a: 0):
            cli.main(["daily"])
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
            rc, calls, _ = run(["--skip-dump", "--skip-pools"])
        self.assertEqual(calls, ["pos"])
