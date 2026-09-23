# -*- coding: utf-8 -*-
"""月度生意计划的**接口层**测试 —— `/api/plan` / `/api/plan/export` 的鉴权与范围。

⚠ 本文件里最要紧的一条是 `Test合计跟着过滤走`：落盘那份 JSON 里是**名单内全部 28 家**，
  而接口层会按身份把 `rows` 滤掉几家。**滤完 `summary` 必须重算** ——
  不重算的话门店看到的合计比自己的明细大十几倍，而界面上完全正常
  （比"多给一行"严重得多，属于最难解释的一类错）。
"""

import datetime
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from src import web
from src.features.plan.monthly import metric as M
from src.features.plan.monthly import plan as P


STORES = ["青岛城阳万达店", "青岛城阳万象汇店", "青岛悦荟店"]


def _sale(store, cat2="Mate80系列", cat3="Mate80", qty=1, amount=6000.0, profit=600.0):
    return M.Sale(store, "手机", cat2, cat3, qty, amount, profit)


def _payload(stores=None, unknown=None):
    """真造一份落盘 JSON（形状跟 `plan.compute` 的一致）—— 手写字典容易跟实现漂移。"""
    stores = stores or STORES
    rows_in = [
        _sale("青岛城阳万达店", qty=10, amount=60000, profit=6000),
        _sale("青岛城阳万象汇店", qty=5, amount=30000, profit=3000),
        _sale("青岛悦荟店", qty=1, amount=6000, profit=600),
    ]
    tree = M.tree(*M.collect(rows_in)[:2], stores)
    return {
        "exists": True,
        "period": {"cur": {"start": "2026-09-01", "end": "2026-09-21", "days": 21},
                   "prev": {"start": "2026-08-01", "end": "2026-08-21", "days": 21},
                   "today": "2026-09-21"},
        "data_as_of": "2026-09-20",
        "computed_at": "2026-09-21 17:00:00",
        "blocks": list(M.BLOCKS),
        "stores": list(stores),
        "regions": {s: "西北区" for s in stores},
        "rows": tree,
        "summary": M.summarize(tree),
        "dropped": {"名单外的店": 5744},
        "unknown": unknown or {},
        "skipped": {},
        "warnings": [],
        "counts": {"cur_rows": 3, "prev_rows": 0},
    }


def _root(tmp):
    root = Path(tmp)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "store-X.yaml").write_text(
        'erp_store_name: "青岛城阳万达店"\nstore_code: "CNSCN162188"\n', encoding="utf-8")
    return root


class _Server:
    """真起一个 HTTP 服务（照 `tests/test_web.py` 的 `_Server`）—— 测路由和状态码。"""

    def __init__(self, root: Path):
        from http.server import ThreadingHTTPServer
        import threading
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def request(self, method, path, body=None):
        from http.client import HTTPConnection
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


def _scope(role, stores):
    """造一个 `role_scope()` 的返回 —— 判据本身由 `tests/test_roles.py` 钉着。"""
    label = {"store": "门店 张三", "manager": "区长 李四（西北区）", "platform": "平台岗"}[role]
    return {"role": role, "label": label, "who": "张三",
            "stores": None if role == "platform" else set(stores),
            "can": {"plan.export": role != "store"},
            "pages": {}}


class Test范围过滤(unittest.TestCase):
    def _app(self, role, stores):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _root(tmp.name)
        P.save(root, _payload())
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            app = web.App(root, "config/store-X.yaml")
        sc = _scope(role, stores)
        return app, mock.patch.object(web, "role_scope", lambda _app: sc)

    def test_门店只看自己那家(self):
        app, pat = self._app("store", ["青岛城阳万达店"])
        with pat:
            d = app.plan()
        self.assertEqual([r["store"] for r in d["rows"]], ["青岛城阳万达店"])
        self.assertEqual(d["role"], "store")

    def test_区长只看所辖(self):
        app, pat = self._app("manager", ["青岛城阳万达店", "青岛城阳万象汇店"])
        with pat:
            d = app.plan()
        self.assertEqual([r["store"] for r in d["rows"]],
                         ["青岛城阳万达店", "青岛城阳万象汇店"])
        self.assertNotIn("青岛悦荟店", [r["store"] for r in d["rows"]])

    def test_平台看全部(self):
        app, pat = self._app("platform", [])
        with pat:
            d = app.plan()
        self.assertEqual(len(d["rows"]), 3)
        self.assertEqual(d["store_filter"], "")          # 平台不做范围说明


