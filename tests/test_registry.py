"""业务功能的**安装注册机制**（`src/features/registry.py`）—— 用户 2026-09-19 要的。

⚠ 这一组盯的是"**只做接入**"能不能兑现：

1. **派生出来的值与手写时逐字段一致**（否则重构就改了行为）；
2. **加一个功能只动两处**（自己的文件夹 + 父模块的一行），步骤表/默认勾选自动跟着变；
3. **校验能拦住撞车**（两个功能抢同一个菜单 key / 步骤名 —— 那种事必须启动时当场报）；
4. **注册表与控制台菜单不漂移**（`index.html` 的 `data-tab` / `data-subtab`）。
"""

import unittest
from pathlib import Path
from unittest import mock

from src import run_daily
from src.features import ALL, registry
from src.features.registry import Feature, Step, Sub

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


class Test派生出来的值跟手写时一致(unittest.TestCase):
    """重构不许改行为 —— 这些值原来是手写的（`git show HEAD:src/run_daily.py`）。

    ⚠ **加步骤时这四条要一起改**（这是有意的"重新基线"）：
      2026-09-19 加了 `attain`（销售达成）；
      **2026-09-20 加了 `erp-dump`（抓取云商数据）** —— 用户：「数据抓取再加个
      云商数据定时抓取吧」⇒ 云商那两个池子从 `dump` 里拆出来单独一步。
      两次都顺手证明了派生这条路是通的：注册表里加一个 `Step`，这四张表**自动就对**了。
    """

    def test_步骤(self):
        # ⚠ 2026-09-21（M18）加了 **`report`（上报数据）+ `report-inbox`（收取门店上报）**
        #   —— 又一次证明派生这条路是通的：注册表里加 `Step`，这几张表自动就对。
        # ⚠ 2026-09-21（M22）加了 **`plan`（月度生意计划）** —— 又一次证明派生这条路
        #   是通的：注册表里加 `Step`，这几张表自动就对。
        # ⚠ 2026-09-22 加了 **`benefit`（无忧会员权益）** —— 同一条路。
        self.assertEqual(run_daily.STEPS,
                         ("dump", "erp-dump", "pos", "pools", "attain", "plan",
                          "film", "benefit", "autoupdate", "report", "report-inbox"))

    def test_步骤中文名(self):
        self.assertEqual(run_daily.STEP_LABELS,
                         {"dump": "抓取玲珑数据", "erp-dump": "抓取云商数据",
                          "pos": "POS 合规",
                          "pools": "双平台数据对比", "attain": "销售达成",
                          "plan": "月度生意计划",
                          "film": "防护膜达成（落快照）",
                          "benefit": "无忧会员权益（落快照）",
                          "autoupdate": "自动更新",
                          "report": "上报数据", "report-inbox": "收取门店上报"})

    def test_跳过开关(self):
        self.assertEqual(run_daily.STEP_FLAGS,
                         {"dump": "--skip-dump", "erp-dump": "--skip-erp-dump",
                          "pos": "--skip-pos",
                          "pools": "--skip-pools", "attain": "--skip-attain",
                          "plan": "--skip-plan", "film": "--skip-film",
                          "benefit": "--skip-benefit",
                          "autoupdate": "--skip-autoupdate",
                          "report": "--skip-report", "report-inbox": "--skip-report-inbox"})

    def test_自动化默认勾选(self):
        # ⚠ 2026-09-21 晚：`report`（上报数据）**有自己的时间**（21:15）⇒ 不在整批里；
        #   `report-inbox`（21:30）也不在 —— 跟 `autoupdate` 一个道理。
        # ⚠ M22 的 `plan` **进整批**：它跟 attain 一样是"每天重算当月至今"，
        #   不另设时刻（跟着 21:00 那趟）。
        self.assertEqual(run_daily.MANUAL_STEPS,
                         ("dump", "erp-dump", "pos", "pools", "attain", "plan"))

    def test_注册表自己也是这仨(self):
        self.assertEqual(registry.steps(), run_daily.STEPS)
        self.assertEqual(registry.step_labels(), run_daily.STEP_LABELS)


