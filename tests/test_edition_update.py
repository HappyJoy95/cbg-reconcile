# -*- coding: utf-8 -*-
"""生活馆版 · Task 8 —— 自更新渠道按版 · 更新日志按版过滤 · 打包脚本按版裁剪。

⚠ 本文件钉的三样东西（`selfupdate.BRANCH` 的 lifehall 段、`whatsnew.LIFEHALL_SKIP`、
`build_package.sh` 的 EDITION 裁剪）都是 **lifehall 分支私有段** ——
合并回 main 时连同对应代码一起处理（提交信息里写明，防误合）。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import edition                                            # noqa: E402


class _EditionCase(unittest.TestCase):
    """每个用例把 `CBG_EDITION` 恢复成进来时的样子 —— conftest 钉的是 full，
    判据被留在 lifehall 会让后面几千条既有测试全变味（坑 17 的同款：
    "改完没恢复现场"比"没改"更难查）。"""

    def setUp(self):
        self._orig = os.environ.get("CBG_EDITION")

    def tearDown(self):
        os.environ["CBG_EDITION"] = self._orig or "full"
        edition.reload()


class Test自更新渠道(_EditionCase):
    def test_branch按版(self):
        """`BRANCH` 是模块级常量，被 `_version_api_url(ref=BRANCH)` /
        `download(ref=BRANCH)` 等的**默认参数在 import 时捕获** ——
        所以必须重载模块来验（改 env 不会变已捕获的默认值）。"""
        from src import selfupdate
        with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
            edition.reload()
            mod = importlib.reload(selfupdate)
            self.assertEqual(mod.BRANCH, "lifehall")
            # 版本**回退源**（源码仓那一腿）跟着渠道走 —— 生活馆读 lifehall 分支
            self.assertIn("ref=lifehall", mod._version_sources()[1][1])
            # ⚠ 下载**不分渠道**（路线 A，2026-10-02「不过渡」）：`_zip_urls`
            #   只有发行仓的密文资产，没有 zipball 明文退路 —— 生活馆独立的
            #   「下发内容」通道在路线 A 下还没设计，这条先钉现状；
            #   真要分渠道时改这里（连同发行仓资产命名一起，别悄悄放明文回来）。
            first_url = mod._zip_urls(mod.BRANCH)[0][0]
            self.assertIn("/releases/", first_url)
            self.assertNotIn("/zipball/", first_url)
        with mock.patch.dict(os.environ, {"CBG_EDITION": "full"}):
            edition.reload()
            mod = importlib.reload(selfupdate)
            self.assertEqual(mod.BRANCH, "main")
        # 恢复现场（conftest 钉 full）—— tests/test_selfupdate.py 里
        # `selfupdate.BRANCH` 构造的断言靠这个值是 main。
        edition.reload()
        importlib.reload(selfupdate)
        self.assertEqual(selfupdate.BRANCH, "main")

    def test_targets跳过PRUNE(self):
        """生活馆机器从 lifehall 分支下 zip —— 那个 zip 是**全量仓库**，
        里面有被裁的文件。`_targets()` 不许把它们铺回磁盘，
        否则第一次自更新就把云商/对账代码带回来了（裁剪白做）。"""
        from src import selfupdate
        with tempfile.TemporaryDirectory() as d:
            z = Path(d)
            (z / "src").mkdir()
            (z / "src" / "erp.py").write_text("x", encoding="utf-8")
            (z / "src" / "cli.py").write_text("x", encoding="utf-8")
            (z / "src" / "features" / "compliance").mkdir(parents=True)
            (z / "src" / "features" / "compliance" / "__init__.py").write_text(
                "x", encoding="utf-8")
            (z / "src" / "features" / "tools").mkdir(parents=True)
            (z / "src" / "features" / "tools" / "x.py").write_text(
                "x", encoding="utf-8")
            (z / "门店操作手册.md").write_text("x", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
                edition.reload()
                pairs = selfupdate._targets(z)
                rels = [selfupdate._rel_key(r) for _, r in pairs]
            self.assertIn("src/cli.py", rels, "保留的代码必须照铺")
            self.assertNotIn("src/erp.py", rels, "被裁的云商代码铺回来了")
            self.assertNotIn("门店操作手册.md", rels, "PRUNE 里的根文档铺回来了")
            self.assertFalse(any("compliance" in r for r in rels))
            self.assertIn("src/features/tools/x.py", rels,
                          "没被裁的 src/features/tools/x.py 该照铺")

    def test_full版不受影响(self):
        """主包那条路零变化：全量铺（既有 test_selfupdate 的白名单/锚点测试
        跑在 conftest 的 full 上，这条把"没被 PRUNE 误伤"也钉一下）。"""
        from src import selfupdate
        with tempfile.TemporaryDirectory() as d:
            z = Path(d)
            (z / "src").mkdir()
            (z / "src" / "erp.py").write_text("x", encoding="utf-8")
            (z / "src" / "cli.py").write_text("x", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CBG_EDITION": "full"}):
                edition.reload()
                rels = [selfupdate._rel_key(r) for _, r in selfupdate._targets(z)]
            self.assertIn("src/erp.py", rels)
            self.assertIn("src/cli.py", rels)


class Test更新日志按版过滤(_EditionCase):
    def test_生活馆看不到对账类更新(self):
        """语义钉死：对账/被裁功能的更新日志**不弹给生活馆**（用户 2026-09-26）。

        2.2.1 的正文里有「**四池对账**两处修正……」—— 生活馆弹窗和待办
        都不该出现它。⚠ 注意 `digest` 的 highlights 来自 `notes_for`、
        待办来自 `versions_after`，两个入口都得滤（缺一个就漏）。
        """
        from src import whatsnew
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
                edition.reload()
                dig = whatsnew.digest(Path(d), "2.2.1", since="2.0.0")
                self.assertIsNotNone(dig, "被 skip 的版本不能变成 None —— 弹窗会空")
                self.assertTrue(all("四池" not in h for h in dig["highlights"]),
                                "生活馆弹窗里出现了对账内容：%s" % dig["highlights"])
                self.assertEqual(dig["todo"], [],
                                 "生活馆带上了被裁功能那几版的待办")
                # 三个入口一起验：notes_for / versions_after / 整份 digest
                self.assertEqual(whatsnew.notes_for("2.2.1"), whatsnew.DEFAULT_NOTE)
                self.assertEqual(whatsnew.versions_after("", "2.2.1"), [])

    def test_主包照常看到(self):
        """对照组：full 下一条都不滤 —— 主包的门店要看这些日志。"""
        from src import whatsnew
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"CBG_EDITION": "full"}):
                edition.reload()
                self.assertTrue(any("四池" in h for h in
                                    whatsnew.notes_for("2.2.1")["highlights"]),
                                "主包不该被 LIFEHALL_SKIP 滤掉")
                vs = whatsnew.versions_after("", "2.2.1")
                self.assertIn("2.2.1", vs)
                self.assertIn("2.0.0", vs)
                dig = whatsnew.digest(Path(d), "2.2.1", since="2.0.0")
                self.assertIsNotNone(dig)
                self.assertTrue(any("四池" in h for h in dig["highlights"]))

    def test_被skip的键都真实存在(self):
        """防打错键：LIFEHALL_SKIP 里写个不存在的版本号 = 那一版永远没人
        被滤（而且看不出来）。⚠ 反向的规矩在代码注释里：**新写 lifehall
        专属 note 时不要加进这张表**，所以这里只钉 ⊆，不钉相等。"""
        from src import whatsnew
        extra = set(whatsnew.LIFEHALL_SKIP) - set(whatsnew.NOTES)
        self.assertFalse(extra,
                         "LIFEHALL_SKIP 里有 NOTES 没有的键：%s" % extra)


class Test打包脚本按版裁剪(_EditionCase):
    """打包脚本的静态检查 —— **只读脚本、不跑它**（beta/正式包都等用户发话才打）。"""

    def _script(self) -> str:
        p = ROOT / "tools" / "build_package.sh"
        self.assertTrue(p.is_file(), "不在仓库里（tools/ 不进包，只在仓库跑）")
        return p.read_text(encoding="utf-8")

    def test_脚本读EDITION并接PRUNE(self):
        s = self._script()
        self.assertIn("EDITION_VAL=", s, "脚本没读 EDITION 文件")
        self.assertIn("from src.edition import PRUNE", s,
                      "裁剪清单不是从 edition.PRUNE 单源读的（会跟自更新走散）")
        self.assertIn("缺 EDITION 文件", s, "lifehall 包少了 EDITION 文件没拦")
        # ⚠ macOS bash 3.2 + `set -u`：空数组直接 `"${PRUNE_ARGS[@]}"` 展开会
        #   报 unbound variable —— 必须用兼容写法（主包模式正是空数组）。
        self.assertIn("${PRUNE_ARGS[@]+", s)

    def test_主包断言还在(self):
        """主包那两条"必须在"的断言挪进了 else 分支，但**内容不许丢** ——
        少了它，正式包可能漏发手册还没人发现（cp 那句由
        tests/test_selfupdate.py::test_发布说明正文住在仓库里_打包只拷贝 钉着）。"""
        s = self._script()
        self.assertIn("缺 门店操作手册.md", s)
        self.assertIn("缺 发布说明.md", s)
        self.assertIn('cp "${ROOT}/门店操作手册.md"', s)

    def test_生活馆断言翻转(self):
        """lifehall 模式：被裁文件必须**不在**包里（rsync 排除 + 反查两道），
        而且 cp 不能把 PRUNE 掉的文档又拷回来。"""
        s = self._script()
        self.assertIn("生活馆包里混进了该裁的文件", s)
        self.assertIn('if [ "${EDITION_VAL}" = "lifehall" ]', s)


class Test_prune候选只看本机(_EditionCase):
    """Step 3 的连带检查：`prune_candidates()` 对"zip 里有、本地没有"的行为。

    实测结论（读源码 + 这里钉住）：候选一律从**本机文件**出发 ——
    "zip 里有、本地没有"的文件压根不进循环，报不出任何东西；
    反向（本地有、zip 没有）才是候选。
    """

    def _root(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "src").mkdir(parents=True, exist_ok=True)
        return d

    def _zip(self, rels):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for r in rels:
            p = d / r
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        return d

    def test_zip里有本地没有的被裁文件不报警(self):
        from src import selfupdate
        root = self._root()
        (root / "src" / "stale.py").write_text("x", encoding="utf-8")  # 真残留
        z = self._zip(["src/erp.py", "src/cli.py"])
        with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
            edition.reload()
            have = {selfupdate._rel_key(r) for _, r in selfupdate._targets(z)}
            c = selfupdate.prune_candidates(root, z, have)
        # have 里没有 src/erp.py（_targets 按 PRUNE 滤掉了），但本机也没有它
        # ⇒ 不进循环 ⇒ 既不算候选也不算"跳过"（zip 里有它 ≠ 本机有它）
        self.assertNotIn("src/erp.py", c["files"])
        self.assertNotIn("src/erp.py", c["skipped"])
        # 审计该报的还得报（证明上面不是"什么都没算"）
        self.assertIn("src/stale.py", c["files"])

    def test_本机残留被裁文件_但zip里还有_不进删除候选(self):
        """即使本机真残留 `src/erp.py`，`prune_reason` 还会问一次文件系统：
        zip 里真有它 ⇒ "按存在处理"、进 skipped 不进 files（宁可不删）。
        （PRUNE_ENABLED 现在也是 False —— 只审计不动手。）"""
        from src import selfupdate
        root = self._root()
        (root / "src" / "erp.py").write_text("x", encoding="utf-8")
        z = self._zip(["src/erp.py", "src/cli.py"])
        with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
            edition.reload()
            have = {selfupdate._rel_key(r) for _, r in selfupdate._targets(z)}
            c = selfupdate.prune_candidates(root, z, have)
        self.assertNotIn("src/erp.py", c["files"])
        self.assertIn("src/erp.py", c["skipped"])


if __name__ == "__main__":
    unittest.main()
