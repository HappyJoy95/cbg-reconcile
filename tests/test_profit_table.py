# -*- coding: utf-8 -*-
"""利润核算结果表（生活馆，迁移 004）—— 开发目标见
`.dsh/docs/2026-09-29-生活馆利润核算-数据表-开发目标.md`。

钉三件事：

1. **004 建得出表**，而且对空库幂等（连跑两次第二次什么都不做）；
2. **核心列固定**（单据 / 行号 / 金额与利润口径），类型不许漂；
3. **政策那边的列走 `ensure_columns` 动态长** —— pmall 导出加字段不用回来改代码
   （项目既定口径「接口多给什么，库里就多一列」）。
"""

import sqlite3
import unittest

from src.storage import migrate
from src.storage import schema


def _mem(with_orders: bool = True):
    conn = sqlite3.connect(":memory:")
    if with_orders:
        # 004 的前置：结果表挂 `orders`（没有它这条迁移会 skipped，下次再来）
        conn.execute("CREATE TABLE orders (document_no TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE order_lines (document_no TEXT, line_no INTEGER)")
    return conn


def _run_to_004(conn):
    """跑到 004（1、2 必跑；3 号要 erp_sales 在才跑，不影响本表）。"""
    return migrate.run(conn)


class Test建表(unittest.TestCase):
    def test_004_建出利润结果表(self):
        conn = _mem()
        _run_to_004(conn)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("profit_result", names)

    def test_连跑两次幂等(self):
        conn = _mem()
        _run_to_004(conn)
        again = migrate.run(conn)
        self.assertEqual(again["applied"], [], "第二次还跑了 —— 那不叫幂等")

    def test_编号接在_3_后面(self):
        conn = _mem()
        _run_to_004(conn)
        self.assertGreaterEqual(migrate.current(conn), 4)
        ns = [m.n for m in migrate.MIGRATIONS]
        self.assertEqual(ns, sorted(ns), "迁移表必须按编号排好")
        self.assertEqual(len(ns), len(set(ns)), "编号不许重")

    def test_老库已有这张表也能过(self):
        """门店库先手动建过同名表 ⇒ 004 必须 IF NOT EXISTS 地跳过，不许报错。"""
        conn = _mem()
        conn.execute("CREATE TABLE profit_result (document_no TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema', '3')")
        res = migrate.run(conn)
        self.assertNotIn("error", res)


class Test核心列(unittest.TestCase):
    def setUp(self):
        self.conn = _mem()
        _run_to_004(self.conn)

    def cols(self):
        return {r[1]: (r[2] or "").upper()
                for r in self.conn.execute("PRAGMA table_info(profit_result)")}

    def test_键列在(self):
        c = self.cols()
        for k in ("document_no", "line_no"):
            self.assertIn(k, c, "缺了 %s 就挂不到订单行上" % k)

    def test_金额列都是_REAL(self):
        c = self.cols()
        for k in ("sale_amount", "cost_amount", "rebate_amount", "profit"):
            self.assertIn(k, c, "缺了利润口径列 %s" % k)
            self.assertEqual(c[k], "REAL", "%s 该是 REAL，实测 %r" % (k, c[k]))

    def test_主键覆盖单据加行号(self):
        """重算要能**覆盖**同一行 —— 主键不在这两列上就会越积越多。"""
        cols = self.cols()
        self.conn.execute(
            "INSERT INTO profit_result (document_no, line_no, profit)"
            " VALUES ('D1', 1, 1.0)")
        self.conn.execute(
            "INSERT OR REPLACE INTO profit_result (document_no, line_no, profit)"
            " VALUES ('D1', 1, 2.0)")
        rows = self.conn.execute(
            "SELECT profit FROM profit_result WHERE document_no='D1'").fetchall()
        self.assertEqual(len(rows), 1, "同一 (单据, 行号) 必须覆盖而不是追加")
        self.assertEqual(rows[0][0], 2.0)
        self.assertIn("document_no", cols)


class Test动态列(unittest.TestCase):
    def test_政策字段进来当场补列(self):
        """pmall 导出的「基准提货价」这类列**不写死在建表语句里**。"""
        conn = _mem()
        _run_to_004(conn)
        have = {r[1] for r in conn.execute("PRAGMA table_info(profit_result)")}
        self.assertNotIn("基准提货价", have, "写死政策列 = 下次接口改名就得动迁移")

        schema.ensure_columns(conn, "profit_result",
                              {"基准提货价": 100.0, "无条件单台返利金额": 10.0})
        conn.commit()
        have = {r[1] for r in conn.execute("PRAGMA table_info(profit_result)")}
        self.assertIn("基准提货价", have)
        self.assertIn("无条件单台返利金额", have)

    def test_补完能写进去(self):
        conn = _mem()
        _run_to_004(conn)
        row = {"document_no": "D9", "line_no": 3, "profit": 5.5, "基准提货价": 88.0}
        schema.ensure_columns(conn, "profit_result", row)
        conn.execute(
            "INSERT INTO profit_result (%s) VALUES (%s)"
            % (",".join('"%s"' % k for k in row), ",".join("?" * len(row))),
            list(row.values()))
        conn.commit()
        got = conn.execute(
            'SELECT profit, "基准提货价" FROM profit_result').fetchone()
        self.assertEqual(got, (5.5, 88.0))


if __name__ == "__main__":
    unittest.main()
