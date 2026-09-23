"""M17 —— **角色与鉴权**（`role_scope()` + 每个接口的越权拦截）。

用户 2026-09-21：「是不是应该给每个行为和操作加个鉴权，门店账号、区长账号、平台账号
对应着不同的功能」→ 拍板 **B 方案**（真登录 + 按账号判角色），口径在
`.dsh/docs/2026-09-20-角色权限矩阵-设计.md`，计划在 3.0.0 开发目标的「四·八」。

这一份盯三件事：

1. **唯一判据**：`role_scope()` 认身份。⚠ 最有价值的一条是 **区长判定优先于平台** ——
   平台岗的判据是"账号能看到 >1 家店"，而**区长账号恰恰就是这种账号**：
   先判平台的话，区长会看到**全部门店**、菜单全开，**界面上不会有任何提示**。
2. **写操作真的被拦**（403，不是"前端藏起来"）：`PUT /api/attain/split` 以前
   **一个校验都没有** —— 任何登录过的人都能改任意门店的目标拆分。
3. **留痕**：改机器属性（邮件/企微/定时/通用设置）与「导出并推送」都要能查出是谁干的。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import web                                              # noqa: E402


class _Server:
    """真起一个 HTTP 服务 —— 测的是**路由和状态码**，不是 mock 出来的路由。

    ⚠ 登录门禁（`setup_state`）在 `/api/*` 前面：先把这台机器配成"能用的"。
      这里配成**体验店**（有门店名 + 编码 + 串号标识）⇒ 门禁过；玲珑那条
      `needs_linglong=True` 会拦……所以下面每个用例都拿 `_ready()` 直接放行门禁，
      这一份测的是**角色**，不是门禁（门禁另有 `test_setup_gate.py` 盯着）。
    """

    def __init__(self, root: Path, *, cfg_text: str = "", store=("", "")):
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            cfg_text or 'erp_store_name: "青岛城阳万达店"\nstore_code: "SCN231409"\n',
            encoding="utf-8")
        (root / "config" / "managers.yaml").write_text(
            "managers:\n"
            "  - name: 杨英梅\n"
            '    accounts: ["SL15763940156"]\n'
            "    region: 西北区\n"
            '    email: "jiuzhang@example.com"\n'
            "    stores:\n"
            "      - 青岛城阳万象汇店\n"
            "      - 青岛城阳万达店\n",
            encoding="utf-8")
        if store[0]:
            # ⚠ 名单里**要有华为编码** —— 真实名单 30 家全带（页面按它出卡）；
            #   没有编码的店在「每店一张卡」上压根出不来（见 `_stores_cards`）。
            (root / "config" / "stores.yaml").write_text(
                "stores:\n"
                "  - erp_name: %s\n    tdoc_name: %s\n    kind: 体验店\n"
                "    marker: W\n    huawei_code: SCN231409\n"
                % (store[0], store[1] or store[0]), encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def request(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Base(unittest.TestCase):
    #: 这台机器登录的云商账号（区长/门店的身份就靠它）
    ACCOUNT = ""
    CFG = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = self.CFG or 'erp_store_name: "青岛城阳万达店"\nstore_code: "SCN231409"\n'
        self.srv = _Server(self.root, cfg_text=cfg,
                           store=("青岛城阳万达店", "城阳万达"))
        self.addCleanup(self.srv.close)
        # 门禁放行（这一份测角色，不测门禁）
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def scope(self, account="", who="", platform=False, kind="体验店",
              needs_linglong=True):
        """把"这台机器是谁"钉成给定的样子，返回 `role_scope()` 的结果。"""
        creds = {"username": account, "who": who, "has_token": True}
        prof = {"erp_name": "青岛城阳万达店", "type": "experience", "kind": kind,
                "platform": platform, "needs_linglong": needs_linglong,
                "show_all": platform, "who": who, "marker": "W"}
        with mock.patch.object(web, "describe_store_credentials", lambda *a, **k: creds), \
             mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof)):
            return web.role_scope(self.srv.app)

    def client(self):
        """把身份钉住的同时打接口（拦截发生在 Handler 里，必须带着身份跑）。"""
        return self.scope_kwargs

    @property
    def scope_kwargs(self):
        return {}


class Test身份判定(_Base):
    """`role_scope()` —— **唯一的判据**。"""

    def test_区长优先于平台(self):
        """⚠⚠ 这是 M17 最有价值的一条。

        区长账号在云商里**看得到好几家店**（他管的就是好几家），
        而平台岗的旧判据正是"账号能看到 >1 家店" ⇒ 先判平台的话，
        区长会被当成平台岗：**看全部门店、菜单全开，界面毫无提示**。
        """
        sc = self.scope(account="SL15763940156", who="杨英梅", platform=True)
        self.assertEqual(sc["role"], "manager", "区长被当成平台岗了")
        self.assertEqual(sc["label"], "区长 杨英梅（西北区）")
        self.assertIn("青岛城阳万象汇店", sc["stores"])
        self.assertIn("青岛城阳万达店", sc["stores"])
        self.assertFalse(sc["can"]["attain.split.write"], "C1：区长对目标拆分只读")

    def test_账号大小写不敏感(self):
        """⚠ 云商登录名大小写**不统一**（`SL…`/`sl…` 混着）—— 比较必须转大写。"""
        sc = self.scope(account="sl15763940156", who="")
        self.assertEqual(sc["role"], "manager")

    def test_平台岗_范围是全部(self):
        sc = self.scope(account="someone-else", who="", platform=True)
        self.assertEqual(sc["role"], "platform")
        self.assertIsNone(sc["stores"], "平台岗 = 不过滤")
        self.assertFalse(sc["can"]["attain.split.write"])

    def test_认不出来退到门店_范围是本店(self):
        sc = self.scope(account="sl00000000", who="张三")
        self.assertEqual(sc["role"], "store")
        self.assertEqual(sc["stores"], {"青岛城阳万达店", "城阳万达"},
                         "本店 + 它在名单里的别名")
        self.assertTrue(sc["can"]["attain.split.write"], "门店能改本店目标")

    def test_没账号也退到门店_不是平台(self):
        sc = self.scope(account="", who="")
        self.assertEqual(sc["role"], "store", "认不出来一律退到范围最小的那个")


class Test范围判断(unittest.TestCase):
    """`scope_store_ok()` —— 写操作和"按店读"都要先问它。"""

    def test_平台全放(self):
        self.assertTrue(web.scope_store_ok({"stores": None}, "随便哪家店"))

    def test_门店只放本店(self):
        sc = {"stores": {"青岛城阳万达店", "城阳万达"}}
        self.assertTrue(web.scope_store_ok(sc, "青岛城阳万达店"))
        self.assertTrue(web.scope_store_ok(sc, "城阳万达"), "别名也算本店")
        self.assertFalse(web.scope_store_ok(sc, "青岛城阳万象汇店"))

    def test_空名字一律不放(self):
        """⚠「没指定门店」不等于「随便哪家」—— 这种默认值最贵。"""
        self.assertFalse(web.scope_store_ok({"stores": None}, ""))
        self.assertFalse(web.scope_store_ok({"stores": {"甲店"}}, "  "))


class Test写操作真的被拦(_Base):
    """打**真接口** —— 前端藏按钮不算权限（项目自己的规矩）。"""

    def _as(self, **kw):
        """在"这个身份"下打接口（拦截在 Handler 里，必须带着身份跑）。"""
        creds = {"username": kw.get("account", ""), "who": kw.get("who", ""),
                 "has_token": True}
        prof = {"erp_name": "青岛城阳万达店", "type": "experience",
                "kind": "体验店", "platform": kw.get("platform", False),
                "needs_linglong": True, "show_all": kw.get("platform", False),
                "who": kw.get("who", ""), "marker": "W"}
        c1 = mock.patch.object(web, "describe_store_credentials", lambda *a, **k: creds)
        c2 = mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof))
        c1.start()
        c2.start()
        self.addCleanup(c1.stop)
        self.addCleanup(c2.stop)

    def test_门店能改本店目标(self):
        self._as(account="sl00000000", who="张三")
        code, d = self.srv.request("PUT", "/api/attain/split",
                                   {"store": "青岛城阳万达店", "period": "2026-W38",
                                    "targets": {"张三": [1]}})
        self.assertEqual(code, 200, d)

    def test_门店改别店_403(self):
        """⚠ 写操作**永远回落到最小范围**：门店只许动本店。"""
        self._as(account="sl00000000", who="张三")
        code, d = self.srv.request("PUT", "/api/attain/split",
                                   {"store": "青岛城阳万象汇店", "period": "2026-W38",
                                    "targets": {"张三": [1]}})
        self.assertEqual(code, 403, d)
        self.assertTrue(d.get("forbidden"))
        self.assertIn("只能改本店", d.get("error") or "")

    def test_区长改目标_403(self):
        """C1：区长对目标拆分**只读**（改了就没人认账 —— 那是店长分到人头上的活）。"""
        self._as(account="SL15763940156", who="杨英梅", platform=True)
        code, d = self.srv.request("PUT", "/api/attain/split",
                                   {"store": "青岛城阳万象汇店", "period": "2026-W38",
                                    "targets": {"张三": [1]}})
        self.assertEqual(code, 403, d)
        self.assertIn("只有门店账号能改", d.get("error") or "")

    def test_平台改目标_403(self):
        self._as(account="boss", who="老板", platform=True)
        code, d = self.srv.request("PUT", "/api/attain/split",
                                   {"store": "青岛城阳万象汇店", "period": "2026-W38",
                                    "targets": {"张三": [1]}})
        self.assertEqual(code, 403, d)

    def test_区长发送给区长_403(self):
        """区长点「发送给区长」= 把自己那份再发一遍，没意义 ⇒ 也拦。"""
        self._as(account="SL15763940156", who="杨英梅", platform=True)
        code, d = self.srv.request("POST", "/api/attain/split/send",
                                   {"store": "青岛城阳万象汇店", "period": "2026-W38"})
        self.assertEqual(code, 403, d)
        self.assertIn("只有门店账号能发", d.get("error") or "")

    def test_平台发送给区长_403(self):
        """⚠ 用户 2026-09-21 再强调过一次：「……**平台也改不了门店的目标**」。

        「发送」这条也一样：那封邮件的内容是**店长分到人头上的活**，
        区长/平台重发一份只会让收信那边分不清"这到底是谁的口径"。
        """
        self._as(account="boss", who="老板", platform=True)
        code, d = self.srv.request("POST", "/api/attain/split/send",
                                   {"store": "青岛城阳万象汇店", "period": "2026-W38"})
        self.assertEqual(code, 403, d)
        self.assertIn("只有门店账号能发", d.get("error") or "")

    def test_门店读别店的拆分_403(self):
        """⚠ 读也要拦：`all=1` 那条已按角色筛，但**单店那条能点名任意门店**。"""
        self._as(account="sl00000000", who="张三")
        code, d = self.srv.request(
            "GET", "/api/attain/split?store=%E9%9D%92%E5%B2%9B%E5%9F%8E%E9%98%B3"
                   "%E4%B8%87%E8%B1%A1%E6%B1%87%E5%BA%97&period=2026-W38")
        self.assertEqual(code, 403, d)

    def test_越权回复必须带_error(self):
        """⚠ 前端 `api()` 读 `data.error || data.message` —— 只给 `why` 的话
        界面上是干巴巴的「HTTP 403」，用户完全不知道被什么拦了。"""
        self._as(account="boss", who="", platform=True)
        _code, d = self.srv.request("PUT", "/api/attain/split",
                                    {"store": "甲店", "period": "W", "targets": {}})
        self.assertIn("error", d)
        self.assertEqual(d.get("role"), "platform")
        self.assertIn("当前身份", d["error"])


class Test身份下发给前端(_Base):
    """甲方案：`/api/overview` 下发 `role`，前端照着渲染（不在前端再猜一套）。"""

    def test_overview_带_role(self):
        _code, d = self.srv.request("GET", "/api/overview")
        self.assertIn("role", d)
        self.assertIn(d["role"]["role"], ("store", "manager", "platform"))
        self.assertIn("can", d["role"])
        self.assertIn("stores", d["role"])

    def test_overview_带_pages(self):
        """⚠ 前端 `applyProfile` 就是读它渲染的 —— 少了这个键，页面**一页都不藏**。"""
        _code, d = self.srv.request("GET", "/api/overview")
        pages = d["role"].get("pages")
        self.assertIsInstance(pages, list)
        self.assertTrue(pages)
        self.assertIn("sales", pages)


class Test可见性对照(unittest.TestCase):
    """⚠⚠ **双向对照**：HTML 上的 key ↔ 后端 `PAGE_RULES`（2026-09-21 M17 甲方案）。

    可见性表**只有后端那一份**了（`web.PAGE_RULES`），前端拿标签自己的
    `data-tab` / `data-subtab` / `data-foot` 值去 `pages` 里查。所以两个方向都会坏：

    * HTML 上有、表里没有 → 那一项**永远不显示**（`pages` 里没它 ⇒ 前端藏掉），
      而"某一页不见了"是**没人会报的 bug**（用户以为那功能就是没有）；
    * 表里有、HTML 上没有 → 死规则（说明有人删了页面忘了删规则，或者写错了 key）。

    ⚠ 新加页面时**忘了任何一边**，这里当场红 —— 这就是它存在的全部意义。
    """

    @classmethod
    def setUpClass(cls):
        import re
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        # ⚠ **必须先去注释**：这份 HTML 的注释里到处引用 `data-tab="sales"` 这类
        #   例子（讲历史用的），不剥掉的话会测出"一堆不存在的 key"。
        cls.html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        cls.keys = set(re.findall(r'data-(?:tab|subtab|foot)="([a-z-]+)"', cls.html))

    def test_HTML上的key解析出来了(self):
        """先确认正则还管用（HTML 一改写法，下面两条会变成"空对空"的假绿）。"""
        self.assertGreaterEqual(len(self.keys), 10, sorted(self.keys))
        self.assertIn("pos", self.keys)
        self.assertIn("linglong", self.keys)

    def test_每个key都在后端表里(self):
        for k in sorted(self.keys):
            with self.subTest(key=k):
                self.assertIn(k, web.PAGE_RULES,
                              "HTML 上有 key=%s，后端 `PAGE_RULES` 里没有 → "
                              "这一项会**永远不显示**" % k)

    def test_表里每一行HTML上都有(self):
        for k in sorted(web.PAGE_RULES):
            with self.subTest(key=k):
                self.assertIn(k, self.keys, "`PAGE_RULES` 里的死规则：HTML 上没有这个 key")

    def test_前端不再自己写可见性(self):
        """⚠ `data-types` 退休 = HTML 上不该再有它、`applyProfile` 里也不该读它。"""
        self.assertNotIn("data-types=", self.html)

    def test_功能那半是从注册表派生的(self):
        """⚠⚠ **可见性只有一处可改：`Feature.types` / `Sub.types`（注册表）。**

        `PAGE_RULES` 里功能那一半是**算出来的**（`web._build_page_rules`）——
        这条测试把它钉死：谁要是回到 `PAGE_RULES` 里手写一行、而注册表上写的是
        另一套，这里当场红。

        ⚠ 为什么值这一条：**加新页时最自然的动作是改注册表**（功能模块必须在那儿
          登记才跑得起来）。派生断了的话，新页在 `pages` 里压根没有 key ⇒
          **对所有人永远不显示**，而"某一页不见了"是没人会报的 bug。
        """
        from src.features import registry
        keys = set()
        for f in registry.all_features():
            keys.add(f.key)
            with self.subTest(feature=f.key):
                self.assertEqual(web.PAGE_RULES.get(f.key), f.types or "")
            for s in f.children:
                keys.add(s.key)
                with self.subTest(sub=s.key):
                    self.assertEqual(web.PAGE_RULES.get(s.key),
                                     s.types or f.types or "",
                                     "子不写 `types` 就继承父的（注册表 `menus()` 同一套）")
        self.assertEqual(web.PAGE_RULES, web._build_page_rules(),
                         "`PAGE_RULES` 跟派生结果不一致（有人手写了？）")
        # 左下角那几项**不在注册表里**（不是功能模块），是 `FOOT_PAGES` 手写的
        for k in ("account", "general", "update", "scheduler", "linglong", "theme"):
            self.assertIn(k, web.PAGE_RULES)
            self.assertNotIn(k, keys)


class Test谁能看见哪些页(unittest.TestCase):
    """`pages_for()` 的三个档 —— ⚠ 这一层**只是菜单可见性**，不是安全边界
    （闸门在每个 `/api/*` 的 `role_scope` + `forbid`，见 `Test写操作真的被拦`）。"""

    LING = ("compliance", "pos", "pools", "compliance-settings", "linglong")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _stores(self, *rows):
        """写一份门店名单（`marker` 非空 = 这家店要走玲珑）。"""
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        txt = "stores:\n"
        for name, kind, marker in rows:
            txt += ("  - erp_name: %s\n    tdoc_name: %s\n    kind: %s\n"
                    "    marker: %s\n" % (name, name, kind, marker))
        (self.root / "config" / "stores.yaml").write_text(txt, encoding="utf-8")

    def test_合作店看不到玲珑那几页(self):
        pages = web.pages_for({"role": web.ROLE_STORE, "needs_linglong": False,
                               "stores": {"青岛麦凯乐店"}}, self.root)
        for k in self.LING:
            with self.subTest(key=k):
                self.assertNotIn(k, pages)
        for k in ("sales", "attain", "inventory", "account"):
            with self.subTest(key=k):
                self.assertIn(k, pages)

    def test_走玲珑的店看得见(self):
        pages = web.pages_for({"role": web.ROLE_STORE, "needs_linglong": True,
                               "stores": {"青岛城阳万达店"}}, self.root)
        self.assertIn("pools", pages)
        self.assertIn("linglong", pages)

    def test_体验店没标识也看不到(self):
        """⚠ 判据是**有没有串号标识**，不是 `kind` ——
        青岛海信广场店就是"体验店但没标识"那一家（用户说的"那十四家"）。"""
        self._stores(("青岛海信广场店", "体验店", ""))
        pages = web.pages_for({"role": web.ROLE_STORE, "needs_linglong": False,
                               "stores": {"青岛海信广场店"}}, self.root)
        self.assertNotIn("pos", pages)

    def test_区长辖区里有玲珑店就看得见(self):
        """⚠ 混合辖区按"**有**"算：整页藏掉会让区长以为那几家店没数据。"""
        self._stores(("青岛麦凯乐店", "合作店", ""),
                     ("青岛城阳万达店", "体验店", "W"))
        pages = web.pages_for({"role": web.ROLE_MANAGER,
                               "stores": {"青岛麦凯乐店", "青岛城阳万达店"}}, self.root)
        self.assertIn("pos", pages, "辖区里有要走玲珑的店 ⇒ 得看得见")

    def test_区长辖区里一家玲珑店都没有就看不见(self):
        self._stores(("青岛麦凯乐店", "合作店", ""), ("青岛李沧店", "合作店", ""))
        pages = web.pages_for({"role": web.ROLE_MANAGER,
                               "stores": {"青岛麦凯乐店", "青岛李沧店"}}, self.root)
        self.assertNotIn("pos", pages)

    def test_名单读不到时区长也别少看(self):
        """⚠ 读不到名单（还没生成 / 坏了）时**给全**：少给的表现是"区长看不到
        辖区里某家店的合规"，而他**没有任何提示**；多给只是菜单多两项。"""
        pages = web.pages_for({"role": web.ROLE_MANAGER, "stores": {"青岛城阳万达店"}},
                              self.root)
        self.assertIn("pos", pages)

    def test_区长名单对不上时也给全(self):
        """⚠ 同上那条的另一种成因：名单在、但**一家都对不上**（新店 / 写法不同）。
        认不出来 ⇒ 宁多勿少（藏掉才是真事故）。"""
        self._stores(("青岛麦凯乐店", "合作店", ""))
        pages = web.pages_for({"role": web.ROLE_MANAGER, "stores": {"青岛新开的店"}},
                              self.root)
        self.assertIn("pos", pages)

    def test_多店那一档只有区长和平台看得见(self):
        """⚠ M20 新加的词：`multi` = 管多店的身份（区长 / 平台）。

        ⚠ 关键是"**同时**属于几档"：区长既要看见「每店一张卡」，
          也不能丢掉「五项合规」（那几页按 `experience`/`partner` 判）。
        """
        mgr = web.pages_for({"role": web.ROLE_MANAGER, "stores": {"青岛城阳万达店"}},
                            self.root)
        self.assertIn("stores", mgr)
        self.assertIn("pos", mgr, "区长把玲珑那几页丢了（multi 不该顶掉 experience）")
        plat = web.pages_for({"role": web.ROLE_PLATFORM, "stores": None}, self.root)
        self.assertIn("stores", plat)
        store = web.pages_for({"role": web.ROLE_STORE, "needs_linglong": True,
                               "stores": {"青岛城阳万达店"}}, self.root)
        self.assertNotIn("stores", store, "门店不该看见「数据交换」那一行")

    def test_平台岗全开(self):
        pages = web.pages_for({"role": web.ROLE_PLATFORM, "stores": None}, self.root)
        self.assertEqual(pages, sorted(web.PAGE_RULES))

    def test_认不出来的身份退到最窄(self):
        """⚠ 空 scope（画像还没读出来）**不能**给全 —— 那是"谁都看不见"。
        `pages_for` 只在 `role` 认不出来时按"没有玲珑"处理。"""
        pages = web.pages_for({}, self.root)
        self.assertNotIn("pos", pages)
        self.assertIn("sales", pages)

    def test_名字两种写法都算(self):
        """区长名单里写的是腾讯文档那种叫法（「城阳万达」），名单里是「青岛城阳万达店」。

        ⚠ `pages_for` 拿到的 `stores` 是 `role_scope()` **已经展开过别名**的那一份
        （`_store_aliases`），所以这两种写法在真实路径上都对得上；
        这里钉的是那个前提 —— 直接塞个没展开的短名字进去是**对不上**的。
        """
        self._stores(("青岛城阳万达店", "体验店", "W"))
        pages = web.pages_for({"role": web.ROLE_MANAGER, "stores": {"城阳万达"}},
                              self.root)
        self.assertIn("pos", pages, "对不上 ⇒ 认不出来 ⇒ 给全（宁多勿少）")
        pages = web.pages_for({"role": web.ROLE_MANAGER,
                               "stores": {"青岛城阳万达店"}}, self.root)
        self.assertIn("pos", pages, "全名对得上 ⇒ 看得见")


