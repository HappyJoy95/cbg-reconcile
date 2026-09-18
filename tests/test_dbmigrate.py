"""2.1.0 一次性库重建（`src/dbmigrate.py`）。

这一步**会在门店电脑上把订单库改名**（效果等于删），所以两条铁律必须有测试盯着：

1. **绝不真删** —— 是改名归档，`.bak-2.1.0-*` 还在，改回来就全回来了
2. **只在正式包做** —— beta 包 / 源码运行**都不做**（判据只能用 `BUILD.txt`，
   因为 beta 和正式包**版本号同号**）
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import dbmigrate as M                              # noqa: E402
from src import version as V                                # noqa: E402


class TestOnlyInRelease(unittest.TestCase):
    """⚠ 这个类测的是**门槛本身**，所以必须把保命开关关掉。

    `tests/conftest.py` 全局设了 `CBG_NO_DB_REBUILD=1`（防测试误改真库）——
    不关掉的话这里每条都会拿到"设了 CBG_NO_DB_REBUILD"，测不出门槛的真假。
    """

    def setUp(self):
        self.addCleanup(os.environ.pop, "CBG_NO_DB_REBUILD", None)
        os.environ.pop("CBG_NO_DB_REBUILD", None)

    def _why(self, build):
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "BUILD.txt"
            with mock.patch.object(V, "BUILD_FILE", bf):
                if build is not None:
                    bf.write_text(build, encoding="utf-8")
                return M.only_in_release(d)

    def test_beta_skips(self):
        self.assertIn("beta", self._why("beta · 2026-09-17 17:40"))

    def test_带编号的_beta_也跳过(self):
        """⚠ 2026-09-17 起 beta 包带编号（`beta1` / `beta2`…，见 build_package.sh）——
        指纹长这样：`beta1 · 2026-09-17 18:49`。
        判据是**子串 "beta"**，所以编号不影响；这条把它钉住，
        免得哪天有人把标记改成纯数字（`1 · 2026-09-17 18:49`）——
        那样 `only_in_release` 会**当成正式包去做库重建**，而门店那边
        只是拿它测一下、根本没打算升版。这是**会改门店数据**的那一类错。
        """
        for build in ("beta1 · 2026-09-17 18:49", "beta2 · 2026-09-17 18:49",
                      "beta12 · 2026-09-17 18:49", "BETA3 · x"):
            with self.subTest(build=build):
                self.assertIn("beta", self._why(build).lower())

    def test_release_runs(self):
        self.assertEqual(self._why("2026-09-17 18:00"), "", "正式包该做")

    def test_no_build_file_skips(self):
        """源码运行 / git clone —— 开发机上绝不能动库。"""
        self.assertIn("BUILD.txt", self._why(None))

    def test_empty_build_file_skips(self):
        self.assertIn("空", self._why(""))

    def test_beta_check_is_case_insensitive(self):
        self.assertIn("beta", self._why("BETA · x"))

    def test_escape_hatch_wins(self):
        """⚠ 保命开关优先于一切 —— 这正是开发机那次事故的补丁。"""
        with mock.patch.dict(os.environ, {"CBG_NO_DB_REBUILD": "1"}):
            self.assertIn("CBG_NO_DB_REBUILD", self._why("2026-09-17 18:00"))

    def test_not_a_release_shaped_file(self):
        """⚠ 项目根上随便一个同名文件不该骗过门槛（开发机那次就是）。"""
        self.assertIn("不像正式包", self._why("这是我随手写的"))


class TestRebuildOnce(unittest.TestCase):
    def _root(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        out = Path(d.name) / "out"
        out.mkdir()
        for y in (2025, 2026):
            (out / ("cbg-%d.db" % y)).write_text("x", encoding="utf-8")
        return d.name, out

    def test_renames_never_deletes(self):
        """⚠ 这是这一步**唯一不可逆**的地方 —— 必须是改名。"""
        root, out = self._root()
        r = M.rebuild_once(root, force=True)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["archived"]), 2)
        names = sorted(p.name for p in out.iterdir())
        self.assertTrue(all(".bak-2.1.0-" in n for n in names), names)
        self.assertEqual(len(names), 2, "老库一个都不许少")

    def test_only_once(self):
        root, out = self._root()
        M.rebuild_once(root, force=True)
        again = M.rebuild_once(root, force=True)
        self.assertTrue(again["skipped"])
        self.assertIn("已经重建过", again["reason"])

    def test_skipped_by_gate_does_not_write_mark(self):
        """⚠ beta 包里跳过时**不能写标记** —— 否则正式包上来就不做了。"""
        root, _ = self._root()
        r = M.rebuild_once(root)           # 本机没有 BUILD.txt ⇒ 会被门槛拦下
        self.assertTrue(r["skipped"])
        self.assertFalse(M.done(root), "跳过不该留标记")

    def test_missing_out_dir_is_ok(self):
        """没有 out/ 也要能返回（别把启动流程弄挂）。"""
        with tempfile.TemporaryDirectory() as d:
            r = M.rebuild_once(d, force=True)
            self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
