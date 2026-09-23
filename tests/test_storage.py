"""存储层（`src/storage/`）—— M15 / 阶段 3.4。

⚠ 这一组盯的是**验收标准本身**：

1. **调用方不再需要 `clear_col_cache()`** —— 缓存挂在连接上（`db._Conn._cbg_cols`），
   "串到上一个连接的列集合"这件事从结构上就不可能；
2. 老名字（`dump.connect` / `open_db` / `ensure_columns` / `colname` / `clear_col_cache`）
   **仍然可用** —— 它们是部署契约（20+ 处引用），重构不许把它们弄断；
3. `connect()` 的两种默认值（元组 vs 命名行）**分开摆在函数名上**。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src import dump
from src.storage import db as sdb
from src.storage import schema


class Test缓存不再需要清理(unittest.TestCase):
    """⚠ 这是**整个 storage 层存在的理由** —— 老实现必炸的那个场景。"""

    def test_两个内存库不会串列(self):
        """实测复现过：`_COLS_CACHE` 按 `id(conn)` 做键，而 `id()` 会被复用 ⇒
        "缓存说这列在、新库其实没有" ⇒ `ALTER` 被跳过 ⇒ 写入 `OperationalError`。

        ⚠ 这里**一次 `clear_col_cache()` 都不调** —— 那正是要消灭的动作。
        """
        a = sdb.connect(":memory:")
        a.execute("CREATE TABLE t (x TEXT)")
        b = sdb.connect(":memory:")
        b.execute("CREATE TABLE t (y TEXT)")
        schema.ensure_columns(a, "t", {"x": "1"})
        schema.ensure_columns(b, "t", {"x": "1"})      # b 没有 x ⇒ 必须真的 ALTER
        self.assertEqual(sorted(schema.table_cols(a, "t")), ["x"])
        self.assertEqual(sorted(schema.table_cols(b, "t")), ["x", "y"])
        b.execute("INSERT INTO t (x) VALUES ('ok')")   # 老实现崩在这一行
        self.assertEqual(b.execute("SELECT x FROM t").fetchone()[0], "ok")

    def test_缓存挂在连接自己身上(self):
        a = sdb.connect(":memory:")
        self.assertIsInstance(getattr(a, "_cbg_cols", None), dict,
                              "缓存没挂在连接上 —— 那又要回到'记得清'那条老路")

    def test_两条连接的缓存互不影响(self):
        a = sdb.connect(":memory:")
        b = sdb.connect(":memory:")
        a.execute("CREATE TABLE t (x TEXT)")
        b.execute("CREATE TABLE t (y TEXT)")
        schema.table_cols(a, "t")
        schema.table_cols(b, "t")
        self.assertEqual(schema.table_cols(a, "t"), {"x"})
        self.assertEqual(schema.table_cols(b, "t"), {"y"})

    def test_裸连接也能加列(self):
        """不是我们造的连接（裸 `sqlite3.connect`）**不缓存**，每次真读。

        慢一点，但**绝不会读到别人的列** —— 这比"漏清缓存"安全得多。
        """
        raw = sqlite3.connect(":memory:")
        raw.execute("CREATE TABLE t (z TEXT)")
        self.assertIsNone(schema.cache_of(raw))
        schema.ensure_columns(raw, "t", {"w": 1})
        self.assertEqual(schema.table_cols(raw, "t"), {"w", "z"})
        raw.execute("INSERT INTO t (w, z) VALUES (1, 'a')")

    def test_数字开头的列名要加引号(self):
        """云商导出的表头里有 `69码` —— 裸写就是 `unrecognized token`。"""
        conn = sdb.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT)")
        schema.ensure_columns(conn, "t", {"69码": "x", "分仓": "y"})
        self.assertIn("69码", schema.table_cols(conn, "t"))
        conn.execute('INSERT INTO t ("69码") VALUES (?)', ("v",))


class Test连接的两种默认值(unittest.TestCase):
    """⚠ 这是踩过事故的地方：`dump.main` 的汇总打印是 `"%-9s %4d" % row` 那种写法。"""

    def test_connect_给元组(self):
        conn = sdb.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT, b INTEGER)")
        conn.execute("INSERT INTO t VALUES ('x', 1)")
        row = conn.execute("SELECT a, b FROM t").fetchone()
        self.assertIsInstance(row, tuple)
        self.assertEqual("%-3s %d" % row, "x   1", "元组才能这么格式化")

    def test_open_db_给命名行(self):
        conn = sdb.open_db(":memory:")
        conn.execute("CREATE TABLE t (sn TEXT)")
        conn.execute("INSERT INTO t VALUES ('A1')")
        self.assertEqual(conn.execute("SELECT sn FROM t").fetchone()["sn"], "A1")

    def test_dump_的老名字还能用(self):
        """部署契约：这些名字不许消失（20+ 处引用，`pools.py` 直接 from 它 import）。"""
        for name in ("connect", "open_db", "ensure_columns", "colname", "clear_col_cache"):
            self.assertTrue(hasattr(dump, name), "dump.%s 不见了" % name)
        self.assertEqual(dump.colname("docCreateTime"), "doc_create_time")
        self.assertIsNone(dump.clear_col_cache(), "兼容壳应当能调、且什么都不做")


class Test只读连接(unittest.TestCase):
    def test_只读连接写不进去(self):
        """`data_state` 那条"判据不许改库"的底气就在这儿 —— 比"记得别写"可靠。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "x.db"
        conn = sdb.connect(str(p))
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.commit()
        conn.close()

        ro = sdb.read_only(p)
        self.assertEqual(ro.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES ('x')")
        ro.close()


class Test事务(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "x.db"
        conn = sdb.connect(str(self.p))
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.commit()
        conn.close()

    def _count(self):
        conn = sdb.connect(str(self.p))
        try:
            return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            conn.close()

    def test_正常退出就提交(self):
        with sdb.tx(self.p) as conn:
            conn.execute("INSERT INTO t VALUES ('a')")
        self.assertEqual(self._count(), 1)

    def test_异常回滚而且原样抛出(self):
        with self.assertRaises(ValueError):
            with sdb.tx(self.p) as conn:
                conn.execute("INSERT INTO t VALUES ('a')")
                raise ValueError("半路炸了")
        self.assertEqual(self._count(), 0, "异常没回滚 —— 半个批次留在库里了")

    def test_SystemExit_也回滚(self):
        """⚠ `SystemExit` 是 `BaseException`，`except Exception` 接不住 ——
        这个项目在"它穿过 except"上踩过两次（AGENTS.md 坑 11）。"""
        with self.assertRaises(SystemExit):
            with sdb.tx(self.p) as conn:
                conn.execute("INSERT INTO t VALUES ('a')")
                raise SystemExit("别把半个批次留下")
        self.assertEqual(self._count(), 0)

    def test_异常之后连接会关掉(self):
        with self.assertRaises(ValueError):
            with sdb.tx(self.p) as conn:
                raise ValueError("x")
        conn2 = sdb.connect(str(self.p))       # 能再开就说明上一个没占着
        conn2.close()
