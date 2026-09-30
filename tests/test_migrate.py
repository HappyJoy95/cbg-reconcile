"""结构迁移（`src/storage/migrate.py`）—— M15 / 阶段 3.6。

四条规矩，一条一条钉：

* **编号**：连续、只增不改，跑过的最大编号写进 `meta.schema`；
* **前置条件**：不满足 ⇒ `skipped`，**下次还会再来**（不是"跳过就算完"）；
* **幂等记录**：每跑完一条**立刻**更新编号（一条一提交）；
* **失败处理**：抛 `MigrationError`（带上是哪一条），库停在**上一条**编号上。

⚠ 背景：`meta.schema` 这条记录**写了但全项目没人读**（`dump.py` 老代码里那句
`{"key": "schema", "value": "1"}`）。更糟的是它每次抓取都会**把编号打回 1** ——
所以本轮把它删了，编号从此归这里管。
"""

import sqlite3
import unittest
from pathlib import Path

from src.storage import migrate
from src.storage.migrate import Migration


def _mem():
    return sqlite3.connect(":memory:")


class Test编号(unittest.TestCase):
    def test_空库从_0_开始(self):
        conn = _mem()
        self.assertEqual(migrate.current(conn), 0)
        self.assertEqual(migrate.run(conn)["from"], 0)

    def test_跑过的记在_meta_schema_里(self):
        conn = _mem()
        res = migrate.run(conn)
        self.assertGreater(res["to"], 0)
        self.assertEqual(migrate.current(conn), res["to"])
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0],
            str(res["to"]))

    def test_编号乱掉时当_0_而不是崩(self):
        conn = _mem()
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema', '不是数字')")
        self.assertEqual(migrate.current(conn), 0, "读不出来就当没迁移过，别抛")

    def test_没有_meta_表时当_0(self):
        self.assertEqual(migrate.current(_mem()), 0)


