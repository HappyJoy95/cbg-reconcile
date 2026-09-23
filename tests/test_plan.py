# -*- coding: utf-8 -*-
"""月度生意计划的**口径**测试 —— `features/plan/monthly/metric.py`（纯函数，零 IO）。

⚠ 本文件里最该盯住的三条（都是用户明确纠正过 / 实测踩出来的）：

1. **`Pura X View` 算 FD，不算折叠机** —— 它跟 `Pura X系列` 挨着，按前缀猜就错，
   实测 10 天 170 台、占 FD 的 38%。
2. **`数量` 在库里是 TEXT** —— `SUM()` 会替你做隐式转换，看着是对的，但那是撞运气。
3. **环比的分母** —— 上月为 0 或为负时**不许给百分比**（`new` / `bare` 两种形态）。

另有一条"恒等式"性质的测试：**七块 + 不纳入 + 未知 == 全部行**，
少算一台就说明映射漏了词 —— 那种错在页面上只表现为"这个系列怎么这么少"。
"""

import datetime
import sqlite3
import unittest
from pathlib import Path

from src.features.plan.monthly import export as E_mod
from src.features.plan.monthly import metric as M
from src.features.plan.monthly import plan as P
from src.paths import ROOT


# ------------------------------------------------------------------ 造行
def _sale(store="青岛城阳万达店", cat1="手机", cat2="Mate80系列", cat3="Mate80",
          qty=1, amount=6000.0, profit=600.0, who=""):
    return M.Sale(store, cat1, cat2, cat3, qty, amount, profit, who)


# ------------------------------------------------------------------ 分类映射
class Test分类映射(unittest.TestCase):
    def test_七个块一个不少(self):
        self.assertEqual(len(M.BLOCKS), 7)
        self.assertEqual(set(M.BLOCKS),
                         {"折叠机", "FD", "ND", "穿戴", "音频", "平板", "电脑"})

    def test_每个词都能归到它自己那个块(self):
        """`PHONE_SERIES` 里写的词，逐个查一遍 —— 写错一个字母这儿就红。"""
        for block, names in M.PHONE_SERIES.items():
            for name in names:
                with self.subTest(word=name):
                    self.assertEqual(M.block_of("手机", name), block)

    def test_一个词只属于一个块(self):
        """反向表不许有重复键 —— 同一个系列落进两块，是**静默**的口径分裂。"""
        seen = {}
        for block, names in M.PHONE_SERIES.items():
            for name in names:
                self.assertNotIn(name, seen, "%s 同时出现在 %s 和 %s" % (name, seen.get(name), block))
                seen[name] = block

    def test_源数据里那些奇怪的写法是照抄的(self):
        """⚠ 大小写/空格/拼写都照抄云商 —— 别"顺手修正"（模块头坑 1）。"""
        self.assertEqual(M.block_of("手机", "mate X5系列"), "折叠机")     # 小写 m
        self.assertEqual(M.block_of("手机", "Nova filp系列"), "折叠机")   # 源数据把 flip 拼成 filp
        self.assertEqual(M.block_of("手机", "nova 15系列"), "ND")        # nova 后面有空格

    def test_Pura_X_View_算_FD_不算折叠机(self):
        """⭐ 用户明确纠正过：「puraxview 算 fd，不是折叠屏」。"""
        self.assertEqual(M.block_of("手机", "Pura X View"), "FD")
        # 而它旁边那两个确实是折叠屏 —— 别一起挪过去
        self.assertEqual(M.block_of("手机", "Pura X系列"), "折叠机")
        self.assertEqual(M.block_of("手机", "Pura X Max系列"), "折叠机")

    def test_wiko_和_麦芒_算_ND(self):
        """⭐ 用户第二轮：「wiko 算 nd，麦芒 nd」。"""
        self.assertEqual(M.block_of("手机", "wiko系列"), "ND")
        self.assertEqual(M.block_of("手机", "麦芒系列"), "ND")

    def test_三个不纳入的系列(self):
        """⭐ 用户第二轮：「别的不算」—— 认得出、但不进任何一块，**计数报出来**。"""
        for word in ("联想Moto系列", "外购手机", "手机办公机"):
            with self.subTest(word=word):
                self.assertEqual(M.block_of("手机", word), M.SKIP)

    def test_没见过的词是_UNKNOWN(self):
        """云商上了新系列 ⇒ 必须冒出来，不许静默归"其它"。"""
        self.assertEqual(M.block_of("手机", "Mate 100系列"), M.UNKNOWN)

    def test_认得出但不在这七块(self):
        for c1 in ("智慧屏", "全屋智能", "潮玩礼品", "手机平板周边", "会员"):
            with self.subTest(cat1=c1):
                self.assertEqual(M.block_of(c1, "随便"), M.OTHER)

    def test_一级分类完全没见过也是_UNKNOWN(self):
        self.assertEqual(M.block_of("量子计算设备", "X"), M.UNKNOWN)

    def test_空的不算(self):
        self.assertIsNone(M.block_of("", ""))
        self.assertIsNone(M.block_of(None, None))

    def test_手机没有二级也算_UNKNOWN(self):
        """空二级和"没见过的二级"一样危险 —— 都不许静默丢。"""
        self.assertEqual(M.block_of("手机", ""), M.UNKNOWN)

    def test_四块直接按一级分类认(self):
        self.assertEqual(M.block_of("智能穿戴", "华为智能手表"), "穿戴")
        self.assertEqual(M.block_of("音频产品", "耳机"), "音频")
        self.assertEqual(M.block_of("平板", "华为平板电脑"), "平板")
        # ⚠「电脑」只算笔记本 —— 台式机/显示器/打印机在"台显打印"那一级，本来就是 OTHER
        self.assertEqual(M.block_of("笔记本", "华为笔记本"), "电脑")
        self.assertEqual(M.block_of("台显打印", "消费台式机"), M.OTHER)

    def test_联想和外购的笔记本平板都不纳入(self):
        """⭐ 用户 2026-09-21 晚：「**联想笔记本去掉**」。

        ⚠ 这四块原来**只看一级分类**、二级一概不看 ⇒ 联想笔记本照样算进「电脑」，
          页面上就会冒出一个「联想笔记本」列（用户就是这么看到的）。
        ⚠ 影响**很小**：名单内 28 家店、2026-09 窗口里只有 1 行 / 1 台
          （全库那 3374 行几乎都是名单外的联想专卖店卖的）。
          小也要改 —— "看着是个华为计划、里面混着联想"比数字不准更糟。
        ⚠ 判据是"**不纳入**"（`SKIP`）而不是 `OTHER`：它要**计数报出来**
          （页面上那行「不纳入的」+ 导出的「说明」），别静默丢。
        """
        for c1, c2 in (("笔记本", "联想笔记本"), ("笔记本", "外购笔记本"),
                       ("笔记本", "笔记本办公机"), ("平板", "联想平板电脑"),
                       ("平板", "外购平板电脑"), ("智能穿戴", "外购手表")):
            self.assertEqual(M.block_of(c1, c2), M.SKIP, "%s / %s" % (c1, c2))
        # 华为的照旧算 —— 别把整块一起关了
        self.assertEqual(M.block_of("笔记本", "华为笔记本"), "电脑")
        self.assertEqual(M.block_of("平板", "华为平板电脑"), "平板")
        self.assertEqual(M.block_of("智能穿戴", "华为智能手表"), "穿戴")

    def test_不纳入的会按二级词计数报出来(self):
        """`collect` 报的是**二级分类那个词** —— 页面上要让人看见"联想笔记本 N 行"。"""
        rows = [M.Sale("青岛城阳万达店", "笔记本", "联想笔记本", "小新14", 3, 15000.0, 900.0),
                M.Sale("青岛城阳万达店", "笔记本", "华为笔记本", "MateBook X", 2, 14000.0, 800.0)]
        acc, unknown, skipped = M.collect(rows)
        self.assertEqual(skipped, {"联想笔记本": 1})
        self.assertEqual(unknown, {})
        # 只有华为那台进了累加表
        self.assertEqual(sum(t.qty for t in acc.values()), 2)


