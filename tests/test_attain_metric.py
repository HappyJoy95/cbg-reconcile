"""销售达成的**口径**（`features/sales/attain/metric.py`，纯函数、零 IO）。

这一份测的全是"数字对不对"，一条 IO 都不碰 —— 所以能直接单测。
口径来源：`开发目标.md` 第三节 + skill 第 3/8/9 条 + 目标表最后一行那条奖惩规则。

⚠ 这里有几条是**踩过的坑钉死的**，改的时候先看注释里的原因：
数量是 TEXT、退货已经是负数、一条退货能退多台、编码要精确 ∈、
"算不出来"不许写成 0.0。
"""

import datetime
import unittest

from src.features.sales.attain import metric as M


def _col(name, codes, weight=0.1):
    return M.Column(name=name, codes=frozenset(codes), weight=weight)


def _plan(columns=None, stores=None):
    columns = columns or (_col("A", ["1001", "1002"], 0.6),
                          _col("B", ["2001"], 0.4),
                          _col("C", [], 0.2))     # 第三列**没映射**（codes 空）
    stores = stores or (M.StorePlan(name="青岛城阳万象汇店", region="城阳\n胶州",
                                    targets=(6, 3, 3)),)
    return M.Plan(period="2026-W38", start=datetime.date(2026, 9, 14),
                  end=datetime.date(2026, 9, 20), columns=columns, stores=stores)


class Test计入与剔除(unittest.TestCase):
    """白名单 + 两条剔除 —— 都按 skill 原文，**别照 `pools.is_sample_marker` 改**。"""

    def test_四种单据类型计入(self):
        for k in ("零售", "分销", "零售退", "分销退"):
            self.assertTrue(M.is_counted(k), k)

    def test_核销和其它一切不计入(self):
        for k in ("核销", "调拨", "入库", "", None, "零售 "):   # '零售 ' 带空格 → 允许 strip
            self.assertEqual(M.is_counted(k), k == "零售 ")

    def test_演示机和体验机剔除(self):
        self.assertTrue(M.is_demo("华为 Mate60 演示机"))
        self.assertTrue(M.is_demo("体验机-手表"))
        self.assertFalse(M.is_demo("华为 Mate60"))
        self.assertFalse(M.is_demo(""))

    def test_外调按分段精确比(self):
        """串号标识长相：`新` / `J,新` / `Y,新` / `外调,新` —— 逗号分隔、位置不固定。"""
        for m in ("外调", "外调,新", "新,外调", "W,外调", "外调，新"):   # 含中文逗号
            self.assertTrue(M.is_transfer(m), m)
        for m in ("新", "J,新", "", None, "外调机", "X外调Y", "不外调"):
            self.assertFalse(M.is_transfer(m), m)

    def test_样式机那套判法故意不照抄(self):
        """⚠ skill 第 9 条：达成**只认商品名称的演示机**，不认串号标识的「样」。

        抄 `pools.is_sample_marker` 会多剔 1900 行（含 `样,新` 那 1816+ 行）。
        """
        self.assertFalse(M.is_demo("华为 Mate60"), "串号标识带「样」不代表商品是演示机")
        self.assertFalse(M.is_transfer("样,新"), "「样」不是外调")

    def test_剔除原因要能报出来(self):
        self.assertEqual(M.drop_reason("零售", "Mate60", "新"), "")
        self.assertEqual(M.drop_reason("零售", "Mate60 演示机", "新"), "演示机/体验机")
        self.assertEqual(M.drop_reason("零售", "Mate60", "外调,新"), "外调")
        self.assertEqual(M.drop_reason("核销", "Mate60", "新"), "单据类型不计入")

    def test_演示机优先于类型报原因(self):
        """两种都占时**报演示机** —— 那一条更具体，门店一看就知道是哪台。"""
        self.assertEqual(M.drop_reason("核销", "体验机", "外调"), "演示机/体验机")


class Test台量的算法(unittest.TestCase):
    """⚠⚠ 三条实测坑都在这一节：数量是 TEXT、退货已是负数、一次退多台。"""

    def test_数量在库里是字符串(self):
        """`erp_sales.数量` 全表 `typeof='text'` —— 别赌 SUM 的隐式转换。"""
        self.assertEqual(M.qty_of("1"), 1)
        self.assertEqual(M.qty_of("-1"), -1)
        self.assertEqual(M.qty_of("-5"), -5)
        self.assertEqual(M.qty_of(1.0), 1)
        self.assertEqual(M.qty_of(None), 0)
        self.assertEqual(M.qty_of(""), 0)
        self.assertEqual(M.qty_of("乱写"), 0)

    def test_退货已经是负数_不许再取负(self):
        """口径说"含退 → 冲减"，而**云商导出里已经冲减好了**。

        写成 `qty if 类型 == '零售' else -qty` 会把退货翻成正的 ——
        结果是"退了货反而更达成"。
        """
        sales = [M.Sale("店", "1001", 1, "零售"), M.Sale("店", "1001", -1, "零售退")]
        self.assertEqual(M.sum_codes(sales, {"1001"}), 0)

    def test_一条退货退多台要按数量算(self):
        """有一条退货一次退 5 台：`COUNT(*)` 会算成 1，`SUM` 才对。"""
        sales = [M.Sale("店", "1001", 6, "零售"), M.Sale("店", "1001", -5, "零售退")]
        self.assertEqual(M.sum_codes(sales, {"1001"}), 1)

    def test_编码要精确命中_不许子串(self):
        """skill 第 8 条：`919724` **不许**命中 `9197245`。"""
        sales = [M.Sale("店", "9197245", 3, "零售")]
        self.assertEqual(M.sum_codes(sales, {"919724"}), 0)
        self.assertEqual(M.sum_codes(sales, {"9197245"}), 3)