class Test合计跟着过滤走(unittest.TestCase):
    """⭐ 本文件最重要的一条（见模块头）。"""

    def _app(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _root(tmp.name)
        P.save(root, _payload())
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            app = web.App(root, "config/store-X.yaml")
        return app

    def test_门店的合计不是全区(self):
        app = self._app()
        with mock.patch.object(web, "role_scope", lambda _a: _scope("store", ["青岛城阳万达店"])):
            d = app.plan()
        # 全区 = 10+5+1 = 16 台；本店只有 10 台
        self.assertEqual(d["summary"]["total"]["cur"]["qty"], 10,
                         "门店看到的合计必须是它自己的，不是名单内全部的")

    def test_区长是小计(self):
        app = self._app()
        with mock.patch.object(web, "role_scope",
                               lambda _a: _scope("manager", ["青岛城阳万达店", "青岛城阳万象汇店"])):
            d = app.plan()
        self.assertEqual(d["summary"]["total"]["cur"]["qty"], 15)

    def test_平台是全部(self):
        app = self._app()
        with mock.patch.object(web, "role_scope", lambda _a: _scope("platform", [])):
            d = app.plan()
        self.assertEqual(d["summary"]["total"]["cur"]["qty"], 16)

    def test_合计等于明细之和(self):
        app = self._app()
        with mock.patch.object(web, "role_scope", lambda _a: _scope("platform", [])):
            d = app.plan()
        blocks = sum(b["cur"]["qty"] for b in d["summary"]["blocks"].values())
        self.assertEqual(blocks, d["summary"]["total"]["cur"]["qty"])


class Test读不到(unittest.TestCase):
    def test_还没算过给_exists_False(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _root(tmp.name)
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            app = web.App(root, "config/store-X.yaml")
        with mock.patch.object(web, "role_scope", lambda _a: _scope("platform", [])):
            d = app.plan()
        self.assertFalse(d["exists"])
        self.assertIn("还没算过", d["error"])

    def test_过滤完一家都不剩要说清楚(self):
        """⚠ "看着很合理的空"最坑：得说清"这份数据里没有你要看的店"。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _root(tmp.name)
        P.save(root, _payload())
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            app = web.App(root, "config/store-X.yaml")
        with mock.patch.object(web, "role_scope", lambda _a: _scope("store", ["查无此店"])):
            d = app.plan()
        self.assertEqual(d["rows"], [])
        self.assertIn("没有", d.get("error", ""))


class Test接口(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = _root(tmp.name)
        P.save(self.root, _payload(unknown={"手机 / Mate 100系列": 2}))
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_GET_plan(self):
        with mock.patch.object(web, "role_scope", lambda _a: _scope("platform", [])):
            code, body = self.srv.request("GET", "/api/plan")
        self.assertEqual(code, 200)
        self.assertTrue(body["exists"])
        self.assertEqual(body["blocks"], list(M.BLOCKS))
        self.assertEqual(len(body["rows"]), 3)
        # ⚠ 认不出的词要**传到前端**（页面上有一行"要补映射表"）
        self.assertIn("手机 / Mate 100系列", body["unknown"])

    def test_导出_门店_403(self):
        """⭐ 用户 2026-09-21 定的：导出是**区长 / 平台**的功能（跟达成同一个口径）。"""
        with mock.patch.object(web, "role_scope", lambda _a: _scope("store", STORES)):
            code, body = self.srv.request("POST", "/api/plan/export", {})
        self.assertEqual(code, 403)
        self.assertIn("error", body)          # ⚠ 必须带 error，否则前端只显示「HTTP 403」

    def test_导出_区长_能导(self):
        got = {}

        def fake_export(root, d, who="", name=""):
            got["d"] = d
            got["who"] = who
            return {"ok": True, "path": "/x.xlsx", "file": "x.xlsx", "rel": "out/x.xlsx",
                    "rows": 9, "sheets": ["总览", "合计", "明细", "说明"]}

        with mock.patch.object(web, "role_scope",
                               lambda _a: _scope("manager", ["青岛城阳万达店"])), \
                mock.patch("src.features.plan.monthly.export.export", fake_export):
            code, body = self.srv.request("POST", "/api/plan/export", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        # ⚠⚠ **导出的必须是过滤后的那一份** —— 直属近路（直接读落盘 JSON）就等于
        #   把 `filter_plan_rows` 整个废掉，而界面上不会有任何异常。
        self.assertEqual([r["store"] for r in got["d"]["rows"]], ["青岛城阳万达店"])
        self.assertEqual(got["d"]["summary"]["total"]["cur"]["qty"], 10)

    def test_导出失败要说为什么(self):
        with mock.patch.object(web, "role_scope", lambda _a: _scope("manager", STORES)), \
                mock.patch("src.features.plan.monthly.export.export",
                           lambda *a, **k: {"ok": False, "why": "磁盘满了"}):
            code, body = self.srv.request("POST", "/api/plan/export", {})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "磁盘满了")


class Test权限表(unittest.TestCase):
    def test_导出_区长平台可以_门店不行(self):
        self.assertTrue(web._can_for("manager")["plan.export"])
        self.assertTrue(web._can_for("platform")["plan.export"])
        self.assertFalse(web._can_for("store")["plan.export"])

    def test_刷新按钮抓的是云商那两步(self):
        """计划读的是 `erp_sales` ⇒ 先抓云商、再算 —— 跟达成同一条链。"""
        self.assertEqual(web.REFRESH_STEPS["monthly"], ("erp-dump", "plan"))


if __name__ == "__main__":
    unittest.main()


class Test分成到人(unittest.TestCase):
    """用户 2026-09-21：「**门店名点开可以拆分到人**」。

    ⚠ 形态是**行展开**（人做成门店下面的子行）：列是产品线，往右摊的话
      "7 个人 × 每个品类"会把表撑到没法看。要改成列的话说一声。
    """

    APP = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    CSS = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text(encoding="utf-8")

    def test_门店名挂了可点的钩子(self):
        self.assertIn("data-plan-store=", self.APP)
        # 只有**真有人**的店才挂箭头（点了没反应最坑）
        self.assertIn("ppl.length", self.APP)
        self.assertIn("planState.openStores", self.APP)

    def test_展开状态记在localStorage(self):
        self.assertIn("openStores: [...planState.openStores]", self.APP)
        self.assertIn("planState.openStores = new Set(o.openStores || [])", self.APP)

    def test_点击委托里有门店那一支(self):
        body = self.APP[self.APP.index("$('#plan-table')?.addEventListener"):
                        self.APP.index("// 刷新 = **先抓云商新数据")]
        self.assertIn("data-plan-store", body)
        # ⚠ 2026-09-21 晚起走 `planToggleStore`（带**上下动画**），不再直接 renderPlan
        self.assertIn("planToggleStore(", body)
        self.assertIn("planState.openStores", self.APP)

    def test_人那一行的样式(self):
        for cls in (".plan-store-open", ".plan-person", ".plan-who"):
            self.assertIn(cls, self.CSS, cls)

    def test_门店列垂直居中(self):
        """用户 2026-09-22：「月度生意计划里面门店名那一列也是垂直居中吧」——
        跟周度达成表同一条；表头「门店」也要（`.plan-lead` 原来是 bottom）。"""
        i = self.CSS.index(".plan-store {")
        self.assertIn("vertical-align: middle", self.CSS[i:i + 280])
        self.assertIn(".plan-th.plan-col-store", self.CSS)
        j = self.CSS.index(".plan-th.plan-col-store")
        self.assertIn("vertical-align: middle", self.CSS[j:j + 80])
        # 区域那列也要 middle ——「和前面的区域对不上」就是 bottom vs middle
        self.assertIn(".plan-lead { vertical-align: middle; }", self.CSS)
        self.assertNotIn(".plan-lead { vertical-align: bottom; }", self.CSS)

    def test_配色对齐增值表(self):
        """用户：「色彩风格可以参考一下增值的那两个表」—— 表头 hover 底、层级浅灰。"""
        self.assertIn("background: var(--hover)", self.CSS)
        head = self.CSS[self.CSS.index(".plan-table thead th {"):
                        self.CSS.index(".plan-th {")]
        self.assertNotIn("#fafbfc", head)
        self.assertIn("#f7f8fa", self.CSS, "层级底色跟 film 同一档")

    def test_按区域排序(self):
        """用户：「也按区域排好序」—— 前端渲染前按 REGION_ORDER 排一次。"""
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        self.assertIn("PLAN_REGION_ORDER", body)
        self.assertIn("西北区", body)
        self.assertIn(".sort(", body)

    def test_门店那一格是直接拼的_不靠replace(self):
        """⚠⚠ 2026-09-21 真踩过：这一格原来是用

            one(...).replace('<td class="plan-store">', '<td class="plan-store plan-store-open"…')

        挂钩子的。后来给每个格都加了列宽 class（`.plan-col plan-col-store`），
        实际拼出来的是 `<td class="plan-store plan-col plan-col-store">` ——
        **锚串一个都没替上、也不报错**（AGENTS.md 坑 12 那一类），
        结果门店名没有 ▸、点不开，"拆到人"整个失效，而源码里看着这段逻辑还在。
        ⇒ 现在直接拼。这条钉子就是防它再变回 replace。
        """
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        self.assertNotIn("replace('<td class=\"plan-store\">'", body)
        self.assertIn("storeAttr", body, "可点属性得作为参数拼进去，不能再靠事后替换")
        self.assertIn("plan-store-open", body)


class Test表头只有一行(unittest.TestCase):
    """用户 2026-09-21：「**二级分类和三级分类和一级分类同一行**」。

    改之前是**多行表头**：一级那格横跨它下面那几列（`colspan` + 第二行放二级）。
    现在：一级 / 二级 / 三级**都摊在同一行**里，组自己占一列（显示它的小计），
    展开了就把子列**插在它右边**。

    ⚠ 这几条是**形态钉子**（前端没有构建步骤，跑不了 JS 单测 ⇒ 钉源码形状）：
      它们防的是"哪天又顺手改回多行表头"，而不是防手滑。
    """
    APP = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    CSS = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text(encoding="utf-8")
    THEME = (Path(__file__).resolve().parent.parent / "web" / "theme.css").read_text(encoding="utf-8")

    def _fn(self, name):
        """抠出某个函数的函数体 —— 按 `function name(` 到下一个顶层 `}` 切。"""
        i = self.APP.index("function %s(" % name)
        j = self.APP.index("\n}\n", i)
        return self.APP[i:j]

    def test_表头不用colspan_因为只有一行(self):
        body = self._fn("planHeadHtml")
        self.assertNotIn("colspan", body, "表头又变回多行了？")
        self.assertNotIn("rowspan", body)
        self.assertIn("门店", body, "首格（门店）不能少 —— 少了整张表错位一格")
        self.assertIn("合计", body, "尾格（合计）同理")

    def test_展开是插在右边_组自己那列还在(self):
        body = self._fn("planLayout")
        push = body.index("cols.push(")
        recurse = body.index("walk(kids")
        self.assertLess(push, recurse,
                        "顺序反了：得先把自己放进列里，再（展开时）把子列接在它右边")
        self.assertIn("depth + 1", body)

    def test_合计那列还是七个块相加(self):
        """⚠ 一级列（块的小计）现在**一直显示**了，合计**不能**把展开出来的子列再加一遍。

        ⚠ 2026-09-21 拆到人之后这条更要紧：人那一行**走同一个 `one()`** ⇒
          合计口径自动一致（"人的数加起来 = 店里那一行"，后端也有一条测试钉着）。
        """
        body = self._fn("renderPlan")
        self.assertIn("(row.blocks || []).reduce", body)
        self.assertNotIn("cols.reduce", body)

    def test_样式里没有多行表头的吸顶偏移(self):
        """表头不再有第二、三行 ⇒ 那两条 `tr:nth-child(2|3) th { top: … }` 得删掉。"""
        self.assertNotIn(".plan-table thead tr:nth-child(2)", self.CSS)
        self.assertNotIn(".plan-table thead tr:nth-child(3)", self.CSS)

    def test_层级靠样式分得开(self):
        for cls in (".plan-th.plan-grp", ".plan-cell.plan-grp",
                    ".plan-th.plan-lv1", ".plan-th.plan-lv2", ".plan-th.plan-lv3",
                    ".plan-cell.plan-lv2"):
            self.assertIn(cls, self.CSS, cls)

    def test_三级字号和颜色是分开写的(self):
        """用户 2026-09-21：「一级标签字大一点，二级保持现状，三级再小一点。
        然后二级浅灰，三级更深一点，做个区分」——四条都得**各自**写出来，
        少一条就是"三级跟二级看着一样"。"""
        import re
        for lv, size in (("lv1", "14px"), ("lv2", "13px"), ("lv3", "12px")):
            m = re.search(r"\.plan-th\.plan-%s\s*\{([^}]*)\}" % lv, self.CSS)
            self.assertIsNotNone(m, lv)
            self.assertIn("font-size: %s" % size, m.group(1), lv)
        lv1 = re.search(r"\.plan-th\.plan-lv1\s*\{([^}]*)\}", self.CSS).group(1)
        lv2 = re.search(r"\.plan-th\.plan-lv2\s*\{([^}]*)\}", self.CSS).group(1)
        lv3 = re.search(r"\.plan-th\.plan-lv3\s*\{([^}]*)\}", self.CSS).group(1)
        # ⚠ 全局那条 `th { color: var(--muted) }` 会把三级都染成同一个灰 ⇒
        #   一级必须**自己写回正文色**，二三级的灰还得**不一样**
        self.assertIn("var(--text)", lv1)
        self.assertIn("var(--muted)", lv2)
        self.assertIn("var(--muted-2)", lv3)
        self.assertNotEqual(lv2, lv3)


class Test列宽固定和展开动画(unittest.TestCase):
    """用户 2026-09-21：「**每一列的宽度固定吧，展开和收起来的时候有往右移动的动画**」。

    ⚠ 这件事**来回折腾了三轮**，三条全是"看着写了、其实没生效"的假动作 ——
      所以下面每一条都对着一个真踩过的坑，不是防手滑：

      ① `width: 0` 在 `table-layout: fixed` + `width: max-content` 的表上**压不到 0**：
         表的 max-content 仍然算上了那几个格子的文字，多出来的宽度被按比例摊回列上
         （实测那些"0 宽"的列是 101~103px）⇒ 起点其实在终态，看着就是"蹦"出来的。
         ⇒ 必须把表的宽度**钉成像素**，并且**每个格**也写死（只钉表不写格，余量照样被摊）。
      ② 单元格有 `padding: 8px 10px`，`box-sizing: border-box` 下 `width: 0` 只是"内容宽 0"，
         那 20px 内边距压不掉（实测列宽停在 20px）⇒ `.plan-col-hide` 必须一起压 padding。
      ③ 量自然宽度要读一次 `getBoundingClientRect()`，这一读就让格子有了计算样式，
         接着写 0 宽**自己会走一遍过渡**；而且一层 `rAF` 时"冻结态"和"终点态"落在同一帧，
         浏览器只在帧末算一次样式 ⇒ 过渡起点变成"点之前那份自然宽度"（新列一上来就是 108）。
         ⇒ 冻结那一帧关过渡（`.plan-frozen`）+ **两层 rAF** 中间夹一次绘制。
    """
    APP = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    CSS = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text(encoding="utf-8")
    THEME = (Path(__file__).resolve().parent.parent / "web" / "theme.css").read_text(encoding="utf-8")

    def _fn(self, name):
        i = self.APP.index("function %s(" % name)
        j = self.APP.index("\n}\n", i)
        return self.APP[i:j]

    def _css(self, sel):
        i = self.CSS.index(sel + " {")
        return self.CSS[i:self.CSS.index("}", i)]

    def test_表是固定布局且列宽只有一处定义(self):
        t = self._css(".plan-table")
        self.assertIn("table-layout: fixed", t)
        self.assertIn("width: max-content", t, "改回 auto 的话列不满一行时会被按比例拉宽")
        self.assertIn("transition: width", t, "表宽不跟着过渡，中间多出来的一截会被摊回列上")
        self.assertIn("width: 108px", self._css(".plan-col"))

    def test_宽度过渡只挂在表头上(self):
        """⚠⚠ 用户 2026-09-21：「**电脑稍微配置不好的看着卡**」。

        列宽本来就是第一行定的（`table-layout: fixed`）⇒ 表体几千个格子跟着列宽走就够了。
        原来每个 `.plan-col` 都挂 `transition`：一万多个属性一起动，弱机器上就是掉帧。
        ⇒ 过渡只留 `thead th.plan-col` 这一条；`.plan-col` 自己**只声明宽度**。
        """
        c = self._css(".plan-col")
        self.assertNotIn("transition", c, "表体格子又挂上过渡了？那就是「卡」的来源")
        th = self.CSS[self.CSS.index(".plan-table thead th.plan-col"):
                      self.CSS.index(".plan-table thead th.plan-col") + 260]
        self.assertIn("transition: width", th)
        self.assertIn("var(--motion-", th, "时长/缓动要走主题令牌")

    def test_动画慢一档(self):
        """用户：「**左右动画慢点**」⇒ 列宽/行高用 `--motion-slow`（比 normal 慢一档）。

        ⚠ 令牌在 `theme.css`（视觉值只有那一份），而且**两处归零**都要带上它
          （`prefers-reduced-motion` 和 `body[data-motion="off"]`），不然"关动画"漏掉这一档。
        """
        self.assertIn("--motion-slow:", self.THEME)
        self.assertIn("var(--motion-slow)", self.CSS)
        for anchor in ("prefers-reduced-motion", 'body[data-motion="off"]'):
            i = self.THEME.index(anchor)
            blk = self.THEME[i:i + 260]
            self.assertIn("--motion-slow: 0s", blk, "%s 那处没把 slow 归零" % anchor)

    def test_隐藏列要把内边距一起压掉(self):
        """坑②：只写 `width: 0` 时列宽停在 20px（那 20px 就是内边距）。"""
        h = self._css(".plan-col-hide")
        self.assertIn("width: 0", h)
        self.assertIn("padding-left: 0", h)
        self.assertIn("padding-right: 0", h)

    def test_冻结那一帧关掉过渡(self):
        """坑③的第一半：不关过渡的话，写起点那一下自己就走一遍过渡。

        ⚠ 用的是 `transition-duration: 0s`，**不是** `transition: none` ——
          全站"关动画"靠的是**令牌归零、属性留着**（`theme.css` 的 `--motion-*`），
          组件表里出现 `transition: none` 会让那套一键切换漏掉这一处
          （`test_theme.py::Test动效总开关` 也钉着这条）。
        """
        i = self.CSS.index(".plan-frozen")
        f = self.CSS[i:self.CSS.index("}", i)]
        self.assertIn("transition-duration: 0s", f)
        self.assertNotIn("transition: none", f)
        self.assertIn(".plan-frozen *", f, "只关表的过渡不够，格子也得关")

    def test_时长缓动都走主题令牌(self):
        """⚠ 写死 `.18s` 的那个动效就是**关不掉的那一个**（`test_theme.py` 有总闸）。"""
        for sel in (".plan-table", ".plan-col"):
            i = self.CSS.index(sel + " {")
            blk = self.CSS[i:self.CSS.index("}", i)]
            for m in __import__("re").finditer(r"transition:\s*([^;]+);", blk):
                self.assertIn("var(--motion-", m.group(1), sel)
                self.assertIn("var(--ease-", m.group(1), sel)

    def test_冻结时表和每个格都写死像素(self):
        """坑①：表宽 + 每个格的宽度都写显式像素 ⇒ 没有余量可摊，0 宽才是真的 0。"""
        body = self._fn("planFreeze")
        self.assertIn("getBoundingClientRect", body, "自然宽度得量出来（表头文字长的列会自己宽一点）")
        self.assertIn("plan-frozen", body)
        self.assertIn("tbl.style.width", body)
        self.assertIn("c.style.width", body, "每个格也得写 —— 只钉表的话余量照样摊回列上")
        self.assertIn("planColHide", body, "要收起来的那几列，表体也要挂上（不然内边距溢到隔壁）")

    def test_第二帧先开过渡再写宽度(self):
        """坑③的第二半：顺序反了就是从半路出发（起点已经走过一段）。"""
        body = self._fn("planAnimStep")
        open_at = body.index("classList.remove('plan-frozen')")
        flush_at = body.index("void tbl.offsetWidth")
        write_at = body.index("tbl.style.width")
        self.assertLess(open_at, flush_at, "先开过渡，再读一次布局让起点落地")
        self.assertLess(flush_at, write_at, "写完终点宽度才发现起点没落地就晚了")

    def test_展开动画是两层rAF(self):
        """坑③的第三半：一层 rAF 时冻结态和终点态同一帧算样式 ⇒ 没有起点。"""
        body = self._fn("planAnimate")
        self.assertIn("requestAnimationFrame(() => requestAnimationFrame(", body)
        self.assertIn("planState.anim = null", body, "收尾要把动画状态清掉")
        self.assertIn("planAnimStep()", body)
        self.assertIn("planClean()", body)
        # ⚠ 收尾**不再重画整张表**（弱机器上一次重画就是一次可见的卡顿）：
        #   该消失的列/行直接 remove，见 `planClean`
        self.assertNotIn("renderPlan", body[self.body_index(body, "setTimeout"):])
        self.assertIn("PLAN_ANIM_MS", body)

    def body_index(self, body, needle):
        return body.index(needle)

    def test_表体也要挂隐藏类(self):
        """列宽只有第一行说了算，但**表体格的内边距会溢到隔壁列**上（底色、下边框跟着跑）。"""
        body = self._fn("planColHide")
        self.assertIn("querySelectorAll('tr')", body)
        self.assertNotIn("thead tr", body)

    def test_上下动画压的是格子里那层div(self):
        """⭐ 用户 2026-09-21：「**上下的动画加上**」（门店 → 人 那几行）。

        ⚠⚠ `tr` 的 `height` **过渡不了** —— 实测给它写 `height: 0`，行高照样 53px
          （表格行高是**内容顶出来的**，`height` 只是个下限）。
          ⇒ 高度压在每个格子里那层 `.plan-in` 上，格子自己的上下内边距也要一起压。
        """
        self.assertIn(".plan-in", self.CSS)
        i = self.CSS.index(".plan-anim-row .plan-in")
        self.assertIn("transition: height", self.CSS[i:i + 120])
        self.assertIn("padding-top: 0", self._css(".plan-row-hide"))
        self.assertIn("planRowsFreeze", self.APP)
        self.assertIn("plan-row-hide", self._fn("planClean"))

    def test_收起来的行收尾才从DOM摘掉(self):
        """收起时那几行先缩到 0 高（看得见），**收尾**才真从 DOM 里摘掉。"""
        body = self._fn("planClean")
        self.assertIn("r.remove()", body)
        self.assertIn("plan-anim-row", body)

    def test_人那几行包了plan_in并带标记(self):
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        self.assertIn("plan-anim-row", body)
        self.assertIn("plan-in", body)
        # ⚠ 收起时那几行要**留在 DOM 里**才收得动（跟列那边的 extraOpen 一个套路）
        self.assertIn("animRows", body)

    def test_收起时箭头按真实状态画(self):
        """⚠ 收尾不再重画整张表 ⇒ 箭头画错就没人改回来（收完还挂着 ▾）。"""
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        self.assertIn("isOpen", body)
        self.assertIn("(isOpen ? '▾' : '▸')", body)

    def test_标记类不直接压零宽(self):
        """`planColCls` 只打标记 —— 压 0 要等自然宽度量完（在 `planFreeze` 里做）。

        ⚠ 这两件事**不能**回到"渲染时就挂 `plan-col-hide`"的老写法：
          那样第一帧的格子已经是 0 宽，`planFreeze` 再也量不到它们本来多宽。
        """
        body = self._fn("planColCls")
        self.assertIn("plan-anim-grow", body)
        self.assertIn("plan-anim-shrink", body)
        self.assertNotIn("plan-col-hide", body)


class Test合计带环比(unittest.TestCase):
    """⭐ 用户 2026-09-21：「**合计也加上和上个月同期对比**」。

    ⚠ 门店行原来只有 `blocks`（合计是前端把七块加起来现算的）⇒ 没有环比可显示。
      现在后端给一行 `growth`（`metric.total_growth`：**七个块相加后**再算环比），
      前端那一格直接用 —— 别在前端拿七个百分比平均（那玩意儿没有意义）。
    """

    APP = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")

    def test_合计那格也画环比(self):
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        i = body.index("const inTot =")
        seg = body[i:i + 600]
        self.assertIn("planGrowthHtml", seg, "合计那格没有环比")
        self.assertIn("row.growth", seg, "环比要取后端给的那一行 growth")

    def test_口径是七个块相加_不是平均百分比(self):
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        self.assertNotIn("blocks.reduce((a, b) => a + Number(((b.growth", body)
        # 后端那一条在 test_plan.py（`total_growth`）
        from src.features.plan.monthly import metric as M
        self.assertTrue(callable(M.total_growth))


class Test区域汇总折叠(unittest.TestCase):
    """用户 2026-09-22：「点击区域名合并，然后才显示汇总，不是现在这样」。

    默认只列门店、**不画**各区共计；点区域名把该区收成一行 `region_sums`。
    """

    APP = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    CSS = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text(encoding="utf-8")

    def test_区域名可点且默认不画汇总(self):
        self.assertIn("data-plan-region=", self.APP, "区域名要挂可点钩子")
        self.assertIn("collapsedRegions", self.APP, "收起态要记在 planState")
        self.assertIn("planNormRegion", self.APP,
                      "分组键要 strip+空→其他，跟后端 region_sums 一致"
                      "（不同键会「显示同名、点了折不起来」）")
        self.assertIn("collapsedRegions: [...planState.collapsedRegions]", self.APP,
                      "收起态要落 localStorage（跟 open / openStores 一样）")
        self.assertIn("planState.collapsedRegions = new Set(o.collapsedRegions || [])",
                      self.APP, "刷新后收起态要恢复")
        body = self.APP[self.APP.index("function renderPlan("):
                        self.APP.index("async function loadPlan(")]
        # 不是「每区末尾常驻一行共计」—— 只有收起的区才画 film-sum
        self.assertIn("planState.collapsedRegions.has(grp.reg) && sum", body,
                      "汇总行只在该区收起时画")
        self.assertIn("grp.rows.map(renderStoreRow)", body, "展开时画门店明细")
        self.assertIn("planNormRegion(regions0[r.store])", body,
                      "切组也要走同一把尺（否则同名被拆成多组）")

    def test_点击委托里有区域那一支(self):
        body = self.APP[self.APP.index("$('#plan-table')?.addEventListener"):
                        self.APP.index("// 刷新 = **先抓云商新数据")]
        self.assertIn("data-plan-region", body)
        self.assertIn("planToggleRegion(", body)

    def test_汇总行上也能点开(self):
        """收起后只剩汇总行 —— 那一行的区域名必须还能点（否则展不开）。"""
        self.assertIn("planRegionBtn(regText, !!row.is_sum)", self.APP)

    def test_区域按钮有样式(self):
        for cls in (".region-toggle", "#attain-table tr.film-sum .region-toggle"):
            self.assertIn(cls, self.CSS, cls)