# ------------------------------------------------------------------ 期间
class Test期间(unittest.TestCase):
    def test_当月至今(self):
        sp = M.month_span(datetime.date(2026, 9, 21))
        self.assertEqual((sp.start, sp.end), (datetime.date(2026, 9, 1),
                                              datetime.date(2026, 9, 21)))
        self.assertEqual(sp.days, 21)

    def test_上月同期正常对齐(self):
        sp = M.prev_month_same_span(datetime.date(2026, 9, 21))
        self.assertEqual((sp.start, sp.end), (datetime.date(2026, 8, 1),
                                              datetime.date(2026, 8, 21)))

    def test_三月底_上月只有28天_取满整月(self):
        """⭐ 3/31 → 2/1~2/28（**不是 2/31 报错**，也不是 3/3）。"""
        sp = M.prev_month_same_span(datetime.date(2026, 3, 31))
        self.assertEqual((sp.start, sp.end), (datetime.date(2026, 2, 1),
                                              datetime.date(2026, 2, 28)))

    def test_闰年二月(self):
        sp = M.prev_month_same_span(datetime.date(2028, 3, 31))
        self.assertEqual((sp.start, sp.end), (datetime.date(2028, 2, 1),
                                              datetime.date(2028, 2, 29)))

    def test_31号对上月30天(self):
        sp = M.prev_month_same_span(datetime.date(2026, 5, 31))
        self.assertEqual((sp.start, sp.end), (datetime.date(2026, 4, 1),
                                              datetime.date(2026, 4, 30)))

    def test_一号(self):
        sp = M.prev_month_same_span(datetime.date(2026, 9, 1))
        self.assertEqual((sp.start, sp.end), (datetime.date(2026, 8, 1),
                                              datetime.date(2026, 8, 1)))

    def test_跨年(self):
        """1 月 → 去年 12 月。"""
        sp = M.prev_month_same_span(datetime.date(2026, 1, 15))
        self.assertEqual((sp.start, sp.end), (datetime.date(2025, 12, 1),
                                              datetime.date(2025, 12, 15)))

    def test_字符串和datetime也认(self):
        self.assertEqual(M.month_span("2026-09-21").days, 21)
        self.assertEqual(M.month_span(datetime.datetime(2026, 9, 21, 18, 0)).days, 21)

    def test_缺月检测(self):
        """上月数据不够 ⇒ 给一句人话，而不是默默算环比（用户纠正过"库是全月的"）。"""
        sp = M.prev_month_same_span(datetime.date(2026, 9, 21))
        self.assertEqual(M.missing_months(21, sp), "")
        self.assertIn("上月数据只覆盖 5 天", M.missing_months(5, sp))


# ------------------------------------------------------------------ 环比
class Test环比(unittest.TestCase):
    def test_涨跌平(self):
        self.assertEqual(M.growth_kind(120, 100), "up")
        self.assertEqual(M.growth_kind(80, 100), "down")
        self.assertEqual(M.growth_kind(100, 100), "flat")

    def test_百分比(self):
        self.assertAlmostEqual(M.growth_rate(120, 100), 0.2)
        self.assertAlmostEqual(M.growth_rate(50, 100), -0.5)
        self.assertAlmostEqual(M.growth_rate(100, 100), 0.0)

    def test_上月为0本月有_是新增_不给百分比(self):
        """⭐ 分母是 0 ⇒ 任何百分比都是编的。"""
        self.assertEqual(M.growth_kind(5, 0), "new")
        self.assertIsNone(M.growth_rate(5, 0))

    def test_两边都是0(self):
        self.assertEqual(M.growth_kind(0, 0), "zero")
        self.assertIsNone(M.growth_rate(0, 0))

    def test_上月为负_不给百分比(self):
        """⭐ 负数分母的"增长率"方向是反的（退了 10 台，这个月卖 5 台是好转还是恶化？）。"""
        self.assertEqual(M.growth_kind(5, -10), "bare")
        self.assertIsNone(M.growth_rate(5, -10))

    def test_浮点尾巴不算变化(self):
        """金额是两位小数累加出来的，`==` 判"持平"会漏。"""
        self.assertEqual(M.growth_kind(100.0000001, 100.0), "flat")

    def test_compare_三个指标都出(self):
        got = M.compare(M.Totals(120, 200.0, 20.0), M.Totals(100, 100.0, 40.0))
        self.assertEqual(set(got), {"qty", "amount", "profit"})
        self.assertEqual(got["qty"]["kind"], "up")
        self.assertEqual(got["profit"]["kind"], "down")
        self.assertAlmostEqual(got["amount"]["rate"], 1.0)

    def test_compare_里没有无穷大和_NaN(self):
        import math
        got = M.compare(M.Totals(1, 1.0, 1.0), M.Totals(0, 0.0, 0.0))
        for m in M.METRICS:
            self.assertEqual(got[m]["kind"], "new")
            self.assertIsNone(got[m]["rate"])          # None，不是 inf/NaN
            self.assertFalse(isinstance(got[m]["rate"], float)
                             and math.isnan(got[m]["rate"]))


