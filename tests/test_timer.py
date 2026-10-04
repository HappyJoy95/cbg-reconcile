"""**计时模块的唤醒接口**（`src/modules/timer/`）—— 用户 2026-09-20 要的那件事：

> 「要给个接口，**让各个模块设置什么时间点唤醒，以及唤醒做什么**。
>   然后**定时器看到到点了就做这个**。设置要可以设置**日期、时间，每周几，每个月几号**这种」

分四层测，**从纯到脏**：

| 层 | 测什么 | 为什么单独一层 |
|---|---|---|
| `when.py` | 时刻表的算术（月末 / 跨年 / 工作日 / 指定日期） | 纯函数，表驱动，错了当场看得出来 |
| 注册表 | 模块**声明**得对不对（默认值、非法值、有 whens 必须有 cmd） | 声明错了要**启动自检**就报，别等某一跳悄悄不跑 |
| `due()` / `tick()` | 到点判定三条（窗口 / 去重 / 正在跑） | 这三条错一条就是"重复跑"或"永远不跑" |
| `run_daily --steps` | 派发出去那条命令的语义 | 用 `--skip-*` 表达不了"只在周一算达成" |

⚠ 全篇**不 sleep、不真起进程**：`tick(spawn=…)` 的 `spawn` 是注入的，
`now=` 也是注入的 —— 时间相关的测试只要碰真表就会变成又慢又飘的测试。
"""

import argparse
import datetime
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import cli, run_daily
from src.features import registry
from src.modules import timer
from src.modules.timer import store
from src.modules.timer.when import When, describe
from src.storage import runlog

ROOT = Path(__file__).resolve().parents[1]


def patch_step_run(command, fn=None, **kwargs):
    """替换注册步骤的执行入口，避免测试误跑真实采集。"""
    step = next(item for item in registry.BUILTIN_STEPS if item.cmd == command)
    if fn is not None:
        kwargs["new"] = fn
    return mock.patch.object(step, "run", **kwargs)


def _next_at(hhmm, now=None):
    """「下一个 HH:MM」的**真实**日期时间字符串 —— 按当前时钟算，别写死今天。

    ⚠⚠ 2026-09-21 晚 21:42 踩出来的：下面两条测试把"每天 21:00 的下一趟"**写死**成
      `2026-09-21 21:00`。**21:00 一过**，"下一个 21:00"就是明天了 ⇒
      这两条**每天 21 点以后必红**（21:42 跑三头，就是各 6~8 条红，
      看着像代码坏了，其实测试自己在跟时钟较劲）。
      ⚠ 别改成"跳过夜间"之类 —— 那是把一条能发现真问题的断言阉掉。
    """
    now = now or datetime.datetime.now()
    at = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    if at <= now:
        at += datetime.timedelta(days=1)
    return at.strftime("%Y-%m-%d %H:%M")


