# -*- coding: utf-8 -*-
"""平台岗身份加固（2026-09-29）—— 名单旧了 / 丢了 / 写坏了，也不许静默降级成门店。

用户当天报的现场：「增值还有其他业绩达成不显示门店，只显示一个平台岗」。

排查出来的形状（本机逐条复现过）：

* 平台岗的判据只有两个信号 —— 配置里的老标志 `platform: true`，或名单
  `config/stores.yaml` 里 `kind: 平台岗` 那一行；
* **新登录流程会把老标志清成空串** ⇒ 只剩名单那一条；
* 名单一旦认不出来（缺那行 / 文件丢 / YAML 写坏），`store_profile()` 就返回
  `type=partner` ⇒ `role_scope()` 兜底成**门店**，范围 = `{"平台岗"}` 这个虚拟店名
  ⇒ 增值 / 达成 / 月度 / 四池所有按店过滤的页全滤空，界面上只剩一句
  「只看本店（平台岗）」，**看不出是身份掉了**。

这一份钉住加固后的三条：
1. 三种名单状态（缺行 / 丢文件 / 写坏）下，平台岗**都还是** `role=platform`、`stores=None`；
2. 按店过滤的页 `store_filter` 为空、明细一行不少；
3. 反过来**不许误伤**：名字不是 `平台岗` 的真门店照旧退到门店（范围=本店）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

from src import config_io, web                                     # noqa: E402

#: 正常名单：一家普通门店 + 两条真店 + **平台岗那一行**（照 `config/stores.yaml` 的形状）
ROSTER_OK = (
    "stores:\n"
    "  - marker: \"\"\n"
    "    erp_name: \"平台岗\"\n"
    '    huawei_code: ""\n'
    '    huawei_name: "盛联平台岗（不绑门店）"\n'
    "    kind: 平台岗\n"
    "  - erp_name: 青岛城阳万达店\n"
    "    region: 西北区\n"
    "    marker: D\n"
    "    huawei_code: SCN075029\n"
    "    kind: 体验店\n"
    "  - erp_name: 青岛麦凯乐店\n"
    "    region: 市区\n"
    "    huawei_code: SCN075031\n"
    "    kind: 合作店\n"
)

#: 升级前的老名单 —— **没有平台岗那一行**（用户报的问题就是它）
ROSTER_NO_PLATFORM = (
    "stores:\n"
    "  - erp_name: 青岛城阳万达店\n"
    "    region: 西北区\n"
    "    marker: D\n"
    "    huawei_code: SCN075029\n"
    "    kind: 体验店\n"
    "  - erp_name: 青岛麦凯乐店\n"
    "    region: 市区\n"
    "    huawei_code: SCN075031\n"
    "    kind: 合作店\n"
)

#: 手抖写坏的名单（`load_raw` 里 `yaml.safe_load` 会抛 —— 原先正好被 role_scope 吞掉）
ROSTER_BROKEN = "stores: [ : ::\n"


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "out").mkdir(parents=True, exist_ok=True)

    def write_roster(self, text):
        (self.root / "config" / "stores.yaml").write_text(text, encoding="utf-8")

    def app(self, cfg_text):
        (self.root / "config" / "store-X.yaml").write_text(cfg_text, encoding="utf-8")
        # ⚠ 服务进程探针不打（跟 test_roles 一个做法）
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            return web.App(self.root, "config/store-X.yaml")

    #: 平台岗登录之后配置的样子：老标志被清成空串，只剩虚拟店名
    PLATFORM_CFG = ('erp_store_name: "平台岗"\nplatform: ""\n'
                    'store_code: ""\nmarker: ""\n')

    def scope_with_roster(self, roster_text, cfg_text=None):
        if roster_text is None:
            pass                                  # 名单文件整个不写 = 丢了
        else:
            self.write_roster(roster_text)
        return web.role_scope(self.app(cfg_text or self.PLATFORM_CFG))


class Test平台岗不靠名单那行(_Base):
    """⚠ 这一组就是用户报的那件事：名单认不出来时，平台岗原来会掉成门店。"""

    def _assert_platform(self, sc):
        self.assertEqual(sc["role"], "platform", "平台岗被降级了（用户报的那个 bug）")
        self.assertIsNone(sc["stores"], "平台岗 = 不过滤（stores=None）")
        self.assertIn("compliance", sc["pages"], "五项合规那一页不该对平台岗藏起来")
        self.assertIn("stores", sc["pages"], "数据交换（multi）不该对平台岗藏起来")
        self.assertIn("linglong", sc["pages"], "玲珑授权不该对平台岗藏起来")

    def test_名单里没有平台岗那行_仍然是平台岗(self):
        """旧名单（升级前手工拷的那份）—— 最可能就是门店现场的形状。"""
        self._assert_platform(self.scope_with_roster(ROSTER_NO_PLATFORM))

    def test_名单文件丢了_仍然是平台岗(self):
        self._assert_platform(self.scope_with_roster(None))

    def test_名单写坏了_仍然是平台岗(self):
        self._assert_platform(self.scope_with_roster(ROSTER_BROKEN))

    def test_正常名单照旧认平台岗(self):
        """加固不许把好的那条路改坏：名单里那行还在时，行为与以前一字不差。"""
        sc = self.scope_with_roster(ROSTER_OK)
        self._assert_platform(sc)
        prof = config_io.store_profile({"erp_store_name": "平台岗"}, self.root)
        self.assertTrue(prof["in_roster"], "名单命中时要照实说命中")
        self.assertEqual(prof["huawei_name"], "盛联平台岗（不绑门店）")

    def test_按名字认时_in_roster_照实为假(self):
        """名单里没命中就是没命中 —— 别写死 True 骗下一个读它的人。"""
        prof = config_io.store_profile({"erp_store_name": "平台岗"}, self.root)  # 还没写名单
        self.assertTrue(prof["platform"])
        self.assertFalse(prof["in_roster"])


class Test明细不被滤掉(_Base):
    """身份对了还不够 —— 页面上要真看得见 28 家店（这就是用户报的"看不到数据"）。"""

    ROWS = [{"store": "青岛城阳万达店"}, {"store": "青岛麦凯乐店"}]

    def _filtered(self, roster_text):
        self.write_roster(roster_text) if roster_text else None
        app = self.app(self.PLATFORM_CFG)
        return app.filter_attain_rows({"rows": list(self.ROWS),
                                       "summary": {}, "exists": True})

    def test_名单缺行时明细一行不少(self):
        d = self._filtered(ROSTER_NO_PLATFORM)
        self.assertEqual(d.get("store_filter"), "", "不该出现「只看本店（平台岗）」那句")
        self.assertEqual(len(d["rows"]), 2)
        self.assertIsNone(d.get("error"))

    def test_名单丢失时明细一行不少(self):
        d = self._filtered(None)
        self.assertEqual(len(d["rows"]), 2)
        self.assertEqual(d.get("store_filter"), "")


class Test不许误伤真门店(_Base):
    """反过来：名字不是「平台岗」的店，名单认不出来时**照旧**退到门店（范围最小）。"""

    CFG = 'erp_store_name: "青岛城阳万达店"\nstore_code: "SCN075029"\nmarker: "D"\n'

    def test_名单里没有本店_退到门店而不是平台岗(self):
        sc = self.scope_with_roster(ROSTER_NO_PLATFORM, cfg_text=self.CFG)
        self.assertEqual(sc["role"], "store", "真门店不该拿到全部范围")
        self.assertEqual(sc["stores"], {"青岛城阳万达店"})
        self.assertNotIn("stores", sc["pages"], "数据交换（multi）只有区长/平台看得见")

    def test_名单写坏了_门店身份不炸也不放全(self):
        sc = self.scope_with_roster(ROSTER_BROKEN, cfg_text=self.CFG)
        self.assertEqual(sc["role"], "store")
        self.assertEqual(sc["stores"], {"青岛城阳万达店"})


class Test名单读取永不抛(unittest.TestCase):
    """`stores_table()` / `managers_table()` 兑现它们自己文档里那句"不抛异常"。

    ⚠ 原来那句是**假的**：`load_raw()` 里是 `yaml.safe_load()`，名单写坏一个字就抛，
      再被 `role_scope()` 的 `except Exception` 吞掉 ⇒ 静默降级（2026-09-29 实测）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)

    def test_坏YAML给空表不抛(self):
        (self.root / "config" / "stores.yaml").write_text(ROSTER_BROKEN, encoding="utf-8")
        (self.root / "config" / "managers.yaml").write_text(ROSTER_BROKEN, encoding="utf-8")
        self.assertEqual(config_io.stores_table(self.root), [])
        self.assertEqual(config_io.managers_table(self.root), [])

    def test_文件不在也给空表(self):
        self.assertEqual(config_io.stores_table(self.root), [])
        self.assertEqual(config_io.managers_table(self.root), [])

    def test_正常名单照旧读得出来(self):
        (self.root / "config" / "stores.yaml").write_text(ROSTER_OK, encoding="utf-8")
        self.assertEqual(len(config_io.stores_table(self.root)), 3)


