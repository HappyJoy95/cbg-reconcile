"""每天**服务刚启动时**自动刷新一次（`src/startup.py`）。

用户 2026-09-18：「加个每天系统刚启动时自动更新数据的逻辑呗，**具体更新什么我们再定**」。

所以这个模块的重点不是"刷什么"，而是**机制**：
一天只跑一次、后台线程、一条坏了不拖累别的、**绝不让服务起不来**。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import startup                                             # noqa: E402
from src.features.store import staff as app_staff                              # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = mock.Mock()
        self.app.root = self.root
        self.said = []
        self.say = self.said.append


class Test一天只跑一次(_Base):
    def test_没记录就跑(self):
        self.assertTrue(startup.should_run(self.root))

    def test_跑完记当天(self):
        startup.mark(self.root)
        self.assertFalse(startup.should_run(self.root))
        self.assertEqual(startup.last_run(self.root),
                         __import__("datetime").date.today().isoformat())

    def test_换一天又跑(self):
        """⚠ 按**日期**记，不是"跑过没" —— 门店那台机器一天可能开关好几次，
        不按日期的话第二天开机不会刷。"""
        startup.mark(self.root, today="2026-09-18")
        self.assertFalse(startup.should_run(self.root, today="2026-09-18"))
        self.assertTrue(startup.should_run(self.root, today="2026-09-19"))

    def test_记录坏了就当没跑过(self):
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        startup._state_path(self.root).write_text("不是 json", encoding="utf-8")
        self.assertTrue(startup.should_run(self.root), "读坏了就该重跑一次，别卡死")


class Test一条坏了不拖累别的(_Base):
    def test_坏的那条记失败_好的照跑(self):
        ok = mock.Mock(return_value="好了")
        bad = mock.Mock(side_effect=RuntimeError("炸了"))
        with mock.patch.object(startup, "TASKS", [("坏的", bad), ("好的", ok)]):
            r = startup.run_once(self.app, say=self.say)
        self.assertTrue(r["ran"])
        self.assertIn("失败", r["results"]["坏的"])
        self.assertEqual(r["results"]["好的"], "好了")
        self.assertTrue(ok.called, "前一条坏了就不跑后面的？")

    def test_坏了也算今天跑过(self):
        """⚠ 不然下次开机又从头再来一遍，**坏的那条会一直挡着**后面所有的。"""
        with mock.patch.object(startup, "TASKS",
                               [("坏的", mock.Mock(side_effect=RuntimeError("x")))]):
            startup.run_once(self.app, say=self.say)
        self.assertFalse(startup.should_run(self.root))

    def test_今天跑过就不跑(self):
        startup.mark(self.root)
        r = startup.run_once(self.app, say=self.say)
        self.assertFalse(r["ran"])
        self.assertIn("今天", r["reason"])

    def test_force_能强制跑(self):
        startup.mark(self.root)
        with mock.patch.object(startup, "TASKS", [("好", mock.Mock(return_value="ok"))]):
            self.assertTrue(startup.run_once(self.app, force=True, say=self.say)["ran"])


class Test不许拖慢也不许弄挂启动(_Base):
    def test_后台线程(self):
        """⚠ 必须后台 —— 启动路径上同步去拉云商，控制台要等好几秒才出来。"""
        with mock.patch.object(startup, "TASKS", []):
            t = startup.start_background(self.app, say=self.say)
            t.join(timeout=5)
        self.assertTrue(t.daemon, "不是 daemon 的话关服务时会被它拖住")

    def test_整轮炸了也不往上抛(self):
        """⚠ 这是**服务启动路径**（`web.serve` 里调的）——
        这里抛出去就是"服务起不来"，而刷新失败跟能不能用一点关系都没有。"""
        with mock.patch.object(startup, "run_once",
                               side_effect=RuntimeError("整轮炸")):
            t = startup.start_background(self.app, say=self.say)   # 不许抛
            t.join(timeout=5)
        self.assertTrue(any("整轮失败" in s for s in self.said))

    def test_记不上也不抛(self):
        """⚠ 磁盘满 / 没权限时 `mark` 抛出去 = 服务起不来。"""
        with mock.patch.object(Path, "write_text", side_effect=OSError("没权限")):
            startup.mark(self.root)                                # 不许抛


class Test接在正确的位置(unittest.TestCase):
    def test_serve_里排在启动提示之后(self):
        """⚠ 顺序有讲究：先让人看到"控制台已启动"，再去后台刷。
        排在前面的话，刷新一旦慢（拉云商要几秒），启动提示会跟着晚出来。"""
        from src import web as webmod
        import inspect
        src = inspect.getsource(webmod.serve)
        self.assertIn("startup.start_background(app)", src)
        self.assertLess(src.index("控制台已启动"), src.index("startup.start_background(app)"))

    def test_serve_里包了try(self):
        """⚠ 启动路径上不许抛（AGENTS.md 坑 2）。"""
        from src import web as webmod
        import inspect
        src = inspect.getsource(webmod.serve)
        i = src.index("startup.start_background(app)")
        self.assertIn("except Exception", src[i:i + 400])


if __name__ == "__main__":
    unittest.main()


class Test人员刷新走业务模块(_Base):
    """阶段 2 的 **2.3**：`startup` **不再反向依赖 HTTP 层**。

    原来 `_refresh_roster` 里是 `from . import web` + `web.staff_state(app)` ——
    业务逻辑长在 `web.py` 里，启动刷新为了复用它只好去 import HTTP 模块。
    现在两边都调执行模块 `src/features/store/staff.py`（结构那条在 `test_module_layout.py`）。
    """

    def test_调的是_app_staff_而不是_web(self):
        self.app.config_path = "config/store-X.yaml"
        self.app.erp_env_file.return_value = ".secrets/erp.env"
        seen = {}

        def fake_staff(root=None, config_path=None, env_file=None):
            seen.update(root=root, config_path=config_path, env_file=env_file)
            return {"ok": True, "count": 7, "active_count": 5}

        with mock.patch.object(app_staff, "staff_state", fake_staff):
            out = startup._refresh_roster(self.app)
        self.assertIn("7", out)
        self.assertIn("5", out)
        self.assertEqual(seen["root"], self.root)
        self.assertEqual(seen["config_path"], "config/store-X.yaml")
        self.assertEqual(seen["env_file"], ".secrets/erp.env")

    def test_读不出来时说的是跳过(self):
        with mock.patch.object(app_staff, "staff_state",
                               lambda *a, **k: {"ok": False, "error": "还没认出这家店"}):
            self.assertIn("跳过", startup._refresh_roster(self.app))