class Test多店视图的范围(_Base):
    """**每店一张卡**（M20）—— C3「区长看多店」＋ C4「门店不能看全区」＋
    四·八验收 6「页面标清数据来源与时间，收信失败时保持原样」。

    ⚠ 这一页是**唯一跨店**的页面：它读的是收信库（各店发来的邮件），
      而收信库里**所有发过信的店都有** ⇒ 不筛就是"区长看到别区"。
    """

    def _as(self, **kw):
        creds = {"username": kw.get("account", ""), "who": kw.get("who", ""),
                 "has_token": True}
        prof = {"erp_name": "青岛城阳万达店", "type": "experience",
                "kind": "体验店", "platform": kw.get("platform", False),
                "needs_linglong": True, "show_all": kw.get("platform", False),
                "who": kw.get("who", ""), "marker": "W"}
        for c in (mock.patch.object(web, "describe_store_credentials", lambda *a, **k: creds),
                  mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof))):
            c.start()
            self.addCleanup(c.stop)

    def test_门店看多店视图是403(self):
        """⚠ C4 明说了门店不能看全区排名 —— 藏菜单从来不算权限。"""
        self._as(account="sl00000000", who="张三")
        code, d = self.srv.request("GET", "/api/report/stores")
        self.assertEqual(code, 403)
        self.assertIn("error", d)
        code, d = self.srv.request("GET", "/api/report/store?code=SCN231409")
        self.assertEqual(code, 403)

    def test_区长只出所辖那几家(self):
        """⚠ 收信库里可能有**全公司**的店（别的区长辖区的）—— 一个都不许露。"""
        self._as(account="SL15763940156", who="杨英梅")     # 名单里的区长账号
        code, d = self.srv.request("GET", "/api/report/stores")
        self.assertEqual(code, 200, d)
        names = [x["store_name"] for x in d["stores"]]
        self.assertTrue(names)
        for n in names:
            with self.subTest(store=n):
                self.assertIn(n, ("青岛城阳万达店", "青岛城阳万象汇店"),
                              "区长的卡片里出现了别的店")

    def test_区长点别家店也是403(self):
        self._as(account="SL15763940156", who="杨英梅")
        code, d = self.srv.request("GET", "/api/report/store?code=SCN999999")
        self.assertEqual(code, 403)
        self.assertIn("error", d)

    def test_平台看全部(self):
        self._as(account="PLATFORM", who="平台", platform=True)
        code, d = self.srv.request("GET", "/api/report/stores")
        self.assertEqual(code, 200, d)
        self.assertTrue(d["stores"], "平台岗该看到门店（夹具名单里有一家）")

    def test_没收到的店也有卡(self):
        """⚠ 这是这个页面最该回答的问题（"哪几家还没报"）——
        没上报的店不列出来的话，它等于不存在。"""
        self._as(account="SL15763940156", who="杨英梅")
        _code, d = self.srv.request("GET", "/api/report/stores")
        self.assertTrue(d["stores"])
        for x in d["stores"]:
            with self.subTest(store=x["store_code"]):
                self.assertFalse(x["known"])
                self.assertIn("从来没收到过", x["why"])

    def test_页面标清来源和时间(self):
        """四·八验收 6：卡片上要能看出"这份来自哪家店哪天的邮件"。"""
        from src.app import report_inbox as inbox_mod
        pkg = _make_pkg(self.root, code="SCN231409")
        inbox_mod.import_package(self.root, pkg, subject="[CBG上报] SCN231409 2026-09-21")
        self._as(account="SL15763940156", who="杨英梅")
        _code, d = self.srv.request("GET", "/api/report/stores")
        card = [x for x in d["stores"] if x["known"]][0]
        self.assertEqual(card["store_code"], "SCN231409")
        self.assertTrue(card["report_date"])
        self.assertTrue(card["imported_at"])
        self.assertGreater(card["rows_total"], 0)
        self.assertIn("orders", card["tables"])

    def test_收信失败时保持原样(self):
        """⚠ 不许"拉不到就清空" —— 那会让人以为数据丢了。"""
        from src.app import report_inbox as inbox_mod
        inbox_mod.import_package(self.root, _make_pkg(self.root, code="SCN231409"))
        self._as(account="SL15763940156", who="杨英梅")
        with mock.patch.object(inbox_mod, "configured",
                               lambda cfg=None, root=None: (False, "这台机器没配收信")):
            _code, d = self.srv.request("GET", "/api/report/stores")
        card = [x for x in d["stores"] if x["known"]]
        self.assertTrue(card, "收信失败把卡片清空了")
        self.assertFalse(d["inbox"]["configured"])
        self.assertIn("没配收信", d["inbox"]["why"])


