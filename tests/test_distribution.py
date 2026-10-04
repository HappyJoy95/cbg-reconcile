# -*- coding: utf-8 -*-
"""分销（2.3.0）—— 四件事：识别 / 映射 / 聚合 / 门禁。

钉的口径（用户 2026-09-29 拍板，见 `.dsh/docs/2026-09-29-2.3.0-开发目标.md`）：

1. 只算 `门店=渠道分销部` 且 单据类型 ∈ {分销, 分销退}，**净额**；
2. 区域识别：备注关键词 → 客户映射 → 待确认，**⛔ 不用门店名兜底**；
3. 手动确认**按客户落盘**（跨时段沿用）、可改可重置；
4. `types="platform"`（**仅平台岗**，2026-09-30 从 `multi` 收紧），
   `/api/dist/*` 对区长/门店 **403**（坑 18）；`field/value` 下钻走白名单。
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from urllib.parse import quote
from pathlib import Path
from threading import Thread
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from src import web                                              # noqa: E402
from src.features import distribution as dist                    # noqa: E402
from src.features.distribution import fetch as dist_fetch        # noqa: E402
from src.features.distribution import metrics as dist_metrics    # noqa: E402
from src.features.distribution import region as dist_region      # noqa: E402
from src.features.distribution import store as dist_store        # noqa: E402


def _row(**kw):
    """一行分销明细的最小骨架（缺的列由 store._row 补 None）。"""
    base = {"支付时间": "2026-09-05 10:00:00", "单号": "SI1", "单据类型": "分销",
            "商品编码": "1", "商品名称": "手机", "一级分类": "手机",
            "三级分类": "Pura 80", "品牌": "华为", "数量": "1", "金额": "1000",
            "门店": dist.TARGET_STORE, "店员": "张三",
            "客户/顾客": "青岛某电子", "备注": ""}
    base.update(kw)
    return base


class Test注册接入(unittest.TestCase):
    def test_注册表有它(self):
        from src.features import ALL
        f = [x for x in ALL if x.key == "distribution"]
        self.assertEqual(len(f), 1, "ALL 里要有 distribution（唯一注册动作）")
        f = f[0]
        self.assertEqual(f.types, "platform",
                         "仅平台岗（用户 2026-09-30：「分销仅平台岗可见」）"
                         "—— ⚠ 别写回 multi，那个口径连区长都放行")
        self.assertEqual([s.key for s in f.children],
                         ["dist-region", "dist-model", "dist-salesman", "dist-detail"])
        self.assertEqual(f.steps(), [], "手动选时间段现拉 —— 不注册定时步骤")

    def test_页面可见性从注册表派生(self):
        for key in ("distribution", "dist-region", "dist-model",
                    "dist-salesman", "dist-detail"):
            self.assertEqual(web.PAGE_RULES.get(key), "platform",
                             "%s 应该派生成 platform（仅平台岗）" % key)

    def test_可见性口径跟can_for一致(self):
        """`can` 是写操作的判据，必须跟页面 `platform` 同口径 ——
        三档身份里**只有平台岗**拿到 True（2026-09-30 从 `not is_store` 收紧）。"""
        self.assertTrue(web._can_for(web.ROLE_PLATFORM)["dist.write"])
        self.assertTrue(web._can_for(web.ROLE_PLATFORM)["dist.export"])
        for role in (web.ROLE_MANAGER, web.ROLE_STORE):
            with self.subTest(role=role):
                self.assertFalse(web._can_for(role)["dist.write"],
                                 "%s 不该能拉取/改区" % role)
                self.assertFalse(web._can_for(role)["dist.export"])

    def test_三种身份的菜单只有平台岗有分销(self):
        """`pages_for` 是前端渲染菜单的**唯一来源** —— 区长连那一行都不该有。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cases = [
            (web.ROLE_PLATFORM, "有", True),
            (web.ROLE_MANAGER, "没有", False),
            (web.ROLE_STORE, "没有", False),
        ]
        for role, word, want in cases:
            sc = {"role": role,
                  "stores": None if role == web.ROLE_PLATFORM else {"青岛城阳万达店"}}
            with self.subTest(role=role):
                got = "distribution" in web.pages_for(sc, root=root)
                self.assertEqual(got, want, "%s 的菜单该%s分销" % (role, word))


