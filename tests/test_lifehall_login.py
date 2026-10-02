# -*- coding: utf-8 -*-
"""生活馆版的登录门禁 + 登录页/首屏的前端接线（实施计划 Task 5）。

后端判据一句话：**生活馆只认玲珑一步，有会话文件就放行**（不卡自检）——
见 `src/web.py::_setup_state_lifehall`。full 版走原来的两步判据，
而且返回体里**不许有 `lifehall` 键**（前端拿到 `lifehall: false` 也会当真判）。

⚠ 切版的测试 **cleanup 必须恢复原值**（conftest 把 `CBG_EDITION` 钉成 full），
   绝不 `os.environ.pop("CBG_EDITION")` —— 本分支 `EDITION` 文件=lifehall，
   pop 掉 env 之后 edition 读文件 → 后面的测试全按 lifehall 跑（连锁打红）。

⚠ `src.web` 在**模块级**import（collection 时 env 还是 full）：web.py 的
   erp / erp_stub 选择是**模块顶层**做的，放进 setUp 里 import 会让 web
   按 lifehall 绑死名字，反过来毒掉 test_setup_gate 这类 full 测试。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import edition, web                                 # noqa: E402


def _restore_edition_env(old):
    """把 `CBG_EDITION` 恢复成跑这条测试之前的值，再清 edition 的文件缓存。"""
    os.environ["CBG_EDITION"] = old if old else "full"        # ⚠ 恢复，绝不 pop
    edition.reload()


class Test生活馆登录门禁(unittest.TestCase):
    def setUp(self):
        # ⚠ 先记原值再切版（cleanup 用它恢复 —— 见文件头那条硬规矩）
        self._old_edition = os.environ.get("CBG_EDITION")
        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(_restore_edition_env, self._old_edition)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        # 名单要拷真的 —— profile（认店/标识）的判据全靠它
        shutil.copy(ROOT / "config" / "stores.yaml",
                    self.root / "config" / "stores.yaml")
        (self.root / "config" / "store-X.yaml").write_text(
            "erp_store_name: \nstore_code: \nmarker: \n", encoding="utf-8")
        self.app = web.App(self.root, "config/store-X.yaml")

    def test_没会话不放行(self):
        st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "linglong")
        self.assertTrue(st["lifehall"])
        self.assertTrue(st["erp"]["ok"])        # 云商那步直接算过
        # 没会话 ⇒ 登录页给「跳过」按钮（用户 2026-09-30 要的）
        self.assertTrue(st["preview_available"])
        self.assertFalse(st["preview"])         # 但还没真开预览

    def test_跳过预览就放行_退出回登录页(self):
        """「跳过按钮」只放行界面：ready 翻真、preview 挂标、need 清空。"""
        self.assertTrue(web.set_preview(self.app, True)["ok"])
        st = web.setup_state(self.app)
        self.assertTrue(st["ready"])
        self.assertTrue(st["preview"])          # 前端据此一直挂横幅
        self.assertEqual(st["need"], "")
        self.assertTrue(st["preview_available"])  # 还没会话 ⇒ 按钮还在（可退出）
        web.set_preview(self.app, False)
        st2 = web.setup_state(self.app)
        self.assertFalse(st2["ready"])
        self.assertEqual(st2["need"], "linglong")

    def test_有会话文件就放行_不卡自检(self):
        # ⚠ store_code 为空 ⇒ 会话文件是 cbg-default.json（`auth.session_path` 的
        #   口径），与 `session_info()` 读的是同一个路径。
        p = self.app.session_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"cookies": "a=b", "csrf": "xxxx"}', encoding="utf-8")
        st = web.setup_state(self.app)
        self.assertTrue(st["ready"])
        self.assertEqual(st["need"], "")
        self.assertFalse(st["preview_available"])   # 有会话就不给跳过按钮

    def test_erp的why文案给抽屉和日志读(self):
        st = web.setup_state(self.app)
        self.assertIn("云商", st["erp"]["why"])

    def test_full版判据不变(self):
        os.environ["CBG_EDITION"] = "full"
        edition.reload()
        st = web.setup_state(self.app)
        self.assertFalse(st["ready"])          # full 下没会话 + 没云商账号同样不放行
        self.assertNotIn("lifehall", st)        # 前端拿到 lifehall:false 也会当真判
        self.assertFalse(st["preview_available"])


class Test前端接线(unittest.TestCase):
    """钉源码模式 —— app.js 无构建步骤，只能断言源码里长这样。"""

    def setUp(self):
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_showSetup有生活馆分支(self):
        self.assertIn("setupState.lifehall", self.js)
        self.assertIn("$('#setup-step-erp').hidden = LH", self.js)

    def test_登录页跳过按钮生活馆不藏(self):
        # 老写法 `pv.hidden = LH || ...` 会把生活馆的跳过按钮藏掉 —— 钉死新写法
        self.assertIn("pv.hidden = !(setupState && setupState.preview_available)",
                      self.js)

    def test_首屏落地会找可见页签(self):
        self.assertIn("active.hidden", self.js)

    def test_首屏兜底落到生活馆唯一一级页(self):
        # boot 固定的 switchTab('sales') 在生活馆被藏 —— 兜底切到 'tools'
        self.assertIn("switchTab(cur ? cur.dataset.tab : 'tools')", self.js)

    def test_登录页那几个节点都还在(self):
        # 前端按 id 找节点，index.html 改名就等于整段静默失效
        for anchor in ("setup-step-erp", "setup-step-linglong", "setup-step-preview",
                       "setup-lead", "setup-linglong-num", "setup-linglong-why"):
            self.assertIn('id="%s"' % anchor, self.html)


if __name__ == "__main__":
    unittest.main()