class Test四池明细按范围筛(_Base):
    """M20 的另一半（M17 遗留③）：**读接口的数据范围**。

    ⚠ 区长机器上抓的是**全公司**云商数据（账号就是系统那个）⇒
      `out/pools-<年>.json` 里别家店的单子也在，不筛就是"看到别区的差异清单"。
    """

    ROWS = {"counts": {"AD": 2, "BC": 1},
            "AD": [{"sn": "A1", "云商门店": "青岛城阳万达店"},
                   {"sn": "A2", "云商门店": "青岛麦凯乐店"}],
            "BC": [{"sn": "B1", "云商门店": "青岛麦凯乐店"}]}

    def test_区长只留所辖(self):
        scope = {"role": "manager", "stores": {"青岛城阳万达店"}}
        got = web._scope_pools(scope, self.ROWS)
        self.assertEqual([r["sn"] for r in got["AD"]], ["A1"])
        self.assertEqual(got["BC"], [])
        self.assertEqual(got["counts"]["AD"], 1)

    def test_平台不过滤(self):
        got = web._scope_pools({"role": "platform", "stores": None}, self.ROWS)
        self.assertEqual(len(got["AD"]), 2)
        self.assertEqual(len(got["BC"]), 1)
        self.assertNotIn("scoped", got)

    def test_两种店名写法都认(self):
        """⚠ 玲珑侧写的是腾讯文档那种短名（「城阳万达」），云商侧是全名。

        ⚠ 这里**用 `role_scope` 那条路展开出来的范围**（`_store_aliases`）——
          真实的 scope 里两种写法都有；`_scope_pools` 的前提就是这个
          （它自己不读名单，免得每个请求多读一遍文件，见 `_store_aliases` 那段）。
        """
        stores = web._store_aliases(["青岛城阳万达店"], self.root)
        self.assertIn("城阳万达", stores, "别名没展开？")
        rows = {"AD": [{"sn": "A1", "玲珑门店": "城阳万达"}], "counts": {"AD": 1}}
        got = web._scope_pools({"role": "manager", "stores": stores}, rows)
        self.assertEqual(len(got["AD"]), 1, "短名对不上 ⇒ 区长看不到自己店的差异")

    def test_两边都没写店名的行留着(self):
        """宁多勿少：看不出来源的行删掉，用户会以为数据丢了。"""
        rows = {"AD": [{"sn": "A1"}, {"sn": "A2", "云商门店": "别家店"}],
                "counts": {"AD": 2}}
        got = web._scope_pools({"role": "manager", "stores": {"青岛城阳万达店"}}, rows)
        self.assertEqual([r["sn"] for r in got["AD"]], ["A1"])


