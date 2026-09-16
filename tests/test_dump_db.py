"""`dump.py` 里"给对账用的那两个函数"的测试 —— 库新鲜度 + 从库取已报量 SN。

为什么单独拎出来测：**这两个函数是对账的取数入口**，而现在对账的华为侧
全部从它们这里来。它们错了，差集就会把当天所有销售算成「未报量」，
而且**看着很合理** —— 门店会照着去补报一批假的。所以：

* `check_freshness` 必须能**说清病因**（返回 `(bool, 为什么)`，不是光一个 False）；
* `reported_sns_from_db` 的返回值必须和 `cbg.CbgClient.reported_sns()` **逐字段同形**，
  否则 `reconcile` 那边换不进来。

测试用**内存 sqlite**，表结构直接取 `dump.SCHEMA` —— 不手抄。手抄的那份
迟早和真的那份对不上，届时时测的是抄错的结构、不是真结构。
"""

import datetime
import sqlite3
import unittest

from src import dump

CST = datetime.timezone(datetime.timedelta(hours=8))


def ts(s):
    """'2026-09-16 14:30:00' → epoch（按北京时间）"""
    return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=CST).timestamp())


def mkdb(service_goods=True):
    """开一个内存库（走 `dump.connect`，和线上同一个入口）。

    ⚠ `order_lines.service_goods` **不在 `SCHEMA` 里** —— 它是
    `ensure_columns` 惰性补出来的列。所以"新库"要**走一次真机制**把它建出来，
    不能手写 `ALTER TABLE`（手写的话测的就不是线上那条路了）。
    `service_goods=False` = 门店那份 2026-09-16 之前 dump 出来的老库。
    """
    # ⚠ 走 `open_db()`（命名行）而不是 `connect()`（元组）—— 这组测试测的是
    #   "读"那条路（`r["sn"]`）。`connect()` 的默认值是给 `dump.main` 的
    #   汇总打印用的，两种用法故意分开，见 `dump.connect` 的说明。
    conn = dump.open_db(":memory:")
    conn.executescript(dump.SCHEMA)
    if service_goods:
        dump.ensure_columns(conn, "order_lines", {"service_goods": 0})
    return conn


def add_fetch(conn, finished, orders, note=""):
    conn.execute("INSERT INTO fetch_log (started_at, finished_at, orders, order_lines,"
                 " payments, note) VALUES (?,?,?,?,?,?)",
                 (finished, finished, orders, 0, 0, note))
    conn.commit()


def add_order(conn, doc, order_no, when, amount=1000.0, guide="张三", remark=""):
    conn.execute("INSERT INTO orders (document_no, order_no, doc_create_time,"
                 " doc_create_ts, included_tax_amount, consumer_guide_name, remark)"
                 " VALUES (?,?,?,?,?,?,?)",
                 (doc, order_no, when, ts(when), amount, guide, remark))
    conn.commit()


def add_line(conn, doc, line_no, sn, item, service_goods=0):
    """有这一列就带上，没有就当老库（不写它）。"""
    has = "service_goods" in {r[1] for r in conn.execute("PRAGMA table_info(order_lines)")}
    if has:
        conn.execute("INSERT INTO order_lines (document_no, line_no, sn, item_name,"
                     " service_goods) VALUES (?,?,?,?,?)",
                     (doc, line_no, sn, item, service_goods))
    else:
        conn.execute("INSERT INTO order_lines (document_no, line_no, sn, item_name)"
                     " VALUES (?,?,?,?)", (doc, line_no, sn, item))
    conn.commit()