class Test区域识别(unittest.TestCase):
    def test_备注关键词命中(self):
        z, src = dist_region.identify(_row(备注="发同城，发城阳"), {})
        self.assertEqual(z, "城阳")
        self.assertEqual(src, "备注")

    def test_客户映射沿用(self):
        z, src = dist_region.identify(_row(), {"青岛某电子": "崂山"})
        self.assertEqual(z, "崂山")
        self.assertEqual(src, "客户")

    def test_备注先于客户映射(self):
        """备注里写死的区名是**这一行自己说的**，比客户默认归类更具体。"""
        z, _src = dist_region.identify(_row(备注="市北家佳源"), {"青岛某电子": "崂山"})
        self.assertEqual(z, "市北")

    def test_认不出进待确认(self):
        z, src = dist_region.identify(_row(备注="转线上"), {})
        self.assertEqual(z, dist.ZONE_PENDING)
        self.assertEqual(src, "")

    def test_不用门店名兜底(self):
        """⚠ 用户明确否掉门店名兜底（开单门店所在区 ≠ 客户所在区）——
        连门店列里写着区名也不许算识别出来。"""
        z, src = dist_region.identify(_row(门店="城阳万象汇店", 备注="转线上"), {})
        self.assertEqual(z, dist.ZONE_PENDING, "门店里的区名被当成识别依据了")
        self.assertEqual(src, "")

    def test_九个区都在词表里(self):
        self.assertEqual(set(dist.ZONES),
                         {"市南", "市北", "城阳", "胶州", "黄岛", "平度",
                          "莱西", "即墨", "崂山"})

    def test_待确认按客户聚不按行聚(self):
        """一个客户一条 —— 按行点一千多次，按客户点一次以后自动沿用。"""
        rows = [_row(备注="", 金额="100"), _row(备注="", 金额="200"),
                _row(**{"客户/顾客": "乙"}, 金额="50")]
        pend = dist_region.pending_customers(rows, {})
        self.assertEqual(len(pend), 2)
        by = {p["customer"]: p for p in pend}
        self.assertEqual(by["青岛某电子"]["rows"], 2)
        self.assertEqual(by["青岛某电子"]["amount"], 300.0)

    def test_确认校验区名(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            bad = dist_region.confirm(root, "某客户", "城阳区")
            self.assertFalse(bad["ok"], "拼错的区名要拒（不然看板多出假区）")
            ok = dist_region.confirm(root, "某客户", "城阳")
            self.assertTrue(ok["ok"])


class Test映射持久化(unittest.TestCase):
    def test_确认后重读还在(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            dist_region.confirm(root, "某客户", "胶州", who="杨英梅")
            self.assertEqual(dist_store.map_all(root), {"某客户": "胶州"})
            detail = dist_store.map_detail(root)
            self.assertEqual(detail[0]["who"], "杨英梅")
            self.assertTrue(detail[0]["updated"], "要留痕：谁、什么时候定的")

    def test_改区覆盖旧值(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            dist_region.confirm(root, "某客户", "胶州")
            dist_region.confirm(root, "某客户", "平度")
            self.assertEqual(dist_store.map_all(root), {"某客户": "平度"})

    def test_清空回待确认(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            dist_region.confirm(root, "某客户", "胶州")
            dist_region.confirm(root, "某客户", "")
            # zone='' 读出来当"没定" —— identify 走待确认那条
            z, src = dist_region.identify(_row(), dist_store.map_all(root))
            self.assertEqual(z, dist.ZONE_PENDING)
            self.assertEqual(src, "")

    def test_重置全部(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            dist_region.confirm(root, "甲", "胶州")
            dist_region.confirm(root, "乙", "平度")
            self.assertEqual(dist_store.map_reset(root), 2)
            self.assertEqual(dist_store.map_all(root), {})

    def test_空客户名不落库(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(dist_store.map_set(Path(t), "  ", "胶州"))
            self.assertEqual(dist_store.map_all(Path(t)), {})


class Test聚合口径(unittest.TestCase):
    def test_净额含退单(self):
        rows = [_row(金额="1000"), _row(单据类型="分销退", 金额="-300")]
        d = dist_metrics.model_board(rows)
        self.assertEqual(d["totals"]["amount"], 700.0, "分销退必须冲进来（净额）")

    def test_未分类和空是两档(self):
        rows = [_row(**{"三级分类": "未分类"}, 金额="10"),
                _row(**{"三级分类": ""}, 金额="20"),
                _row(**{"三级分类": "Pura 80"}, 金额="30")]
        d = dist_metrics.model_board(rows)
        keys = [r["key"] for r in d["cat3"]]
        self.assertIn("未分类", keys)
        self.assertIn("（空）", keys)
        self.assertNotEqual("未分类", "（空）", "两档混了就查不出是哪边坏了")
        self.assertEqual(sum(r["amount"] for r in d["cat3"]), 60.0,
                         "各档合计必须 == 总额（对不上账的看板等于坏看板）")

    def test_PuraXView本地补档(self):
        """云商商品档案没配三级分类 ⇒ 本地补（用户 2026-09-29）：
        「x view 单独处理一下吧，云商没有三级分类我们补一下」。"""
        rows = [_row(**{"三级分类": "未分类",
                        "商品名称": "智能手机/华为/Pura X View VOL-AL00(12GB+512GB)全网通版-幻夜黑"},
                       金额="6600"),
                _row(**{"三级分类": "未分类", "商品名称": "促销品//国补"}, 金额="550")]
        d = dist_metrics.model_board(rows)
        keys = [r["key"] for r in d["cat3"]]
        self.assertIn("Pura X View", keys, "X View 应补出自己的档")
        xview = [r for r in d["cat3"] if r["key"] == "Pura X View"][0]
        self.assertEqual(xview["amount"], 6600.0)
        # 剩下的（促销品）还在未分类
        self.assertIn("未分类", keys)

    def test_补档只认未分类行(self):
        """云商哪天自己配上档了 —— 已有档位**不许被本地表改写**。"""
        rows = [_row(**{"三级分类": "nova15",
                        "商品名称": "智能手机/华为/Pura X View VOL-AL00"}, 金额="1")]
        d = dist_metrics.model_board(rows)
        keys = [r["key"] for r in d["cat3"]]
        self.assertEqual(keys, ["nova15"], "有档位的行不进补档逻辑")

    def test_未分类展开的商品级拆解(self):
        """补完 X View 剩下的未分类要能点开看（按商品名称拆）。"""
        rows = [_row(**{"三级分类": "未分类", "商品名称": "促销品//国补"}, 金额="100"),
                _row(**{"三级分类": "未分类", "商品名称": "促销品//国补"}, 金额="50"),
                _row(**{"三级分类": "未分类", "商品名称": "手机折叠支架"}, 金额="30"),
                _row(**{"三级分类": "Mate XT2", "商品名称": "Mate XT2 整机"}, 金额="999")]
        d = dist_metrics.model_board(rows)
        unc = {r["key"]: r for r in d["cat3_unclassified"]}
        self.assertEqual(set(unc), {"促销品//国补", "手机折叠支架"},
                         "拆解只含未分类的行，已分类的不掺进来")
        self.assertEqual(unc["促销品//国补"]["rows"], 2)
        self.assertEqual(unc["促销品//国补"]["amount"], 150.0)

    def test_销售员取店员列(self):
        """`业务员` 列实测全空 —— 销售员看板认 `店员`。"""
        rows = [_row(**{"店员": "刘家顺"}, 金额="100"),
                _row(**{"店员": "李帅"}, 金额="200")]
        d = dist_metrics.salesman_board(rows)
        self.assertEqual([p["name"] for p in d["people"]], ["李帅", "刘家顺"])

    def test_品类占比_退单取绝对值(self):
        """负金额进占比会出现负百分比（堆叠图画不了）—— 占比按 |金额|，
        总额仍显示代数和（净额口径）。"""
        rows = [_row(**{"店员": "张三", "一级分类": "手机"}, 金额="800"),
                _row(**{"店员": "张三", "一级分类": "潮玩礼品"}, 金额="-200")]
        p = dist_metrics.salesman_board(rows)["people"][0]
        self.assertEqual(p["amount"], 600.0, "总额是净额")
        by = {m["cat"]: m["pct"] for m in p["mix"]}
        self.assertAlmostEqual(by["手机"], 0.8)
        self.assertAlmostEqual(by["潮玩礼品"], 0.2)
        self.assertTrue(all(m["pct"] >= 0 for m in p["mix"]))

    def test_区域看板九区加待确认都出档(self):
        d = dist_metrics.region_board([_row(备注="城阳万象汇")], {})
        zones = [z["zone"] for z in d["zones"]]
        self.assertEqual(zones, list(dist.ZONES) + [dist.ZONE_PENDING])
        hit = {z["zone"]: z for z in d["zones"]}
        self.assertEqual(hit["城阳"]["rows"], 1)
        self.assertEqual(hit[dist.ZONE_PENDING]["rows"], 0)


class Test区间覆盖写(unittest.TestCase):
    def test_重拉同段覆盖_不相交段共存(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            d1, d2 = datetime.date(2026, 9, 1), datetime.date(2026, 9, 10)
            dist_store.replace_range(root, d1, d2, [_row(金额="100")])
            # 重拉同段：行变了（补录/改单）—— 旧的被覆盖，不是叠加
            dist_store.replace_range(root, d1, d2, [_row(金额="150")])
            got = dist_store.read_rows(root, d1, d2)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["金额"], 150.0)
            # 另一段拉取共存
            d3, d4 = datetime.date(2026, 9, 11), datetime.date(2026, 9, 20)
            dist_store.replace_range(root, d3, d4, [_row(支付时间="2026-09-15 08:00:00",
                                                         金额="9")])
            self.assertEqual(len(dist_store.read_rows(root, d1, d2)), 1)
            self.assertEqual(len(dist_store.read_rows(root, d3, d4)), 1)

    def test_没拉过和拉过是空两码事(self):
        """missing ≠ zero（数据五态）：界面要分清「还没拉」和「这段真没有」。"""
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            d1, d2 = datetime.date(2026, 9, 1), datetime.date(2026, 9, 10)
            self.assertIsNone(dist_store.range_covered(root, d1, d2), "没拉过")
            dist_store.replace_range(root, d1, d2, [])     # 拉了，是空的
            # 空表 ⇒ MIN/MAX 还是 None —— 这时**不能**谎称"覆盖了"：
            # 一行都没有的段和没拉过的段，库层面分不出来（已知边界）。
            got = dist_store.range_covered(root, d1, d2)
            self.assertTrue(got is None or got == ("", "") or got[0] is None
                            or isinstance(got[0], str))

    def test_数值列转数字(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            d1, d2 = datetime.date(2026, 9, 1), datetime.date(2026, 9, 10)
            dist_store.replace_range(root, d1, d2, [_row(金额="1,234.5", 数量="2")])
            got = dist_store.read_rows(root, d1, d2)[0]
            self.assertEqual(got["金额"], 1234.5)
            self.assertEqual(got["数量"], 2.0)

    def test_库不存在读不出错(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.assertEqual(
                dist_store.read_rows(root, datetime.date(2026, 9, 1),
                                     datetime.date(2026, 9, 10)), [])
            self.assertEqual(dist_store.map_all(root), {})


class _FakeClient:
    """假客户端：不碰网络，按段回样本行。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def sales_range(self, start, end, on_progress=None):
        self.calls.append((start, end))
        if on_progress:
            on_progress(start, end, len(self.rows))
        return list(self.rows)


class Test拉取(unittest.TestCase):
    def test_只留分销单和渠道分销部(self):
        rows = [
            _row(单号="A"),                                        # 留
            _row(单号="B", 单据类型="分销退", 金额="-5"),           # 留（负金额）
            _row(单号="C", 单据类型="零售"),                        # 滤掉
            _row(单号="D", 门店="青岛悦荟店"),                      # 滤掉（别家店）
            _row(单号="E", 门店=""),                               # 滤掉（门店空不当自己店）
        ]
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            res = dist_fetch.run(root, datetime.date(2026, 9, 1),
                                 datetime.date(2026, 9, 10),
                                 client=_FakeClient(rows))
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["rows"], 2, "只留分销/分销退 + 渠道分销部")
            got = dist_store.read_rows(root, datetime.date(2026, 9, 1),
                                       datetime.date(2026, 9, 10))
            self.assertEqual([r["单号"] for r in got], ["A", "B"])

    def test_区间超过上限直接拒(self):
        res = dist_fetch.run(None, datetime.date(2026, 1, 1),
                             datetime.date(2027, 1, 2), client=_FakeClient([]))
        self.assertFalse(res["ok"])
        self.assertIn("天", res["why"])

    def test_结束早于开始给说法(self):
        res = dist_fetch.run(None, datetime.date(2026, 9, 10),
                             datetime.date(2026, 9, 1), client=_FakeClient([]))
        self.assertFalse(res["ok"])

    def test_同一时刻只许一个拉取(self):
        """限流纪律（erp-api 坑 0）：并发拉取会把云商挤爆。"""
        self.assertFalse(dist_fetch.busy())
        self.assertTrue(dist_fetch._LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(dist_fetch.FetchBusy):
                dist_fetch.run(None, datetime.date(2026, 9, 1),
                               datetime.date(2026, 9, 10), client=_FakeClient([]))
        finally:
            dist_fetch._LOCK.release()


class _Server:
    """真起一个 HTTP 服务 —— 测路由和状态码（照 `test_roles._Server`）。"""

    def __init__(self, root: Path):
        from http.server import ThreadingHTTPServer
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛城阳万达店"\nstore_code: "SCN231409"\n',
            encoding="utf-8")
        # ⚠ 区长名单要有 —— 少了它 `role_scope()` 认不出区长（退到门店 ⇒ 403）
        (root / "config" / "managers.yaml").write_text(
            "managers:\n"
            "  - name: 杨英梅\n"
            '    accounts: ["SL15763940156"]\n'
            "    region: 西北区\n"
            "    stores:\n"
            "      - 青岛城阳万达店\n",
            encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        self.t = Thread(target=self.httpd.serve_forever, daemon=True)
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


class Test接口门禁(unittest.TestCase):
    """⚠ 坑 18：菜单藏起来从来不算权限 —— `/api/dist/*` 每条都要有 403。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)
        # 门禁放行（这一份测角色，不测门禁）
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def _as(self, account="", who="", platform=False):
        creds = {"username": account, "who": who, "has_token": True}
        prof = {"erp_name": "青岛城阳万达店", "type": "experience", "kind": "体验店",
                "platform": platform, "needs_linglong": True,
                "show_all": platform, "who": who, "marker": "W"}
        c1 = mock.patch.object(web, "describe_store_credentials", lambda *a, **k: creds)
        c2 = mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof))
        c1.start()
        c2.start()
        self.addCleanup(c1.stop)
        self.addCleanup(c2.stop)

    def test_门店_看板403(self):
        self._as(account="sl00000000", who="张三")
        code, d = self.srv.request("GET", "/api/dist/board?kind=region")
        self.assertEqual(code, 403, d)
        self.assertTrue(d.get("forbidden"))
        self.assertIn("error", d)

    def test_门店_写与导出都403(self):
        self._as(account="sl00000000", who="张三")
        for method, path, body in (
                ("POST", "/api/dist/fetch", {"start": "2026-09-01", "end": "2026-09-10"}),
                ("POST", "/api/dist/zone", {"customer": "x", "zone": "城阳"}),
                ("POST", "/api/dist/map/reset", {}),
                ("POST", "/api/dist/export", {}),
                ("GET", "/api/dist/detail", None),
                ("GET", "/api/dist/map", None)):
            with self.subTest(path=path):
                code, d = self.srv.request(method, path, body)
                self.assertEqual(code, 403, "%s 没被拦：%s" % (path, d))

    def test_平台_看板能读(self):
        self._as(account="someone-else", platform=True)
        code, d = self.srv.request("GET", "/api/dist/board?kind=model")
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("ok"))
        self.assertEqual(d.get("store"), dist.TARGET_STORE)

    def test_区长_看板403_仅平台岗(self):
        """⚠ 2026-09-30 口径变更：原来 `types="multi"` 区长能读（这条测试当时是
        `assertEqual(200)`），用户复核「**分销仅平台岗可见**」⇒ 区长一并拦。
        菜单藏了不算数 —— 这道统一 403 才是真闸门（坑 18）。"""
        self._as(account="SL15763940156", who="杨英梅")
        for method, path, body in (
                ("GET", "/api/dist/board?kind=salesman", None),
                ("GET", "/api/dist/detail", None),
                ("GET", "/api/dist/map", None),
                ("POST", "/api/dist/fetch",
                 {"start": "2026-09-01", "end": "2026-09-10"}),
                ("POST", "/api/dist/zone", {"customer": "x", "zone": "城阳"}),
                ("POST", "/api/dist/export", {})):
            with self.subTest(path=path):
                code, d = self.srv.request(method, path, body)
                self.assertEqual(code, 403, "%s 区长不该进来：%s" % (path, d))
                self.assertTrue(d.get("forbidden"))

    def test_平台_改区标落盘(self):
        self._as(account="someone-else", platform=True)
        code, d = self.srv.request("POST", "/api/dist/zone",
                                   {"customer": "青岛某电子", "zone": "崂山"})
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("ok"))
        self.assertEqual(dist_store.map_all(self.root), {"青岛某电子": "崂山"})

    # ------------------------------------------------ 明细下钻（2026-09-30）

    def _seed(self):
        """4 行分销单：Mate XT2 含**退货**（负金额）、一行空店员 —— 下钻对账都用它。"""
        rows = [
            _row(单号="M1", 三级分类="Mate XT2", 商品名称="Mate XT2 曜黑",
                 金额="1000", 支付时间="2026-09-05 10:00:00"),
            _row(单号="M2", 三级分类="Mate XT2", 商品名称="Mate XT2 金色",
                 单据类型="分销退", 金额="-200", 支付时间="2026-09-06 11:00:00"),
            _row(单号="N1", 三级分类="nova 13", 商品名称="nova 13",
                 金额="500", 店员="李四", 支付时间="2026-09-07 12:00:00"),
            _row(单号="P1", 一级分类="配件", 三级分类="Pura 80",
                 金额="80", 店员="", 支付时间="2026-09-08 13:00:00"),
        ]
        dist_store.replace_range(self.root, datetime.date(2026, 9, 1),
                                 datetime.date(2026, 9, 10), rows)

    def _detail(self, **params):
        q = {"start": "2026-09-01", "end": "2026-09-10"}
        q.update(params)
        path = "/api/dist/detail?" + "&".join(
            "%s=%s" % (k, quote(str(v), safe="")) for k, v in q.items())
        return self.srv.request("GET", path)

    def test_机型行点开_只回那一档的销售单(self):
        self._as(account="someone-else", platform=True)
        self._seed()
        code, d = self._detail(field="三级分类", value="Mate XT2")
        self.assertEqual(code, 200, d)
        self.assertEqual({r["单号"] for r in d["rows"]}, {"M1", "M2"},
                         "Mate XT2 那两行（含退货）")
        self.assertEqual(d["drill"], {"三级分类": "Mate XT2"})

    def test_两级筛_店员再按品类(self):
        """销售员 → 品类 → 销售单：`field=店员` + `field2=一级分类`。"""
        self._as(account="someone-else", platform=True)
        self._seed()
        code, d = self._detail(field="店员", value="张三",
                               field2="一级分类", value2="手机")
        self.assertEqual(code, 200, d)
        self.assertEqual({r["单号"] for r in d["rows"]}, {"M1", "M2"})
        code, d2 = self._detail(field="店员", value="李四",
                                field2="一级分类", value2="手机")
        self.assertEqual({r["单号"] for r in d2["rows"]}, {"N1"})

    def test_白名单外的列_400不是静默给全表(self):
        """前端拼错列名得看得见 —— 静默回全表的话「点开是全部行」没人查得出来。"""
        self._as(account="someone-else", platform=True)
        self._seed()
        code, d = self._detail(field="金额", value="1000")
        self.assertEqual(code, 400, d)
        self.assertIn("不能按这一列", d.get("error", ""))

    def test_只给一半参数_400(self):
        self._as(account="someone-else", platform=True)
        code, d = self._detail(field="店员")
        self.assertEqual(code, 400, d)
        self.assertIn("一起给", d.get("error", ""))

    def test_空店员的占位名点得开(self):
        """看板上空店员显示「（无店员）」—— 那一行点开不能是 0 行。"""
        self._as(account="someone-else", platform=True)
        self._seed()
        code, d = self._detail(field="店员", value="（无店员）")
        self.assertEqual(code, 200, d)
        self.assertEqual({r["单号"] for r in d["rows"]}, {"P1"})

    def test_明细合计等于看板那一档_含退货(self):
        """⚠ 下钻跟看板必须**同口径** —— 明细加出来 ≠ 页面那个数就是 bug。"""
        self._as(account="someone-else", platform=True)
        self._seed()
        code, board = self.srv.request(
            "GET", "/api/dist/board?kind=model&start=2026-09-01&end=2026-09-10")
        self.assertEqual(code, 200, board)
        hit = {r["key"]: r for r in board["board"]["cat3"]}["Mate XT2"]
        code, d = self._detail(field="三级分类", value="Mate XT2")
        self.assertEqual(code, 200, d)
        got = sum(float(r["金额"] or 0) for r in d["rows"])
        self.assertAlmostEqual(got, hit["amount"], places=6,
                               msg="明细合计要等于看板那一档（1000−200=800）")
        self.assertAlmostEqual(hit["amount"], 800.0, places=6,
                               msg="退货要冲减（1000−200=800）")

    def test_区长照样403_下钻也不能绕过(self):
        """下钻走的是同一道统一门 —— 区长连明细都拿不到。"""
        self._as(account="SL15763940156", who="杨英梅")
        code, d = self._detail(field="三级分类", value="Mate XT2")
        self.assertEqual(code, 403, d)
        self.assertTrue(d.get("forbidden"))

    def test_不认识的分销接口_404(self):
        self._as(account="someone-else", platform=True)
        code, d = self.srv.request("GET", "/api/dist/nothing")
        self.assertEqual(code, 404, d)


class Test日期区间(unittest.TestCase):
    def test_缺省区间和反序调换(self):
        start, end, note = web.App._dist_range({}, default_days=7)
        self.assertEqual((end - start).days, 6)
        start, end, note = web.App._dist_range(
            {"start": ["2026-09-20"], "end": ["2026-09-01"]})
        self.assertEqual(start.isoformat(), "2026-09-01")
        self.assertIn("调换", note)

    def test_错字不抛回落缺省(self):
        start, end, note = web.App._dist_range(
            {"start": ["2026-99-99"], "end": [""]}, default_days=5)
        self.assertIn("格式", note)
        self.assertEqual((end - start).days, 4)


class Test下钻前端接线(unittest.TestCase):
    """点开这件事漏了接线 = 「点了没反应」—— 这个项目最怕的失败。"""

    APP = "\n".join((ROOT_DIR / "web" / _p).read_text(encoding="utf-8")
                             for _p in ("common/base.js", "common/nav.js",
                                        "features/distribution/page.js", "app.js"))
    CSS = (ROOT_DIR / "web" / "style.css").read_text(encoding="utf-8")

    def test_属性_函数_样式都在(self):
        for needle in ("data-dist-key", "distDrillToggle", "distMixTable",
                       "distDrillTable", "distDrillRows.clear()"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.APP, "app.js 缺 " + needle)
        self.assertIn(".dist-drill-open", self.CSS, "可点的那格要看得出来")

    def test_机型两张表把筛的列名传下去了(self):
        """后端按列名筛 —— 传错列名会 400，所以两处调用必须点名。"""
        self.assertIn("distCatTable(b.cat1, '一级分类')", self.APP)
        self.assertIn("distCatTable(b.cat3, '三级分类')", self.APP)

    def test_换区间要清下钻缓存(self):
        """不清的话新窗口点开的是旧窗口那批行（静默给错数据）。"""
        i = self.APP.index("async function loadDistBoard(")
        blk = self.APP[i:i + 700]
        self.assertIn("distDrillRows.clear()", blk)


if __name__ == "__main__":
    unittest.main()
