"""报量查询历史（`src/pools_history.py`）的回归。

这一页原来是「报量排查」，2026-09-17 用户选了 **B：形态留着、内容换成报量查询的记录**
（那个判据已被证伪，留着只会跟双平台数据对比打架）。
所以这里额外钉一条**前端 tab 名** —— 改回去就说明有人没搞清这页现在是什么。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import pools_history as H                         # noqa: E402

INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js",
                              "features/compliance/comparison/page.js", "app.js"))


class TestSaveDay(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            H.save_day(d, "2026-09-17", {"AD": 2, "BC": 3, "AC": 429, "BD": 304},
                       [{"sn": "A1"}], [{"sn": "B1"}])
            got = H.detail(d, "2026-09-17")
            self.assertEqual(got["AD"], [{"sn": "A1"}])
            self.assertEqual(got["BC"], [{"sn": "B1"}])
            self.assertEqual(got["counts"]["AD"], 2)

    def test_same_day_overwrites_not_appends(self):
        """一天跑两次不该出两条记录。"""
        with tempfile.TemporaryDirectory() as d:
            H.save_day(d, "2026-09-17", {"AD": 1, "BC": 0}, [{"sn": "A1"}], [])
            H.save_day(d, "2026-09-17", {"AD": 5, "BC": 2}, [{"sn": "A9"}], [])
            days = H.days(d)
            self.assertEqual(len(days), 1)
            self.assertEqual(days[0]["AD"], 5)
            self.assertEqual(H.detail(d, "2026-09-17")["AD"], [{"sn": "A9"}])

    def test_list_newest_first(self):
        with tempfile.TemporaryDirectory() as d:
            for day in ("2026-09-15", "2026-09-17", "2026-09-16"):
                H.save_day(d, day, {"AD": 0, "BC": 0}, [], [])
            self.assertEqual([r["date"] for r in H.days(d)],
                             ["2026-09-17", "2026-09-16", "2026-09-15"])

    def test_one_file_per_year(self):
        with tempfile.TemporaryDirectory() as d:
            H.save_day(d, "2026-09-17", {"AD": 0, "BC": 0}, [], [])
            H.save_day(d, "2025-12-31", {"AD": 1, "BC": 1}, [], [])
            self.assertEqual(H.years(d), [2026, 2025])
            self.assertEqual(H.days(d, 2025)[0]["AD"], 1)

    def test_broken_file_reads_as_empty(self):
        """历史看不了不该把控制台弄挂。"""
        with tempfile.TemporaryDirectory() as d:
            p = H.path_for(d, 2026)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{ 这不是 json", encoding="utf-8")
            self.assertEqual(H.load(d, 2026), {})
            self.assertEqual(H.days(d, 2026), [])

    def test_missing_day_is_empty_not_none(self):
        """前端不用判 None。"""
        with tempfile.TemporaryDirectory() as d:
            got = H.detail(d, "2026-01-01")
            self.assertEqual(got["AD"], [])
            self.assertEqual(got["BC"], [])


class TestFrontendWiring(unittest.TestCase):
    """这一页现在叫什么、渲染哪来的 —— 钉住，别再被人改回「报量排查」。"""

    def test_tab_is_renamed(self):
        # ⚠ 2026-09-18 前端融合改版：报量查询从**一级页签**降成
        #   「五项合规」页里的**二级标签**（用户定：POS 和报量查询各是各的表，不许混）。
        # ⚠ 2026-09-18 傍晚用户又改了一次名：「报量查询」→「报量查询」。
        #   页签 id 还是 `pools`（id 改了 `GO_TARGETS` / 老日志的 `go` 都要迁移）。
        self.assertRegex(INDEX_HTML, r'data-subtab="pools"[^>]*>报量查询')
        # ⚠ 这两个是**旧名**，故意留着字面 —— 别顺手"统一"成新名，
        #   那等于把"不该出现的东西"改成"应该出现的东西"，断言必然反过来红
        #   （批量改名时真踩了这一次）。
        for dead in ("报量排查", "四池比对"):
            with self.subTest(dead=dead):
                self.assertNotRegex(INDEX_HTML, r'data-subtab="pools"[^>]*>%s' % dead,
                                 "这个标签名已经被用户改掉了，别改回去")

    def test_renders_pools_history(self):
        self.assertIn("renderPoolsHistory", APP_JS)
        self.assertIn("/api/pools/history", APP_JS)
        self.assertNotIn("async function renderPoolsHistory", (ROOT / "web" / "app.js").read_text(
            encoding="utf-8"), "报量查询逻辑仍留在公共 app.js")
        self.assertNotIn("renderReports(o.reports", APP_JS,
                         "又在渲染旧的报量排查清单了")

    def test_api_exists(self):
        web = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"/api/pools/history"', web)


if __name__ == "__main__":
    unittest.main()
