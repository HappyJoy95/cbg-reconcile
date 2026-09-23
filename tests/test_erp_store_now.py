"""`ErpClient.store_now()` / `warehouses()` —— **盘点账面**取数与完整性自检。

为什么值得单独钉：这套自检是从 `Inventory Check/src/erp.js` 搬过来的（那边线上跑过很久），
搬的过程中最容易丢的就是"**少给了还不吭声**"那几条分支。而盘点的后果比别处重 ——
账面少一半，扫到的机器会被判成「表外码」（窜货嫌疑），
等于把"接口少给了"变成"门店的台账有问题"。

判据（六条，逐条一个用例）：
结构不对 · `TotalRows` 不是数 · 实际行数 ≠ 自报 · 提前空页 · 重复页 · 总分页数变化 · 分页上限。
另有两条**必须成功**的：合法的零库存、正好一页拿全。
"""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import erp                                          # noqa: E402


def _row(rid, imei="A1"):
    return {"RowId": rid, "Imei": imei, "ProCount": 1, "ProCount_OnTransfer": 0}


class _Fake:
    """按页吐数据 —— 记下每次请求的 body，好验证"参数真的传下去了"。"""

    def __init__(self, pages, totals=None):
        #: pages: [[第1页行...], [第2页行...]]；totals: 每页自报的 TotalRows
        self.pages = pages
        self.totals = totals or [sum(len(p) for p in pages)] * len(pages)
        self.bodies = []

    def __call__(self, url, body, timeout=None):
        i = len(self.bodies)
        self.bodies.append(body)
        page = self.pages[i] if i < len(self.pages) else []
        total = self.totals[i] if i < len(self.totals) else self.totals[-1]
        return {"ResponseID": 0, "Data": {"Data": page, "TotalRows": total}}


def _client(fake):
    c = erp.ErpClient({"token": "t", "username": "u", "password": "p", "company": "c"})
    c.call = fake
    return c


class TestStoreNow(unittest.TestCase):
    def test_一页拿全(self):
        fake = _Fake([[_row(1), _row(2), _row(3)]])
        got = _client(fake).store_now(datetime.date(2026, 9, 20), store_id="318826")
        self.assertEqual(got["total"], 3)
        self.assertEqual(got["pages"], 1)
        self.assertEqual([r["RowId"] for r in got["rows"]], [1, 2, 3])
        self.assertEqual(got["date"], "2026-09-20")
        self.assertEqual(got["store_id"], "318826")

    def test_参数真的传下去了(self):
        """`StoreIds` 是"盘这家店的仓"的唯一手段（仓名是假筛，skill 坑 3b）。"""
        fake = _Fake([[]], totals=[0])
        _client(fake).store_now(datetime.date(2026, 9, 20), store_id="318826")
        body = fake.bodies[0]
        self.assertEqual(body["StoreIds"], "318826")
        self.assertEqual(body["DateOfSnapshot"], "2026-09-20")
        self.assertEqual(body["outCol"], "ProCount,ProCount_OnTransfer")
        self.assertIn("OldFlag", body["groupBy"])
        self.assertEqual(body["PageIndex"], 1)

    def test_合法的零库存算成功(self):
        """店里真没货和"接口没给"是两件事 —— 前者不该报错。"""
        fake = _Fake([[]], totals=[0])
        got = _client(fake).store_now(datetime.date(2026, 9, 20))
        self.assertEqual(got["rows"], [])
        self.assertEqual(got["total"], 0)

    def test_应有3行只收到1行要报错(self):
        """原版 6 条里的第一条 —— "少给了还不吭声"必须拦住。"""
        fake = _Fake([[_row(1)]], totals=[3])
        with self.assertRaises(erp.ErpIncomplete):
            _client(fake).store_now(datetime.date(2026, 9, 20))

    def test_提前返回空页要报错(self):
        fake = _Fake([[_row(1), _row(2)], []], totals=[4, 4])
        with self.assertRaises(erp.ErpIncomplete) as cm:
            _client(fake).store_now(datetime.date(2026, 9, 20), page_size=2)
        self.assertIn("空页", str(cm.exception))

    def test_返回重复页要报错(self):
        """同一页给两遍 ⇒ 账面里同一台机器出现两次，行数还正好对得上。"""
        same = [_row(1), _row(2)]
        fake = _Fake([same, list(same)], totals=[4, 4])
        with self.assertRaises(erp.ErpIncomplete) as cm:
            _client(fake).store_now(datetime.date(2026, 9, 20), page_size=2)
        self.assertIn("重复", str(cm.exception))

    def test_总行数中途变了要报错(self):
        fake = _Fake([[_row(1), _row(2)], [_row(3)]], totals=[4, 3])
        with self.assertRaises(erp.ErpIncomplete) as cm:
            _client(fake).store_now(datetime.date(2026, 9, 20), page_size=2)
        self.assertIn("总行数中途变了", str(cm.exception))

    def test_拉满上限还没拉全要报错(self):
        """别无限拉 —— 也不能返回半份。"""
        pages = [[_row(i * 2 + 1), _row(i * 2 + 2)] for i in range(erp.STORE_NOW_MAX_PAGES + 2)]
        fake = _Fake(pages, totals=[999] * len(pages))
        with self.assertRaises(erp.ErpIncomplete) as cm:
            _client(fake).store_now(datetime.date(2026, 9, 20), page_size=2)
        self.assertIn("还没拉全", str(cm.exception))

    def test_结构不对要报错(self):
        c = _client(lambda url, body, timeout=None: {"ResponseID": 0, "Data": []})
        with self.assertRaises(erp.ErpIncomplete):
            c.store_now(datetime.date(2026, 9, 20))
        c2 = _client(lambda url, body, timeout=None: {"ResponseID": 0, "Data": {}})
        with self.assertRaises(erp.ErpIncomplete):
            c2.store_now(datetime.date(2026, 9, 20))

    def test_TotalRows不是数要报错(self):
        c = _client(lambda url, body, timeout=None:
                    {"ResponseID": 0, "Data": {"Data": [_row(1)], "TotalRows": ""}})
        with self.assertRaises(erp.ErpIncomplete):
            c.store_now(datetime.date(2026, 9, 20))

    def test_多页正常拼接(self):
        fake = _Fake([[_row(1), _row(2)], [_row(3)]], totals=[3, 3])
        got = _client(fake).store_now(datetime.date(2026, 9, 20), page_size=2)
        self.assertEqual(len(got["rows"]), 3)
        self.assertEqual(got["pages"], 2)

    def test_它是_ErpError_的子类(self):
        """调用方（功能模块/接口）接 `ErpError` 就该能兜住它 —— 别到处改成接两个类。"""
        self.assertTrue(issubclass(erp.ErpIncomplete, erp.ErpError))


