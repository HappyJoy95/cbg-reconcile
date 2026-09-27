"""系统健康状态（`storage/runlog.py` + `app/health.py`）—— 用户 2026-09-19 的第六个能力。

盯三件事：

1. **上报成本接近零**（一行 `begin/done`），而且**绝不抛**（记不上日志不许影响正事）；
2. **启动自检分级**：硬门槛 / 软警告 / 待办 —— 而且**只有硬门槛才拦**；
3. ⭐ **"能自己修的"不许变成"拦住用户的理由"**：库结构那项要**先跑迁移再判**
   （第一版只报"还欠迁移"，升级后的门店第一次打开控制台就被拦在门外 —— 自己踩的）。
"""

import sqlite3
import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

# ⚠ 打桩要打在**函数真正的家**上（`summary`）—— 包 `__init__` 里那些名字是**副本**，
#   patch 它们不影响 `summary.snapshot()` 内部的查找（本轮踩到，3 条测试红了）。
from src.modules import health
from src.modules.health import checks as hsum
from src.storage import migrate, runlog


def _root_with_db():
    """造一个带最小结构的临时安装目录。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir()
    db = root / "out" / "cbg-2026.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    return tmp, root, db


class Test运行记录(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.db = _root_with_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_记一条再读回来(self):
        runlog.record("pos", True, note="POS 合规", rows=3, root=self.root)
        rows = runlog.recent(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "pos")
        self.assertEqual(rows[0]["ok"], 1)
        self.assertEqual(rows[0]["detail"], {"rows": 3})

    def test_begin_done_只记一次(self):
        res = runlog.begin("attain", note="周度达成", root=self.root)
        res.done(ok=True, rows=12)
        res.done(ok=False, why="重复调用不该再写一行")
        self.assertEqual(len(runlog.recent(self.root)), 1)

    def test_没有库就安静跳过(self):
        """刚装完还没抓过数 —— 别为了记一行日志去建库。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runlog.record("x", True, root=Path(tmp.name))     # 不该抛、也不该建库
        self.assertFalse((Path(tmp.name) / "out").exists())

    def test_记不上绝不抛(self):
        bad = self.root / "out" / "cbg-2026.db"
        bad.write_text("这不是 sqlite", encoding="utf-8")
        runlog.record("x", True, root=self.root)          # 不该抛
        self.assertEqual(runlog.recent(self.root), [])

    def test_连着失败几次(self):
        for ok, why in ((True, ""), (False, "抖"), (False, "又抖")):
            runlog.record("dump", ok, why=why, root=self.root)
        s = runlog.summary(self.root)["dump"]
        self.assertEqual(s["fail_streak"], 2, "最近两次是连着的失败")
        self.assertEqual(s["last_fail_why"], "又抖")

    def test_中间成功过就不算连着(self):
        runlog.record("dump", False, why="很久以前失败过", root=self.root)
        runlog.record("dump", True, root=self.root)
        runlog.record("dump", False, why="刚失败", root=self.root)
        self.assertEqual(runlog.summary(self.root)["dump"]["fail_streak"], 1)

    def test_剪枝按_kind_各留N条(self):
        for i in range(6):
            runlog.record("dump", True, root=self.root)
        runlog.record("pos", True, root=self.root)
        self.assertEqual(runlog.prune(self.root, keep=2), 4)
        self.assertEqual(len(runlog.recent(self.root, kind="dump")), 2)
        self.assertEqual(len(runlog.recent(self.root, kind="pos")), 1)


