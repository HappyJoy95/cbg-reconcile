# -*- coding: utf-8 -*-
"""防护膜达成 —— 只读 `erp_sales`（SQLite）的计算与缓存。

⚠ 业务数**只认 fetch 落的库**，不扫销售 xlsx、不读用户桌面 Excel（2026-09-22）。
"""

from __future__ import annotations

import datetime
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.features.valueadd.film import compute as film_compute
from src.features.valueadd.film import metric as film_metric
from src.paths import ROOT


class Test只读sqlite(unittest.TestCase):
    def test_不再有xlsx回落入口(self):
        self.assertFalse(hasattr(film_compute, "_load_rows_xlsx"))
        self.assertFalse(hasattr(film_compute, "_sales_files"))
        src = open(film_compute.__file__, encoding="utf-8").read()
        self.assertNotIn("_load_rows_xlsx(", src)
        self.assertNotIn("load_workbook", src)
        self.assertNotIn("Desktop", src)

    def test_指纹只看库(self):
        fp = film_compute._fp(ROOT, "2026-09-01", "2026-09-30")
        flat = str(fp)
        self.assertNotIn("xlsx", flat)
        self.assertIn("db", flat)


class _FilmDbCase(unittest.TestCase):
    """临时库 + monkeypatch `find_db`。"""

    def _db(self, rows, with_clerk=True):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = sqlite3.connect(path)
        # ⚠ `单号` 在列里 —— 明细下钻要列销售单号（老库缺列走 `'' AS 单号` 兜底）
        if with_clerk:
            conn.execute(
                "CREATE TABLE erp_sales (单号 TEXT, 门店 TEXT, 单据类型 TEXT, 数量 REAL,"
                " 零售考核毛利 REAL, 一级分类 TEXT, 二级分类 TEXT, 商品名称 TEXT,"
                " 备注 TEXT, 单行备注 TEXT, \"客户/顾客\" TEXT, 付款方式 TEXT,"
                " 支付时间 TEXT, 店员 TEXT)")
        else:
            conn.execute(
                "CREATE TABLE erp_sales (单号 TEXT, 门店 TEXT, 单据类型 TEXT, 数量 REAL,"
                " 零售考核毛利 REAL, 一级分类 TEXT, 二级分类 TEXT, 商品名称 TEXT,"
                " 备注 TEXT, 单行备注 TEXT, \"客户/顾客\" TEXT, 付款方式 TEXT,"
                " 支付时间 TEXT)")
        for r in rows:
            keys = list(r.keys())
            # ⚠ `客户/顾客` 含 `/`，不加引号 INSERT 会语法错
            cols = ", ".join('"%s"' % k if "/" in k else k for k in keys)
            ph = ",".join("?" for _ in keys)
            conn.execute(
                "INSERT INTO erp_sales (%s) VALUES (%s)" % (cols, ph),
                [r[k] for k in keys])
        conn.commit()
        conn.close()
        return path

    def _patch(self, path):
        self._orig = film_compute.find_db
        film_compute.find_db = lambda root=None: path
        film_compute._CACHE.clear()
        self.addCleanup(self._restore)
        self.addCleanup(film_compute._CACHE.clear)

    def _restore(self):
        film_compute.find_db = self._orig

    @staticmethod
    def _sale(**over):
        base = {"单号": "", "门店": "甲店", "店员": "张三", "单据类型": "零售", "数量": 1,
                "零售考核毛利": 0, "一级分类": "手机", "二级分类": "手机",
                "商品名称": "P", "备注": "", "单行备注": "", "客户/顾客": "",
                "付款方式": "", "支付时间": "2026-09-10 10:00:00"}
        base.update(over)
        return base


