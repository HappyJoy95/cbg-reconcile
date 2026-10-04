# -*- coding: utf-8 -*-
"""注册协议 v2 —— 操作/数据范围声明、统一执行、注册表执行入口。

**为什么要单独钉**（2026-10-02 功能注册制，开发目标 §4）：

1. 声明（`Sub.ops` / `Sub.data` / `Step.run` / `partner_ok` / `fatal`）**只写一处**，
   后端 403（`require`）、`role.can`、前端显隐（`role.ops`）、数据过滤
   （`filter_scoped`）、合作店早退与失败中止**全部从它派生** ——
   任何"第二份判据"都要在这儿变红（这个项目为两份定义栽过太多次）；
2. **未声明 = 拒绝**（不是放行）：漏声明的表现必须是 403，而不是"静默多给"；
3. 每一步都必须在注册表声明唯一的 `Step.run` 执行入口。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import run_daily, web                                    # noqa: E402
from src.features import registry                                 # noqa: E402
from src.features.valueadd.film import SUB as FILM_SUB           # noqa: E402


def _scope(role, stores=None, label=""):
    if stores is None and role in ("store", "manager"):
        stores = {"甲店"}
    return {"role": role, "label": label or role, "stores": stores,
            "who": "测试", "account": "SL0"}


class Test声明与派生(unittest.TestCase):
    """词表、字段、`page_perms()` 的继承与覆盖。"""

    def test_film声明形状(self):
        """试点声明 = 后端 403 与前端显隐的**唯一判据**（改口径只改注册表）。"""
        self.assertEqual(set(FILM_SUB.ops), {"view", "export"})
        self.assertEqual(FILM_SUB.ops["view"], ("store", "manager", "platform"))
        self.assertEqual(FILM_SUB.ops["export"], ("manager", "platform"))
        self.assertEqual(FILM_SUB.data, "authorized")
        self.assertTrue(callable(FILM_SUB.step.run), "film 的执行入口没挂上")

    def test_词表只此一份(self):
        self.assertEqual(registry.OPS, ("view", "enter", "modify", "export"))
        self.assertEqual(set(registry.OP_LABELS), set(registry.OPS))
        self.assertEqual(registry.OP_ROLES, ("store", "manager", "platform"))
        self.assertEqual(registry.DATA_SCOPES, ("store", "authorized"))

    def test_validate拦住坏声明(self):
        """声明错词表要在**启动自检**里当场报（validate 是硬门槛）。"""
        bad_feature = registry.Feature(
            key="x", label="X",
            children=[registry.Sub(key="x1", label="X1",
                                   ops={"destroy": ("store",)},     # 不在词表
                                   data="everything")])            # 不在词表
        bad_ops_role = registry.Feature(
            key="y", label="Y",
            children=[registry.Sub(key="y1", label="Y1",
                                   ops={"view": ("root",)})])       # 不认识的身份
        with mock.patch.object(registry, "all_features",
                               return_value=[bad_feature, bad_ops_role]):
            bad = registry.validate()
        self.assertTrue(any("不存在的操作" in b for b in bad), bad)
        self.assertTrue(any("data=" in b for b in bad), bad)
        self.assertTrue(any("不认识的身份" in b for b in bad), bad)

    def test_page_perms继承与覆盖(self):
        """子不写就继承父；子写了**整块覆盖**（跟 `types` 一个思路）。"""
        feat = registry.Feature(
            key="f", label="F",
            ops={"view": ("store", "platform")}, data="authorized",
            children=[
                registry.Sub(key="inherit", label="继承"),           # 两样都继承
                registry.Sub(key="own", label="覆盖",
                             ops={"export": ("platform",)}, data="store"),
            ])
        with mock.patch.object(registry, "all_features", return_value=[feat]):
            perms = registry.page_perms()
        self.assertEqual(perms["inherit"]["ops"]["view"],
                         frozenset({"store", "platform"}))
        self.assertEqual(perms["inherit"]["data"], "authorized")
        self.assertEqual(set(perms["own"]["ops"]), {"export"})       # 整块覆盖
        self.assertEqual(perms["own"]["data"], "store")

    def test两头都没声明_就是空的(self):
        """默认拒绝的原料：没声明 ops 的页在 `page_perms` 里就是空 ops。"""
        feat = registry.Feature(key="f", label="F",
                                children=[registry.Sub(key="bare", label="空")])
        with mock.patch.object(registry, "all_features", return_value=[feat]):
            perms = registry.page_perms()
        self.assertEqual(perms["bare"]["ops"], {})
        self.assertEqual(perms["bare"]["data"], "store")             # 最窄缺省

    def test合作店三步与失败中止从声明派生(self):
        """⚠ 这两个集合是**行为**的单源，改动要同步改 run_daily 的文案与测试：

        * `partner_ok=False` 的集合被 `run_daily` 的玲珑跳过文案点名（"三步"）；
        * `fatal` 取代了写死的 `_ABORT_AFTER`（抓数失败中止后面）。
        """
        self.assertEqual({s.cmd for s in registry.all_steps() if not s.partner_ok},
                         {"dump", "pos", "pools"})
        self.assertEqual({s.cmd for s in registry.all_steps() if s.fatal},
                         {"dump", "erp-dump"})

    def test_page_perms单源在注册表(self):
        """`web.PERM_RULES` 必须等于注册表派生结果（web 里不许另抄一份）。"""
        self.assertEqual(web.PERM_RULES, registry.page_perms())

    def test所有一级功能都声明入口受众且生活馆边界明确(self):
        perms = registry.page_perms()
        for feature in registry.all_features():
            with self.subTest(feature=feature.key):
                self.assertIsNotNone(feature.audience, "一级功能必须声明可用进入方式")
        self.assertEqual(perms["inventory"]["audience"], frozenset(("erp", "platform")))
        self.assertEqual(perms["distribution"]["audience"], frozenset(("platform",)))
        self.assertIn("lifehall", perms["claim-pending"]["audience"])
        self.assertIn("lifehall", perms["cashier"]["audience"])
        self.assertNotIn("lifehall", perms["badge"]["audience"])

    def test权益领取的数据范围声明与门店区长平台约定一致(self):
        """门店看本店、区长看辖区、平台看全部；生活馆 scope 自身仍只含本店。"""
        perms = registry.page_perms()
        self.assertEqual(perms["claim-pending"]["data"], "authorized")
        self.assertTrue(registry.route_data())
        for route, page_op in registry.route_perms().items():
            if page_op[0] == "claim-pending":
                self.assertEqual(registry.route_data()[route], "authorized", route)

    def test_validate要求一级功能明确声明audience(self):
        feature = registry.Feature(key="missing-audience", label="未声明入口",
                                   children=[registry.Sub(key="missing-audience-sub",
                                                          label="继承")])
        with mock.patch.object(registry, "all_features", return_value=[feature]):
            problems = registry.validate()
        self.assertTrue(any("没有声明 audience" in item for item in problems))

    def test_迁移批次的声明形状(self):
        """2026-10-02 迁移批：attain / monthly / benefit / inventory / distribution
        的声明 = `_can_for` 与路由 `require()` 的唯一判据（收编前后口径必须一致）。"""
        pm = registry.page_perms()
        # attain：改 = 门店；导出 = 区长/平台（C1 + 用户 2026-09-21 口径）
        self.assertEqual(pm["attain"]["ops"]["modify"], frozenset({"store"}))
        self.assertEqual(pm["attain"]["ops"]["export"],
                         frozenset({"manager", "platform"}))
        self.assertEqual(pm["attain"]["ops"]["view"], frozenset(registry.OP_ROLES))
        # 月度计划 / 无忧：导出同达成；无写操作
        for key in ("monthly", "benefit"):
            self.assertEqual(pm[key]["ops"]["export"],
                             frozenset({"manager", "platform"}))
            self.assertNotIn("modify", pm[key]["ops"])
        # 库存盘点：全员可导（push.inventory 原来写死 True）；仓库读按授权门店，
        # 只有用户确认的表外码索引通过 route_data 单独放宽到全仓。
        self.assertEqual(pm["inventory"]["ops"]["export"], frozenset(registry.OP_ROLES))
        self.assertEqual(pm["inventory"]["data"], "authorized")
        self.assertEqual(registry.route_data()[("POST", "/api/inventory/index")], "all")
        self.assertEqual(registry.route_data()[("POST", "/api/inventory/book")], "authorized")
        # 分销：**功能级**声明（整组一个判据），三档全只给平台岗
        self.assertEqual(set(pm["distribution"]["ops"]), {"view", "modify", "export"})
        for op in ("view", "modify", "export"):
            self.assertEqual(pm["distribution"]["ops"][op], frozenset({"platform"}))
        # 子页不写 ops ⇒ 继承功能级（require 打子页 key 也一样拦）
        self.assertEqual(pm["dist-region"]["ops"]["view"], frozenset({"platform"}))

    def test_require带自定义文案(self):
        """`why=` 只改身份不符时的用户文案；未声明的 403 永远说"没声明"（不许混）。"""
        r = require_msg = web.require(_scope("manager"), "attain", "modify",
                                      why="只有门店账号能改目标拆分（区长/平台只读）")
        self.assertIsNotNone(require_msg)
        self.assertIn("只有门店账号能改目标拆分", r["error"])
        r2 = web.require(_scope("platform"), "pos", "modify")
        self.assertIn("没有声明", r2["error"])


class Testrequire默认拒绝(unittest.TestCase):
    """`web.require()` —— 后端的操作闸（未声明 403、身份不符 403、放行 None）。"""

    def test_声明了的身份放行(self):
        for role in ("store", "manager", "platform"):
            self.assertIsNone(web.require(_scope(role), "film", "view"), role)
        for role in ("manager", "platform"):
            self.assertIsNone(web.require(_scope(role), "film", "export"), role)

    def test_门店导出被拒(self):
        r = web.require(_scope("store"), "film", "export")
        self.assertIsNotNone(r)
        self.assertTrue(r["forbidden"])
        self.assertIn("导出", r["error"])
        self.assertEqual(r["what"], "film:export")

    def test_没声明的操作连平台也拒(self):
        """默认拒绝：pos 页没声明 modify —— 平台岗来了照样 403（不是放行）。"""
        r = web.require(_scope("platform"), "pos", "modify")
        self.assertIsNotNone(r)
        self.assertTrue(r["forbidden"])
        self.assertIn("没有声明", r["error"])

    def test_完全没登记的页面也拒(self):
        r = web.require(_scope("platform"), "不存在的页", "view")
        self.assertIsNotNone(r)
        self.assertTrue(r["forbidden"])


class Test数据范围执行(unittest.TestCase):
    """`web.filter_scoped()` —— `data` 声明的执行处（两档各钉一遍）。"""

    ROWS = [{"store": "甲店"}, {"store": "乙店"},
            {"store": "甲店T"}, {"store": ""}, {"store": None}]

    def test_authorized平台岗不过滤(self):
        out = web.filter_scoped(_scope("platform", stores=None),
                                self.ROWS, "authorized")
        self.assertEqual(len(out), 5)

    def test_authorized按范围滤_空名不放行(self):
        sc = _scope("manager", stores={"甲店", "甲店T"})
        out = web.filter_scoped(sc, self.ROWS, "authorized")
        self.assertEqual([r["store"] for r in out], ["甲店", "甲店T"])

    def test_store档只留本店含别名(self):
        out = web.filter_scoped(_scope("platform", stores=None), self.ROWS,
                                "store", own={"甲店", "甲店T"})
        self.assertEqual([r["store"] for r in out], ["甲店", "甲店T"])

    def test_store档必须先有有效scope且own不能扩大授权范围(self):
        rows = [
            {"store": "甲店", "erp_name": "甲店"},
            {"store": "乙店", "erp_name": "乙店"},
            {"store": "甲店", "erp_name": "乙店"},
            {"store": "", "erp_name": "甲店"},
            {"store": "", "erp_name": ""},
        ]
        self.assertEqual(web.filter_scoped({}, rows, "store", own={"甲店"}), [])
        scoped = _scope("manager", stores={"甲店"})
        self.assertEqual(web.filter_scoped(scoped, rows, "store", own={"甲店", "乙店"},
                                           store_field=("store", "erp_name")),
                         [rows[0], rows[3]])

    def test_store档own为空_一行都不给(self):
        """空 own 不等于"全放行"（默认值宁可小 —— 空名不放行同款规矩）。"""
        out = web.filter_scoped(_scope("platform", stores=None),
                                self.ROWS, "store", own=())
        self.assertEqual(out, [])


class Testcan与ops同源(unittest.TestCase):
    """`role.can` 与 `role.ops` 都从声明派生 —— 改声明两头跟着变。"""

    def test_film_export的can从声明来(self):
        self.assertFalse(web._can_for("store")["film.export"])
        self.assertTrue(web._can_for("manager")["film.export"])
        self.assertTrue(web._can_for("platform")["film.export"])

    def test_迁移批的can与声明等价(self):
        """收编前后**逐身份等价**（收编出错的表现就是某身份多给/少给一次写）。"""
        expect = {
            # key:            store  manager platform
            "attain.split.write": (True, False, False),
            "attain.split.send":  (True, False, False),
            "attain.export":      (False, True, True),
            "plan.export":        (False, True, True),
            "benefit.export":     (False, True, True),
            "film.export":        (False, True, True),
            "dist.write":         (False, False, True),
            "dist.export":        (False, False, True),
            "push.inventory":     (True, True, True),
            # 非功能页的两把尺**不走声明**，口径不变
            "staff.write":        (True, False, False),
            "machine.config":     (True, True, True),
        }
        for key, (st, mgr, plat) in expect.items():
            with self.subTest(key=key):
                self.assertEqual(web._can_for("store").get(key), st, key)
                self.assertEqual(web._can_for("manager").get(key), mgr, key)
                self.assertEqual(web._can_for("platform").get(key), plat, key)

    def test_改声明_can跟着变(self):
        """把 export 收成仅平台 —— can 立刻只剩平台（单源的铁证）。"""
        only_plat = dict(web.PERM_RULES)
        only_plat["film"] = {"ops": {"view": frozenset(registry.OP_ROLES),
                                     "export": frozenset({"platform"})},
                             "data": "authorized"}
        with mock.patch.object(web, "PERM_RULES", only_plat):
            self.assertFalse(web._can_for("store")["film.export"])
            self.assertFalse(web._can_for("manager")["film.export"])
            self.assertTrue(web._can_for("platform")["film.export"])

    def test_ops_payload按身份给(self):
        """`/api/overview` 的 `role.ops`：每页列出**这个身份**允许的操作。"""
        with mock.patch.object(web, "pages_for", return_value=[]):
            sc = web._with_pages({"role": "store", "stores": {"甲店"}}, None, [])
            mgr = web._with_pages({"role": "manager", "stores": {"甲店"}}, None, [])
        self.assertEqual(sc["ops"].get("film"), ["view"])
        self.assertEqual(mgr["ops"].get("film"), ["export", "view"])
        # POS 现已显式声明 view；不再依赖隐式页面权限。
        self.assertEqual(sc["ops"].get("pos"), ["view"])
        self.assertEqual(mgr["ops"].get("pos"), ["view"])

    def test_ops声明表空时给空_不是全放行(self):
        """fail-closed：声明表读不到 ⇒ `ops` 是空字典（一个操作都不给），
        而不是"算不出来就全放行"（后者是安全洞，前者只是按钮藏了）。"""
        with mock.patch.object(web, "PERM_RULES", {}):
            with mock.patch.object(web, "pages_for", return_value=["film"]):
                sc = web._with_pages({"role": "platform", "stores": None}, None, [])
        self.assertEqual(sc["ops"], {})
        self.assertEqual(sc["pages"], ["film"])

    def test_系统页操作ops从foot路由声明派生(self):
        """账号页等系统页的前端操作也从其显式路由规则下发。"""
        with mock.patch.object(web, "pages_for", return_value=["account"]):
            scopes = [
                web._with_pages({"role": role, "stores": stores}, None, [])
                for role, stores in (("store", {"甲店"}),
                                     ("manager", {"甲店"}),
                                     ("platform", None))
            ]
        self.assertEqual(scopes[0]["ops"].get("account"), ["modify", "view"])
        self.assertEqual(scopes[1]["ops"].get("account"), ["view"])
        self.assertEqual(scopes[2]["ops"].get("account"), ["view"])


class Test前端声明对照(unittest.TestCase):
    """HTML 上的 `data-op` ↔ 业务注册/foot 路由声明，防"标了个没人声明的操作"。"""

    INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js",
                              "features/sales/attain/page.js", "app.js"))

    def test_每个data_op都有声明(self):
        pairs = re.findall(r'data-op="([a-z]+)"\s+data-op-page="([a-z-]+)"', self.INDEX)
        # 也认属性顺序反过来的写法
        pairs += [(op, page)
                  for page, op in re.findall(
                      r'data-op-page="([a-z-]+)"\s+data-op="([a-z]+)"', self.INDEX)]
        self.assertTrue(pairs, "index.html 里一个 data-op 都没有？")
        for op, page in pairs:
            with self.subTest(page=page, op=op):
                self.assertIn(op, registry.OPS)
                declared = set((web.PERM_RULES.get(page, {}).get("ops") or {}).keys())
                for foot in web.FOOT_ROUTE_RULES.values():
                    if foot.get("page") == page:
                        declared.update((foot.get("ops") or {}).keys())
                self.assertIn(op, declared,
                              "HTML 标了操作，但业务注册/foot 路由都没声明 —— 前端会永远藏它")

    def test_前端按role_ops显隐且fail_closed(self):
        self.assertIn("[data-op]", self.APP_JS)
        self.assertIn("role.ops", self.APP_JS)

    def test_业务导出按钮都由注册操作控制(self):
        for button, page in (("btn-export-attain", "attain"),
                             ("btn-export-plan", "monthly"),
                             ("btn-export-film", "film"),
                             ("btn-export-benefit", "benefit")):
            with self.subTest(button=button):
                match = re.search(r'<button[^>]*id="%s"[^>]*>' % button,
                                  self.INDEX, re.S)
                self.assertIsNotNone(match, button)
                self.assertIn('data-op="export"', match.group(0), button)
                self.assertIn('data-op-page="%s"' % page, match.group(0), button)

    def test_人员编辑按钮由foot路由操作控制(self):
        for button in ("btn-staff-all", "btn-staff-none", "btn-staff-save"):
            with self.subTest(button=button):
                match = re.search(r'<button[^>]*id="%s"[^>]*>' % button,
                                  self.INDEX, re.S)
                self.assertIsNotNone(match, button)
                self.assertIn('data-op="modify"', match.group(0), button)
                self.assertIn('data-op-page="account"', match.group(0), button)

    def test_页面内容键都能对应权限页面(self):
        """subpanel 的 DOM id 可用 data-page-key 显式映射系统页面别名。"""
        for match in re.finditer(
                r'<div\b[^>]*\bid="subpanel-([a-z0-9-]+)"[^>]*>', self.INDEX, re.S):
            dom_key, attrs = match.group(1), match.group(0)
            page_key = re.search(r'data-page-key="([a-z0-9-]+)"', attrs)
            page_key = page_key.group(1) if page_key else dom_key
            with self.subTest(dom_key=dom_key, page_key=page_key):
                self.assertIn(page_key, web.PAGE_RULES)

    def test_动态目标拆分控件标记注册操作(self):
        self.assertIn('data-op="modify" data-op-page="attain"', self.APP_JS)
        self.assertIn('applyOperationVisibility(tr.parentElement)', self.APP_JS)


class Test执行入口(unittest.TestCase):
    """`Step.run` 接进 `run_daily` —— 回落声明、失败码、合作店早退都按声明走。"""

    def _patch_film(self, ok=True, calls=None):
        calls = calls if calls is not None else []

        def fake_run(**kw):
            calls.append(kw)
            return {"ok": ok, "why": "" if ok else "桩：film 没算成"}

        return mock.patch(
            "src.features.valueadd.film.compute.run", side_effect=fake_run)

    def test_点名film走注册表执行入口(self):
        from tests.test_run_daily import run                         # 同目录夹具
        with self._patch_film() as p:
            rc, calls, _ = run(["--steps", "film"])
        self.assertEqual(rc, 0)
        self.assertEqual(p.call_count, 1, "step_run 没调到 compute.run")
        # ctx 由引擎统一构造（root=None = 项目根，跟原 `_step_*` 块一致）
        kw = p.call_args[1]
        self.assertIs(kw.get("root"), None)
        self.assertTrue(callable(kw.get("emit")))

    def test_film失败_后面的步骤照跑(self):
        """`film.fatal=False` ⇒ 失败只记账不中止（中止判据也来自声明）。

        ⚠ 点名 `film,report`：按注册表顺序 film(47) 在 report(90) **前面**，
        这样"film 挂了 report 还跑"才是非中止的真证据。
        """
        from tests.test_run_daily import run
        with self._patch_film(ok=False):
            rc, calls, _ = run(["--steps", "film,report"])
        self.assertEqual(rc, 2, "退出码 = 第一个不成功的（EXIT_FETCH）")
        self.assertIn("report", calls, "film 不是 fatal，失败后 report 要照跑")

    def test_dump失败仍中止(self):
        """`fatal=True` 的行为回归 —— 判据从 `_ABORT_AFTER` 换成了声明，语义不变。"""
        from tests.test_run_daily import run
        rc, calls, _ = run(["--steps", "dump,attain"], dump_rc=99)
        self.assertEqual(rc, 99)
        self.assertEqual(calls, ["dump"], "抓数失败必须中止后面")

    def test_合作店点名film照跑(self):
        """合作店白名单从声明派生：film `partner_ok=True` ⇒ 照跑（不早退）。"""
        from tests.test_run_daily import run
        partner = {"erp_store_name": "合作小店", "store_code": "", "marker": ""}
        with self._patch_film() as p:
            rc, calls, _ = run(["--steps", "film"], config=partner)
        self.assertEqual(rc, 0)
        self.assertEqual(p.call_count, 1, "合作店点名 film 被静默早退了")

    def test_合作店只点玲珑步_仍然早退(self):
        """派生后的早退语义不许变：合作店点名的全是 `partner_ok=False` 的步 ⇒ 空手而归。"""
        from tests.test_run_daily import run
        partner = {"erp_store_name": "合作小店", "store_code": "", "marker": ""}
        rc, calls, _ = run(["--steps", "dump,pos"], config=partner)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], "玲珑三步合作店一步都不该跑")


class Test路由级(unittest.TestCase):
    """真起 HTTP 服务打 `/api/film/*` —— `require()` 接进 Handler 的铁证。

    ⚠ 门禁（`setup_state`）直接放行：这一份测**操作权限**，门禁另有
    `test_setup_gate.py` 盯着（跟 `test_export` 的 `_ServerCase` 同款约定）。
    """

    def setUp(self):
        import json as _json
        import tempfile
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        self._HTTPConnection = HTTPConnection
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛城阳万达店"\nstore_code: "SCN231409"\n',
            encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def request(self, method, path, body=None):
        import json as _json
        c = self._HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = _json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        c.close()
        try:
            return r.status, _json.loads(raw)
        except ValueError:
            return r.status, raw

    def test_门店看film_放行(self):
        with mock.patch.object(web.App, "film",
                               lambda self, day=None: {"ok": True}):
            st, d = self.request("GET", "/api/film")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])

    def test_门店导出film_403_且不写文件(self):
        st, d = self.request("POST", "/api/film/export", {})
        self.assertEqual(st, 403, d)
        self.assertTrue(d["forbidden"])
        self.assertIn("导出", d["error"])
        self.assertEqual(d["what"], "film:export")
        self.assertFalse((Path(self.tmp.name) / "out" / "exports").exists(),
                         "被拦了却还是写了文件")

    def test_区长导出film_放行(self):
        """区长声明里有 export ⇒ 路由放行（桩掉真正落 Excel 的那步）。"""
        (Path(self.tmp.name) / "config" / "managers.yaml").write_text(
            "managers:\n  - name: 杨英梅\n"
            '    accounts: ["SL15763940156"]\n    region: 西北区\n'
            "    stores:\n      - 青岛城阳万达店\n", encoding="utf-8")
        with mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"username": "SL15763940156",
                                                "who": "杨英梅"}):
            def fake_export(app, **_kwargs):
                target = Path(app.root) / "out" / "exports" / "x.xlsx"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"test workbook")
                return {"ok": True, "file": target.name, "path": str(target)}

            with mock.patch.object(web.App, "film_export",
                                   fake_export):
                st, d = self.request("POST", "/api/film/export", {})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])


if __name__ == "__main__":
    unittest.main()
