# -*- coding: utf-8 -*-
"""生活馆版判据 `src/edition.py` —— env > EDITION 文件 > full。"""

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
        # ⚠ 别拿**仓库根**验这条：`EDITION` 文件是**分支**的安装版别
        #   （lifehall 分支写 lifehall、main 写 full），仓库根的值随分支变。
        #   用临时目录验「文件被读、值合法就认」这段逻辑本身。
        with tempfile.TemporaryDirectory() as d:
            Path(d, "EDITION").write_text("lifehall\n", encoding="utf-8")
            with mock.patch.dict(os.environ, _without_edition_env(), clear=True):
                edition.reload()
                self.assertEqual(edition.value(root=Path(d)), "lifehall")

    def test_仓库根是主包(self):
        """main 分支的仓库根必须落 `full` —— 主包机器装错版 = 菜单整片被裁。

        ⚠ 这条是**分支身份的钉子**：lifehall 分支上 `EDITION` 写 lifehall、
        这条测试会红 —— 那正是它该红的时候（合回时逼人做选择，防误合）。
        """
        with mock.patch.dict(os.environ, _without_edition_env(), clear=True):
            edition.reload()
            self.assertEqual(edition.value(), "full")
            self.assertFalse(edition.is_lifehall())

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
        self.assertIn("theme", edition.LIFEHALL_PAGES)
        self.assertIn("linglong", edition.LIFEHALL_PAGES)
        self.assertIn("general", edition.LIFEHALL_PAGES)
        self.assertIn("update", edition.LIFEHALL_PAGES)   # 自更新引擎在跑，页面得看得见
        self.assertNotIn("account", edition.LIFEHALL_PAGES)  # 云商账号设置，生活馆没有（用户定）
        self.assertNotIn("badge", edition.LIFEHALL_PAGES)
        self.assertEqual(set(edition.LIFEHALL_BUILTIN_STEPS),
                         {"dump", "autoupdate"})

    def test_pruned前缀匹配(self):
        self.assertTrue(edition.pruned("src/erp.py"))
        self.assertTrue(edition.pruned("src/features/compliance/pos/x.py"))
        self.assertTrue(edition.pruned("src\\features\\compliance\\__init__.py"))
        self.assertFalse(edition.pruned("src/features/tools/claim/pending.py"))
        self.assertFalse(edition.pruned("src/erp_helpers.py"))  # 前缀不能误伤


# ✅ 2026-09-26：T2 已落地（`build_all()` / `_children()` / `builtin_steps()` 的
#   edition 分支），blocked-on-T2 的 skip 已摘 —— 下面两条现在就该绿。
class Test裁剪树能启动(unittest.TestCase):
    """把 src/ 按 PRUNE 裁一刀扔进临时目录，import 四个入口 ——
    这就是"包里缺文件但启动不崩"的铁证（比逐条断言守卫可靠）。"""

    # ⚠ 计划里这行原写成 `... registry, import src.run_daily, ...` —— 两个 import
    #   关键字是笔误，`python -c` 会直接 SyntaxError（红得莫名其妙）。合成一条。
    _ENTRY = ("import src.edition, src.features, src.features.registry, "
              "src.run_daily, src.cli, src.web, src.startup; print('OK')")

    def _stage(self, tmp: str) -> str:
        dst = Path(tmp) / "pkg"
        shutil.copytree(ROOT / "src", dst / "src",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        from src.edition import PRUNE
        for rel in PRUNE:
            if not rel.startswith("src/"):
                continue
            p = dst / rel
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        (dst / "EDITION").write_text("lifehall\n", encoding="utf-8")
        return str(dst)

    def test_裁剪后四个入口都能import(self):
        env = {k: v for k, v in os.environ.items() if k != "CBG_EDITION"}
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._stage(tmp)
            r = subprocess.run([sys.executable, "-c", self._ENTRY],
                               cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0,
                             "裁剪树 import 失败：\n%s\n%s"
                             % (r.stdout, r.stderr))
            self.assertIn("OK", r.stdout)

    def test_裁剪树注册表自检也干净(self):
        env = {k: v for k, v in os.environ.items() if k != "CBG_EDITION"}
        code = ("from src.features import registry; "
                "print('|'.join(s.cmd for s in registry.all_steps()));"
                "print(registry.validate())")
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._stage(tmp)
            r = subprocess.run([sys.executable, "-c", code],
                               cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dump", r.stdout)
            self.assertNotIn("erp-dump", r.stdout)
            self.assertIn("[]", r.stdout)      # validate() 干净


if __name__ == "__main__":
    unittest.main()