class Test指纹缓存(_FilmDbCase):
    """⚠ **必须用临时库**（原来直接读本机 `out/cbg-*.db`）——
    干净 checkout 里那个文件根本不存在（2026-10-02 实测：CI 一跑就红两条，
    而本机恰好有库时又是绿的 —— 时红时绿的测试比没有测试更糟）。"""

    def test_指纹没变就回缓存_变了就重算(self):
        self._patch(self._db([self._sale(门店="甲店", 数量=3)]))
        film_compute._CACHE.clear()
        d1 = film_compute.load(force=True)
        self.assertTrue(d1.get("ok"), d1.get("why"))
        self.assertEqual(len(film_compute._CACHE), 1, "算完该进缓存")

        d2 = film_compute.load()
        # ⚠ 不用"耗时"当判据：临时库只有几行，重算可能比 1ms 还快，
        #   阈值断言会随机器快慢乱红。`assertIs` 是**确定性**的 ——
        #   重算必然产生新对象，回缓存才是同一个对象。
        self.assertIs(d1, d2, "指纹没变应直接回同一份结果")

    def test_force_不吃缓存(self):
        self._patch(self._db([self._sale(门店="甲店", 数量=3)]))
        film_compute.load(force=True)
        d = film_compute.load(force=True)
        self.assertTrue(d.get("ok"), d.get("why"))
        self.assertIn("summary", d)
        blob = str(d.get("source") or "") + str(d.get("note") or "")
        self.assertIn("erp_sales", blob)


class Test拆到人(_FilmDbCase):
    """点门店拆到人：同一公式 · 人加总 = 店 · 按新机降序。"""

    def test_人加总等于店_同一公式(self):
        rows = [
            self._sale(门店="甲店", 店员="张三", 数量=4, 一级分类="手机"),
            self._sale(门店="甲店", 店员="张三", 数量=2, 零售考核毛利=80,
                       一级分类="配件", 二级分类="贴膜", 商品名称="钢化膜",
                       支付时间="2026-09-10 10:01:00"),
            self._sale(门店="甲店", 店员="李四", 数量=6, 一级分类="手机",
                       支付时间="2026-09-10 11:00:00"),
            self._sale(门店="甲店", 店员="李四", 数量=3, 零售考核毛利=120,
                       一级分类="配件", 二级分类="贴膜", 商品名称="钢化膜",
                       支付时间="2026-09-10 11:01:00"),
        ]
        path = self._db(rows)
        self._patch(path)
        d = film_compute.load(root=ROOT, stores=["甲店"],
                              day=datetime.date(2026, 9, 15), force=True)
        self.assertTrue(d.get("ok"), d)
        store = d["rows"][0]
        ppl = store["people"]
        self.assertEqual([p["name"] for p in ppl], ["李四", "张三"],
                         "按个人新机降序")
        self.assertAlmostEqual(sum(p["new"] for p in ppl), store["new"], places=6)
        self.assertAlmostEqual(sum(p["done"] for p in ppl), store["done"], places=6)
        self.assertAlmostEqual(sum(p["target"] for p in ppl), store["target"],
                               places=6)
        for p in ppl:
            self.assertAlmostEqual(p["target"], p["new"] / 2.0, places=6)
            self.assertEqual(p["baseline"], store["baseline"],
                             "人吃本店台均基线")

    def test_没写店员_兜底显示(self):
        rows = [self._sale(门店="乙店", 店员=None, 数量=2)]
        path = self._db(rows)
        self._patch(path)
        d = film_compute.load(root=ROOT, stores=["乙店"],
                              day=datetime.date(2026, 9, 15), force=True)
        ppl = d["rows"][0]["people"]
        self.assertEqual([p["name"] for p in ppl], ["（没写店员）"])

    def test_老库没有店员列不炸(self):
        rows = [self._sale(门店="丙店", 数量=1)]
        rows[0].pop("店员")
        path = self._db(rows, with_clerk=False)
        self._patch(path)
        d = film_compute.load(root=ROOT, stores=["丙店"],
                              day=datetime.date(2026, 9, 15), force=True)
        self.assertTrue(d.get("ok"), d)
        self.assertEqual([p["name"] for p in d["rows"][0]["people"]],
                         ["（没写店员）"])