class TestCheckFreshness(unittest.TestCase):
    """库够不够新 —— 这是对账第一步那道闸门。"""

    def test_空库要报出来(self):
        ok, why = dump.check_freshness(mkdb(), ts("2026-09-16 00:00:00"))
        self.assertFalse(ok)
        self.assertIn("没有任何成功的抓取记录", why)

    def test_抓到了零单的那次不算数(self):
        """⚠ 这条是**判据的核心**：`finished_at` 新 ≠ 库里有数据。

        只按时间判的话，一次"跑成功了但一张没抓到"（会话半死、窗口传错）
        也会被判成新鲜 —— 于是对账拿一份空库去比，全员「未报量」。
        """
        conn = mkdb()
        add_fetch(conn, "2026-09-16 21:00:00", 0)          # 最新，但是 0 单
        ok, why = dump.check_freshness(conn, ts("2026-09-16 00:00:00"))
        self.assertFalse(ok)
        self.assertIn("没有任何成功的抓取记录", why)

    def test_有数据的旧记录_比零单的新记录更可信(self):
        conn = mkdb()
        add_fetch(conn, "2026-09-10 21:00:00", 447)        # 有数据，但是旧的
        add_fetch(conn, "2026-09-16 21:00:00", 0)          # 新的，空跑
        ok, why = dump.check_freshness(conn, ts("2026-09-15 00:00:00"))
        self.assertFalse(ok)
        self.assertIn("2026-09-10 21:00:00", why)          # 报的是**有数据那次**

    def test_库落后于窗口_不新鲜且说清两头时间(self):
        conn = mkdb()
        add_fetch(conn, "2026-09-14 21:00:00", 400)
        ok, why = dump.check_freshness(conn, ts("2026-09-16 23:59:59"))
        self.assertFalse(ok)
        self.assertIn("2026-09-14 21:00:00", why)          # 库里到哪
        self.assertIn("2026-09-16", why)                   # 要查到哪
        self.assertIn("先跑一次 dump", why)                 # 给出下一步

    def test_边界_正好相等算新鲜(self):
        conn = mkdb()
        add_fetch(conn, "2026-09-16 21:00:00", 447)
        ok, _ = dump.check_freshness(conn, ts("2026-09-16 21:00:00"))
        self.assertTrue(ok)

    def test_边界_差一秒就不新鲜(self):
        conn = mkdb()
        add_fetch(conn, "2026-09-16 21:00:00", 447)
        ok, _ = dump.check_freshness(conn, ts("2026-09-16 21:00:01"))
        self.assertFalse(ok)

    def test_新鲜时也把抓取时间和单数带回来(self):
        conn = mkdb()
        add_fetch(conn, "2026-09-16 21:00:00", 447)
        ok, why = dump.check_freshness(conn, ts("2026-09-16 00:00:00"))
        self.assertTrue(ok)
        self.assertIn("2026-09-16 21:00:00", why)
        self.assertIn("447", why)

    def test_时间格式坏了要报出来而不是崩(self):
        conn = mkdb()
        add_fetch(conn, "2026/09/16 21:00:00", 447)        # 斜杠，解析不了
        ok, why = dump.check_freshness(conn, ts("2026-09-16 00:00:00"))
        self.assertFalse(ok)
        self.assertIn("读不出来", why)

    def test_取的是最后一条有数据的记录(self):
        conn = mkdb()
        add_fetch(conn, "2026-09-14 21:00:00", 100)
        add_fetch(conn, "2026-09-15 21:00:00", 200)
        add_fetch(conn, "2026-09-16 21:00:00", 300)
        ok, why = dump.check_freshness(conn, ts("2026-09-16 00:00:00"))
        self.assertTrue(ok)
        self.assertIn("300", why)