# ------------------------------------------------------------------ 谓词
class Test谓词(unittest.TestCase):
    def test_数量在库里是文本(self):
        """⭐ 全表 `typeof='text'` —— 别指望 SQL 的隐式转换。"""
        self.assertEqual(M.qty_of("1"), 1)
        self.assertEqual(M.qty_of("-1"), -1)
        self.assertEqual(M.qty_of("2.0"), 2)
        self.assertEqual(M.qty_of(""), 0)
        self.assertEqual(M.qty_of(None), 0)
        self.assertEqual(M.qty_of(" 3 "), 3)

    def test_金额带逗号也认(self):
        self.assertAlmostEqual(M.money_of("1,234.50"), 1234.5)
        self.assertAlmostEqual(M.money_of(None), 0.0)
        self.assertAlmostEqual(M.money_of(""), 0.0)

    def test_单据类型白名单(self):
        for k in ("零售", "分销", "零售退", "分销退"):
            self.assertTrue(M.is_counted(k), k)
        # ⭐ 用户第二轮：核销 / 客情单**不算**（实测金额全是 0，商品是贴膜/第三方耳机）
        for k in ("核销", "客情单", "其它", "", None):
            self.assertFalse(M.is_counted(k), k)

    def test_演示机和体验机(self):
        self.assertTrue(M.is_demo("手机/华为/Mate80 Pro 演示机"))
        self.assertTrue(M.is_demo("平板/华为/MatePad 体验机"))
        self.assertFalse(M.is_demo("手机/华为/Mate80 Pro"))

    def test_外调按逗号分段比(self):
        """⭐ `外调,新` 要认；`X外调Y` 不许认（子串判法会静默误剔）。"""
        self.assertTrue(M.is_transfer("外调,新"))
        self.assertTrue(M.is_transfer("新,外调"))
        self.assertTrue(M.is_transfer("外调"))
        self.assertFalse(M.is_transfer("X外调Y"))
        self.assertFalse(M.is_transfer("新"))
        self.assertFalse(M.is_transfer(None))

    def test_drop_reason_三条(self):
        self.assertEqual(M.drop_reason("零售", "Mate80 演示机", "新"), "演示机/体验机")
        self.assertEqual(M.drop_reason("零售", "Mate80", "外调,新"), "外调")
        self.assertEqual(M.drop_reason("核销", "Mate80", "新"), "单据类型不计入")
        self.assertEqual(M.drop_reason("零售", "Mate80", "新"), "")      # 空 = 留着