def _make_pkg(root, *, code="SCN231409", date="2026-09-21"):
    """造一个最小上报包（只给卡片那几条用）。"""
    import json as _json
    import sqlite3
    p = Path(root) / ("cbg-%s-%s.db" % (code, date))
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE orders (document_no TEXT PRIMARY KEY, remark TEXT)")
    conn.execute("INSERT INTO orders VALUES ('D1','好')")
    tables = {"orders": {"rows": 1, "mode": "incremental", "keys": ["document_no"],
                         "cols": ["document_no", "remark"], "hash": "h"}}
    conn.execute("""CREATE TABLE _manifest (protocol INTEGER, store_code TEXT,
        store_name TEXT, erp_name TEXT, tdoc_name TEXT, date TEXT, generated_at TEXT,
        version TEXT, reason TEXT, rows_total INTEGER, tables_json TEXT, cols_hash TEXT,
        prev_cols_hash TEXT, cols_changed_json TEXT, PRIMARY KEY (store_code, date))""")
    conn.execute("INSERT INTO _manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (1, code, "青岛城阳万达店", "青岛城阳万达店", "城阳万达", date,
                  date + " 21:03:00", "2.1.1", "daily", 1,
                  _json.dumps(tables, ensure_ascii=False), "ch", "", "[]"))
    conn.commit()
    conn.close()
    return str(p)


