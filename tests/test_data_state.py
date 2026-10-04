"""数据能不能算 —— **五种状态各有断言**（M14 / 阶段 3.2）。

⚠ 为什么每个状态都要一条：以前只有"有没有产物"两种画面，于是
「ERP 挂了抓取一直失败」和「今天确实没卖」在门店眼里**一模一样**。
用户 2026-09-19 在 M2M5 §9.4 点过名：深蓝中心店 / 市南金茂湾店的 0.0%
是**真 0**，不是没数据。

判据是**纯函数**（`judge`），所以大部分状态直接单测；另配几条走真库的集成测试
（`data_state`），盯"表不在 / 库坏了也不许抛"。
"""

import datetime
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.app import data_state as ds


def _dt(text):
    return ds._ts(text)


class Test五态判据(unittest.TestCase):
    """`judge()`：纯函数，状态一条一条来。"""

    def test_没有数据也没有尝试记录是_missing(self):
        st = ds.judge(0, None, key="k", label="甲")
        self.assertEqual(st.state, ds.MISSING)
        self.assertIn("从来没有采过", st.why)

    def test_采集成功但没有行是_zero_不是_missing(self):
        """⚠ 这是整件事的要害：**真 0 和没数据不能混**。"""
        now = datetime.datetime.now(ds.CST)
        st = ds.judge(0, now, need=now, key="k", label="甲",
                      attempt={"ok": 1, "finished_at": ds._fmt(now)})
        self.assertEqual(st.state, ds.ZERO)
        self.assertNotEqual(st.state, ds.MISSING)
        self.assertIn("别当成没采到", st.why)

    def test_采集失败是_failed_而且带原因(self):
        st = ds.judge(0, None, key="k", label="甲",
                      attempt={"ok": 0, "finished_at": "2026-09-19 21:00:00",
                               "why": "ERP 登录超时"})
        self.assertEqual(st.state, ds.FAILED)
        self.assertIn("ERP 登录超时", st.why)
        self.assertIn("2026-09-19 21:00:00", st.why)

    def test_失败比过期更具体(self):
        """⚠ 顺序：failed → missing → stale → zero → ok。

        数据旧 + 上次抓失败 ⇒ 要说"抓失败了"，因为那才是病因
        （只说"数据过期"会让人以为是没跑定时任务）。
        """
        st = ds.judge(10, _dt("2026-09-10"), need="2026-09-19",
                      attempt={"ok": 0, "finished_at": "2026-09-19 21:00:00",
                               "why": "连接被拒"}, key="k", label="甲")
        self.assertEqual(st.state, ds.FAILED)

    def test_采集时点太早是_stale(self):
        st = ds.judge(10, _dt("2026-09-16 19:51:49"), need="2026-09-19", key="k", label="甲")
        self.assertEqual(st.state, ds.STALE)
        self.assertIn("数据只到 2026-09-16", st.why)
        self.assertIn("2026-09-19", st.why)

    def test_不传_need_就不判过期(self):
        st = ds.judge(10, _dt("2020-01-01"), key="k", label="甲")
        self.assertEqual(st.state, ds.OK, "没说要算到哪，就不该判人家过期")

    def test_数据够新又有行是_ok(self):
        st = ds.judge(447, _dt("2026-09-19 21:30:00"), need="2026-09-19", key="k", label="甲")
        self.assertEqual(st.state, ds.OK)
        self.assertIn("447 行", st.why)

    def test_五态互斥且穷尽(self):
        """任意构造都落到**恰好一态**（不是"可能同时像两个"）。"""
        cases = [
            (ds.MISSING, 0, None, None, None),
            (ds.ZERO, 0, "2026-09-19 12:00:00", "2026-09-19", {"ok": 1}),
            (ds.FAILED, 5, "2026-09-19 12:00:00", None, {"ok": 0, "why": "x"}),
            (ds.STALE, 5, "2026-09-10 12:00:00", "2026-09-19", None),
            (ds.OK, 5, "2026-09-19 12:00:00", "2026-09-19", None),
        ]
        for want, rows, as_of, need, att in cases:
            with self.subTest(want=want):
                st = ds.judge(rows, _dt(as_of) if as_of else None, need=need,
                              attempt=att, key="k", label="甲")
                self.assertEqual(st.state, want)
                self.assertIn(st.state, (ds.OK, ds.ZERO, ds.FAILED, ds.MISSING, ds.STALE))