class Test启动自检分级(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.db = _root_with_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _boot(self, **kw):
        with mock.patch.object(hsum, "check_timer", lambda root=None: [
                hsum._item("timer", "missing", "没定时任务")]), \
                mock.patch.object(hsum, "check_notify", lambda root=None, config_path=None: [
                    hsum._item("notify", "missing", "没配邮件")]), \
                mock.patch.object(hsum, "check_runs", lambda root=None: [
                    hsum._item("features", "missing", "还没跑过")]), \
                mock.patch.object(hsum, "check_code", lambda root=None: [
                    hsum._item("code", "ok", "没事")]), \
                mock.patch.object(hsum, "check_data", lambda root=None, need=None: [
                    hsum._item("data", "ok", "数据正常")]):
            return health.boot(self.root, **kw)

    def test_软警告不拦启动(self):
        """⚠ 没定时任务 / 没配推送**都是常态** —— 拦住门店，他连报 bug 都点不了。"""
        st = self._boot()
        self.assertTrue(st["allow_start"])
        self.assertTrue(st["warnings"])
        self.assertEqual(st["blocking"], [])

    def test_硬门槛才拦(self):
        with mock.patch.object(hsum, "check_schema", lambda root=None, apply=True: [
                hsum._item("schema", "failed", "还欠 3 条迁移", level=hsum.BLOCKING)]):
            st = self._boot()
        self.assertFalse(st["allow_start"])
        self.assertEqual(st["blocking"][0]["group"], "schema")

    def test_某一项自己炸了不拖累别的(self):
        """⚠ 健康模块自己去调别人，别人炸了**不许**把健康模块带崩。

        （这条测试第一版写错了：`_boot` 里也 patch 了 `check_code`，后 patch 的赢，
          `boom` 根本没跑到 —— 被测试自己逮住。）
        """
        def boom(root=None):
            raise RuntimeError("这项坏了")

        def ok(*a, **k):
            return [hsum._item("x", "ok", "没事")]

        with mock.patch.object(hsum, "check_code", boom), \
                mock.patch.object(hsum, "check_schema", ok), \
                mock.patch.object(hsum, "check_registry", ok), \
                mock.patch.object(hsum, "check_data", ok), \
                mock.patch.object(hsum, "check_timer", ok), \
                mock.patch.object(hsum, "check_notify", ok), \
                mock.patch.object(hsum, "check_theme", ok), \
                mock.patch.object(hsum, "check_auth", ok), \
                mock.patch.object(hsum, "check_runs", ok):
            st = health.snapshot(self.root)
        self.assertEqual(len(st["items"]), 9, "一项炸了，别的九项还要在")
        self.assertTrue(any("自己出错" in i["why"] for i in st["items"]),
                        "炸掉的那项要退化成一条警告，而不是消失")
        self.assertFalse(st["blocking"], "退化成警告 ⇒ 不该拦启动")


class Test生活馆登录自检(unittest.TestCase):
    def test_不把云商账号列为待办(self):
        from src import edition
        old = os.environ.get("CBG_EDITION")
        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(edition.reload)  # 后执行：先恢复环境，再清缓存
        self.addCleanup(lambda: (os.environ.__setitem__("CBG_EDITION", old)
                                 if old is not None else os.environ.pop("CBG_EDITION", None)))
        from src.modules import auth
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(auth, "accounts", return_value={
                        "erp": {"why": "生活馆版没有云商"},
                        "erp_store": {"why": "生活馆版没有云商"},
                        "cbg": {"ready": True},
                    }):
            st = auth.state({}, d)
        self.assertNotIn("erp", [item["key"] for item in st["items"]])


class Test结构那项先修再判(unittest.TestCase):
    """⚠⚠ 第一版只**报**"还欠迁移" ⇒ 升级后的门店第一次打开控制台被拦在门外。"""

    def setUp(self):
        self.tmp, self.root, self.db = _root_with_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_还欠迁移会自己跑掉(self):
        self.assertGreater(len(migrate.status(sqlite3.connect(str(self.db)))["pending"]), 0)
        items = health.check_schema(self.root)
        self.assertEqual(items[0]["state"], "ok", items[0]["why"])
        self.assertIn("结构版本", items[0]["why"])
        # 迁移真的跑了（表建出来了）
        conn = sqlite3.connect(str(self.db))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("fetch_attempt", names)

    def test_迁移跑不成才算硬门槛(self):
        with mock.patch.object(health, "_禁止", create=True), \
                mock.patch("src.storage.migrate.run",
                           side_effect=sqlite3.OperationalError("库是只读的")):
            items = health.check_schema(self.root)
        self.assertEqual(items[0]["state"], "failed")
        self.assertEqual(items[0]["level"], hsum.BLOCKING)
        self.assertIn("只读", items[0]["why"])


class Test自动更新策略(unittest.TestCase):
    """用户：「健康状态**也负责自动更新**」—— 这里管的是**策略**（机制在 selfupdate）。"""

    def setUp(self):
        self.tmp, self.root, self.db = _root_with_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, **kw):
        with mock.patch.object(hsum, "snapshot",
                               lambda root=None, **k: {"blocking": [], "warnings": [], "items": []}), \
                mock.patch("src.selfupdate.pending", lambda root: {}):
            return health.update_plan(self.root, **kw)

    def test_今天那趟没跑就等一等(self):
        self.assertEqual(self._plan()["action"], "later")

    def test_跑完了就可以更新(self):
        p = self._plan(timer_state={"ran_today": True})
        self.assertEqual(p["action"], "yes")

    def test_正在跑就别动(self):
        p = self._plan(timer_state={"ran_today": True, "running": True})
        self.assertEqual(p["action"], "no")
        self.assertIn("正在跑", p["why"])

    def test_升级断在半路先修它(self):
        with mock.patch.object(hsum, "snapshot",
                               lambda root=None, **k: {"blocking": [], "warnings": [], "items": []}), \
                mock.patch("src.selfupdate.pending", lambda root: {"state": "failed"}):
            p = health.update_plan(self.root)
        self.assertEqual(p["action"], "no")
        self.assertIn("断在半路", p["why"])

    def test_硬门槛没过也不动(self):
        with mock.patch.object(hsum, "snapshot",
                               lambda root=None, **k: {"blocking": [{"why": "库结构不对"}],
                                                       "warnings": [], "items": []}), \
                mock.patch("src.selfupdate.pending", lambda root: {}):
            p = health.update_plan(self.root, timer_state={"ran_today": True})
        self.assertEqual(p["action"], "no")
        self.assertIn("硬门槛", p["why"])

    def test_每个结果都带_why(self):
        for kw in ({}, {"timer_state": {"ran_today": True}},
                   {"timer_state": {"running": True}}):
            self.assertTrue(self._plan(**kw)["why"], "自动更新做没做、为什么没做，必须写清")