class Test达成数据按身份过滤(_Base):
    """`filter_attain_rows()` —— 读的时候**必须再滤一遍**。

    ⚠ 落盘那份 `out/attain-<年>.json` 是"算的时候"按当时的门店滤的，
      而页面读的可能是**别人（或上一次）**算好的那一份。
    """

    def _rows(self):
        return {"exists": True, "rows": [
            {"store": "青岛城阳万达店", "erp_name": "青岛城阳万达店"},
            {"store": "青岛城阳万象汇店", "erp_name": "青岛城阳万象汇店"}]}

    def test_门店只留本店那行(self):
        with mock.patch.object(web, "role_scope",
                               return_value={"role": "store", "label": "门店 · 万达",
                                             "stores": {"青岛城阳万达店"}}):
            d = self.srv.app.filter_attain_rows(self._rows())
        self.assertEqual([r["store"] for r in d["rows"]], ["青岛城阳万达店"])

    def test_区长留所辖那几行(self):
        """⚠ 这一档以前**根本不存在**（老逻辑只认"本店 / 全部"两种）。"""
        with mock.patch.object(web, "role_scope",
                               return_value={"role": "manager", "label": "区长 杨英梅",
                                             "stores": {"青岛城阳万达店", "青岛城阳万象汇店"}}):
            d = self.srv.app.filter_attain_rows(self._rows())
        self.assertEqual(len(d["rows"]), 2)

    def test_平台不过滤(self):
        with mock.patch.object(web, "role_scope",
                               return_value={"role": "platform", "label": "平台岗",
                                             "stores": None}):
            d = self.srv.app.filter_attain_rows(self._rows())
        self.assertEqual(len(d["rows"]), 2)
        self.assertEqual(d["store_filter"], "")

    def test_滤空要说清而不是给空白(self):
        with mock.patch.object(web, "role_scope",
                               return_value={"role": "store", "label": "门店",
                                             "stores": {"别家店"}}):
            d = self.srv.app.filter_attain_rows(self._rows())
        self.assertEqual(d["rows"], [])
        self.assertIn("没有", d.get("error") or "", "滤空了要说清，别给一片空白")


