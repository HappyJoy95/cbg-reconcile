"""四池存储层（`src/pools.py`）的回归。

盯的都是**会静默出错的语义** —— 不报错、只是悄悄少给或多给东西：

* 快照"同一天重跑"必须**覆盖**：不能留半份，也不能叠加成两倍
* 快照轮转只删超期的，当天和最近 30 天**一根手指都不许碰**
* 销售明细的「串号」列是「主串 空格 副串」挤在一格 —— 必须**拆成两行**，
  否则按串号 JOIN 时会漏掉副串那台
* 没有串号的行**不能丢**（配件/礼品占 29%），但也不能拿空串去撞主键
* `pool_colname`：云商表头是全大写（`IMEI1`），走 `dump.colname` 会得到 `i_m_e_i1`
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import pools                                     # noqa: E402


def mem():
    conn = sqlite3.connect(":memory:")
    pools.ensure(conn)
    return conn


class TestPoolColname(unittest.TestCase):
    def test_all_caps_stays_readable(self):
        # ⚠ 这条是加 `pool_colname` 的全部理由：走 `dump.colname` 会得到 i_m_e_i1
        self.assertEqual(pools.pool_colname("IMEI1"), "imei1")
        self.assertEqual(pools.pool_colname("IMEI2"), "imei2")

    def test_camel_still_snake(self):
        self.assertEqual(pools.pool_colname("storeCode"), "store_code")
        self.assertEqual(pools.pool_colname("warehouseName"), "warehouse_name")

    def test_chinese_untouched(self):
        # 数字开头的 `69码` 正是裸写 SQL 会炸的那个
        self.assertEqual(pools.pool_colname("69码"), "69码")
        self.assertEqual(pools.pool_colname("分仓"), "分仓")


class TestSnapshot(unittest.TestCase):
    def test_write_and_count(self):
        conn = mem()
        n = pools.save_snapshot(conn, "erp-stock",
                                [{"Imei": "SNAAAAAA1", "StoreName": "仓甲"},
                                 {"Imei": "SNAAAAAA2", "StoreName": "仓乙"}],
                                date="2026-09-17", sn_field="imei")
        self.assertEqual(n, 2)
        self.assertEqual(pools.latest(conn, "erp-stock"), "2026-09-17")
        rows = list(conn.execute("SELECT sn, store_name FROM erp_stock ORDER BY sn"))
        self.assertEqual(rows, [("SNAAAAAA1", "仓甲"), ("SNAAAAAA2", "仓乙")])

    def test_same_day_rerun_overwrites_not_appends(self):
        """同一天重跑 = 覆盖。叠加的话"这天有多少台"会翻倍，而且看不出来。"""
        conn = mem()
        pools.save_snapshot(conn, "erp-stock", [{"Imei": "SNAAAAAA1"}, {"Imei": "SNAAAAAA2"}],
                            date="2026-09-17", sn_field="imei")
        pools.save_snapshot(conn, "erp-stock", [{"Imei": "SNAAAAAA1"}],
                            date="2026-09-17", sn_field="imei")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM erp_stock").fetchone()[0], 1)

    def test_blank_sn_kept_but_not_primary_key_collision(self):
        """空串号的行**要留住**（否则行数对不上接口自报），但两条不能互相盖掉。"""
        conn = mem()
        n = pools.save_snapshot(conn, "erp-stock",
                                [{"Imei": "", "ProName": "配件甲"},
                                 {"Imei": "", "ProName": "配件乙"}],
                                date="2026-09-17", sn_field="imei")
        self.assertEqual(n, 2, "空串号的两行必须都在")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM erp_stock").fetchone()[0], 2)

    def test_new_field_creates_column(self):
        """接口加字段 → 下次抓取自己补列（用户定的"列存全，避免别的调用"）。"""
        conn = mem()
        pools.save_snapshot(conn, "erp-stock", [{"Imei": "SNAAAAAA1", "BrandNew": "x"}],
                            date="2026-09-17", sn_field="imei")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(erp_stock)")}
        self.assertIn("brand_new", cols)

    def test_sales_pool_rejects_snapshot(self):
        conn = mem()
        with self.assertRaises(pools.PoolError):
            pools.save_snapshot(conn, "erp-sales", [{"Imei": "SNAAAAAA1"}])


class TestPurge(unittest.TestCase):
    def test_keeps_window_and_today(self):
        conn = mem()
        today = datetime.date.today()
        old = (today - datetime.timedelta(days=pools.SNAP_KEEP_DAYS + 5)).isoformat()
        edge = (today - datetime.timedelta(days=pools.SNAP_KEEP_DAYS - 1)).isoformat()
        pools.save_snapshot(conn, "lg-stock", [{"sn": "SNAAAAAA1"}], date=old)
        pools.save_snapshot(conn, "lg-stock", [{"sn": "SNAAAAAA2"}], date=edge)
        pools.save_snapshot(conn, "lg-stock", [{"sn": "A3"}], date=today.isoformat())

        purged = pools.purge_snapshots(conn)
        self.assertEqual(purged["lg-stock"], 1, "只该删掉超期那一天")
        left = {r[0] for r in conn.execute("SELECT snapshot_date FROM lg_stock")}
        self.assertEqual(left, {edge, today.isoformat()})

    def test_does_not_touch_sales(self):
        """轮转只管快照表 —— 销售是累积账，删了就永久没了。"""
        conn = mem()
        pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "A1234567"}])
        pools.purge_snapshots(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM erp_sales").fetchone()[0], 1)


class TestSales(unittest.TestCase):
    def test_splits_double_sn(self):
        """「主串 空格 副串」→ 两行。不拆的话副串那台在对账时**凭空消失**。"""
        conn = mem()
        w, sns, nosn = pools.save_sales(conn, "erp-sales",
                                        [{"单号": "D1", "串号": "A1234567 B7654321"}])
        self.assertEqual((w, sns, nosn), (2, 2, 0))
        got = {r[0] for r in conn.execute("SELECT sn FROM erp_sales")}
        self.assertEqual(got, {"A1234567", "B7654321"})

    def test_counts_rows_without_sn(self):
        """没串号的行**计入 nosn 但不落库** —— 29% 的配件/礼品走不了串号级对账，
        这个数必须报出来，不能静默吞掉。"""
        conn = mem()
        w, sns, nosn = pools.save_sales(conn, "erp-sales",
                                        [{"单号": "D1", "串号": ""},
                                         {"单号": "D2", "串号": "A1234567"}])
        self.assertEqual((w, sns, nosn), (1, 1, 1))

    def test_short_sn_ignored(self):
        """串号太短的当脏值丢掉（玲珑的 sn 里混着 `***` 这种）。"""
        conn = mem()
        w, _, nosn = pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "***"}])
        self.assertEqual((w, nosn), (0, 1))

    def test_rerun_replaces_same_doc(self):
        """同一单重拉 = 覆盖（`INSERT OR REPLACE`），不是又加一行。"""
        conn = mem()
        pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "A1234567", "金额": 1}])
        pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "A1234567", "金额": 2}])
        rows = list(conn.execute("SELECT COUNT(*), MAX(金额) FROM erp_sales"))
        self.assertEqual(rows[0], (1, 2))

    def test_snapshot_pool_rejects_sales(self):
        conn = mem()
        with self.assertRaises(pools.PoolError):
            pools.save_sales(conn, "lg-stock", [{"单号": "D1", "串号": "A1234567"}])


class TestQuadrants(unittest.TestCase):
    """四象限 —— 四池的全部对账逻辑。

    构造：池A 有效单 A1、关闭单 A2（不该算）；池B {B1,B2}；
    池C {C1, B2, R1(退货单)}；池D {A1, B1}。
    期望：AD={A1}、BC={B2}、BD={B1}，A2 和 R1 都不许冒出来。
    """

    def _seed(self):
        conn = mem()
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY, status_name TEXT,"
            " return_status INTEGER, refund_status INTEGER, store_name TEXT,"
            " doc_create_time TEXT, sales_assistant_id TEXT);"
            "CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT, sn TEXT,"
            " item_name TEXT, included_tax_amount REAL);")
        conn.execute("INSERT INTO orders (document_no,status_name,return_status,refund_status) VALUES ('D1','已完成',0,0)")
        conn.execute("INSERT INTO orders (document_no,status_name,return_status,refund_status) VALUES ('D2','已关闭',1,1)")
        conn.execute("INSERT INTO order_lines (document_no,sn,item_name,included_tax_amount) VALUES ('D1','SNAAAAAA1','机型A1',100.0)")
        conn.execute("INSERT INTO order_lines (document_no,sn,item_name,included_tax_amount) VALUES ('D2','SNAAAAAA2','机型A2',200.0)")
        pools.save_snapshot(conn, "lg-stock", [{"sn": "SNBBBBBB1", "item_name": "机型B1", "warehouse_name": "可售仓", "stock_age": 10}, {"sn": "SNBBBBBB2", "item_name": "机型B2", "warehouse_name": "可售仓", "stock_age": 20}],
                            date="2026-09-17")
        pools.save_sales(conn, "erp-sales", [
            {"单号": "S1", "串号": "SNCCCCCC1"},
            {"单号": "S2", "串号": "SNBBBBBB2", "商品名称": "机型B2", "门店": "店乙", "支付时间": "2026-09-01", "串号标识": "GT,新", "金额": 2999.0, "业务员": "张三"},   # → BC
            {"单号": "S3", "串号": "SNRRRRRR1", "单据类型": "零售退"},      # 退货：不算卖了
        ])
        pools.save_snapshot(conn, "erp-stock", [{"Imei": "SNAAAAAA1", "ProName": "机型A1", "StoreName": "仓甲", "Ages": 5, "Status": "在库"}, {"Imei": "SNBBBBBB1", "ProName": "机型B1", "StoreName": "仓甲", "Ages": 6, "Status": "在库"}],
                            date="2026-09-17", sn_field="imei")
        return conn

    def test_quadrants(self):
        q = pools.quadrants(self._seed(), detail=True)
        self.assertEqual(q["AD"], {"SNAAAAAA1"}, "玲珑报了、云商还挂着 → 云商没报")
        self.assertEqual(q["BC"], {"SNBBBBBB2"}, "云商卖了、玲珑还挂着 → 玲珑没报")
        self.assertEqual(q["BD"], {"SNBBBBBB1"})
        self.assertEqual(q["AC"], set())

    def test_closed_order_excluded_from_A(self):
        """已关闭/退货的订单不算"报了量" —— 否则会伪装成 AD（实测误报过 1 台）。"""
        q = pools.quadrants(self._seed(), detail=True)
        self.assertNotIn("SNAAAAAA2", q["A"])

    def test_return_bill_excluded_from_C(self):
        """退货单不算"卖了" —— 退货后货回库，同时出现在 C 和 D 是正常的。"""
        q = pools.quadrants(self._seed(), detail=True)
        self.assertNotIn("SNRRRRRR1", q["C"])

    def test_counts_only_mode(self):
        q = pools.quadrants(self._seed())
        self.assertEqual(q["AD"], 1)
        self.assertEqual(q["BC"], 1)
        self.assertNotIn("all", q)

    def test_erp_stock_uses_all_three_sn_columns(self):
        """云商三列串号取并集 —— 只取 imei 会把"其实有"的判成没有，造出假漏报。"""
        conn = mem()
        pools.save_snapshot(conn, "erp-stock",
                            [{"Imei": "", "SubImei": "SNXXXXXX1", "SubImei1": "SNXXXXXX2"}],
                            date="2026-09-17", sn_field="imei")
        got = pools._latest_sn_set(conn, "erp_stock",
                                   ("sn", "imei", "sub_imei", "sub_imei1"))
        self.assertEqual(got, {"SNXXXXXX1", "SNXXXXXX2"})

    def test_lg_stock_uses_sn_only(self):
        """玲珑只认 sn —— 把 imei1/imei2 也塞进来等于把一台机器当成两台。"""
        conn = mem()
        pools.save_snapshot(conn, "lg-stock",
                            [{"sn": "S1", "imei1": "868435086367645"}], date="2026-09-17")
        self.assertEqual(pools._latest_sn_set(conn, "lg_stock", ("sn",)), {"S1"})


class TestDetails(unittest.TestCase):
    """明细 —— 最后推给门店、让他们照着处理的那份。"""

    def test_ad_returns_the_right_items(self):
        rows = pools.details(TestQuadrants()._seed(), "AD")
        self.assertEqual([r["sn"] for r in rows], ["SNAAAAAA1"])
        self.assertEqual(rows[0]["direction"], "AD")
        self.assertIn("问题", rows[0], "推送要让门店看懂，得有句话说明")

    def test_bc_returns_the_right_items(self):
        rows = pools.details(TestQuadrants()._seed(), "BC")
        self.assertEqual([r["sn"] for r in rows], ["SNBBBBBB2"])
        self.assertEqual(rows[0]["问题"], pools.QUADRANT_LABELS["BC"])

    def test_only_ad_bc_allowed(self):
        """只有 AD/BC 是「有事」的象限；AC/BD 是正常态，问它们没意义。"""
        for bad in ("AC", "BD", "A", "", "XX"):
            with self.assertRaises(pools.PoolError):
                pools.details(mem(), bad)

    def test_empty_when_nothing_wrong(self):
        conn = mem()
        self.assertEqual(pools.details(conn, "AD"), [])
        self.assertEqual(pools.details(conn, "BC"), [])


class TestStatus(unittest.TestCase):
    def test_lists_four_pools(self):
        conn = mem()
        rows = pools.status(conn)
        labels = [r[0] for r in rows]
        self.assertEqual(len(labels), 4, "四个池子都要出现，哪怕表是空的")
        self.assertTrue(any("池A" in x for x in labels))
        self.assertTrue(any("池D" in x for x in labels))

    def test_survives_missing_tables(self):
        """池A 的表不在时不能炸 —— 只报"还没建"。"""
        conn = sqlite3.connect(":memory:")
        pools.ensure(conn)
        rows = dict((r[0], r[1]) for r in pools.status(conn))
        self.assertIn("还没建", rows["池A 玲珑销售单"])


if __name__ == "__main__":
    unittest.main()