class TestReportedSnsFromDb(unittest.TestCase):
    """从库取「已报量」SN —— 形状必须和接口版一致。"""

    SEP, OCT = ts("2026-09-01 00:00:00"), ts("2026-09-30 23:59:59")

    def test_返回的字段和接口版逐个对得上(self):
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00", 6999.0, "李四")
        add_line(conn, "D1", 1, "SN1", "HUAWEI Mate 80")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(list(got), ["SN1"])
        # ⚠ 键名一个都不能改 —— reconcile 那边按这些键取值
        self.assertEqual(set(got["SN1"]),
                         {"documentNo", "orderNo", "item", "amount", "time", "guide"})
        self.assertEqual(got["SN1"]["documentNo"], "D1")
        self.assertEqual(got["SN1"]["orderNo"], "O1")
        self.assertEqual(got["SN1"]["item"], "HUAWEI Mate 80")
        self.assertEqual(got["SN1"]["amount"], 6999.0)
        self.assertEqual(got["SN1"]["time"], "2026-09-16 10:00:00")
        self.assertEqual(got["SN1"]["guide"], "李四")

    def test_空SN不进集合(self):
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00")
        add_line(conn, "D1", 1, "", "HUAWEI Mate 80")
        add_line(conn, "D1", 2, "SN1", "HUAWEI Mate 80")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(list(got), ["SN1"])

    def test_窗口外的单不要(self):
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-08-31 23:59:00")
        add_line(conn, "D1", 1, "SN_OLD", "老单")
        add_order(conn, "D2", "O2", "2026-09-01 00:00:00")
        add_line(conn, "D2", 1, "SN_NEW", "新单")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(list(got), ["SN_NEW"])

    def test_窗口两端都是闭区间(self):
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-01 00:00:00")
        add_line(conn, "D1", 1, "SN_A", "A")
        add_order(conn, "D2", "O2", "2026-09-30 23:59:59")
        add_line(conn, "D2", 1, "SN_B", "B")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(sorted(got), ["SN_A", "SN_B"])

    def test_空库返回空字典(self):
        self.assertEqual(dump.reported_sns_from_db(mkdb(), self.SEP, self.OCT), {})

    # ---------------------------------------------------- 一个 SN 挂多张单
    def test_一个SN挂多张单时_取非服务产品那张(self):
        """⚠ 这是**从库读相对接口版的实质改进**，别当噪音去掉。

        实测 4 个 SN 上接口版取到的是 HUAWEI Care+ 服务单（499/899 元），
        而真正的销售是机器单（6999 元）—— 报量那一栏会显示错的产品名和金额。
        """
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00", 6999.0)   # 机器单，先开
        add_line(conn, "D1", 1, "SN1", "HUAWEI Mate 80", 0)
        add_order(conn, "D2", "O2", "2026-09-16 10:01:00", 499.0)    # 服务单，后开
        add_line(conn, "D2", 1, "SN1", "HUAWEI Care+ 服务", 1)
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(got["SN1"]["item"], "HUAWEI Mate 80")
        self.assertEqual(got["SN1"]["amount"], 6999.0)

    def test_服务单先开也要取机器单(self):
        """排序是 `service_goods ASC` 在前 —— 和时间无关，先开的服务单也得让位。"""
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00", 499.0)
        add_line(conn, "D1", 1, "SN1", "HUAWEI Care+ 服务", 1)       # 服务单**先**
        add_order(conn, "D2", "O2", "2026-09-16 10:05:00", 6999.0)
        add_line(conn, "D2", 1, "SN1", "HUAWEI Mate 80", 0)
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(got["SN1"]["item"], "HUAWEI Mate 80")

    def test_都是机器单时取最早那张(self):
        conn = mkdb()
        add_order(conn, "D2", "O2", "2026-09-16 12:00:00", 6999.0)   # 后开
        add_line(conn, "D2", 1, "SN1", "第二次卖")
        add_order(conn, "D1", "O1", "2026-09-16 09:00:00", 5999.0)   # 先开
        add_line(conn, "D1", 1, "SN1", "第一次卖")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(got["SN1"]["item"], "第一次卖")

    def test_后到的同SN不覆盖先到的(self):
        """⚠ 曾经写反过：docstring 说"最早的赢"，代码却让后来的覆盖掉。

        结果是**取决于 SQL 返回顺序**的不确定行为 —— 时对时错，最难查。
        """
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-16 09:00:00", 5999.0)
        add_line(conn, "D1", 1, "SN1", "第一次卖")
        add_order(conn, "D2", "O2", "2026-09-16 12:00:00", 6999.0)
        add_line(conn, "D2", 1, "SN1", "第二次卖")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(got["SN1"]["item"], "第一次卖")
        self.assertEqual(got["SN1"]["documentNo"], "D1")

    def test_同一张单里两行同SN也只留一条(self):
        conn = mkdb()
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00")
        add_line(conn, "D1", 1, "SN1", "机身")
        add_line(conn, "D1", 2, "SN1", "同 SN 重复行")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(list(got), ["SN1"])

    # ------------------------------------------------------ 老库的兼容路径
    def test_老库没有service_goods列_退回商品名匹配(self):
        """门店那份 2026-09-16 之前 dump 的库**没有这一列**。

        没有兜底的话，`reported_sns_from_db` 直接 OperationalError ——
        而对账是第一步之后立刻就要调它的。
        """
        conn = mkdb(service_goods=False)
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00", 6999.0)
        add_line(conn, "D1", 1, "SN1", "HUAWEI Mate 80")
        add_order(conn, "D2", "O2", "2026-09-16 10:01:00", 499.0)
        add_line(conn, "D2", 1, "SN1", "HUAWEI Care+ 服务")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(got["SN1"]["item"], "HUAWEI Mate 80")

    def test_新库走的是service_goods列而不是商品名(self):
        """⚠ 反过来也要钉住：商品名里**没有** "Care+" 但 `service_goods=1` 的行，

        新库必须把它当服务单。否则哪天华为改了服务单的命名，
        商品名那条兜底会静默失效。
        """
        conn = mkdb(service_goods=True)
        add_order(conn, "D1", "O1", "2026-09-16 09:00:00", 499.0)
        add_line(conn, "D1", 1, "SN1", "增值服务包（不含 Care+ 字样）", 1)
        add_order(conn, "D2", "O2", "2026-09-16 10:00:00", 6999.0)
        add_line(conn, "D2", 1, "SN1", "HUAWEI Mate 80", 0)
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)
        self.assertEqual(got["SN1"]["item"], "HUAWEI Mate 80")

    def test_没有service_goods列也不许崩(self):
        """老库这条路要能走通 —— 不是"报个清楚的错"，是**真的能算出结果**。"""
        conn = mkdb(service_goods=False)
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00")
        add_line(conn, "D1", 1, "SN1", "HUAWEI Mate 80")
        got = dump.reported_sns_from_db(conn, self.SEP, self.OCT)   # 不许抛
        self.assertEqual(list(got), ["SN1"])