class Test明细下钻(_FilmDbCase):
    """点门店行上**每个能下钻的数字**弹出来的明细 —— 合计必须等于页面那个数。

    用户 2026-09-29：「每个门店新机销量那个数字，点开要有纳入统计的门店对应的
    销售单号、商品名称和数量，以及销售时间，包含退货。」
    同日追加：「几个具体达成的，点开也显示一下对应的销售单据？比如钢化膜的展示
    钢化膜的销售单」+「金额类也一起做」+「带上毛利吧」。
    ⚠ 最要紧的一条：明细和页面**共用 `row_kind()`** —— 判据写两份就会出现
      「页面 3.6、明细加出来 4」，而这种不一致没人报、只会被业务抓包。
    """

    def test_明细合计乘折算等于页面新机(self):
        day = datetime.date(2026, 9, 15)
        rows = [
            self._sale(单号="A1", 数量=3, 支付时间="2026-09-10 10:00:00"),   # 零售 +3
            self._sale(单号="R1", 单据类型="零售退", 数量=-1,
                       支付时间="2026-09-11 11:00:00"),                      # 退货 −1
            self._sale(单号="M1", 单据类型="分销", 数量=2, 备注="美团线上下单",
                       支付时间="2026-09-12 12:00:00"),                      # 美团 +2
            self._sale(单号="J1", 单据类型="分销", 数量=5, 备注="9.13京东国补订单",
                       支付时间="2026-09-13 13:00:00"),                      # 京东不算
            self._sale(单号="F1", 一级分类="配件", 二级分类="贴膜",
                       商品名称="钢化膜", 支付时间="2026-09-14 14:00:00"),   # 贴膜不算
        ]
        self._patch(self._db(rows))
        d = film_compute.load(root=ROOT, stores=["甲店"], day=day, force=True)
        det = film_compute.drill_rows(root=ROOT, store="甲店", kind="new", day=day)
        st = d["rows"][0]
        self.assertAlmostEqual(det["total"] * det["factor"], st["new"], places=6,
                               msg="明细合计 ×0.9 必须等于页面「新机销售」")
        self.assertAlmostEqual(det["shown"], st["new"], places=6)
        self.assertEqual(det["factor"], film_metric.NEW_FACTOR)
        nos = {r["no"] for r in det["rows"]}
        self.assertEqual(nos, {"A1", "R1", "M1"},
                         "京东分销 / 贴膜不该出现在明细里（页面也没算它们）")
        ret = [r for r in det["rows"] if r["no"] == "R1"][0]
        self.assertEqual(ret["qty"], -1, "退货行要进来（负数），合计里已冲减")
        self.assertEqual(ret["typ"], "零售退", "单据类型让退货一眼看得见")
        self.assertEqual(ret["ts"], "2026-09-11 11:00:00")
        self.assertTrue(ret["name"], "商品名称要带上")

    def test_只回本店的单(self):
        day = datetime.date(2026, 9, 15)
        rows = [self._sale(门店="甲店", 单号="A1"),
                self._sale(门店="乙店", 单号="B1")]
        self._patch(self._db(rows))
        a = film_compute.drill_rows(root=ROOT, store="甲店", kind="new", day=day)
        b = film_compute.drill_rows(root=ROOT, store="乙店", kind="new", day=day)
        self.assertEqual({r["no"] for r in a["rows"]}, {"A1"})
        self.assertEqual({r["no"] for r in b["rows"]}, {"B1"},
                         "查乙店只能拿到乙店的单 —— 别把别家的销售单摊出来")

    def test_老库没有单号列不炸(self):
        """老库缺 `单号` → `'' AS 单号` 兜底，别让整条接口 `no such column`。"""
        day = datetime.date(2026, 9, 15)
        rows = [self._sale(数量=2)]
        rows[0].pop("单号")
        self._patch(self._db(rows))
        det = film_compute.drill_rows(root=ROOT, store="甲店", kind="new", day=day)
        self.assertTrue(det["ok"], det)
        self.assertEqual(len(det["rows"]), 1)
        self.assertEqual(det["rows"][0]["no"], "")

    def test_没给门店就报错不是空成功(self):
        self._patch(self._db([self._sale()]))
        det = film_compute.drill_rows(root=ROOT, store="", kind="new", day=None)
        self.assertFalse(det.get("ok"))
        self.assertIn("门店", det.get("why") or "")

    def test_不认识的指标回错不是空成功(self):
        """`kind` 打错 → 400 那条路的源头：**别默默回一张空表**。"""
        self._patch(self._db([self._sale()]))
        det = film_compute.drill_rows(root=ROOT, store="甲店", kind="nope")
        self.assertFalse(det.get("ok"))
        self.assertIn("指标", det.get("why") or "")

    def test_贴膜与礼包那些达成的明细也对得上(self):
        """用户 2026-09-29：「几个具体达成的，点开也显示一下对应的销售单据？
        比如钢化膜的展示钢化膜的销售单」+「金额类也一起做」。

        ⚠ 一个 fixture 同时验**台数类**（达成 / 礼包达成）和**金额类**
          （三份毛利）：金额类跟台数类常常是同一批行换个取值字段，
          所以判据必须还是那一份 `row_kind()`。
        """
        day = datetime.date(2026, 9, 15)
        rows = [
            self._sale(单号="M1", 一级分类="配件", 二级分类="贴膜",
                       商品名称="手机贴膜/华为/钢化膜", 数量=2, 零售考核毛利=40,
                       支付时间="2026-09-10 10:00:00"),                # 贴膜 +2
            self._sale(单号="M2", 一级分类="配件", 二级分类="贴膜",
                       商品名称=film_metric.SOFT, 数量=3, 零售考核毛利=30,
                       支付时间="2026-09-10 10:01:00"),                # 软膜 —— 页面剔
            self._sale(单号="MR", 单据类型="零售退", 一级分类="配件",
                       二级分类="贴膜", 商品名称="手机贴膜/华为/钢化膜",
                       数量=-1, 零售考核毛利=-20,
                       支付时间="2026-09-11 11:00:00"),                # 贴膜退货
            self._sale(单号="G1", 一级分类="配件", 二级分类="礼包",
                       商品名称="99元光固电镀膜礼包", 数量=1, 零售考核毛利=10,
                       支付时间="2026-09-12 12:00:00"),                # 礼包 +1
            self._sale(单号="A1", 支付时间="2026-09-13 13:00:00"),     # 新机，不该混进来
        ]
        self._patch(self._db(rows))
        d = film_compute.load(root=ROOT, stores=["甲店"], day=day, force=True)
        st = d["rows"][0]
        cases = [
            ("film", st["done"], {"M1", "MR"}),
            ("gift", st["gift_done"], {"G1"}),
            ("film_profit", st["film_profit"], {"M1", "MR"}),
            ("gift_profit", st["gift_profit"], {"G1"}),
            ("total_profit", st["total_profit"], {"M1", "MR", "G1"}),
        ]
        for kind, want, nos in cases:
            det = film_compute.drill_rows(root=ROOT, store="甲店",
                                          kind=kind, day=day)
            self.assertTrue(det["ok"], (kind, det))
            self.assertAlmostEqual(det["total"], want, places=6,
                                   msg="kind=%s 的合计要等于页面那一格" % kind)
            self.assertEqual({r["no"] for r in det["rows"]}, nos, kind)
            for r in det["rows"]:
                self.assertIn("profit", r, "明细行要带毛利列（用户：带上毛利吧）")
                self.assertIn("qty", r)
        self.assertAlmostEqual(st["done"], 1, places=6, msg="软膜剔掉、退货冲减 = 1")
        self.assertAlmostEqual(st["total_profit"], 30, places=6,
                               msg="40−20（贴膜）+10（礼包）= 30")

    def test_贴膜明细只回贴膜行(self):
        """钢化膜的展示钢化膜的销售单 —— 别把新机/礼包混进来。"""
        day = datetime.date(2026, 9, 15)
        rows = [
            self._sale(单号="M1", 一级分类="配件", 二级分类="贴膜",
                       商品名称="手机贴膜/华为/钢化膜"),
            self._sale(单号="A1", 支付时间="2026-09-10 11:00:00"),
        ]
        self._patch(self._db(rows))
        det = film_compute.drill_rows(root=ROOT, store="甲店", kind="film", day=day)
        self.assertEqual({r["no"] for r in det["rows"]}, {"M1"})
        self.assertEqual(det["label"], "达成（贴膜）")
        self.assertEqual(det["unit"], "台")
        self.assertEqual(det["field"], "qty")