class _DbCase(unittest.TestCase):
    """造一个临时库（只建判据要用的那几张表）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir()
        self.db = self.root / "out" / "cbg-2026.db"
        self.conn = sqlite3.connect(str(self.db))
        self.conn.executescript("""
            CREATE TABLE fetch_log (run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT, finished_at TEXT, days INTEGER,
                window_start TEXT, window_end TEXT, store_code TEXT,
                orders INTEGER, order_lines INTEGER, payments INTEGER,
                returns INTEGER, refunds INTEGER, errors INTEGER, note TEXT);
            CREATE TABLE lg_stock (snapshot_date TEXT NOT NULL, sn TEXT NOT NULL);
            CREATE TABLE erp_stock (snapshot_date TEXT NOT NULL, sn TEXT NOT NULL);
            CREATE TABLE erp_sales (sn TEXT, document_no TEXT, 制单时间 TEXT,
                                    PRIMARY KEY (sn, document_no));
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _state(self, key, **kw):
        st = ds.data_state(self.root, need=kw.pop("need", None))
        return [s for s in st["sources"] if s["key"] == key][0]


class Test在真库上判(_DbCase):
    def test_空库全是_missing(self):
        st = ds.data_state(self.root)
        self.assertFalse(st["ok"])
        self.assertTrue(all(s["state"] == ds.MISSING for s in st["sources"]),
                        [s["state"] for s in st["sources"]])

    def test_抓成功过就是_ok(self):
        self.conn.execute("INSERT INTO fetch_log (finished_at, orders) VALUES (?,?)",
                          ("2026-09-19 21:00:00", 447))
        self.conn.commit()
        self.assertEqual(self._state("dump", need="2026-09-19")["state"], ds.OK)

    def test_抓到了零张是_zero_不是_missing(self):
        """⚠ `fetch_log` 里"抓到 0 张"的那次**不算成功**（`check_freshness` 也这么判）——
        所以这里应当落到 missing，而不是 zero。这条钉的是"别把空跑当成功"。"""
        self.conn.execute("INSERT INTO fetch_log (finished_at, orders) VALUES (?,?)",
                          ("2026-09-19 21:00:00", 0))
        self.conn.commit()
        self.assertEqual(self._state("dump", need="2026-09-19")["state"], ds.MISSING)

    def test_快照只到三天前要判_stale(self):
        """⚠ 最容易漏的一条：`_latest_sn_set` 取的是"库里最新那天"，
        它**不等于今天**。不显式比日期就会拿三天前的快照当今天用。"""
        self.conn.executemany("INSERT INTO lg_stock (snapshot_date, sn) VALUES (?,?)",
                              [("2026-09-16", "A1"), ("2026-09-16", "A2")])
        self.conn.commit()
        s = self._state("lg-stock", need="2026-09-19")
        self.assertEqual(s["state"], ds.STALE)
        self.assertEqual(s["rows"], 2)
        self.assertIn("2026-09-16", s["why"])

    def test_快照是今天的就_ok(self):
        today = datetime.datetime.now(ds.CST).strftime("%Y-%m-%d")
        self.conn.execute("INSERT INTO erp_stock (snapshot_date, sn) VALUES (?,?)",
                          (today, "A1"))
        self.conn.commit()
        self.assertEqual(self._state("erp-stock", need=today)["state"], ds.OK)

    def test_云商销售按数据覆盖到哪天判(self):
        self.conn.execute("INSERT INTO erp_sales (sn, document_no, 制单时间) VALUES (?,?,?)",
                          ("A1", "D1", "2026-09-19 10:00:00"))
        self.conn.commit()
        s = self._state("erp-sales", need="2026-09-19")
        self.assertEqual(s["state"], ds.OK)
        self.assertEqual(s["rows"], 1)

    def test_判据是只读的(self):
        """⚠ 只读 —— 它要在界面上随时被调（概览页 30 秒刷一次）。"""
        before = self.db.read_bytes()
        ds.data_state(self.root, need="2026-09-19")
        self.assertEqual(self.db.read_bytes(), before, "判据把库改了")


class Test采集尝试记录(_DbCase):
    """D2 选的**乙方案**：新表 `fetch_attempt` 专门记"试过没有、成没成"。"""

    def test_失败也要记下来(self):
        ds.record_attempt(self.conn, "lg-stock", False, why="云商超时", rows=0)
        a = ds.last_attempt(self.conn, "lg-stock")
        self.assertEqual(a["ok"], 0)
        self.assertIn("云商超时", a["why"])

    def test_记了失败就能判出_failed(self):
        ds.record_attempt(self.conn, "lg-stock", False, why="云商超时")
        s = self._state("lg-stock")
        self.assertEqual(s["state"], ds.FAILED)
        self.assertIn("云商超时", s["why"])

    def test_失败之后又成功就回到_ok(self):
        ds.record_attempt(self.conn, "lg-stock", False, why="云商超时")
        self.conn.execute("INSERT INTO lg_stock (snapshot_date, sn) VALUES (?,?)",
                          ("2026-09-19", "A1"))
        ds.record_attempt(self.conn, "lg-stock", True, rows=1)
        self.conn.commit()
        s = self._state("lg-stock", need="2026-09-19")
        self.assertEqual(s["state"], ds.OK)
        self.assertEqual(s["collected_at"][:10], datetime.datetime.now(ds.CST).strftime("%Y-%m-%d"))

    def test_老库没有这张表也不炸(self):
        self.conn.execute("DROP TABLE IF EXISTS fetch_attempt")
        self.conn.commit()
        self.assertIsNone(ds.last_attempt(self.conn, "lg-stock"))
        self.assertEqual(self._state("lg-stock")["state"], ds.MISSING)

    def test_记不上不影响正事(self):
        """库是只读的 / 磁盘满了 —— `record_attempt` 不许抛。"""
        self.conn.close()
        ds.record_attempt(self.conn, "lg-stock", False, why="x")   # 不该抛
        self.conn = sqlite3.connect(str(self.db))