def _root_with_db():
    """带最小结构的临时安装目录（和 `test_modules.py` 同一个套路）。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir()
    db = root / "out" / "cbg-2026.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    return tmp, root, db


class Test唤醒时刻的算术(unittest.TestCase):
    """`When` 是**纯函数** —— 跨月、跨年、月末这些边界必须表驱动地钉住。

    ⚠ 基准时间挑的是 **2026-09-20（周日）13:40**：
      周日能同时试出"每周一"（刚过去）和"工作日"（周五那次）两种最近 slot。
    """

    NOW = datetime.datetime(2026, 9, 20, 13, 40)          # 周日

    def test_每天(self):
        w = When()                                        # 默认 = 每天 21:00
        self.assertEqual(w.text(), "每天 21:00")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 9, 19, 21, 0),
                         "13:40 还没到 21:00 ⇒ 最近一次是昨天")
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 9, 20, 21, 0))

    def test_刚过点(self):
        w = When(time="13:00")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 9, 20, 13, 0))
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 9, 21, 13, 0))

    def test_每周几(self):
        w = When(kind="weekly", weekdays=(1,), time="08:30")
        self.assertEqual(w.text(), "每周一 08:30")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 9, 14, 8, 30))
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 9, 21, 8, 30))

    def test_一周里多天(self):
        w = When(kind="weekly", weekdays=(4, 1), time="09:00")
        self.assertEqual(w.text(), "每周一、周四 09:00", "周几要排好序再说")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 9, 17, 9, 0))
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 9, 21, 9, 0))

    def test_工作日(self):
        """`weekly` 不给周几 = **工作日**（用户要的"工作日"就是这个）。"""
        w = When(kind="weekly")
        self.assertEqual(w.text(), "工作日 21:00")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 9, 18, 21, 0), "周五那次")
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 9, 21, 21, 0),
                         "周末不跑，下一次是周一")

    def test_选满七天就等于每天(self):
        self.assertEqual(When(kind="weekly", weekdays=(1, 2, 3, 4, 5, 6, 7)).text(), "每天 21:00")

    def test_每月几号(self):
        w = When(kind="monthly", day=1, time="09:00")
        self.assertEqual(w.text(), "每月 1 号 09:00")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 9, 1, 9, 0))
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 10, 1, 9, 0))

    def test_每月最后一天(self):
        w = When(kind="monthly", day=0, time="09:00")
        self.assertEqual(w.text(), "每月最后一天 09:00")
        self.assertEqual(w.slot(self.NOW), datetime.datetime(2026, 8, 31, 9, 0))
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 9, 30, 9, 0))
        self.assertEqual(w.next_after(datetime.datetime(2026, 1, 31, 10, 0)),
                         datetime.datetime(2026, 2, 28, 9, 0), "2 月要认闰年之外的天数")

    def test_每月31号_小月顺延到月末(self):
        """⚠ 2 月没有 31 号 —— **顺延到月末**，不许跳过那个月（跳了就变成两个月才跑一次）。"""
        w = When(kind="monthly", day=31, time="09:00")
        self.assertEqual(w.slot(datetime.datetime(2026, 9, 20, 13, 40)),
                         datetime.datetime(2026, 8, 31, 9, 0))
        self.assertEqual(w.next_after(datetime.datetime(2026, 9, 20, 13, 40)),
                         datetime.datetime(2026, 9, 30, 9, 0))
        self.assertEqual(w.next_after(datetime.datetime(2027, 2, 1, 0, 0)),
                         datetime.datetime(2027, 2, 28, 9, 0))

    def test_跨年(self):
        w = When(kind="monthly", day=5, time="09:00")
        self.assertEqual(w.next_after(datetime.datetime(2026, 12, 31, 23, 0)),
                         datetime.datetime(2027, 1, 5, 9, 0))

    def test_指定日期只跑一次(self):
        w = When(kind="once", date="2026-10-01", time="09:00")
        self.assertEqual(w.text(), "2026-10-01 09:00")
        self.assertIsNone(w.slot(self.NOW), "还没到那天 ⇒ 没有'最近一次'")
        self.assertEqual(w.next_after(self.NOW), datetime.datetime(2026, 10, 1, 9, 0))
        after = datetime.datetime(2026, 10, 2, 0, 0)
        self.assertEqual(w.slot(after), datetime.datetime(2026, 10, 1, 9, 0))
        self.assertIsNone(w.next_after(after), "跑过就再也不跑")

    # ------------------------------------------------------------------ 校验
    def test_非法值当场报(self):
        for kw, hit in ((dict(kind="每小时"), "不认识的频率"),
                        (dict(time="25:00"), "时间"),
                        (dict(time="2100"), "HH:MM"),
                        (dict(kind="weekly", weekdays=(8,)), "周几"),
                        (dict(kind="monthly", day=32), "几号"),
                        (dict(kind="once"), "指定日期"),
                        (dict(kind="once", date="2026-13-01"), "YYYY-MM-DD")):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError) as cm:
                    When(**kw)
                self.assertIn(hit, str(cm.exception))

    def test_from_dict_认不出来要抛(self):
        """⚠ **不许回落成每天 21:00** —— 存盘里一个错字会让"只在周一跑"变成"每天跑"。"""
        with self.assertRaises(ValueError):
            When.from_dict({"kind": "每天"})
        with self.assertRaises(ValueError):
            When.from_dict("每天 21:00")
        self.assertEqual(When.from_dict({"kind": "daily", "time": "08:00"}).text(), "每天 08:00")

    def test_存盘形状只留用得上的字段(self):
        self.assertEqual(When().as_dict(), {"kind": "daily", "time": "21:00"})
        self.assertEqual(When(kind="weekly", weekdays=(1,)).as_dict(),
                         {"kind": "weekly", "time": "21:00", "weekdays": [1]})
        self.assertEqual(When(kind="monthly", day=5, time="09:00").as_dict(),
                         {"kind": "monthly", "time": "09:00", "day": 5})
        self.assertEqual(When(kind="once", date="2026-10-01", time="09:00").as_dict(),
                         {"kind": "once", "time": "09:00", "date": "2026-10-01"})

    def test_存盘来回一趟不走样(self):
        for w in (When(), When(kind="weekly", weekdays=(1, 4), time="08:30"),
                  When(kind="weekly"), When(kind="monthly", day=0, time="09:00"),
                  When(kind="once", date="2026-10-01", time="09:00")):
            with self.subTest(w=w.text()):
                self.assertEqual(When.from_dict(w.as_dict()).as_dict(), w.as_dict())

    def test_一句话的说法(self):
        self.assertEqual(describe([]), "只手动跑")
        self.assertEqual(describe([When(), When(kind="weekly", weekdays=(1,))]),
                         "每天 21:00 / 每周一 21:00")


class Test注册表里的唤醒声明(unittest.TestCase):
    """「什么时候唤醒我、醒来做什么」是**功能模块自己声明**的（用户 2026-09-20 的接口）。"""

    def test_每个步骤都有默认唤醒时刻(self):
        """⚠ 业务那几步的默认值 = 今天那条计划任务的时间点（21:00）——
        定时器上线当天的行为要跟之前**逐字一致**，门店零感知。

        ⚠ **例外两个**：
          * 自动更新（2026-09-20）：按小时跑（`:17`），不跟着每天那趟走；
          * 收取门店上报（2026-09-21，M19）：**21:30** —— 门店那趟 21:00 跑完才发信，
            同一时刻收只会收个空（"区长今晚就看到"就变成"明天才看到"）。
        """
        for s in registry.wakes():
            with self.subTest(cmd=s.cmd):
                self.assertTrue(s.whens)
                if s.cmd == "autoupdate":
                    self.assertEqual(s.whens[0].kind, "hourly")
                    continue
                if s.cmd == "report-inbox":
                    self.assertEqual(s.whens[0].text(), "每天 21:30")
                    continue
                if s.cmd == "report":
                    self.assertEqual(s.whens[0].text(), "每天 21:15")
                    continue
                self.assertEqual(s.whens[0].text(), "每天 21:00")

    def test_五个步骤都可唤醒(self):
        """⚠ 2026-09-20 加了 `erp-dump`（抓取云商数据）—— 顺序就是 `Step.order`，
        也就是**同一个时间点跑的时候的执行顺序**（先抓、后算）。"""
        # ⚠ 别写死 —— 断言"这几步都能唤醒"，注册表里加步骤不该把这条弄红
        cmds = [s.cmd for s in registry.wakes()]
        # ⚠ 2026-09-21 晚：「上报数据」也有自己的时刻了（21:15）——
        #   原来它 `whens=()` 时**不在这张表里**，界面上那个开关因此永远开不了。
        for want in ("dump", "erp-dump", "pos", "pools", "attain", "autoupdate",
                     "report", "report-inbox"):
            self.assertIn(want, cmds)

    def test_没声明的步骤不进唤醒表(self):
        """⚠ 「没声明」和「声明了每天 21:00」是两件事 —— 前者=只手动跑。"""
        with mock.patch.object(registry, "all_steps",
                               lambda: [registry.Step(cmd="x", label="临时",
                                                      whens=(registry.DEFAULT_WHENS[0],)),
                                        registry.Step(cmd="y", label="只手动")]):
            self.assertEqual([s.cmd for s in registry.wakes()], ["x"])
            self.assertEqual(registry.step_by_cmd("y").cmd, "y")
            self.assertIsNone(registry.step_by_cmd("没有这个"))

    def test_声明了whens就必须有cmd(self):
        """定时器是**按 cmd 起进程**的 —— 没有 cmd 它不知道怎么跑。"""
        bad = registry.validate()
        self.assertEqual(bad, [])
        with mock.patch.object(registry, "all_steps",
                               lambda: [registry.Step(cmd="", label="没名字",
                                                      whens=(When(),))]):
            self.assertTrue(any("没有 cmd" in m for m in registry.validate()))

    def test_注册表校验盯着非法声明(self):
        with mock.patch.object(registry, "all_steps",
                               lambda: [registry.Step(cmd="z", label="写错了",
                                                      whens=("每天 21:00",))]):
            self.assertTrue(any("不是 When" in m for m in registry.validate()))


class Test唤醒设置的存盘(unittest.TestCase):
    """`.secrets/wakes.json` —— 跟"注册了哪几条系统计划任务"**分开存**。

    ⚠ 分开的理由：那个文件的生命周期跟着**计划任务**走（`remove_all` 会整个删掉），
      而"这台机器几点干活"跟计划任务在不在没关系。

    ⚠ 形状 2026-09-20 改过一次：原来是 `{cmd: [When, …]}`，加了那个**开关滑块**之后
      一条设置里有**两样东西**（时间点 + 开没开）⇒ 包成 `{cmd: {whens, enabled}}`。
    """

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def test_没设置过就是全默认(self):
        self.assertEqual(store.load(self.root), ({}, []))
        self.assertEqual(store.whens_of(self.root, "attain"), [])
        self.assertTrue(store.enabled_of(self.root, "attain"), "没动过 ⇒ 注册着")

    def test_改了时间能读回来(self):
        store.set_cmd(self.root, "attain", [When(kind="weekly", weekdays=(1,), time="08:30")])
        got = store.whens_of(self.root, "attain")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text(), "每周一 08:30")
        self.assertTrue(store.enabled_of(self.root, "attain"), "改时间不影响开关")

    def test_开关能关能开_而且不动时间(self):
        """⭐ 用户 2026-09-20：「开了就注册到定时器，不开就不注册」。

        ⚠ 关掉**不等于抹掉时间点** —— 时间点留着，再打开还是原来那个时间。
        """
        store.set_cmd(self.root, "pools", [When(time="08:15")])
        store.set_enabled(self.root, "pools", False)
        self.assertFalse(store.enabled_of(self.root, "pools"))
        self.assertEqual(store.whens_of(self.root, "pools")[0].time, "08:15",
                         "关掉把时间点也弄丢了")
        store.set_enabled(self.root, "pools", True)
        self.assertTrue(store.enabled_of(self.root, "pools"))
        self.assertEqual(store.whens_of(self.root, "pools")[0].time, "08:15")

    def test_只写开关也能存(self):
        """⚠ 只拨开关、没动时间 ⇒ 文件里那条**只有 `enabled`** ——
        读的时候时间要回落到注册表默认值，不能读成"没有时间点"。"""
        store.set_enabled(self.root, "dump", False)
        raw = json.loads(store.path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(raw["dump"], {"enabled": False})
        self.assertEqual(store.whens_of(self.root, "dump"), [])
        self.assertFalse(store.enabled_of(self.root, "dump"))

    def test_恢复默认时间不动开关(self):
        store.set_enabled(self.root, "attain", False)
        store.set_cmd(self.root, "attain", [When(time="08:00")])
        store.set_cmd(self.root, "attain", [])
        self.assertEqual(store.whens_of(self.root, "attain"), [])
        self.assertFalse(store.enabled_of(self.root, "attain"),
                         "「恢复默认时间」不该顺手把开关也打开")

    def test_恢复默认_开关也回默认(self):
        store.set_enabled(self.root, "attain", False)
        store.clear(self.root, "attain")
        self.assertTrue(store.enabled_of(self.root, "attain"))

    def test_写坏了要说出来而不是当没设置(self):
        """⚠ 静默当成"没设置过"= 用户设的时间点悄悄变回默认、下一跳就按默认时间跑。"""
        store.path(self.root).parent.mkdir(parents=True, exist_ok=True)
        store.path(self.root).write_text("{ 这不是 json", encoding="utf-8")
        over, problems = store.load(self.root)
        self.assertEqual(over, {})
        self.assertTrue(problems, "读坏了必须报出来")

    def test_坏了一条不影响别的条(self):
        store.path(self.root).parent.mkdir(parents=True, exist_ok=True)
        store.path(self.root).write_text(
            '{"attain": {"whens": [{"kind": "每天"}]},'
            ' "dump": {"whens": [{"kind": "daily", "time": "08:00"}]}}', encoding="utf-8")
        over, problems = store.load(self.root)
        self.assertEqual(list(over), ["dump"])
        self.assertTrue(any("attain" in p for p in problems))

    def test_改一步不动别步(self):
        store.set_cmd(self.root, "attain", [When(time="08:00")])
        store.set_cmd(self.root, "dump", [When(time="09:00")])
        self.assertEqual(store.whens_of(self.root, "attain")[0].time, "08:00")
        self.assertEqual(store.whens_of(self.root, "dump")[0].time, "09:00")

    def test_老的列表写法还认得(self):
        """升级路径：文件里可能还是 `{"attain": [ … ]}` 那种老形状 —— 照样读得出来。"""
        store.path(self.root).parent.mkdir(parents=True, exist_ok=True)
        store.path(self.root).write_text(
            '{"attain": [{"kind": "daily", "time": "07:30"}]}', encoding="utf-8")
        self.assertEqual(store.whens_of(self.root, "attain")[0].time, "07:30")
        self.assertTrue(store.enabled_of(self.root, "attain"))


class Test任务表与到点判定(unittest.TestCase):
    """`tasks()` / `due()` / `next_at()` —— 定时器的"该不该跑"。"""

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def _at(self, hh=21, mm=5, day=20):
        return datetime.datetime(2026, 9, day, hh, mm)

    def test_任务表带上给人看的说法(self):
        rows = {t["cmd"]: t for t in timer.tasks(self.root)}
        self.assertEqual(rows["attain"]["when_text"], "每天 21:00")
        self.assertTrue(rows["attain"]["declared"])
        self.assertFalse(rows["attain"]["overridden"])
        self.assertTrue(rows["attain"]["enabled"], "默认那几件是勾着的")
        # ⚠ 「下次」要**按这一步自己的时间点**算 —— 各模块时间点不同，
        #   给一个全局值等于让门店自己心算。
        self.assertRegex(rows["attain"]["next_text"], r"^\d\d-\d\d \d\d:\d\d$")

    def test_改过的会在表里标出来(self):
        # ⚠ 后面的步骤不能早于数据抓取 ⇒ 先把抓取挪到 08:00（两个抓取会一起同步）
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "attain", [{"kind": "weekly", "weekdays": [1], "time": "08:30"}])
        row = [t for t in timer.tasks(self.root) if t["cmd"] == "attain"][0]
        self.assertEqual(row["when_text"], "每周一 08:30")
        self.assertTrue(row["overridden"])

    def test_改不认识的步骤要抛(self):
        with self.assertRaises(ValueError):
            timer.set_whens(self.root, "没有这一步", [When()])

    def test_到点了就该唤醒(self):
        got = [d["cmd"] for d in timer.due(self.root, now=self._at())]
        # ⚠ 2026-09-21（M22）：加了 `plan`（月度生意计划，order=46，紧跟 attain）
        self.assertEqual(got, ["dump", "erp-dump", "pos", "pools", "attain", "plan"],
                         "按 order 排好（先抓玲珑、再抓云商、最后分析）")

    def test_没到点不唤醒(self):
        self.assertEqual(timer.due(self.root, now=datetime.datetime(2026, 9, 20, 20, 59)), [])

    def test_过了窗口就不补跑(self):
        """用户 2026-09-20 选的：「不补，错过了就等下一个时间点」。"""
        late = datetime.datetime(2026, 9, 20, 21, 0) + datetime.timedelta(
            minutes=timer.WAKE_WINDOW_MINUTES + 1)
        # ⚠ 21:31 那一刻，**自动更新**（每小时 :17）那一跳还在窗口里（21:17 + 30 分），
        #   而 21:30 那一跳（收取门店上报，M19）**刚开始** ⇒ 两条都在。
        #   ⇒ "什么都不该跑"要挑一个连它们也不在窗口里的时刻（见下面 22:05）。
        # ⚠ 21:31：report（21:15，窗口到 21:45）**也还在窗口里** ——
        #   2026-09-21 晚它有了自己的时刻（21:15），所以这里多了它。
        self.assertEqual([d["cmd"] for d in timer.due(self.root, now=late)],
                         ["report", "autoupdate", "report-inbox"])
        later = datetime.datetime(2026, 9, 20, 22, 5)      # 21:15 + 30 分 = 21:45 已过
        self.assertEqual(timer.due(self.root, now=later), [], "过了窗口的一律不补")
        inside = datetime.datetime(2026, 9, 20, 21, 0) + datetime.timedelta(
            minutes=timer.WAKE_WINDOW_MINUTES - 1)
        # 21:29 在 21:00 那批的窗口里（5 步）；21:17 那次自动更新也还在窗口里 ⇒ 6 条
        got = [d["cmd"] for d in timer.due(self.root, now=inside)]
        self.assertIn("autoupdate", got)
        self.assertIn("dump", got)

    def test_这个slot跑过就不跑了(self):
        runlog.record(timer.WAKE_KIND, True, note="跑过了",
                      detail={"slot": "2026-09-20 21:00",
                              "steps": ["dump", "erp-dump", "pos"]},
                      root=self.root)
        got = [d["cmd"] for d in timer.due(self.root, now=self._at())]
        self.assertEqual(got, ["pools", "attain", "plan"], "跑过的几步不再跑")

    def test_失败也算跑过(self):
        """⚠⚠ 按"试过没"去重，**不按"成没成"** ——
        按成功去重的话，一次失败会被每 30 秒重试一遍（云商不能并行登录，会一起挂）。"""
        runlog.record(timer.WAKE_KIND, False, why="网断了",
                      detail={"slot": "2026-09-20 21:00",
                              "steps": ["dump", "erp-dump", "pos", "pools", "attain",
                                        "plan"]},
                      root=self.root)
        self.assertEqual(timer.due(self.root, now=self._at()), [])

    def test_没勾的步骤不唤醒(self):
        with mock.patch.object(timer, "enabled_cmds",
                               lambda root=None: ("dump", "attain")):
            self.assertEqual([d["cmd"] for d in timer.due(self.root, now=self._at())],
                             ["dump", "attain"])

    def test_下次什么时候(self):
        """⚠ 白天问"下次什么时候"，答案往往是**每小时那次自动更新**（:17）——
        它确实就是下一件会自动跑的事。整批那趟要挑一个 :17 之后的时刻才看得到。"""
        self.assertEqual(timer.next_at(self.root, now=datetime.datetime(2026, 9, 20, 20, 0)),
                         "2026-09-20 20:17")
        self.assertEqual(timer.next_at(self.root, now=datetime.datetime(2026, 9, 20, 20, 20)),
                         "2026-09-20 21:00")
        self.assertEqual(timer.next_at(self.root, now=datetime.datetime(2026, 9, 20, 22, 20)),
                         "2026-09-20 23:17")

    def test_每一步各跑各的时间(self):
        """⭐ 这就是用户要的那件事：**每个模块自己的时间点**。
        ⚠ 2026-09-23：后面步骤不能早于抓取 ⇒ 先把抓取同步到 08:00。"""
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "attain", [{"kind": "weekly", "weekdays": [1], "time": "08:30"}])
        monday = datetime.datetime(2026, 9, 21, 8, 31)
        # ⚠ 8:31 这一刻，8:17 那次自动更新还在 30 分钟窗口里 ⇒ 两条都要出现。
        #   抓取 08:00 的窗口 08:30 已过 ⇒ 不再补。attain 08:30 刚进窗口。
        #   排序按 **(slot, order)**：8:17 那次排在 8:30 那次前面（slot 小），
        #   所以自动更新在前 —— 这跟"同一跳里按 order 排"不是一回事。
        self.assertEqual([d["cmd"] for d in timer.due(self.root, now=monday)],
                         ["autoupdate", "attain"])
        monday_night = datetime.datetime(2026, 9, 21, 21, 1)
        self.assertEqual([d["cmd"] for d in timer.due(self.root, now=monday_night)],
                         ["pos", "pools", "plan"],
                         "抓取已挪到早上 ⇒ 晚上这趟只剩还在 21:00 的分析/计划；attain 不在")
        tuesday = datetime.datetime(2026, 9, 22, 8, 31)
        self.assertEqual([d["cmd"] for d in timer.due(self.root, now=tuesday)],
                         ["autoupdate"], "周二早上不该再跑达成（只有每小时的自动更新）")


class Test派发一跳(unittest.TestCase):
    """`tick()` —— 到点就派一条命令出去。**测试里 spawn 是假的**（不真起进程）。"""

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)
        self.spawned = []
        self.spawn = lambda argv: self.spawned.append(argv)
        self.at = datetime.datetime(2026, 9, 20, 21, 5)

    def _tick(self, **kw):
        kw.setdefault("spawn", self.spawn)
        kw.setdefault("config", "config/store-X.yaml")
        kw.setdefault("now", self.at)
        kw.setdefault("busy", lambda: False)
        return timer.tick(self.root, **kw)

    def test_到点派一条命令_几步合成一条(self):
        res = self._tick()
        self.assertEqual(res["ran"],
                         ["dump", "erp-dump", "pos", "pools", "attain", "plan"])
        self.assertEqual(len(self.spawned), 1, "同一时刻到点的几步**合成一条命令**")
        argv = self.spawned[0]
        self.assertIn("--steps", argv)
        self.assertEqual(argv[argv.index("--steps") + 1],
                         "dump,erp-dump,pos,pools,attain,plan")
        self.assertEqual(argv[argv.index("--wake-slot") + 1], "2026-09-20 21:00")

    def test_正在跑就不开第二趟(self):
        res = self._tick(busy=lambda: True)
        self.assertEqual(res["ran"], [])
        self.assertEqual(self.spawned, [])
        self.assertIn("正在跑", res["why"])

    def test_同一跳里不会重复派发(self):
        """⚠ 状态文件记着"刚派出去"，下一跳看到还在跑就什么都不做。"""
        self._tick()
        self.assertTrue(timer.current(self.root), "派出去之后要留下状态")
        res = timer.tick(self.root, spawn=self.spawn, config="c.yaml", now=self.at,
                         busy=lambda: bool(timer.current(self.root)))
        self.assertEqual(res["ran"], [])
        self.assertEqual(len(self.spawned), 1)

    def test_跑完了会把状态抹掉_而且不会再派一遍(self):
        """⚠⚠ 这里藏着一个真会出事的场景：**子进程没记成**（刚装好、库里还没有
        `run_record` 表时 `runlog.record` 是**静默不写**的）。

        只靠记录去重的话，那种机器会被**每 30 秒重派一次**、连着派 30 分钟，
        云商账号直接被并发的登录挤爆。⇒ 派发时先记本地台账（`_mark_done`）。
        """
        self._tick()
        res = timer.tick(self.root, spawn=self.spawn, config="c.yaml", now=self.at,
                         busy=lambda: False)         # 上一趟已经结束
        self.assertEqual(res["ran"], [], "那个 slot 已经派过了（本地台账兜底）")
        self.assertEqual(len(self.spawned), 1, "一秒钟内不许派第二遍")
        self.assertFalse(timer.current(self.root), "不在跑了 ⇒ 状态要清掉")

    def test_子进程崩在前面也不会被无限重派(self):
        def boom(_argv):
            raise RuntimeError("起不来")
        self._tick(spawn=boom)
        again = self._tick()
        self.assertEqual(again["ran"], [], "派过一次就算试过，不重派")
        self.assertEqual(len(self.spawned), 0)

    def test_没有派发入口时只说算出来了(self):
        res = timer.tick(self.root, spawn=None, config="c.yaml", now=self.at,
                         busy=lambda: False)
        self.assertEqual(res["ran"], [])
        self.assertIn("没跑", res["why"])
        self.assertTrue(res["command"], "至少要把打算跑的命令说出来")

    def test_派发失败要把状态抹掉(self):
        """⚠ 留着的话下一跳会一直以为"有任务在跑"，**从此再也不唤醒任何东西**。"""
        def boom(_argv):
            raise RuntimeError("起不来")
        res = self._tick(spawn=boom)
        self.assertEqual(res["ran"], [])
        self.assertIn("没派出去", res["why"])
        self.assertFalse(timer.current(self.root))

    def test_探针炸了不许把定时器带崩(self):
        def boom():
            raise RuntimeError("探针坏了")
        res = self._tick(busy=boom)
        self.assertEqual(res["ran"], [])
        self.assertIn("出错", res["why"])

    def test_不在窗口里什么都不做(self):
        res = self._tick(now=datetime.datetime(2026, 9, 20, 23, 59))
        self.assertEqual(res["ran"], [])
        self.assertEqual(self.spawned, [])


class Test心跳线程(unittest.TestCase):
    """服务里那个心跳 —— **绝不许把异常抛到启动路径上**（AGENTS.md 坑 2）。"""

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def test_起得来_而且抛了也不影响服务(self):
        import threading
        stop = threading.Event()
        said = []
        with mock.patch.object(timer, "tick", side_effect=RuntimeError("炸了")):
            th = timer.start_heartbeat(root=self.root, spawn=lambda a: None,
                                       interval=1, say=said.append, stop=stop)
            self.assertTrue(th.daemon, "守护线程 —— 别拦着进程退出")
            for _ in range(60):                       # 等它至少跑一圈
                if said:
                    break
                stop.wait(0.05)
        stop.set()
        self.assertTrue(said, "出错了要留一句话")
        self.assertIn("出错", said[0])
        self.assertNotIn("Traceback", said[0])

    def test_名字固定(self):
        import threading
        stop = threading.Event()
        with mock.patch.object(timer, "tick", return_value={"ran": [], "why": ""}):
            th = timer.start_heartbeat(root=self.root, spawn=lambda a: None,
                                       interval=1, stop=stop)
        stop.set()
        self.assertEqual(th.name, "timer-heartbeat")


class Test唤醒记录(unittest.TestCase):
    """`timer.now()` —— 自动更新的前置（`health.update_plan(timer_state=…)` 要它）。"""

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def test_没跑过时说得清楚(self):
        got = timer.now(self.root)
        self.assertFalse(got["running"])
        self.assertFalse(got["ran_today"])
        self.assertEqual(got["last"], {})

    def test_今天跑过就算数(self):
        runlog.record(timer.WAKE_KIND, True, note="跑完了",
                      detail={"slot": "2026-09-20 21:00", "steps": ["dump"]},
                      root=self.root)
        got = timer.now(self.root)
        self.assertTrue(got["ran_today"])
        self.assertEqual(got["last"]["steps"], ["dump"])
        self.assertTrue(got["next_at"], "下次什么时候也要给出来")

    def test_今天失败不算跑过(self):
        runlog.record(timer.WAKE_KIND, False, why="会话过期",
                      detail={"slot": "2026-09-20 21:00", "steps": ["dump"], "exit_code": 5},
                      root=self.root)
        got = timer.now(self.root)
        self.assertFalse(got["ran_today"])
        self.assertEqual(got["last"]["exit_code"], 5, "失败也要能看出退出码")

    def test_喂给健康模块的两个字段都在(self):
        """⚠ 自动更新就卡在这两个字段上 —— 少一个它就只能一直 `later`。"""
        got = timer.now(self.root)
        self.assertIn("running", got)
        self.assertIn("ran_today", got)


class Test每天这一步要记一笔(unittest.TestCase):
    """`daily --wake-slot` 收尾写的 `run_record` —— **定时器去重靠它**。

    ⚠ 写入者是**真跑这一步的进程**，不是定时器：服务中途被杀 / 机器断电时，
      记录里就不会出现一条"跑成功了"。
    ⚠ 它俩都得打桩：`find_db`（否则写进**开发机真库** —— 这个项目为它栽过）
      和真跑的那几步（否则会去抓玲珑）。
    """

    def setUp(self):
        self.tmp, self.root, self.db = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, steps="dump,pools", slot="2026-09-20 21:00", rc=0):
        """走**真路**：`src.cli daily --steps … --wake-slot …`（定时器派发的就是它）。

        ⚠ 记录写在 `cli.cmd_daily` 的收尾，**不在 `run_daily.main` 里** ——
          那边第 1 步失败 / 合作店早退都是直接 return，会漏掉最该记的那一趟。
        """
        with mock.patch("src.storage.runlog.find_db", lambda root=None: self.db), \
             patch_step_run("dump", return_value=rc), \
             mock.patch("src.features.compliance.comparison.execution.run",
                        return_value=rc):
            return cli.main(["-c", "config/store-X.yaml", "daily",
                             "--steps", steps, "--wake-slot", slot])

    def test_记下来的槽位和步骤(self):
        self.assertEqual(self._run(), 0)
        rows = runlog.recent(self.root, kind=timer.WAKE_KIND)
        self.assertEqual(len(rows), 1)
        d = rows[0]["detail"]
        self.assertEqual(d["slot"], "2026-09-20 21:00")
        self.assertEqual(d["steps"], ["dump", "pools"], "只记**真跑了**的那几步")
        self.assertEqual(d["trigger"], "timer")
        self.assertEqual(d["exit_code"], 0)
        self.assertTrue(rows[0]["ok"])

    def test_第1步失败也要记一笔(self):
        """⚠⚠ 只记成功的话，这个 slot 会被每 30 秒重试一遍 —— 失败恰恰是最该记的。

        而"第 1 步失败"正是**最早 return 的那条路**（`run_daily` 直接 return rc），
        所以这条同时钉住了"记录不能写在 `main()` 里"。
        """
        self._run(rc=5)
        rows = runlog.recent(self.root, kind=timer.WAKE_KIND)
        self.assertEqual(len(rows), 1, "第 1 步失败也必须留下记录")
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["detail"]["exit_code"], 5)

    def test_没给槽位就不记(self):
        """⚠ 手动「跑一次」**不该**被记成"这一跳跑过了" ——
        记了的话那一跳就永远不会自己跑了（看着像"定时器坏了"）。"""
        with mock.patch("src.storage.runlog.find_db", lambda root=None: self.db), \
             patch_step_run("dump", return_value=0), \
             mock.patch("src.features.compliance.comparison.execution.run",
                        return_value=0):
            cli.main(["-c", "config/store-X.yaml", "daily", "--steps", "dump,pools"])
        self.assertEqual(runlog.recent(self.root, kind=timer.WAKE_KIND), [])

    def test_记的那一笔能让定时器不再重派(self):
        """端到端：跑过之后，`due()` 就该当它跑过了。"""
        self._run()
        at = datetime.datetime(2026, 9, 20, 21, 5)
        self.assertEqual([d["cmd"] for d in timer.due(self.root, now=at)],
                         ["erp-dump", "pos", "attain", "plan"],
                         "跑过的 dump/pools 不再跑，没跑的照旧（按 order 排）")


class Test合作店也要跑达成(unittest.TestCase):
    """⚠ 2026-09-20 改的：**合作店照样跑"销售达成"**。

    合作店没有串号标识 ⇒ 不走玲珑 ⇒ 抓数/POS/双平台那三步没得跑。
    原来这儿是**整趟直接 return**（理由是"达成还没做"）—— 而达成做好之后，
    那句话就成了**静默的假成功**：合作店的达成永远不会自动算，
    日志里却写着"日常流程结束 exit=0"。定时器上线后这一条更要命：
    它按时间点派发 daily，派了等于没派。
    """

    def _run(self, steps, needs_linglong=False, attain_ok=True):
        from src import config_io
        hit = {}
        plan_calls = []

        def fake_attain(**_kw):
            plan_calls.append(_kw)
            return {"ok": attain_ok, "why": "" if attain_ok else "读目标表失败"}

        # ⚠ **第 2 步（抓云商数据）也要挡** —— 不挡就真去登云商拉明细（网络！），
        #   实测把这几个用例从 0.2 秒拖到 69 秒。
        # ⚠ 达成那个桩要打在 **run_daily 里那个名字**上：它是
        #   `from ...attain import run as attain_run` 导进来的，
        #   改 `attain.run` 对 run_daily 没有影响。
        # ⚠ `with` 那一串**中间不许插注释** —— 注释会把 `\` 续行截断（今天踩第二次了）。
        # ⚠ `load_config` 也要打桩：真去读一个不存在的配置文件会抛 SystemExit，
        #   被 `main` 接住之后 `_cfg` 是 None ⇒ 画像回落成"要走玲珑"，
        #   合作店那个分支压根不会被走到（第一版测试就是这么假绿的）。
        with mock.patch.object(run_daily.cli, "load_config",
                               lambda p: {"erp_store_name": "甲店"}), \
             mock.patch.object(config_io, "load_raw",
                               lambda p: {"erp_store_name": "甲店"}), \
             mock.patch("src.cli.store_profile_of",
                        lambda cfg: {"needs_linglong": needs_linglong,
                                     "erp_name": "甲店", "show_all": False}), \
             patch_step_run("dump",
                            side_effect=lambda *a, **k: hit.setdefault("dump", 1) and 0), \
             patch_step_run("erp-dump",
                            side_effect=lambda *a, **k: hit.setdefault("erp-dump", 1) and 0), \
             mock.patch("src.features.sales.attain.attain.run", side_effect=fake_attain):
            rc = run_daily.main(["-c", "config/x.yaml", "--steps", steps])
        return rc, hit, plan_calls

    def test_合作店会跑达成(self):
        rc, hit, calls = self._run("dump,pos,pools,attain")
        self.assertEqual(rc, 0)
        self.assertNotIn("dump", hit, "合作店不该去抓玲珑数据")
        self.assertNotIn("erp-dump", hit, "这一步在 --steps 里没点名，就不该跑")
        self.assertEqual(len(calls), 1, "达成必须跑起来")
        self.assertEqual(calls[0].get("store_filter"), "甲店", "只看本店")

    def test_合作店只剩玲珑那几步时才早退(self):
        rc, hit, calls = self._run("dump,pos,pools")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], "没有能跑的步骤就别跑")
        self.assertNotIn("dump", hit, "合作店不抓玲珑")

    def test_体验店照旧全跑(self):
        rc, hit, calls = self._run("dump,attain", needs_linglong=True)
        self.assertEqual(rc, 0)
        self.assertIn("dump", hit, "体验店要走抓数那一步")

    def test_合作店照样抓云商(self):
        """⚠ 合作店不走玲珑（池A/B 没得抓），但**云商那两个池子照抓** ——
        销售达成读的就是云商销售明细。第 2 步（`erp-dump`）不该被按掉。"""
        rc, hit, calls = self._run("erp-dump,attain")
        self.assertEqual(rc, 0)
        self.assertIn("erp-dump", hit, "合作店也要抓云商数据")
        self.assertEqual(len(calls), 1)


class Test_steps_参数的语义(unittest.TestCase):
    """`--steps` = **就这几步**；`--skip-*` = 整批里跳过谁。**两者不一样。**"""

    def test_点名是精确的(self):
        """⭐ 这就是为什么要有 `--steps`，也是为什么"整批"最后被删掉。

        ⚠ 当年 `--skip-*` 那条路上有 `ALWAYS_STEPS`（dump/attain 永远补回来），
          于是"只在周一算达成"用 `--skip-*` 根本表达不出来 —— 每天还是整批跑。
          2026-09-21 晚连"整批"这个模式一起删了：**点谁跑谁**，没有补回来的东西。
        """
        self.assertFalse(hasattr(run_daily, "ALWAYS_STEPS"),
                         "`ALWAYS_STEPS` 又回来了？那说明「整批」也回来了")
        self.assertFalse(hasattr(run_daily, "with_always"))

    def test_合成命令用的是steps(self):
        argv = timer.wake_argv(".", "config/x.yaml", ["attain"], "2026-09-21 08:30")
        self.assertIn("--steps", argv)
        self.assertNotIn("--skip-dump", argv)

    def test_认不出来的步骤当场报错(self):
        with mock.patch.object(run_daily, "STEPS", run_daily.STEPS):
            rc = run_daily.main(["-c", "config/x.yaml", "--steps", "dump,没有这一步"])
        self.assertNotEqual(rc, 0, "不许回落成'整批跑'")

    def test_steps会翻开对应的跳过开关(self):
        seen = {}

        def fake_dump(*a, **k):
            seen["dump"] = True
            return 0

        def fake_pools(*a, **k):
            seen["pools"] = True
            return 0

        with patch_step_run("dump", side_effect=fake_dump), \
             mock.patch("src.features.compliance.comparison.execution.run",
                        side_effect=fake_pools):
            run_daily.main(["-c", "config/store-X.yaml", "--steps", "dump"])
        self.assertTrue(seen.get("dump"), "说了只跑 dump，它就得跑")
        self.assertFalse(seen.get("pools"), "没说的那步不许跑")


if __name__ == "__main__":
    unittest.main()


class Test每一步归哪个模块(unittest.TestCase):
    """`registry.step_owner()` —— 用户 2026-09-20：「**每个模块的设置页面加上
    定时执行相关设置**」⇒ 得知道每一步归谁（界面按它过滤）。

    ⚠ 归属**从注册表推**，界面和计划都不许自己写一份：
      "哪一步属于哪个模块"只有一个答案（它声明在哪个 `Feature.children` 里）。
    """

    def test_四个步骤各归各家(self):
        self.assertEqual(registry.step_owner("attain"), {"key": "sales", "label": "周度重点产品"})
        self.assertEqual(registry.step_owner("pos")["key"], "compliance")
        self.assertEqual(registry.step_owner("pools")["key"], "compliance")
        # 抓数不属于任何**功能**模块（它是能力层的），但界面上也得有个"哪个模块的"
        self.assertEqual(registry.step_owner("dump"), {"key": "fetch", "label": "数据抓取"})

    def test_任务表带上归属(self):
        tmp, root, _ = _root_with_db()
        self.addCleanup(tmp.cleanup)
        rows = {t["cmd"]: t for t in timer.tasks(root)}
        self.assertEqual(rows["attain"]["owner_key"], "sales")
        self.assertEqual(rows["pos"]["owner_label"], "五项合规")
        for t in rows.values():
            self.assertTrue(t["owner_key"] and t["owner_label"])


class Test下一次跑什么(unittest.TestCase):
    """`timer.next_run()` —— 定时器页顶上那行**大字**用它（用户 2026-09-20：

    > 「定时器设置页面**最上面大字**写着**下一次执行的是啥，什么时间**」）。

    ⚠ 同刻到点的几步**合成一条** —— 那正是派发时的行为（`tick()` 一次只处理一个
      slot，几步走同一条命令）。分开列会写成"21:00 抓数据，21:00 算 POS"，
      看着像要跑两趟。
    """

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def test_下一步是什么_什么时候(self):
        """⚠ 2026-09-20 起**最近的一趟通常是"自动更新"**（每小时 :17）——
        它就是要写在顶上大字里的那件事（"下一次会自动跑的是它"）。
        整批那趟（21:00，五步）用 `next_run` 在 21:17 之后问能看到。"""
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 15, 0))
        self.assertEqual(got["at"], "2026-09-20 15:17")
        self.assertEqual(got["at_text"], "今天 15:17")
        self.assertEqual(got["cmds"], ["autoupdate"])
        self.assertEqual(got["label"], "自动更新")

    def test_整批那一趟是五点(self):
        """⏰ 21:00 那一跳：五步合成一条（同刻到点），**自动更新不在里面**。"""
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 21, 17))
        # ⚠ 2026-09-21（M19）：21:17 之后的下一跳是 **21:30 收取门店上报**
        #   （门店那趟 21:00 跑完才发信），再往后才是 22:17 的自动更新。
        self.assertEqual(got["at"], "2026-09-20 21:30", "21:17 刚过，最近的是收上报")
        self.assertEqual(got["cmds"], ["report-inbox"])
        # 20:20 之后最近的是 21:00 那一批（20:17 那趟自动更新已经过去了）
        nxt = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 20, 20))
        self.assertEqual(nxt["at"], "2026-09-20 21:00")
        self.assertNotIn("autoupdate", nxt["cmds"], "每天那趟里没有内部步骤")
        self.assertIn("dump", nxt["cmds"])

    def test_改过的那一步按自己的时间算(self):
        """⚠ 改的是**哪一趟里有它**，不是"它变成下一次" ——
        周日下午三点看，最近的一趟仍然是**今晚 21:00**（只是里面没有达成）。
        ⚠ 抓取先同步到 08:00（后面的 attain 不能早于它）。"""
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "attain", [{"kind": "weekly", "weekdays": [1],
                                               "time": "08:30"}])
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 20, 20))
        self.assertEqual(got["at"], "2026-09-20 21:00")
        self.assertEqual(got["cmds"], ["pos", "pools", "plan"],
                         "达成改成周一早上了、抓取挪到早上 ⇒ 今晚这趟里没有它们")
        # 今晚那趟过去之后，下一次就是达成的周一早上
        # 21:00 那趟过去之后（22:20 之后最近的自动更新也过去了），下一次是周一的达成
        # 周一 08:20（08:17 那次自动更新刚过；08:00 抓取的窗口也过了）：下一次是 08:30 达成
        later = timer.next_run(self.root, now=datetime.datetime(2026, 9, 21, 8, 20))
        self.assertEqual(later["at"], "2026-09-21 08:30")
        self.assertEqual(later["label"], "销售达成")
        self.assertEqual(later["when_texts"], ["每周一 08:30"])

    def test_没有唤醒时刻时是空的(self):
        with mock.patch.object(timer, "tasks", lambda root=None: []):
            self.assertEqual(timer.next_run(self.root), {})

    def test_时间的说法(self):
        """今天 / 明天 / 后天 / X 天后 / 再远写日期 —— **不写倒计时**
        （那种数每看一次都不一样，页面又不会自己刷新，看着像卡住）。"""
        now = datetime.datetime(2026, 9, 20, 15, 0)
        cases = ((datetime.datetime(2026, 9, 20, 21, 0), "今天 21:00"),
                 (datetime.datetime(2026, 9, 21, 8, 30), "明天 08:30"),
                 (datetime.datetime(2026, 9, 22, 8, 30), "后天 08:30"),
                 (datetime.datetime(2026, 9, 24, 8, 30), "4 天后（09-24）08:30"),
                 (datetime.datetime(2026, 10, 1, 9, 0), "2026-10-01 09:00"))
        for when, want in cases:
            with self.subTest(when=when):
                self.assertEqual(timer.at_text(when, now), want)


class Test执行日志(unittest.TestCase):
    """`timer.wakes()` —— 用户 2026-09-20：

    > 「加一个**定时器执行日志**，记录**什么时间唤醒了什么，成功了没**」

    ⚠ 数据源是 `run_record(kind="wake")`，**由真跑那一步的进程写** ——
      所以"记的"和"发生的"是同一件事（断电时不会留下一条"跑成功了"）。
    """

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def test_空的时候给空列表(self):
        self.assertEqual(timer.wakes(self.root), [])

    def test_记了时间点_唤醒了什么_成功没(self):
        runlog.record(timer.WAKE_KIND, True, note="内置定时器：销售达成",
                      detail={"slot": "2026-09-20 21:00", "steps": ["dump", "attain"],
                              "trigger": "timer", "exit_code": 0, "seconds": 12.5},
                      root=self.root)
        runlog.record(timer.WAKE_KIND, False, why="退出码 5",
                      detail={"slot": "2026-09-21 21:00", "steps": ["attain"],
                              "trigger": "timer", "exit_code": 5},
                      root=self.root)
        got = timer.wakes(self.root)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["slot"], "2026-09-21 21:00", "新的在前")
        self.assertFalse(got[0]["ok"])
        self.assertEqual(got[0]["why"], "退出码 5")
        self.assertEqual(got[0]["labels"], ["销售达成"])
        self.assertTrue(got[1]["ok"])
        self.assertEqual(got[1]["label"], "抓取玲珑数据 + 销售达成")
        self.assertEqual(got[1]["seconds"], 12.5)

    def test_标签从注册表来(self):
        """⚠ 中文名**不在这儿再写一份** —— 加一步/改个名字，这里跟着变。"""
        runlog.record(timer.WAKE_KIND, True, detail={"slot": "x", "steps": ["pools"]},
                      root=self.root)
        self.assertEqual(timer.wakes(self.root)[0]["labels"],
                         [registry.step_by_cmd("pools").label])

    def test_认不出来的步骤退回_cmd(self):
        runlog.record(timer.WAKE_KIND, True, detail={"slot": "x", "steps": ["没有这步"]},
                      root=self.root)
        self.assertEqual(timer.wakes(self.root)[0]["labels"], ["没有这步"])

    def test_读不出来给空列表_不抛(self):
        with mock.patch("src.storage.runlog.recent", side_effect=RuntimeError("库坏了")):
            self.assertEqual(timer.wakes(self.root), [])


class Test开关滑块_注册到定时器(unittest.TestCase):
    """⭐ 用户 2026-09-20：「模块的什么时候自动跑这个功能，**左边加个开关滑块**吧，

    > **开了就注册到定时器，不开就不注册**」

    这条链路要一条不落地对上：`set_enabled` → `store` → `enabled_cmds` →
    `tasks()['enabled']` → `due()` / `next_run()` / `next_at()`。
    ⚠ 少盯一环的后果是"界面上关了、到点还在跑" —— 而那**看不出来**。
    """

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def _at(self, hh=21, mm=5):
        return datetime.datetime(2026, 9, 20, hh, mm)

    def test_默认全都注册(self):
        got = timer.enabled_cmds(self.root)
        for want in ("dump", "erp-dump", "pos", "pools", "attain", "autoupdate",
                     "report-inbox"):
            self.assertIn(want, got)
        # ⚠ 2026-09-21 晚：上报**有自己的时刻**（21:15）⇒ 就在定时器里
        self.assertIn("report", got)
        # ⚠ 2026-09-22：`film` / `benefit`（防护膜 / 无忧会员权益）**只手动** —— 不在唤醒名单
        self.assertNotIn("film", got)
        self.assertNotIn("benefit", got)
        for t in timer.tasks(self.root):
            with self.subTest(cmd=t["cmd"]):
                if t["cmd"] in ("film", "benefit"):
                    # 手动步：列在表里，但**没声明时刻**、开关也开不了
                    self.assertFalse(t["declared"], "只手动跑的步骤不该声明时刻")
                    continue
                self.assertTrue(t["declared"], "有时刻的步骤必须声明")
                self.assertTrue(t["enabled"])
                self.assertFalse(t["switched"], "没动过 ⇒ 不该标成'这台机器改过'")

    def test_没有唤醒时刻的步骤开不了开关(self):
        """⚠ 用户 2026-09-21：「**这个第一个开不了，是啥情况**」——
        那时的「上报数据」`whens=()`（跟着每天那趟跑），开关点是**静默无效果**，
        界面上看着就是"这功能坏了"。⇒ ① 明确拒 + 说清；② 当晚改成**有自己的时刻**
