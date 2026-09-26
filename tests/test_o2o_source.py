"""O2O 核心三件套的单测（4.0.0 M-A1/A2/A3）：

* `snapshot` —— 门店→分仓匹配 / 快照口径（在库·样机·演示机）/ 本地读壳五种失败态；
* `source`  —— 库存源决策五态（cloud/manual/missing_mapping/missing_snapshot/invalid_manual）；
* `settings`—— config/o2o 三件套读写、稀疏覆盖、表头/平台校验。

⚠ 全部用 tmp root + 手造行，**不联网、不碰真库**（2026-09-24 实测联网探测
会把云商账号挤下线，见 snapshot 模块头）。
"""

import datetime
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.app.o2o import settings as o2o_settings
from src.app.o2o import snapshot, source


def _row(**kw):
    """池D 行骨架（字段名 = erp_stock 实际列）。"""
    base = {"pro_id": "80000001", "pro_name": "智能手机/华为/测试机",
            "store_name": "顺和汇库", "status": "在库", "old_flag": "新"}
    base.update(kw)
    return base


class TestMatchStore(unittest.TestCase):
    """门店名 → 分仓名：精确 → 核心词互含 → 不猜。"""

    def test_精确相等直通(self):
        hit, _ = snapshot.match_store("顺和汇库", ["顺和汇库", "青岛悦荟库"])
        self.assertEqual(hit, "顺和汇库")

    def test_核心词互含_店对库(self):
        # 华为授权体验店-顺和汇店 → 顺和汇；顺和汇库 → 顺和汇 —— 唯一命中
        hit, _ = snapshot.match_store("华为授权体验店-顺和汇店",
                                      ["青岛胶州龙湖库", "顺和汇库"])
        self.assertEqual(hit, "顺和汇库")

    def test_核心词互含_长名短名(self):
        # 库核心「胶南合美MALL」包含店核心「合美mall」（大小写不敏感）
        hit, _ = snapshot.match_store("华为授权体验店-合美mall店",
                                      ["青岛悦荟库", "胶南合美MALL库"])
        self.assertEqual(hit, "胶南合美MALL库")

    def test_多命中不猜_返回候选(self):
        hit, cands = snapshot.match_store("华为授权体验店-万达店",
                                          ["青岛城阳万达库", "青岛CBD万达库"])
        self.assertEqual(hit, "")                       # 两个都含「万达」→ 不猜
        self.assertEqual(cands, ["青岛城阳万达库", "青岛CBD万达库"])

    def test_零命中不猜(self):
        hit, cands = snapshot.match_store("平台岗", ["顺和汇库", "青岛悦荟库"])
        self.assertEqual(hit, "")
        self.assertEqual(cands, ["顺和汇库", "青岛悦荟库"])

    def test_空输入_空结果(self):
        self.assertEqual(snapshot.match_store("", ["顺和汇库"]), ("", ["顺和汇库"]))
        self.assertEqual(snapshot.match_store("某店", []), ("", []))


class TestBuild(unittest.TestCase):
    """快照口径：门店过滤 → 在库 → 剔样机/演示机 → ProId 聚合。"""

    def test_按门店过滤_别的仓不计(self):
        rows = [_row(store_name="顺和汇库", pro_id="1"),
                _row(store_name="青岛悦荟库", pro_id="2")]
        self.assertEqual(snapshot.build(rows, "顺和汇库"), {"1": 1})

    def test_剔在途(self):
        rows = [_row(), _row(status="在途"), _row(status="")]
        # 在库1 + status空1（老库缺列的兼容面：空不当在途杀，当在库算？否 ——
        # 口径写死「只要在库」：非"在库"一律剔）
        self.assertEqual(snapshot.build(rows, "顺和汇库"), {"80000001": 1})

    def test_剔样机_串号标识判据(self):
        rows = [_row(), _row(old_flag="样,新"), _row(old_flag="s,样，新")]
        self.assertEqual(snapshot.build(rows, "顺和汇库"), {"80000001": 1})

    def test_剔演示机_名称判据_标识不带样也剔(self):
        # 实测 575 行 old_flag=新 但名字带演示 —— 只按标识判会漏
        rows = [_row(),
                _row(pro_id="9", pro_name="智能手机/华为/nova 15 ULTRA-黑马演示机"),
                _row(pro_id="8", pro_name="笔记本/华为/x[样机]")]
        self.assertEqual(snapshot.build(rows, "顺和汇库"), {"80000001": 1})

    def test_未选分仓_返回空(self):
        self.assertEqual(snapshot.build([_row()], ""), {})

    def test_跨仓同ProId_只算本店(self):
        rows = [_row(pro_id="7"), _row(pro_id="7"), _row(pro_id="7", store_name="青岛悦荟库")]
        self.assertEqual(snapshot.build(rows, "顺和汇库"), {"7": 2})


