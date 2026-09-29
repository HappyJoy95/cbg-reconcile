# -*- coding: utf-8 -*-
"""识别「备注点名别家门店」的行（转单/转线上）—— 判据 + 两页接入。

⚠ **归属口径 2026-09-29 翻了**：这些行**算转出店**（行上的门店），不再剔。
用户：「算转出店的，因为这个是转出店没有这个线上平台，通过别的店走的量，
**增值业务肯定要算是原门店的销售**」。
（历史：2026-09-26 拍的是「整行剔」，麦凯乐 40 vs 手算 39 那台引出的通用判据；
 判据本身一条没改，改的是**调用处**——从 continue 变成记台账。）

三层钉：
* `foreign.py` 判据单测（短名 / 自己不算 / 撞名放过 / 平台岗不参与）—— **保持原样**；
* **权益页集成**：临时库 + monkeypatch `find_db`（与 film 夹具同套路）——
  转单行必须**计入本店**（店行、人行、利润都算），note 报出台账；
* **防护膜页集成**：同源同口径，两页一台都不能少。
"""

from __future__ import annotations

import datetime
import datetime
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.features.valueadd import foreign
from src.paths import ROOT

#: 真实名单里的两家店（短名 丽达茂 / 麦凯乐 是本次事发那对）
MAIKAILER = "青岛麦凯乐店"
LIDAMAO = "青岛丽达茂店"


class Test判据(unittest.TestCase):
    ROWS = [
        {"erp_name": MAIKAILER},
        {"erp_name": LIDAMAO},
        {"erp_name": "绿城丽达店"},
        {"erp_name": "青岛鲁疆广场店", "tdoc_name": "鲁疆广场"},
        {"erp_name": "平台岗", "kind": "平台岗"},
        {"erp_name": ""},                      # 没云商名的（颐高）
    ]

    def setUp(self):
        self.idx = foreign.name_index(rows=self.ROWS)

    def test_短名也在索引里(self):
        """「丽达茂美团」不含全名「青岛丽达茂店」—— 短名匹配是这条判据的命根。"""
        self.assertIn(LIDAMAO, self.idx)          # 全名
        self.assertIn("丽达茂", self.idx)         # 去「青岛」+ 去「店」
        self.assertIn("麦凯乐", self.idx)
        self.assertIn("鲁疆广场", self.idx)       # tdoc 别名

    def test_平台岗与无名不参与(self):
        self.assertNotIn("平台岗", self.idx)
        self.assertFalse(any("颐高" in k for k in self.idx))

    def test_点名别家返回那家店(self):
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "丽达茂美团", self.idx), LIDAMAO)
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "转悦荟线上", self.idx), "")
        # 悦荟不在喂进来的名单里 → 查不到就不判（fail-open）
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "青岛鲁疆广场店下单", self.idx),
            "青岛鲁疆广场店")
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "走鲁疆广场", self.idx),
            "青岛鲁疆广场店")

    def test_自己店名不算(self):
        """备注写本店名是常态（「麦凯乐自提」）—— 不算点名别家。"""
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "麦凯乐自提", self.idx), "")
        self.assertEqual(
            foreign.other_store_in(LIDAMAO, "丽达茂美团", self.idx), "")

    def test_绿城丽达与丽达茂分得开(self):
        """`绿城丽达店` 的短名是「绿城丽达」，不含「丽达茂」—— 不能互相误伤。"""
        idx = foreign.name_index(rows=self.ROWS)
        self.assertEqual(
            foreign.other_store_in("绿城丽达店", "丽达茂美团", self.idx), LIDAMAO)
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "绿城丽达自提", self.idx), "绿城丽达店")

    def test_空备注不扫(self):
        self.assertEqual(foreign.other_store_in(MAIKAILER, "", self.idx), "")
        self.assertEqual(foreign.other_store_in(MAIKAILER, None, self.idx), "")
        self.assertEqual(foreign.other_store_in(MAIKAILER, "美团外卖", self.idx), "")

    def test_空名单一个都不剔(self):
        """名单读不到 → 回到旧口径（不凭空改数）。"""
        self.assertEqual(foreign.name_index(rows=[]), {})
        self.assertEqual(
            foreign.other_store_in(MAIKAILER, "丽达茂美团", {}), "")


class _BenefitDbCase(unittest.TestCase):
    """临时库 + monkeypatch `find_db`（与 `test_film_compute._FilmDbCase` 同套路）。"""

    def _db(self, rows):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE erp_sales (门店 TEXT, 店员 TEXT, 单据类型 TEXT,"
            " 商品名称 TEXT, 一级分类 TEXT, 二级分类 TEXT, 数量 REAL,"
            " 零售考核毛利 REAL, 支付时间 TEXT, 备注 TEXT, 单行备注 TEXT)")
        for r in rows:
            keys = list(r.keys())
            conn.execute(
                "INSERT INTO erp_sales (%s) VALUES (%s)"
                % (", ".join(keys), ",".join("?" for _ in keys)),
                [r[k] for k in keys])
        conn.commit()
        conn.close()
        return path

    def _patch(self, path):
        from src.features.valueadd.benefit import compute as bcomp
        self._mods = []

        def swap(mod, name, val):
            self._mods.append((mod, name, getattr(mod, name)))
            setattr(mod, name, val)

        swap(bcomp, "find_db", lambda root=None: path)
        # 名册走系统人店表（可能联网）—— 集成测试不碰网络
        swap(bcomp, "load_people", lambda *a, **k: ({}, "empty", ""))
        bcomp._CACHE.clear()
        self.addCleanup(bcomp._CACHE.clear)
        self.addCleanup(self._restore)

    def _restore(self):
        for mod, name, val in reversed(self._mods):
            setattr(mod, name, val)

    @staticmethod
    def _sale(**over):
        base = {"门店": MAIKAILER, "店员": "张三", "单据类型": "零售",
                "商品名称": "智能手机/华为/nova 16 EMA-AL00U", "一级分类": "手机",
                "二级分类": "手机", "数量": 1, "零售考核毛利": 0,
                "支付时间": "2026-09-11 20:33:00", "备注": "", "单行备注": ""}
        base.update(over)
        return base