class Test三条达成率规则(unittest.TestCase):
    def test_目标为零记百分之百(self):
        """不是 0%，也不是无穷大 —— skill 原文第一条。"""
        self.assertEqual(M.rate_of(0, 0), 1.0)
        self.assertEqual(M.rate_of(0, 7), 1.0)

    def test_封顶一百二(self):
        self.assertAlmostEqual(M.rate_of(6, 12), 1.2, places=6)
        self.assertAlmostEqual(M.rate_of(6, 100), 1.2, places=6)
        self.assertAlmostEqual(M.rate_of(6, 3), 0.5, places=6)

    def test_封顶那个数的依据写在代码里(self):
        """1.2 来自目标表最后一行（办公室写的奖惩规则），**不是我们定的**。"""
        self.assertEqual(M.CAP, 1.2)

    def test_加权平均(self):
        # 0.6×1.0 + 0.4×0.5 = 0.8
        self.assertAlmostEqual(M.total_of((0.6, 0.4), (1.0, 0.5)), 0.8, places=6)

    def test_没映射的列跳过_分母同步减(self):
        """⚠ 分母是 `sum(weights)`，不是硬编码 1 —— 缺列之后就不是 1 了。"""
        # 只剩 0.6 那一列（1.0）⇒ 结果还是 1.0，不是 0.6×1.0 = 0.6
        self.assertAlmostEqual(M.total_of((0.6, 0.4), (1.0, None)), 1.0, places=6)

    def test_一列都没剩返回_None_不是零(self):
        """⚠⚠ 本模块最重要的一条：「0%」和「算不出来」在界面上是两件事。

        写成 0.0 就造出了达标线以下的**假数字**（前端把 null 渲染成 `—`）。
        """
        self.assertIsNone(M.total_of((0.6, 0.4), (None, None)))
        self.assertIsNone(M.total_of((), ()))
        self.assertIsNone(M.total_of((0.0, 0.0), (1.0, 1.0)), "权重全 0 ⇒ 分母 0，也算不出来")


class Test一家店的核算(unittest.TestCase):
    def test_逐列算再加权(self):
        plan = _plan()
        sales = [M.Sale("青岛城阳万象汇店", "1001", 7, "零售"),     # 列A 目标6 → 1.1667
                 M.Sale("青岛城阳万象汇店", "2001", 2, "零售")]     # 列B 目标3 → 0.6667
        r = M.store_result(plan, plan.stores[0], sales, erp_name="青岛城阳万象汇店")
        self.assertEqual(r.actuals[0], 7)
        self.assertEqual(r.actuals[1], 2)
        self.assertAlmostEqual(r.rates[0], 7 / 6.0, places=4)
        self.assertIsNone(r.rates[2], "没映射的那列不算")
        # (0.6×7/6 + 0.4×2/3) / 1.0 = 0.7 + 0.2667 = 0.9667
        self.assertAlmostEqual(r.total, (0.6 * (7 / 6.0) + 0.4 * (2 / 3.0)), places=6)
        self.assertTrue(r.matched)

    def test_没匹配上的店给_None_不给零(self):
        plan = _plan()
        rows = M.all_results(plan, {}, store_map={})
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].matched)
        self.assertIsNone(rows[0].total)
        self.assertEqual(rows[0].rates, (None, None, None))
        self.assertEqual(rows[0].targets, (6, 3, 3), "目标照抄目标表，界面要显示它")

    def test_一行销售都没有_是零不是_None(self):
        """⚠ 和上面那条**故意不同**：店匹配上了、这周确实没卖 ⇒ 0%（真数字）。

        分不清这两个，界面上"没匹配到店"和"这周没卖"会长得一样。
        """
        plan = _plan()
        rows = M.all_results(plan, {"青岛城阳万象汇店": []},
                             store_map={"青岛城阳万象汇店": "青岛城阳万象汇店"})
        self.assertTrue(rows[0].matched)
        self.assertEqual(rows[0].actuals[:2], (0, 0))
        self.assertEqual(rows[0].rates[:2], (0.0, 0.0))
        self.assertAlmostEqual(rows[0].total, 0.0, places=6)

    def test_顺序照目标表(self):
        stores = (M.StorePlan("A店", "", (1, 1, 1)), M.StorePlan("B店", "", (1, 1, 1)))
        plan = _plan(stores=stores)
        rows = M.all_results(plan, {}, store_map={})
        self.assertEqual([r.store for r in rows], ["A店", "B店"], "顺序 = 目标表顺序")

    def test_落盘字典的形状对得上前端(self):
        """`as_dict` 直接喂前端的 `rows[]`（契约见设计 §5.3）。"""
        plan = _plan()
        r = M.store_result(plan, plan.stores[0], [M.Sale("店", "1001", 3, "零售")],
                           erp_name="青岛城阳万象汇店")
        d = r.as_dict()
        for k in ("store", "erp_name", "matched", "targets", "actuals", "rates", "total"):
            self.assertIn(k, d)
        self.assertEqual(len(d["rates"]), 3)
        self.assertIsNone(d["rates"][2], "null 到前端渲染成 —")
        self.assertEqual(d["total"], round(0.6 * 0.5, 4))


class Test期间名(unittest.TestCase):
    def test_按_iso_周推(self):
        self.assertEqual(M.period_of(datetime.date(2026, 9, 14),
                                     datetime.date(2026, 9, 20)), "2026-W38")

    def test_跨年那周也算得对(self):
        # 2027-01-01 是周五，属于 2026 年的第 53 周（ISO）
        self.assertEqual(M.period_of(datetime.date(2026, 12, 28),
                                     datetime.date(2027, 1, 3)), "2026-W53")


if __name__ == "__main__":
    unittest.main()