class Test幂等(unittest.TestCase):
    def test_连跑两次第二次什么都不做(self):
        conn = _mem()
        migrate.run(conn)
        again = migrate.run(conn)
        self.assertEqual(again["applied"], [], "第二次还跑了 —— 那不叫幂等")
        self.assertEqual(again["from"], again["to"])

    def test_真的建出了东西(self):
        conn = _mem()
        migrate.run(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("fetch_attempt", names)
        self.assertIn("meta", names)

    def test_老库只有schema键时按它补账(self):
        """**升级路径的命门**：门店库只有 `meta.schema=3`（老记账法），
        没有 `migrations` 键 —— 必须把 1..3 当已跑，只补新账；
        要是当成"没跑过"重跑一遍倒也无害（都幂等），但**记账会漂**。"""
        conn = _mem()
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema', '3')")
        conn.execute("CREATE TABLE erp_sales (sn TEXT, 制单时间 TEXT)")
        res = migrate.run(conn)
        self.assertEqual([x["n"] for x in res["applied"]], [4, 5, 6, 7, 8],
                         "1..3 当已跑（按 schema 补账），只补新账")
        self.assertEqual(migrate.applied(conn), {1, 2, 3, 4, 5, 6, 7, 8})
        self.assertEqual(migrate.current(conn), 8)

    def test_纯手动新建库一路到最新(self):
        """收银机可能**永远不跑抓取**：空库直接迁移，除了 003 全都要建出来
        （利润/流水/政策三张表当场可用）。"""
        conn = _mem()
        migrate.run(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("profit_result", "sale_entries", "price_policy"):
            self.assertIn(t, names, "纯手动店等不到抓取，%s 必须当场建" % t)
        self.assertEqual(migrate.run(conn)["applied"], [], "幂等：再跑一遍什么都不做")


class Test前置条件(unittest.TestCase):
    def test_条件不满足就_skipped_而且下次还会再来(self):
        """⚠ 这是"跳过"和"做完"的区别 —— 混了的话索引可能**永远建不出来**。"""
        conn = _mem()
        res = migrate.run(conn)
        skipped = [x for x in res["skipped"] if x["n"] == 3]
        self.assertEqual(len(skipped), 1, "erp_sales 还没建，3 号该被跳过")
        self.assertIn("erp_sales", skipped[0]["why"])
        # ⚠ 2026-09-29 改：老断言是 `current(conn) < 3` —— 005/006 起无条件建表，
        #   跑完 schema 会是 6，但 3 **一条都没跑**。判据从"编号大小"换成
        #   "在不在已跑集合里"（语义没变：跳过的不算做完）。
        self.assertNotIn(3, [x["n"] for x in res["applied"]],
                         "跳过的不能算进已跑集合")
        self.assertNotIn(3, migrate.applied(conn))

    def test_条件满足之后自己就跑了(self):
        """⚠ 这条现在**同时钉迁移记账的新语义**（2026-09-29 收银那轮）：

        第一遍 005/006 已经把 `meta.schema` 顶到 6 —— 老实现
        （`n <= schema 就跳过`）会让 3 号**永远补不上**，此测试当場红。
        新实现按编号逐条记账：前面跳过的，条件到了照样来。
        """
        conn = _mem()
        migrate.run(conn)
        self.assertGreaterEqual(migrate.current(conn), 5,
                                "005/006 无条件建表，编号至少到 5")
        conn.execute("CREATE TABLE erp_sales (sn TEXT, 制单时间 TEXT)")
        res = migrate.run(conn)
        self.assertEqual([x["n"] for x in res["applied"]], [3])
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("ix_erp_sales_made", idx)


class Test失败处理(unittest.TestCase):
    """⚠ 迁移失败**绝对不能**被咽下去 —— 库结构没更新，后面会以"跑着跑着报错"的形式出现。"""

    def _boom(self, n, name):
        def f(conn):
            raise sqlite3.OperationalError("磁盘满了")
        return Migration(n=n, name=name, apply=f)

    def test_失败要抛而且说是哪一条(self):
        conn = _mem()
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.run(conn, migrations=[self._boom(1, "假装的一条")])
        self.assertIn("001", str(cm.exception))
        self.assertIn("假装的一条", str(cm.exception))
        self.assertIn("磁盘满了", str(cm.exception))

    def test_失败停在上一条编号上(self):
        conn = _mem()
        ok = Migration(n=1, name="好的", apply=lambda c: c.execute("CREATE TABLE t (a TEXT)"))
        with self.assertRaises(migrate.MigrationError):
            migrate.run(conn, migrations=[ok, self._boom(2, "坏的")])
        self.assertEqual(migrate.current(conn), 1,
                         "第 1 条成功了、第 2 条失败 ⇒ 编号必须停在 1")

    def test_失败之后重跑会从失败那条接着来(self):
        conn = _mem()
        ok = Migration(n=1, name="好的", apply=lambda c: c.execute("CREATE TABLE t (a TEXT)"))
        calls = []

        def flaky(c):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("第一次不行")
            c.execute("CREATE TABLE u (b TEXT)")

        with self.assertRaises(migrate.MigrationError):
            migrate.run(conn, migrations=[ok, Migration(n=2, name="时好时坏", apply=flaky)])
        res = migrate.run(conn, migrations=[ok, Migration(n=2, name="时好时坏", apply=flaky)])
        self.assertEqual([x["n"] for x in res["applied"]], [2])
        self.assertEqual(migrate.current(conn), 2)


class Test状态与文案(unittest.TestCase):
    def test_status_只读(self):
        conn = _mem()
        st = migrate.status(conn)
        self.assertEqual(st["schema"], 0)
        self.assertTrue(st["pending"], "空库该有待跑的和等条件的")
        self.assertEqual(migrate.current(conn), 0, "status 不许改库")

    def test_一句话说得清(self):
        conn = _mem()
        st = migrate.status(conn)
        self.assertIn("结构版本", migrate.describe(st))
        # ⚠ 空库时"还欠 N 条"比"有 N 条等条件"更要紧 —— 文案按这个优先级写
        self.assertIn("还欠", migrate.describe(st))
        migrate.run(conn)                    # 跑完只剩"等 erp_sales"那条
        self.assertIn("等条件", migrate.describe(migrate.status(conn)))

    def test_跑完之后说已是最新(self):
        conn = _mem()
        conn.execute("CREATE TABLE erp_sales (sn TEXT, 制单时间 TEXT)")
        # 004（利润结果表）的前置是 `orders` —— 真实库里 dump.SCHEMA 必建它，
        # 这里没有的话它会一直"等条件"，文案就不是"已是最新"了。
        conn.execute("CREATE TABLE orders (document_no TEXT PRIMARY KEY)")
        migrate.run(conn)
        self.assertIn("已是最新", migrate.describe(migrate.status(conn)))

    def test_编号不许重复也不许倒着排(self):
        ns = [m.n for m in migrate.MIGRATIONS]
        self.assertEqual(ns, sorted(ns), "迁移表必须按编号排好")
        self.assertEqual(len(ns), len(set(ns)), "编号不许重")


class Test接在抓取流程上(unittest.TestCase):
    """`dump.main` / `cmd_pools` 建完表之后会各跑一次（幂等、便宜）。"""

    def test_抓取流程里的那个助手绝不抛(self):
        from src import dump
        conn = _mem()

        class Boom:
            @staticmethod
            def run(c):
                raise sqlite3.OperationalError("炸")
            @staticmethod
            def current(c):
                return 7

        res = dump._run_migrations(conn, Boom)      # 不该抛
        self.assertEqual(res["error"], "炸")
        self.assertEqual(res["to"], 7)

    def test_老代码里那句写死的_schema_1_没了(self):
        """⚠ 留着它每次抓取都会把编号打回 1 ⇒ 2、3 号迁移永远被判成"还没做"。"""
        from src import dump
        src = Path(dump.__file__).read_text(encoding="utf-8")
        self.assertNotIn('{"key": "schema", "value": "1"}', src,
                         "写死的 schema=1 又回来了 —— 编号归 storage/migrate.py 管")
