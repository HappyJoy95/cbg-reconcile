# -*- coding: utf-8 -*-
"""生活馆版：注册表 / 菜单 / 步骤按 edition 收窄（实施计划 Task 2）。

⚠ 这一类**必须放在独立文件**里（不进 tests/test_edition.py）：它要切
`CBG_EDITION`，而本分支的 `EDITION` 文件写着 `lifehall` —— 切完不**恢复原值**
就 `reload()`（见 `_restore`）会毒掉同进程里后续所有测试。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import edition                                            # noqa: E402


class Test注册表收窄(unittest.TestCase):
    def setUp(self):
        edition.reload()
        self._old = os.environ.get("CBG_EDITION")
        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(self._restore)

    def _restore(self):
        # ⚠ 硬规矩（父 agent 2026-09-26）：恢复**原值**再 reload ——
        #   直接 `os.environ.pop("CBG_EDITION")` 会让后面的测试读到
        #   EDITION 文件（= lifehall），全仓 2810 条 full 断言当场被毒。
        if self._old is None:
            os.environ.pop("CBG_EDITION", None)
        else:
            os.environ["CBG_EDITION"] = self._old
        edition.reload()

    def test_ALL只剩小工具(self):
        from src.features import build_all
        keys = [f.key for f in build_all()]
        self.assertEqual(keys, ["tools"])

    def test_小工具只剩三个子模块(self):
        from src.features import build_all
        kids = [s.key for s in build_all()[0].children]
        self.assertEqual(kids, ["pricetag", "badge", "claim-pending"])

    def test_步骤只剩dump和自动更新(self):
        from src.features import registry
        cmds = [s.cmd for s in registry.all_steps()]
        self.assertEqual(set(cmds), {"dump", "autoupdate"})

    def test_dump不进定时表(self):
        """用户定的：不建计划任务，手动刷 —— dump 的 whens 剥成空。"""
        from src.features import registry
        dump = [s for s in registry.all_steps() if s.cmd == "dump"][0]
        self.assertEqual(dump.whens, ())
        auto = [s for s in registry.all_steps() if s.cmd == "autoupdate"][0]
        self.assertTrue(auto.whens)            # 自动更新照旧每小时

    def test_注册表自检仍干净(self):
        from src.features import registry
        self.assertEqual(registry.validate(), [])

    def test_页面只剩保留名单(self):
        import src.web as web
        rules = web._build_page_rules()
        self.assertEqual(set(rules), set(edition.LIFEHALL_PAGES))
        # 生活馆只有"门店"一种身份，全部放行（"" = 都看得见）
        self.assertTrue(all(v == "" for v in rules.values()))

    def test_刷新待领走dump(self):
        import src.web as web
        steps = web._refresh_steps()
        self.assertEqual(steps, {"claim-pending": ("dump",)})

    def test_画像一律要玲珑(self):
        """名单里没这家店（认店失败）也得走玲珑 —— 否则 daily 把 dump 按掉，
        待领清单的数据源就断了。"""
        from src import config_io
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(ROOT / "config" / "stores.yaml", Path(d) / "stores.yaml")
            prof = config_io.store_profile({"erp_store_name": ""}, Path(d))
            self.assertTrue(prof["needs_linglong"])
