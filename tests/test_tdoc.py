"""腾讯文档读取（`src/tdoc.py`）的回归。

盯的都是**会静默出错**的地方 —— 3.0.0 最忌讳的失败模式是
"算出一份达成率全 0、看着很合理的错数据"：

* **特殊数值（日期/百分比）配对**：格子里 N 个、值池里 N 个 double，
  两个数不相等时**必须抛**，不许错配 —— 错配出来就是一份看着对的错日期
* **定长字段必须跳过不记**：`鸿蒙PC(…)` 那种文本的字节**恰好能解析成合法消息**，
  记下来就会把那一格判成"消息"、字符串取不到 → **最后一列名字变成空串**
  （踩过：映射表只读到 8 列，不报错）
* **两个 preload 链接**：HTML 里只有一个带 `tab=` —— 抓错了"换 tab"就是空操作，
  7 个 tab 拿回同一份数据（看着像"tab 参数不生效"）
* **读不到就抛**：期间读不到、某列没编码、列序不一致 —— 一律 `TdocError`，
  绝不返回空/零（回落成"当前周"或"算成 0"是最坏的一种失败）

fixture 是**真实响应脱敏**出来的（`tests/fixtures/README.md` 写了怎么脱的），
结构与线上一致：9 个产品列、28 家门店、期间 2026-09-14~09-20。
"""

from __future__ import annotations

import base64
import datetime
import io
import json
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import tdoc                                     # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def fixture(name):
    return io.open(FIX / name, encoding="utf-8").read()


def _vi(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _fld(no, payload):
    """length-delimited 字段。"""
    return _vi((no << 3) | 2) + _vi(len(payload)) + payload


def _fld_var(no, value):
    """varint 字段。"""
    return _vi(no << 3) + _vi(value)


def _synthetic_block(texts=(), doubles=(), special_cells=0, number_cells=0):
    """拼一个**最小但合法**的 block（结构照线上：f19 → f5 值池 + f6 单元格）。

    ⚠ 必须真的合法 —— 拼错了 protobuf 会退化成"整块是个字符串"，
    测试就变成在测空气（第一版就踩过：长度字节少写一位，断言全落空）。
    """
    pool = b""
    for text in texts:
        pool += _fld(1, _fld(1, text.encode("utf-8")))
    for value in doubles:
        pool += _fld(3, b"\t" + struct.pack("<d", value))

    cells = b""
    for i in range(len(texts)):                        # 种类 4：文本
        f3 = _fld_var(1, 4) + _fld(2, _fld_var(1, i))
        cells += _fld(6, _fld_var(1, 0) + _fld_var(2, i) + _fld(3, f3))
    for k in range(special_cells):                     # 种类 2 + f4.f1==1：特殊数值
        f3 = _fld_var(1, 2) + _fld(2, _fld_var(1, 120 + k)) + _fld(4, _fld_var(1, 1))
        cells += _fld(6, _fld_var(1, 1) + _fld_var(2, k) + _fld(3, f3))
    for k in range(number_cells):                      # 种类 2：普通数字
        f3 = _fld_var(1, 2) + _fld(2, _fld_var(1, 7))
        cells += _fld(6, _fld_var(1, 1) + _fld_var(2, 1 + k) + _fld(3, f3))

    blob = _fld(19, _fld(5, pool) + cells)
    return {"block_datas": [{"related_sheet":
                             base64.b64encode(zlib.compress(blob, 9)).decode()}]}


class TestVarintAndParse(unittest.TestCase):
    def test_varint_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 2 ** 20):
            buf = bytearray()
            v = n
            while True:
                b = v & 0x7F
                v >>= 7
                buf.append(b | (0x80 if v else 0))
                if not v:
                    break
            got, i = tdoc._varint(bytes(buf), 0)
            self.assertEqual(got, n)
            self.assertEqual(i, len(buf))

    def test_varint_越界要抛(self):
        with self.assertRaises(tdoc.TdocError):
            tdoc._varint(b"\x80", 0)          # 高位一直是 1，读到底也没结束

    def test_字符串块解成_str(self):
        payload = "门店".encode("utf-8")
        buf = b"\x0a" + bytes([len(payload)]) + payload
        nodes, used = tdoc.parse_message(buf)
        self.assertEqual(used, len(buf))
        self.assertEqual(nodes, [(1, "门店")])

    def test_定长字段块不被当成消息(self):
        """⚠ 回归：`\\t` + 8 字节 = 「字段1 + wire type 1」，**看着是合法消息**。

        记进结果的话，`sub` 非空 ⇒ 这块被判成消息 ⇒ 里面的字符串再也取不到。
        真踩过：映射表最后一列名字变空、只读到 8 列，**而且不报错**。
        """
        double = b"\t" + b"\x00" * 8
        buf = b"\x0a" + bytes([len(double)]) + double
        nodes, used = tdoc.parse_message(buf)
        self.assertEqual(used, len(buf))
        self.assertEqual(len(nodes), 1)
        self.assertNotIsInstance(nodes[0][1], list, "定长字段块被当成消息了")

    def test_嵌套消息能解出来(self):
        payload = "产品列".encode("utf-8")
        inner = b"\x0a" + bytes([len(payload)]) + payload
        buf = b"\x0a" + bytes([len(inner)]) + inner
        nodes, _ = tdoc.parse_message(buf)
        self.assertIsInstance(nodes[0][1], list)
        self.assertEqual(tdoc.leaf_strings(nodes), ["产品列"])

    def test_as_double_两种形态都认(self):
        raw = b"\t" + __import__("struct").pack("<d", 46279.0)
        self.assertAlmostEqual(tdoc._as_double(raw), 46279.0)          # bytes 形态
        self.assertAlmostEqual(tdoc._as_double([(1, raw[1:])]), 46279.0)  # 被当成消息
        self.assertIsNone(tdoc._as_double("不是数字"))
        self.assertIsNone(tdoc._as_double("短".encode("utf-8")))