# ------------------------------------------------------------------ 聚合
class Test聚合(unittest.TestCase):
    def test_按四层累加(self):
        rows = [_sale(qty=2, amount=12000, profit=1200),
                _sale(qty=1, amount=6000, profit=600)]
        acc, unk, skip = M.collect(rows)
        self.assertEqual(len(acc), 1)
        tot = acc[("青岛城阳万达店", "FD", "Mate80系列", "Mate80")]
        self.assertEqual(tot.qty, 3)
        self.assertAlmostEqual(tot.amount, 18000)
        self.assertEqual(unk, {})
        self.assertEqual(skip, {})

    def test_退货是负数_直接相加(self):
        """⭐ 源数据里退货已经是负的（`分销退` −1）—— 再取反就成了"退货反而更达成"。"""
        rows = [_sale(qty=2, amount=12000, profit=1200),
                _sale(cat3="Mate80", qty=-1, amount=-6000, profit=-600)]
        acc, _u, _s = M.collect(rows)
        tot = acc[("青岛城阳万达店", "FD", "Mate80系列", "Mate80")]
        self.assertEqual(tot.qty, 1)
        self.assertAlmostEqual(tot.amount, 6000)

    def test_一条退货退多台(self):
        rows = [_sale(qty=5, amount=30000, profit=3000), _sale(qty=-5, amount=-30000, profit=-3000)]
        acc, _u, _s = M.collect(rows)
        self.assertEqual(acc[("青岛城阳万达店", "FD", "Mate80系列", "Mate80")].qty, 0)

    def test_未认出的词被收集(self):
        rows = [_sale(cat1="手机", cat2="Mate 100系列"),
                _sale(cat1="量子计算设备", cat2="Q1")]
        _acc, unk, _s = M.collect(rows)
        self.assertIn("手机 / Mate 100系列", unk)
        self.assertIn("量子计算设备", unk)

    def test_不纳入的词单独收(self):
        rows = [_sale(cat1="手机", cat2="联想Moto系列"), _sale(cat1="手机", cat2="联想Moto系列")]
        acc, _u, skip = M.collect(rows)
        self.assertEqual(skip, {"联想Moto系列": 2})
        self.assertEqual(acc, {})

    def test_恒等式_七块加不纳入加未知等于全部行(self):
        """⭐ 少算一台就是映射漏了词 —— 页面上只表现为"这个系列怎么这么少"。"""
        rows = [
            _sale(cat2="Mate80系列"), _sale(cat2="Pura X View"), _sale(cat2="nova 16系列"),
            _sale(cat2="wiko系列"), _sale(cat2="联想Moto系列"), _sale(cat2="Mate 100系列"),
            _sale(cat1="智能穿戴", cat2="华为智能手表"),
            _sale(cat1="笔记本", cat2="华为笔记本"),
            _sale(cat1="智慧屏", cat2="华为智慧屏"),
        ]
        acc, unk, skip = M.collect(rows)
        in_blocks = sum(t.qty for t in acc.values())
        self.assertEqual(in_blocks + sum(skip.values()) + sum(unk.values()) + 1, len(rows))

    def test_rollup_三层加起来等于总数(self):
        rows = [_sale(cat2="Mate80系列", cat3="Mate80", qty=2),
                _sale(cat2="Mate80系列", cat3="Mate80 Pro", qty=3),
                _sale(cat2="Pura 90系列", cat3="Pura 90", qty=5),
                _sale(cat1="笔记本", cat2="华为笔记本", cat3="MateBook D 系列", qty=7)]
        acc, _u, _s = M.collect(rows)
        for depth in (2, 3, 4):
            got = sum(t.qty for t in M.rollup(acc, depth).values())
            self.assertEqual(got, 17, "depth=%d 加总不对" % depth)

    def test_tree_每家店都出现_哪怕一行都没有(self):
        """⭐ "0 是 0"和"这家店不见了"是两件事（§三·0）。"""
        acc, _u, _s = M.collect([_sale(store="青岛城阳万达店")])
        tree = M.tree(acc, {}, ["青岛城阳万达店", "青岛悦荟店"])
        self.assertEqual([r["store"] for r in tree], ["青岛城阳万达店", "青岛悦荟店"])
        empty = tree[1]
        self.assertEqual(empty["blocks"][0]["cur"]["qty"], 0)
        self.assertEqual(len(empty["blocks"]), 7)          # 七块都列出来

    def test_tree_三层结构与上下层一致(self):
        """⭐ 上层**必须**等于下层之和 —— 不许两处各算一遍。"""
        rows = [_sale(cat2="Mate80系列", cat3="Mate80", qty=2, amount=12000, profit=1200),
                _sale(cat2="Mate80系列", cat3="Mate80 Pro", qty=3, amount=24000, profit=2400),
                _sale(cat2="Pura 90系列", cat3="Pura 90", qty=5, amount=20000, profit=2000)]
        acc, _u, _s = M.collect(rows)
        tree = M.tree(acc, {}, ["青岛城阳万达店"])
        fd = [b for b in tree[0]["blocks"] if b["name"] == "FD"][0]
        self.assertEqual(fd["cur"]["qty"], 10)
        self.assertEqual(sum(s["cur"]["qty"] for s in fd["series"]), 10)
        for s in fd["series"]:
            self.assertEqual(sum(m["cur"]["qty"] for m in s["models"]), s["cur"]["qty"])
        m80 = [s for s in fd["series"] if s["name"] == "Mate80系列"][0]
        self.assertEqual([m["name"] for m in m80["models"]], ["Mate80", "Mate80 Pro"])

    def test_tree_带环比(self):
        cur, _u, _s = M.collect([_sale(qty=12, amount=72000, profit=7200)])
        prev, _u2, _s2 = M.collect([_sale(qty=10, amount=60000, profit=6000)])
        fd = [b for b in M.tree(cur, prev, ["青岛城阳万达店"])[0]["blocks"]
              if b["name"] == "FD"][0]
        self.assertEqual(fd["cur"]["qty"], 12)
        self.assertEqual(fd["prev"]["qty"], 10)
        self.assertEqual(fd["growth"]["qty"]["kind"], "up")
        self.assertAlmostEqual(fd["growth"]["qty"]["rate"], 0.2)

    def test_只有真有数的块才有系列(self):
        """全 0 的块留一行（一眼看到这块没卖），但不该拖着一串空的系列行。"""
        acc, _u, _s = M.collect([_sale(cat1="笔记本", cat2="华为笔记本", cat3="MateBook D 系列")])
        tree = M.tree(acc, {}, ["青岛城阳万达店"])
        pc = [b for b in tree[0]["blocks"] if b["name"] == "电脑"][0]
        fd = [b for b in tree[0]["blocks"] if b["name"] == "FD"][0]
        self.assertEqual(len(pc["series"]), 1)
        self.assertEqual(fd["series"], [])

    def test_门店行带合计的环比(self):
        """⭐ 用户 2026-09-21：「**合计也加上和上个月同期对比**」。

        ⚠ 门店行原来**只有 `blocks`**（合计是前端把七块加起来现算的）⇒ 没有环比可显示。
          现在后端给一行 `growth`，**分子分母都是七块相加**（跟合计那个数同口径）——
          别拿七个百分比去平均、也别拿"全部叶子"算（后者把系列/机型重复算一遍）。
        """
        cur, _u, _s = M.collect([_sale(qty=12, amount=72000, profit=7200)])
        prev, _u2, _s2 = M.collect([_sale(qty=10, amount=60000, profit=6000)])
        row = M.tree(cur, prev, ["青岛城阳万达店"])[0]
        g = row["growth"]
        self.assertEqual(g["qty"]["kind"], "up")
        self.assertAlmostEqual(g["qty"]["rate"], 0.2)          # 12 vs 10
        self.assertAlmostEqual(g["amount"]["rate"], 0.2)
        self.assertAlmostEqual(g["profit"]["rate"], 0.2)
        # 口径：合计的环比 == 七块相加后的环比（换一组只有一块有数的，也一样）
        acc_c, _a, _b = M.collect([_sale(qty=3, amount=9000, profit=300),
                                   _sale(cat1="笔记本", cat2="华为笔记本", cat3="X",
                                         qty=1, amount=7000, profit=400)])
        acc_p, _a2, _b2 = M.collect([_sale(qty=2, amount=8000, profit=200)])
        row2 = M.tree(acc_c, acc_p, ["青岛城阳万达店"])[0]
        self.assertAlmostEqual(row2["growth"]["amount"]["rate"], (16000 - 8000) / 8000.0)
        self.assertEqual(row2["growth"]["qty"]["kind"], "up")

    def test_人那行也带合计的环比(self):
        rows_c = [_sale(qty=12, amount=72000, profit=7200)._replace(who="李明"),
                  _sale(qty=4, amount=24000, profit=2400)._replace(who="王强")]
        rows_p = [_sale(qty=10, amount=60000, profit=6000)._replace(who="李明")]
        cur, _u, _s = M.collect(rows_c, with_who=True)
        prev, _u2, _s2 = M.collect(rows_p, with_who=True)
        ppl = M.people_of(cur, prev, ["青岛城阳万达店"])["青岛城阳万达店"]
        by = {p["name"]: p for p in ppl}
        self.assertAlmostEqual(by["李明"]["growth"]["qty"]["rate"], 0.2)   # 12 vs 10
        # 王强上月没卖 ⇒ 「新增」（不给百分比）—— 跟块级/门店级同一套判据
        self.assertEqual(by["王强"]["growth"]["qty"]["kind"], "new")
        self.assertIsNone(by["王强"]["growth"]["qty"]["rate"])

    def test_summary_七块合计(self):
        rows = [_sale(cat2="Mate80系列", qty=2, amount=12000, profit=1200),
                _sale(cat1="笔记本", cat2="华为笔记本", cat3="MateBook D 系列",
                      qty=1, amount=5000, profit=300)]
        got = M.summarize(M.tree(*M.collect(rows)[:2], ["青岛城阳万达店"]))
        self.assertEqual(got["blocks"]["FD"]["cur"]["qty"], 2)
        self.assertEqual(got["blocks"]["电脑"]["cur"]["qty"], 1)
        self.assertEqual(got["blocks"]["音频"]["cur"]["qty"], 0)
        self.assertEqual(got["total"]["cur"]["qty"], 3)
        self.assertAlmostEqual(got["total"]["cur"]["amount"], 17000)

    def test_summary_只算名单内的店(self):
        """名单外的店（联想专卖店那种）一行都不许进合计。"""
        rows = [_sale(store="青岛城阳万达店", qty=2),
                _sale(store="联想青岛万象城店", qty=99)]
        tree = M.tree(*M.collect(rows)[:2], ["青岛城阳万达店"])
        self.assertEqual(M.summarize(tree)["total"]["cur"]["qty"], 2)

    def test_summarize_跟着被滤过的_rows_走(self):
        """⭐ 门店只看到自己那家时，合计**不能**还是全区 —— 那是最难解释的一种错。"""
        rows = [_sale(store="青岛城阳万达店", qty=2),
                _sale(store="青岛悦荟店", qty=99)]
        stores = ["青岛城阳万达店", "青岛悦荟店"]
        tree = M.tree(*M.collect(rows)[:2], stores)
        self.assertEqual(M.summarize(tree)["total"]["cur"]["qty"], 101)
        self.assertEqual(M.summarize(tree[:1])["total"]["cur"]["qty"], 2)

    def test_summarize_合计等于明细之和(self):
        rows = [_sale(cat2="Mate80系列", qty=3, amount=18000, profit=1800),
                _sale(cat2="Pura 90系列", qty=2, amount=7000, profit=700),
                _sale(cat1="音频产品", cat2="耳机", cat3="FreeClip 2",
                      qty=5, amount=5000, profit=500)]
        tree = M.tree(*M.collect(rows)[:2], ["青岛城阳万达店"])
        got = M.summarize(tree)
        self.assertEqual(sum(got["blocks"][b]["cur"]["qty"] for b in M.BLOCKS),
                         got["total"]["cur"]["qty"])
        # 也等于明细行加起来
        self.assertEqual(got["total"]["cur"]["qty"], 10)


