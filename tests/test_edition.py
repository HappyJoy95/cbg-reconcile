# -*- coding: utf-8 -*-
"""生活馆版判据 `src/edition.py` —— env > EDITION 文件 > full。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import edition                                            # noqa: E402


def _without_edition_env():
    """剥掉 CBG_EDITION 的 env 副本（用来验证"文件路径"这一档）。"""
    return {k: v for k, v in os.environ.items() if k != "CBG_EDITION"}


class Test取值优先级(unittest.TestCase):
    def test_env_优先于文件(self):
        # conftest 已经把 env 钉成 full —— 这里反向验证 env 真的压过文件
        with mock.patch.dict(os.environ, {"CBG_EDITION": "full"}):
            edition.reload()
            self.assertEqual(edition.value(), "full")
            self.assertFalse(edition.is_lifehall())

    def test_没有env_读EDITION文件(self):
        with mock.patch.dict(os.environ, _without_edition_env(), clear=True):
            edition.reload()
            # 本仓库（lifehall 分支）的 EDITION 文件写着 lifehall
            self.assertEqual(edition.value(), "lifehall")
            self.assertTrue(edition.is_lifehall())

    def test_env写lifehall(self):
        with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
            edition.reload()
            self.assertTrue(edition.is_lifehall())

    def test_文件缺失默认full(self):
        # 主包（main）没有 EDITION 文件 → full（主包零影响的关键默认）
        with mock.patch.dict(os.environ, _without_edition_env(), clear=True):
            edition.reload()
            self.assertEqual(edition.value(root=Path("/nonexistent-xyz-abc")), "full")

    def test_垃圾文件内容默认full(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "EDITION").write_text("半角SＮＢ\n", encoding="utf-8")
            with mock.patch.dict(os.environ, _without_edition_env(), clear=True):
                edition.reload()
                self.assertEqual(edition.value(root=Path(d)), "full")

    def test_reload清文件缓存(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "EDITION").write_text("lifehall\n", encoding="utf-8")
            with mock.patch.dict(os.environ, _without_edition_env(), clear=True):
                edition.reload()
                self.assertEqual(edition.value(root=Path(d)), "lifehall")
                # 同目录改文件 → reload 之前读缓存之外的路径（root 不进缓存）
                Path(d, "EDITION").write_text("full\n", encoding="utf-8")
                self.assertEqual(edition.value(root=Path(d)), "full")
        edition.reload()


class Test清单(unittest.TestCase):
    def test_PRUNE每一条都真实存在(self):
        """清单写错路径 = 打包静默漏裁 —— 每条都得能在仓库里指出来。"""
        self.assertTrue(edition.PRUNE)
        for rel in edition.PRUNE:
            self.assertTrue((ROOT / rel).exists(),
                            "PRUNE 里的路径不存在：%s" % rel)

    def test_data_state不在裁剪清单(self):
        """claim 的 find_db 和刷新冷却都靠它 —— 裁了生活馆包启动就崩。"""
        self.assertNotIn("src/app/data_state.py", edition.PRUNE)

    def test_内置凭据文件在裁剪清单(self):
        """erp.py 里有内置公司账号凭据 —— 生活馆包绝不能带（用户点名要裁）。"""
        self.assertIn("src/erp.py", edition.PRUNE)

    def test_保留名单(self):
        self.assertIn("tools", edition.LIFEHALL_PAGES)
        self.assertIn("claim-pending", edition.LIFEHALL_PAGES)
        self.assertNotIn("sn-trace", edition.LIFEHALL_PAGES)
        self.assertNotIn("scheduler", edition.LIFEHALL_PAGES)
        self.assertNotIn("stores", edition.LIFEHALL_PAGES)
        self.assertEqual(set(edition.LIFEHALL_BUILTIN_STEPS),
                         {"dump", "autoupdate"})

    def test_pruned前缀匹配(self):
        self.assertTrue(edition.pruned("src/erp.py"))
        self.assertTrue(edition.pruned("src/features/compliance/pos/x.py"))
        self.assertTrue(edition.pruned("src\\features\\compliance\\__init__.py"))
        self.assertFalse(edition.pruned("src/features/tools/claim/pending.py"))
        self.assertFalse(edition.pruned("src/erp_helpers.py"))  # 前缀不能误伤


if __name__ == "__main__":
    unittest.main()
