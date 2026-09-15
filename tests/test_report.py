"""报告的落盘、命名与删除。

背景（用户报的问题）：文件名原来只带目标日，**同一天跑第二次就把第一次覆盖了**。
而"上一次跑出来是什么样"往往正是要对比的东西。
"""

import datetime
import tempfile
import unittest
from pathlib import Path

from src.reconcile import ReconcileResult
from src.report import delete_report, list_reports, report_path, write_report

CTX = {"门店": "青岛新业广场店", "华为门店编码": "SCN231409", "串号标识": "Y",
       "目标日": "2026-09-14", "销售区间": "x", "华为区间": "y",
       "生成时间": "2026-09-15 11:30:00", "配置文件": "c.yaml"}


class TestReportPath(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_name_carries_target_date_store_and_run_time(self):
        p = report_path(self.out, "2026-09-14", "SCN231409",
                        when=datetime.datetime(2026, 9, 15, 11, 30, 45))
        self.assertEqual(p.name, "差异_2026-09-14_SCN231409_20260915-113045.xlsx")

    def test_different_runs_get_different_names(self):
        """核心诉求：同一天跑多次，一份都不能被覆盖。"""
        names = []
        for i in range(5):
            p = report_path(self.out, "2026-09-14", "SCN231409",
                            when=datetime.datetime(2026, 9, 15, 11, 30, 45 + i))
            p.write_text("x", encoding="utf-8")
            names.append(p.name)
        self.assertEqual(len(set(names)), 5)

    def test_same_second_falls_back_to_suffix(self):
        """同一秒内连跑两次（脚本连点）也不能覆盖。"""
        when = datetime.datetime(2026, 9, 15, 12, 0, 0)
        names = []
        for _ in range(3):
            p = report_path(self.out, "2026-09-14", "SCN231409", when=when)
            p.write_text("x", encoding="utf-8")
            names.append(p.name)
        self.assertEqual(names[0], "差异_2026-09-14_SCN231409_20260915-120000.xlsx")
        self.assertTrue(names[1].endswith("-2.xlsx"), names[1])
        self.assertTrue(names[2].endswith("-3.xlsx"), names[2])

    def test_unknown_store_still_works(self):
        p = report_path(self.out, "2026-09-14", "")
        self.assertIn("unknown", p.name)


class TestWriteReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)
        self.res = ReconcileResult()

    def tearDown(self):
        self.dir.cleanup()

    def test_two_runs_keep_both_files(self):
        a = write_report(self.out, self.res, CTX, "2026-09-14", "SCN231409")
        b = write_report(self.out, self.res, CTX, "2026-09-14", "SCN231409")
        self.assertNotEqual(a, b)
        self.assertTrue(a.exists() and b.exists(), "两次都得在")
        self.assertEqual(len(list(self.out.glob("差异_*.xlsx"))), 2)

    def test_sidecar_json_written_next_to_it(self):
        p = write_report(self.out, self.res, CTX, "2026-09-14", "SCN231409")
        side = p.with_suffix(".json")
        self.assertTrue(side.exists())
        self.assertEqual(side.stem, p.stem, "摘要 json 的主名必须跟 xlsx 一致，删的时候才好一起删")

    def test_creates_out_dir_if_missing(self):
        nested = self.out / "还没建的目录"
        p = write_report(nested, self.res, CTX, "2026-09-14", "SCN231409")
        self.assertTrue(p.exists())


class TestListReports(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)
        self.res = ReconcileResult()

    def tearDown(self):
        self.dir.cleanup()

    def test_lists_all_runs_of_the_same_day(self):
        for _ in range(3):
            write_report(self.out, self.res, CTX, "2026-09-14", "SCN231409")
        items = list_reports(self.out)
        self.assertEqual(len(items), 3, "三次跑的三份都要列出来")
        self.assertTrue(all(i["date"] == "2026-09-14" for i in items))

    def test_sorted_newest_first(self):
        import time
        for _ in range(3):
            write_report(self.out, self.res, CTX, "2026-09-14", "SCN231409")
            time.sleep(0.02)
        items = list_reports(self.out)
        self.assertEqual([i["mtime"] for i in items],
                         sorted([i["mtime"] for i in items], reverse=True))

    def test_empty_dir(self):
        self.assertEqual(list_reports(self.out), [])
        self.assertEqual(list_reports(self.out / "不存在"), [])


class TestDeleteReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _make(self):
        return write_report(self.out, ReconcileResult(), CTX, "2026-09-14", "SCN231409")

    def test_deletes_xlsx_and_sidecar(self):
        p = self._make()
        ok, msg = delete_report(self.out, p.name)
        self.assertTrue(ok, msg)
        self.assertFalse(p.exists())
        self.assertFalse(p.with_suffix(".json").exists(), "摘要 json 要一起删")
        self.assertIn("已删除", msg)

    def test_other_reports_untouched(self):
        a = self._make()
        b = self._make()
        delete_report(self.out, a.name)
        self.assertTrue(b.exists(), "删一份不能影响别的")
        self.assertTrue(b.with_suffix(".json").exists())

    def test_missing_file(self):
        ok, msg = delete_report(self.out, "差异_没有这个.xlsx")
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_rejects_path_traversal(self):
        """只允许删 out/ 目录下的 xlsx —— 别让人从接口删掉别的东西。"""
        secret = self.out.parent / "secret.env"
        secret.write_text("TOPSECRET", encoding="utf-8")
        for bad in ("../secret.env", "../../etc/passwd", "sub/x.xlsx", "/etc/passwd",
                    "..%2Fsecret.env"):
            ok, msg = delete_report(self.out, bad)
            self.assertFalse(ok, f"{bad} 不该被接受")
        self.assertTrue(secret.exists(), "别的东西不能被删掉")

    def test_rejects_non_xlsx(self):
        (self.out / "笔记.txt").write_text("x", encoding="utf-8")
        ok, msg = delete_report(self.out, "笔记.txt")
        self.assertFalse(ok)
        self.assertIn("xlsx", msg)

    def test_idempotent_after_delete(self):
        p = self._make()
        delete_report(self.out, p.name)
        ok, _ = delete_report(self.out, p.name)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
