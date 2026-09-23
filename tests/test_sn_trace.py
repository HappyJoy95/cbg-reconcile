# -*- coding: utf-8 -*-
"""串号全程追踪 —— 86 码 / SN → 库存快照 + 销售记录 + 反查真 SN。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.features import registry
from src.features.tools.sn_trace import trace


def _mk_db(td: str) -> Path:
    # `_find_db(root)` 认的是 `root/out/cbg-<年>.db`
    out = Path(td) / "out"
    out.mkdir(parents=True, exist_ok=True)
    db = out / "cbg-2026.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE erp_stock (
            snapshot_date TEXT, sn TEXT, imei TEXT, sub_imei TEXT, sub_imei1 TEXT,
            pro_name TEXT, store_name TEXT, status TEXT);
        CREATE TABLE erp_sales (
            sn TEXT, document_no TEXT, 单号 TEXT, 单据类型 TEXT,
            商品名称 TEXT, 支付时间 TEXT, 门店 TEXT, 店员 TEXT, 串号标识 TEXT,
            串号 TEXT, 串号2 TEXT, 串号3 TEXT, 一级分类 TEXT, 备注 TEXT, 单行备注 TEXT);
    """)
    # 手机：imei=86码、sub_imei=真 SN
    conn.execute(
        "INSERT INTO erp_stock VALUES (?,?,?,?,?,?,?,?)",
        ("2026-09-20", "864468081285466", "864468081285466",
         "7DZ9K26508003757", None,
         "智能手机/华为/nova 16 Pro …", "青岛顺和汇库", "在库"))
    conn.execute(
        "INSERT INTO erp_stock VALUES (?,?,?,?,?,?,?,?)",
        ("2026-09-23", "OTHER000000000001", "OTHER000000000001",
         None, None, "别的机器", "别仓", "在库"))
    # 销售：主串号 86 码，副列有真 SN
    conn.execute(
        "INSERT INTO erp_sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("864468081285466", "D1", "D1", "零售",
         "智能手机/华为/nova 16 Pro CRS-AL00 …",
         "2026-09-19 16:24:58", "青岛顺和汇店", "申杰", "售后换回,新",
         "864468081285466", "864468081299467", "7DZ9K26508003757",
         "手机", "", ""))
    # 别家店的销售（身份滤店时不该出现在明细里）
    conn.execute(
        "INSERT INTO erp_sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("7DZ9K26508003757", "D2", "D2", "零售",
         "同款", "2026-09-18 10:00:00", "别家店", "张三", "Y,新",
         "7DZ9K26508003757", "", "", "手机", "", ""))
    conn.commit()
    conn.close()
    return db


class Test追踪口径(unittest.TestCase):
    def test_86码反查真SN并串起库存销售(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_db(td)
            d = trace.collect(Path(td), "864468081285466")
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["query_kind"], "imei")
        self.assertEqual(d["sn"], "7DZ9K26508003757")
        self.assertIn("864468081285466", d["imeis"])
        self.assertTrue(d["stock"], "应命中库存快照")
        self.assertTrue(d["sales"], "应命中销售明细")
        self.assertEqual(d["sales"][0]["store"], "青岛顺和汇店")

    def test_输入真SN也能串回86码(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_db(td)
            d = trace.collect(Path(td), "7DZ9K26508003757")
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["query_kind"], "sn")
        self.assertEqual(d["sn"], "7DZ9K26508003757")
        self.assertIn("864468081285466", d["related"] + d["imeis"])
        self.assertTrue(d["stock"] or d["sales"])

    def test_身份滤店_别家销售明细不展示但码仍关联(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_db(td)
            d = trace.collect(Path(td), "7DZ9K26508003757",
                              stores={"青岛顺和汇店"})
        self.assertTrue(d["ok"], d)
        stores = {r.get("store") for r in d["sales"]}
        self.assertNotIn("别家店", stores)

    def test_空输入与缺库(self):
        self.assertFalse(trace.collect(Path("."), "  ")["ok"])
        with tempfile.TemporaryDirectory() as td:
            d = trace.collect(Path(td), "864468081285466")
            self.assertFalse(d["ok"])
            self.assertIn("抓取", d["why"])

    def test_两轮扩散_销售副列SN再回库存(self):
        """销售副列里的 SN 应能扩到库存里以 SN 为主键的行。"""
        with tempfile.TemporaryDirectory() as td:
            db = _mk_db(td)
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO erp_stock VALUES (?,?,?,?,?,?,?,?)",
                ("2026-09-21", "7DZ9K26508003757", "7DZ9K26508003757",
                 None, None, "同机 SN 行", "青岛顺和汇库", "在库"))
            conn.commit()
            conn.close()
            d = trace.collect(Path(td), "864468081285466")
        self.assertTrue(d["ok"], d)
        dates = {r["date"] for r in d["stock"]}
        self.assertIn("2026-09-20", dates)
        self.assertIn("2026-09-21", dates)


class Test注册挂到小工具(unittest.TestCase):
    def test_tools_children_含串号追踪(self):
        f = [x for x in registry.all_features() if x.key == "tools"][0]
        keys = [c.key for c in f.children]
        self.assertIn("sn-trace", keys)

    def test_没有步骤(self):
        self.assertNotIn("sn-trace", registry.steps())
        self.assertEqual(registry.validate(), [])

    def test_导航与加载器接线(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        js = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-subtab="sn-trace"', html)
        self.assertIn('id="subpanel-sn-trace"', html)
        sub_block = js[js.index("const SUBTABS"):js.index("const SUBTAB_LOADERS")]
        self.assertIn("'sn-trace'", sub_block)
        load_block = js[js.index("const SUBTAB_LOADERS"):]
        load_block = load_block[:load_block.index("};")]
        self.assertIn("sn-trace", load_block)


if __name__ == "__main__":
    unittest.main()
