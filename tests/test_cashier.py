# -*- coding: utf-8 -*-
"""收银界面（生活馆利润核算录入端，2026-09-29）—— 存储 / App / 接口 / 页面接线。

开发目标见 `.dsh/docs/2026-09-29-生活馆收银界面-开发目标.md`。钉的都是
"错了没人会报"的那类：

* 政策**按快照保留**（2026-09-30 用户：「老的也保留可查」）——
  反查/新鲜度都认**最新一份**（`fetched_at DESC, rowid DESC`）；
* 编码反查认 `基准提货价*` **带星表头**（xlsx 原样，别"顺手"洗掉）；
* 接口门禁：**区长/平台 403**（收银是门店本机操作）、块内未知子路径 **404**；
* 政策刷新的类锁：失败后必须**释放**（否则一次失败永久卡死）；
* 页面三件套（HTML 面板 / SUBTABS / loader）少一边就点不开。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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

from src import pmall                                                # noqa: E402
from src import web                                                  # noqa: E402
from src.features.cashier import store                               # noqa: E402
from src.features.cashier import exporter                            # noqa: E402

INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _scope(role):
    return {"role": role, "label": "测试·%s" % role, "stores": set(),
            "can": web._can_for(role), "pages": [], "who": "张三",
            "account": "acc", "kind": "", "needs_linglong": False}


class _RootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


# ───────────────────────────────────────────────── 存储层
class Test库与表(_RootCase):
    def test_纯手动新建库三张表都在(self):
        """收银机可能**永远不跑抓取** —— ensure() 自己把库建出来跑到最新。"""
        path = store.ensure(self.root)
        self.assertEqual(path.parent, self.root / "out")
        self.assertRegex(path.name, r"^cbg-\d{4}\.db$")
        import sqlite3
        conn = sqlite3.connect(str(path))
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for t in ("sale_entries", "price_policy", "profit_result", "meta"):
            self.assertIn(t, names)

    def test_已有库优先不再造新的(self):
        out = self.root / "out"
        out.mkdir(parents=True)
        (out / "cbg-2025.db").write_bytes(b"")
        self.assertEqual(store.db_path(self.root), out / "cbg-2025.db")

    def test_ensure幂等(self):
        p1 = store.ensure(self.root)
        p2 = store.ensure(self.root)
        self.assertEqual(p1, p2)
        self.assertEqual(store.policy_meta(self.root),
                         {"rows": 0, "fetched_at": ""})


class Test流水(_RootCase):
    ROW = {"sold_at": "2026-09-29 14:30", "goods_code": "51996188",
           "goods_name": "华为风范双肩包 随行款 棕色", "quantity": 1,
           "amount": 399, "seller": "小张", "note": ""}

    def test_存改删一条龙(self):
        r = store.save_entry(self.root, self.ROW)
        self.assertTrue(r["ok"], r)
        eid = r["id"]
        rows = store.list_entries(self.root, day="2026-09-29")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 399.0)
        self.assertEqual(rows[0]["source"], "manual", "来源默认 manual")

        r2 = store.save_entry(self.root, dict(self.ROW, amount=369.5), entry_id=eid)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(store.list_entries(self.root)[0]["amount"], 369.5)

        self.assertTrue(store.delete_entry(self.root, eid)["ok"])
        self.assertEqual(store.list_entries(self.root), [])

    def test_改不存在的不许静默新建(self):
        r = store.save_entry(self.root, self.ROW, entry_id=999)
        self.assertFalse(r["ok"])
        self.assertIn("没有这条流水", r["why"])
        self.assertEqual(store.list_entries(self.root), [], "失败不许落一行")

    def test_坏数据都回why不抛(self):
        cases = [
            ("sold_at", "乱写", "时间"),
            ("amount", "abc", "数字"),
            ("amount", -1, "负数"),
            ("quantity", 0, "大于 0"),
            ("source", "hack", "来源"),
        ]
        for field, bad, why in cases:
            with self.subTest(field=field):
                r = store.save_entry(self.root, dict(self.ROW, **{field: bad}))
                self.assertFalse(r["ok"])
                self.assertIn(why, r["why"])

    def test_按天过滤_新在前(self):
        store.save_entry(self.root, dict(self.ROW, sold_at="2026-09-28 10:00"))
        store.save_entry(self.root, dict(self.ROW, sold_at="2026-09-29 09:00"))
        store.save_entry(self.root, dict(self.ROW, sold_at="2026-09-29 18:00"))
        day = store.list_entries(self.root, day="2026-09-29")
        self.assertEqual([r["sold_at"] for r in day],
                         ["2026-09-29 18:00", "2026-09-29 09:00"], "当天新→旧")
        self.assertEqual(len(store.list_entries(self.root)), 3, "空 day = 全部")
        self.assertEqual(store.list_entries(self.root, day="2026-01-01"), [])

    def test_销售员下拉靠积累_最近优先(self):
        store.save_entry(self.root, dict(self.ROW, seller="小张"))
        store.save_entry(self.root, dict(self.ROW, seller="小王"))
        store.save_entry(self.root, dict(self.ROW, seller="小张"))
        store.save_entry(self.root, dict(self.ROW, seller=""))
        self.assertEqual(store.sellers(self.root), ["小张", "小王"],
                         "去重 + 最近出现的排前面；空串不算人")


class Test政策快照(_RootCase):
    def _rows(self, *codes):
        return [{"商品编码": c, "商品名称": "货" + c, "基准提货价*": "399.0",
                 "无条件单台返利金额": "75.012"} for c in codes]

    def test_快照保留老的_反查认最新(self):
        """2026-09-30 用户：「老的也保留可查」—— **不再先清后写**。

        同编码存两次 ⇒ 反查回最新那份（`fetched_at DESC, rowid DESC`）；
        上一份里独有的编码（B）不许被清掉。
        """
        store.save_policy(self.root, self._rows("A", "B"))
        r2 = store.save_policy(self.root, [{"商品编码": "A", "商品名称": "新A",
                                            "基准提货价*": "459.0"}])
        self.assertEqual(r2["rows"], 1)
        self.assertIsNotNone(store.lookup(self.root, "B"),
                             "老快照的行要留着（可查）")
        self.assertEqual(store.lookup(self.root, "A")["商品名称"], "新A",
                         "同编码反查认最新一份")
        # 最新那份几行 = policy_meta 的口径（老快照行不掺进来）
        self.assertEqual(store.policy_meta(self.root)["rows"], 1)

    def test_没编码的行不进库(self):
        rows = [{"商品名称": "没有编码的"}, {"商品编码": " X ", "商品名称": "带空格"}]
        r = store.save_policy(self.root, rows)
        self.assertEqual(r["rows"], 1, "没编码反查不到，进库只是死数据")
        self.assertIsNotNone(store.lookup(self.root, "X"), "编码要 strip 再存")

    def test_反查带星表头原样(self):
        store.save_policy(self.root, self._rows("51996188"))
        row = store.lookup(self.root, "51996188")
        self.assertIn("基准提货价*", row, "xlsx 表头的星号不许被'顺手'洗掉")
        self.assertEqual(row["基准提货价*"], "399.0", "金额按解析层原样存（文本）")
        self.assertIn("fetched_at", row)

    def test_反查边界(self):
        self.assertIsNone(store.lookup(self.root, ""), "空编码不许 SELECT")
        self.assertIsNone(store.lookup(self.root, "不存在"))
        store.save_policy(self.root, self._rows("A"))
        self.assertEqual(store.policy_meta(self.root)["rows"], 1)
        self.assertTrue(store.policy_meta(self.root)["fetched_at"])


# ───────────────────────────────────────────────── App 层
class TestApp组合(_RootCase):
    def setUp(self):
        super().setUp()
        self.app = web.App(self.root, "config/store-X.yaml")

    def test_entries一次带回三样(self):
        store.save_entry(self.root, {"sold_at": "2026-09-29 10:00",
                                     "amount": 10, "seller": "小张"})
        d = self.app.cashier_entries("2026-09-29")
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["rows"]), 1)
        self.assertEqual(d["sellers"], ["小张"])
        self.assertIn("policy", d)

    def test_存改删的why透传(self):
        bad = self.app.cashier_entry_save({"sold_at": "坏", "amount": 1})
        self.assertFalse(bad["ok"])
        self.assertIn("时间", bad["why"])
        ok = self.app.cashier_entry_save({"sold_at": "2026-09-29 10:00", "amount": 5})
        self.assertTrue(ok["ok"])
        self.assertTrue(self.app.cashier_entry_delete(ok["id"])["ok"])
        self.assertFalse(self.app.cashier_entry_delete(42)["ok"])

    def test_lookup_found不是错误(self):
        d = self.app.cashier_lookup("空政策也200")
        self.assertEqual((d["ok"], d["found"]), (True, False),
                         "政策没刷新是常态，不是接口错误")


class Test政策刷新锁与链路(_RootCase):
    def setUp(self):
        super().setUp()
        self.app = web.App(self.root, "config/store-X.yaml")

    def _ok_chain(self):
        return [
            mock.patch.object(pmall, "ensure_session",
                              lambda root=None, say=None, **k:
                              (say and say("复用已开着的登录窗口（18660228618）"),
                               pmall.PmallSession(cookies="a=b",
                                                  csrf="x.y.z", source="window"))[1]),
            mock.patch.object(pmall, "fetch_policy",
                              lambda sess, **k: (b"PK\x03\x04", {"fileName": "s.xlsx"})),
            mock.patch.object(pmall, "parse_policy",
                              lambda data: [{"商品编码": "51996188",
                                             "商品名称": "双肩包",
                                             "基准提货价*": "399.0"}]),
        ]

    def test_全链路落库(self):
        patches = self._ok_chain()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        d = self.app.cashier_policy_refresh()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["rows"], 1)
        self.assertEqual(d["file"], "s.xlsx")
        self.assertTrue(d["log"], "say 的过程要带回来（界面要知道发生了什么）")
        self.assertIsNotNone(store.lookup(self.root, "51996188"))

    def test_登录超时的why原样出去(self):
        with mock.patch.object(
                pmall, "ensure_session",
                side_effect=pmall.PmallError("等登录超时（600 秒）没完成 —— "
                                             "下次点更新会重新弹窗")):
            d = self.app.cashier_policy_refresh()
        self.assertFalse(d["ok"])
        self.assertIn("等登录超时", d["why"])

    def test_失败之后锁要释放(self):
        """⚠ 类锁没释放 = 一次失败**永久卡死**（再点永远"正在更新"）。"""
        with mock.patch.object(pmall, "ensure_session",
                               side_effect=pmall.PmallError("断了")):
            self.assertFalse(self.app.cashier_policy_refresh()["ok"])
        # 还能拿到锁 = 释放了
        self.assertTrue(self.app._cashier_policy_lock.acquire(blocking=False))
        self.app._cashier_policy_lock.release()

    def test_并发点第二次直接劝退(self):
        self.assertTrue(self.app._cashier_policy_lock.acquire(blocking=False))
        try:
            d = self.app.cashier_policy_refresh()
            self.assertFalse(d["ok"])
            self.assertIn("正在更新", d["why"])
        finally:
            self.app._cashier_policy_lock.release()


# ───────────────────────────────────────────────── 接口层（真 HTTP）
class _Server:
    """照抄 `test_export.py` 的套件 —— 测的是**真路由和状态码**。"""

    def __init__(self, root: Path):
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛城阳万象汇店"\nstore_code: "SCN231409"\n',
            encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return r.status, raw

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Test接口(_RootCase):
    def setUp(self):
        super().setUp()
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_门店能录能查(self):
        st, d = self.srv.request("POST", "/api/cashier/entry-save",
                                 {"sold_at": "2026-09-29 14:00", "amount": 399,
                                  "goods_code": "51996188", "seller": "小张"})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("GET", "/api/cashier/entries?day=2026-09-29")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["rows"]), 1)
        st, d = self.srv.request("GET", "/api/cashier/lookup?code=51996188")
        self.assertEqual((st, d["ok"], d["found"]), (200, True, False))

    def test_区长平台_403带error文案(self):
        """收银是**门店本机**的操作 —— 管多店的身份不能替门店记一笔（M17 红线）。"""
        for role in ("manager", "platform"):
            with self.subTest(role=role):
                with mock.patch.object(web, "role_scope",
                                       lambda _a, r=role: _scope(r)):
                    st, d = self.srv.request("GET", "/api/cashier/entries")
                self.assertEqual(st, 403)
                self.assertTrue(d.get("forbidden"))
                self.assertIn("error", d, "前端 api() 只认 error/message")
                self.assertIn("收银", d["error"])

    def test_门店身份放行_有forbid也不误伤(self):
        with mock.patch.object(web, "role_scope",
                               lambda _a: _scope("store")):
            st, d = self.srv.request("GET", "/api/cashier/entries")
        self.assertEqual(st, 200, d)

    def test_块内未知子路径_404不糊过去(self):
        st, d = self.srv.request("GET", "/api/cashier/nothing-here")
        self.assertEqual(st, 404)
        self.assertIn("error", d)

    def test_坏数据回400带error(self):
        st, d = self.srv.request("POST", "/api/cashier/entry-save",
                                 {"sold_at": "坏", "amount": 1})
        self.assertEqual(st, 400)
        self.assertIn("时间", d.get("error", ""))

    def test_生活馆不拦收银接口(self):
        """`LIFEHALL_GONE` 是黑名单 —— 收银是生活馆自己的功能，绝不能进黑名单。"""
        old = __import__("os").environ.get("CBG_EDITION", "full")
        try:
            __import__("os").environ["CBG_EDITION"] = "lifehall"
            from src import edition
            edition.reload()
            self.assertFalse(web.lifehall_gone("/api/cashier/entries"))
            self.assertFalse(web.lifehall_gone("/api/cashier/policy-refresh"))
        finally:
            __import__("os").environ["CBG_EDITION"] = old
            edition.reload()

    def test_政策刷新路由接通_不真打pmall(self):
        with mock.patch.object(
                web.App, "cashier_policy_refresh",
                lambda self: {"ok": True, "rows": 4177, "log": ["x"]}):
            st, d = self.srv.request("POST", "/api/cashier/policy-refresh", {})
        self.assertEqual((st, d["rows"]), (200, 4177))


# ───────────────────────────────────────────────── 页面接线
class Test页面接线(unittest.TestCase):
    def test_HTML三件套都在(self):
        """面板 / 子面板 / 直连入口 —— 少一个都点不开（switchTab 全靠 id 对）。"""
        self.assertIn('id="panel-cashier"', INDEX_HTML)
        self.assertIn('id="subpanel-cashier"', INDEX_HTML)
        self.assertIn('data-direct-subtab="cashier"', INDEX_HTML)
        self.assertIn('data-tab="cashier" data-subtab="cashier"', INDEX_HTML,
                      "key 要同时在 data-tab/data-subtab 上：前者给 tab 同步、"
                      "后者给 roles 的 HTML↔PAGE_RULES 双向对照")

    def test_表单关键控件在(self):
        for cid in ("cashier-sold-at", "cashier-scan", "cashier-name",
                    "cashier-qty", "cashier-amount", "cashier-seller",
                    "cashier-note", "cashier-save", "cashier-day",
                    "cashier-table", "cashier-refresh"):
            with self.subTest(id=cid):
                self.assertIn('id="%s"' % cid, INDEX_HTML)

    def test_订单卡新结构的控件在(self):
        """2026-09-30 改版：吸顶录入 + 汇总条 + 卡片容器 + 导入/黑名单入口。"""
        for cid in ("cashier-scan", "cashier-scan-ok", "cashier-summary",
                    "cashier-count", "cashier-qty-sum", "cashier-amount-sum",
                    "cashier-acc-sum", "cashier-cards", "cashier-import",
                    "cashier-blacklist-open", "cashier-blacklist-row",
                    "cashier-blacklist-input", "cashier-blacklist-save",
                    "cashier-blacklist-hint", "cashier-day-label",
                    "cashier-sn", "cashier-cancel"):
            with self.subTest(id=cid):
                self.assertIn('id="%s"' % cid, INDEX_HTML)
        # 吸顶容器：录入区和汇总条都得在 .cashier-top 里（在 cashier-cards 之前）
        i = INDEX_HTML.index('id="subpanel-cashier"')
        blk = INDEX_HTML[i:i + 4000]
        self.assertIn('class="cashier-top"', blk)
        t = blk.index('class="cashier-top"')
        end = blk.index('id="cashier-cards"')
        seg = blk[t:end]
        self.assertIn('id="cashier-summary"', seg, "汇总条必须在吸顶容器里")
        self.assertIn('id="cashier-scan"', seg, "录入区必须在吸顶容器里")

    def test_收银订单卡样式段在(self):
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        # ⚠ 认「选择器 + {」，别裸认 token —— 注释里提到 .cc-side 也算命中，
        #   会假绿（2026-09-30 code review 指出）。
        for token in ("cashier-top", "cashier-card", "cc-side",
                      "cc-group-title", "pay-block", "cc-close"):
            with self.subTest(token=token):
                self.assertIsNotNone(
                    re.search(r"\.%s\s*\{" % re.escape(token), css),
                    "找不到选择器 .%s {" % token)
        self.assertIn("position: sticky",
                      css[css.index(".cashier-top"):css.index(".cashier-top") + 400])

    def test_SUBTABS和loader接上了(self):
        blk = APP_JS[APP_JS.index("const SUBTABS = {"):]
        blk = blk[:blk.index("\n};")]
        self.assertIn("cashier: ['cashier']", blk)
        self.assertIn("cashier: () => loadCashier()", APP_JS)
        for fn in ("loadCashier", "cashierLookup", "cashierSave",
                   "cashierRemove", "cashierRefreshPolicy", "bindCashierEvents"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)

    def test_扫码枪回车直接进金额(self):
        """扫码枪 = 键盘：回车要反查**并把光标送进实收金额** —— 收银的主路径。"""
        i = APP_JS.index("function bindCashierEvents")
        blk = APP_JS[i:i + 1600]
        self.assertIn("cashierLookup(true)", blk, "回车没走'聚焦金额'那条路")

    def test_卡片渲染和汇总接上了(self):
        """表格换订单卡（Task 9）：三函数在、旧表格渲染删干净、汇总四要素有落点。"""
        for fn in ("renderCashierCards", "cashierRenderSummary",
                   "cashierCardHtml"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        self.assertNotIn("renderCashierTable", APP_JS, "旧表格渲染要删干净")
        self.assertNotIn("cashier-code", APP_JS)
        # 汇总四要素：笔数/件数/实收/配件 都有落点
        for cid in ("cashier-count", "cashier-qty-sum",
                    "cashier-amount-sum", "cashier-acc-sum"):
            self.assertIn("'%s'" % cid, APP_JS)

    def test_卡内编辑接上了(self):
        for fn in ("cashierCardEdit", "cashierCardSave", "cashierCardCancel",
                   "cashierAccAdd", "cashierAccDel", "cashierEditState"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        # 卡内编辑读的是卡里的字段，不再回填顶部表单
        self.assertNotIn("cashierFill(", APP_JS, "回填顶部表单的老路要拆")

    def test_支付块和删除分叉接上了(self):
        """Task 11：卡内可编辑的支付方块 + ✕ 按来源分叉（玲珑排除 / 手工真删）。"""
        for fn in ("cashierPayEditBlock", "cashierPayAdd", "cashierPayDel",
                   "cashierPaySync", "cashierCardClose", "cashierPAY_METHODS"):
            with self.subTest(fn=fn):
                self.assertIn(fn, APP_JS)
        # 支付清单写死但存原始字符串（清单变动不坏老数据）
        self.assertIn("支付宝直连", APP_JS)
        self.assertIn("微信直连", APP_JS)
        # 删除语义分叉：linglong → 软排除；manual → cashierRemove（真删接口在它里面）
        i = APP_JS.index("function cashierCardClose")
        blk = APP_JS[i:i + 700]
        self.assertIn("exclude", blk, "玲珑卡 ✕ 没走软排除")
        self.assertIn("cashierRemove", blk, "手工卡 ✕ 没走真删")
        self.assertIn("/api/cashier/entry-delete", APP_JS, "真删接口还接在")

    def test_导入和黑名单入口接上了(self):
        """Task 12：一键导入按钮（防连点）+ 排除关键词行内设置。"""
        for fn in ("cashierImport", "cashierBlacklistOpen",
                   "cashierBlacklistSave"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        for ep in ("/api/cashier/import", "/api/cashier/import-settings",
                   "/api/cashier/exclude"):
            with self.subTest(ep=ep):
                self.assertIn(ep, APP_JS)
        # 导入按钮跑的时候要禁用（拉单可能几十秒，防连点）
        i = APP_JS.index("function cashierImport")
        self.assertIn("disabled", APP_JS[i:i + 900])

    def test_两段式和导出接上了(self):
        """2026-09-30 两段式：确认添加进暂存 → 保存并记录入库 → 导出走下载。"""
        for cid in ("cashier-commit", "cashier-export", "cashier-category"):
            with self.subTest(id=cid):
                self.assertIn('id="%s"' % cid, INDEX_HTML)
        self.assertIn("确认添加", INDEX_HTML, "「保存一笔」要改名 —— 它不入账了")
        self.assertNotIn(">保存一笔<", INDEX_HTML)
        self.assertIn("保存并记录", INDEX_HTML)
        for fn in ("cashierCommit", "cashierExport"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        self.assertIn("status: 'staged'", APP_JS, "确认添加要进暂存，不直接入账")
        self.assertIn("/api/cashier/commit", APP_JS)
        self.assertIn("/api/cashier/export", APP_JS)
        self.assertIn("未入库", APP_JS, "暂存行要有徽章")
        i = APP_JS.index("function cashierExport")
        self.assertIn("triggerDownload", APP_JS[i:i + 700],
                      "导出要走浏览器下载，不是新窗口")

    def test_品类下拉录入和编辑两边都在(self):
        for o in ("手机", "平板", "笔记本", "穿戴", "音频", "配件",
                  "第三方配件", "服务"):
            with self.subTest(opt=o):
                self.assertIn("<option>%s</option>" % o, INDEX_HTML)
        self.assertIn("cashierCATEGORIES", APP_JS)
        self.assertIn('data-prod-f="category"', APP_JS,
                      "品类在**商品行**上（第三轮：品类跟单条商品走）")
        self.assertIn("category: ($('#cashier-category')", APP_JS,
                      "录入卡的品类要进保存体（= 首商品的品类）")

    def test_卡内商品行接上了(self):
        """一张卡 = 一个订单：卡内能加商品行，数量/实收按 Σ 派生只读。"""
        for fn in ("cashierProdAdd", "cashierProdDel", "cashierProdSync",
                   "cashierProdRead", "cashierProdBlank"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        self.assertIn("+ 添加商品", APP_JS)
        self.assertIn("data-prod-blocks", APP_JS)
        self.assertNotIn('<span class="k">品类</span>', APP_JS,
                         "卡片右侧的品类行要撤（品类跟商品走）")
        # 派生值：实收/数量只读，由商品行 Σ 出来
        self.assertIn('data-f="amount" type="number" step="0.01" readonly',
                      APP_JS)
        self.assertIn('data-f="quantity" type="number"', APP_JS)
        # 保存体带 products；草稿在编辑态铺出来
        i = APP_JS.index("function cashierEditState")
        self.assertIn("products", APP_JS[i:i + 1600])
        self.assertIn("_cashierEditProd", APP_JS)

    def test_node语法检查(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("本机没有 node")
        r = subprocess.run([node, "--check", str(ROOT / "web" / "app.js")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_注册表两版都有cashier(self):
        """roles 的双向对照按 **full 版**跑 —— full 注册表少了 cashier 就红。"""
        from src import edition
        from src.features import build_all
        old = __import__("os").environ.get("CBG_EDITION", "full")
        try:
            __import__("os").environ["CBG_EDITION"] = "full"
            edition.reload()
            self.assertIn("cashier", [f.key for f in build_all()])
            self.assertIn("cashier", web.PAGE_RULES,
                          "full 版 PAGE_RULES 里没有 cashier → HTML 那个 key "
                          "会被前端当'看不见'（roles 双向对照的另一半）")
        finally:
            __import__("os").environ["CBG_EDITION"] = old
            edition.reload()


# ────────────────────────────────────── 迁移：SN/配件/支付/来源/排除 列
class Test迁移新列(_RootCase):
    def test_五列都在_老行读得出来(self):
        import sqlite3
        path = store.ensure(self.root)
        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sale_entries)")}
        # 模拟"迁移前写进去的老行"：只给老列，新列是 NULL
        conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, created_at, updated_at)"
            " VALUES ('2026-09-30 10:00:00','c1','老货',1,99,'小张','',"
            " 'manual','2026-09-30 10:00:00','2026-09-30 10:00:00')")
        conn.commit()
        conn.close()
        self.assertTrue(
            {"sn", "accessories", "payments", "external_id", "excluded"} <= cols,
            "m007 没把新列补上：%s" % sorted(cols))
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(len(rows), 1, "老行要能读出来")
        r = rows[0]
        self.assertEqual(r["sn"], "")
        self.assertEqual(r["accessories"], [])
        self.assertEqual(r["payments"], [])
        self.assertEqual(r["external_id"], "")
        self.assertEqual(r["excluded"], 0)

    def test_唯一索引只管非空_external_id(self):
        """部分索引的行为钉子：NULL / 空串互不冲突，真单号重复才 IntegrityError。"""
        import sqlite3
        path = store.ensure(self.root)
        conn = sqlite3.connect(str(path))
        base = ("INSERT INTO sale_entries (sold_at, amount, external_id)"
                " VALUES ('2026-09-30 10:00:00', 1, %s)")

        # 两条 external_id 为 NULL 的行（手工单）都能插
        conn.execute(base % "NULL")
        conn.execute(base % "NULL")
        # 两条 external_id='' 的行也能插（部分索引不参与）
        conn.execute(base % "''")
        conn.execute(base % "''")
        conn.commit()
        # 两条同 external_id 非空的行：第二条必报 IntegrityError（玲珑单幂等）
        conn.execute(base % "'DOC-1'")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(base % "'DOC-1'")
        conn.close()


# ────────────────────────────────────── 新字段：SN / 配件 / 支付
class Test新字段存取(_RootCase):
    def test_存改查一条龙(self):
        body = {
            "sold_at": "2026-09-30 14:32", "goods_code": "6901", "goods_name": "MatePad",
            "quantity": 1, "amount": 1899, "seller": "张三", "note": "老客户",
            "sn": "HXR123",
            "accessories": [{"name": "原装保护壳", "amount": 199}],
            "payments": [{"method": "现金", "amount": 1000},
                          {"method": "微信直连", "amount": 899}],
        }
        res = store.save_entry(self.root, body)
        self.assertTrue(res.get("ok"), res)
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["sn"], "HXR123")
        self.assertEqual(r["accessories"],
                         [{"name": "原装保护壳", "amount": 199.0}])
        self.assertEqual([p["method"] for p in r["payments"]], ["现金", "微信直连"])
        # 改
        body.update({"id": r["id"], "sn": "HXR456", "accessories": []})
        self.assertTrue(store.save_entry(self.root, body, entry_id=r["id"]).get("ok"))
        r2 = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r2["sn"], "HXR456")
        self.assertEqual(r2["accessories"], [])

    def test_坏明细回why不抛(self):
        base = {"sold_at": "2026-09-30 14:32", "amount": 100}
        for bad, frag in (
                ({"accessories": [{"amount": 5}]}, "名字"),
                ({"accessories": [{"name": "壳", "amount": "abc"}]}, "数字"),
                ({"payments": [{"method": "现金", "amount": -1}]}, "负数"),
                ({"payments": "不是列表"}, "列表"),
                ({"accessories": [["壳"]]}, "不是对象"),
                ({"payments": [{"method": "现金"}]}, "数字")):
            with self.subTest(frag=frag):
                res = store.save_entry(self.root, dict(base, **bad))
                self.assertFalse(res.get("ok"))
                self.assertIn(frag, res.get("why", ""))

    def test_明细接受JSON字符串入参(self):
        """前端可能把明细当 JSON 字符串发过来 —— 存进去、读出来都得是 list。"""
        res = store.save_entry(self.root, {
            "sold_at": "2026-09-30 14:32", "amount": 109,
            "accessories": '[{"name":"壳","amount":9}]',
        })
        self.assertTrue(res.get("ok"), res)
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r["accessories"], [{"name": "壳", "amount": 9.0}])

    def test_nan_inf不进库(self):
        """nan/inf 过得了 `< 0` 检查（nan < 0 是 False），写进去 json.dumps
        出 NaN → 前端 JSON.parse 挂 → 整页读挂且删不掉毒行。"""
        base = {"sold_at": "2026-09-30 14:32"}
        for bad, frag in (
                ({"amount": "nan"}, "数字"),
                ({"amount": "inf"}, "数字"),
                ({"amount": "1e999"}, "数字"),          # 溢出成 inf
                ({"amount": 100, "quantity": "nan"}, "数量"),
                ({"amount": 100,
                  "accessories": [{"name": "壳", "amount": "nan"}]}, "数字"),
                ({"amount": 100,
                  "payments": [{"method": "现金", "amount": "inf"}]}, "数字")):
            with self.subTest(why=frag, bad=bad):
                res = store.save_entry(self.root, dict(base, **bad))
                self.assertFalse(res.get("ok"), res)
                self.assertIn(frag, res.get("why", ""))

    def test_毒JSON读侧钉子(self):
        """读出去的流水必须是严格 JSON —— 不许 NaN/Infinity 常量（防回归）。"""
        self.assertTrue(store.save_entry(self.root, {
            "sold_at": "2026-09-30 14:32", "amount": 100,
            "accessories": [{"name": "壳", "amount": 9}],
            "payments": [{"method": "现金", "amount": 100}],
        }).get("ok"))
        raw = json.dumps(store.list_entries(self.root, day="2026-09-30"))

        def _boom(c):
            raise ValueError("毒常量 %s" % c)
        json.loads(raw, parse_constant=_boom)   # NaN/Infinity 会在这里抛

    def test_软排除两分支都过滤(self):
        """excluded=1 的行不许出现在任何 list_entries 分支里。"""
        self.assertTrue(store.save_entry(self.root, {
            "sold_at": "2026-09-30 14:32", "amount": 100}).get("ok"))
        import sqlite3
        conn = sqlite3.connect(str(store.ensure(self.root)))
        conn.execute("UPDATE sale_entries SET excluded=1")
        conn.commit()
        conn.close()
        self.assertEqual(store.list_entries(self.root), [])
        self.assertEqual(store.list_entries(self.root, day="2026-09-30"), [])


# ────────────────────────────────────── 导入黑名单（本机设置）
class Test导入黑名单设置(_RootCase):
    def test_默认空_存读往返(self):
        from src.features.cashier import import_cfg
        self.assertEqual(import_cfg.load(self.root), [])
        self.assertTrue(import_cfg.save(self.root, ["样机", "展示机", "样机"]))
        self.assertEqual(import_cfg.load(self.root), ["样机", "展示机"])

    def test_空串过滤_坏文件当空(self):
        from src.features.cashier import import_cfg
        import_cfg.save(self.root, ["  ", "已退货", ""])
        self.assertEqual(import_cfg.load(self.root), ["已退货"])
        p = self.root / ".secrets" / "cashier-import.json"
        p.write_text("{{{", encoding="utf-8")
        self.assertEqual(import_cfg.load(self.root), [], "坏文件当没设置，不抛")


# ────────────────────────────────────── 软排除 / 玲珑导入生成流水
def _seed_order(conn, dn, day="2026-09-30", remark="", name="MatePad Air",
                qty=1, sn="HXR001", amount=1899.0, guide="张三"):
    """直接喂 orders / order_lines 两行（导入的读侧只认这两张表）。"""
    conn.execute(
        "INSERT INTO orders (document_no, doc_create_time, included_tax_amount,"
        " remark, consumer_guide_name) VALUES (?,?,?,?,?)",
        (dn, "%s 14:32:00" % day, amount, remark, guide))
    conn.execute(
        "INSERT INTO order_lines (document_no, line_no, sn, ean, item_name,"
        " quantity) VALUES (?,?,?,?,?,?)", (dn, 1, sn, "6901234567890", name, qty))


class Test排除与导入(_RootCase):
    def _mk_orders_table(self, path):
        # orders/order_lines 是 dump.SCHEMA 建的（m001 之前就存在），测试里手动补
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY,"
                     " doc_create_time TEXT, included_tax_amount REAL, remark TEXT,"
                     " consumer_guide_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT,"
                     " line_no INTEGER, sn TEXT, ean TEXT, item_name TEXT,"
                     " quantity REAL)")
        conn.commit()
        conn.close()

    def test_导入生成玲珑卡_再导不重复(self):
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN001")
        _seed_order(conn, "DN002", name="Watch GT5", qty=2, sn="HXR002")
        conn.commit()
        conn.close()
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res["ok"], res["imported"]), (True, 2))
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(len(rows), 2)
        r = {x["external_id"]: x for x in rows}["DN001"]
        self.assertEqual((r["source"], r["sn"], r["seller"]),
                         ("linglong", "HXR001", "张三"))
        self.assertEqual(r["amount"], 1899.0)
        # 再导一次：幂等
        res2 = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res2["imported"], res2["skipped_dup"]), (0, 2))

    def test_黑名单命中的单不入卡(self):
        from src.features.cashier import import_cfg
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN003", remark="样机勿售")
        _seed_order(conn, "DN004", remark="正常单")
        conn.commit()
        conn.close()
        import_cfg.save(self.root, ["样机"])
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["skipped_blacklist"], 1)
        ids = [x["external_id"] for x in store.list_entries(self.root, day="2026-09-30")]
        self.assertEqual(ids, ["DN004"])

    def test_多行单聚合_名称等N件编码拼接(self):
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN005")
        conn.execute("INSERT INTO order_lines (document_no, line_no, sn, ean,"
                     " item_name, quantity) VALUES (?,?,?,?,?,?)",
                     ("DN005", 2, "HXR002", "69999", "碎屏险", 1))
        conn.commit()
        conn.close()
        store.entries_from_orders(self.root, "2026-09-30")
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r["quantity"], 2)
        self.assertIn("等2件", r["goods_name"])
        self.assertIn("6901234567890", r["goods_code"])
        self.assertIn("69999", r["goods_code"])
        self.assertEqual(r["sn"], "HXR001|HXR002")

    def test_软排除后列表不见_再导不回来_手工单删不掉排除标记依赖(self):
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN006")
        conn.commit()
        conn.close()
        store.entries_from_orders(self.root, "2026-09-30")
        row = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertTrue(store.exclude_entry(self.root, row["id"]).get("ok"))
        self.assertEqual(store.list_entries(self.root, day="2026-09-30"), [])
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res["imported"], res["skipped_dup"]), (0, 1),
                         "已排除的 external_id 也要挡住重新导入")
        # 手工单不允许走软排除（语义分叉：手工 ✕ = 真删）
        mid = store.save_entry(self.root,
                               {"sold_at": "2026-09-30 10:00", "amount": 1})["id"]
        bad = store.exclude_entry(self.root, mid)
        self.assertFalse(bad.get("ok"))
        self.assertIn("手工", bad.get("why", ""))

    def test_零明细单_名称用单号兜底不空白(self):
        """dump 详情失败会留 0 行明细的单 —— 卡面不能是空字符串（没法认）。"""
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        # 只有 orders 行、order_lines 一行都没有（dump 详情失败的真实形状）
        conn.execute(
            "INSERT INTO orders (document_no, doc_create_time, included_tax_amount,"
            " remark, consumer_guide_name) VALUES (?,?,?,?,?)",
            ("DN-NO-LINE", "2026-09-30 14:32:00", 99.0, "", "张三"))
        conn.commit()
        conn.close()
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res["ok"], res["imported"]), (True, 1))
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertTrue(r["goods_name"], "0 行明细不许落空名")
        self.assertIn("DN-NO-LINE", r["goods_name"])

    def test_inf金额归0_导入不废整批(self):
        """SQLite REAL 存得出 inf —— 归 0 保住页面（导入路径拒收会废掉整批）。"""
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN-INF2", amount=float("inf"))
        _seed_order(conn, "DN-OK", amount=1899.5)
        conn.commit()
        conn.close()
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertTrue(res["ok"], res)
        rows = {x["external_id"]: x
                for x in store.list_entries(self.root, day="2026-09-30")}
        self.assertEqual(rows["DN-INF2"]["amount"], 0.0)
        self.assertEqual(rows["DN-OK"]["amount"], 1899.5)


# ────────────────────────────────────── 导入 / 排除 / 黑名单 接口
class Test导入接口(_RootCase):
    def setUp(self):
        super().setUp()
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_黑名单读写往返(self):
        st, d = self.srv.request("PUT", "/api/cashier/import-settings",
                                 {"blacklist": ["样机", " 展示 "]})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("GET", "/api/cashier/import-settings")
        self.assertEqual((st, d["blacklist"]), (200, ["样机", "展示"]))

    def test_导入把网络那步mock掉_只验编排(self):
        def fake_import(self, body):
            return {"ok": True, "day": body.get("day"),
                    "imported": 3, "skipped_blacklist": 1, "skipped_dup": 2}
        with mock.patch.object(web.App, "cashier_import", fake_import):
            st, d = self.srv.request("POST", "/api/cashier/import",
                                     {"day": "2026-09-30"})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["imported"], d["skipped_blacklist"], d["skipped_dup"]),
                         (3, 1, 2))

    def test_导入日期不对_400带error(self):
        with mock.patch.object(web.App, "cashier_import",
                               lambda self, b: {"ok": False, "why": "日期格式不对"}):
            st, d = self.srv.request("POST", "/api/cashier/import", {"day": "x"})
        self.assertEqual(st, 400)
        self.assertIn("日期", d["error"])

    def test_导入真路径_坏日期不500(self):
        """⚠ 不 mock App.cashier_import —— 编排测试全绿也挡不住实现层崩（2026-09-30 实测教训）。"""
        st, d = self.srv.request("POST", "/api/cashier/import", {"day": "not-a-date"})
        self.assertEqual(st, 400)
        self.assertIn("日期", d.get("error", ""))

    def test_导入并发点第二次劝退_跑完锁要释放(self):
        """⚠ 类锁没释放 = 一次失败**永久卡死**（再点永远"正在导入"）。"""
        lock = web.App._cashier_import_lock
        self.assertTrue(lock.acquire(blocking=False))
        try:
            st, d = self.srv.request("POST", "/api/cashier/import",
                                     {"day": "2026-09-30"})
            self.assertEqual(st, 400)
            self.assertIn("正在导入", d.get("error", ""))
        finally:
            lock.release()
        # 上一次（含失败路径）跑完后锁要能再进 —— 坏日期走完整条真路径后释放
        st, d = self.srv.request("POST", "/api/cashier/import", {"day": "bad"})
        self.assertEqual((st, "日期" in d.get("error", "")), (400, True))
        self.assertTrue(lock.acquire(blocking=False), "跑完没释放 = 永久卡死")
        lock.release()

    def test_黑名单保存写失败_人话不500(self):
        with mock.patch("src.features.cashier.import_cfg.save",
                        side_effect=OSError("磁盘满")):
            st, d = self.srv.request("PUT", "/api/cashier/import-settings",
                                     {"blacklist": ["x"]})
        self.assertEqual(st, 400)
        self.assertIn("写不进去", d["error"])

    def test_排除接口_软排除走通(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00",
                                     "amount": 1, "source": "linglong"})
        _, rows = self.srv.request("GET", "/api/cashier/entries?day=2026-09-30")
        eid = rows["rows"][0]["id"]
        st, d = self.srv.request("POST", "/api/cashier/exclude", {"id": eid})
        self.assertEqual(st, 200, d)
        _, rows2 = self.srv.request("GET", "/api/cashier/entries?day=2026-09-30")
        self.assertEqual(rows2["rows"], [])

    def test_区长平台照样403(self):
        with mock.patch.object(web, "role_scope", lambda _a: _scope("manager")):
            st, d = self.srv.request("POST", "/api/cashier/import",
                                     {"day": "2026-09-30"})
        self.assertEqual(st, 403)


# ────────────────────────────────────── 暂存两段式（2026-09-30）：确认添加 → 保存并记录
class Test暂存与入库(_RootCase):
    def test_迁移补两列_老行读出来是已入库(self):
        import sqlite3
        path = store.ensure(self.root)
        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sale_entries)")}
        self.assertIn("category", cols, "m008 没补 category")
        self.assertIn("status", cols, "m008 没补 status")
        conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, created_at, updated_at)"
            " VALUES ('2026-09-30 10:00:00','c1','老货',1,99,'小张','',"
            " 'manual','2026-09-30 10:00:00','2026-09-30 10:00:00')")
        conn.commit()
        conn.close()
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r["status"], "saved", "老行（status 空）要当已入库")
        self.assertEqual(r["category"], "")

    def test_新录默认已入库_给了staged才暂存(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00", "amount": 1})
        store.save_entry(self.root, {"sold_at": "2026-09-30 11:00", "amount": 2,
                                     "status": "staged"})
        rows = {str(r["sold_at"])[11:16]: r for r in
                store.list_entries(self.root, day="2026-09-30")}
        self.assertEqual(rows["10:00"]["status"], "saved")
        self.assertEqual(rows["11:00"]["status"], "staged")

    def test_保存并记录_只转当天的暂存(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00", "amount": 1,
                                     "status": "staged"})
        store.save_entry(self.root, {"sold_at": "2026-09-30 11:00", "amount": 2,
                                     "status": "staged"})
        store.save_entry(self.root, {"sold_at": "2026-10-01 11:00", "amount": 3,
                                     "status": "staged"})
        res = store.commit_entries(self.root, "2026-09-30")
        self.assertEqual((res.get("ok"), res.get("saved")), (True, 2), res)
        rows = {str(r["sold_at"]): r for r in store.list_entries(self.root)}
        self.assertEqual(rows["2026-09-30 10:00"]["status"], "saved")
        self.assertEqual(rows["2026-09-30 11:00"]["status"], "saved")
        self.assertEqual(rows["2026-10-01 11:00"]["status"], "staged",
                         "别的天的暂存不许被顺手转正")
        # 没有暂存也要回 ok（第二次点、空手点）
        self.assertEqual(store.commit_entries(self.root, "2026-09-30").get("saved"), 0)

    def test_改状态不许被编辑带跑(self):
        sid = store.save_entry(self.root, {"sold_at": "2026-09-30 10:00",
                                           "amount": 1, "status": "staged"})["id"]
        gid = store.save_entry(self.root, {"sold_at": "2026-09-30 11:00",
                                           "amount": 2})["id"]
        # 编辑时前端就算漏传 status，也不能把已入库的打回暂存
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00", "amount": 5,
                                     "status": "staged"}, entry_id=gid)
        rows = {r["id"]: r for r in store.list_entries(self.root, day="2026-09-30")}
        self.assertEqual(rows[gid]["status"], "saved")
        # 暂存行编辑完还是暂存
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00", "amount": 6},
                         entry_id=sid)
        self.assertEqual(rows[sid]["status"], "staged")
        rows2 = {r["id"]: r for r in store.list_entries(self.root, day="2026-09-30")}
        self.assertEqual(rows2[sid]["status"], "staged")
        self.assertEqual(rows2[sid]["amount"], 6)

    def test_品类只认清单(self):
        ok = store.save_entry(self.root, {"sold_at": "2026-09-30 10:00",
                                          "amount": 1, "category": "手机"})
        self.assertTrue(ok.get("ok"), ok)
        bad = store.save_entry(self.root, {"sold_at": "2026-09-30 10:00",
                                           "amount": 1, "category": "电视机"})
        self.assertFalse(bad.get("ok"))
        self.assertIn("品类", bad.get("why", ""))
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(rows[0]["category"], "手机")

    def test_导入生成的是暂存行(self):
        path = store.ensure(self.root)
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY,"
                     " doc_create_time TEXT, included_tax_amount REAL, remark TEXT,"
                     " consumer_guide_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT,"
                     " line_no INTEGER, sn TEXT, ean TEXT, item_name TEXT,"
                     " quantity REAL)")
        conn.execute(
            "INSERT INTO orders VALUES ('DN1','2026-09-30 14:32:00',1899,'','张三')")
        conn.execute("INSERT INTO order_lines VALUES ('DN1',1,'HXR1','6901','M',1)")
        conn.commit()
        conn.close()
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual(res.get("imported"), 1, res)
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r["status"], "staged", "导入的卡片是暂存，不直接入账")


# ────────────────────────────────────── 保存并记录 / 导出 接口
class Test入库与导出接口(_RootCase):
    def setUp(self):
        super().setUp()
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_commit接口转正当天暂存(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00", "amount": 1,
                                     "status": "staged"})
        st, d = self.srv.request("POST", "/api/cashier/commit",
                                 {"day": "2026-09-30"})
        self.assertEqual((st, d.get("saved")), (200, 1), d)

    def test_commit日期不对_400带error(self):
        st, d = self.srv.request("POST", "/api/cashier/commit", {"day": "x"})
        self.assertEqual(st, 400)
        self.assertIn("日期", d["error"])

    def test_导出接口回file_文件真是xlsx_白名单认得(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00", "amount": 100,
                                     "goods_name": "MatePad", "category": "平板"})
        st, d = self.srv.request("POST", "/api/cashier/export",
                                 {"month": "2026-09"})
        self.assertEqual(st, 200, d)
        self.assertTrue(d.get("file", "").endswith(".xlsx"), d)
        head = Path(d["path"]).read_bytes()[:2]
        self.assertEqual(head, b"PK", "导出件必须是真 xlsx（zip 头）")
        # 下载接口认得（只许 out/exports 下的一个文件名 —— 走现成白名单）
        self.assertIsNotNone(self.srv.app.export_file(d["file"]))

    def test_导出月份不对_400带error(self):
        st, d = self.srv.request("POST", "/api/cashier/export", {"month": "9月"})
        self.assertEqual(st, 400)
        self.assertTrue(d.get("error"), d)

    def test_区长平台照样403(self):
        with mock.patch.object(web, "role_scope", lambda _a: _scope("manager")):
            for path in ("/api/cashier/commit", "/api/cashier/export"):
                with self.subTest(path=path):
                    st, d = self.srv.request("POST", path, {"day": "2026-09-30",
                                                            "month": "2026-09"})
                    self.assertEqual(st, 403)


# ────────────────────────────────────── 导出内容（两 sheet，对齐参考表）
class Test导出内容(_RootCase):
    def _sheets(self, month="2026-09"):
        return exporter.sheets(self.root, month)

    def test_销售表列头逐列对齐参考表(self):
        """列序 = 用户《9月份机场销售表》表头逐列（含空列占位）。"""
        self.assertEqual(exporter.HEAD, [
            "时间", "品类", "编码", "明细", "序列号", "数量", "合计", "金额",
            "#", ".", "销售员",
            "助手", "C扫B", "POS", "现金", "公对公",
            "付以旧换新(旧)", "付以旧换新(新)", "预收款",
            "企业微信", "支付宝直连", "微信直连",
            "发票备注", "服务", "备注", "京东到家配送地址", "姓名", "电话",
            "身份证号", "住址（送货地址）", "购买机型", "金额", "发票号",
        ])

    def test_手工单一行_成本反查_毛利相减(self):
        store.save_policy(self.root, rows=[{
            "商品名称": "MatePad", "商品编码": "6901",
            "入库价": 1000, "无条件单台返利金额": 100,
            "有条件最高单台返利金额": 50}])
        store.save_entry(self.root, {
            "sold_at": "2026-09-03 10:00", "amount": 990, "quantity": 1,
            "goods_code": "6901", "goods_name": "MatePad", "category": "平板",
            "seller": "张三", "note": "老客户",
            "payments": [{"method": "现金", "amount": 990}]})
        sheets = self._sheets()
        head, rows = sheets["9月份销售表"]
        self.assertEqual(head, exporter.HEAD)
        self.assertEqual(len(rows), 1)
        r = dict(zip(head, rows[0]))
        self.assertEqual(r["时间"], "9.3")
        self.assertEqual(r["品类"], "平板")
        self.assertEqual(r["合计"], 990)
        self.assertEqual(r["金额"], 990)
        self.assertEqual(r["#"], 850, "成本 = 入库价 − 无条件 − 有条件")
        self.assertEqual(r["."], 140, "毛利 = 金额 − 成本")
        self.assertEqual(r["销售员"], "张三")
        self.assertEqual(r["现金"], 990)
        self.assertEqual(r["备注"], "老客户")
        # 没数据的列保留空占位
        self.assertIn("姓名", head)
        self.assertIsNone(r["姓名"])

    def test_成本查不到就留空(self):
        store.save_entry(self.root, {"sold_at": "2026-09-03 10:00",
                                     "amount": 100, "goods_code": "NO-POLICY"})
        _, rows = self._sheets()["9月份销售表"]
        r = dict(zip(exporter.HEAD, rows[0]))
        self.assertIsNone(r["#"])
        self.assertIsNone(r["."])

    def test_玲珑多行单拆行_合计相同_支付只落首行(self):
        path = store.ensure(self.root)
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY,"
                     " doc_create_time TEXT, included_tax_amount REAL, remark TEXT,"
                     " consumer_guide_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT,"
                     " line_no INTEGER, sn TEXT, ean TEXT, item_name TEXT,"
                     " quantity REAL, included_tax_amount REAL)")
        conn.execute("INSERT INTO orders VALUES ('DN9','2026-09-05 14:32:00',"
                     " 1000,'','张三')")
        conn.execute("INSERT INTO order_lines VALUES ('DN9',1,'S1','6901','手机A',1,800)")
        conn.execute("INSERT INTO order_lines VALUES ('DN9',2,'S2','6902','壳',1,200)")
        conn.commit()
        conn.close()
        store.entries_from_orders(self.root, "2026-09-05")
        # 给这张卡补支付（模拟人在卡里录了组合支付再入库）
        rows0 = store.list_entries(self.root, day="2026-09-05")
        store.save_entry(self.root, dict(rows0[0], amount=1000,
                                         payments=[{"method": "现金", "amount": 400},
                                                   {"method": "微信直连", "amount": 600}]),
                         entry_id=rows0[0]["id"])
        head, rows = self._sheets()["9月份销售表"]
        self.assertEqual(len(rows), 2, "一个订单两个商品 = 两行")
        a, b = dict(zip(head, rows[0])), dict(zip(head, rows[1]))
        self.assertEqual(a["合计"], 1000)
        self.assertEqual(b["合计"], 1000, "合计是订单合计，每行重复")
        self.assertEqual(a["金额"] + b["金额"], 1000, "金额是各商品自己的")
        self.assertEqual(a["编码"], "6901")
        self.assertEqual(b["明细"], "壳")
        self.assertEqual(a["现金"], 400, "支付只落首行，防止求和翻倍")
        self.assertIsNone(b["现金"])
        self.assertEqual(a["微信直连"], 600)
        self.assertEqual(a["序列号"], "S1")

    def test_政策表sheet带现行表(self):
        store.save_policy(self.root, rows=[{
            "商品名称": "MatePad", "商品编码": "6901", "入库价": 1000,
            "无条件单台返利金额": 100, "有条件最高单台返利金额": 50}])
        sheets = self._sheets()
        self.assertIn("政策表", sheets)
        head, rows = sheets["政策表"]
        self.assertIn("商品编码", head)
        self.assertNotIn("fetched_at", head, "记账列不进导出")
        self.assertNotIn("goods_code", head, "记账列不进导出（原表头是商品编码）")
        self.assertEqual(rows[0][head.index("商品编码")], "6901")

    def test_当月过滤_别的月不导(self):
        store.save_entry(self.root, {"sold_at": "2026-08-31 10:00", "amount": 1})
        store.save_entry(self.root, {"sold_at": "2026-09-01 10:00", "amount": 2})
        _, rows = self._sheets("2026-09")["9月份销售表"]
        self.assertEqual(len(rows), 1)

    def test_商品行跟卡片走_品类按行(self):
        """第三轮：导出用卡片里的商品行（人改了卡，导出跟着变），品类按行取。"""
        store.save_entry(self.root, {
            "sold_at": "2026-09-07 10:00", "amount": 1, "seller": "张三",
            "products": [
                {"name": "MatePad", "code": "6901", "sn": "S1",
                 "quantity": 1, "amount": 1899, "category": "平板"},
                {"name": "键盘", "code": "6903", "sn": "",
                 "quantity": 1, "amount": 499, "category": "配件"},
            ],
            "accessories": [{"name": "碎屏险", "amount": 199}],
        })
        head, rows = self._sheets()["9月份销售表"]
        self.assertEqual(len(rows), 3, "2 商品 + 1 配件 = 3 行")
        a, b, c = (dict(zip(head, r)) for r in rows)
        # 商品行：品类跟行、合计=订单合计（=Σ商品）、金额是各商品自己的
        self.assertEqual((a["品类"], a["金额"], a["合计"]), ("平板", 1899, 2398))
        self.assertEqual((b["品类"], b["金额"], b["合计"]), ("配件", 499, 2398))
        self.assertEqual(a["编码"], "6901")
        self.assertIsNone(b["序列号"], "空 SN 就是空单元格（跟空列占位一个口径）")
        # 配件行：品类恒「配件」、数量 1、编码/SN 空、合计=订单合计
        self.assertEqual((c["品类"], c["金额"], c["数量"]), ("配件", 199, 1))
        self.assertEqual(c["合计"], 2398)
        self.assertIsNone(c["编码"])
        self.assertIsNone(c["序列号"])
        self.assertEqual(c["明细"], "碎屏险")

    def test_商品行品类空了回退卡片品类(self):
        """行上没填品类（老口径后补的商品行）⇒ 用卡片品类兜底。"""
        import sqlite3
        store.save_entry(self.root, {
            "sold_at": "2026-09-08 10:00", "amount": 1,
            "products": [{"name": "Mate70", "code": "6901", "sn": "",
                          "quantity": 1, "amount": 5499, "category": ""}]})
        path = store.ensure(self.root)
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE sale_entries SET category='手机'")
        conn.commit()
        conn.close()
        _, rows = self._sheets()["9月份销售表"]
        r = dict(zip(exporter.HEAD, rows[0]))
        self.assertEqual(r["品类"], "手机")

    def test_月份格式不对回why(self):
        with self.assertRaises(ValueError) as cm:
            exporter.sheets(self.root, "2026/09")
        self.assertIn("月份", str(cm.exception))


# ────────────────────────────────────── 商品行（一张卡 = 一个订单，2026-09-30 第三轮）
class Test商品行(_RootCase):
    def test_迁移补products列_老行读出来是空列表(self):
        import sqlite3
        path = store.ensure(self.root)
        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sale_entries)")}
        self.assertIn("products", cols, "m009 没补 products")
        conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, created_at, updated_at)"
            " VALUES ('2026-09-30 10:00:00','c1','老货',1,99,'小张','',"
            " 'manual','2026-09-30 10:00:00','2026-09-30 10:00:00')")
        conn.commit()
        conn.close()
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r["products"], [])

    def test_存改查一条龙_每行带品类(self):
        body = {
            "sold_at": "2026-09-30 14:32", "goods_name": "MatePad", "quantity": 1,
            "amount": 1899, "seller": "张三",
            "products": [
                {"name": "MatePad", "code": "6901", "sn": "S1",
                 "quantity": 1, "amount": 1899, "category": "平板"},
                {"name": "保护壳", "code": "6902", "sn": "",
                 "quantity": 2, "amount": 198, "category": "配件"},
            ],
        }
        res = store.save_entry(self.root, body)
        self.assertTrue(res.get("ok"), res)
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(len(r["products"]), 2)
        p0, p1 = r["products"]
        self.assertEqual((p0["name"], p0["code"], p0["sn"], p0["category"]),
                         ("MatePad", "6901", "S1", "平板"))
        self.assertEqual((p1["quantity"], p1["amount"], p1["category"]),
                         (2.0, 198.0, "配件"))
        # 改第二行的金额 → 合计/件数跟着商品行走
        body.update({"id": r["id"], "amount": 9999,
                     "products": [body["products"][0],
                                  dict(body["products"][1], amount=150)]})
        self.assertTrue(store.save_entry(self.root, body, entry_id=r["id"]).get("ok"))
        r2 = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r2["amount"], 2049, "合计 = Σ商品金额（传进来的 9999 不算）")
        self.assertEqual(r2["quantity"], 3)
        self.assertEqual(r2["category"], "平板", "顶层品类镜像首行")

    def test_空商品行时金额件数还是老口径(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00",
                                     "amount": 500, "quantity": 2,
                                     "category": "手机"})
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual((r["amount"], r["quantity"], r["category"]),
                         (500.0, 2.0, "手机"))
        self.assertEqual(r["products"], [])

    def test_坏商品行回why不抛(self):
        base = {"sold_at": "2026-09-30 14:32", "amount": 100}
        cases = (
            ({"products": [{"code": "1", "amount": 5}]}, "名字"),
            ({"products": [{"name": "A", "amount": "abc"}]}, "数字"),
            ({"products": [{"name": "A", "amount": -1}]}, "负数"),
            ({"products": [{"name": "A", "amount": 1, "quantity": 0}]}, "大于 0"),
            ({"products": [{"name": "A", "amount": 1, "category": "电视机"}]},
             "品类"),
            ({"products": "不是列表"}, "列表"),
        )
        for bad, frag in cases:
            with self.subTest(frag=frag):
                res = store.save_entry(self.root, dict(base, **bad))
                self.assertFalse(res.get("ok"))
                self.assertIn(frag, res.get("why", ""))

    def test_导入把order_lines写进商品行(self):
        path = store.ensure(self.root)
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY,"
                     " doc_create_time TEXT, included_tax_amount REAL, remark TEXT,"
                     " consumer_guide_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT,"
                     " line_no INTEGER, sn TEXT, ean TEXT, item_name TEXT,"
                     " quantity REAL, included_tax_amount REAL)")
        conn.execute("INSERT INTO orders VALUES ('DN-PROD','2026-09-06 10:00:00',"
                     " 1000,'','张三')")
        conn.execute("INSERT INTO order_lines VALUES"
                     " ('DN-PROD',1,'S1','6901','手机A',1,800)")
        conn.execute("INSERT INTO order_lines VALUES"
                     " ('DN-PROD',2,'S2','6902','壳',1,200)")
        conn.commit()
        conn.close()
        res = store.entries_from_orders(self.root, "2026-09-06")
        self.assertTrue(res.get("ok"), res)
        r = store.list_entries(self.root, day="2026-09-06")[0]
        self.assertEqual(len(r["products"]), 2, "导入要按订单行铺商品行")
        self.assertEqual(r["products"][0]["name"], "手机A")
        self.assertEqual(r["products"][0]["amount"], 800.0)
        self.assertEqual(r["products"][1]["code"], "6902")
        self.assertEqual(r["amount"], 1000, "卡片合计仍是订单合计")


if __name__ == "__main__":
    unittest.main()