class TestJsonp(unittest.TestCase):
    def test_正常_jsonp(self):
        body = 'cb({"clientVars":{"collab_client_vars":' \
               '{"initialAttributedText":{"text":[{"max_row":1}]}}}})'
        self.assertEqual(tdoc.text_vars(body), {"max_row": 1})

    def test_不是_jsonp要抛(self):
        with self.assertRaises(tdoc.TdocError):
            tdoc.text_vars("<html>登录页</html>")

    def test_结构变了要抛(self):
        with self.assertRaises(tdoc.TdocError):
            tdoc.text_vars('cb({"clientVars":{}})')


class TestOpendocUrl(unittest.TestCase):
    #: ⚠ 点就在这儿：**两个 preload 链接，只有一个带 `tab=`**
    HTML = ('<link rel="preload" as="script" '
            'href="//docs.qq.com/dop-api/opendoc?u=&amp;startrow=0&amp;endrow=60&amp;id=DOC">'
            '<link rel="preload" as="script" '
            'href="//docs.qq.com/dop-api/opendoc?u=&amp;tab=TAB1&amp;startrow=0&amp;id=DOC">')

    def test_挑带_tab_的那个(self):
        url = tdoc.opendoc_url(self.HTML)
        self.assertIn("tab=TAB1", url)
        self.assertNotIn("&amp;", url)          # HTML 实体要还原
        self.assertTrue(url.startswith("https:"))

    def test_默认_tab_取得出来(self):
        self.assertEqual(tdoc.default_tab_id(self.HTML), "TAB1")

    def test_没有_preload_要抛(self):
        with self.assertRaises(tdoc.TdocError):
            tdoc.opendoc_url("<html>改版了</html>")