class TestColumnCacheAcrossDatabases(unittest.TestCase):
    """⚠ 回归测试：**同一个进程里开第二个库**时，列缓存必须不能骗人。

    真踩过：`_COLS_CACHE` 只按表名做键，第一个库存了
    `order_lines.service_goods` 之后，第二个库（`SCHEMA` 里没有这一列）
    的 `ALTER` 被跳过 → 写入直接
    `OperationalError: table order_lines has no column named service_goods`。

    线上形状：Web 是长驻进程，跨年那天它要建 `cbg-2027.db` ——
    一年只错一次，最难查的那种。
    """

    ROW = {"document_no": "D1", "line_no": 1, "sn": "SN1",
           "item_name": "Mate 80", "service_goods": 0}

    def test_第二个库照样能把列建出来(self):
        a = mkdb(service_goods=False)
        dump.put(a, "order_lines", self.ROW)
        a.commit()

        b = mkdb(service_goods=False)                 # 同一个进程里的第二个库
        dump.put(b, "order_lines", self.ROW)          # ← 修好之前这里必炸
        b.commit()
        self.assertEqual(
            b.execute("SELECT service_goods FROM order_lines").fetchone()[0], 0)

    def test_open_db_也走同一条路(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.db"
            c1 = dump.connect(p)
            c1.executescript(dump.SCHEMA)
            dump.put(c1, "order_lines", self.ROW)
            c1.commit()
            c1.close()

            c2 = dump.connect(p)                      # 第二个连接，同一个文件
            try:
                dump.put(c2, "order_lines", dict(self.ROW, line_no=2))
                c2.commit()
                self.assertEqual(
                    c2.execute("SELECT COUNT(*) FROM order_lines").fetchone()[0], 2)
            finally:
                c2.close()


class TestDbStaleIsAnError(unittest.TestCase):
    """`DbStale` 必须是异常 —— 结构上保证没人能"将就用旧库"。"""

    def test_是Exception的子类(self):
        self.assertTrue(issubclass(dump.DbStale, Exception))

    def test_不是SystemExit(self):
        """⚠ 不能是 SystemExit 那种"顺手就退出了"的东西 ——

        调用方要能**捕获它、写清病因、再决定退哪个码**。
        """
        self.assertFalse(issubclass(dump.DbStale, SystemExit))


class TestMainReturnsCodeNotSystemExit(unittest.TestCase):
    """⚠ `dump.main` 会被**进程内**调用（`cli.cmd_dump` → `run_daily`）。

    `SystemExit` 是 `BaseException`，会**穿过**调用方的 `if rc != 0` 把整个进程
    带走 —— 退出码约定失效，`run_daily`"第 1 步失败就别跑 2、3" 那条判断
    根本轮不到执行。
    """

    def test_没会话文件时返回码而不是抛(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            rc = dump.main(["--session", str(Path(d) / "nope.json"), "--month", "current"])
        self.assertEqual(rc, 1)

    def test_跨年时返回码而不是抛(self):
        """构造"抓到的数据跨了两年" —— 用假 client 绕开网络。"""
        import src.dump as dumpmod
        from unittest import mock

        # ⚠ `docCreateTime` 是 **epoch 整数**（不是字符串）—— `ts_year` 直接 int() 它
        orders = [{"docCreateTime": ts("2025-12-31 23:00:00")},
                  {"docCreateTime": ts("2026-01-01 01:00:00")}]
        sess = mock.Mock()
        with mock.patch.object(dumpmod.CbgSession, "load", return_value=sess), \
             mock.patch.object(dumpmod, "CbgClient") as Cli, \
             mock.patch.object(dumpmod, "list_all_orders", return_value=orders):
            Cli.return_value.ping.return_value = (True, "ok")
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                sf = Path(d) / "s.json"
                sf.write_text("{}", encoding="utf-8")
                rc = dumpmod.main(["--session", str(sf), "--all"])
        self.assertEqual(rc, 1)                    # 修好之前这里是 SystemExit


if __name__ == "__main__":
    unittest.main()


class TestRowFactoryTrap(unittest.TestCase):
    """⚠⚠ **门店实测炸过一次**，这组测试就是那次事故的回归测试。

    `dump.main` 末尾那段汇总打印（按 POS/设备、按支付方式…）写的是
    `"%-9s %4d" % row` 这种按位置展开的写法。而 Python 的 `%`
    **只对元组展开** —— 换成 `sqlite3.Row` 之后只允许**一个**占位符，
    多一个就 `TypeError: not enough arguments for format string`。

    最坏的地方在于**数据其实已经写进库了**，崩的只是最后的打印：
    退出码 9 ⇒ 整条日常流程中止 ⇒ 报量排查和 POS 一个都没跑。
    **活儿干完了，工具却说自己失败了。**

    根因是给主连接顺手设了 `row_factory`（那是给"读"的人用的）。
    所以现在两条路分开：`connect()` 默认元组、`open_db()` 才是命名行。
    """

    def _db(self):
        """一个五脏俱全的小库 —— 汇总那段要查 4 个视图，缺一个就白测。"""
        conn = dump.connect(":memory:")
        conn.executescript(dump.SCHEMA)
        add_order(conn, "D1", "O1", "2026-09-16 10:00:00", 6999.0, "李四")
        add_line(conn, "D1", 1, "SN1", "HUAWEI Mate 80")
        conn.execute("INSERT INTO payments (document_no, payment_no, media_name,"
                     " media_no, media_member_no, payment_amount)"
                     " VALUES ('D1','P1','现金',1,'',6999.0)")
        conn.execute("INSERT INTO order_labels (document_no, source, label)"
                     " VALUES ('D1','remark','国补')")
        conn.commit()
        return conn

    def test_connect_默认给元组(self):
        """⚠ 默认值**必须**是元组 —— 上面那段事故就是它被改成 Row 引起的。"""
        conn = dump.connect(":memory:")
        self.assertIs(type(conn.execute("SELECT 1").fetchone()), tuple)

    def test_open_db_给命名行(self):
        """读的人要 `r["sn"]`，所以这条路必须是 Row。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.db"
            c = dump.connect(p)
            c.executescript(dump.SCHEMA)
            c.commit()
            c.close()
            conn = dump.open_db(p)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"], 0)
            finally:
                conn.close()

    def test_汇总打印在元组连接上跑得通(self):
        """`dump.main` 走的就是这条（`connect()` 默认）。"""
        import contextlib
        import io
        conn = self._db()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dump.print_summary(conn)          # ← 修好之前这里必炸
        out = buf.getvalue()
        self.assertIn("按 POS/设备", out)
        self.assertIn("按支付方式", out)
        self.assertIn("退货单 + 退的 SN", out)
        self.assertIn("src=", out)          # 那行真的打印出来了
        self.assertIn("现金", out)          # 支付方式那张也有

    def test_汇总打印在命名行连接上也跑得通(self):
        """⚠ 反过来也要钉住 —— 万一哪天有人给主连接换了 `row_factory`。

        每处都写了 `tuple(row)`，所以**行类型不该影响它**。
        """
        import contextlib
        import io
        conn = self._db()
        conn.row_factory = sqlite3.Row          # 故意换成 Row
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dump.print_summary(conn)          # 不许炸
        self.assertIn("按 POS/设备", buf.getvalue())

    def test_非元组不能直接百分号展开(self):
        """把这条**语言规则**本身钉下来，免得以后有人又踩。

        （不是说它"不该这样"，是记一笔：`%` 和 `Row` 天生不搭。）
        """
        conn = self._db()
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE orders SET pos_id='web-pos', device_no='PC-POS'")
        row = conn.execute("SELECT pos_id, device_no, orders FROM v_pos_usage").fetchone()
        self.assertEqual(len(row), 3)                 # 列数是够的
        with self.assertRaises(TypeError):
            "%-9s %-9s %4d" % row                     # 但非元组只允许一个占位符
        self.assertIn("web-pos", "%-9s %-9s %4d" % tuple(row))   # tuple() 就好了