class Test分区汇总(unittest.TestCase):
    """每区末尾一行「共计」—— 后端算好再下发（`region_sums`），照 film 的 film-sum。"""

    def test_按区域分组_形状和门店行一样(self):
        rows_a = M.tree(*M.collect([
            _sale(store="甲店", qty=2, amount=12000, profit=1200)])[:2], ["甲店"])
        rows_b = M.tree(*M.collect([
            _sale(store="乙店", qty=3, amount=18000, profit=1800)])[:2], ["乙店"])
        regions = {"甲店": "西北区", "乙店": "市区"}
        got = M.region_sums(rows_a + rows_b, regions)
        self.assertEqual(set(got), {"西北区", "市区"})
        s = got["西北区"]
        self.assertEqual(s["store"], "共计：西北区")
        self.assertEqual(s["region"], "西北区")
        self.assertTrue(s["is_sum"])
        # blocks 形状跟 tree() 的一行一样（七块齐全、能被 planCellHtml 消费）
        self.assertEqual([b["name"] for b in s["blocks"]], list(M.BLOCKS))
        self.assertIn("growth", s)

    def test_数字是加总_环比用合计重算(self):
        rows = M.tree(*M.collect([
            _sale(store="甲店", qty=2, amount=12000, profit=1200),
            _sale(store="乙店", qty=3, amount=18000, profit=1800)])[:2],
                      ["甲店", "乙店"])
        got = M.region_sums(rows, {"甲店": "市区", "乙店": "市区"})["市区"]
        fd = [b for b in got["blocks"] if b["name"] == "FD"][0]
        self.assertEqual(fd["cur"]["qty"], 5)
        self.assertAlmostEqual(fd["cur"]["amount"], 30000)
        self.assertAlmostEqual(fd["cur"]["profit"], 3000)
        # 上月全 0 ⇒ 新增（不给百分比），跟 compare 同一套
        self.assertEqual(fd["growth"]["qty"]["kind"], "new")
        self.assertIsNone(fd["growth"]["qty"]["rate"])

    def test_没配区域的归其他(self):
        rows = M.tree(*M.collect([_sale(store="甲店")])[:2], ["甲店"])
        got = M.region_sums(rows, {})
        self.assertIn("其他", got)

    def test_系列机型也并进去(self):
        rows = M.tree(*M.collect([
            _sale(store="甲店", cat2="Mate80系列", cat3="Mate80", qty=1),
            _sale(store="乙店", cat2="Mate80系列", cat3="Mate80 Pro", qty=2)])[:2],
                      ["甲店", "乙店"])
        got = M.region_sums(rows, {"甲店": "市区", "乙店": "市区"})["市区"]
        fd = [b for b in got["blocks"] if b["name"] == "FD"][0]
        ser = [s for s in fd["series"] if s["name"] == "Mate80系列"][0]
        self.assertEqual(ser["cur"]["qty"], 3)
        self.assertEqual(sorted(m["name"] for m in ser["models"]),
                         ["Mate80", "Mate80 Pro"])

    def test_跟着被滤过的_rows_走(self):
        rows = M.tree(*M.collect([
            _sale(store="甲店", qty=2),
            _sale(store="乙店", qty=99)])[:2], ["甲店", "乙店"])
        regions = {"甲店": "市区", "乙店": "南区"}
        self.assertEqual(M.region_sums(rows, regions)["市区"]["blocks"][1]["cur"]["qty"], 2)
        self.assertEqual(M.region_sums(rows[:1], regions)["市区"]["blocks"][1]["cur"]["qty"], 2)
        self.assertNotIn("南区", M.region_sums(rows[:1], regions))


if __name__ == "__main__":
    unittest.main()



# ------------------------------------------------------------------ IO 层
#: 真库里 `数量` / `金额` / `零售考核毛利` 都是 **TEXT**（实测）—— 测试也照 TEXT 造，
#: 这样 `qty_of` / `money_of` 那两条转换路径才真的被走到。
_SCHEMA = """
CREATE TABLE erp_sales (
    单号 TEXT, 商品编码 TEXT, 门店 TEXT,
    一级分类 TEXT, 二级分类 TEXT, 三级分类 TEXT,
    数量 TEXT, 金额 TEXT, 零售考核毛利 TEXT,
    单据类型 TEXT, 商品名称 TEXT, 串号标识 TEXT, 支付时间 TEXT);
"""