class TestDecodeGrid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = tdoc.text_vars(fixture("tdoc-mapping.jsonp"))
        cls.target = tdoc.text_vars(fixture("tdoc-target.jsonp"))

    def test_映射表解得出来(self):
        grid, rich, numbers = tdoc.decode_grid(self.mapping)
        self.assertEqual(len(numbers), 2, "C1/D1 两个日期应该在值池里")
        self.assertEqual(len(rich), 0, "映射表没有多行单元格")
        self.assertIsInstance(grid[(1, 0)], str)
        self.assertTrue(grid[(1, 0)].strip(), "第一列产品名不该是空的")

    def test_目标表解得出来(self):
        grid, rich, numbers = tdoc.decode_grid(self.target)
        self.assertEqual(len(numbers), 9, "9 个产品列的权重")
        self.assertEqual(len(rich), 4, "目标表有 4 个多行单元格")
        # 权重行 + 门店行都在
        self.assertIsInstance(grid[(0, 2)], float)

    def test_空_block_要抛(self):
        with self.assertRaises(tdoc.TdocError):
            tdoc.decode_grid({"block_datas": []})

    def test_特殊数值个数对不上要抛(self):
        """⚠ 宁可报错，不许错配 —— 错配出来是"看着很合理"的错日期。"""
        t0 = _synthetic_block(texts=["名称"], doubles=[46279.0], special_cells=2)
        with self.assertRaises(tdoc.TdocError) as ctx:
            tdoc.decode_grid(t0)
        self.assertIn("特殊数值", str(ctx.exception))

    def test_合成块能正常解出来(self):
        """不依赖 fixture 的最小通路：文本 / 普通数字 / 特殊数值 三种都要对。"""
        t0 = _synthetic_block(texts=["名称", "值"], doubles=[0.25],
                              special_cells=1, number_cells=2)
        grid, _rich, numbers = tdoc.decode_grid(t0)
        self.assertEqual(numbers, [0.25])
        self.assertEqual(grid[(0, 0)], "名称")     # 种类 4 → 池里第 0 个
        self.assertEqual(grid[(0, 1)], "值")
        self.assertEqual(grid[(1, 0)], 0.25)      # 种类 2 + f4.f1==1 → 池里的 double
        self.assertEqual(grid[(1, 1)], 7)         # 种类 2 → f2.f1 就是值


class TestSheetTabs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = tdoc.text_vars(fixture("tdoc-mapping.jsonp"))

    def test_列出所有_tab(self):
        tabs = tdoc.sheet_tabs(self.mapping)
        names = [n for _i, n in tabs]
        self.assertIn("周度重点产品", names)
        self.assertIn("周度重点映射表", names)

    def test_按名字找_tab(self):
        html = ('<link rel="preload" href="//docs.qq.com/dop-api/opendoc'
                '?tab=BB08J2&id=DOC">')
        self.assertEqual(tdoc.resolve_tab(self.mapping, html, "周度重点映射表"), "izvld9")
        self.assertEqual(tdoc.resolve_tab(self.mapping, html, "周度重点产品"), "BB08J2")

    def test_找不到_要抛且列出可选项(self):
        html = '<link rel="preload" href="//docs.qq.com/dop-api/opendoc?tab=X&id=D">'
        with self.assertRaises(tdoc.TdocError) as ctx:
            tdoc.resolve_tab(self.mapping, html, "不存在的表")
        self.assertIn("周度重点产品", str(ctx.exception))

    def test_第一个_sheet_没有_id_用_url_兜底(self):
        """第一个 sheet 不存 id —— 没有兜底就拿不到它。"""
        tabs = tdoc.sheet_tabs(self.mapping)
        first_id, first_name = tabs[0]
        self.assertEqual(first_name, "周度重点产品")
        self.assertIsNone(first_id, "这个表的 id 本来就该是 None（靠 URL 兜底）")
        html = ('<link rel="preload" href="//docs.qq.com/dop-api/opendoc'
                '?tab=BB08J2&id=D">')
        self.assertEqual(tdoc.resolve_tab(self.mapping, html, first_name), "BB08J2")


class TestReadMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        grid, _rich, _n = tdoc.decode_grid(tdoc.text_vars(fixture("tdoc-mapping.jsonp")))
        cls.grid = grid

    def test_读出九列和期间(self):
        info = tdoc.read_mapping(self.grid)
        self.assertEqual(len(info["columns"]), 9)
        self.assertEqual(info["start"], datetime.date(2026, 9, 14))
        self.assertEqual(info["end"], datetime.date(2026, 9, 20))
        for name, codes in info["columns"]:
            self.assertTrue(name.strip())
            self.assertTrue(codes, "%s 没有编码" % name)
            for c in codes:
                self.assertTrue(c.isdigit(), c)

    def test_产品列名最后一行不能丢(self):
        """⚠ 回归：定长字段被当成消息时，最后一列名字变空 → 只读到 8 列。"""
        info = tdoc.read_mapping(self.grid)
        self.assertEqual(len(info["columns"]), 9, "最后一列又被吃掉了")

    def test_期间读不到要抛_不许回落当前周(self):
        grid = dict(self.grid)
        grid.pop((0, 2))
        with self.assertRaises(tdoc.TdocError) as ctx:
            tdoc.read_mapping(grid)
        self.assertIn("C1/D1", str(ctx.exception))

    def test_某列没有编码要抛_不许当空(self):
        grid = dict(self.grid)
        grid[(3, 1)] = 0                     # 编码那格变成数字
        with self.assertRaises(tdoc.TdocError):
            tdoc.read_mapping(grid)