class Test口径(unittest.TestCase):
    def test_row_name可选(self):
        a = film_metric.row("甲店", 4, 0, 2, 80, 0, 0)
        b = film_metric.row("甲店", 4, 0, 2, 80, 0, 0, name="张三")
        self.assertNotIn("name", a)
        self.assertEqual(b["name"], "张三")
        for k in a:
            if k == "store":
                continue
            self.assertEqual(a[k], b[k])


class Test前端拆到人(unittest.TestCase):
    """点门店名拆到人 —— 钉住属性直接拼（别用 replace 补，坑 12）。"""

    APP = open("web/app.js", encoding="utf-8").read()
    HTML = open("web/index.html", encoding="utf-8").read()
    CSS = open("web/style.css", encoding="utf-8").read()

    def test_门店格属性直接拼不是replace(self):
        i = self.APP.index("function bodyTr(")
        blk = self.APP[i:i + 900]
        self.assertIn('data-film-store="', blk)
        self.assertIn("film-store-open", blk)
        # ⚠ 不能是「先拼好再 replace 换属性」那种写法
        self.assertNotIn(".replace('<td", blk)

    def test_点击委托和人行class(self):
        self.assertIn("data-film-store", self.APP)
        self.assertIn("film-person", self.APP)
        self.assertIn("filmOpenStores", self.APP)
        self.assertIn(".film-store-open", self.CSS)
        self.assertIn("tr.film-person", self.CSS)
        # 人行率类标色：必须显式写 background，否则被 `.film-person td` 灰底盖住
        self.assertIn("tr.film-person td.film-lo", self.CSS)
        self.assertIn("tr.film-person td.film-hi", self.CSS)

    def test_说明里写了拆到人(self):
        self.assertIn("点门店名", self.HTML)
        self.assertIn("拆到个人", self.HTML)


