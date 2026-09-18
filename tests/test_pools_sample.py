"""BC 排除样机（用户 2026-09-17）。

业务：**「云商卖了样机开单，但是玲珑开不了」** —— 样机在玲珑那边报不了量，
所以它会一直挂在玲珑在库、天天出现在 BC 里。**那是误报。**

⚠ 三条不能错的：
  * 判据要认「位置不固定」（`样,新` 和 `N,样,新` 都见过）
  * 判据要认「中文逗号」（`s,样，新,新`）
  * 判据要认「样机」这种写法（不只是「样」）
  * **排除掉多少台必须留痕**（`BC_样机`），不能静默过滤
"""

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import pools as P                                  # noqa: E402


class TestIsSampleMarker(unittest.TestCase):
    def test_real_values_seen_in_the_wild(self):
        """这四个都是 `erp_sales` 里**实测出现过**的写法，一个都不能漏。"""
        for m in ("样,新", "s,样,新", "s,样，新,新", "样机,N,新", "N,样,新"):
            with self.subTest(marker=m):
                self.assertTrue(P.is_sample_marker(m), m)

    def test_position_does_not_matter(self):
        """⚠ 位置不固定 —— 必须按逗号拆开逐段比，不能只看开头。"""
        self.assertTrue(P.is_sample_marker("新,样"))
        self.assertTrue(P.is_sample_marker("GT,样,新"))

    def test_normal_markers_are_not_sample(self):
        for m in ("新,GT", "T,新", "新", "s,新", "CM,新"):
            with self.subTest(marker=m):
                self.assertFalse(P.is_sample_marker(m), m)

    def test_empty_is_false(self):
        for m in (None, "", "  "):
            self.assertFalse(P.is_sample_marker(m))


def _db(rows):
    """建个只有 `erp_sales` / `lg_stock` / `erp_stock` 的内存库。"""
    conn = sqlite3.connect(":memory:")
    P.ensure(conn)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY, status_name TEXT,"
        " return_status INTEGER, refund_status INTEGER);"
        "CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT, sn TEXT);")
    P.save_sales(conn, "erp-sales", rows)
    return conn


class TestBcExcludesSample(unittest.TestCase):
    def test_sample_is_not_in_bc(self):
        conn = _db([{"单号": "S1", "串号": "SNAAAAAA1", "串号标识": "样,新"}])
        P.save_snapshot(conn, "lg-stock", [{"sn": "SNAAAAAA1"}], date="2026-09-17")
        q = P.quadrants(conn)
        self.assertEqual(q["BC"], 0, "样机不该进 BC")
        self.assertEqual(q["BC_样机"], 1, "但必须单列出来，不能静默丢掉")

    def test_normal_still_in_bc(self):
        conn = _db([{"单号": "S2", "串号": "SNBBBBBB1", "串号标识": "新,GT"}])
        P.save_snapshot(conn, "lg-stock", [{"sn": "SNBBBBBB1"}], date="2026-09-17")
        q = P.quadrants(conn)
        self.assertEqual(q["BC"], 1)
        self.assertEqual(q["BC_样机"], 0)

    def test_details_of_bc_excludes_sample(self):
        conn = _db([{"单号": "S1", "串号": "SNAAAAAA1", "串号标识": "样,新"},
                    {"单号": "S2", "串号": "SNBBBBBB1", "串号标识": "新,GT"}])
        # ⚠ `details` 会取 item_name / warehouse_name / stock_age —— 内存库里
        #   不传这几列的话列根本不会被建出来，查询直接 no such column。
        P.save_snapshot(conn, "lg-stock",
                        [{"sn": "SNAAAAAA1", "item_name": "样机",
                          "warehouse_name": "可售仓", "stock_age": 9},
                         {"sn": "SNBBBBBB1", "item_name": "正常机",
                          "warehouse_name": "可售仓", "stock_age": 3}],
                        date="2026-09-17")
        got = [r["sn"] for r in P.details(conn, "BC")]
        self.assertEqual(got, ["SNBBBBBB1"])

    def test_one_sample_row_wins_over_a_normal_one(self):
        """一台机器卖过两次，**只要有一行是样机就算样机** ——
        样机卖出去玲珑就报不了量，别的行是什么都不改变这件事。"""
        conn = _db([{"单号": "S1", "串号": "SNAAAAAA1", "串号标识": "样,新"},
                    {"单号": "S2", "串号": "SNAAAAAA1", "串号标识": "新,GT"}])
        P.save_snapshot(conn, "lg-stock", [{"sn": "SNAAAAAA1"}], date="2026-09-17")
        q = P.quadrants(conn)
        self.assertEqual(q["BC"], 0)
        self.assertEqual(q["BC_样机"], 1)

    def test_sample_still_counts_as_sold_for_only_C(self):
        """⚠ 样机也是"云商卖了" —— 算「只在 C」时不能把它漏掉。

        ⚠ 注意**别把样机也放进玲珑在库** —— 那样它就成了 BC（只是被排除），
        压根不在"只在 C"里。第一版就是这么写错的。
        """
        conn = _db([{"单号": "S1", "串号": "SNAAAAAA1", "串号标识": "样,新"}])
        q = P.quadrants(conn)
        self.assertEqual(q["only_C"], 1, "样机被整个丢掉了？那 only_C 会少算")

    def test_ad_not_touched(self):
        """⚠ 用户只说了 BC。AD（玲珑报了、云商没报）是完全另一个方向，不许动。"""
        conn = _db([])
        conn.executescript(
            "INSERT INTO orders (document_no,status_name,return_status,refund_status)"
            " VALUES ('D1','已完成',0,0);"
            "INSERT INTO order_lines (document_no,sn) VALUES ('D1','SNAAAAAA1');")
        P.save_snapshot(conn, "erp-stock", [{"Imei": "SNAAAAAA1"}],
                        date="2026-09-17", sn_field="imei")
        q = P.quadrants(conn)
        self.assertEqual(q["AD"], 1, "AD 不该受样机过滤影响")


if __name__ == "__main__":
    unittest.main()
