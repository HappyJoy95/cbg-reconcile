# -*- coding: utf-8 -*-
"""权益领取（小工具）—— 活动目录 / 匹配口径 / 状态落盘。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.features import registry
from src.features.tools.claim.activities import catalog
from src.features.tools.claim.pending import metric, status


class Test活动目录(unittest.TestCase):
    def test_内置默认有活动(self):
        self.assertTrue(catalog.DEFAULT_ACTIVITIES)
        ids = [a["id"] for a in catalog.DEFAULT_ACTIVITIES]
        self.assertEqual(len(ids), len(set(ids)), "活动 id 必须唯一")
        for a in catalog.DEFAULT_ACTIVITIES:
            self.assertTrue(a.get("match"), a.get("id"))
            self.assertTrue(a.get("title") or a.get("benefit"))

    def test_缺配置时用内置(self):
        with tempfile.TemporaryDirectory() as td:
            acts = catalog.load_activities(td)
            self.assertEqual(len(acts), len(catalog.DEFAULT_ACTIVITIES))

    def test_配置覆盖内置(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config"
            cfg.mkdir()
            (cfg / "benefit-claim.yaml").write_text(
                "activities:\n"
                "  - id: only-one\n"
                "    category: 手机\n"
                "    title: 唯一活动\n"
                "    match: [TEST-MODEL]\n"
                "    start: 2026-09-01\n"
                "    end: 2026-09-30\n"
                "    benefit: Care+\n",
                encoding="utf-8")
            acts = catalog.load_activities(td)
            self.assertEqual(len(acts), 1)
            self.assertEqual(acts[0]["id"], "only-one")

    def test_日期窗口含首尾且可不限(self):
        act = {"start": "2026-09-01", "end": "2026-09-30"}
        self.assertTrue(catalog.in_window(act, "2026-09-01"))
        self.assertTrue(catalog.in_window(act, "2026-09-30"))
        self.assertFalse(catalog.in_window(act, "2026-08-31"))
        self.assertFalse(catalog.in_window(act, "2026-10-01"))
        open_act = {"start": None, "end": None}
        self.assertTrue(catalog.in_window(open_act, "2026-01-01"))

    def test_机型子串大小写不敏感_长的优先命中(self):
        act = {"match": ["nova 16 Pro"]}
        self.assertTrue(catalog.matches_model(act, "HUAWEI nova 16 Pro 5G"))
        self.assertFalse(catalog.matches_model(act, "HUAWEI nova 16 SE"))
        # nova 16 不应误吃 nova 16 SE 当「仅 nova 16」—— 由 match 列表顺序/更长串保证
        act2 = {"match": ["nova 16 SE", "nova 16 Pro", "nova 16"]}
        self.assertTrue(catalog.matches_model(act2, "nova 16 SE"))


class Test匹配与状态(unittest.TestCase):
    def test_退货不进待领(self):
        self.assertTrue(metric.is_return("零售退"))
        self.assertTrue(metric.is_return("退货"))
        self.assertFalse(metric.is_return("零售"))

    def test_状态键优先串号(self):
        r1 = {"sn": "ABC123", "store": "店", "doc": "D1", "name": "x", "ts": "2026-09-01"}
        self.assertEqual(metric.status_key(r1), "sn:ABC123")
        r2 = {"sn": "nosn:1", "store": "店", "doc": "D1", "name": "x", "ts": "2026-09-01"}
        k = metric.status_key(r2)
        self.assertTrue(k.startswith("row:"))
        self.assertEqual(metric.status_key(r2), k, "同单必须稳定")

    def test_join_status_无记录默认待领(self):
        rows = [{"sn": "A", "store": "s", "doc": "d", "name": "n", "ts": "t"}]
        out = metric.join_status(rows, {})
        self.assertEqual(out[0]["status"], "pending")
        self.assertEqual(out[0]["status_label"], "待领")

    def test_summarize_重算合计(self):
        rows = [
            {"status": "pending"},
            {"status": "claimed"},
            {"status": "claimed"},
            {"status": "na"},
        ]
        s = metric.summarize(rows)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["claimed"], 2)
        self.assertEqual(s["na"], 1)
        self.assertAlmostEqual(s["claimed_rate"], 0.5)

    def test_match_activities_机型加日期(self):
        acts = [{
            "id": "a1", "match": ["Mate XT"],
            "start": "2026-09-01", "end": "2026-09-30",
        }]
        self.assertTrue(metric.match_activities("HUAWEI Mate XT", "2026-09-10", acts))
        self.assertFalse(metric.match_activities("HUAWEI Mate XT", "2026-08-10", acts))
        self.assertFalse(metric.match_activities("Mate X", "2026-09-10", acts))


class Test状态落盘(unittest.TestCase):
    def test_写入并读回(self):
        with tempfile.TemporaryDirectory() as td:
            r = status.set_status(td, "sn:ABC", "claimed", by="张三", note="客户已领")
            self.assertTrue(r["ok"], r)
            data = status.load(td)
            self.assertEqual(data["sn:ABC"]["status"], "claimed")
            self.assertEqual(data["sn:ABC"]["by"], "张三")
            # 文件在 out/ 下
            p = Path(td) / "out" / "claim-status.json"
            self.assertTrue(p.exists())
            json.loads(p.read_text(encoding="utf-8"))

    def test_非法状态被拒(self):
        with tempfile.TemporaryDirectory() as td:
            r = status.set_status(td, "k", "banana")
            self.assertFalse(r["ok"])
            self.assertIn("状态只许", r["why"])

    def test_清空(self):
        with tempfile.TemporaryDirectory() as td:
            status.set_status(td, "k", "claimed")
            self.assertTrue(status.clear_all(td)["ok"])
            self.assertEqual(status.load(td), {})


class Test整机品类与典藏版(unittest.TestCase):
    def test_手提袋不进待领(self):
        name = "促销品//定制-Pura X专属礼品-手提袋-2025年"
        self.assertFalse(metric.is_device_row("潮玩礼品", "促销品", name))
        hits = metric.match_activities(
            name, "2026-09-10", catalog.DEFAULT_ACTIVITIES,
            c1="潮玩礼品", c2="促销品")
        self.assertEqual(hits, [])

    def test_普通FreeClip2不匹配典藏活动(self):
        name = "耳机麦克/华为/无线耳机/FreeClip 2 耳夹耳机 T0027-摩登黑"
        hits = metric.match_activities(
            name, "2026-09-10", catalog.DEFAULT_ACTIVITIES,
            c1="音频产品", c2="耳机")
        self.assertFalse(any(h["id"] == "freeclip2-lost-202609" for h in hits))

    def test_典藏版匹配(self):
        name = "耳机麦克/华为/无线耳机/FreeClip 2 典藏版 耳夹耳机 T0027-星海蓝"
        hits = metric.match_activities(
            name, "2026-09-10", catalog.DEFAULT_ACTIVITIES,
            c1="音频产品", c2="耳机")
        self.assertTrue(any(h["id"] == "freeclip2-lost-202609" for h in hits))

    def test_延保单不是整机(self):
        self.assertFalse(metric.is_device_row(
            "手机平板周边", "延保服务",
            "延保服务/华为/无线耳机/FreeClip 2 Care+12月"))


class Test排除PuraXView(unittest.TestCase):
    def test_折叠屏活动不含View(self):
        act = catalog.activity_by_id("care-fold-202609")
        self.assertFalse(catalog.matches_model(
            act,
            "智能手机/华为/Pura X View VOL-AL00(12GB+1TB)全网通版-亚麻灰"))
        self.assertFalse(catalog.matches_model(
            act, "智能手机/华为/Pura X VDE-AL00(16GB+512GB)全网通版-型格紫"))
        # 真·Pura X / Max 仍要命中
        self.assertTrue(catalog.matches_model(
            act, "智能手机/华为/Pura X/内屏6.3英寸 外屏3.5英寸 12GB+512GB"))
        self.assertTrue(catalog.matches_model(
            act, "智能手机/华为/Pura X Max HOP-AL00(12GB+512GB)全网通版"))
        self.assertTrue(catalog.matches_model(
            act, "智能手机/华为/Pura X 典藏版/内屏6.3英寸..."))


class Test排除FreeBuds7i(unittest.TestCase):
    """2.2.1 bug #2（用户 2026-09-26）：「领取权益时 FreeBuds 7i 会混在
    FreeBuds 7 里面，这个权益应该只有 buds7，没有 7i」。

    和 `Pura X` 吸 `Pura X View` 是同一类坑：`match` 是**子串**匹配，
    `FreeBuds 7i 耳机 T0025-…`（真实商品名，out/cbg-2026.db 里实测有）
    含子串 `FreeBuds 7` ⇒ 被吸进丢失无忧活动。活动级 `exclude` 挡。
    """

    def test_7i不进buds7丢失无忧(self):
        act = catalog.activity_by_id("freebuds7-lost-202609")
        for name in (
            "耳机麦克/华为/无线耳机/FreeBuds 7i 耳机 T0025-深空灰",
            "耳机麦克/华为/无线耳机/FreeBuds 7i 耳机 T0025-贝母白-JC",
            "耳机麦克/华为/无线耳机/FreeBuds 7i 耳机 T0025-演示机",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    catalog.matches_model(act, name),
                    "FreeBuds 7i 不是本活动机型（只有 buds7）")
                self.assertEqual(
                    metric.match_activities(
                        name, "2026-09-10", catalog.DEFAULT_ACTIVITIES,
                        c1="音频产品", c2="耳机"),
                    [], "7i 不该进待领清单")

    def test_真FreeBuds7仍命中(self):
        act = catalog.activity_by_id("freebuds7-lost-202609")
        for name in (
            "耳机麦克/华为/无线耳机/FreeBuds 7 T0028-星空黑",
            "耳机麦克/华为/无线耳机/FreeBuds 7 T0028-月光白-HX",
        ):
            with self.subTest(name=name):
                self.assertTrue(catalog.matches_model(act, name))
        hits = metric.match_activities(
            "耳机麦克/华为/无线耳机/FreeBuds 7 T0028-星空黑",
            "2026-09-10", catalog.DEFAULT_ACTIVITIES,
            c1="音频产品", c2="耳机")
        self.assertTrue(any(h["id"] == "freebuds7-lost-202609" for h in hits))


class Test设备SN识别(unittest.TestCase):
    def test_JC前缀去掉后16位(self):
        # 用户：序列号前缀可能有 JC
        self.assertEqual(
            metric.pick_device_sn("JC5NC0226819005453"),
            "5NC0226819005453")
        self.assertEqual(metric.strip_sn_prefix("JC5NC0226819005453"),
                         "5NC0226819005453")
        # 已经 16 位且不以 JC 开头的不动
        self.assertEqual(metric.strip_sn_prefix("7MXTQ26721004552"),
                         "7MXTQ26721004552")

    def test_串号标识不当SN(self):
        self.assertEqual(metric.pick_device_sn("W,新"), "")
        self.assertEqual(metric.pick_device_sn("W,新", "5GV0225A09002368"),
                         "5GV0225A09002368")

    def test_优先16位(self):
        self.assertEqual(
            metric.pick_device_sn("123456789012345", "5GV0225A09002368"),
            "5GV0225A09002368")

    def test_多串挤一格取第一个16位(self):
        self.assertEqual(
            metric.pick_device_sn("5GV0225A09002368 5GV0225B11001321"),
            "5GV0225A09002368")

    def test_串号1到3_候选都扫(self):
        # 模拟 sn_raw 别名
        self.assertEqual(
            metric.pick_device_sn("", None, "not-s", "7MXTQ26721004552"),
            "7MXTQ26721004552")

    def test_15位纯数字IMEI也可(self):
        # 仍进 sn（状态键要稳），但 sn_kind 必须标成 imei
        self.assertEqual(metric.pick_device_sn("123456789012345"),
                         "123456789012345")

    def test_86码标imei_真SN标sn(self):
        self.assertTrue(metric.looks_like_imei("864468081285466"))
        self.assertFalse(metric.looks_like_imei("7ED9K26611031362"))
        self.assertEqual(metric.sn_kind("864468081285466"), "imei")
        self.assertEqual(metric.sn_kind("7ED9K26611031362"), "sn")
        self.assertEqual(metric.sn_kind(""), "")

    def test_pick_true_sn拒绝回落到IMEI(self):
        # 库存常见：imei=86码、sub_imei=真SN
        self.assertEqual(
            metric.pick_true_sn("864468081285466", "88Z9K26819007417"),
            "88Z9K26819007417")
        # 三列全是 86 码 → 空（不能拿 IMEI 去在线领）
        self.assertEqual(
            metric.pick_true_sn("864468081285466", "864468081285467"), "")

    def test_resolve_claim_sn_反查与透传(self):
        smap = {"864468081285466": "7ED9K26611031362"}
        self.assertEqual(
            metric.resolve_claim_sn("864468081285466", smap),
            "7ED9K26611031362")
        self.assertEqual(
            metric.resolve_claim_sn("864468081285466", {}), "")
        # 销售侧本来就是 SN → 原样，不查表
        self.assertEqual(
            metric.resolve_claim_sn("7ED9K26611031362", {}),
            "7ED9K26611031362")
        # 表里映射到 IMEI 的脏数据 → 拒收
        self.assertEqual(
            metric.resolve_claim_sn(
                "864468081285466", {"864468081285466": "864468081285467"}),
            "")


class Test销售导出三串号列(unittest.TestCase):
    def test_SALES_COLUMNS含串号2和串号3(self):
        from src.erp import SALES_COLUMNS, _column_form
        labels = [x[1] for x in SALES_COLUMNS]
        self.assertIn("串号", labels)
        self.assertIn("串号2", labels)
        self.assertIn("串号3", labels)
        keys = [x[0] for x in SALES_COLUMNS]
        self.assertIn("Imei2", keys)
        self.assertIn("Imei3", keys)
        # 表单也要真的发出去（不带 Column[] 服务端直接拒）
        form = _column_form()
        self.assertIn("串号2", set(form.values()))
        self.assertIn("串号3", set(form.values()))

    def test_待领SQL扫串号2到3(self):
        """库里有 串号2/3 列时，SELECT 必须带上 —— 否则真 SN 在副列也读不到。"""
        import sqlite3
        from src.features.tools.claim.pending import compute
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE erp_sales (
                sn TEXT, 串号 TEXT, 串号2 TEXT, 串号3 TEXT,
                门店 TEXT, 单据类型 TEXT, 商品名称 TEXT, 数量 TEXT,
                支付时间 TEXT, 备注 TEXT, 单行备注 TEXT,
                一级分类 TEXT, 二级分类 TEXT, 单号 TEXT, 店员 TEXT);
        """)
        sql = compute._select_sql(conn)
        self.assertIn("串号2", sql)
        self.assertIn("串号3", sql)
        self.assertIn("串号", sql)
        conn.close()