class TestReadTargets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m_grid, _r, _n = tdoc.decode_grid(tdoc.text_vars(fixture("tdoc-mapping.jsonp")))
        cls.mapping = tdoc.read_mapping(m_grid)
        cls.grid, cls.rich, _nums = tdoc.decode_grid(
            tdoc.text_vars(fixture("tdoc-target.jsonp")))
        cls.names = [n for n, _c in cls.mapping["columns"]]

    def test_读出权重列名和门店(self):
        info = tdoc.read_targets(self.grid, self.rich, mapping_columns=self.names)
        self.assertEqual(len(info["weights"]), 9)
        self.assertAlmostEqual(sum(info["weights"]), 1.0, places=6)
        self.assertEqual(len(info["columns"]), 9)
        self.assertEqual(len(info["rows"]), 28)
        for _store, targets in info["rows"]:
            self.assertEqual(len(targets), 9)
            self.assertTrue(all(isinstance(t, int) for t in targets))

    def test_合计行被跳过(self):
        info = tdoc.read_targets(self.grid, self.rich, mapping_columns=self.names)
        self.assertNotIn("合计", [s for s, _t in info["rows"]])

    def test_列名缺失且没给映射要抛(self):
        with self.assertRaises(tdoc.TdocError) as ctx:
            tdoc.read_targets(self.grid, self.rich)
        self.assertIn("mapping_columns", str(ctx.exception))

    def test_列数不一致要抛(self):
        with self.assertRaises(tdoc.TdocError):
            tdoc.read_targets(self.grid, self.rich, mapping_columns=self.names[:8])

    def test_列序不一致要抛(self):
        """⚠ 按位置补名字**必须先核对** —— 列被挪过就不能硬补。"""
        swapped = list(self.names)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        with self.assertRaises(tdoc.TdocError):
            tdoc.read_targets(self.grid, self.rich, mapping_columns=swapped)

    def test_多行列名按位置补齐(self):
        info = tdoc.read_targets(self.grid, self.rich, mapping_columns=self.names)
        for i, name in enumerate(info["columns"]):
            self.assertTrue(name and name.strip(), "第 %d 列名字空着" % (i + 1))


class TestNormColumn(unittest.TestCase):
    """产品列名归一化 —— 两边写法天生不同，不归一就静默算成 0。"""

    def test_换行与斜杠等价(self):
        self.assertEqual(tdoc.norm_column("畅享90plus\n（m plus）"),
                         tdoc.norm_column("畅享90plus/（m plus）"))

    def test_全角括号与半角等价(self):
        self.assertEqual(tdoc.norm_column("鸿蒙PC（鸿蒙14除外）"),
                         tdoc.norm_column("鸿蒙PC(鸿蒙14除外)"))

    def test_空格无影响(self):
        self.assertEqual(tdoc.norm_column("Mate 70 Air"), tdoc.norm_column("Mate70Air"))

    def test_不同列不能混为一谈(self):
        self.assertNotEqual(tdoc.norm_column("nova 16 Pro"), tdoc.norm_column("nova 16 Ultra"))


class TestXlDate(unittest.TestCase):
    def test_序列号换算(self):
        self.assertEqual(tdoc.xl_date(46279), datetime.date(2026, 9, 14))
        self.assertEqual(tdoc.xl_date(46285), datetime.date(2026, 9, 20))
        self.assertEqual(tdoc.xl_date(1), datetime.date(1899, 12, 31))


if __name__ == "__main__":
    unittest.main()