class Test绝不抛(unittest.TestCase):
    def test_库不存在时说清楚(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = ds.data_state(Path(tmp.name))
        self.assertFalse(st["ok"])
        self.assertIn("没有找到订单库", st["sources"][0]["why"])

    def test_库文件是坏的不许抛(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "out").mkdir()
        (root / "out" / "cbg-2026.db").write_text("这不是 sqlite", encoding="utf-8")
        st = ds.data_state(root)
        self.assertFalse(st["ok"])
        self.assertTrue(all(s["state"] == ds.MISSING for s in st["sources"]))

    def test_缺表时说清楚哪张表(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "out").mkdir()
        conn = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        st = ds.data_state(root)
        self.assertFalse(st["ok"])
        self.assertTrue(any("没有" in s["why"] or "从来" in s["why"] for s in st["sources"]))


class Test给人看的文案(unittest.TestCase):
    def test_每个状态都有自己的标记和名字(self):
        for st in (ds.OK, ds.ZERO, ds.FAILED, ds.MISSING, ds.STALE):
            self.assertIn(st, ds.LABELS)
        self.assertEqual(len(set(ds.LABELS.values())), 5, "五个状态的名字不能重样")

    def test_确实为零的文案里不许出现没有数据(self):
        """⚠ M2M5 §9.4 的红线：真 0 不许写成"没有数据"（反之也一样）。"""
        now = datetime.datetime.now(ds.CST)
        zero = ds.judge(0, now, need=now, attempt={"ok": 1}, key="k", label="甲")
        lines = ds.summary_lines({"sources": [zero.as_dict()]})
        self.assertIn("确实为零", lines[0])
        self.assertNotIn("没有数据", lines[0])

    def test_摘要每行一个来源(self):
        st = ds.data_state(need=None)
        self.assertEqual(len(ds.summary_lines(st)), len(ds.SOURCES))


class Test采集点真的会记(_DbCase):
    """⚠ 判据再好，**没人写记录**也是空的 —— 这三条盯的是"埋点真的在"。

    dump 的埋点在 `modules.fetch.execution.run_dump`；通用采集器也由该执行模块
    统一记录。CLI 只提供 `_record_fetch` 服务，不在命令壳里决定尝试何时开始。
    """

    def test_端到端_抓失败也会留一笔(self):
        """跑一次**真的** dump 执行入口（把华为那边换成假货），失败要留痕。"""
        from src import cli
        sess = self.root / "s.json"
        sess.write_text("{}", encoding="utf-8")
        with mock.patch.object(cli, "load_config", lambda *a, **k: {"store_code": "SCN1"}), \
                mock.patch.object(cli, "session_path", lambda cfg: sess), \
                mock.patch.object(cli, "require_session", lambda *a, **k: (None, None)), \
                mock.patch.object(cli, "_find_pos_db", return_value=self.db), \
                mock.patch("src.dump.main", lambda argv=None: 3):
            rc = cli.cmd_dump(mock.Mock(config="config/x.yaml", year=0, all=False,
                                        month="current", verbose=False, no_refresh=True))
        self.assertEqual(rc, 3)
        a = ds.last_attempt(self.conn, "dump")
        self.assertIsNotNone(a, "抓失败了却没留记录 —— failed 这一态就是空的")
        self.assertEqual(a["ok"], 0)
        self.assertIn("退出码 3", a["why"])

    def test_助手写进去的是失败记录(self):
        from src import cli
        with mock.patch.object(cli, "_find_pos_db", return_value=self.db):
            cli._record_fetch("dump", False, why="华为接口断了", started="2026-09-19 21:00:00")
        a = ds.last_attempt(self.conn, "dump")
        self.assertIsNotNone(a, "失败没记进库里 —— 那 failed 这一态永远是空的")
        self.assertEqual(a["ok"], 0)
        self.assertIn("华为接口断了", a["why"])

    def test_助手绝不抛(self):
        from src import cli
        with mock.patch.object(cli, "_find_pos_db", side_effect=RuntimeError("库没了")):
            cli._record_fetch("dump", True)          # 不该抛


class Test自检那一项(unittest.TestCase):
    """自检第 8 项「数据能不能算」（M14 / 阶段 3.3）。

    ⚠ 口径：**只有"采集失败"算故障**。刚装完（`missing`）和数据旧了（`stale`）
    都只是信息 —— 门店第一次跑 selftest 时库里本来就是空的，
    那时候报红等于把"还没开始用"说成"坏了"。
    """

    def setUp(self):
        # ⚠ 2026-09-19 起这一项里多了"周度目标那份腾讯文档读得到吗" ——
        #   真读的话这三条测试**每次都要联网**（实测各 ~2 秒，而且换台机器就红）。
        #   钉成一句话：它只是第 8 项多打的那一行，跟"数据能不能算"的判据无关。
        from src import cli
        p = mock.patch.object(cli, "_attain_doc_line", lambda: "周度目标：（测试里不打桩）")
        p.start()
        self.addCleanup(p.stop)

    def test_没抓过不算故障(self):
        from src import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lines, failed = cli.selftest_data_lines(Path(tmp.name))
        self.assertFalse(failed, "刚装完就跑自检，不该判成故障")
        self.assertTrue(lines)
        self.assertTrue(any("不算故障" in ln for ln in lines))

    def test_数据旧了也不算故障(self):
        from src import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "out").mkdir()
        conn = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
        conn.execute("CREATE TABLE fetch_log (run_id INTEGER PRIMARY KEY, finished_at TEXT, orders INTEGER)")
        conn.execute("INSERT INTO fetch_log (finished_at, orders) VALUES ('2020-01-01 00:00:00', 3)")
        conn.commit()
        conn.close()
        _lines, failed = cli.selftest_data_lines(root)
        self.assertFalse(failed, "数据旧了是信息，不是故障")

    def test_采集失败要算故障(self):
        from src import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "out").mkdir()
        conn = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
        ds.ensure_attempt_table(conn)
        conn.commit()
        ds.record_attempt(conn, "dump", False, why="华为接口断了")
        conn.close()
        lines, failed = cli.selftest_data_lines(root)
        self.assertTrue(failed, "采集失败必须在自检里报出来")
        self.assertTrue(any("采集失败" in ln for ln in lines))