class Test权益页接入(_BenefitDbCase):
    """`benefit/compute`：转单行（备注点名别家）**计入本店 = 转出店** ——
    2026-09-29 拍板；店行、人行、利润都要算上，note 报出台账（不许静默）。"""

    def test转单行算转出店(self):
        rows = [
            self._sale(备注=""),                                   # 正常 1 台
            self._sale(备注="丽达茂美团", 单据类型="分销",
                       支付时间="2026-09-11 20:34:00"),             # 转单 → **算本店**
            self._sale(备注="麦凯乐自提", 支付时间="2026-09-12 10:00:00"),  # 提自己 → 留
        ]
        self._patch(self._db(rows))
        from src.features.valueadd.benefit import compute as bcomp
        d = bcomp.compute(stores=[MAIKAILER], day=datetime.date(2026, 9, 26))
        st = d["stores"][0]
        self.assertEqual(st["new"], 3,
                         "转单行算转出店 → 2 零售 + 1 美团分销 = 3（修前剔成 2）")
        self.assertEqual(st["new_retail"] + st["new_online"], 3)
        self.assertEqual(d.get("transfer_rows"), 1,
                         "麦凯乐自提是本店名、不算转单；只有「丽达茂美团」那行记台账")
        self.assertIn("已计入本店", d.get("note", ""),
                      "note 要报出台账，否则以后没人知道口径变过（坑13 同类）")

    def test空备注店一个不剔(self):
        rows = [self._sale(备注=""), self._sale(备注="京东比价")]
        self._patch(self._db(rows))
        from src.features.valueadd.benefit import compute as bcomp
        d = bcomp.compute(stores=[MAIKAILER], day=datetime.date(2026, 9, 26))
        self.assertEqual(d["stores"][0]["new"], 2)
        self.assertEqual(d.get("transfer_rows"), 0)


class Test防护膜页接入(unittest.TestCase):
    """`film/compute` 同源同口径 —— 转单行也算转出店（两页必须一致，否则对不上）。"""

    def test转单行算转出店(self):
        import tempfile
        from src.features.valueadd.film import compute as fcomp
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE erp_sales (门店 TEXT, 店员 TEXT, 单据类型 TEXT,"
            " 商品名称 TEXT, 一级分类 TEXT, 二级分类 TEXT, 数量 REAL,"
            " 零售考核毛利 REAL, 支付时间 TEXT, 备注 TEXT, 单行备注 TEXT,"
            " \"客户/顾客\" TEXT, 付款方式 TEXT)")
        for over, label in [(dict(备注=""), "正常"),
                            (dict(备注="丽达茂美团", 单据类型="分销"), "转单")]:
            d = {"门店": MAIKAILER, "店员": "张三", "单据类型": "零售",
                 "商品名称": "智能手机/华为/nova 16", "一级分类": "手机",
                 "二级分类": "手机", "数量": 1, "零售考核毛利": 0,
                 "支付时间": "2026-09-11 20:33:00", "备注": "", "单行备注": "",
                 "客户/顾客": "", "付款方式": ""}
            d.update(over)
            conn.execute(
                "INSERT INTO erp_sales (门店,店员,单据类型,商品名称,一级分类,二级分类,"
                "数量,零售考核毛利,支付时间,备注,单行备注,\"客户/顾客\",付款方式)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [d[k] for k in ("门店", "店员", "单据类型", "商品名称", "一级分类",
                                "二级分类", "数量", "零售考核毛利", "支付时间", "备注",
                                "单行备注", "客户/顾客", "付款方式")])
        conn.commit()
        conn.close()
        orig = fcomp.find_db
        fcomp.find_db = lambda root=None: path
        self.addCleanup(lambda: setattr(fcomp, "find_db", orig))
        fcomp._CACHE.clear()
        self.addCleanup(fcomp._CACHE.clear)
        d = fcomp.compute(stores=[MAIKAILER], day=datetime.date(2026, 9, 26))
        st = d["rows"][0]
        # 正常零售1台 + 转单分销1台（美团）→ **都算转出店** = 2 台（×0.9 = 1.8）
        self.assertAlmostEqual(st["new"], 2 * 0.9, places=6,
                               msg="转单行该算进防护膜页新机（修前剔成 0.9）")
        self.assertEqual(d.get("transfer_rows"), 1, "转单行数要记台账")
        self.assertIn("已计入本店", d.get("note", ""), "note 要报出台账，别静默")


if __name__ == "__main__":
    unittest.main()
