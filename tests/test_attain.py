"""销售达成的 **IO 层**（`features/sales/attain/attain.py`）—— 读文档 / 查库 / 落盘。

⚠⚠ **本文件里最重要的一条是 `Test假数据必须炸`**（验收标准第 3 条）：
读不到目标表时**必须抛**，不许返回"全 0 的一份很合理的假报告"。
那是这类功能最坏的失败形态 —— 数字看着正常，门店照着它以为自己没达标。

fixture 用的是 M1 那两份**真实响应脱敏**出来的 jsonp（`tests/fixtures/README.md`），
所以门店名和产品名是乱码字符，但**结构、列数、权重、台量都是真的**。
"""

import datetime
import json
import sqlite3
import tempfile
import re
import unittest
from unittest import mock
from pathlib import Path

from src import tdoc
from src.features.sales.attain import attain as A
from src.features.sales.attain import metric as M
from src.paths import ROOT

FIX = Path(__file__).resolve().parent / "fixtures"


def _grids():
    """两份 fixture → `(mapping_grid, target_grid, rich)`。"""
    def one(name):
        text0 = tdoc.text_vars((FIX / name).read_text(encoding="utf-8"))
        return tdoc.decode_grid(text0)
    m_grid, _mr, _mn = one("tdoc-mapping.jsonp")
    t_grid, rich, _tn = one("tdoc-target.jsonp")
    return m_grid, t_grid, rich


def _sales_conn(rows):
    """造一个只有 `erp_sales` 的临时库（列名照真库）。"""
    conn = sqlite3.connect(":memory:")
    # ⚠ 列名照**真库**（`PRAGMA table_info(erp_sales)`）—— 少一列 SQL 就会报
    #   `no such column`（2026-09-19 给 SQL 加 `店员` 时踩到，13 条测试一起红）。
    conn.execute("CREATE TABLE erp_sales (门店 TEXT, 商品编码 TEXT, 数量 TEXT,"
                 " 单据类型 TEXT, 商品名称 TEXT, 串号标识 TEXT, 支付时间 TEXT,"
                 " 店员 TEXT)")
    # 允许老用例只写 7 个值（店员留空）—— 加列不该逼着 40 条用例一起改
    fixed = [tuple(r) + ("",) * (8 - len(r)) for r in rows]
    conn.executemany("INSERT INTO erp_sales VALUES (?,?,?,?,?,?,?,?)", fixed)
    conn.commit()
    return conn