class Test人员设置只有门店能改(_Base):
    """C2 **当天改过**（用户 2026-09-21）：「区长/平台**不能改**别家店的这份名单，
    **读取门店发送的状态表**吧」。

    ⚠ 原来 `_can_for()` 里 `staff.write = True`（三种角色都"能写"）——
    那是按旧的 C2（区长改所辖、平台改全部）声明的，现在收成"只有门店"。
    """

    def _as(self, **kw):
        creds = {"username": kw.get("account", ""), "who": kw.get("who", ""),
                 "has_token": True}
        prof = {"erp_name": "青岛城阳万达店", "type": "experience",
                "kind": "体验店", "platform": kw.get("platform", False),
                "needs_linglong": True, "show_all": kw.get("platform", False),
                "who": kw.get("who", ""), "marker": "W"}
        for c in (mock.patch.object(web, "describe_store_credentials", lambda *a, **k: creds),
                  mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof))):
            c.start()
            self.addCleanup(c.stop)

    def test_区长改人员名单_403(self):
        self._as(account="SL15763940156", who="杨英梅")
        code, d = self.srv.request("PUT", "/api/staff", {"excluded": ["sl001"]})
        self.assertEqual(code, 403, d)
        self.assertIn("error", d)

    def test_平台改人员名单_403(self):
        self._as(account="boss", who="老板", platform=True)
        code, d = self.srv.request("PUT", "/api/staff", {"excluded": ["sl001"]})
        self.assertEqual(code, 403, d)

    def test_区长读的是门店发来的状态表(self):
        """⚠ 不是现场查云商（那只能查到本机那家店），而是收信库里那份。"""
        from src.app import report_inbox as inbox_mod
        conn = inbox_mod.open_db(self.root)
        conn.execute("INSERT INTO inbox (store_code, report_date, store_name, imported_at, "
                     "subject) VALUES ('SCN231409','2026-09-21','青岛城阳万达店',"
                     "'2026-09-21 21:30:00','[CBG上报] SCN231409 2026-09-21')")
        conn.execute("INSERT INTO rows_ (store_code, table_name, row_key, report_date, "
                     "row_json) VALUES ('SCN231409','staff','sl001','2026-09-21',?)",
                     (json.dumps({"account": "sl001", "real": "张三", "active": 1},
                                 ensure_ascii=False),))
        conn.commit()
        conn.close()
        self._as(account="SL15763940156", who="杨英梅")
        code, d = self.srv.request("GET", "/api/staff")
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("from_mail"), "区长读的不是'门店发来的状态表'")
        self.assertTrue(d.get("readonly"))
        self.assertEqual(len(d["stores"]), 1)
        self.assertEqual(d["stores"][0]["store_name"], "青岛城阳万达店")
        self.assertEqual([p["real"] for p in d["stores"][0]["people"]], ["张三"])
        self.assertTrue(d["stores"][0]["report_date"], "要标清这份是哪天的")


