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


class Test指纹缓存(unittest.TestCase):
    def test_指纹没变就回缓存_变了就重算(self):
        film_compute._CACHE.clear()
        t0 = time.time()
        d1 = film_compute.load(force=True)
        t1 = time.time()
        self.assertTrue(d1.get("ok"))
        d2 = film_compute.load()
        t2 = time.time()
        self.assertIs(d1, d2, "指纹没变应直接回同一份结果")
        self.assertLess(t2 - t1, 0.05, "缓存命中不该重算")
        self.assertGreater(t1 - t0, 0.01, "第一次至少要真算")

    def test_force_不吃缓存(self):
        film_compute._CACHE.clear()
        film_compute.load(force=True)
        d = film_compute.load(force=True)
        self.assertTrue(d.get("ok"))
        self.assertIn("summary", d)
        blob = str(d.get("source") or "") + str(d.get("note") or "")
        self.assertIn("erp_sales", blob)


class _FilmDbCase(unittest.TestCase):
    """临时库 + monkeypatch `find_db`。"""

    def _db(self, rows, with_clerk=True):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = sqlite3.connect(path)
        if with_clerk:
            conn.execute(
                "CREATE TABLE erp_sales (门店 TEXT, 单据类型 TEXT, 数量 REAL,"
                " 零售考核毛利 REAL, 一级分类 TEXT, 二级分类 TEXT, 商品名称 TEXT,"
                " 备注 TEXT, 单行备注 TEXT, \"客户/顾客\" TEXT, 付款方式 TEXT,"
                " 支付时间 TEXT, 店员 TEXT)")
        else:
            conn.execute(
                "CREATE TABLE erp_sales (门店 TEXT, 单据类型 TEXT, 数量 REAL,"
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
        base = {"门店": "甲店", "店员": "张三", "单据类型": "零售", "数量": 1,
                "零售考核毛利": 0, "一级分类": "手机", "二级分类": "手机",
                "商品名称": "P", "备注": "", "单行备注": "", "客户/顾客": "",
                "付款方式": "", "支付时间": "2026-09-10 10:00:00"}
        base.update(over)
        return base


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


if __name__ == "__main__":
    unittest.main()