class Test库存反查映射(unittest.TestCase):
    def test_load_stock_sn_map_imei映射真SN(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from src.features.tools.claim.pending import compute
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE erp_stock (
                    snapshot_date TEXT, sn TEXT,
                    imei TEXT, sub_imei TEXT, sub_imei1 TEXT);
            """)
            # 旧快照有映射、新快照机器已出库 —— 必须仍能反查（扫全部快照）
            conn.execute(
                "INSERT INTO erp_stock VALUES (?,?,?,?,?)",
                ("2026-09-20", "864468081285466",
                 "864468081285466", "88Z9K26819007417", None))
            conn.execute(
                "INSERT INTO erp_stock VALUES (?,?,?,?,?)",
                ("2026-09-23", "OTHERSN000000001",
                 "OTHERSN000000001", None, None))
            # 全是 86 码的行不进表
            conn.execute(
                "INSERT INTO erp_stock VALUES (?,?,?,?,?)",
                ("2026-09-23", "864468081285400",
                 "864468081285400", "864468081285401", None))
            conn.commit()
            conn.close()
            m = compute.load_stock_sn_map(db)
            self.assertEqual(m.get("864468081285466"), "88Z9K26819007417")
            self.assertNotIn("864468081285400", m)

    def test_无库存表_返回空不炸(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from src.features.tools.claim.pending import compute
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            sqlite3.connect(db).close()
            self.assertEqual(compute.load_stock_sn_map(db), {})


class Test时间自动过滤(unittest.TestCase):
    def test_期外销售不进待领(self):
        act = {"id": "x", "match": ["Mate XTs"],
               "start": "2026-09-01", "end": "2026-09-30"}
        name = "智能手机/华为/Mate XTs"
        self.assertEqual(
            metric.match_activities(name, "2026-08-31", [act], c1="手机"), [])
        self.assertTrue(
            metric.match_activities(name, "2026-09-15", [act], c1="手机"))
        self.assertEqual(
            metric.match_activities(name, "2026-10-01", [act], c1="手机"), [])

    def test_活动一览标过期供前端开关(self):
        """接口返回全部 + expired 标记；默认列表由前端滤进行中。"""
        import datetime
        from src import web
        from unittest import mock
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        app = web.App(root, "config/store-X.yaml")
        with mock.patch.object(web, "role_scope",
                               lambda _a: {"role": "platform", "label": "p",
                                           "stores": None, "can": {}, "pages": [],
                                           "who": "", "account": "", "kind": "",
                                           "needs_linglong": False}):
            d = app.claim_activities()
        today = datetime.date.today().isoformat()
        self.assertTrue(d["ok"])
        self.assertIn("expired_count", d)
        n_out = 0
        for a in d["activities"]:
            want_expired = not catalog.in_window(a, today)
            self.assertEqual(bool(a.get("expired")), want_expired, a["id"])
            if a.get("expired"):
                n_out += 1
        self.assertEqual(d["expired_count"], n_out)
        self.assertEqual(d["active_count"], len(d["activities"]) - n_out)
        # 过期示例仍应在列表里但标 expired（供「显示已过期」）
        pro = catalog.activity_by_id("matepad-pro12-2026")
        if pro and pro.get("end") and pro["end"] < today:
            row = [x for x in d["activities"] if x["id"] == "matepad-pro12-2026"]
            self.assertTrue(row and row[0].get("expired"))


class Test最新领取链接与商品编码(unittest.TestCase):
    def test用户给的链接都进目录(self):
        want = {
            "care-fold-202609": "receive-huawei-care",
            "nova16-gift-202609": "nova16-care",
            "nova16se-gift-202609": "nova16se-care",
            "enjoy90pm-gift-202609": "changxiang90series",
            "freearc-lost-202609": "/freearc/",
            "freeclip2-lost-202609": "/freeclip2s/",
            "freebuds7-lost-202609": "/freebuds7/",
            "matepad-air-gen3-2026": "worry-free-care-package/matepad-air",
            "watch6-202609": "worry-free-care-package/watch6",
            "watch-gt7-202609": "/watch-gt-7/",
        }
        for aid, frag in want.items():
            a = catalog.activity_by_id(aid)
            self.assertIsNotNone(a, aid)
            self.assertIn(frag, a.get("url") or "", aid)
            self.assertTrue(a.get("privilege_codes"), aid + " 缺 privilege_codes")

    def test_GT7无空格商品名能命中(self):
        act = catalog.activity_by_id("watch-gt7-202609")
        self.assertTrue(catalog.matches_model(
            act, "手表/华为/HUAWEI WATCH GT7/46mm(黑色易扣氟橡胶表带)疾影黑"))

    def test_air第三代有独立码(self):
        a = catalog.activity_by_id("matepad-air-gen3-2026")
        self.assertEqual(a["privilege_codes"], ["8813047641"])


class Test注册挂到小工具(unittest.TestCase):
    def test_tools_children_待领在活动靠页内按钮(self):
        """活动一览不再是二级页 —— 待领页内按钮弹悬浮窗（用户 2026-09-23）。"""
        f = [x for x in registry.all_features() if x.key == "tools"][0]
        keys = [c.key for c in f.children]
        self.assertIn("claim-pending", keys)
        self.assertNotIn("claim-activities", keys)

    def test_没有一级claim也不占步骤(self):
        keys = [x.key for x in registry.all_features()]
        self.assertNotIn("claim", keys)
        steps = registry.steps()
        self.assertNotIn("claim-activities", steps)
        self.assertNotIn("claim-pending", steps)

    def test_validate空(self):
        self.assertEqual(registry.validate(), [])


class Test玲珑数据源(unittest.TestCase):
    """生活馆版待领清单读 orders × order_lines（池A 玲珑销售单）。"""

    def setUp(self):
        import os
        from src import edition
        prev = os.environ.get("CBG_EDITION")

        def _restore():
            # ⚠ 必须**恢复原值**而不是 pop：本分支根下有 `EDITION` 文件
            #   写着 `lifehall`，pop 掉 conftest 钉的 full 之后，
            #   后面所有测试都会读到文件版 lifehall（实测污染 test_web 定时器）。
            if prev is None:
                os.environ.pop("CBG_EDITION", None)
            else:
                os.environ["CBG_EDITION"] = prev
            edition.reload()

        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(_restore)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "cbg-2026.db"
        conn = sqlite3.connect(str(self.db))
        conn.executescript("""
            CREATE TABLE orders (
                document_no TEXT PRIMARY KEY, store_code TEXT, store_name TEXT,
                status_name TEXT, pay_status INTEGER, return_status INTEGER,
                consumer_guide_name TEXT, doc_create_time TEXT, remark TEXT);
            CREATE TABLE order_lines (
                document_no TEXT NOT NULL, line_no INTEGER NOT NULL,
                sn TEXT, item_name TEXT, quantity REAL,
                PRIMARY KEY (document_no, line_no));
            CREATE TABLE returns (document_no TEXT, related_doc_no TEXT);
        """)
        rows = [
            # (doc, store, item, sn, qty, time, return_status) —— 窗口 2026-09
            ("D1", "店A", "手机/华为Pura 70", "1234567890ABCDEF", 1,
             "2026-09-10 12:00:00", 0),                    # ✓ 命中
            ("D2", "店A", "手机/华为Pura 70 保护壳", "", 1,
             "2026-09-11 12:00:00", 0),                    # ✗ 无 SN（配件）
            ("D3", "店A", "手提袋", "", 2,
             "2026-09-12 12:00:00", 0),                    # ✗ 无 SN + 名字不匹配
            ("D4", "店A", "手机/华为Pura 70", "FFEDCBA098765432", 1,
             "2026-08-01 12:00:00", 0),                    # ✗ 窗口外
            ("D5", "店A", "手机/华为Pura 70", "0123456789ABCDEF", 1,
             "2026-09-13 12:00:00", 1),                    # ✗ 已退货
        ]
        for doc, store, item, sn, qty, ts, ret in rows:
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                         (doc, "SCN1", store, "已完成", 2, ret, "张三", ts, ""))
            conn.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)",
                         (doc, 1, sn, item, qty))
        conn.commit()
        conn.close()

    def test_按机型窗口退货过滤(self):
        from src.features.tools.claim.pending import compute
        out = compute.load_sales_linglong(
            self.db, "2026-09-01", "2026-09-30")
        names = [(r["doc"], r["sn"]) for r in out]
        self.assertEqual(names, [("D1", "1234567890ABCDEF")])

    def test_load走玲珑分支(self):
        from src.features.tools.claim.pending import compute
        with mock.patch.object(compute, "find_db", return_value=self.db), \
             mock.patch.object(compute, "load_sales_linglong",
                               return_value=[{
                                   "store": "店A", "who": "张三", "typ": "销售",
                                   "name": "手机/华为Pura 70", "qty": 1,
                                   "ts": "2026-09-10 12:00:00", "doc": "D1",
                                   "sn": "1234567890ABCDEF",
                                   "claim_sn": "1234567890ABCDEF",
                                   "sn_kind": "sn", "c1": "", "c2": "",
                                   "note": ""}]) as ld, \
             mock.patch("src.features.tools.claim.activities."
                        "catalog.load_activities", return_value=[]):
            d = compute.load(root=Path(self._tmp.name))
        ld.assert_called_once()
        self.assertTrue(d["ok"])
        self.assertEqual(d["src"], "orders")

    def test_full版仍读erp_sales(self):
        import os
        from src import edition
        os.environ["CBG_EDITION"] = "full"
        edition.reload()
        from src.features.tools.claim.pending import compute
        with mock.patch.object(compute, "find_db", return_value=self.db), \
             mock.patch.object(compute, "load_sales") as ld, \
             mock.patch.object(compute, "load_stock_sn_map", return_value={}), \
             mock.patch("src.features.tools.claim.activities."
                        "catalog.load_activities", return_value=[]):
            compute.load(root=Path(self._tmp.name))
        ld.assert_called_once()


if __name__ == "__main__":
    unittest.main()