class Test改配置要留痕(_Base):
    """矩阵对"机器属性"那一档的要求：**改可以，但要记一条日志：谁改的**。

    ⚠ 临时 root 里**得先有个库** —— `runlog.record` 的既定行为是
      "没有库就安静跳过"（刚装完还没抓过数的机器不该为了记一行去建库）。
      不建库的话这条测试会绿得莫名其妙？不会：它会**红**（连"留痕"都没有），
      所以这里显式建一个空库，测的才是"记没记那一笔"。
    """

    def setUp(self):
        super().setUp()
        import sqlite3
        from src.storage import runlog
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.root / "out" / "cbg-2026.db"))
        runlog.ensure(conn)
        conn.commit()
        conn.close()

    def test_改通用设置记一笔(self):
        from src.storage import runlog
        _code, d = self.srv.request("PUT", "/api/config",
                                    {"values": {"timezone": "Asia/Shanghai"}})
        self.assertTrue(d.get("ok"), d)
        rows = runlog.recent(self.root, kind="config", limit=5)
        self.assertTrue(rows, "改配置没有留痕")
        self.assertIn("通用设置", rows[0].get("note") or "")
        detail = rows[0].get("detail") or {}
        self.assertIn("who", detail, "要能查出是谁改的")


# ---------------------------------------------------------------- 区域圈店（2026-09-21 B 方案）
#
# 用户原话：「平台找的是全部 28 家店，然后区长是分开的呀，你说的应该不是店是区域吧」
#   → 他选了 B：**区域是门店自己的属性**（`stores.yaml` 的 `region` 列），
#     区长只写区名，程序按区域自动圈店。
#
# ⚠ 为什么非要改：老写法是区长手里**手抄一份门店清单**。办公室在名单里加了新店、
#   忘了加进某位区长的清单 ⇒ 那家店**对所有区长都不可见**（平台还看得见），
#   而界面上不会报任何错。这正是这个项目最怕的"看着很合理的空"。

#: 三家店 + 一个虚拟平台岗 + 一个**没标区域**的合作店（体检要点它的名）
_STORES_YAML = """\
stores:
  - marker: W
    erp_name: "青岛城阳万象汇店"
    region: 西北区
    huawei_code: "SCN231409"
  - marker: D
    erp_name: "青岛城阳万达店"
    tdoc_name: "城阳万达"
    region: 西北区
    huawei_code: "SCN075029"
  - marker: ""
    erp_name: "胶南合美MALL店"
    region: 南区
    huawei_code: "SCN000001"
  - marker: ""
    erp_name: "颐高"
    huawei_code: "RA05320282"
  - marker: ""
    erp_name: "平台岗"
    kind: 平台岗
"""

_MANAGERS_YAML = """\
managers:
  - name: 杨英梅
    accounts: ["SL15763940156"]
    region: 西北区
    email: "a@example.com"
    regions: [西北区]
  - name: 徐崇龙
    accounts: ["SL15865560659"]
    region: 南区
    email: "b@example.com"
    regions: [南区]
"""

#: 老写法（手抄清单）—— **必须继续能用**，否则区长机器上那份老文件一升级就瞎了
_MANAGERS_OLD = """\
managers:
  - name: 杨英梅
    accounts: ["SL15763940156"]
    region: 西北区
    email: "a@example.com"
    stores:
      - 青岛城阳万象汇店
      - 城阳万达
"""