class Test配置读炸了也不许500(_Base):
    """⚠ `cfg` 原来只在 `try` **里面**绑定 —— 配置 YAML 写坏时它压根没绑上。

    `except` 只兜了 `prof`，而门店兜底那段（`role_scope()` 第 ③ 段）还要读
    `cfg.get("erp_store_name")` ⇒ **`UnboundLocalError`**。
    而这个函数**每个 `/api/*` 请求都要算一次** —— 那一刻整站接口全 500，
    比"身份掉成门店"严重得多（掉身份至少还看得见页面）。

    2026-09-29 复现：`config_io.load_raw` 一抛，`role_scope` 当场炸。
    ⇒ `cfg = {}` 挪到 try **外面**，读不到就当"认不出这家店"，照旧退到门店兜底。
    """

    def test_配置YAML写坏_不抛且退到最小范围(self):
        app = self.app("platform: [ :\n")            # yaml.safe_load 会抛
        sc = web.role_scope(app)                      # ← 关键：不许抛
        self.assertEqual(sc["role"], "store", "认不出就退到门店（范围最小）")
        self.assertEqual(len(sc["stores"]), 0, "认不出店名 ⇒ 不给任何一家店")
        self.assertIn("还没认出是哪家店", sc["label"], "界面上要说清是没认出来")

    def test_配置读炸时画像不许被当成平台岗(self):
        """读不出「我是谁」时**不能**顺手放成平台岗 —— 那是把范围放大到全部门店。"""
        sc = web.role_scope(self.app("platform: [ :\n"))
        self.assertFalse(sc["stores"] is None, "配置坏不等于平台岗")

    def test_load_raw整个炸也要扛住(self):
        """不只 yaml 语法错 —— 读文件本身出意外（权限 / 半截文件）同样不许 500。"""
        app = self.app(self.PLATFORM_CFG)
        with mock.patch.object(config_io, "load_raw", side_effect=RuntimeError("boom")):
            sc = web.role_scope(app)
        self.assertEqual(sc["role"], "store", "画像读不到 ⇒ 退到最小范围")
        self.assertEqual(sc["stores"], set(), "没名字就不给范围")


if __name__ == "__main__":
    unittest.main()