class Test日期窗口(unittest.TestCase):
    """日期窗口（2026-09-29 用户：「加个日期选择窗口吧，每个月 1 号可以手动
    拉取上个月全月数据」）。

    形态 = **月份 + 该月内截止日**（不跨月）；窗口 = 该月 1 号 ～ 这天；
    时间进度 = 已过天数 ÷ 30，**封顶 100%**（旧式 `(day-1)/30` 到 30 号只有 96.7%）。
    """

    def test窗口是该月1号到截止日(self):
        self.assertEqual(film_compute.month_window(datetime.date(2026, 8, 31)),
                         ("2026-08-01", "2026-08-31"))
        self.assertEqual(film_compute.month_window(datetime.date(2026, 9, 28)),
                         ("2026-09-01", "2026-09-28"))

    def test时间进度按已过天数且封顶(self):
        self.assertAlmostEqual(film_compute.time_progress(datetime.date(2026, 9, 28)),
                               28 / 30.0, places=6,
                               msg="截止 28 日 = 93.3%（源表右上角实写 93.3%）")
        self.assertAlmostEqual(film_compute.time_progress(datetime.date(2026, 9, 30)),
                               1.0, places=6, msg="30 号要 100%，旧式永远 96.7%")
        self.assertAlmostEqual(film_compute.time_progress(datetime.date(2026, 8, 31)),
                               1.0, places=6, msg="上月最后一天 = 100%，不能 31/30")


if __name__ == "__main__":
    unittest.main()