class TestWarehouses(unittest.TestCase):
    def test_返回列表(self):
        c = _client(lambda url, body, timeout=None:
                    {"ResponseID": 0, "Data": [{"Id": 1, "Name": "仓甲",
                                                "BranchName": "店甲"}]})
        got = c.warehouses()
        self.assertEqual(got[0]["Id"], 1)
        self.assertEqual(got[0]["BranchName"], "店甲")

    def test_Data_不是列表时给空表(self):
        """宁可给空表让界面说"没读到仓"，也别把 dict 当列表去迭代。"""
        c = _client(lambda url, body, timeout=None: {"ResponseID": 0, "Data": {"a": 1}})
        self.assertEqual(c.warehouses(), [])


if __name__ == "__main__":
    unittest.main()


class TestTransitImei(unittest.TestCase):
    """在途兜底（`InventoryType=1`）—— 账面那张表没给在途列时才走它。

    ⚠ 判据照 erp-api skill：`ageStart`/`ageEnd` **必传**（不传报「参数错误」），
      还要带 `column[]` 规格；`InventoryType` 只对当天生效。
    """

    def _fake(self, pages, shape="dict"):
        class F:
            def __init__(self):
                self.bodies = []
                self.pages = pages

            def __call__(self, url, body, timeout=None):
                self.bodies.append(body)
                i = len(self.bodies) - 1
                batch = self.pages[i] if i < len(self.pages) else []
                if shape == "list":
                    return {"ResponseID": 0, "Data": batch}
                return {"ResponseID": 0, "Data": {"Data": batch, "TotalRows": 3}}

        return F()

    def test_分页拼接(self):
        fake = self._fake([[_row(1), _row(2)], [_row(3)]])
        got = _client(fake).transit_imei(datetime.date(2026, 9, 20),
                                         store_id="318826", page_size=2)
        self.assertEqual([r["RowId"] for r in got], [1, 2, 3])
        self.assertEqual(fake.bodies[-1]["PageIndex"], "2")

    def test_参数照契约发(self):
        fake = self._fake([[]])
        _client(fake).transit_imei(datetime.date(2026, 9, 20), store_id="318826")
        b = fake.bodies[0]
        self.assertEqual(b["InventoryType"], "1")
        self.assertEqual(b["StoreId"], "318826")
        self.assertIn("ageStart", b, "skill 记着：`ageStart` 不传会报参数错误")
        self.assertIn("ageEnd", b)
        self.assertIn("column[0][__Key]", b, "要带 column[] 规格")

    def test_Data_是数组也认(self):
        """上游 `Inventory Check` 两种形状都兜着 —— 后端照抄，别只认一种。"""
        fake = self._fake([[_row(1)]], shape="list")
        got = _client(fake).transit_imei(datetime.date(2026, 9, 20))
        self.assertEqual(len(got), 1)

    def test_形状不对要报错(self):
        """**绝不悄悄当成"没有在途"** —— 那会让「在途待入库」整页消失且不报错。"""
        c = _client(lambda url, body, timeout=None: {"ResponseID": 0, "Data": {"foo": 1}})
        with self.assertRaises(erp.ErpIncomplete):
            c.transit_imei(datetime.date(2026, 9, 20))
