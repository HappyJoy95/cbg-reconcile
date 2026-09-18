"""四池比对历史（`src/pools_history.py`）的回归。

这一页原来是「报量排查」，2026-09-17 用户选了 **B：形态留着、内容换成四池记录**
（那个判据已被证伪，留着只会跟四池对账打架）。
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
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


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
        self.assertIn('data-tab="reports">四池比对', INDEX_HTML)
        # ⚠ 只钉 **tab**。运行页那个按钮也叫「报量排查」，那是对的 ——
        #   第 2 步 reconcile 还在跑（等四池这页稳定了才会把它拿掉）。
        self.assertNotIn('data-tab="reports">报量排查', INDEX_HTML,
                         "tab 改回「报量排查」了？那页现在是四池比对的历史")

    def test_renders_pools_history(self):
        self.assertIn("renderPoolsHistory", APP_JS)
        self.assertIn("/api/pools/history", APP_JS)
        self.assertNotIn("renderReports(o.reports", APP_JS,
                         "又在渲染旧的报量排查清单了")

    def test_api_exists(self):
        web = (ROOT / "src" / "web.py").read_text(encoding="utf-8")
        self.assertIn('"/api/pools/history"', web)


if __name__ == "__main__":
    unittest.main()