class TestLoad(unittest.TestCase):
    """本地读壳：五种失败态全显式；成功态带 day/fresh/stores/建议。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.db = self.root / "out" / ("cbg-%d.db" % datetime.date.today().year)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_db(self, days=("2026-01-01",), extra_cols=True):
        conn = sqlite3.connect(str(self.db))
        cols = ("snapshot_date TEXT NOT NULL, sn TEXT NOT NULL, pro_id TEXT,"
                "pro_name TEXT, store_name TEXT, status TEXT, old_flag TEXT")
        if not extra_cols:
            cols = "snapshot_date TEXT NOT NULL, sn TEXT NOT NULL"
        conn.execute("CREATE TABLE IF NOT EXISTS erp_stock (%s)" % cols)
        for d in days:
            conn.execute(
                "INSERT INTO erp_stock (snapshot_date, sn, pro_id, pro_name,"
                " store_name, status, old_flag) VALUES (?,?,?,?,?,?,?)",
                (d, "sn-%s" % d, "80000001", "智能手机/华为/测试机",
                 "顺和汇库", "在库", "新")) if extra_cols else conn.execute(
                "INSERT INTO erp_stock VALUES (?,?)", (d, "sn-%s" % d))
        conn.commit()
        conn.close()

    def test_缺库文件_显式失败(self):
        res = snapshot.load(self.root, "顺和汇库")
        self.assertFalse(res["ok"])
        self.assertIn("还没抓过数", res["why"])
        self.assertIsNone(res["stock"])

    def test_没快照_显式失败(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE erp_stock (snapshot_date TEXT NOT NULL, sn TEXT NOT NULL)")
        conn.commit(); conn.close()
        res = snapshot.load(self.root, "顺和汇库")
        self.assertFalse(res["ok"])
        self.assertIn("没有快照", res["why"])

    def test_缺列_显式失败(self):
        self._make_db(extra_cols=False)
        res = snapshot.load(self.root, "顺和汇库")
        self.assertFalse(res["ok"])
        self.assertIn("缺列", res["why"])

    def test_未选分仓_给门店建议(self):
        self._make_db(days=("2026-01-01",))
        res = snapshot.load(self.root, "", erp_store_name="华为授权体验店-顺和汇店")
        self.assertFalse(res["ok"])
        self.assertIn("还没选定", res["why"])
        self.assertEqual(res["suggested"], "顺和汇库")      # 门店名自动对上分仓
        self.assertEqual(res["stores"], ["顺和汇库"])

    def test_成功_聚合与新鲜度(self):
        self._make_db(days=("2026-01-01",))
        today = datetime.date(2026, 1, 1)
        res = snapshot.load(self.root, "顺和汇库", today=today)
        self.assertTrue(res["ok"], res["why"])
        self.assertEqual(res["stock"], {"80000001": 1})
        self.assertEqual(res["day"], "2026-01-01")
        self.assertTrue(res["fresh"])
        self.assertEqual(res["rows_in_store"], 1)
        # 非今天 → fresh=False 但依然可用（今天照常跑 erp-dump 才会 fresh）
        res2 = snapshot.load(self.root, "顺和汇库",
                             today=datetime.date(2026, 1, 2))
        self.assertTrue(res2["ok"])
        self.assertFalse(res2["fresh"])

    def test_存的分仓过期_显式失败并给建议(self):
        self._make_db(days=("2026-01-01",))
        res = snapshot.load(self.root, "已改名旧库",
                            erp_store_name="华为授权体验店-顺和汇店")
        self.assertFalse(res["ok"])
        self.assertIn("不在最新快照", res["why"])
        self.assertEqual(res["suggested"], "顺和汇库")


class TestDecide(unittest.TestCase):
    """库存源决策五态。"""

    M = [{"itemid": "1", "sku_id": "s1", "title": "A", "pro_id": "p1", "status": "review"},
         {"itemid": "1", "sku_id": "s2", "title": "B", "pro_id": "", "status": "miss"},
         {"itemid": "2", "sku_id": "s3", "title": "C", "pro_id": "p3", "status": "review"}]

    def test_默认全跟云商(self):
        out = source.decide(self.M, {}, {"p1": 5, "p3": 0})
        self.assertEqual([r["state"] for r in out],
                         [source.STATE_CLOUD, source.STATE_NO_MAPPING, source.STATE_CLOUD])
        self.assertEqual([r["value"] for r in out], [5, None, 0])   # 0 台 = 传 0

    def test_手动常驻_不吃快照(self):
        out = source.decide(self.M, {"s1": {"source": "manual", "value": "12"}},
                            None)                                   # 快照整体缺失
        by = {r["sku_id"]: r for r in out}
        self.assertEqual(by["s1"]["state"], source.STATE_MANUAL)
        self.assertEqual(by["s1"]["value"], 12)                     # 手动行快照没了也活
        self.assertEqual(by["s3"]["state"], source.STATE_NO_SNAPSHOT)
        self.assertIsNone(by["s3"]["value"])

    def test_缺映射优先于快照(self):
        out = source.decide(self.M, {}, None)
        by = {r["sku_id"]: r for r in out}
        self.assertEqual(by["s2"]["state"], source.STATE_NO_MAPPING)

    def test_miss与weak的ProId不参与_哪怕非空(self):
        # 验收实锤：miss/weak 行的 ProId 是垃圾猜测（MatePad→matebook 13…）
        m = [{"itemid": "9", "sku_id": "a", "title": "T", "pro_id": "7211882",
              "status": "miss"},
             {"itemid": "9", "sku_id": "b", "title": "T", "pro_id": "10133949",
              "status": "weak"},
             {"itemid": "9", "sku_id": "c", "title": "T", "pro_id": "9244361",
              "status": "review"}]
        out = source.decide(m, {}, {"7211882": 50, "10133949": 60, "9244361": 1})
        by = {r["sku_id"]: r for r in out}
        self.assertEqual(by["a"]["state"], source.STATE_NO_MAPPING)
        self.assertEqual(by["b"]["state"], source.STATE_NO_MAPPING)
        self.assertIsNone(by["a"]["value"])       # 垃圾数一个都不许流出
        self.assertIsNone(by["b"]["value"])
        self.assertEqual(by["c"]["state"], source.STATE_CLOUD)
        self.assertEqual(by["c"]["value"], 1)

    def test_非法手动值_显式失败不回落云商(self):
        for bad in ("-1", "abc", "", None, "1.5"):
            out = source.decide(self.M, {"s1": {"source": "manual", "value": bad}},
                                {"p1": 5})
            by = {r["sku_id"]: r for r in out}
            self.assertEqual(by["s1"]["state"], source.STATE_BAD_MANUAL, bad)
            self.assertIsNone(by["s1"]["value"], bad)               # 不许悄悄用云商数

    def test_整数文本与数值都认(self):
        for ok in ("12", 12, 12.0, 0):
            out = source.decide(self.M, {"s1": {"source": "manual", "value": ok}}, {})
            self.assertEqual(out[0]["value"], int(ok), ok)

    def test_显式cloud覆盖_回落云商(self):
        # 手动行存回 cloud（稀疏表里消失）⇒ 跟随云商
        out = source.decide(self.M, {"s1": {"source": "cloud", "value": ""}},
                            {"p1": 7})
        self.assertEqual(out[0]["state"], source.STATE_CLOUD)
        self.assertEqual(out[0]["value"], 7)


class TestSettings(unittest.TestCase):
    """config/o2o 三件套：读写往返、稀疏、校验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_设置读写往返_只存已知键(self):
        self.assertEqual(o2o_settings.load_settings(self.root), {"store_name": ""})
        o2o_settings.save_settings(self.root, "顺和汇库")
        self.assertEqual(o2o_settings.load_settings(self.root),
                         {"store_name": "顺和汇库"})
        # 脏文件：非 dict → 显式错
        p = self.root / "config" / "o2o" / "settings.yaml"
        p.write_text("- a\n- b\n", encoding="utf-8")
        with self.assertRaises(o2o_settings.O2oSettingsError):
            o2o_settings.load_settings(self.root)

    def test_映射表读写往返(self):
        rows = [{"itemid": "1", "sku_id": "s1", "title": "T", "pro_id": "p1",
                 "status": "review", "cloud_name": "", "spec": ""}]
        o2o_settings.save_mapping(self.root, "tmall", rows)
        got = o2o_settings.load_mapping(self.root, "tmall")
        self.assertEqual(got, rows)
        # 平台白名单逐个都要读写通（2026-09-26 京东接入后 PLATFORMS 不止 tmall ——
        # 旧写法只存 tmall 却去读 jd，加平台当天就把这条踩红了）
        for plat in o2o_settings.PLATFORMS:
            o2o_settings.save_mapping(self.root, plat, rows)
            self.assertEqual(o2o_settings.load_mapping(self.root, plat), rows,
                             "平台 %s 读写不通" % plat)

    def test_未知平台_显式报错(self):
        with self.assertRaises(o2o_settings.O2oSettingsError):
            o2o_settings.load_mapping(self.root, "not-a-platform")

    def test_覆盖表_稀疏_云商行不落盘(self):
        over = {"s1": {"source": "manual", "value": 9},
                "s2": {"source": "cloud", "value": ""}}       # cloud → 不写
        o2o_settings.save_overrides(self.root, "tmall", over)
        got = o2o_settings.load_overrides(self.root, "tmall")
        self.assertEqual(set(got), {"s1"})                    # 稀疏：只有手动行
        self.assertEqual(got["s1"]["value"], "9")
        # 切回云商 = 删行
        o2o_settings.save_overrides(self.root, "tmall", {"s2": {"source": "cloud"}})
        self.assertEqual(o2o_settings.load_overrides(self.root, "tmall"), {})

    def test_表头不符_显式报错不猜(self):
        o2o_settings.save_mapping(self.root, "tmall", [])
        p = o2o_settings.mapping_path(self.root, "tmall")
        p.write_text("坏表头,乱的\n1,2\n", encoding="utf-8-sig")
        with self.assertRaises(o2o_settings.O2oSettingsError):
            o2o_settings.load_mapping(self.root, "tmall")

    def test_端到端_快照决策(self):
        """快照 → 决策串起来：门店/商品编号两键 + 手动行。"""
        tmp2 = tempfile.TemporaryDirectory()
        try:
            root = pathlib.Path(tmp2.name)
            (root / "out").mkdir(parents=True)
            db = root / "out" / ("cbg-%d.db" % datetime.date.today().year)
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE erp_stock (snapshot_date TEXT NOT NULL,"
                         " sn TEXT NOT NULL, pro_id TEXT, pro_name TEXT,"
                         " store_name TEXT, status TEXT, old_flag TEXT)")
            conn.execute("INSERT INTO erp_stock VALUES ('2026-01-01','s','pA',"
                         "'智能手机/华为/x','顺和汇库','在库','新')")
            conn.execute("INSERT INTO erp_stock VALUES ('2026-01-01','s2','pA',"
                         "'智能手机/华为/x','顺和汇库','在途','新')")
            conn.execute("INSERT INTO erp_stock VALUES ('2026-01-01','s3','pA',"
                         "'智能手机/华为/x[演示机]','顺和汇库','在库','新')")
            conn.commit(); conn.close()
            snap = snapshot.load(root, "顺和汇库", today=datetime.date(2026, 1, 1))
            self.assertEqual(snap["stock"], {"pA": 1})         # 在途+演示机全剔
            mapping = [{"itemid": "i1", "sku_id": "sk1", "title": "A",
                        "pro_id": "pA", "status": "review"},
                       {"itemid": "i1", "sku_id": "sk2", "title": "B",
                        "pro_id": "pB", "status": "review"}]
            over = {"sk2": {"source": "manual", "value": 3}}
            out = source.decide(mapping, over, snap["stock"])
            self.assertEqual([r["value"] for r in out], [1, 3])
            self.assertEqual([r["state"] for r in out],
                             [source.STATE_CLOUD, source.STATE_MANUAL])
        finally:
            tmp2.cleanup()