（21:15），所以这条改用**临时造一个没有时刻的步骤**来钉那段逻辑。
        """
        fake = registry.Step(cmd="manual-only", label="只手动跑", whens=())
        with mock.patch.object(registry, "all_steps",
                               lambda: list(registry.all_steps()) + [fake]), \
                mock.patch.object(registry, "step_by_cmd",
                                  lambda c: fake if c == "manual-only"
                                  else registry.__dict__["step_by_cmd"](c)):
            with self.assertRaises(ValueError) as ctx:
                timer.set_enabled(self.root, "manual-only", True)
        self.assertIn("跟着每天那趟", str(ctx.exception))

    def test_关掉之后到点不叫它(self):
        timer.set_enabled(self.root, "pools", False)
        self.assertNotIn("pools", timer.enabled_cmds(self.root))
        self.assertNotIn("pools", [d["cmd"] for d in timer.due(self.root, now=self._at())],
                         "关了还在跑 —— 这就是那个最难发现的错")
        self.assertNotIn("pools", timer.next_run(self.root, now=self._at())["cmds"])

    def test_关掉不影响别步(self):
        timer.set_enabled(self.root, "pools", False)
        got = [d["cmd"] for d in timer.due(self.root, now=self._at())]
        self.assertEqual(got, ["dump", "erp-dump", "pos", "attain", "plan"])

    def test_关掉能关的那几步(self):
        """⚠ **"全关掉"已经不可能了** —— 抓数两步和自动更新是**必做**的
        （用户 2026-09-20：「自动更新和数据抓取模块不允许关闭」）：
        能关的只有 POS / 双平台 / 达成（「上报数据」也有自己的时刻，同样关得掉）。"""
        for cmd in ("pos", "pools", "attain"):
            timer.set_enabled(self.root, cmd, False)
        # ⚠ 21:05 这一刻，最近的一趟是 **21:15 的「上报数据」**（2026-09-21 起
        #   它有自己的时刻 —— 用户：「上报数据还是有自己的吧」）。它**不是必做**的，
        #   所以下面那条"关不掉的步骤"名单里没有它。
        got = timer.next_run(self.root, now=self._at())
        self.assertEqual(got["cmds"], ["report"], "上报数据有自己的 21:15")
        # 过了 21:15 再看：下一件才是每小时那趟自动更新
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 21, 16))
        self.assertEqual(got["cmds"], ["autoupdate"], "每小时那趟也是必做的")
        # 今晚整批那趟：只剩抓数两步 + 月度生意计划（POS / 双平台 / 达成 被关掉了）
        batch = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 20, 20))
        self.assertEqual(batch["cmds"], ["dump", "erp-dump", "plan"],
                         "该跑的还是得跑（而且它们关不掉）")
        self.assertEqual([d["cmd"] for d in timer.due(self.root, now=self._at())],
                         ["dump", "erp-dump", "plan"])
        # 必做那几步：关它要报错，而不是静静地关掉
        for cmd in ("dump", "erp-dump", "autoupdate"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(ValueError):
                    timer.set_enabled(self.root, cmd, False)

    def test_每一步的下一趟只算它自己(self):
        """⚠⚠ 2026-09-21 用户：「这个下一趟是自动更新的，**不要显示自动更新的**」。

        自动更新每小时都跑（:17）⇒ 用全局的"下一趟"（`next_run` / `next_at`）
        去回答"某一步下一次什么时候跑"，白天任何时候都会答成 :17。
        ⇒ `next_of(root, cmd)` 只算**那一步自己的**时刻。
        """
        now = datetime.datetime(2026, 9, 21, 15, 0)      # 15:00，下一趟全局是 15:17
        got = timer.next_of(self.root, "dump", now=now)
        self.assertEqual(got["at"], "2026-09-21 21:00", "算成自动更新那趟了")
        self.assertEqual(got["at_text"], "今天 21:00")
        # 全局那个（对照）：确实是自动更新
        self.assertEqual(timer.next_run(self.root, now=now)["cmds"], ["autoupdate"])
        # 关掉的步骤：现在根本不会跑 ⇒ 不给时间
        timer.set_enabled(self.root, "pools", False)
        off = timer.next_of(self.root, "pools", now=now)
        self.assertTrue(off["off"])
        self.assertEqual(off["at"], "")

    def test_点默认是恢复默认_不是改成只手动跑(self):
        """⚠⚠ 2026-09-21 用户点「默认」按钮：「『attain』改成：**只手动跑**」。

        真相：点「默认」= `whens: []` = **跟随模块声明的默认值**（每天 21:00）——
        存下来的是"空"，而"空"的意思就是"用默认"（`tasks()` 里
        `entry.get("whens") or s.whens`）。可返回里那句 `describe(存下来的那份)`
        对着空列表说出了「只手动跑」，而"只手动跑"是"这一步**没有**时刻"的意思。
        ⇒ 报的必须是**生效的那一份**。
        """
        # ⚠ 先把抓取挪到早上，否则 attain 08:30 会撞上「不能早于数据抓取」
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "attain", [{"kind": "weekly", "weekdays": [1],
                                               "time": "08:30"}])
        res = timer.set_whens(self.root, "attain", [])
        self.assertTrue(res["restored"], "这次是恢复默认，得能区分出来")
        self.assertEqual(res["text"], "每天 21:00", "把'恢复默认'说成'只手动跑'了")
        # ⚠ `set_whens` 没有 `now` 参数（`next_of` 有）⇒ 这条只能按**真实时钟**算下一个 21:00
        self.assertEqual(res["next"]["at"], _next_at("21:00"))
        # 落到盘上/表里也确实是默认那份（不是"没时刻"）
        row = [t for t in timer.tasks(self.root) if t["cmd"] == "attain"][0]
        self.assertEqual(row["when_text"], "每天 21:00")
        self.assertFalse(row["overridden"], "恢复默认之后还标着'这台机器改过'")
        self.assertTrue(row["declared"])

    def test_改时间和开开关的提示语都用自己那一步的下一趟(self):
        # ⚠ 这里原来有一句 `now = datetime(2026, 9, 21, 15, 0)` —— **没传给谁**（死代码），
        #   而 `set_whens` 也没有 `now` 参数 ⇒ 它算的其实是真实时钟。
        #   所以期望值得按真实时钟推（写死 "2026-09-21 21:00" 的话，21:00 一过就红）。
        res = timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "21:00"}])
        self.assertEqual(res["label"], "抓取玲珑数据")
        self.assertEqual(res["next"]["at"], _next_at("21:00"))
        on = timer.set_enabled(self.root, "pools", True)
        self.assertNotIn(":17", on["message"], "开个开关报了自动更新的时间")

    def test_调顺序_表按它排_due_也按它跑(self):
        """⭐⭐ 2026-09-21 用户：「**相同时间执行的任务，按照定时器这个列表从上到下执行**，
        然后定时器列表给个调顺序的功能」。

        三件事必须一致：**表里的顺序 = `due()` 的顺序 = 派发出去的命令里的顺序**。
        少一环就是"界面调了、实际没按它跑"（静默不一致，最难发现）。
        """
        before = [t["cmd"] for t in timer.tasks(self.root)]
        self.assertEqual(before[:7],
                         ["dump", "erp-dump", "pos", "pools", "attain", "plan", "film"],
                         "默认顺序 = 模块声明的 `Step.order`（film 紧跟 plan）")
        # ⭐ 2026-09-23：界面怎么排都行，`set_order` 会把两个抓取**钉回最前**
        seven = ["attain", "pos", "dump", "erp-dump", "film", "pools", "plan"]
        res = timer.set_order(self.root, seven)
        self.assertIn("销售达成", res["text"])
        want = ["dump", "erp-dump", "attain", "pos", "film", "pools", "plan"]
        rows = [t["cmd"] for t in timer.tasks(self.root)]
        # ⚠ 只断言**这七步的相对顺序**：`set_order` 记的是 `(i+1)*10`，
        #   没点名的步骤仍用 `Step.order` —— 两边在同一数轴上，步数一多就可能
        #   跟 `autoupdate`(60) 撞号，绝对下标会抖（2026-09-22 踩过）。
        self.assertEqual([c for c in rows if c in seven], want)
        # 21:00 那一跳：`due()` 按新顺序给（⚠ **只有声明了时刻的**才 due 得出来 ——
        #   film 是 `whens=()`，所以不在 due 里，只参与表内排序）
        now = datetime.datetime(2026, 9, 21, 21, 0, 30)
        got = [d["cmd"] for d in timer.due(self.root, now=now)]
        self.assertEqual([c for c in got if c in seven],
                         [c for c in want if c != "film"],
                         "due() 没按新顺序 ⇒ 派发出去的命令还是老顺序")
        self.assertNotIn("film", got, "手动步不该被到点叫醒")
        self.assertNotIn("benefit", got, "手动步不该被到点叫醒")
        # 派发出去的那条命令里也是这个顺序（定时器拼的是 due() 给的 cmds）
        argv = timer.wake_argv(self.root, "c.yaml", got, "2026-09-21 21:00")
        self.assertIn("--steps", argv)
        self.assertTrue(argv[argv.index("--steps") + 1].startswith("dump,erp-dump,attain"))

    def test_只改顺序_不动时间和开关(self):
        """⚠ 调顺序**不许**碰到 `whens` / `enabled` —— 那是另一个入口的事。"""
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "pos", [{"kind": "daily", "time": "08:15"}])
        timer.set_enabled(self.root, "pools", False)
        timer.set_order(self.root, ["attain", "dump"])
        timer.set_order(self.root, ["pos", "pools", "dump", "erp-dump", "attain"])
        rows = {t["cmd"]: t for t in timer.tasks(self.root)}
        self.assertEqual(rows["pos"]["when_text"], "每天 08:15")
        self.assertFalse(rows["pools"]["enabled"])
        self.assertEqual(rows["dump"]["when_text"], "每天 08:00",
                         "调顺序不许动时间")
        self.assertEqual(rows["erp-dump"]["when_text"], "每天 08:00",
                         "两个抓取时间保持同步")

    def test_顺序名单里认不出来的步骤要抛(self):
        """⚠ 落回的后果是"界面调了顺序、实际按老顺序跑"（界面说一套、跑另一套）。"""
        with self.assertRaises(ValueError):
            timer.set_order(self.root, ["dump", "没有这一步"])
        with self.assertRaises(ValueError):
            timer.set_order(self.root, [])

    def test_没调过顺序的步骤照旧用声明值(self):
        """⚠ 只调了一部分（比如只写了 `attain, dump`）时，**没出现的步骤**
        仍然按 `Step.order` 排 —— 不然"加一步"就没地方放了。
        ⭐ 但名单里的抓取仍会被钉到最前（`attain, dump` → `dump, attain`）。"""
        timer.set_order(self.root, ["attain", "dump"])
        rows = [t["cmd"] for t in timer.tasks(self.root)]
        self.assertEqual(rows[0], "dump", "抓取固定排最前")
        self.assertIn("attain", rows)
        self.assertLess(rows.index("dump"), rows.index("attain"))
        self.assertIn("report", rows, "没进名单的步骤不许消失")
        self.assertIn("report-inbox", rows)

    def test_再打开还是原来那个时间(self):
        """⚠ 关掉**不抹掉时间点** —— 改过时间再关，打开时那个时间得还在。"""
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "pools", [{"kind": "daily", "time": "08:15"}])
        timer.set_enabled(self.root, "pools", False)
        timer.set_enabled(self.root, "pools", True)
        row = [t for t in timer.tasks(self.root) if t["cmd"] == "pools"][0]
        self.assertEqual(row["when_text"], "每天 08:15")
        self.assertTrue(row["enabled"])

    def test_两个抓取时间同步_改谁都是改一对(self):
        """⭐ 2026-09-23 用户：两个数据抓取**同步**。"""
        res = timer.set_whens(self.root, "erp-dump", [{"kind": "daily", "time": "20:30"}])
        self.assertEqual(res.get("synced"), ["erp-dump", "dump"])
        rows = {t["cmd"]: t for t in timer.tasks(self.root)}
        self.assertEqual(rows["dump"]["when_text"], "每天 20:30")
        self.assertEqual(rows["erp-dump"]["when_text"], "每天 20:30")
        # 改 dump 也带着 erp-dump
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "20:45"}])
        rows = {t["cmd"]: t for t in timer.tasks(self.root)}
        self.assertEqual(rows["dump"]["when_text"], "每天 20:45")
        self.assertEqual(rows["erp-dump"]["when_text"], "每天 20:45")

    def test_后面的时间不能早于数据抓取(self):
        """⭐ 2026-09-23 用户：「后面的时间不能早于这个时间」——后端必须拒。"""
        with self.assertRaises(ValueError) as cm:
            timer.set_whens(self.root, "pos", [{"kind": "daily", "time": "08:00"}])
        self.assertIn("不能早于数据抓取", str(cm.exception))
        # 抓取挪早之后，08:15 就合法了
        timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "08:00"}])
        timer.set_whens(self.root, "pos", [{"kind": "daily", "time": "08:15"}])
        rows = {t["cmd"]: t for t in timer.tasks(self.root)}
        self.assertEqual(rows["pos"]["when_text"], "每天 08:15")
        # 抓取改晚、会让后面变早 ⇒ 拒
        with self.assertRaises(ValueError) as cm:
            timer.set_whens(self.root, "dump", [{"kind": "daily", "time": "09:00"}])
        self.assertIn("会比后面这些还早", str(cm.exception))
        # 拒了就不落盘
        rows = {t["cmd"]: t for t in timer.tasks(self.root)}
        self.assertEqual(rows["dump"]["when_text"], "每天 08:00")
        self.assertEqual(rows["erp-dump"]["when_text"], "每天 08:00")

    def test_两个抓取钉在顺序最前(self):
        """⭐ 界面把抓取往后排，`set_order` 也要钉回最前。"""
        full = [t["cmd"] for t in timer.tasks(self.root)]
        rest = [c for c in full if c not in ("dump", "erp-dump")]
        res = timer.set_order(self.root, rest + ["dump", "erp-dump"])
        self.assertEqual(res["order"][:2], ["dump", "erp-dump"])
        rows = [t["cmd"] for t in timer.tasks(self.root)]
        self.assertEqual(rows[:2], ["dump", "erp-dump"])

    def test_任务表把开关状态给前端(self):
        timer.set_enabled(self.root, "attain", False)
        row = [t for t in timer.tasks(self.root) if t["cmd"] == "attain"][0]
        self.assertFalse(row["enabled"])
        self.assertTrue(row["switched"], "这台机器动过这个开关")
        self.assertEqual(row["when_text"], "每天 21:00", "时间点还在（不是被清掉）")

    def test_认不出来的步骤抛错(self):
        with self.assertRaises(ValueError):
            timer.set_enabled(self.root, "没有这一步", True)

    def test_消息说清注册没注册(self):
        on = timer.set_enabled(self.root, "pools", True)
        self.assertIn("已注册到定时器", on["message"])
        # ⚠ 下一趟**按当前最近的时刻算**：自动更新是每小时的，所以白天打开开关，
        #   "下一趟"往往就是它那个 :17，而不是今晚 21:00（别把时间写真了）
        self.assertIn("下一趟", on["message"])
        off = timer.set_enabled(self.root, "pools", False)
        self.assertIn("不再注册", off["message"])
        self.assertIn("时间点留着", off["message"], "得说清时间点没丢")


class Test带短横线的步骤名(unittest.TestCase):
    """⚠⚠ 2026-09-20 真踩到：`erp-dump` 的**短横线**。

    `--steps` 那条路原来是 `setattr(args, "skip_" + cmd, …)` ——
    对 `erp-dump` 会写成 `args.skip_erp-dump`（**一个取不到的属性名**），
    而读的地方 `getattr(args, "skip_erp_dump", False)` 永远拿 False
    ⇒ **`--steps` 里没点名的步骤照样会跑**，而且一声不吭
    （实测：`--steps dump` 把抓云商那步也带跑了，而且它还会去动数据）。

    ⚠ 这类"名字拼错了但两边都不报错"的 bug，只有**跨过那条边界**的用例能抓到 ——
      所以这里直接对着 `set_skip` / `skip_of` 这一对函数钉。
    """

    def test_短横线会换成下划线(self):
        args = argparse.Namespace()
        run_daily.set_skip(args, "erp-dump", True)
        self.assertTrue(run_daily.skip_of(args, "erp-dump"))
        self.assertTrue(getattr(args, "skip_erp_dump"), "得写成下划线，否则读不到")
        self.assertFalse(hasattr(args, "skip_erp-dump"), "属性名里不可能有短横线")

    def test_没设过就是没跳过(self):
        self.assertFalse(run_daily.skip_of(argparse.Namespace(), "erp-dump"))

    def test_steps里没点名的带短横线步骤不会跑(self):
        """端到端：`--steps dump` ⇒ 只跑 dump（连它后面那步都不许跑）。"""
        seen = {}
        # ⚠ 用**括号**把多个上下文管理器括起来，别靠行尾反斜杠续行 ——
        #   续行符旁边一旦写歪/加注释就断（今天为这个踩了两次）。
        def spy(name):
            def fn(*a, **k):
                seen[name] = 1
                return 0
            return fn

        # ⚠⚠ **不许用括号把多个 with 括起来**（`with (A, B):`）—— 那是 **3.10+**
        #   的语法；3.8 上它会被当成"一个元组上下文管理器" ⇒ `AttributeError: __enter__`。
        #   门店 Win7 就是 3.8.10，三个头必须都绿（这条就是 3.8 那个头抓出来的）。
        #   ⇒ 用 `ExitStack`（3.8 就有），或者老老实实嵌套 with。
        import contextlib
        with contextlib.ExitStack() as stack:
            for name, command in (("dump", "dump"), ("erp", "erp-dump")):
                stack.enter_context(patch_step_run(command, side_effect=spy(name)))
            stack.enter_context(mock.patch(
                "src.features.compliance.comparison.execution.run", side_effect=spy("pools")))
            run_daily.main(["-c", "config/store-X.yaml", "--steps", "dump"])
            run_daily.main(["-c", "config/store-X.yaml", "--steps", "dump"])
        self.assertIn("dump", seen)
        self.assertNotIn("erp", seen, "没点名的步骤跑了（短横线的坑）")
        self.assertNotIn("pools", seen)


class Test每小时这个频率(unittest.TestCase):
    """⚠ 2026-09-20 新加的 `hourly`（用户：「健康模块默认注册一个自动更新，
    **固定一个小时执行一次**」）。

    ⚠ 它的"几点"**不是时刻**，是**每小时的第几分钟**（`When.minute`，独立字段）——
      借 `time` 表达"每小时的第 17 分"要靠"小时那半截不许看"，读代码的人一定会看错。
    """

    def test_最近的整点(self):
        w = When(kind="hourly", minute=17)
        # 16:05 → 最近一次该醒是 15:17
        self.assertEqual(w.slot(datetime.datetime(2026, 9, 20, 16, 5)),
                         datetime.datetime(2026, 9, 20, 15, 17))
        # 16:20 → 就是 16:17
        self.assertEqual(w.slot(datetime.datetime(2026, 9, 20, 16, 20)),
                         datetime.datetime(2026, 9, 20, 16, 17))

    def test_下一次(self):
        w = When(kind="hourly", minute=17)
        self.assertEqual(w.next_after(datetime.datetime(2026, 9, 20, 16, 5)),
                         datetime.datetime(2026, 9, 20, 16, 17))
        self.assertEqual(w.next_after(datetime.datetime(2026, 9, 20, 16, 17)),
                         datetime.datetime(2026, 9, 20, 17, 17))

    def test_跨天(self):
        """零点过后的头 40 分钟里，"最近一次"是**昨天 23:40** —— 别算成今天。"""
        w = When(kind="hourly", minute=40)
        self.assertEqual(w.slot(datetime.datetime(2026, 9, 20, 0, 10)),
                         datetime.datetime(2026, 9, 19, 23, 40))

    def test_整点不带参数就是整点(self):
        self.assertEqual(When(kind="hourly").text(), "每小时")
        self.assertEqual(When(kind="hourly").next_after(
            datetime.datetime(2026, 9, 20, 16, 5)), datetime.datetime(2026, 9, 20, 17, 0))

    def test_说法(self):
        self.assertEqual(When(kind="hourly", minute=17).text(), "每小时的第 17 分钟")

    def test_存回来还是每小时(self):
        """⚠ `hourly` 的 `as_dict` **不带 `time`** —— 带上一个用不着的 21:00
        会让存盘看着像"每天 21 点"。"""
        d = When(kind="hourly", minute=17).as_dict()
        self.assertEqual(d, {"kind": "hourly", "minute": 17})
        self.assertEqual(When.from_dict(d).minute, 17)
        self.assertEqual(When.from_dict(d).text(), "每小时的第 17 分钟")

    def test_分钟越界要拦(self):
        with self.assertRaises(ValueError):
            When(kind="hourly", minute=60)

    def test_parse(self):
        self.assertEqual(When.parse("hourly 17").minute, 17)
        self.assertEqual(When.parse("hourly").minute, 0)


class Test自动更新那一步(unittest.TestCase):
    """⭐ 用户 2026-09-20：「**健康模块默认注册一个自动更新，固定一个小时执行一次**」。

    ⚠ 三件事各钉一条：
      ① 注册表里**默认就注册**（不用门店去配）；
      ② 频率是**每小时**；
      ③ 它**不在"每天那趟整批"里**（半夜对账跑到一半被换代码是最不该发生的事）。
    """

    def test_默认就注册着(self):
        step = registry.step_by_cmd("autoupdate")
        self.assertIsNotNone(step, "自动更新那一步没注册")
        self.assertEqual(step.label, "自动更新")
        self.assertTrue(step.whens, "得有唤醒时刻，不然它永远不会跑")
        self.assertEqual(step.whens[0].kind, "hourly")
        self.assertEqual(step.whens[0].minute, 17)

    def test_归健康模块(self):
        self.assertEqual(registry.step_owner("autoupdate"),
                         {"key": "health", "label": "系统健康"})

    def test_不在每天那趟整批里(self):
        """⚠ `default=False` ⇒ 它**不在"每天那趟"的名单里**（`MANUAL_STEPS`）。
        它按小时自己跑（定时器派 `daily --steps autoupdate`）。"""
        self.assertNotIn("autoupdate", run_daily.MANUAL_STEPS)
        self.assertIn("autoupdate", run_daily.STEPS, "但它仍然得是一步（有自己的开关）")

    def test_整批时会被按掉(self):
        seen = {}

        def spy(name):
            def fn(*a, **k):
                seen[name] = 1
                return 0
            return fn

        import contextlib
        import types
        with contextlib.ExitStack() as stack:
            for name, command in (("dump", "dump"), ("erp", "erp-dump")):
                stack.enter_context(patch_step_run(command, side_effect=spy(name)))
            for name, target in (("pools", "src.features.compliance.comparison.execution.run"),
                                 ("auto", "src.modules.health.auto_update")):
                stack.enter_context(mock.patch(target, side_effect=spy(name)))
            # ⚠ 达成的桩要回 **dict**（`run_daily` 读 `res.get("ok")`），不是整数
            stack.enter_context(mock.patch(
                "src.features.sales.attain.attain.run",
                side_effect=lambda **kw: (seen.setdefault("attain", 1),
                                          {"ok": True})[1]))
            # ⚠ POS 走**执行模块**，桩要按新契约回一个带 `.ok` 的对象
            #   （返回整数会让 `res.ok` 直接 AttributeError）
            stack.enter_context(mock.patch(
                "src.app.pos.run",
                side_effect=lambda **kw: (seen.setdefault("pos", 1),
                                          types.SimpleNamespace(ok=True, why=""))[1]))
            # ⚠ 本店必须"要走玲珑"，否则会走合作店那条早退分支
            stack.enter_context(mock.patch.object(cli, "load_config", lambda *a, **k: {
                "erp_store_name": "青岛CBD万达店", "store_code": "SCN328987",
                "marker": "C"}))
            stack.enter_context(mock.patch.object(cli, "_find_pos_db",
                                                  return_value=Path("/tmp/cbg-2026.db")))
            # ⚠ 2026-09-21（M18）：第 6 步（上报数据）**也要挡** —— 它 `root=None`
            #   ⇒ 会去读**项目根**那个真库、并往真 `out/report/` 写包与指纹
            #   （实测污染过一次：`out/report/{pending,state.db}`）。
            stack.enter_context(mock.patch(
                "src.app.report.run",
                side_effect=lambda **kw: (seen.setdefault("report", 1), {"ok": True})[1]))
            stack.enter_context(mock.patch(
                "src.app.report_inbox.run",
                side_effect=lambda **kw: {"ok": True, "skipped": "没配收信"}))
            run_daily.main([])
        self.assertNotIn("auto", seen, "整批把自动更新捎上了 —— 半夜换代码")

    def test_点名了才跑(self):
        """`daily --steps autoupdate`（定时器就是这么叫它的）必须真跑到。"""
        seen = {}
        with mock.patch("src.modules.health.auto_update",
                        side_effect=lambda *a, **k: seen.setdefault("auto", 1) and {
                            "ok": True, "action": "uptodate"}):
            run_daily.main(["--steps", "autoupdate"])
        self.assertIn("auto", seen)


class Test只算某一批的下一趟(unittest.TestCase):
    """`next_run(cmds=…)` —— 「**每天那趟**」= `default=True` 的那几步。

    ⚠ 为什么要有这个参数：左下角那个小标要显示"干活那趟"的时间（21:00），
      而**下一次任意步骤**往往是每小时的自动更新（:17）——
      拿它当小标的话永远显示"一小时内"，门店看不出东西。
    """

    def setUp(self):
        self.tmp, self.root, _ = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def test_不给_cmds_就是全部(self):
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 15, 0))
        self.assertEqual(got["cmds"], ["autoupdate"], "最近的是每小时那次")

    def test_只算每天那趟(self):
        batch = ["dump", "erp-dump", "pos", "pools", "attain"]
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 15, 0),
                             cmds=batch)
        self.assertEqual(got["at"], "2026-09-20 21:00")
        self.assertEqual(got["cmds"], batch)
        self.assertNotIn("autoupdate", got["cmds"])

    def test_那一批里关掉的不算(self):
        timer.set_enabled(self.root, "pools", False)
        batch = ["dump", "erp-dump", "pos", "pools", "attain"]
        got = timer.next_run(self.root, now=datetime.datetime(2026, 9, 20, 15, 0),
                             cmds=batch)
        self.assertNotIn("pools", got["cmds"], "关掉的那步不该出现在下一趟里")

    def test_空的一批给空(self):
        self.assertEqual(timer.next_run(self.root, cmds=[]), {})
