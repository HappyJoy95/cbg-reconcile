# -*- coding: utf-8 -*-
"""生活馆版：被裁功能的接口统一 404 + 数据探针按版过滤（实施计划 Task 4）。

⚠ M17 的教训：**菜单藏起来了 ≠ 接口拦住了** —— 前缀表 `web.LIFEHALL_GONE` +
白名单例外（logout / report-bug）就是给"写个 curl 也进不来"的那道。
"""

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
from src import web                                                 # noqa: E402
from src.app import data_state                                      # noqa: E402


def _switch_to(value):
    """把 CBG_EDITION 钉成 `value`，返回**恢复原值**的清理函数。

    ⚠ 清理必须"恢复原值再 reload"，**绝不 `os.environ.pop("CBG_EDITION")`**：
    本分支的 `EDITION` 文件写着 lifehall，pop 掉 env 之后后续所有测试都会
    读到文件 = 生活馆版 —— 实测连锁打红 16 条别的测试。
    """
    old = os.environ.get("CBG_EDITION", "full")
    os.environ["CBG_EDITION"] = value
    edition.reload()

    def _restore():
        os.environ["CBG_EDITION"] = old
        edition.reload()
    return _restore


class Test接口404(unittest.TestCase):
    """生活馆：被裁功能的接口统一 404 —— 菜单藏了不等于接口拦了（M17 教训）。"""

    def setUp(self):
        self.addCleanup(_switch_to("lifehall"))

    def _gone(self, path):
        # 直接测判据函数，不起 HTTP（dispatch 路由逻辑另测）
        return web.lifehall_gone(path)

    def test_对账类接口被拦(self):
        for p in ("/api/pools/history", "/api/attain", "/api/plan",
                  "/api/pos", "/api/report", "/api/erp", "/api/inventory/ready",
                  "/api/staff"):
            self.assertTrue(self._gone(p), p)

    def test_保留接口不拦(self):
        for p in ("/api/setup", "/api/session", "/api/session/ping",
                  "/api/session/store-code",
                  "/api/hwlogin", "/api/refresh", "/api/claim-pending",
                  "/api/health", "/api/boot", "/api/report-bug"):
            self.assertFalse(self._gone(p), p)

    def test_前缀是整段匹配_不误伤兄弟路径(self):
        """`/api/report` 不许把 `/api/report-bug` 拖下水 —— 用 `p + "/"` 段边界。"""
        self.assertTrue(self._gone("/api/report/stores"))
        self.assertFalse(self._gone("/api/report-bug"))
        # 段边界之外的"同前缀不同名"也不该被拦
        self.assertFalse(self._gone("/api/possession"))
        # Lifehall 不能通过旧 ERP logout 接口清掉本机进入编码。
        self.assertTrue(self._gone("/api/store-account"))
        self.assertTrue(self._gone("/api/store-account/list"))
        self.assertTrue(self._gone("/api/store-account/logout"))

    def test_full版一律不拦(self):
        with mock.patch.dict(os.environ, {"CBG_EDITION": "full"}):
            edition.reload()
            self.assertFalse(web.lifehall_gone("/api/attain"))
            self.assertFalse(web.lifehall_gone("/api/report"))
        edition.reload()  # patch.dict 已还原 env —— 缓存也得跟着还原

    def test_404拦截排在登录门禁之前(self):
        """404 要先于"还没登录好"的 403 —— 否则未登录 curl 云商接口会得到误导性 403。

        ⚠ 这条钉的是**源码里的顺序**（不起 HTTP：门禁里要算 `role_scope()`，
        夹具里没有真门店配置）。`lifehall_gone(path)` 那行必须出现在
        `SETUP_ALLOW` 门禁那行**之前**。
        """
        src = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")
        i_gone = src.index("if lifehall_gone(path, app.root)")
        i_gate = src.index('if path.startswith("/api/") and (not path.startswith(SETUP_ALLOW) or setup_aware):')
        self.assertLess(i_gone, i_gate, "404 拦截必须排在 SETUP_ALLOW 门禁之前")


class Test数据探针按版(unittest.TestCase):
    """生活馆没有云商池 —— 五态横幅里画出 `erp-*` 探针会永远"没抓过"，纯吓人。"""

    def setUp(self):
        self.addCleanup(_switch_to("lifehall"))

    def test_生活馆滤掉云商探针(self):
        keys = [p["key"] for p in data_state.probes()]
        self.assertEqual(keys, ["dump", "lg-stock"], keys)
        self.assertFalse(any(k.startswith("erp") for k in keys), keys)

    def test_full版四个探针都在(self):
        with mock.patch.dict(os.environ, {"CBG_EDITION": "full"}):
            edition.reload()
            keys = [p["key"] for p in data_state.probes()]
        edition.reload()
        self.assertEqual(keys, ["dump", "erp-sales", "lg-stock", "erp-stock"], keys)

    def test_生活馆的五态横幅里没有云商那两条(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = data_state.data_state(Path(d))   # 空 root：没有 out/ → 走"没找到库"那条
        keys = [s["key"] for s in st["sources"]]
        self.assertEqual(keys, ["dump", "lg-stock"], keys)

    def test_原始清单_SOURCES_不许被改(self):
        """`probes()` 是过滤器，`SOURCES` 仍是**单源清单** —— full 版靠它。"""
        self.assertEqual([s["key"] for s in data_state.SOURCES],
                         ["dump", "erp-sales", "lg-stock", "erp-stock"])


class Test定时器按版(unittest.TestCase):
    """计划 Task 4 Step 5（防御性）：lifehall 下 STEPS 少了 erp-dump，
    调顺序的校验不许因此把还剩下的步骤也判成"不认识的"。"""

    def setUp(self):
        self.addCleanup(_switch_to("lifehall"))

    def test_定时器改顺序在生活馆不炸(self):
        import tempfile
        from src.modules import timer
        with tempfile.TemporaryDirectory() as d:
            got = timer.set_order(Path(d), ["dump", "autoupdate"])
        self.assertTrue(got["ok"])
        self.assertEqual(got["order"][:2], ["dump", "autoupdate"])


class Test生活馆悬浮状态(unittest.TestCase):
    def test_状态抽屉不加载云商推送和定时器(self):
        restore = _switch_to("lifehall")
        self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config").mkdir()
            cfg = root / "config" / "store-X.yaml"
            cfg.write_text('store_code: "X123"\n', encoding="utf-8")
            app = web.App(root, str(cfg))
            with mock.patch.object(web.mailer, "describe_mail",
                                   side_effect=AssertionError("不应读取邮件配置")), \
                    mock.patch.object(web.wecom, "describe_wecom",
                                      side_effect=AssertionError("不应读取企微配置")), \
                    mock.patch.object(web.timer, "wakes",
                                      side_effect=AssertionError("生活馆没有定时执行")):
                rows = app.status_brief()["rows"]
        labels = [row["label"] for row in rows]
        self.assertNotIn("推送通道", labels)
        self.assertNotIn("上次自动跑", labels)
        self.assertIn("玲珑会话", labels)


if __name__ == "__main__":
    unittest.main()
