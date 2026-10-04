# -*- coding: utf-8 -*-
"""生活馆版的登录门禁 + 登录页/首屏的前端接线（实施计划 Task 5）。

后端判据一句话：**生活馆只认手输并保存的门店编码**，两种安装方式共用判据。
玲珑授权独立，旧会话或预览均不能替代门店编码；full 的其它入口保留两步判据。

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
from unittest import mock
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

    def test_没编码不放行_直接给编码表单(self):
        st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "storecode")
        self.assertEqual(st["entry_kind"], "lifehall")
        self.assertTrue(st["lifehall"])
        self.assertTrue(st["erp"]["ok"])
        self.assertFalse(st["preview_available"])
        self.assertFalse(st["preview"])

    def test_旧预览和会话不能替代编码(self):
        web.set_preview(self.app, True)
        p = self.app.session_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"cookies": "a=b", "csrf": "xxxx"}', encoding="utf-8")
        st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "storecode")
        self.assertFalse(st["preview"])

    def test_两种生活馆入口只读编码_不识别身份或会话(self):
        for installed in ("lifehall", "full"):
            for code in ("", "自填店码"):
                with self.subTest(installed=installed, code=code):
                    os.environ["CBG_EDITION"] = installed
                    edition.reload()
                    self.app.config_path.write_text('store_code: "%s"\n' % code,
                                                    encoding="utf-8")
                    if installed == "full":
                        web.save_entry(self.app, "lifehall")
                    with mock.patch.object(web, "describe_store_credentials",
                                           side_effect=AssertionError("不能读 ERP")), \
                            mock.patch.object(web.config_io, "store_profile",
                                              side_effect=AssertionError("不能认店")), \
                            mock.patch.object(self.app, "session_info",
                                              side_effect=AssertionError("不能查会话")), \
                            mock.patch.object(web, "preview_on",
                                              side_effect=AssertionError("不能查预览")):
                        st = web.setup_state(self.app)
                    self.assertEqual(st["ready"], bool(code))
                    self.assertEqual(st["need"], "" if code else "storecode")
                    self.assertEqual(st.get("lifehall", False), installed == "lifehall")
                    self.assertFalse(st["profile"]["platform"])
                    self.assertEqual(st["entry_kind"], "lifehall")

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
        self.js = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js", "app.js"))
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_showSetup有生活馆分支(self):
        # Lifehall code-only entry is handled before rendering the ERP login mask.
        self.assertIn("setupState.entry_kind === 'lifehall'", self.js)
        self.assertIn("if (setupState.need === 'storecode') showEntry({ code: true });",
                      self.js)
        self.assertIn("else if (setupState.ready)", self.js)
        self.assertNotIn('id="setup-linglong-lifehall-note"', self.html)

    def test_登录页跳过按钮生活馆不藏(self):
        # 老写法 `pv.hidden = LH || ...` 会把生活馆的跳过按钮藏掉 —— 钉死新写法
        self.assertIn("pv.hidden = !(setupState && setupState.preview_available)",
                      self.js)

    def test_首屏落地会找可见页签(self):
        self.assertIn("active.hidden", self.js)

    def test_首屏兜底落到生活馆唯一一级页(self):
        # 首屏选择已抽到公共导航；权限加载后选首个可见业务页。
        self.assertIn("function openFirstAvailablePage()", self.js)
        self.assertIn("openFirstAvailablePage()", self.js)
        self.assertNotIn("switchTab(cur ? cur.dataset.tab : 'tools')", self.js)

    def test_生活馆设置有编码重输和玲珑直达入口(self):
        self.assertIn('id="lifehall-entry-settings-card"', self.html)
        self.assertIn('id="lifehall-current-code"', self.html)
        self.assertIn('id="btn-lifehall-edit-code"', self.html)
        self.assertIn('id="btn-lifehall-open-ll"', self.html)
        self.assertIn("showEntry({ code: true })", self.js)
        self.assertIn("switchTab('settings', 'linglong')", self.js)
        self.assertIn("lifehall-entry-settings-card", self.js)

    def test_生活馆编码页可取消且显示当前编码(self):
        self.assertIn('id="btn-entry-code-back"', self.html)
        self.assertIn("setupState.lifehall", self.js)
        self.assertIn("entry-store-code", self.js)
        self.assertIn("setupState.profile.huawei_code", self.js)

    def test_登录页那几个节点都还在(self):
        # 前端按 id 找节点，index.html 改名就等于整段静默失效
        for anchor in ("setup-step-erp", "setup-step-linglong", "setup-step-preview",
                       "setup-lead", "setup-linglong-num", "setup-linglong-why"):
            self.assertIn('id="%s"' % anchor, self.html)


if __name__ == "__main__":
    unittest.main()