class Test选仓即用(unittest.TestCase):
    """GET `/api/oto/source` 的 `store` 参数：选完当场出数、**不落盘**。

    用户 2026-09-26：「不保存，我选择门店，加载云商的库存数。应该这样」——
    分仓是页面的即时选择（只管本次请求）；保存设置只管手动值和"记住上次选择"。
    ⚠ `parse_qs` 默认丢空值 → 清空选择走哨兵 `__none__`（见 `_o2o_store_query`）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "out").mkdir(parents=True)
        db = self.root / "out" / ("cbg-%d.db" % datetime.date.today().year)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE erp_stock (snapshot_date TEXT NOT NULL,"
                     " sn TEXT NOT NULL, pro_id TEXT, pro_name TEXT,"
                     " store_name TEXT, status TEXT, old_flag TEXT)")
        day = datetime.date.today().isoformat()
        rows = [(day, "s1", "pA", "手机X", "甲库", "在库", "新"),    # 算
                (day, "s2", "pA", "手机X", "甲库", "在库", "新"),    # 算（甲库共 2 台）
                (day, "s3", "pA", "手机X", "甲库", "在途", "新"),    # 剔：在途
                (day, "s4", "pB", "演示机", "甲库", "在库", "新"),    # 剔：名称带演示
                (day, "s5", "pA", "手机X", "乙库", "在库", "新")]     # 乙库 1 台
        conn.executemany("INSERT INTO erp_stock VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        o2o_settings.save_mapping(self.root, "tmall", [
            {"itemid": "i1", "sku_id": "sk1", "title": "A", "cloud_name": "c",
             "spec": "", "pro_id": "pA", "status": "review"}])
        from src import web
        self.app = web.App(self.root, "config/store-X.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_带store参数_用本次选择且不落盘(self):
        got = self.app.o2o_source("tmall", store="甲库")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["snapshot"]["store_name"], "甲库")
        self.assertEqual(got["snapshot"]["rows_in_store"], 4)   # 甲库全 4 行（在途/演示只在这儿剔）
        self.assertEqual(got["rows"][0]["value"], 2)            # 在库 2 台
        # 参数只管本次请求：设置文件一个字都没动
        self.assertEqual(o2o_settings.load_settings(self.root), {"store_name": ""})

    def test_换一个仓数字跟着变(self):
        got = self.app.o2o_source("tmall", store="乙库")
        self.assertEqual(got["snapshot"]["store_name"], "乙库")
        self.assertEqual(got["snapshot"]["rows_in_store"], 1)
        self.assertEqual(got["rows"][0]["value"], 1)

    def test_不带参数_回落已存设置(self):
        o2o_settings.save_settings(self.root, "乙库")
        got = self.app.o2o_source("tmall")                     # store=None
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["snapshot"]["store_name"], "乙库")

    def test_选的仓不在快照_显式报错且不落盘(self):
        got = self.app.o2o_source("tmall", store="不存在库")
        self.assertFalse(got["snapshot"]["ok"])
        self.assertIn("不存在库", got["snapshot"]["why"])       # 显式说清，不猜别的仓
        self.assertEqual(o2o_settings.load_settings(self.root), {"store_name": ""})

    def test_路由参数解析_空值哨兵(self):
        from src import web
        self.assertIsNone(web._o2o_store_query({}))                       # 没带 → 已存设置
        self.assertEqual(web._o2o_store_query({"store": ["顺和汇库"]}), "顺和汇库")
        self.assertEqual(web._o2o_store_query({"store": ["__none__"]}), "")   # 清空选择


if __name__ == "__main__":
    unittest.main()
