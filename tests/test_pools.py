"""双平台存储层（`src/pools.py`）的回归。

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
        # ⚠ 2026-09-22：无串号行**也落库**（`nosn:<单号>` 当 sn）——
        #   所以 written = 1 真串号 + 1 条无串号 = 2；nosn 仍计 1。
        self.assertEqual((w, sns, nosn), (2, 1, 1))

    def test_short_sn_ignored(self):
        """串号太短的当脏值丢掉（玲珑的 sn 里混着 `***` 这种）——
        走无串号那条路：**计入 nosn 并落库**（同 2026-09-22 口径）。"""
        conn = mem()
        w, _, nosn = pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "***"}])
        self.assertEqual((w, nosn), (1, 1))

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

    def test_three_serial_cols_kept(self):
        """导出带串号2/3 时必须整列落库 —— 待领要靠它们挑真 SN。

        销售报表 `SALES_COLUMNS` 已请求 `Imei2/Imei3`；`pool_row_from`
        不丢中文键，`ensure_columns` 动态补列。缺了的话 86 码那台
        只能干等库存反查，销售侧的副串号白抓了。
        """
        conn = mem()
        pools.save_sales(conn, "erp-sales", [{
            "单号": "D1",
            "串号": "864468081285466",
            "串号2": "",
            "串号3": "7ED9K26611031362",
            "商品名称": "nova 16 Pro",
        }])
        cols = {r[1] for r in conn.execute("PRAGMA table_info(erp_sales)")}
        self.assertIn("串号", cols)
        self.assertIn("串号2", cols)
        self.assertIn("串号3", cols)
        row = conn.execute(
            "SELECT sn, 串号, 串号2, 串号3 FROM erp_sales"
        ).fetchone()
        self.assertEqual(row[0], "864468081285466")
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "7ED9K26611031362")

    def test_ensure_补齐老库三串号列(self):
        """老库（加列之前建的）走 `ensure()` 必须补出 串号2/3 ——
        空值行也要有列，待领 SQL 才扫得到。"""
        conn = sqlite3.connect(":memory:")
        # 模拟老表：只有主串号、没有副列
        conn.execute(
            "CREATE TABLE erp_sales (sn TEXT NOT NULL,"
            " document_no TEXT NOT NULL, 串号 TEXT,"
            " PRIMARY KEY (sn, document_no))")
        from src.features.compliance.comparison import store as P
        P.clear_col_cache()
        P.ensure(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(erp_sales)")}
        self.assertIn("串号", cols)
        self.assertIn("串号2", cols)
        self.assertIn("串号3", cols)
        # 再 ensure 一次不炸（幂等）
        P.ensure(conn)
        conn.close()


class TestQuadrants(unittest.TestCase):
    """四象限 —— 双平台的全部对账逻辑。

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

    def test_sold_then_returned_offset_out_of_C(self):
        """⭐ 2026-09-23 报障：**原销售行要被退货行冲销**，不能只剔退货行。

        卖→退→货回库 ⇒ C 有原单、B/D 都有货 ⇒ 老口径造出假 BC。
        这里造一台「卖了又退」+ 池B 有货，必须不算卖。
        """
        conn = self._seed()
        conn.execute(
            'INSERT INTO erp_sales (sn, document_no, 单号, 单据类型, 商品名称, 支付时间, 串号标识)'
            " VALUES ('SNBBBBBB3','S4','S4','零售','机型B3','2026-09-01 10:00:00','新')")
        conn.execute(
            'INSERT INTO erp_sales (sn, document_no, 单号, 单据类型, 商品名称, 支付时间, 串号标识)'
            " VALUES ('SNBBBBBB3','S5','S5','零售退','机型B3','2026-09-02 10:00:00','新')")
        pools.save_snapshot(conn, "lg-stock",
                            [{"sn": "SNBBBBBB3", "item_name": "机型B3",
                              "warehouse_name": "可售仓", "stock_age": 5}],
                            date="2026-09-17")
        c, _ = pools._sold_sns(conn)
        self.assertNotIn("SNBBBBBB3", c, "卖了又退的不该算卖")
        q = pools.quadrants(conn, detail=True)
        self.assertNotIn("SNBBBBBB3", q["BC"], "否则就是退货造成的假 BC")

    def test_sold_returned_then_resold_stays_in_C(self):
        """⚠ 卖→退→**又卖掉** = 真卖了，要留着（实测 6HR0226528000127）。

        一刀切"有退货就剔"会把这种真差异误杀 —— 少给是本项目最忌讳的方向。
        """
        conn = self._seed()
        for doc, kind, t in (("S6", "零售", "2026-07-30 21:00:00"),
                             ("S7", "零售退", "2026-08-19 12:00:00"),
                             ("S8", "分销", "2026-08-31 19:00:00")):
            conn.execute(
                'INSERT INTO erp_sales (sn, document_no, 单号, 单据类型, 商品名称, 支付时间, 串号标识)'
                " VALUES ('SNBBBBBB4',?,?,?,?,?,'新')", (doc, doc, kind, "机型B4", t))
        c, _ = pools._sold_sns(conn)
        self.assertIn("SNBBBBBB4", c, "退货后又卖掉 = 真卖了")

    def test_sale_without_time_still_counts(self):
        """⚠⚠ 支付时间**可空** —— 没时间的销售不能被当成"没卖过"（真踩过）。

        `net_sold` 曾拿时间戳的 truthiness 判"有没有销售"，测试库不写支付时间
        ⇒ 整个池C 被清空 ⇒ BC 全 0。
        """
        from src.features.compliance.comparison.rules import net_sold, SALE, RETURN
        self.assertTrue(net_sold([(SALE, "")]), "卖了但没记时间 = 卖了")
        self.assertFalse(net_sold([(RETURN, "")]), "只有退货 = 没卖")
        self.assertFalse(net_sold([(SALE, ""), (RETURN, "2026-09-02")]),
                         "同刻分不出先后 → 保守判没卖")
        # 卖得比退晚 = 卖掉了
        self.assertTrue(net_sold([(RETURN, "2026-09-01"), (SALE, "2026-09-05")]))

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