class Test目标表读得对(unittest.TestCase):
    """M2 的验收：期间 / 列 / 权重 / 门店 / 台量，逐项对。"""

    @classmethod
    def setUpClass(cls):
        cls.m_grid, cls.t_grid, cls.rich = _grids()
        cls.plan = A.build_plan(cls.m_grid, cls.t_grid, rich=cls.rich)

    def test_期间来自文档_C1D1_不是当前周(self):
        """⚠ 办公室忘改表 ⇒ 我们照它算**上一周**（界面上标"数据截至"，但不硬失败）。"""
        self.assertEqual(self.plan.period, "2026-W38")
        self.assertEqual(self.plan.start, datetime.date(2026, 9, 14))
        self.assertEqual(self.plan.end, datetime.date(2026, 9, 20))

    def test_九个产品列和权重(self):
        self.assertEqual(len(self.plan.columns), 9)
        self.assertEqual(list(self.plan.weights()),
                         [0.15, 0.15, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
        self.assertAlmostEqual(sum(self.plan.weights()), 1.0, places=6)

    def test_每列的编码是集合(self):
        """skill 第 8 条：**精确 ∈**（frozenset），不是子串匹配。"""
        for c in self.plan.columns:
            self.assertIsInstance(c.codes, frozenset, c.name)
            self.assertTrue(c.codes, "%s 一个编码都没有" % c.name)

    def test_二十八家门店_台量是逐格的(self):
        self.assertEqual(len(self.plan.stores), 28)
        first = self.plan.stores[0]
        self.assertEqual(first.targets, (6, 3, 3, 3, 3, 1, 1, 2, 1))
        self.assertEqual(len(first.targets), len(self.plan.columns))
        for sp in self.plan.stores:
            self.assertEqual(len(sp.targets), 9, sp.name)

    def test_合计行没被当门店(self):
        self.assertNotIn("合计", [s.name for s in self.plan.stores])

    def test_区域按合并单元格向下填充(self):
        """A 列只有合并块第一行有值 —— 其余行要**继承**上面那个。"""
        regions = [s.region for s in self.plan.stores]
        self.assertTrue(regions[0])
        self.assertEqual(regions[1], regions[0], "同一个合并块里的行应该同区域")
        self.assertGreaterEqual(len(set(regions)), 2, "至少两个区域块")
        self.assertNotIn("", regions, "每一行都该有区域（填充过）")

    def test_区域原样留着不拆(self):
        """实测一个合并块里可能写着两个区域（`城阳\\n胶州`）—— 原样存，不拆。"""
        self.assertIn("\n", self.plan.stores[0].region)


class Test假数据必须炸(unittest.TestCase):
    """⚠⚠ 验收标准第 3 条 —— 也是实现时最容易被"兜底"掉的一条。"""

    def test_映射表空(self):
        with self.assertRaises(Exception) as cm:
            A.build_plan({}, _grids()[1])
        self.assertNotIn("0", str(cm.exception)[:1], "别把失败变成 0")

    def test_目标表空(self):
        with self.assertRaises(Exception):
            A.build_plan(_grids()[0], {})

    def test_映射表没有期间(self):
        m_grid, t_grid, rich = _grids()
        no_date = {k: v for k, v in m_grid.items()
                   if not (k[0] == 0 and k[1] in (2, 3))}
        with self.assertRaises(Exception) as cm:
            A.build_plan(no_date, t_grid, rich=rich)
        self.assertIn("C1/D1", str(cm.exception), "报错要说清是**哪一步**")

    def test_列数对不上(self):
        m_grid, t_grid, rich = _grids()
        # 删掉映射表最后一列的产品名 → 两边列数不等
        trimmed = {k: v for k, v in m_grid.items() if not (k[1] == 0 and k[0] >= 8)}
        with self.assertRaises(Exception):
            A.build_plan(trimmed, t_grid, rich=rich)

    def test_run_读不到文档时是失败不是全零(self):
        """端到端那一条：`run()` 也要**返回 ok=False**，不许落一份全 0 的盘。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        real = A.read_plan

        def boom(*a, **k):
            raise A.AttainError("假装文档读不到")
        A.read_plan = boom
        try:
            res = A.run(root=root)
        finally:
            A.read_plan = real
        self.assertFalse(res["ok"])
        self.assertIn("读目标表失败", res["why"])
        self.assertFalse((root / "out").exists(), "失败了还落盘？那门店就会看到一份假报告")


class Test门店名匹配(unittest.TestCase):
    """⚠ 门店名对不上是**第一位的排查点**（设计 §0.2：目标表写「鲁疆广场」）。"""

    def test_按云商名精确命中(self):
        row = A.match_store("青岛城阳万象汇店", ROOT)
        self.assertIsNotNone(row)
        self.assertEqual(row["erp_name"], "青岛城阳万象汇店")

    def test_按_tdoc_name_别名命中(self):
        """目标表里叫「鲁疆广场」，云商里叫「青岛鲁疆广场店」—— 靠别名对上。"""
        row = A.match_store("鲁疆广场", ROOT)
        self.assertIsNotNone(row, "别名没生效（config/stores.yaml 的 tdoc_name）")
        self.assertEqual(row["erp_name"], "青岛鲁疆广场店")

    def test_不做模糊匹配(self):
        """⚠ 模糊一下就会配到隔壁店，而界面上**看不出来**。

        ⚠ 唯一的例外是**首尾空白**（见下一条）：那是文档里肉眼看不见的字符，
          不是"另一个名字"。
        """
        for bad in ("鲁疆", "鲁疆广场店", "青岛鲁疆", "城阳万象汇", "青岛城阳万象汇"):
            self.assertIsNone(A.match_store(bad, ROOT), "『%s』不该匹配上" % bad)

    def test_首尾空白可以容忍(self):
        """文档里肉眼看不见的空格 —— 允许 `strip()`（多这一下不会配错店）。"""
        self.assertIsNotNone(A.match_store("鲁疆广场 ", ROOT))
        self.assertIsNotNone(A.match_store(" 鲁疆广场", ROOT))

    def test_空名字不算命中(self):
        self.assertIsNone(A.match_store("", ROOT))
        self.assertIsNone(A.match_store(None, ROOT))

    def test_store_map_只用匹配上的(self):
        plan = M.Plan(period="2026-W38", start=datetime.date(2026, 9, 14),
                      end=datetime.date(2026, 9, 20),
                      columns=(M.Column("A", frozenset(["1"]), 1.0),),
                      stores=(M.StorePlan("青岛城阳万象汇店", "", (1,)),
                              M.StorePlan("查无此店", "", (1,))))
        got = A.store_map(plan, ROOT)
        self.assertEqual(list(got), ["青岛城阳万象汇店"])


class Test取销售行(unittest.TestCase):
    """四个剔除 + 三个实测坑（TEXT 数量、退货负数、一次退多台）。"""

    def setUp(self):
        self.conn = _sales_conn([
            ("甲店", "1001", "1", "零售", "Mate60", "新", "2026-09-15 10:00:00"),
            ("甲店", "1001", "-1", "零售退", "Mate60", "新", "2026-09-16 10:00:00"),
            ("甲店", "1002", "1", "分销", "Mate60 演示机", "新", "2026-09-15 11:00:00"),
            ("甲店", "1003", "1", "零售", "体验机 手表", "新", "2026-09-15 12:00:00"),
            ("甲店", "1004", "1", "零售", "Mate60", "外调,新", "2026-09-15 13:00:00"),
            ("甲店", "1005", "1", "核销", "Mate60", "新", "2026-09-15 14:00:00"),
            ("甲店", "1006", "-5", "零售退", "Mate60", "新", "2026-09-17 10:00:00"),
            ("乙店", "1001", "2", "零售", "Mate60", "新", "2026-09-15 15:00:00"),
            ("甲店", "1007", "1", "零售", "Mate60", "新", "2026-09-13 10:00:00"),   # 期间外
        ])
        self.addCleanup(self.conn.close)

    def _load(self, start="2026-09-14", end="2026-09-20"):
        return A.load_sales(self.conn, datetime.date.fromisoformat(start),
                            datetime.date.fromisoformat(end))

    def test_四种剔除各自计数(self):
        sales, dropped = self._load()
        self.assertEqual(dropped.get("演示机/体验机"), 2)
        self.assertEqual(dropped.get("外调"), 1)
        self.assertEqual(dropped.get("单据类型不计入"), 1)
        self.assertEqual(dropped.get("期间外", 0), 0, "期间由 SQL 管，不该出现在剔除统计里")

    def test_退货直接相加_不翻正(self):
        sales, _ = self._load()
        甲 = [s for s in sales if s.store == "甲店"]
        self.assertEqual(M.sum_codes(甲, {"1001"}), 0, "1 台 + 退 1 台 = 0")
        self.assertEqual(M.sum_codes(甲, {"1006"}), -5, "一次退 5 台：负数保留")

    def test_数量从_TEXT_转成_int(self):
        sales, _ = self._load()
        for s in sales:
            self.assertIsInstance(s.qty, int, "库里是 TEXT，必须转成 int")

    def test_期间是左闭右闭(self):
        sales, _ = self._load()
        self.assertNotIn("1007", [s.code for s in sales])

    def test_剔除是逐行的不是逐店的(self):
        """甲店的演示机被剔，但**同一家店**的正常销售要留着。"""
        sales, _ = self._load()
        self.assertEqual({s.code for s in sales if s.store == "甲店"},
                         {"1001", "1006"})

    def test_按门店分组(self):
        sales, _ = self._load()
        got = A.group_by_store(sales)
        self.assertEqual(sorted(got), ["乙店", "甲店"])
        self.assertEqual(len(got["乙店"]), 1)

    def test_data_until_是期间内最大的那天(self):
        got = A.data_until(self.conn, datetime.date(2026, 9, 14), datetime.date(2026, 9, 20))
        self.assertEqual(got, "2026-09-17")

    def test_data_until_没有数据时是空串(self):
        got = A.data_until(self.conn, datetime.date(2026, 1, 1), datetime.date(2026, 1, 2))
        self.assertEqual(got, "")


class Test算完落盘(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "stores.yaml").write_text(
            "stores:\n  - erp_name: \"甲店\"\n    huawei_code: \"X1\"\n"
            "  - erp_name: \"乙店\"\n    huawei_code: \"X2\"\n", encoding="utf-8")
        self.plan = M.Plan(
            period="2026-W38", start=datetime.date(2026, 9, 14),
            end=datetime.date(2026, 9, 20),
            columns=(M.Column("A", frozenset(["1001"]), 0.5),
                     M.Column("B", frozenset(["2001"]), 0.3),
                     M.Column("C", frozenset(), 0.2)),      # 第三列**没映射**
            stores=(M.StorePlan("甲店", "市区", (10, 10, 10)),
                    M.StorePlan("乙店", "市区", (10, 10, 10))))
        self.conn = _sales_conn([
            ("甲店", "1001", "5", "零售", "Mate60", "新", "2026-09-15 10:00:00"),
            ("乙店", "1001", "20", "零售", "Mate60", "新", "2026-09-15 10:00:00"),
        ])
        self.addCleanup(self.conn.close)

    def _compute(self, **kw):
        sales, _ = A.load_sales(self.conn, self.plan.start, self.plan.end)
        return A.compute(self.plan, sales, conn=self.conn, root=self.root, **kw)

    def test_落盘字典的形状(self):
        p = self._compute()
        for k in ("exists", "period", "start", "end", "data_until", "computed_at",
                  "columns", "weights", "missing_columns", "rows", "region_sums"):
            self.assertIn(k, p)
        self.assertEqual(p["period"], "2026-W38")
        self.assertEqual(p["data_until"], "2026-09-15")
        self.assertEqual(p["columns"], ["A", "B", "C"])
        self.assertEqual(p["weights"], [0.5, 0.3, 0.2])

    def test_没映射的列被标出来_且不计入总达成率(self):
        p = self._compute()
        self.assertEqual(p["missing_columns"], ["C"])
        甲 = [r for r in p["rows"] if r["store"] == "甲店"][0]
        self.assertIsNone(甲["rates"][2])
        # (0.5×0.5 + 0.3×0) / 0.8 = 0.3125 —— 分母**只算参与的两列**
        self.assertAlmostEqual(甲["total"], 0.3125, places=4)

    def test_封顶在这条链上也生效(self):
        p = self._compute()
        乙 = [r for r in p["rows"] if r["store"] == "乙店"][0]
        self.assertEqual(乙["rates"][0], 1.2, "20/10 要封顶到 120%")

    def test_本店过滤在_落盘之前(self):
        """门店端只要自己那一行；办公室要全区（设计 §5.3）。"""
        p = self._compute(store_filter="甲店")
        self.assertEqual([r["store"] for r in p["rows"]], ["甲店"])
        p2 = self._compute()
        self.assertEqual(len(p2["rows"]), 2)

    def test_没匹配上的店给_None(self):
        plan = M.Plan(period=self.plan.period, start=self.plan.start, end=self.plan.end,
                      columns=self.plan.columns,
                      stores=self.plan.stores + (M.StorePlan("查无此店", "", (1, 1, 1)),))
        sales, _ = A.load_sales(self.conn, plan.start, plan.end)
        p = A.compute(plan, sales, conn=self.conn, root=self.root)
        row = [r for r in p["rows"] if r["store"] == "查无此店"][0]
        self.assertFalse(row["matched"])
        self.assertIsNone(row["total"], "没匹配上 ≠ 0%")

    def test_先写临时文件再改名(self):
        """⚠ 直接覆盖写的话，中途断电就是一个半截 JSON，
        而前端读它只会说"读不出来"（跟"还没算过"长得一样）。"""
        p = self._compute()
        path = A.save(self.root, p)
        self.assertEqual(path.name, "attain-2026.json")
        self.assertTrue(path.is_file())
        self.assertEqual(list(path.parent.glob("*.tmp")), [], "临时文件要清掉")

    def test_读回来还是那份(self):
        p = self._compute()
        A.save(self.root, p)
        got = A.load(self.root, year=2026)
        self.assertTrue(got["exists"])
        self.assertEqual(got["period"], p["period"])
        self.assertEqual(len(got["rows"]), len(p["rows"]))

    def test_没算过时给_error_不给空壳(self):
        """⚠ **绝不给 `exists: true` + 空 rows 的壳** —— 那就是"看着很合理的空"。"""
        got = A.load(self.root)
        self.assertFalse(got["exists"])
        self.assertIn("还没算过", got["error"])

    def test_坏文件也是_exists_False(self):
        (self.root / "out" / "attain-2026.json").write_text("{半截", encoding="utf-8")
        got = A.load(self.root)
        self.assertFalse(got["exists"])
        self.assertIn("读不出来", got["error"])

    def test_跨年退回上一年那份(self):
        (self.root / "out" / "attain-2025.json").write_text(
            json.dumps({"exists": True, "period": "2025-W52", "rows": []}),
            encoding="utf-8")
        got = A.load(self.root)          # 今天（2026）那份还没有
        self.assertTrue(got["exists"])
        self.assertEqual(got["period"], "2025-W52")


if __name__ == "__main__":
    unittest.main()


class Test接口与前端接线(unittest.TestCase):
    """M4 的最后一公里：**后端 → 接口 → 页面**。

    ⚠ 每一步只到后端就等于没做（门店看到的只有页面），所以这里钉的是这条链。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        # ⚠ 门店名单要**这个 root 里**有一份：`match_store` 按 `app.root` 解析，
        #   缺了的话每家店都 `matched=False`（`total=None`）—— 那是"没配名单"，
        #   不是"算不出来"，两件事在界面上长得不一样。
        (self.root / "config").mkdir()
        (self.root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "甲店"\n    huawei_code: "X1"\n', encoding="utf-8")
        # ⚠ 2026-09-21（M17）：**这台机器是哪家店**现在是看得到哪些行的依据
        #   （ → ）—— 夹具里不配店名的话，
        #   范围是空的 ⇒ 达成一行都读不回来。生产上门禁会先拦住没配店名的机器，
        #   这里是直接调 ，得自己把前提摆上。
        (self.root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "甲店"\n', encoding="utf-8")

    def test_接口是纯读盘_不读文档(self):
        """⚠ 概览页 30 秒刷一次 —— 每次都去读腾讯文档的话：慢，而且办公室改表
        会让界面在两次刷新之间跳数字（"哪个数算数"就说不清了）。"""
        from src import web
        app = web.App(self.root, "config/store-X.yaml")
        with mock.patch.object(A, "read_plan",
                               side_effect=AssertionError("接口不许读文档")):
            got = app.attain()
        self.assertFalse(got["exists"])

    def test_没算过时给_error_不给空壳(self):
        from src import web
        got = web.App(self.root, "config/store-X.yaml").attain()
        self.assertFalse(got["exists"])
        self.assertIn("还没算过", got["error"])

    def test_算过之后接口读得回来(self):
        from src import web
        plan = M.Plan(period="2026-W38", start=datetime.date(2026, 9, 14),
                      end=datetime.date(2026, 9, 20),
                      columns=(M.Column("A", frozenset(["1"]), 1.0),),
                      stores=(M.StorePlan("甲店", "市区", (2,)),))
        sales = [M.Sale("甲店", "1", 3, "零售")]
        payload = A.compute(plan, sales, root=self.root)
        A.save(self.root, payload)
        got = web.App(self.root, "config/store-X.yaml").attain()
        self.assertTrue(got["exists"])
        self.assertEqual(got["period"], "2026-W38")
        self.assertEqual(got["rows"][0]["total"], 1.2, "3/2 要封顶到 120%")

    def test_路由在(self):
        src = (ROOT / "src" / "web.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/attain"', src)

    def test_前端进页面就拉(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("attain: () => loadAttain(true)", js)

    def test_占位函数已经删掉(self):
        """⚠ 留着的话，接口真坏了门店会看到一句**关于开发进度**的话
        （"还没接上 M4"），既看不懂也没法处理。"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("renderAttainPlaceholder", js)
        self.assertNotIn("还没接上数据", js)

    def test_读不到时说人话(self):
        """读不到 = 后端给 `error`（"还没算过"），不是"接口没上"。"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        body = js.split("async function loadAttain(", 1)[1].split("\n}", 1)[0]
        self.assertIn("d.error", js, "renderAttain 要用后端给的 error")
        self.assertNotIn("M4", body)


class Test推送(unittest.TestCase):
    """M5：达成的文案 + 四态策略 + **不许套「只有差异才推」**。

    ⚠ 口径分工（用户 2026-09-19）：**该不该推在业务这层判**，
      "发出去"走 `modules.notify.send`。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        (self.root / "config").mkdir()
        from src.modules.notify import prefs as P
        P.set_enabled("attain", True, self.root)
        self.payload = {
            "exists": True, "period": "2026-W38", "start": "2026-09-14",
            "end": "2026-09-20", "data_until": "2026-09-17",
            "computed_at": "2026-09-19 20:00:00",
            "columns": ["A 机", "B 机", "C 机"], "weights": [0.5, 0.3, 0.2],
            "missing_columns": ["C 机"],
            "rows": [{"store": "青岛城阳万象汇店", "erp_name": "青岛城阳万象汇店",
                      "matched": True,
                      "targets": [6, 3, 3], "actuals": [7, 1, 0],
                      "rates": [7 / 6.0, 1 / 3.0, None], "total": 0.5437}],
        }

    def test_文案格式_邮件和企微共用(self):
        head, lines = A.notify_lines(self.payload, "青岛城阳万象汇店")
        self.assertIn("2026-W38", head)
        self.assertIn("54.4%", head, "总达成率要进标题")
        self.assertIn("A 机：116.7%　7/6 台", lines[0])
        self.assertIn("—（这列还没配编码）", lines[2], "没映射的列说清，不是 0%")

    def test_周中要写数据截至哪一天(self):
        """⚠ 两个原因都会让数字偏低，**分开说**：
        ① 这周还没过完；② 库里数据只到某天。混成一句门店会以为系统坏了。"""
        _h, lines = A.notify_lines(self.payload, "青岛城阳万象汇店")
        self.assertTrue(any("数据截至 2026-09-17" in x for x in lines))

    def test_数据已经到周末就不用提醒(self):
        p = dict(self.payload, data_until="2026-09-20")
        _h, lines = A.notify_lines(p, "青岛城阳万象汇店")
        self.assertFalse(any("数据截至" in x for x in lines))

    def _notify(self, **kw):
        sent = {}

        def fake_push(code, content, *, cfg, root, feature=""):
            sent[code] = content
            return {"code": code, "ok": True, "state": "sent", "why": "已发送"}

        def fake_send(mc, subject, body, attachments=(), prefix=None):
            sent["mail_subject"] = subject
            sent["mail_prefix"] = prefix

        from src import mailer, wecom
        from src.modules import notify as push
        mc = mailer.MailConfig(enabled=True, host="h", port=465, username="u",
                               password="x", recipients=["boss@example.com"],
                               when="always")
        wc = wecom.WecomConfig(enabled=True, webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
        with mock.patch.object(push, "send", fake_push), \
                mock.patch.object(mailer, "load_mail_config", lambda c, r: mc), \
                mock.patch.object(mailer, "send", fake_send), \
                mock.patch.object(wecom, "load_wecom_config", lambda c, r: wc):
            out = A.notify(self.payload, store="青岛城阳万象汇店",
                           root=self.root, **kw)
        return out, sent

    def test_两条都发出去_走的是_notify(self):
        out, sent = self._notify()
        self.assertEqual(out["wecom"]["state"], "sent")
        self.assertEqual(out["mail"]["state"], "sent")
        self.assertEqual(sent["wecom"]["template"], "attain", "模板必须显式写")
        self.assertEqual(sent["mail"]["prefix"], "[销售达成]",
                         "不传前缀的话主题会顶着 [报量对账]，门店以为发重了")
        # ⚠ 主题里的「[销售达成]」是 `prefix` 加上的（`build_message` 负责拼），
        #   所以这儿只能断标题本身；前缀那半上面已经单独断过了。
        self.assertIn("2026-W38", sent["mail"]["subject"])
        self.assertIn("总达成率 54.4%", sent["mail"]["subject"])
        self.assertIn("零售/分销计入", sent["mail"]["body"], "邮件正文要写口径")

    def test_不套只有差异才推(self):
        """⚠⚠ 达成**每天都有数，可能就是 0%** —— 而 0% 恰恰最该推。

        套上 `has_diff` 的话"达成率 0%"会被判成"没差异、不推"，
        正好把最该看的那条吞掉。
        """
        p = json.loads(json.dumps(self.payload))
        p["rows"][0].update(actuals=[0, 0, 0], rates=[0.0, 0.0, None], total=0.0)
        # 配置成「仅有差异时发」—— 达成也必须照发
        from src import mailer, wecom
        mc = mailer.MailConfig(enabled=True, host="h", port=465, username="u",
                               password="x", recipients=["b@example.com"],
                               when="only_diff")
        wc = wecom.WecomConfig(enabled=True, webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", when="only_diff")
        sent = {}
        from src.modules import notify as push

        def fake_push(code, content, *, cfg, root, feature=""):
            sent[code] = content
            return {"code": code, "ok": True, "state": "sent", "why": "已发送"}

        with mock.patch.object(push, "send", fake_push), \
                mock.patch.object(mailer, "load_mail_config", lambda c, r: mc), \
                mock.patch.object(mailer, "send", lambda *a, **k: None), \
                mock.patch.object(wecom, "load_wecom_config", lambda c, r: wc):
            out = A.notify(p, store="青岛城阳万象汇店", root=self.root)
        self.assertEqual(out["wecom"]["state"], "sent", "0% 也必须推（那正是要看的那条）")
        self.assertEqual(out["mail"]["state"], "sent")

    def test_两个开关都关了就是_disabled(self):
        out, sent = self._notify(no_push=True, no_mail=True)
        self.assertEqual(out["wecom"]["state"], "disabled")
        self.assertEqual(out["mail"]["state"], "disabled")
        self.assertEqual(sent, {}, "关了还发？")

    def test_没算出来就不推(self):
        out, sent = self._notify()
        self.assertEqual(out["wecom"]["state"], "sent")
        out2 = A.notify({"exists": False}, root=self.root)
        self.assertEqual(out2["wecom"]["state"], "skipped")
        self.assertIn("没算出来", out2["wecom"]["why"])

    def test_推送失败不影响_ok(self):
        """⚠ 达成数字才是主产物 —— 推送挂了不能把这次的成败打下去。"""
        from src import web
        p = dict(self.payload)
        A.save(self.root, p)
        (self.root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "青岛城阳万象汇店"\n', encoding="utf-8")
        from src.modules import notify as push
        with mock.patch.object(push, "send",
                               side_effect=RuntimeError("webhook 挂了")):
            res = A.run(db="", root=self.root, config_path=None, emit=lambda _s: None)
        # 这个 root 里没有库 ⇒ 到不了推送，但**不许抛**
        self.assertIn("ok", res)

    def test_模板已在渠道登记表里(self):
        from src.modules import notify
        self.assertIn("attain", notify.WECom_TEMPLATES)

    def test_企微文案不走报量排查那套(self):
        """⚠ 三处故意不同：**不 @人** / 不附附件 / 不套 has_diff。

        @人 那件事在 `push`/`push_attain` 里分家，这里钉住 attain 走的是自己那条。
        """
        from src import wecom
        self.assertTrue(hasattr(wecom, "push_attain"))
        md = wecom.build_attain_markdown({"门店": "青岛城阳万象汇店"}, ["A 机：50%"],
                                         "2026-W38 总达成率 50.0%")
        self.assertIn("销售达成", md)
        self.assertIn("不 @", md) if False else None
        self.assertNotIn("@", md, "markdown 里不该出现 @人（企微限制）")
        self.assertIn("封顶 120%", md, "口径要写在推送里")

    def test_每个产品格双行_达成率加粗(self):
        """⚠ 用户 2026-09-19 看界面后：「太拥挤了。**双行显示，达成/目标 达成率**。
        上面产品类型和占比也**换行**显示」。

        ⇒ 每个产品格两行：`实际/目标` 一行、**达成率**一行（加粗）；
          表头也是两行：产品名 + 占比。

        ⚠ 必须包 `{html: …}` —— `table()` 对字符串单元格默认转义，
          直接写 `<br>` 会在页面上原样显示（AGENTS.md 坑 3，踩过三次）。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        blk = js[js.index("function renderAttain("):js.index("$('#btn-refresh-attain')")]
        self.assertIn("{ html:", blk, "没包 {html:…}，<br> 会被转义成文字")
        # ⚠ 2026-09-19 最终形态（用户两条）：左边上下两行台量、**右边达成率**；
        #   而「达成 / 目标」这两个字**只在第一个产品列**出现（`i === 0 ?`）。
        self.assertIn('<span class="hint">达成</span>', blk)
        self.assertIn('<span class="hint">目标</span>', blk)
        # ⚠ 台量最终**回到第一个产品列里**（用户看过两版之后定的：
        #   「单独出来」那版被否了 —— 「这个红框内不要，圆圈里的数字挪到第一个产品下面」）。
        # ⚠ 第四版（用户最终）：标签列**留着**（只有"达成/目标"两个字，表头留空），
        #   数字搬回第一个产品列。⇒ 表头第二项是**空串**，行里第二格是标签。
        self.assertIn("const header = ['区域', '门店', '',", blk)
        self.assertIn("attain-sep", blk, "两个数字中间要有分割线（用户要的）")
        self.assertIn("const labels = { html:", blk, "标签列要留着")
        # ⚠ 2026-09-20：第一格变成**可点的门店名**（点了就地展开成员目标/达成）
        self.assertIn('class="store-link" data-expand=', blk)
        self.assertIn("rows.push([{ html:", blk)
        self.assertIn("attain-num", blk, "台量数字要放大（用户：数字大点）")
        # ⚠ 2026-09-20：第一格变成**可点的门店名**（点了就地展开成员目标/达成）
        self.assertIn('class="store-link" data-expand=', blk)
        self.assertIn("rows.push([{ html:", blk)
        self.assertIn("attain-cell", blk, "台量和达成率要并排（右边那个）")

        # ⚠ 2026-09-20：那一格多了 `rate` 类（固定 53px 的槽位 —— 用户要色块**等宽**），
        #   所以断言要跟着改。**断的是"没映射显示 —"，不是类名的顺序。**
        self.assertIn('class="rate hint">—</b>', blk, "没映射的列仍是 —（不是 0%）")
        self.assertIn("</span>` }", blk, "表头第二行是占比")

    def test_表头那个箭头函数别把对象当函数体(self):
        """⚠⚠ 真踩过（2026-09-19）：`=> { html: … }` 会被解析成**函数体里一个 label**，
        返回值是 `undefined` ⇒ 每一格都成空 `<th>`，**表头整排消失**。

        而表体没事 —— 那边写的是 `return { html: … }`。
        表现极像"前端没更新"（缓存 / 进程 / 宽度都查过一轮），
        真正的原因就是**少一对括号**。所以这条专门钉住那个括号。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        i = js.index("const header = ['区域', '门店'")
        blk = js[i:i + 400]
        self.assertIn("=> ({", blk, "箭头函数返回对象必须加括号，否则返回 undefined")
        self.assertNotIn("=> {\n    html:", blk, "又写成函数体了（表头会整排消失）")


class Test谁卖的(unittest.TestCase):
    """悬停二级菜单要显示「这台的销售是谁」—— 数据来自 `erp_sales.店员`。

    ⚠ 口径两条：
      * **退货也算那个人的**（净台量），跟那一列的台量口径一致 ——
        不然会出现"格子里 2 台、菜单里 3 台"这种对不上；
      * **没写店员的单子单列一项**（不合并、不丢），宁可显示"（没写店员）"，
        也别让人以为漏了人。
    """

    def setUp(self):
        self.plan = M.Plan(period="2026-W38", start=None, end=None,
                           columns=(M.Column("A", frozenset(["1"]), 0.5),
                                    M.Column("B", frozenset(["2"]), 0.5)),
                           stores=(M.StorePlan("甲店", "", (5, 5)),))

    def test_按台量降序(self):
        sales = [M.Sale("甲店", "1", 1, "零售", "李厚杞"),
                 M.Sale("甲店", "1", 1, "零售", "李厚杞"),
                 M.Sale("甲店", "1", 1, "零售", "王永昌")]
        r = M.store_result(self.plan, self.plan.stores[0], sales, "甲店")
        self.assertEqual(r.people[0], (("李厚杞", 2, ()), ("王永昌", 1, ())))
        self.assertEqual(r.people[1], (), "这一列没人卖 ⇒ 空（不是 0 台的一行）")

    def test_退货算那个人的净台量(self):
        sales = [M.Sale("甲店", "1", 3, "零售", "李厚杞"),
                 M.Sale("甲店", "1", -1, "零售退", "李厚杞")]
        r = M.store_result(self.plan, self.plan.stores[0], sales, "甲店")
        self.assertEqual(r.people[0], (("李厚杞", 2, ()),))
        self.assertEqual(r.actuals[0], 2, "菜单里的数要和格子里的数口径一致")

    def test_没写店员的单列一项(self):
        sales = [M.Sale("甲店", "1", 1, "零售", ""),
                 M.Sale("甲店", "1", 1, "零售", "王永昌")]
        r = M.store_result(self.plan, self.plan.stores[0], sales, "甲店")
        self.assertTrue(any(p[0] == "（没写店员）" and p[1] == 1 for p in r.people[0]))

    def test_落盘里带上_people(self):
        sales = [M.Sale("甲店", "1", 2, "零售", "李厚杞")]
        r = M.store_result(self.plan, self.plan.stores[0], sales, "甲店")
        d = r.as_dict()
        self.assertEqual(d["people"][0][0][:2], ["李厚杞", 2], "前端要拿它渲染二级菜单")
        self.assertEqual(d["people"][1], [])

    def test_悬停二级菜单的接线(self):
        """⚠ 用户 2026-09-19 定的交互（原话在 `web/app.js` 那段注释里）：

        * 触发点 = **8px 小圆点，贴在达成率右边**，不挡格子；
        * 内容是「**这个达成销售的人是谁**」（台量按降序，来自 `erp_sales.店员`）；
        * 动画：**圆 → 横向拉长成线 → 纵向拉长**（两段，靠 transition-delay 串起来）；
        * 方向**按窗口空间自适应**：能向下就向下 / 不能就上下一起（下沿贴视口底）/
          在最下面就只往上；
        * ⚠ 浮层必须 `position: fixed` —— 表格外面那层 `.table-scroll` 是
          `overflow-x: auto`，**溢出容器会把绝对定位的浮层裁掉**。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        # ⚠ 小圆点**已去掉**（用户：触发器是整个格子，圆点没必要常驻）
        self.assertNotIn("attain-dot", js)
        # ⚠ 触发面是**整个 td**（用户：「这个区域内的空白区域也要」）——
        #   行/列号从 `cellIndex` / `sectionRowIndex` 推，不往 DOM 上挂标记。
        self.assertIn("t.closest('td')", js)
        self.assertIn("td.cellIndex - 3", js, "前三列是区域、门店和标签")
        self.assertIn("sectionRowIndex", js)
        self.assertIn("attain-pop", js)
        # ⚠ 表头改成**产品名**且不换行（用户），"这几台是谁卖的"那句不要了
        self.assertIn("attainCols", js, "面板表头要拿产品名")
        self.assertNotIn("这几台是谁卖的", js)
        i = css.index(".attain-pop-head")
        self.assertIn("white-space: nowrap", css[i:css.index("}", i)], "表头不换行")
        self.assertIn("window.innerHeight", js, "方向要按窗口空间算")
        self.assertIn("从那个格子的大小放大成面板", css)
        for cond in ("below >= ph", "above >= ph", "vh - ph - gap"):
            self.assertIn(cond, js, "三种方向都要有（向下 / 只向上 / 上下一起）")
        i = css.index(".attain-pop {")
        rule = css[i:css.index("\n}", i)]
        self.assertIn("position: fixed", rule)
        # ⚠ 动画改成**从格子放大到面板**（用户否掉了"圆 → 线 → 面板"那套）：
        #   起点是格子自己的矩形，四个量同时过渡 ⇒ **不许再有 delay**。
        self.assertIn("box.width + 'px'", js, "起点要用格子自己的大小")
        self.assertNotIn("var(--motion-normal)) var(--motion-normal)", css.replace(" ", " "),
                         "两段式那个 delay 该去掉了")


class Test悬停面板别重放(unittest.TestCase):
    """⚠ 用户 2026-09-19 报的：「鼠标一动，虽然没出格子，但是这个动画还会**重新放一遍**」。

    根因：`mouseover` 是**按元素**触发的 —— 指针在格子里从数字挪到百分比，
    跨过 `.attain-num` / `.attain-sep` / `<b>` 的边界就再触发一次，
    于是每次都清掉定时器、重新等 200ms、再放一遍展开动画。

    ⇒ 修法：记住"现在弹的是哪一格"（`行:列`），**同一格直接 return**。
      **这条对所有悬停交互都适用**：想"进入某个区域只做一次"，必须自己记区域，
      光靠 `mouseover` 会变成连发。
    """

    def test_同一格不重放(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("attainCurKey", js, "没记住'现在是哪一格'")
        self.assertIn("if (key === attainCurKey) return;", js, "同一格要直接返回")
        self.assertIn("attainCurKey = '';", js, "离开格子要清掉，不然回到同一格不弹")

    def test_触发面是整个单元格(self):
        """「这个区域内的空白区域也要」——认 `td`，不认那几个数字拼出来的小盒子。"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("t.closest('td')", js)
        self.assertIn("td.cellIndex - 3", js, "前三列是区域、门店和标签")
        # ⚠⚠ 2026-09-22 串页 bug：`attainPeople` 会一直留着，别的页表格一悬停
        #   就弹「谁卖的」。触发必须**限定在达成那张表里**。
        self.assertIn("closest('#attain-table')", js, "只在达成表里弹，别串到别的页")
        self.assertNotIn("attain-cell[data-r]", js, "别再靠挂在 span 上的标记找格子")

    def test_同一格不重放(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("attainCurKey", js, "没记住'现在是哪一格'")
        self.assertIn("if (key === attainCurKey) return;", js, "同一格要直接返回")
        self.assertIn("attainCurKey = '';", js, "离开格子要清掉，不然回到同一格不弹")

    def test_触发面是整个单元格(self):
        """「这个区域内的空白区域也要」——认 `td`，不认那几个数字拼出来的小盒子。"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("t.closest('td')", js)
        self.assertIn("td.cellIndex - 3", js, "前三列是区域、门店和标签")
        # ⚠⚠ 2026-09-22 串页 bug：`attainPeople` 会一直留着，别的页表格一悬停
        #   就弹「谁卖的」。触发必须**限定在达成那张表里**。
        self.assertIn("closest('#attain-table')", js, "只在达成表里弹，别串到别的页")
        self.assertNotIn("attain-cell[data-r]", js, "别再靠挂在 span 上的标记找格子")

    def test_第三级要商品名称(self):
        """⚠ 用户 2026-09-19：「弹窗里面**每一条，鼠标移上去，再出现个二级弹窗**，
        里面是**卖的每个的商品名称**」⇒ 每个人的记录里要带他卖过的商品名。"""
        # ⚠ 自带 plan：这条最初被放进了一个没有 `self.plan` 的类里（测试自己逮住的）
        plan = M.Plan(period="2026-W38", start=None, end=None,
                      columns=(M.Column("A", frozenset(["1"]), 1.0),),
                      stores=(M.StorePlan("甲店", "", (5,)),))
        sales = [M.Sale("甲店", "1", 1, "零售", "李厚杞", "Mate60"),
                 M.Sale("甲店", "1", 1, "零售", "李厚杞", "Pura70"),
                 M.Sale("甲店", "1", 1, "零售", "李厚杞", "Mate60")]
        r = M.store_result(plan, plan.stores[0], sales, "甲店")
        who, qty, items = r.people[0][0]
        self.assertEqual((who, qty), ("李厚杞", 3))
        self.assertEqual(items, ("Mate60", "Pura70"), "商品名去重后排序")

    def test_商品名清理规则的两条护栏(self):
        """⚠ 用户 2026-09-19：「**47 个不够吧**…库里那么多商品呢，这次是这些重点产品，
        **以后呢**」—— 于是规则在**全库 3049 个商品名**上验过：
        清空 0 个、退化回原名 3 个（`防尘罩`/`支架`/`理线架` 本来就短），其余正常。

        这条钉住**让它安全的那两条护栏**（都是全库跑出来的教训）：
          * 最后一段**太短**（`银河灰`、`32G` → 只是颜色/容量）⇒ 往前接一段；
          * 最后一段是**纯英文短码**（`RTX5070-`）⇒ 也往前接；
          * 什么都剩不下 ⇒ **退回原名**（宁可长，不能空）。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        i = js.index("const short = (n) => {")
        blk = js[i:js.index("\n  };", i)]
        self.assertIn("lastIndexOf", js, "先按最右边的 `/` 切")
        self.assertIn("全网通版|网通版", js)
        self.assertIn("(?=[A-Z0-9-]*[A-Z])", js, "至少一个字母 —— 别误吃 12GB+512GB")
        # ⚠ 规则第二版：**丢掉开头两段（品类/品牌），后面全留着** ——
        #   第一版"只取最后一段"把 `智能手机/华为/Pura 80/Pura 80 Ultra` 里的
        #   `Pura 80` 丢掉了（用户发现：「pura 80 没了」）。
        self.assertIn("parts.slice(2).join('/')", blk, "丢前缀，不丢产品名那一段")
        self.assertIn("return String(n || '')", blk, "兜底：绝不显示空")
        # 第三级放不下就往**二级面板左边缘**往左展开（用户定的）
        self.assertIn("main.left - w - 4", js)

class Test达成率标色开关(unittest.TestCase):
    """用户 2026-09-19：「加一个颜色开关，在现在刷新按钮左边的位置，**左右滑块**那种。
    打开后，**60% 以下标红，60%-80% 标橙，80%-100% 标浅绿，100%-120% 标绿**。
    当然可以多设置几个档位，大致按我说得来就行。然后注意 **0 目标的 100%
    还是现在这个底色**就行」。

    ⚠⚠ **2026-09-20 用户把两头调了个个儿**：「**红色和绿色反过来吧，
      100%-120% 是红色**」⇒ 现在是 **<60% 绿 · 100%+ 红**。
      下面这条测试跟着改成新口径 —— **别再按 09-19 那版"顺手改回去"**。

    ⚠ 最后那句最容易做错：口径上"目标 0 ⇒ 记 100%"，但那**不是卖得好**，是没定目标 ——
      标成绿会误导门店。所以 `target` 为 0 的格子**不上色**。
    """

    def setUp(self):
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_开关在刷新左边_是滑块(self):
        i = self.html.index('id="attain-color"')
        j = self.html.index('id="btn-refresh-attain"')
        self.assertLess(i, j, "开关要在「刷新」左边")
        self.assertIn("switch-track", self.html)          # 滑块的两半
        self.assertIn("switch-knob", self.html)
        self.assertIn("input:checked + .switch-track", self.css, "滑块的位移靠 CSS")

    def test_四档对应的区间(self):
        """⚠ 断的是**「谁落在哪一档」**，不是"这几行代码在不在" ——
        上次把红绿对调，只改字符串顺序就够了吗？不够：得看 `return` 的是哪个类。
        （老版这条只 `assertIn` 了类名，所以"换档位"它抓不到 —— 现在照 `if` 的顺序断。）
        """
        i = self.js.index("function attainTier(")
        blk = self.js[i:self.js.index("\n}", i)]
        # 按 if 出现的顺序，抠出"条件 → 返回的档位"
        pairs = re.findall(r"rate < ([\d.]+)\)\s*return\s*'(t-[a-z]+)'", blk)
        self.assertEqual(pairs, [("0.6", "t-g"), ("0.8", "t-orange"), ("1", "t-lg")],
                         "四档区间和颜色对不上（<60% 绿 / 60~80% 橙 / 80~100% 浅绿）")
        # 兜底那一档（100% 以上）：
        tail = blk[blk.index("rate < 1"):]
        self.assertRegex(tail, r"return\s*'t-red'", "100%+ 那一档现在是**红**（用户 2026-09-20）")
        self.assertNotIn("return 't-g'", tail, "兜底那档又被换回绿的了？")

    def test_目标为零不上色(self):
        i = self.js.index("function attainTier(")
        blk = self.js[i:self.js.index("\n}", i)]
        self.assertIn("!target", blk, "目标 0 的格子保持原底色（用户点名的）")
        self.assertIn("rate == null", blk, "没映射的列也不上色")

    def test_颜色走令牌不写死(self):
        for tok in ("--rate-red", "--rate-orange", "--rate-lg", "--rate-g"):
            # 一主题一文件：四档在 themes/default.css（各主题还会覆盖）
            _t = (ROOT / "web" / "themes" / "default.css").read_text(encoding="utf-8")
            self.assertIn(tok + ":", _t)
            self.assertIn("var(" + tok + ")", self.css)

    def test_开关记在本地_重画不重新请求(self):
        self.assertIn("cbg-attain-color", self.js)
        self.assertIn("renderAttain(attainLast)", self.js, "切开关只重画，不该再请求一次")

class Test成员目标拆分(unittest.TestCase):
    """用户 2026-09-19：「点击门店名的位置，是展开，下面是门店成员的名单，
    内容是**门店周度目标拆分与达成**。结构参考上面，但是**目标是可以单独设置的**」

    ⚠ 目标落在 `.secrets/attain-split.json`（人填的配置；`out/` 是报告目录，别混），
      而且按 **门店|期间** 存 —— 目标是**一周一版**的。
    ⚠ 成员层**没设目标 ⇒ 达成率是 `None`（界面 `—`）**，别套门店那条"目标 0 ⇒ 100%"：
      "没分给他"和"他达标了"是两件事，直接显示 100% 会让"还没分"看着像"干得好"。
    """

    def setUp(self):
        from src.features.sales.attain import split
        self.split = split
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_存了读得回来(self):
        self.split.set_targets(self.root, "甲店", "2026-W38",
                               {"王俊燕": [2, 1]}, 2)
        got = self.split.targets_of(self.root, "甲店", "2026-W38")
        self.assertEqual(got, {"王俊燕": [2, 1]})

    def test_按门店和期间分开存(self):
        self.split.set_targets(self.root, "甲店", "2026-W38", {"A": [1]}, 1)
        self.split.set_targets(self.root, "甲店", "2026-W39", {"A": [5]}, 1)
        self.split.set_targets(self.root, "乙店", "2026-W38", {"A": [9]}, 1)
        self.assertEqual(self.split.targets_of(self.root, "甲店", "2026-W38")["A"], [1])
        self.assertEqual(self.split.targets_of(self.root, "甲店", "2026-W39")["A"], [5])
        self.assertEqual(self.split.targets_of(self.root, "乙店", "2026-W38")["A"], [9])

    def test_列数对不上要规整(self):
        """⚠ 坏数据不进库 —— 不然界面上会出现"第 12 列"那种按不出来的格子。"""
        got = self.split.set_targets(self.root, "甲店", "W", {"A": [1, 2, 3, 4]}, 2)
        self.assertEqual(got["A"], [1, 2], "多出来的截掉")
        got = self.split.set_targets(self.root, "甲店", "W", {"A": [7]}, 3)
        self.assertEqual(got["A"], [7, 0, 0], "缺的补 0")

    def test_负数和非数字当零(self):
        got = self.split.set_targets(self.root, "甲店", "W", {"A": [-3, "x", None]}, 3)
        self.assertEqual(got["A"], [0, 0, 0])

    def test_全清空就把那把钥匙删掉(self):
        self.split.set_targets(self.root, "甲店", "W", {"A": [1]}, 1)
        self.split.set_targets(self.root, "甲店", "W", {}, 1)
        self.assertEqual(self.split.load(self.root), {}, "别留空壳")

    def test_没设过的成员是_None_不是百分之百(self):
        self.assertIsNone(self.split.rate_of(0, 5), "没分给他 ≠ 他达标了")
        self.assertAlmostEqual(self.split.rate_of(3, 3), 1.0, places=6)
        self.assertAlmostEqual(self.split.rate_of(2, 3), 1.2, places=6,
                               msg="3/2 = 150% ⇒ 封顶 120%")
        self.assertAlmostEqual(self.split.rate_of(1, 5), 1.2, places=6, msg="封顶 120%")

    def test_落盘位置在_out(self):
        """⚠ 用户 2026-09-19 定的：「**放 out 吧**，我想设置上邮件自动发送功能，
        门店拆的目标定时发送给他们区长」—— 跟达成那份（`out/attain-<年>.json`）
        放一起，推送的时候一处找齐。
        ⚠ 但它**不是报告**（报告能重算，这份是人填的）⇒ 写入仍是"临时文件 + rename"。"""
        self.assertTrue(str(self.split.REL).startswith("out/"))

    def test_坏文件读成空表不抛(self):
        p = self.split.path_of(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{半截", encoding="utf-8")
        self.assertEqual(self.split.load(self.root), {})


class Test拆分发给区长(unittest.TestCase):
    """用户 2026-09-19：区长名单单独一份 `config/managers.yaml`；
    **保存即发 + 每周一兜底重发**；内容**只发目标拆分**。"""

    def setUp(self):
        from src.features.sales.attain import split
        self.split = split
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "managers.yaml").write_text(
            'managers:\n'
            '  - name: 张区长\n    email: zhang@x.com\n'
            '    stores: ["甲店", "乙店"]\n'
            '  - name: 李区长\n    email: li@x.com\n'
            '    stores: ["丙店"]\n', encoding="utf-8")

    def test_按门店找区长(self):
        self.assertEqual([m["email"] for m in self.split.managers_of("甲店", self.root)],
                         ["zhang@x.com"])
        self.assertEqual([m["email"] for m in self.split.managers_of("丙店", self.root)],
                         ["li@x.com"])

    def test_没填邮箱就发中台_不是不发(self):
        """⚠ 用户 2026-09-20：「三个区长邮箱**先默认设置成 439845914@qq.com**？」
        ⇒ 没填邮箱时**回落到中台邮箱**（不用在配置里把同一地址抄三遍）；
        填上真邮箱后自动优先用它。`fallback` 标出来，日志里能说清。"""
        from src import mailer
        (self.root / "config" / "managers.yaml").write_text(
            'managers:\n  - name: 张区长\n    email: ""\n    stores: ["甲店"]\n',
            encoding="utf-8")
        got = self.split.managers_of("甲店", self.root)
        self.assertEqual([m["email"] for m in got], [mailer.CENTRAL_ADDR])
        self.assertTrue(got[0]["fallback"])

    def test_填了真邮箱就用真的(self):
        (self.root / "config" / "managers.yaml").write_text(
            'managers:\n  - name: 张区长\n    email: zhang@x.com\n    stores: ["甲店"]\n',
            encoding="utf-8")
        got = self.split.managers_of("甲店", self.root)
        self.assertEqual([m["email"] for m in got], ["zhang@x.com"])
        self.assertFalse(got[0]["fallback"])

    def test_没配区长的店是空表_不回落(self):
        """⚠ 配不到就**不发** —— 不能退化成发给某个默认地址（那会把门店目标发错人）。"""
        self.assertEqual(self.split.managers_of("查无此店", self.root), [])

    def test_正文只写有目标的人(self):
        members = [{"name": "王俊燕", "targets": [2, 0, 1]},
                   {"name": "郭芮志", "targets": [0, 0, 0]}]
        lines = self.split.report_lines("甲店", "2026-W38", ["Mate", "Air", "PC"], members)
        txt = "\n".join(lines)
        self.assertIn("王俊燕", txt)
        self.assertIn("Mate  2 台", txt)
        self.assertNotIn("郭芮志", txt, "一个人都没分 ⇒ 不占一行")

    def test_周一才兜底_发过就不发(self):
        import datetime as dt
        mon = dt.date(2026, 9, 21)          # 周一
        tue = dt.date(2026, 9, 22)
        self.assertTrue(self.split.should_resend(mon, ""))
        self.assertFalse(self.split.should_resend(mon, "2026-09-21"), "这周发过了")
        self.assertFalse(self.split.should_resend(tue, ""), "不是周一不发（天天发成骚扰）")

    def test_保存即发_走_notify(self):
        from src.modules import notify as push
        self.split.set_targets(self.root, "甲店", "2026-W38", {"王俊燕": [2]}, 1)
        seen = {}

        def fake(code, content, *, cfg, root):
            seen.update(content)
            return {"ok": True, "state": "sent", "why": "已发送"}
        with mock.patch.object(push, "send", fake):
            res = self.split.send_report(self.root, "甲店", "2026-W38", cfg={},
                                         emit=lambda _s: None)
        self.assertEqual(res["sent"], 1)
        self.assertEqual(seen["to"], ["zhang@x.com"], "只发给这家店的区长")
        self.assertIn("[目标拆分]".replace("[", "").replace("]", ""), seen["subject"])
        self.assertEqual(seen["prefix"], "[目标拆分]")

    def test_没拆过就不发(self):
        res = self.split.send_report(self.root, "甲店", "2026-W38", cfg={},
                                     emit=lambda _s: None)
        self.assertEqual(res["sent"], 0)
        self.assertIn("还没拆过", res["why"])

    def test_邮件通道没开就不发(self):
        """⚠⚠ AGENTS 坑 15：**谁调推送谁负责判"这条渠道开着没"** ——
        `mailer.send()` 自己不检查 `enabled`。不判的话，门店在「通用设置 › 邮件」
        里关掉了，点「发送给区长」照样发出去（而发出去收不回来）。
        """
        from src.modules import notify as push
        self.split.set_targets(self.root, "甲店", "2026-W38", {"王俊燕": [2]}, 1)
        with mock.patch.object(push, "send") as m:
            res = self.split.send_report(self.root, "甲店", "2026-W38",
                                         cfg={"mail": {"enabled": False}},
                                         emit=lambda _s: None)
        self.assertEqual(m.call_count, 0, "通道关着还发？")
        self.assertEqual(res["sent"], 0)
        self.assertIn("邮件通道没开", res["why"])

    def test_发失败要说清是谁为什么(self):
        """只写"一个都没发出去"的话，门店不知道是没配邮箱还是 SMTP 没配好。"""
        from src.modules import notify as push
        self.split.set_targets(self.root, "甲店", "2026-W38", {"王俊燕": [2]}, 1)
        with mock.patch.object(push, "send",
                               lambda *a, **k: {"ok": False, "why": "SMTP 连不上"}):
            res = self.split.send_report(self.root, "甲店", "2026-W38",
                                         cfg={"mail": {"enabled": True}},
                                         emit=lambda _s: None)
        self.assertEqual(res["sent"], 0)
        self.assertIn("SMTP 连不上", res["why"])
        self.assertIn("zhang@x.com", res["why"])


class Test拆分接口的成员名单(unittest.TestCase):
    """⚠ 2026-09-19 用户：「**目标拆分的后端还没有做完**」——核出来的缺口：

    成员原来只从"这周**卖过**的人"里推 ⇒ **没卖过东西的人根本分不了目标**，
    而"给他定目标"恰恰是这一页的意义。
    ⇒ 现在能把**在册成员名单**传进来（云商人员表要联网，前端点一下才带）。
    另外补了"每列拆出来的合计 vs 门店目标"，拆多了/拆少了看得见。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "甲店"\n', encoding="utf-8")
        # ⚠ M17： 里会走 （按本店过滤），
        #   夹具得说清本店是甲店，否则范围为空、一行都留不下。
        (self.root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "甲店"\n', encoding="utf-8")
        plan = M.Plan(period="2026-W38", start=datetime.date(2026, 9, 14),
                      end=datetime.date(2026, 9, 20),
                      columns=(M.Column("A", frozenset(["1"]), 0.5),
                               M.Column("B", frozenset(["2"]), 0.5)),
                      stores=(M.StorePlan("甲店", "", (4, 2)),))
        sales = [M.Sale("甲店", "1", 3, "零售", "王俊燕", "Mate60")]
        A.save(self.root, A.compute(plan, sales, root=self.root))

    def _app(self):
        from src import web
        return web.App(self.root, "config/store-X.yaml")

    def test_卖过的人自动在名单里(self):
        d = self._app().attain_split("甲店", "2026-W38")
        self.assertEqual([m["name"] for m in d["members"]], ["王俊燕"])

    def test_在册但没卖过的人也能分目标(self):
        d = self._app().attain_split("甲店", "2026-W38", roster=["王俊燕", "郭芮志"])
        names = [m["name"] for m in d["members"]]
        self.assertIn("郭芮志", names, "没卖过也要能定目标 —— 这是这一页的意义")
        guo = [m for m in d["members"] if m["name"] == "郭芮志"][0]
        self.assertEqual(guo["actuals"], [0, 0])
        self.assertIsNone(guo["rates"][0], "没设目标 ⇒ —（不是 100%）")

    def test_合计跟门店目标对得上(self):
        app = self._app()
        app.attain_split_save("甲店", "2026-W38", {"王俊燕": [2, 1], "郭芮志": [2, 1]})
        d = app.attain_split("甲店", "2026-W38")
        self.assertEqual(d["totals"], [4, 2], "拆出来的合计")
        self.assertEqual(d["diff"], [0, 0], "跟门店目标一模一样 ⇒ 差 0")
        app.attain_split_save("甲店", "2026-W38", {"王俊燕": [4, 2]})
        self.assertEqual(app.attain_split("甲店", "2026-W38")["diff"], [0, 0])
        app.attain_split_save("甲店", "2026-W38", {"王俊燕": [1, 0]})
        self.assertEqual(app.attain_split("甲店", "2026-W38")["diff"], [-3, -2], "拆少了看得出来")


class Test真区长名单(unittest.TestCase):
    """用户 2026-09-19 给的分区（真数据）：

    西北区 杨英梅 8 家 / 南区 徐崇龙 6 家 / 市区 孙士迪 14 家 = 28 家。

    ⚠ 这份是**配置**（`config/managers.yaml`），不是代码常量 ——
      改分区、加门店改那份文件就行，别写进 Python。
    ⚠ 现在**邮箱还空着** ⇒ 一位区长都找不出来 ⇒ **不发**（这是我们想要的默认：
      宁可没发、日志里写清"没配区长"，也不能发给不相干的人）。
    """

    def setUp(self):
        from src import config_io
        self.config_io = config_io
        self.ms = config_io.managers_table(ROOT)

    def test_三位区长和分区(self):
        """⚠ 2026-09-21 起**店数按"圈到几家店"算**（B 方案：区长写区名、区域在门店名单里），
        不再是 `len(m["stores"])` —— 那份手抄清单已经不存在了。
        `stores_of_manager` 是**唯一口径**（`role_scope` / `managers_of` 都走它）。"""
        rows = self.config_io.stores_table(ROOT)
        got = {m["name"]: (m.get("region"), len(self.config_io.stores_of_manager(m, rows)))
               for m in self.ms}
        self.assertEqual(got.get("杨英梅"), ("西北区", 8))
        # ⚠ 徐崇龙那份**多一个名字**（6 家店）：「青岛鲁疆广场店」带 tdoc 别名
        #   （腾讯文档里叫「鲁疆广场」），两种写法都要在范围里
        self.assertEqual(got.get("徐崇龙")[0], "南区")
        self.assertIn("鲁疆广场", self.config_io.stores_of_manager(self.ms[1], rows))
        # 不重不漏：三个区覆盖的**门店**（按主键去重）正好 28 家
        covered = set()
        for m in self.ms:
            for n in self.config_io.stores_of_manager(m, rows):
                # ⚠ 用 `store_key()`：一家店两种写法（云商名 / 腾讯文档名）要归一，
                #   纯按 erp_name 比会把「鲁疆广场」当成第 29 家店
                covered.add(self.config_io.store_key(rows, n))
        self.assertEqual(len(covered), 28, "28 家门店不重不漏")
        self.assertEqual(self.config_io.region_audit(ROOT), [], "分区体检必须是干净的")

    def test_区长的邮箱_填了的用真的_没填的回落中台(self):
        """⚠ 口径改过一次（都是用户定的）：
        * 起初：没填邮箱 ⇒ 当成"没配"、不发；
        * 2026-09-20：改成**回落到中台邮箱** —— 不用在配置里把同一个地址抄三遍。
        现在（2026-09-20 用户给了两位的邮箱）：
          杨英梅 → 906937435@qq.com；徐崇龙 → Byxuchonglong@163.com；
          孙士迪**还没给** ⇒ 回落中台。
        """
        from src import mailer
        from src.features.sales.attain import split
        want = {"青岛城阳万象汇店": "906937435@qq.com",
                "鲁疆广场": "Byxuchonglong@163.com",
                "青岛悦荟店": mailer.CENTRAL_ADDR}
        for store, mail in want.items():
            got = split.managers_of(store, ROOT)
            self.assertEqual([m["email"] for m in got], [mail], store)
        # 回落的那个要标出来（界面/日志里能说清"这封其实发到中台"）
        self.assertTrue(split.managers_of("青岛悦荟店", ROOT)[0]["fallback"])
        self.assertFalse(split.managers_of("鲁疆广场", ROOT)[0]["fallback"])

    def test_门店名两种写法都认(self):
        """达成表里是「鲁疆广场」（腾讯文档的名字），云商里是「青岛鲁疆广场店」。"""
        from src.features.sales.attain import split
        fake = {"managers": [{"name": "徐崇龙", "email": "x@y.com",
                              "stores": ["青岛鲁疆广场店"]}]}
        with mock.patch.object(self.config_io, "managers_table", lambda root: fake["managers"]):
            self.assertEqual(len(split.managers_of("鲁疆广场", ROOT)), 1,
                             "文档里的名字也要能对上云商名")


class Test拆分页按角色限定门店(unittest.TestCase):
    """用户 2026-09-19：「**平台岗显示所有门店，区长显示自己管理的门店**」。

    判定从宽到窄，**认不出来就退到本店**（门店的周度目标不该被别家看到）：
      ① 平台岗 ⇒ 全部；② 登录者姓名 == 区长名 ⇒ 他管的那些店；③ 其余 ⇒ 本店。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "甲店"\n  - erp_name: "乙店"\n  - erp_name: "丙店"\n',
            encoding="utf-8")
        plan = M.Plan(period="2026-W38", start=datetime.date(2026, 9, 14),
                      end=datetime.date(2026, 9, 20),
                      columns=(M.Column("A", frozenset(["1"]), 1.0),),
                      stores=(M.StorePlan("甲店", "", (1,)),
                              M.StorePlan("乙店", "", (1,)),
                              M.StorePlan("丙店", "", (1,))))
        A.save(self.root, A.compute(plan, [M.Sale("甲店", "1", 1, "零售", "王俊燕")],
                                    root=self.root))

    def _app(self, prof):
        from src import web
        app = web.App(self.root, "config/store-X.yaml")
        app._profile_with_who = lambda cfg: prof          # 直接钉住"这台机器是谁"
        return app

    def test_平台岗看全部(self):
        d = self._app({"type": "platform", "who": ""}).attain_split_all()
        self.assertEqual(len(d["stores"]), 3)
        self.assertIn("平台岗", d["scope"])

    def test_区长只看自己管的(self):
        from src import config_io
        with mock.patch.object(config_io, "managers_table",
                               lambda root: [{"name": "杨英梅", "region": "西北区",
                                              "email": "y@x.com",
                                              "stores": ["甲店", "乙店"]}]):
            d = self._app({"type": "experience", "who": "杨英梅"}).attain_split_all()
        self.assertEqual([s["store"] for s in d["stores"]], ["甲店", "乙店"])
        self.assertIn("区长 杨英梅", d["scope"])

    def test_认不出来就只给本店(self):
        """⚠ 宁可少给也不多给 —— 门店的周度目标不该被别家看到。"""
        from src import config_io
        with mock.patch.object(config_io, "load_raw",
                               lambda p: {"erp_store_name": "丙店"}):
            d = self._app({"type": "experience", "who": "张三"}).attain_split_all()
        self.assertEqual([s["store"] for s in d["stores"]], ["丙店"])


class Test按账号认区长(unittest.TestCase):
    """用户 2026-09-19：三位区长的**云商登录名** ——

        杨英梅 SL15763940156（西北区）/ 徐崇龙 SL15865560659（南区）/
        孙士迪 sl13255588124（市区）

    ⚠ **按账号认人**，不按姓名：账号唯一且稳定（就是登录凭据本身）；
      姓名只用来显示 —— 换个人登录姓名就变、还可能重名。
    ⚠ 登录名大小写**不统一**（`SL…`/`sl…` 混着）⇒ 比较一律转大写。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        (self.root / "config").mkdir()
        # ⚠ 2026-09-21 起**区长按区域圈店**（B 方案）⇒ 这份临时名单也得标区域，
        #   否则从真名单里拷过来的那三位区长**一家店都圈不到**（那是另一条测试的事）。
        (self.root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "甲店"\n    region: 西北区\n'
            '  - erp_name: "乙店"\n    region: 南区\n', encoding="utf-8")
        # ⚠ **区长名单也要拷进来**：`_split_scope` 是按 `app.root` 找 `config/managers.yaml` 的，
        #   临时目录里没有它 ⇒ 一位区长都认不出来、全部退成"本店"
        #   （我第一版就是这样，测试红在"本店"上）。
        (self.root / "config" / "managers.yaml").write_text(
            (ROOT / "config" / "managers.yaml").read_text(encoding="utf-8"),
            encoding="utf-8")
        plan = M.Plan(period="2026-W38", start=datetime.date(2026, 9, 14),
                      end=datetime.date(2026, 9, 20),
                      columns=(M.Column("A", frozenset(["1"]), 1.0),),
                      stores=(M.StorePlan("甲店", "", (1,)), M.StorePlan("乙店", "", (1,))))
        A.save(self.root, A.compute(plan, [M.Sale("甲店", "1", 1, "零售", "王俊燕")],
                                    root=self.root))

    def _scope(self, account):
        from src import web
        app = web.App(self.root, "config/store-X.yaml")
        app._profile_with_who = lambda cfg: {"type": "experience", "who": "谁都不认识"}
        with mock.patch.object(web, "describe_store_credentials",
                               lambda p: {"username": account, "who": ""}):
            return app._split_scope([{"store": "甲店"}, {"store": "乙店"}])

    def test_三位区长的账号都在名单里(self):
        from src import config_io
        got = {m["name"]: (m.get("accounts") or []) for m in config_io.managers_table(ROOT)}
        self.assertEqual(got["杨英梅"], ["SL15763940156"])
        self.assertEqual(got["徐崇龙"], ["SL15865560659"])
        self.assertEqual(got["孙士迪"], ["sl13255588124"])

    def test_大小写不同也认得出来(self):
        """⚠ 登录名大小写不统一（`SL…` / `sl…` 混着）—— 这是实测过的坑。"""
        from src import config_io
        # ⚠ 名单要读**临时根**那份（`_scope()` 是按 `app.root` 找配置的）
        rows = config_io.stores_table(self.root)
        store = config_io.stores_of_manager(config_io.managers_table(self.root)[0], rows)[0]
        scope, label = self._scope("sl15763940156")      # 全小写
        self.assertIsNotNone(scope)
        self.assertIn("杨英梅", label)
        self.assertIn(store, scope)

    def test_不认识的人只给本店(self):
        from src import config_io
        with mock.patch.object(config_io, "load_raw", lambda p: {"erp_store_name": "乙店"}):
            scope, label = self._scope("sl00000000000")
        self.assertEqual(scope, {"乙店"})
        self.assertIn("本店", label)


class Test历史存档(unittest.TestCase):
    """用户 2026-09-20：「加个**历史记录**功能，这一周过去之后，比如这一周 14-20 号，
    21 号再获取达成，就把**上一周的给锁住存档**，在工作区的周度目标达成情况下面
    加个历史记录」。

    ⚠ 触发点是**"落盘那份的期间 ≠ 新算出来的期间"**，不靠"今天几号"猜；
    ⚠ **归档过的期间绝不再写**（那就是"锁住"）—— 否则上周的成绩会被人事后改掉，
      而那正是复盘要引用的东西。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()

    def _payload(self, period, total=0.5):
        return {"exists": True, "period": period, "start": "2026-09-14",
                "end": "2026-09-20", "data_until": "2026-09-17",
                "computed_at": "x", "columns": ["A"], "weights": [1.0],
                "missing_columns": [],
                "rows": [{"store": "甲店", "erp_name": "甲店", "matched": True,
                          "targets": [2], "actuals": [1], "rates": [0.5], "total": total,
                          "people": []}]}

    def test_换了一周就把上一周锁住(self):
        A.save(self.root, self._payload("2026-W38"))
        got = A.archive_rolled(self.root, self._payload("2026-W39"), emit=lambda _s: None)
        self.assertEqual(got, "2026-W38")
        one = A.history_load(self.root, "2026-W38")
        self.assertTrue(one["exists"])
        self.assertTrue(one["locked"], "存档是**锁住**的")
        self.assertTrue(one["locked_at"], "记下什么时候锁的")

    def test_同一周不归档(self):
        A.save(self.root, self._payload("2026-W38"))
        self.assertEqual(A.archive_rolled(self.root, self._payload("2026-W38"),
                                          emit=lambda _s: None), "")

    def test_归档过的不许再改_锁住(self):
        A.save(self.root, self._payload("2026-W38", total=0.5))
        A.archive_rolled(self.root, self._payload("2026-W39"), emit=lambda _s: None)
        first = A.history_load(self.root, "2026-W38")["rows"][0]["total"]
        # 又跑一次同一周（比如补跑），存档**不该被动**
        A.save(self.root, self._payload("2026-W38", total=0.9))
        A.archive_rolled(self.root, self._payload("2026-W39"), emit=lambda _s: None)
        self.assertEqual(A.history_load(self.root, "2026-W38")["rows"][0]["total"], first,
                         "历史被改写了 —— 那正是复盘要引用的东西")

    def test_空的那份不归档(self):
        A.save(self.root, {"exists": True, "period": "2026-W38", "rows": []})
        self.assertEqual(A.archive_rolled(self.root, self._payload("2026-W39"),
                                          emit=lambda _s: None), "")

    def test_列表新的在前(self):
        for p in ("2026-W38", "2026-W39", "2026-W37"):
            A.save(self.root, self._payload(p, total=0.4))
            A.archive_rolled(self.root, self._payload("2999-W01"), emit=lambda _s: None)
        got = [x["period"] for x in A.history_list(self.root)]
        self.assertEqual(got, ["2026-W39", "2026-W38", "2026-W37"])
        self.assertEqual(A.history_list(self.root)[0]["stores"], 1)

    def test_没有存档时列表是空的_读单周报错(self):
        self.assertEqual(A.history_list(self.root), [])
        self.assertFalse(A.history_load(self.root, "2026-W01")["exists"])


class Test换周时run真的归档(unittest.TestCase):
    """⚠ 单测 `archive_rolled` 还不够 —— 这条走 **`run()` 整条链**，盯的是**顺序**：

        「**先归档上一周，再覆盖落盘**」

    顺序反了就是一个**不可逆的丢数据**：新的一覆盖，上一周那份就没了，
    而它正是用户要"锁住"的东西（2026-09-20）。所以把中间几段（读文档 / 查库 /
    核算 / 推送）都换掉，只留归档和落盘两步真的跑。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()

    def _old_payload(self, period="2026-W38", total=0.5):
        return {"exists": True, "period": period, "start": "2026-09-14",
                "end": "2026-09-20", "data_until": "2026-09-17",
                "columns": ["A"], "weights": [1.0], "missing_columns": [],
                "rows": [{"store": "甲店", "erp_name": "甲店", "matched": True,
                          "targets": [2], "actuals": [1], "rates": [0.5], "total": total,
                          "people": []}]}

    def _new_payload(self, period="2026-W39"):
        p = self._old_payload(period, total=0.9)
        p["start"], p["end"] = "2026-09-21", "2026-09-27"
        return p

    def test_run_先归档再覆盖(self):
        import types
        A.save(self.root, self._old_payload())
        db = self.root / "out" / "cbg-2026.db"
        db.write_bytes(b"")                       # 文件存在就行，查库那步被换掉了
        plan = types.SimpleNamespace(period="2026-W39", start="2026-09-21",
                                     end="2026-09-27")
        with mock.patch.object(A, "read_plan", return_value=plan), \
             mock.patch.object(A, "load_sales", return_value=([], {})), \
             mock.patch.object(A, "compute", return_value=self._new_payload()), \
             mock.patch.object(A, "notify", return_value={}), \
             mock.patch.object(A, "find_db", return_value=db):
            res = A.run(root=self.root, emit=lambda _s: None)
        self.assertTrue(res["ok"], res.get("why"))
        self.assertEqual(res["archived"], "2026-W38", "上一周没被归档")
        one = A.history_load(self.root, "2026-W38")
        self.assertTrue(one["locked"])
        self.assertEqual(one["rows"][0]["total"], 0.5,
                         "归档的得是**上一周那份**，不是刚算出来的")
        self.assertEqual(A.load(self.root)["period"], "2026-W39", "新的没落盘")

    def test_同一周里重跑不会多出存档(self):
        """⚠ 周中反复点「跑一次」是常态 —— 每次都在同一周里，不该攒出一堆存档。"""
        import types
        A.save(self.root, self._old_payload("2026-W38", total=0.5))
        db = self.root / "out" / "cbg-2026.db"
        db.write_bytes(b"")
        plan = types.SimpleNamespace(period="2026-W38", start="2026-09-14",
                                     end="2026-09-20")
        for _ in range(3):
            with mock.patch.object(A, "read_plan", return_value=plan), \
                 mock.patch.object(A, "load_sales", return_value=([], {})), \
                 mock.patch.object(A, "compute", return_value=self._old_payload()), \
                 mock.patch.object(A, "notify", return_value={}), \
                 mock.patch.object(A, "find_db", return_value=db):
                res = A.run(root=self.root, emit=lambda _s: None)
            self.assertEqual(res["archived"], "")
        self.assertEqual(A.history_list(self.root), [], "同一周不该产生存档")


class Test合计跟着目标拆分刷新(unittest.TestCase):
    """展开一家店、改某个人的目标 ⇒ **最下面那行「合计」要跟着变**。

    用户 2026-09-20 的原话：「最下面的合计，根据上面个人目标设定**可以实时刷新**」，
    随后报了一次：「周度这个合计的数**还是不会根据目标拆分改变**」。

    ⚠ 真因不是"没写刷合计"，而是**写在一个拿不到行的地方**：
      `inp.replaceWith(span)` 之后 `inp` 已经**脱离 DOM**，
      而 `closest('tr')` 是往上找祖先 —— 脱离的节点没有祖先 ⇒ `null`
      ⇒ `tag` 成了空串 ⇒ `refreshDetailTotal()` **压根没被调用**。
      这一类"先换掉节点、再回头问它在哪"的写法**不报错**，只静默不生效
      （页面上看着一切正常，就是数不动）。
    """

    JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def _edit_block(self):
        i = self.JS.index("const sp = e.target.closest && e.target.closest('.split-edit')")
        return self.JS[i:self.JS.index("inp.addEventListener('blur'", i)]

    def test_行标记在换掉输入框之前就取(self):
        blk = self._edit_block()
        self.assertIn("const rowTag", blk, "没记住这一行，合计不知道刷谁")
        self.assertLess(blk.index("const rowTag"), blk.index("sp.replaceWith(inp)"),
                        "行标记必须在 replaceWith **之前**取 —— 换掉之后 closest 拿不到祖先")

    def test_不许拿已经脱离的输入框去找行(self):
        """⚠ 这条是**真正的钉子**：上面那条只钉了顺序，这条钉的是"别用错节点"。"""
        self.assertNotIn("inp.closest('tr')", self.JS,
                         "inp 被 replaceWith 换掉之后已脱离 DOM，closest 返回 null"
                         " ⇒ 合计静默不刷新")

    def test_落定时会刷合计(self):
        blk = self._edit_block()
        self.assertIn("refreshDetailTotal(rowTag)", blk)

    def test_边打边刷(self):
        """⚠ 用户说的是"**实时**刷新" —— 只在落定时刷的话，敲数字时合计不动。"""
        i = self.JS.index("inp.addEventListener('input'")
        blk = self.JS[i:i + 220]
        self.assertIn("refreshDetailTotal(rowTag)", blk)

    def test_回车落定_Esc_放弃(self):
        blk = self._edit_block()
        self.assertIn("'Enter'", blk)
        self.assertIn("'Escape'", blk)
        # ⚠ 放弃也要真的放弃：靠 `cancel` 带过 blur（`back(keep)` 本来就有这个参数）
        self.assertIn("cancel = true", blk)
        self.assertIn("back(!cancel)", self.JS)


class Test拆分那两个按钮真的接了线(unittest.TestCase):
    """⚠⚠ 2026-09-21：用户问「**发送到区长这个功能做好了吗**，实机测试能用了吗」
    —— 一查才发现**前端两个按钮从来没接线**：

        `data-detail-save` / `data-detail-send` 只出现在**渲染那段模板**里，
        `app.js` 里**没有任何 click 处理器** ⇒ 点了什么都不发生。
        后端两条接口（`PUT /api/attain/split` / `POST /api/attain/split/send`）
        和单测都是好的 —— 差的只是这两段。

    所以这条测试盯的是**接线**：按钮渲染出来了（`data-*` 在），就**必须**有人处理它。
    """

    def setUp(self):
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_两个按钮都有处理器(self):
        for attr in ("data-detail-save", "data-detail-send"):
            with self.subTest(attr=attr):
                self.assertIn("closest('[%s]')" % attr, self.js,
                              "「%s」没有 click 处理器 —— 点了没反应" % attr)

    def test_保存打的是_PUT(self):
        blk = self.js[self.js.index("closest('[data-detail-save]')"):][:900]
        self.assertIn("method: 'PUT'", blk)
        self.assertIn("'/api/attain/split'", blk)
        self.assertIn("targets:", blk, "要把每个成员的目标带上")

    def test_发送打的是_split_send(self):
        blk = self.js[self.js.index("closest('[data-detail-send]')"):][:900]
        self.assertIn("'/api/attain/split/send'", blk)
        self.assertIn("period: splitPeriod", blk, "期间要跟着走")

    def test_没发出去要说清原因_不报成失败(self):
        """⚠ 「没发出去」不是错误（目标已经存好了）—— 页面上得写**为什么**
        （谁没配邮箱 / 邮件没开），别只丢一句"失败"。"""
        blk = self.js[self.js.index("closest('[data-detail-send]')"):][:1200]
        self.assertIn("r.why", blk)
        self.assertIn("没发出去", blk)

    def test_收集目标时按列数补零(self):
        """⚠ 后端要的是**定长数组**（跟产品列数一致）—— 中间没填的补 0，
        不然会被截断成"填了却没存上"。"""
        blk = self.js[self.js.index("function collectSplitTargets()"):][:900]
        self.assertIn("slice(0, n)", blk)
        self.assertIn("= 0", blk)
        self.assertIn("splitMembers[mi]", blk, "下标要换成成员名")


class Test展开收起有上下动画(unittest.TestCase):
    """⭐ 用户 2026-09-22：周度重点产品里门店→店员，**对齐月度生意计划的展开动画**。

    ⚠⚠ `tr` 的 `height` **过渡不了**（行高是内容顶出来的，height 只是下限）
      ⇒ 高度压在每个格子里那层 `.attain-in` 上，格子自己的上下内边距一起压。
      跟月度 `planRowsFreeze` / `test_plan_web.py` 同一套三步，别改回"直接 remove"。
    """

    def setUp(self):
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def _fn(self, name):
        i = self.js.index("function %s(" % name)
        # 到下一个顶层 function / const 声明为止（够用即可）
        j = self.js.index("\nfunction ", i + 1)
        return self.js[i:j]

    def test_高度压在_attain_in_上不是_tr(self):
        self.assertIn(".attain-in", self.css)
        self.assertIn("tr.attain-anim .attain-in", self.css)
        self.assertIn("transition: height", self.css)
        self.assertIn("padding-top: 0", self.css, "收起时格子的上下内边距要一起压")
        self.assertIn("attainWrapCells", self.js)
        self.assertIn("attain-frozen", self.js)
        self.assertIn("attain-frozen", self.css)

    def test_两层rAF_且收尾才摘行(self):
        body = self._fn("attainAnimate")
        # 展开要两层 rAF（0 高先画一帧）；收起起点已在屏上，等两层 = 点了卡一下才收
        self.assertIn("requestAnimationFrame(() => requestAnimationFrame(", body)
        self.assertIn("dir === 'grow'", body, "收起不该也等两层 rAF")
        self.assertIn("ATTAIN_ANIM_MS", body)
        self.assertIn("r.remove()", body, "收起的行要等收尾才真删")
        self.assertIn("attain-anim", body)

    def test_收起时边框也跟着过渡(self):
        """只挂 padding 的话，`border-*-width: 0` 当帧跳没 ⇒ 高度还在滑、中间顿一下。"""
        i = self.css.index("tr.attain-anim > td")
        self.assertIn("border-width", self.css[i:i + 240])

    def test_门店列不挤成三行也不留大空(self):
        """5% 三行柱 → 13% 空太多挤了产品列 ⇒ **140px** + 最多两行；
        整列（含表头）垂直居中，别吊在上面。
        ⚠ 2026-09-22 起列宽是**固定像素**（跟月度一样不随窗口缩放）——
          原来那条 `width: 10%` 已废，钉子跟着改。
        ⚠ 门店从标签列匀了 32px（108→140），标签列 112→80 —— 展开行
          补齐区域占位后 col2/col3 两边列位一致，才匀得动。"""
        self.assertRegex(self.css, r"nth-child\(2\)[\s\S]{0,80}width:\s*140px")
        self.assertRegex(self.css, r"nth-child\(3\)[\s\S]{0,80}width:\s*80px")
        self.assertIn("-webkit-line-clamp: 2", self.css, "门店名最多两行")
        self.assertRegex(
            self.css,
            r"#attain-table th:nth-child\(2\),[\s\S]{0,80}vertical-align: middle")
        # 列宽不随窗口缩放（跟 .plan-table 同一套：max-content + 固定像素）
        self.assertRegex(
            self.css,
            r"#attain-table \.table-scroll table \{[^}]*max-content",
            "表宽不能是 100%，否则列会跟着窗口缩放")

    def test_达成目标贴紧产品列(self):
        """用户 2026-09-22 三轮调完定的：标签**右贴产品**（截图：
        「离门店名太近，离右边太远了」）—— 左贴会在标签和台量之间空出大半列。"""
        self.assertIn("tr:not(.attain-detail-row) > td:nth-child(3)", self.css)
        # 标签右贴产品（不再左贴门店）
        self.assertIn("align-items: flex-end", self.css)
        self.assertRegex(
            self.css,
            r"td:nth-child\(3\)[\s\S]{0,120}text-align: right")
        # 门店名右对齐，紧挨右边的标签列
        self.assertRegex(
            self.css,
            r"td:nth-child\(2\)[\s\S]{0,120}text-align: right")
        self.assertIn("padding-left: 3px", self.css)
        # 产品格也略收，给「后面数据有点拥挤」腾一点
        self.assertRegex(self.css, r"\.attain-cell \{[^}]*padding: 2px 5px")

    def test_收起要先写死自然高(self):
        """`height: auto → 0` **过渡不了** —— 冻结帧必须先把起点写成像素。"""
        body = self._fn("attainAnimate")
        self.assertIn("dir === 'grow' ? nats.map(() => 0) : nats", body)

    def test_展开收起都走动画不直接删(self):
        body = self._fn("doToggleStoreDetail")
        self.assertIn("attainAnimate(Array.from(mine), 'shrink')", body)
        self.assertIn("attainAnimate(inserted, 'grow')", body)
        self.assertNotIn("mine.forEach((x) => x.remove())", body,
                         "收起不许再直接 remove（会啪一下没了）")

    def test_切换与展开是串行的(self):
        """「保存目标」要 `await` 收起再 `await` 展开 —— 并发会把行删在半路。"""
        self.assertIn("attainToggleChain", self.js)
        self.assertIn("function toggleStoreDetail(btn)", self.js)
        blk = self.js[self.js.index("function toggleStoreDetail(btn)"):][:400]
        self.assertIn("doToggleStoreDetail", blk)

    def test_令牌关动画能盖住这个动效(self):
        """时长/缓动必须走 `var(--motion-slow)` —— 写死 .34s 就是关不掉的那一个。"""
        i = self.css.index("tr.attain-anim .attain-in")
        self.assertIn("var(--motion-slow)", self.css[i:i + 200])
        self.assertIn("var(--ease-out)", self.css[i:i + 200])


class Test分区汇总(unittest.TestCase):
    """每区末尾一行「共计」—— 后端算好再下发（`region_sums`），照 film 的 film-sum。"""

    def _row(self, region, targets, actuals, rates, total=None, store="店"):
        return {"store": store, "erp_name": store, "matched": True, "region": region,
                "targets": targets, "actuals": actuals, "rates": rates,
                "total": total, "people": []}

    def test_按区域分组_台量加总(self):
        rows = [
            self._row("市区", [10, 10], [5, 0], [0.5, 0.0], 0.25, "甲"),
            self._row("市区", [10, 10], [20, 0], [1.2, 0.0], 0.6, "乙"),
            self._row("南区", [10], [10], [1.2], 1.2, "丙"),
        ]
        got = M.region_sums(rows, [0.5, 0.5])
        self.assertEqual(set(got), {"市区", "南区"})
        s = got["市区"]
        self.assertEqual(s["store"], "共计：市区")
        self.assertTrue(s["is_sum"])
        self.assertEqual(s["targets"], [20, 20])
        self.assertEqual(s["actuals"], [25, 0])
        # 率用**加总后的台量重算**（不拿各行率平均）：25/20 封顶 120%
        self.assertEqual(s["rates"][0], 1.2)
        self.assertEqual(s["rates"][1], 0.0)

    def test_没映射的列汇总仍是_None(self):
        rows = [self._row("市区", [10], [5], [None], None)]
        got = M.region_sums(rows, [1.0])["市区"]
        self.assertIsNone(got["rates"][0])
        self.assertIsNone(got["total"])

    def test_跟着被滤过的_rows_走(self):
        rows = [
            self._row("市区", [10], [5], [0.5], 0.5, "甲"),
            self._row("南区", [10], [10], [1.2], 1.2, "乙"),
        ]
        self.assertEqual(M.region_sums(rows, [1.0])["市区"]["actuals"], [5])
        self.assertEqual(M.region_sums(rows[:1], [1.0])["市区"]["actuals"], [5])
        self.assertNotIn("南区", M.region_sums(rows[:1], [1.0]))

    def test_compute_带上_region_sums(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "out").mkdir()
        (root / "config").mkdir()
        (root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "甲店"\n    huawei_code: "X1"\n'
            '  - erp_name: "乙店"\n    huawei_code: "X2"\n', encoding="utf-8")
        plan = M.Plan(
            period="2026-W38", start=datetime.date(2026, 9, 14),
            end=datetime.date(2026, 9, 20),
            columns=(M.Column("A", frozenset(["1001"]), 1.0),),
            stores=(M.StorePlan("甲店", "市区", (10,)),
                    M.StorePlan("乙店", "市区", (10,))))
        conn = _sales_conn([
            ("甲店", "1001", "5", "零售", "Mate60", "新", "2026-09-15 10:00:00"),
            ("乙店", "1001", "5", "零售", "Mate60", "新", "2026-09-15 10:00:00"),
        ])
        self.addCleanup(conn.close)
        sales, _ = A.load_sales(conn, plan.start, plan.end)
        p = A.compute(plan, sales, conn=conn, root=root)
        s = p["region_sums"]["市区"]
        self.assertEqual(s["targets"], [20])
        self.assertEqual(s["actuals"], [10])
        self.assertAlmostEqual(s["rates"][0], 0.5)
        self.assertAlmostEqual(s["total"], 0.5)

    def test_接口层区域以_stores_yaml_为准(self):
        """用户 2026-09-22：「周度重点产品分区错了」——
        腾讯文档 A 列是 `城阳\\n胶州`，**不能压过** stores.yaml 的西北区/市区/南区。
        （原来 `if not r.get("region")` 才补 ⇒ 文档分区非空就永远补不上。）"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("planNormRegion", js, "分组键要跟后端 region_sums 同一把尺")
        web = (ROOT / "src" / "web.py").read_text(encoding="utf-8")
        self.assertIn("yaml_reg", web, "接口层要拿 yaml 区域覆盖文档 A 列")
        self.assertNotIn('if not r.get("region"):\n                r["region"] = regmap.get(',
                         web, "不能再只在 region 为空时才补 —— 文档分区非空就补不上")
        attain = (ROOT / "src" / "features" / "sales" / "attain" / "attain.py").read_text(encoding="utf-8")
        self.assertIn("stores_by_region", attain, "落盘时就该用 yaml 区域")

    def test_前端区汇总默认不画_点区域名才收起(self):
        """用户 2026-09-22 纠正：汇总行**不要常驻** ——
        默认只列门店；点区域名把该区收成一行 `region_sums` 汇总。"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("region_sums", js, "前端要读后端算好的分区汇总")
        self.assertIn("cls: 'film-sum'", js, "汇总行要挂 film-sum（红底白字）")
        self.assertIn("attainPeople.push([])", js,
                      "汇总行要占一个 people 下标 —— 否则悬停行号错位")
        self.assertIn("attainCollapsedRegions", js, "收起态要记下来（localStorage）")
        self.assertIn("data-attain-region=", js, "区域名要可点")
        self.assertIn("gCollapsed && sum", js,
                      "只有收起的区才画汇总行，不是每区末尾常驻")
        self.assertIn("if (!gCollapsed)", js, "收起的区不画门店明细行")
        self.assertIn("cbg-attain-collapsed-regions", js, "收起态要落 localStorage")
        self.assertIn("planNormRegion(r.region)", js,
                      "周度分组键也要跟后端 region_sums 同一把尺")

    def test_table_支持行级_class(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("'cells' in r", js, "table() 要认 {cls, cells} 行对象")
        self.assertIn("rcls", js)

    def test_CSS_红底盖住两张表(self):
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn("#attain-table tr.film-sum td", css)
        self.assertIn(".plan-table tr.film-sum td", css)
