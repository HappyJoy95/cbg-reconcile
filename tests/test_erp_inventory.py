"""库存导出解析（`erp._inventory_rows`）的回归。

盯的是**两个实测踩到的格式差异** —— 都表现为"看起来像接口变了"：

* **表头在第几行不固定**：销售明细是「第 0 行大标题、第 1 行表头」，
  库存导出**第 0 行就是表头**；接口直出的文件和 skill CLI 用 `write_xlsx`
  重写过的文件还差一行。照抄另一个函数的假设 → 把数据行当表头。
* **末尾有 1 行 `RowId='合计'`**（`Imei` 为空、`ProCount` 是全库台数）——
  不剔的话"库里有多少台"虚高 1（skill 记着：CLI 曾因此报 23601，真实 23600）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import erp                                          # noqa: E402

HEAD = ["序号", "IMEI1", "IMEI2", "名称", "分仓", "状态"]


class TestInventoryRows(unittest.TestCase):
    def _run(self, rows):
        orig = erp.read_rows
        erp.read_rows = lambda _p: rows
        try:
            return erp._inventory_rows("whatever.xlsx")
        finally:
            erp.read_rows = orig

    def test_header_on_row_zero(self):
        """库存导出的实际格式：第 0 行就是表头。"""
        out = self._run([HEAD, ["1", "A1", "", "手机甲", "仓甲", "在库"]])
        self.assertEqual(len(out), 1)
        # 中文表头要映射成英文 key —— 否则建表会得到一堆中文列名
        self.assertEqual(out[0]["Imei"], "A1")
        self.assertEqual(out[0]["StoreName"], "仓甲")
        self.assertEqual(out[0]["RowId"], "1")

    def test_header_on_row_one(self):
        """skill CLI 用 write_xlsx 重写过的文件：多一行大标题。"""
        out = self._run([["库存串号"], HEAD, ["1", "A1", "", "手机甲", "仓甲", "在库"]])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["Imei"], "A1")

    def test_drops_total_row(self):
        """合计行的 `Imei` 为空 —— 必须从源头剔掉，不能让行数虚高。"""
        out = self._run([HEAD,
                         ["1", "A1", "", "手机甲", "仓甲", "在库"],
                         ["合计", "", "", "", "", ""]])
        self.assertEqual([r["Imei"] for r in out], ["A1"])

    def test_skips_blank_rows(self):
        out = self._run([HEAD, ["1", "A1", "", "手机甲", "仓甲", "在库"], [None] * 6, []])
        self.assertEqual(len(out), 1)

    def test_no_header_raises(self):
        """找不到表头要**报错**，不能静默返回空 —— 空数据会被当成"店里没货"。"""
        with self.assertRaises(erp.ErpError):
            self._run([["随便", "几", "列"], ["1", "2", "3"]])

    def test_empty_file_raises(self):
        with self.assertRaises(erp.ErpError):
            self._run([HEAD] if False else [])


if __name__ == "__main__":
    unittest.main()