class Test导出Excel真的能跑(unittest.TestCase):
    """⚠ 这条是**补的**：`export_xlsx` 一直没人真跑过，于是它里面
    `details()` **漏了 import** 也没人发现 —— 直到 2026-09-20 走
    `daily --steps pools` 才炸出 `NameError: name 'details' is not defined`。

    教训：**"导出的入口"这种只在真跑时才走到的地方，必须有一条真跑一遍的测试**
    （不用测排版好不好看，测"它能不能把文件写出来"）。
    """

    def test_空库也能导出两个_sheet(self):
        import tempfile
        from src.features.compliance.comparison import export as _export
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "双平台数据对比.xlsx"
            path, counts = _export.export_xlsx(mem(), out)
            self.assertTrue(Path(path).is_file(), "文件没写出来")
            self.assertGreater(Path(path).stat().st_size, 0)
        self.assertEqual(sorted(counts), ["AD", "BC"])

    def test_两个_sheet_的表头是明细列(self):
        import tempfile
        from src.features.compliance.comparison import export as _export
        from src.features.compliance.comparison.rules import DETAIL_COLS
        with tempfile.TemporaryDirectory() as d:
            path, _ = _export.export_xlsx(mem(), Path(d) / "x.xlsx")
            import openpyxl
            wb = openpyxl.load_workbook(str(path))
            self.assertEqual(len(wb.sheetnames), 2)
            head = [c.value for c in next(wb[wb.sheetnames[0]].iter_rows())]
        self.assertEqual(head, [label for _, label in DETAIL_COLS])


class Test强制刷新整表重写(unittest.TestCase):
    """**强制刷新**（2026-09-29 用户：「设置里面加个强制刷新按钮吧，
    按照新规则全部重写数据库」）—— `pools.replace_sales`。

    要钉的是三种"看着成功、数没了"的失败：
    * 老口径留下的行**必须真的被删掉**（`INSERT OR REPLACE` 删不掉它们 ——
      这正是"重写"和"再写一遍"的差别）；
    * 抓到 **0 行 ⇒ 拒绝清库**；
    * 写到一半抛异常 ⇒ **回滚**，旧数据一行不少。
    """

    def test重写会删掉旧口径留下的行(self):
        conn = mem()
        pools.save_sales(conn, "erp-sales",
                         [{"单号": "OLD1", "串号": "A1234567", "金额": 9}])
        # 旧规则写进去、新规则不会再产生的"幽灵行"（REPLACE 永远删不掉它）
        conn.execute("INSERT INTO erp_sales (sn, document_no, 金额) "
                     "VALUES ('nosn:OLDGHOST:1', 'OLDGHOST', 5)")
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM erp_sales").fetchone()[0], 2)

        w, _sns, _nosn = pools.replace_sales(
            conn, "erp-sales", [{"单号": "NEW1", "串号": "B7654321", "金额": 3}])
        got = {(r[0], r[1]) for r in conn.execute("SELECT sn, document_no FROM erp_sales")}
        self.assertEqual(w, 1)
        self.assertEqual(got, {("B7654321", "NEW1")},
                         "旧行必须一张不剩 —— 这正是 save_sales（REPLACE）做不到的")

    def test_抓到0行拒绝清库(self):
        conn = mem()
        pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "A1234567"}])
        with self.assertRaises(pools.PoolError) as cm:
            pools.replace_sales(conn, "erp-sales", [])
        self.assertIn("0 行", str(cm.exception))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM erp_sales").fetchone()[0], 1,
                         "空结果不许清库 —— 那是「看着成功、数没了」")

    def test_写到一半失败要回滚_旧数据还在(self):
        from unittest import mock
        from src.features.compliance.comparison import store as _st
        conn = mem()
        pools.save_sales(conn, "erp-sales", [{"单号": "D1", "串号": "A1234567", "金额": 1}])
        real_put = _st.put
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("模拟写到一半断了")
            return real_put(*a, **k)

        with mock.patch.object(_st, "put", boom):
            with self.assertRaises(RuntimeError):
                pools.replace_sales(conn, "erp-sales",
                                    [{"单号": "N1", "串号": "B1111111"},
                                     {"单号": "N2", "串号": "C2222222"}])
        got = [r[0] for r in conn.execute("SELECT sn FROM erp_sales")]
        self.assertEqual(got, ["A1234567"],
                         "删了没写完必须回滚 —— 旧数据一行都不能少")

    def test_快照池拒绝重写(self):
        conn = mem()
        with self.assertRaises(pools.PoolError):
            pools.replace_sales(conn, "lg-stock", [{"单号": "D1", "串号": "A1234567"}])


if __name__ == "__main__":
    unittest.main()
