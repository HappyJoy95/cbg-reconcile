# -*- coding: utf-8 -*-
"""收银界面（生活馆利润核算录入端，2026-09-29）—— 存储 / App / 接口 / 页面接线。

开发目标见 `.dsh/docs/2026-09-29-生活馆收银界面-开发目标.md`。钉的都是
"错了没人会报"的那类：

* 政策整表**覆盖**不留旧（政策是"当前版"，第二次存要能清掉第一次的）；
* 编码反查认 `基准提货价*` **带星表头**（xlsx 原样，别"顺手"洗掉）；
* 接口门禁：**区长/平台 403**（收银是门店本机操作）、块内未知子路径 **404**；
* 政策刷新的类锁：失败后必须**释放**（否则一次失败永久卡死）；
* 页面三件套（HTML 面板 / SUBTABS / loader）少一边就点不开。
"""

from __future__ import annotations

import json
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

    def test_整表覆盖不留旧(self):
        r1 = store.save_policy(self.root, self._rows("A", "B", "C"))
        self.assertEqual((r1["ok"], r1["rows"]), (True, 3))
        r2 = store.save_policy(self.root, self._rows("D"))
        self.assertEqual(r2["rows"], 1)
        self.assertIsNone(store.lookup(self.root, "A"),
                          "第二次存是**覆盖** —— 旧政策行不许残留")
        self.assertIsNotNone(store.lookup(self.root, "D"))

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
        for cid in ("cashier-sold-at", "cashier-code", "cashier-name",
                    "cashier-qty", "cashier-amount", "cashier-seller",
                    "cashier-note", "cashier-save", "cashier-day",
                    "cashier-table", "cashier-refresh"):
            with self.subTest(id=cid):
                self.assertIn('id="%s"' % cid, INDEX_HTML)

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
                ({"payments": "不是列表"}, "列表")):
            with self.subTest(frag=frag):
                res = store.save_entry(self.root, dict(base, **bad))
                self.assertFalse(res.get("ok"))
                self.assertIn(frag, res.get("why", ""))


if __name__ == "__main__":
    unittest.main()