class Test加一个功能只动两处(unittest.TestCase):
    """⭐ 这是"只做接入"的实证：往注册表加一条，步骤表/默认勾选**自动跟着变**。"""

    def _fake(self):
        return Feature(key="demo", label="演示功能", order=99, types="",
                       children=[Sub(key="demo-x", label="演示子模块", order=10,
                                     step=Step(cmd="demo-x", label="演示步骤", order=99,
                                               default=True))])

    def test_加了就出现在步骤表里(self):
        with mock.patch.object(registry, "all_features", lambda: list(ALL) + [self._fake()]):
            self.assertIn("demo-x", registry.steps())
            self.assertIn("demo-x", registry.step_labels())
            self.assertEqual(registry.step_flags()["demo-x"], "--skip-demo-x")
            self.assertIn("demo-x", registry.default_steps())

    def test_顺位由_order_决定(self):
        with mock.patch.object(registry, "all_features", lambda: list(ALL) + [self._fake()]):
            self.assertEqual(registry.steps()[-1], "demo-x", "order=99 应该排最后")

    def test_不要默认勾就不勾(self):
        f = self._fake()
        f.children[0].step.default = False
        with mock.patch.object(registry, "all_features", lambda: list(ALL) + [f]):
            self.assertNotIn("demo-x", registry.default_steps())


class Test校验能拦住撞车(unittest.TestCase):
    def _bad(self, features):
        with mock.patch.object(registry, "all_features", lambda: list(features)):
            return registry.validate()

    def test_功能_key_重复(self):
        a = Feature(key="x", label="甲")
        b = Feature(key="x", label="乙")
        self.assertTrue(any("功能 key 重复" in m for m in self._bad([a, b])))

    def test_子模块_key_重复(self):
        a = Feature(key="a", label="甲", children=[Sub(key="s", label="设置")])
        b = Feature(key="b", label="乙", children=[Sub(key="s", label="设置")])
        self.assertTrue(any("子模块 key 重复" in m for m in self._bad([a, b])))

    def test_步骤名重复(self):
        a = Feature(key="a", label="甲", children=[Sub(key="a1", label="一",
                                                       step=Step(cmd="dup", label="步骤"))])
        b = Feature(key="b", label="乙", children=[Sub(key="b1", label="一",
                                                       step=Step(cmd="dup", label="步骤"))])
        self.assertTrue(any("定时步骤重复" in m for m in self._bad([a, b])))

    def test_没中文名也报(self):
        self.assertTrue(any("没有中文名" in m
                            for m in self._bad([Feature(key="a", label="")])))

    def test_现在是干净的(self):
        self.assertEqual(registry.validate(), [])


class Test注册表与控制台菜单不漂移(unittest.TestCase):
    """⚠ 菜单现在还在 `index.html` 里手写 —— 那就**先钉住"两边一致"**。

    等前端也从注册表派生（下一步），这条测试就可以退休了；
    在那之前，它是唯一能拦住"注册表改了、菜单没改"的东西。
    """

    def test_每个一级菜单在_HTML_里都有(self):
        for m in registry.menus():
            self.assertIn('data-tab="%s"' % m["key"], INDEX,
                          "注册表里有 %s，页面上没有" % m["key"])

    def test_每个子模块在_HTML_里都有(self):
        for m in registry.menus():
            for c in m["children"]:
                self.assertIn('data-subtab="%s"' % c["key"], INDEX,
                              "注册表里有子模块 %s，页面上没有" % c["key"])

    def test_页面上的一级标签也都注册了(self):
        """反向：页面上有的，注册表里也得有（否则就是漏登记）。"""
        import re
        keys = {m["key"] for m in registry.menus()}
        for got in re.findall(r'<button class="tab[^"]*" data-tab="([^"]+)"', INDEX):
            self.assertIn(got, keys, "页面上有 %s，注册表里没有" % got)

    def test_左下角那组不算功能菜单(self):
        """`settings`（账号/人员/通用/玲珑）是**左下角**那组，不是一级功能菜单 ——
        ⚠ 别为了"整齐"把它塞进注册表，它的位置和交互是另一套。"""
        self.assertNotIn("settings", {m["key"] for m in registry.menus()})

    def test_可见性跟着一级走(self):
        """子模块没写 `types` 就继承父的 —— 合作店看不到「五项合规」，三个子一起消失。"""
        comp = [m for m in registry.menus() if m["key"] == "compliance"][0]
        for c in comp["children"]:
            self.assertEqual(c["types"], "experience platform")