def _db(rows):
    """造一个只有 `erp_sales` 的内存库。`rows` 是 dict 列表。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute(_SCHEMA)
    for r in rows:
        conn.execute("INSERT INTO erp_sales VALUES (%s)"
                     % ",".join("?" * 13),
                     tuple(str(r.get(k, "")) for k in
                           ("单号", "商品编码", "门店", "一级分类", "二级分类", "三级分类",
                            "数量", "金额", "零售考核毛利",
                            "单据类型", "商品名称", "串号标识", "支付时间")))
    conn.commit()
    return conn


def _row(store="青岛城阳万达店", cat1="手机", cat2="Mate80系列", cat3="Mate80",
         qty="1", amount="6000", profit="600", kind="零售", product="手机/华为/Mate80",
         mark="新", when="2026-09-10 12:00:00", doc="SI001", code="1001"):
    return {"门店": store, "一级分类": cat1, "二级分类": cat2, "三级分类": cat3,
            "数量": qty, "金额": amount, "零售考核毛利": profit, "单据类型": kind,
            "商品名称": product, "串号标识": mark, "支付时间": when,
            "单号": doc, "商品编码": code}


def _root(tmp, stores=None):
    """造一个临时的**安装根**（只有 `config/stores.yaml`）。"""
    import yaml
    base = Path(tmp)
    (base / "config").mkdir(parents=True, exist_ok=True)
    rows = stores if stores is not None else [
        {"erp_name": "青岛城阳万达店", "region": "西北区", "kind": "体验店"},
        {"erp_name": "青岛悦荟店", "region": "市区", "kind": "体验店"},
        {"erp_name": "平台岗", "kind": "平台岗"},          # ⚠ 没 region ⇒ 不是店
        {"erp_name": "", "region": "市区", "kind": "合作店"},   # ⚠ 空名 ⇒ 不是店
    ]
    (base / "config" / "stores.yaml").write_text(
        yaml.safe_dump({"stores": rows}, allow_unicode=True), encoding="utf-8")
    return base


class Test名单(unittest.TestCase):
    def test_只取有名字有区域的(self):
        """⭐ 平台岗（虚拟）和空名那两条**不是店** —— 天然被这个条件排掉。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = P.stores_in_scope(_root(tmp))
        self.assertEqual(got, ["青岛城阳万达店", "青岛悦荟店"])

    def test_按区域排_区内照名单(self):
        """⭐ 用户 2026-09-22：「也按区域排好序」—— 口径跟 film 的 REGION_ORDER 一致。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = P.stores_in_scope(_root(tmp, [
                {"erp_name": "B店", "region": "南区"},
                {"erp_name": "A店", "region": "市区"},
                {"erp_name": "C店", "region": "西北区"},
                {"erp_name": "D店", "region": "市区"},
            ]))
        # 西北区 → 市区 → 南区；市区内仍按名单（A 在 D 前）
        self.assertEqual(got, ["C店", "A店", "D店", "B店"])

    def test_区域顺序跟防护膜一致(self):
        """别两处各写一份 —— film.export.REGION_ORDER 改了这里要跟。"""
        from src.features.valueadd.film.export import REGION_ORDER as film_order
        self.assertEqual(P.REGION_ORDER, tuple(film_order))

    def test_读不到名单给空表(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(P.stores_in_scope(Path(tmp)), [])

    def test_真名单是_28_家(self):
        """⭐ 用户 2026-09-21：「我这 28 家店里没联想」—— 这个数就是那条口径。"""
        got = P.stores_in_scope(ROOT)
        self.assertEqual(len(got), 28, "名单变了？先确认是不是有意为之：%s" % got)
        self.assertNotIn("平台岗", got)
        # 名单外那 7 家联想专卖店，一家都不该在
        for n in got:
            self.assertNotIn("联想", n)


class Test取销售(unittest.TestCase):
    def test_基本取数(self):
        conn = _db([_row(qty="2", amount="12000", profit="1200")])
        sales, dropped = P.load_sales(conn, datetime.date(2026, 9, 1),
                                      datetime.date(2026, 9, 30))
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0].qty, 2)              # TEXT → int
        self.assertAlmostEqual(sales[0].amount, 12000.0)
        self.assertEqual(dropped, {})

    def test_名单外的店一条都不进_但要计数(self):
        """⭐ 联想专卖店那种 —— 不进计算，但必须报出来（不许静默丢）。"""
        conn = _db([_row(), _row(store="联想青岛万象城店"), _row(store="渠道分销部")])
        sales, dropped = P.load_sales(conn, datetime.date(2026, 9, 1),
                                      datetime.date(2026, 9, 30),
                                      stores=["青岛城阳万达店"])
        self.assertEqual(len(sales), 1)
        self.assertEqual(dropped, {"名单外的店": 2})

    def test_三类剔除(self):
        conn = _db([
            _row(),
            _row(kind="核销"),                                   # 用户定：不算
            _row(kind="客情单"),
            _row(product="手机/华为/Mate80 演示机"),               # 演示机
            _row(product="手机/华为/Mate80 体验机"),
            _row(mark="外调,新"),                                 # 外调
        ])
        sales, dropped = P.load_sales(conn, datetime.date(2026, 9, 1),
                                      datetime.date(2026, 9, 30))
        self.assertEqual(len(sales), 1)
        self.assertEqual(dropped["单据类型不计入"], 2)
        self.assertEqual(dropped["演示机/体验机"], 2)
        self.assertEqual(dropped["外调"], 1)

    def test_期间是左闭右闭(self):
        conn = _db([_row(when="2026-09-01 00:00:00"),
                    _row(when="2026-09-30 23:59:59"),
                    _row(when="2026-08-31 23:59:59"),
                    _row(when="2026-10-01 00:00:00")])
        sales, _d = P.load_sales(conn, datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
        self.assertEqual(len(sales), 2)

    def test_表不存在给_PlanError(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(P.PlanError):
            P.load_sales(conn, datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))

    def test_数据截至用库里的最大时间(self):
        """⭐ 抓数断了三天时写"截至 9/18"，不是"今天" —— 否则看着像"这三天没卖"。"""
        conn = _db([_row(when="2026-09-10 09:00:00"), _row(when="2026-09-18 20:00:00")])
        self.assertEqual(P.data_as_of(conn, datetime.date(2026, 9, 1),
                                      datetime.date(2026, 9, 30)), "2026-09-18")

    def test_覆盖天数(self):
        conn = _db([_row(when="2026-08-01 09:00:00"),
                    _row(when="2026-08-01 18:00:00"),
                    _row(when="2026-08-02 09:00:00")])
        self.assertEqual(P.covered_days(conn, M.Span(datetime.date(2026, 8, 1),
                                                     datetime.date(2026, 8, 21))), 2)


class Test算与落盘(unittest.TestCase):
    def _payload(self, tmp, rows, day=datetime.date(2026, 9, 21), stores=None):
        root = _root(tmp, stores)
        conn = _db(rows)
        return P.compute(conn, P.stores_in_scope(root), day=day)

    def test_两段各查各的(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = self._payload(tmp, [
                _row(when="2026-09-10 12:00:00", qty="3", amount="18000", profit="1800"),
                _row(when="2026-08-10 12:00:00", qty="2", amount="12000", profit="1200"),
                _row(when="2026-07-10 12:00:00", qty="99"),          # 两段都不该收
            ])
        self.assertEqual(d["period"]["cur"]["start"], "2026-09-01")
        self.assertEqual(d["period"]["cur"]["end"], "2026-09-21")
        self.assertEqual(d["period"]["prev"]["start"], "2026-08-01")
        self.assertEqual(d["period"]["prev"]["end"], "2026-08-21")
        self.assertEqual(d["counts"]["cur_rows"], 1)
        self.assertEqual(d["counts"]["prev_rows"], 1)
        fd = [b for b in d["rows"][0]["blocks"] if b["name"] == "FD"][0]
        self.assertEqual(fd["cur"]["qty"], 3)
        self.assertEqual(fd["prev"]["qty"], 2)
        self.assertEqual(fd["growth"]["qty"]["kind"], "up")

    def test_名单里每家店都在(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = self._payload(tmp, [_row()])
        self.assertEqual([r["store"] for r in d["rows"]], ["青岛城阳万达店", "青岛悦荟店"])
        self.assertEqual(d["rows"][1]["blocks"][0]["cur"]["qty"], 0)

    def test_认不出的词和不纳入的分开收(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = self._payload(tmp, [_row(cat2="Mate 100系列"), _row(cat2="联想Moto系列")])
        self.assertIn("手机 / Mate 100系列", d["unknown"])
        self.assertIn("联想Moto系列", d["skipped"])

    def test_上月不够就给_warning(self):
        """⭐ 用户纠正过"库是全月的" —— 这道体检只兜"整月没开机"那种。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = self._payload(tmp, [
                _row(when="2026-09-10 12:00:00"),
                _row(when="2026-08-10 12:00:00"),      # 上月只覆盖 1 天（应有 21 天）
            ])
        self.assertTrue(any("上月数据只覆盖" in w for w in d["warnings"]), d["warnings"])

    def test_本月数据落后也要说(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = self._payload(tmp, [_row(when="2026-09-10 12:00:00")],
                              day=datetime.date(2026, 9, 21))
        self.assertTrue(any("只到 2026-09-10" in w for w in d["warnings"]), d["warnings"])

    def test_落盘读回(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            payload = self._payload(tmp, [_row()])
            path = P.save(root, payload)
            self.assertEqual(path.name, "plan-2026.json")
            back = P.load(root)
        self.assertTrue(back["exists"])
        self.assertEqual(back["period"]["cur"]["end"], "2026-09-21")
        self.assertEqual(len(back["rows"]), 2)

    def test_没算过给_exists_False(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = P.load(Path(tmp))
        self.assertFalse(got["exists"])
        self.assertIn("还没算过", got["error"])

    def test_落盘不是先截断再写(self):
        """先写 `.tmp` 再 rename —— 半截 JSON 跟"还没算过"长得一样，分不出来。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            P.save(root, self._payload(tmp, [_row()]))
            self.assertEqual([p.name for p in (root / "out").glob("plan-*")],
                             ["plan-2026.json"])

    def test_run_端到端(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            db = Path(tmp) / "cbg-2026.db"
            src = _db([_row(qty="2", amount="12000", profit="1200")])
            dst = __import__("sqlite3").connect(str(db))
            src.backup(dst)
            dst.close()
            got = P.run(db=str(db), root=root, day=datetime.date(2026, 9, 21))
            self.assertTrue(got["ok"], got.get("why"))
            self.assertTrue(Path(got["path"]).is_file())
            loaded = P.load(root)
        self.assertEqual(loaded["summary"]["blocks"]["FD"]["cur"]["qty"], 2)

    def test_run_没库就说清楚(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = P.run(db=str(Path(tmp) / "nope.db"), root=_root(tmp))
        self.assertFalse(got["ok"])
        self.assertIn("没找到订单库", got["why"])

    def test_run_名单空就说清楚(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = P.run(root=_root(tmp, stores=[]))
        self.assertFalse(got["ok"])
        self.assertIn("门店名单读不出来", got["why"])


# ---------------------------------------------------------------- 拆分到人（2026-09-21）
class Test拆分到人(unittest.TestCase):
    """用户 2026-09-21：「**门店名点开可以拆分到人**」。

    ⚠ 最要紧的一条是**恒等式**：所有人的数加起来 = 店里那一行。
      它靠"同一个 `collect` + 同一个 `_blocks_of`"保证 —— 两份口径各算各的，
      迟早出现"人对不上店"，而那种错最难查（两边看着都合理）。
    """

    def _two(self):
        # ⚠ 系列名要用**映射表里认识**的（`Mate80系列`→FD、`Mate X7系列`→折叠机）——
        #   随手编一个（比如 `Pura X 系列`，注意中间那个空格）会被判"认不出的品类"、
        #   整行**不进累加表**，于是测试里"人不见了"而看不出原因。
        cur = [_sale(who="张三", amount=100.0, qty=1),
               _sale(who="李四", cat2="Mate X7系列", cat3="Mate X7", amount=50.0, qty=1),
               _sale(who="张三", cat2="Mate X7系列", cat3="Mate X7", amount=25.0, qty=1)]
        return cur

    def _acc(self, sales):
        return M.collect(sales, with_who=True)[0]

    def test_人的数加起来等于店里那一行(self):
        cur = self._two()
        prev = [_sale(who="张三", amount=80.0)]
        rows = M.tree(M.collect(cur)[0], M.collect(prev)[0], ["青岛城阳万达店"])
        ppl = M.people_of(self._acc(cur), self._acc(prev), ["青岛城阳万达店"])["青岛城阳万达店"]
        self.assertEqual(len(ppl), 2)
        store_blk = {b["name"]: b["cur"] for b in rows[0]["blocks"]}
        for b in M.BLOCKS:
            # ⚠ 取的是**那一块**的数（`x["cur"]`），不是这个人的总数（`p["cur"]`）——
            #   写成后者的话，每个人只卖一个块时看着也对，两个块就悄悄错了。
            got = sum(float(x["cur"]["amount"]) for p in ppl
                      for x in p["blocks"] if x["name"] == b)
            self.assertAlmostEqual(got, float(store_blk[b]["amount"]), places=6, msg=b)

    def test_人按本月金额从多到少(self):
        cur = self._two()
        ppl = M.people_of(self._acc(cur), {}, ["青岛城阳万达店"])["青岛城阳万达店"]
        self.assertEqual([p["name"] for p in ppl], ["张三", "李四"])   # 125 > 50

    def test_全零的人不占一行(self):
        # ⚠ 三个指标都要抵消掉（只抵 qty/amount、毛利还留着的话，这人**不是**全 0）
        cur = [_sale(who="退了", qty=1, amount=100.0, profit=10.0),
               _sale(who="退了", qty=-1, amount=-100.0, profit=-10.0)]
        ppl = M.people_of(self._acc(cur), {}, ["青岛城阳万达店"])["青岛城阳万达店"]
        self.assertEqual(ppl, [])

    def test_只有上月有数的人也留着(self):
        """他会显示成 ↓100% —— 那正是要看见的（人走了 / 这个月没开单）。"""
        prev = [_sale(who="老王", amount=999.0)]
        ppl = M.people_of({}, self._acc(prev), ["青岛城阳万达店"])["青岛城阳万达店"]
        self.assertEqual([p["name"] for p in ppl], ["老王"])
        self.assertAlmostEqual(float(ppl[0]["cur"]["amount"]), 0.0)

    def test_没写店员的行归到一个兜底名字下(self):
        ppl = M.people_of(self._acc([_sale(who="", amount=10.0)]), {},
                          ["青岛城阳万达店"])["青岛城阳万达店"]
        self.assertEqual([p["name"] for p in ppl], ["（没写店员）"])

    def test_键多一层但数一样(self):
        cur = self._two()
        a4 = M.collect(cur)[0]
        a5 = self._acc(cur)
        self.assertEqual({len(k) for k in a5}, {5})
        self.assertEqual({len(k) for k in a4}, {4})
        self.assertAlmostEqual(sum(t.amount for t in a4.values()),
                               sum(t.amount for t in a5.values()))


class Test老库没有店员列(unittest.TestCase):
    """⚠ `erp_sales` 的列是 `ensure_columns()` **按云商返回动态建的** ——
    老库可能没有 `店员`。直接 `SELECT 店员` 会让**整个 plan 步骤**报
    "no such column"，而这一页只是少个"拆到人"。"""

    def test_没有那一列也照跑_只是没人(self):
        conn = _db([_row()])                      # `_SCHEMA` 里**故意没有** 店员
        sales, _drop = P.load_sales(conn, datetime.date(2026, 9, 1),
                                    datetime.date(2026, 9, 30))
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0].who, "")

    def test_SQL_按实际列拼(self):
        conn = _db([_row()])
        self.assertIn("'' AS 店员", P.sales_sql(conn))
        conn2 = sqlite3.connect(":memory:")
        conn2.execute(_SCHEMA.replace("串号标识 TEXT,", "串号标识 TEXT, 店员 TEXT,"))
        self.assertNotIn("'' AS 店员", P.sales_sql(conn2))


# ------------------------------------------------------------------ 导出摆表
class Test导出三张表(unittest.TestCase):
    """⭐ 用户 2026-09-21 晚：「**导出的excel里销量、销售额、利润是三个sheet分开，
    不用一级分类，用二级分类就行**」。

    ⚠ 这一份测的是**摆表**（`features/plan/monthly/export.py` 的纯函数部分），
      落盘 / 文件格式在 `test_export.py` 那一套里。
    ⚠ 两条最要紧的：① 列**不是**一级分类（七个大块一个都不许当列名）；
      ② 合计那三格 == 这一行所有二级分类之和（不然"列加起来 ≠ 合计"没人看得出来）。
    """

    def _d(self, rows=None, stores=None):
        import tempfile
        from src.features.plan.monthly import export as E
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Test算与落盘("test_两段各查各的")
        return E, t._payload(tmp.name, rows or [
            _row(cat2="Mate80系列", cat3="Mate80", qty="3", amount="18000", profit="1800"),
            _row(cat2="Mate80系列", cat3="Mate80 Pro", qty="1", amount="7000", profit="700"),
            _row(cat1="笔记本", cat2="华为笔记本", cat3="MateBook X",
                 qty="2", amount="14000", profit="800"),
        ], stores=stores)

    def test_三张表的表名就是三个指标(self):
        """⭐ 用户：「**销量、销售额、利润是三个sheet分开**」—— 表名就是指标名。"""
        E, d = self._d()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            r = E.export(tmp, d, who="测试")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sheets"], ["销量", "销售额", "利润", "说明"])

    def test_列是二级分类_不是一级分类(self):
        E, d = self._d()
        head, _rows, _fmt = E.metric_sheet(d, "qty")
        names = [h for h in head[0] if h]
        self.assertIn("Mate80系列", names)
        self.assertIn("华为笔记本", names)
        for blk in M.BLOCKS:                       # 七个大块一个都不许当列名
            self.assertNotIn(blk, names, "又用一级分类当列了：%s" % blk)

    def test_表头两行_每个系列横跨三格(self):
        E, d = self._d()
        head, _rows, _fmt = E.metric_sheet(d, "qty")
        self.assertEqual(len(head), 2)
        self.assertEqual(head[1][:2], ["", ""])            # 门店 / 区域 跨两行
        i = head[0].index("Mate80系列")
        self.assertEqual(head[0][i + 1:i + 3], [None, None], "系列名要横跨它那三格")
        self.assertEqual(head[1][i:i + 3], ["本月", "上月同期", "环比"])
        self.assertEqual(head[0][-3:], ["合计", None, None])
        self.assertEqual(head[1][-3:], ["本月", "上月同期", "环比"])

    def test_合计那三格等于这一行所有系列之和(self):
        E, d = self._d()
        head, rows, _fmt = E.metric_sheet(d, "amount")
        cur_idx = [i for i, s in enumerate(head[1]) if s == "本月"]
        store_row = rows[0]
        series_sum = sum(float(store_row[i] or 0) for i in cur_idx[:-1])
        self.assertAlmostEqual(series_sum, float(store_row[cur_idx[-1]] or 0), places=6)

    def test_合计行只在多于一家店时才加(self):
        E, d = self._d()
        _h, rows, _f = E.metric_sheet(d, "qty")
        self.assertEqual(rows[-1][0], "合计", "两家店以上要有一行合计")
        # 只有一家店（门店账号导出的那种）⇒ 不加那一行，省得跟门店那行重复
        one = {"rows": [d["rows"][0]], "blocks": d["blocks"], "regions": d.get("regions")}
        _h2, rows2, _f2 = E.metric_sheet(one, "qty")
        self.assertEqual([r[0] for r in rows2], [d["rows"][0]["store"]])

    def test_环比是百分比列_没百分比就留空(self):
        E, d = self._d()
        head, rows, fmt = E.metric_sheet(d, "qty")
        for i, span in enumerate(head[1]):
            if span == "环比":
                self.assertEqual(fmt.get(i + 1), E.RATE_FMT, "列号是从 1 起算的")
        idx = [i for i, s in enumerate(head[1]) if s == "环比"]
        self.assertTrue(any(rows[0][i] is None for i in idx),
                        "上月是 0 ⇒ 环比必须留空，不许写 0")

    def test_列跨门店取并集(self):
        """有的店卖过某个系列、有的没卖过 —— 列要按全集铺开，不然各店的列对不上。"""
        import tempfile
        from src.features.plan.monthly import export as E
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Test算与落盘("test_两段各查各的")
        d = t._payload(tmp.name, [
            _row(store="青岛城阳万达店", cat2="Mate80系列", qty="1"),
            _row(store="青岛悦荟店", cat2="Mate70系列", qty="1"),
        ])
        _h, rows, _f = E.metric_sheet(d, "qty")
        by_store = {r[0]: r for r in rows}
        i80 = 2 + 3 * [n for _b, n in E.series_order(d)].index("Mate80系列")
        self.assertEqual(by_store["青岛城阳万达店"][i80], 1)
        self.assertEqual(by_store["青岛悦荟店"][i80], 0, "没卖过的店是 0，不是缺列")

    def test_二级分类重名就带上一级名(self):
        """⚠ 列名直接就是二级分类 ⇒ **重名的话两列同名**（看着一样、加的时候却分开加）。

        ⚠ 二级分类只在**它自己那一级**里唯一：同一个词挂两个一级分类是可能的
          （全库实测：`延保服务` 在 手机平板周边/电脑周边、`支架` 在外购散件/电脑周边 ——
          那两个一级分类本来就不参与统计，所以这份表里现在没有重名）。
          这里用"穿戴/音频 都卖耳机"造一个重名，钉住"必须带上块名"。
        """
        cur, _u, _s = M.collect([
            _sale(cat1="音频产品", cat2="耳机", cat3="FreeBuds"),
            _sale(cat1="智能穿戴", cat2="耳机", cat3="FreeBuds"),
        ])
        d = {"rows": M.tree(cur, {}, ["青岛城阳万达店"]),
             "blocks": list(M.BLOCKS), "regions": {}}
        labels = E_mod.column_labels(E_mod.series_order(d))
        self.assertEqual(sorted(labels), ["穿戴·耳机", "音频·耳机"])

    def test_说明里留了二级到一级的对应(self):
        """一级分类不占列，但**不能把这个信息丢了** —— 写进说明表。"""
        E, d = self._d()
        meta = E.meta_of(d)
        self.assertIn("二级分类 → 一级分类", meta)
        self.assertIn("Mate80系列", meta["二级分类 → 一级分类"])
        self.assertIn("FD", meta["二级分类 → 一级分类"])