class Test接口层(unittest.TestCase):
    """`/api/pos` 的 hint 要带**病因**（不然"没算过"三个字什么都说明不了）。"""

    def test_没算过时_hint_说得清为什么(self):
        import tempfile as _tf
        from src import web
        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "out").mkdir()
        app = web.App(root=root, config="config/x.yaml")
        d = app.pos()
        self.assertFalse(d["exists"])
        self.assertIn("data_state", d, "POS 页也要能看到数据状态")
        self.assertNotEqual(d["hint"], "", "hint 不能是空的")


class Test黄横幅能关掉(unittest.TestCase):
    """⭐ 用户 2026-09-21：「图片中上面这个东西**一直不让关**」。

    ⚠⚠ 关键在于"关掉"的语义：关的是**这一条**，不是把这个告警关掉 ——
      指纹一变（又失败了一次 / 换了状态 / 换了数据源）就重新露出来。
      一关就永远不出现的话，那就是把告警功能关了。
    """

    def _state(self, key="erp-sales", st="failed", when="2026-09-21 05:40:26"):
        return {"ok": False, "worst": st, "sources": [
            {"key": key, "label": "原始销售明细（云商·池C）", "state": st,
             "state_label": "采集失败", "why": "退出码 2", "collected_at": when}]}

    def test_全_ok_时没有指纹(self):
        self.assertEqual(ds.fingerprint({"ok": True, "sources": [
            {"key": "a", "state": "ok"}]}), "")

    def test_指纹带上状态和数据源和时间(self):
        fp = ds.fingerprint(self._state())
        self.assertIn("erp-sales", fp)
        self.assertIn("failed", fp)
        self.assertIn("05:40:26", fp)

    def test_又失败一次指纹就变(self):
        """⚠ 这是"关掉"不等于"永远不报"的**唯一保证**：再失败一次是新的时间戳。"""
        a = ds.fingerprint(self._state(when="2026-09-21 05:40:26"))
        b = ds.fingerprint(self._state(when="2026-09-21 06:40:26"))
        self.assertNotEqual(a, b, "又失败一次必须重新弹出来")

    def test_换了数据源或状态也变(self):
        base = ds.fingerprint(self._state())
        self.assertNotEqual(base, ds.fingerprint(self._state(key="lg-stock")))
        self.assertNotEqual(base, ds.fingerprint(self._state(st="stale")))

    def test_brief_里带上指纹(self):
        b = ds.brief(self._state())
        self.assertEqual(b["fingerprint"], ds.fingerprint(self._state()))