class Test区域圈店(unittest.TestCase):
    """`config_io.stores_of_manager()` —— **区长管哪些店的唯一口径**。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self._write(_STORES_YAML, _MANAGERS_YAML)

    def _write(self, stores, managers):
        (self.root / "config" / "stores.yaml").write_text(stores, encoding="utf-8")
        (self.root / "config" / "managers.yaml").write_text(managers, encoding="utf-8")

    def _scopes(self):
        from src import config_io
        rows = config_io.stores_table(self.root)
        return {m["name"]: config_io.stores_of_manager(m, rows)
                for m in config_io.managers_table(self.root)}

    def test_按区域圈到那几家(self):
        got = self._scopes()
        self.assertEqual(sorted(got["杨英梅"]), ["城阳万达", "青岛城阳万象汇店", "青岛城阳万达店"])
        self.assertEqual(sorted(got["徐崇龙"]), ["胶南合美MALL店"])
        # ⚠ 别名（`tdoc_name`）也要在里面 —— 达成表里写的是它
        self.assertIn("城阳万达", got["杨英梅"])

    def test_新店标了区域就自动进(self):
        """⭐ 这条就是 B 方案的**全部意义** —— 以后不用再去改区长的清单。"""
        self._write(_STORES_YAML.replace(
            '  - marker: ""\n    erp_name: "胶南合美MALL店"\n    region: 南区',
            '  - marker: ""\n    erp_name: "胶南合美MALL店"\n    region: 南区\n'
            '  - marker: ""\n    erp_name: "新开的店"\n    region: 西北区'), _MANAGERS_YAML)
        self.assertIn("新开的店", self._scopes()["杨英梅"])

    def test_没标区域的店谁都不给(self):
        got = self._scopes()
        for names in got.values():
            self.assertNotIn("颐高", names)

    def test_老写法照旧能用(self):
        """⚠ 手抄清单**原样返回**（别名由调用方补：`web._store_aliases` /
        `split.managers_of` 各自都会补）—— 这里只钉"老文件照旧圈得到那两家"。"""
        self._write(_STORES_YAML, _MANAGERS_OLD)
        self.assertEqual(sorted(self._scopes()["杨英梅"]),
                         ["城阳万达", "青岛城阳万象汇店"])

    def test_两种写法取并集(self):
        """手抄的那几家 + 区域圈出来的，**都算**（升级途中两种写法会并存）。"""
        self._write(_STORES_YAML, _MANAGERS_YAML.replace(
            "    regions: [西北区]", "    regions: [西北区]\n    stores:\n      - 胶南合美MALL店"))
        self.assertIn("胶南合美MALL店", self._scopes()["杨英梅"])

    def test_只写了单数_region_也当选择器(self):
        """`region:` 平时是**标签**，但一个人只写了它（没 stores / regions）时按它圈 ——
        不然那份配置会"圈到 0 家店"，而他什么都看不到。"""
        self._write(_STORES_YAML, _MANAGERS_YAML.replace("    regions: [西北区]\n", ""))
        self.assertEqual(sorted(self._scopes()["杨英梅"]),
                         ["城阳万达", "青岛城阳万象汇店", "青岛城阳万达店"])

    def test_标签不会偷偷改范围(self):
        """⚠ 老文件里 `region:` 和 `stores:` 并存 —— 那时 `region:` **只是标签**，
        不能拿去圈店（否则范围会悄悄变）。"""
        self._write(_STORES_YAML, _MANAGERS_OLD.replace("region: 西北区", "region: 南区"))
        # 写了 `stores:` ⇒ `region:` 只是标签：范围**还是那两家**，没被南区圈走
        self.assertEqual(sorted(self._scopes()["杨英梅"]),
                         ["城阳万达", "青岛城阳万象汇店"])


class Test分区体检(unittest.TestCase):
    """`region_audit()` —— 三件会让数据**静默看不见**的事。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)

    def _write(self, stores, managers):
        (self.root / "config" / "stores.yaml").write_text(stores, encoding="utf-8")
        (self.root / "config" / "managers.yaml").write_text(managers, encoding="utf-8")

    def _audit(self):
        from src import config_io
        return config_io.region_audit(self.root)

    def test_配得对就是干净的(self):
        self._write(_STORES_YAML.replace('  - marker: ""\n    erp_name: "颐高"\n'
                                         '    huawei_code: "RA05320282"\n', "")
                    .replace('  - marker: ""\n    erp_name: "平台岗"\n    kind: 平台岗\n', ""),
                    _MANAGERS_YAML)
        self.assertEqual(self._audit(), [])

    def test_区域名打错要说出来(self):
        self._write(_STORES_YAML, _MANAGERS_YAML.replace("regions: [南区]", "regions: [南]"))
        probs = self._audit()
        self.assertTrue(probs)
        self.assertIn("一家店都看不到", probs[0])
        self.assertIn("徐崇龙", probs[0])

    def test_没标区域的店要点名(self):
        probs = self._audit() if self._write(_STORES_YAML, _MANAGERS_YAML) is None else []
        self.assertTrue(any("没标区域" in p and "颐高" in p for p in probs), probs)
        # ⚠ 平台岗那种**虚拟店**不算"没标区域"（它本来就不属于任何区）
        self.assertFalse(any("平台岗" in p for p in probs), probs)

    def test_一家店被两个区长圈到要说出来(self):
        self._write(_STORES_YAML, _MANAGERS_YAML.replace("regions: [南区]", "regions: [西北区]"))
        self.assertTrue(any("同时圈到" in p for p in self._audit()))

    def test_没配区长名单就不体检(self):
        """门店机器上不该冒出一堆"没标区域"的警告。"""
        self._write(_STORES_YAML, "managers: []\n")
        self.assertEqual(self._audit(), [])


class Test区长范围走区域(_Base):
    """`role_scope()` 里那一份 —— **接口判据**用的就是它。"""

    def _write(self, stores, managers):
        (self.root / "config" / "stores.yaml").write_text(stores, encoding="utf-8")
        (self.root / "config" / "managers.yaml").write_text(managers, encoding="utf-8")

    def test_区长按区域圈店(self):
        self._write(_STORES_YAML, _MANAGERS_YAML)
        sc = self.scope(account="SL15763940156")
        self.assertEqual(sc["role"], "manager")
        self.assertIn("青岛城阳万象汇店", sc["stores"])
        self.assertIn("城阳万达", sc["stores"])                # 别名
        self.assertNotIn("胶南合美MALL店", sc["stores"])       # 别区的，一个都不许露

    def test_另一位区长只看到自己那个区(self):
        self._write(_STORES_YAML, _MANAGERS_YAML)
        sc = self.scope(account="SL15865560659")
        self.assertEqual(sorted(sc["stores"]), ["胶南合美MALL店"])
        self.assertIn("南区", sc["label"])          # 界面上那句标签也跟得上
        self.assertIn("徐崇龙", sc["label"])

    def test_只按区域配的区长_不会掉成门店(self):
        """⚠ 老代码是 `if not m.get("stores"): continue` —— 那种写法下，
        按区域配的区长会被**整条跳过**，他掉成门店身份、只看得到一家店，
        而界面上没有任何提示。"""
        self._write(_STORES_YAML, _MANAGERS_YAML)
        self.assertEqual(self.scope(account="SL15763940156")["role"], "manager")

    def test_给区长的目标拆分也找得到区长(self):
        """`attain.split.managers_of()` 原来只读 `m["stores"]` —— 按区域配的区长
        会**静默匹配不上**（门店点发送 ⇒ "没有配区长"，而配置里明明写着）。"""
        from src.features.sales.attain import split
        self._write(_STORES_YAML, _MANAGERS_YAML)
        got = split.managers_of("青岛城阳万象汇店", self.root)
        self.assertEqual([x["name"] for x in got], ["杨英梅"])
        self.assertEqual(got[0]["email"], "a@example.com")
        # 别名（腾讯文档那种写法）也得认
        self.assertEqual([x["name"] for x in split.managers_of("城阳万达", self.root)], ["杨英梅"])
        self.assertEqual(split.managers_of("胶南合美MALL店", self.root)[0]["name"], "徐崇龙")
