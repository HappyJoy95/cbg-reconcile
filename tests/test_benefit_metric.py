# -*- coding: utf-8 -*-
"""无忧会员权益（`features/valueadd/benefit/`）的口径与配置。

钉的是 `metric.py` 里的公式 —— 改口径先看这里。
⚠ 开发逆推稿（含源表文件名）**不进正式包**，只留在仓库 `.dsh/`。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.features.valueadd.benefit import metric as m


class Test分类(unittest.TestCase):
    def test六档认得出(self):
        self.assertEqual(m.tier_of("无忧会员 优选版本-二年299元（新）"), "opt")
        self.assertEqual(m.tier_of("无忧会员 超值版本-二年399元（新）"), "sup")
        self.assertEqual(m.tier_of("无忧会员 全能进阶-二年699元（新1）"), "adv")
        self.assertEqual(m.tier_of("无忧会员 旗舰顶配-二年899元（新）"), "max")
        self.assertEqual(m.tier_of("399元轻奢防护套装权益"), "p399")
        self.assertEqual(m.tier_of("499元全能尊享套装权益"), "p499")

    def test贴膜礼包不算无忧(self):
        self.assertIsNone(m.tier_of("99元光固电镀膜礼包"))
        self.assertIsNone(m.tier_of("178直面屏钢化膜礼包-第二张半价1"))

    def testCare与手机(self):
        self.assertTrue(m.is_care("延保服务"))
        self.assertFalse(m.is_care("会员卡"))
        self.assertTrue(m.is_phone("手机"))
        self.assertTrue(m.is_meituan("美团转线上"))
        self.assertTrue(m.is_meituan("抖音"))
        self.assertFalse(m.is_meituan("京东"))

    def test核销不算(self):
        self.assertFalse(m.sell_ok("核销"))
        self.assertTrue(m.sell_ok("零售"))
        self.assertTrue(m.sell_ok("零售退"))


class Test门店行(unittest.TestCase):
    def _row(self, **kw):
        base = dict(
            store="店", track="A", day_target=224.4, day=22,
            new_retail=100.0, new_online=20.0,
            tiers={"opt": 0, "sup": 0, "adv": 0, "max": 0, "p399": 20, "p499": 0},
            tier_profit=5801.0, care_qty=4.0, care_profit=643.0,
        )
        base.update(kw)
        return m.store_row(**base)

    def test台量进度与目标(self):
        r = self._row()
        # day=22 → **22**/30 = 0.7333；224.4*22/30 = 164.48
        # ⚠ 2026-09-29 从 `(day-1)/30` 改成 `day/30`（用户：「2 问题不大」）：
        #   截止 28 日 = 93.3%（源表右上角实写 93.3%），30 号 = 100%（旧式永远 96.7%）。
        self.assertAlmostEqual(r["slot_progress"], 224.4 * 22 / 30.0, places=3)
        self.assertAlmostEqual(r["goal"], r["slot_progress"] * 0.2, places=4)

    def test进度封顶一百_看上月全月不越界(self):
        """**日期窗口**（2026-09-29）：选上月最后一天（day=31）时
        进度封顶 100%、台量进度 = 整月目标，不能冲到 31/30。"""
        self.assertAlmostEqual(self._row(day=30)["slot_progress"], 224.4, places=3)
        self.assertAlmostEqual(self._row(day=31)["slot_progress"], 224.4, places=3,
                               msg="31 号（8 月最后一天）不能超过整月目标")
        self.assertAlmostEqual(self._row(day=1)["slot_progress"],
                               224.4 / 30.0, places=3,
                               msg="1 号 = 1/30（不再是 0）")

    def test后返与台均不含后返(self):
        r = self._row()
        self.assertEqual(r["rebate"], 20 * 100)          # 399套装×100
        self.assertEqual(r["total"], 24)                 # 20 无忧 + 4 Care+
        # 台均 = (5801+643)/120
        self.assertAlmostEqual(r["avg_profit"], (5801 + 643) / 120.0, places=6)
        self.assertAlmostEqual(r["profit_total"], 2000 + 643 + 5801, places=6)

    def test总达成率权重(self):
        r = self._row()
        expect = r["new_rate"] * 0.3 + r["attach_goal_rate"] * 0.7
        self.assertAlmostEqual(r["overall"], expect, places=9)


class Test人员行(unittest.TestCase):
    def test公式跟源表(self):
        r = m.person_row(
            "青岛永旺东部店", "张鸿兴", "销售顾问",
            new_retail=29, new_online=0,
            tiers={"opt": 6, "sup": 3, "adv": 1, "max": 2, "p399": 1, "p499": 0},
            tier_profit=3386.0, care_qty=3, care_profit=179.0,
        )
        # 后返 = 6*100+3*100+1*150+2*150+1*100 + 3*150 = 1650+450 = 2100？
        # 源表：F*100+G*100+H*150+I*150+J*100+K*150+L*150
        # =600+300+150+300+100+0+450 = 1900
        self.assertEqual(r["rebate"], 600 + 300 + 150 + 300 + 100 + 450)
        self.assertEqual(r["total"], 6 + 3 + 1 + 2 + 1)  # 合计不含 Care+
        self.assertEqual(r["care"], 3)
        # 源表跟进表张鸿兴：后返1900 Care+179 权益3386 利润5465 奖金546.5
        self.assertEqual(r["profit_total"], 1900 + 179 + 3386)
        self.assertAlmostEqual(r["bonus"], 546.5, places=6)
        self.assertAlmostEqual(r["avg_profit"], 3386 / 29.0, places=6)
        self.assertAlmostEqual(r["bonus_month"], 546.5 / 7 * 36, places=6)

    def testCare后返无4单门槛(self):
        r = m.person_row("店", "人", "店长", care_qty=1, care_profit=10)
        self.assertEqual(r["rebate"], 150)  # 只有 1 单也给


class Test排名(unittest.TestCase):
    def test奖金降序(self):
        rows = [
            {"store": "a", "name": "甲", "bonus": 10},
            {"store": "b", "name": "乙", "bonus": 30},
            {"store": "c", "name": "丙", "bonus": 20},
        ]
        out = m.rank_people(rows)
        self.assertEqual([r["name"] for r in out], ["乙", "丙", "甲"])
        self.assertEqual([r["rank"] for r in out], [1, 2, 3])


class Test赛道奖金(unittest.TestCase):
    def test前三分配与门槛(self):
        stores = [
            {"store": "s1", "track": "A", "overall": 1.2, "attach": 0.4,
             "new": 100, "total": 40, "goal": 50},
            {"store": "s2", "track": "A", "overall": 0.95, "attach": 0.3,
             "new": 100, "total": 30, "goal": 50},
            {"store": "s3", "track": "A", "overall": 0.5, "attach": 0.2,
             "new": 100, "total": 20, "goal": 50},
            {"store": "s4", "track": "A", "overall": 1.5, "attach": 0.25,
             "new": 100, "total": 25, "goal": 50},
        ]
        # 连带达成 = total/goal：40/50=0.8 ≥0.5 ⇒ 不罚
        r = m.allocate_track("A", 2000, stores)
        self.assertFalse(r["fined"])
        ranks = [(x["store"], x["paid"]) for x in r["rows"]]
        # overall: s4=1.5, s1=1.2, s2=0.95 → 前三 s4/s1/s2；s3 0.5 进不了
        self.assertEqual(ranks[0], ("s4", 1000.0))
        self.assertEqual(ranks[1], ("s1", 600.0))
        self.assertEqual(ranks[2], ("s2", 400.0))
        self.assertEqual(r["paid"], 2000.0)

    def test低于90不发收回(self):
        stores = [
            {"store": "s1", "overall": 0.99, "attach": 0.4, "new": 10, "total": 5, "goal": 10},
            {"store": "s2", "overall": 0.80, "attach": 0.4, "new": 10, "total": 5, "goal": 10},
            {"store": "s3", "overall": 0.70, "attach": 0.4, "new": 10, "total": 5, "goal": 10},
        ]
        r = m.allocate_track("B", 1500, stores)
        self.assertTrue(r["rows"][0]["gate_ok"])
        self.assertFalse(r["rows"][1]["gate_ok"])
        self.assertEqual(r["rows"][1]["paid"], 0)
        self.assertEqual(r["paid"], 750.0)
        self.assertEqual(r["recycled"], 750.0)

    def test连带达成低于50扣一笔300(self):
        stores = [
            {"store": "s1", "overall": 1.2, "attach": 0.1, "new": 100, "total": 10, "goal": 100},
            {"store": "s2", "overall": 1.1, "attach": 0.1, "new": 100, "total": 10, "goal": 100},
            {"store": "s3", "overall": 1.0, "attach": 0.1, "new": 100, "total": 10, "goal": 100},
        ]
        # total/goal = 30/300 = 0.1 < 0.5 ⇒ 罚
        r = m.allocate_track("C", 1000, stores)
        self.assertTrue(r["fined"])
        self.assertEqual(r["fine"], 300)
        # 从最后有份额的人往回扣（reversed）→ 第三名 200 先扣 200，再扣第一名？
        # 实现是从 reversed(rows) 扣：先 s3(200) 扣光 200，再 s2 扣 100
        self.assertEqual(r["paid"], 700.0)
        self.assertEqual(r["recycled"], 300.0)


class Test配置(unittest.TestCase):
    def test默认赛道齐(self):
        cfg = m.default_config()
        self.assertEqual(set(cfg["tracks"]), {"A", "B", "C", "D"})
        n = sum(len(v["stores"]) for v in cfg["tracks"].values())
        self.assertEqual(n, 27)

    def testyaml覆盖(self):
        base = m.default_config()
        over = {"tracks": {"A": {"pool": 999, "stores": {"新店": 10.0}}},
                "people": [{"name": "张三", "title": "店长", "store": "新店"}]}
        out = m.merge_config(base, over)
        self.assertEqual(out["tracks"]["A"]["pool"], 999)
        # people 仅 merge 单元能力；**load_config 会丢掉**（名册不走 yaml）
        self.assertEqual(len(out["people"]), 1)
        self.assertIn("B", out["tracks"])

    def testtrack_of_region_of(self):
        cfg = m.default_config()
        t, tgt = m.track_of("青岛城阳万达店", cfg["tracks"])
        self.assertEqual(t, "A")
        self.assertAlmostEqual(tgt, 224.4)
        self.assertEqual(m.region_of("青岛城阳万达店", cfg["regions"]), "城阳胶州")
        self.assertEqual(m.track_of("不存在的店", cfg["tracks"]), ("", 0.0))


class Test区域行(unittest.TestCase):
    def test目标是新机乘百分之十五且分子不含care(self):
        stores = [m.store_row("a", new_retail=100, tiers={"p399": 10},
                              care_qty=5, tier_profit=100, care_profit=50),
                  m.store_row("b", new_retail=100, tiers={"opt": 6},
                              care_qty=2, tier_profit=60, care_profit=20)]
        r = m.region_row("市区", stores=stores)
        self.assertAlmostEqual(r["goal"], 200 * 0.15, places=6)
        self.assertEqual(r["wuyou"], 16)
        self.assertEqual(r["care"], 7)
        self.assertEqual(r["total"], 23)
        self.assertAlmostEqual(r["goal_rate"], 16 / 30.0, places=6)  # 分子只有无忧
        self.assertEqual(r["profit_total"], r["rebate"] + r["tier_profit"])  # 不含 Care+


class Test系统人店表(unittest.TestCase):
    """名册 = 云商人店表；业务数 = fetch 的 sqlite —— 都不读用户 Excel（2026-09-22）。"""

    def test_load_people_只认系统(self):
        from src.features.valueadd.benefit import compute as bcomp
        roster, src, why = bcomp.load_people()
        self.assertIn(src, ("system", "empty"))
        self.assertNotEqual(src, "yaml")
        if src == "system":
            self.assertIn(("青岛城阳家佳源店", "公雨"), roster)
            self.assertFalse(any("宝龙" in st for st, _ in roster))

    def test默认配置没有people名册(self):
        self.assertEqual(m.default_config().get("people") or [], [])

    def test_load_config丢掉yaml里的people(self):
        from src.features.valueadd.benefit import compute as bcomp
        cfg = bcomp.load_config()
        self.assertEqual(cfg.get("people") or [], [])

    def test店名单不来自名册(self):
        from src.features.valueadd.benefit import compute as bcomp
        d = bcomp.compute(stores=["青岛城阳家佳源店"])
        self.assertEqual([s["store"] for s in d["stores"]], ["青岛城阳家佳源店"])
        self.assertTrue(any(p["name"] == "公雨" for p in d["people"]))

    def test_不读用户桌面excel(self):
        from src.features.valueadd.benefit import compute as bcomp
        src = open(bcomp.__file__, encoding="utf-8").read()
        self.assertNotIn("load_workbook", src)
        self.assertNotIn("Desktop", src)
        # 桌面源表文件名拆开写 —— 正式包反查禁词不要命中测试本身
        needle = "汇机保" + "数据统计"
        self.assertNotIn(needle, src)
        self.assertIn("erp_sales", src)


class _BenefitDbCase:
    """脚手架（**故意不继承 `unittest.TestCase`** —— 继承了会被整个跑一遍）：

    建一张带 `单号` 的 `erp_sales` 临时库 → 打掉 `find_db` / `load_people`
    （名册走系统人店表会联网，集成测试一律 patch）→ 算一遍。
    `Test新机口径` 和 `Test新机明细下钻` 共用它，免得两份 fixture 走散。
    """

    STORE = "青岛城阳万达店"

    def _build(self, rows, store=None):
        import sqlite3
        store = store or self.STORE
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = sqlite3.connect(path)
        # ⚠ `单号` 在列里 —— 明细下钻要列销售单号（老库缺列走 `'' AS 单号` 兜底）
        conn.execute(
            "CREATE TABLE erp_sales (单号 TEXT, 门店 TEXT, 店员 TEXT, 单据类型 TEXT,"
            " 商品名称 TEXT, 一级分类 TEXT, 二级分类 TEXT, 数量 REAL,"
            " 零售考核毛利 REAL, 支付时间 TEXT, 备注 TEXT, 单行备注 TEXT,"
            " \"客户/顾客\" TEXT, 串号标识 TEXT)")
        for d in rows:
            base = {"单号": "", "门店": store, "店员": "张三", "单据类型": "零售",
                    "商品名称": "智能手机/华为/nova 16", "一级分类": "手机",
                    "二级分类": "手机", "数量": 1, "零售考核毛利": 0,
                    "支付时间": "2026-09-12 10:00:00", "备注": "", "单行备注": "",
                    "客户/顾客": "", "串号标识": "新"}
            base.update(d)
            ks = list(base)
            conn.execute("INSERT INTO erp_sales (%s) VALUES (%s)"
                         % (", ".join('"%s"' % k if "/" in k else k for k in ks),
                            ",".join("?" * len(ks))),
                         [base[k] for k in ks])
        conn.commit()
        conn.close()
        return path

    def _patch(self, path):
        from src.features.valueadd.benefit import compute as bcomp
        swaps = [(bcomp, "find_db", lambda root=None: path),
                 (bcomp, "load_people", lambda *a, **k: ({}, "empty", ""))]
        olds = [(mm, nn, getattr(mm, nn)) for mm, nn, _ in swaps]
        for mm, nn, v in swaps:
            setattr(mm, nn, v)
        self.addCleanup(lambda: [setattr(mm, nn, o) for mm, nn, o in olds])
        bcomp._CACHE.clear()
        self.addCleanup(bcomp._CACHE.clear)
        return bcomp

    def _compute(self, rows, day=None):
        import datetime
        from src.features.valueadd.benefit import compute as bcomp
        day = day or datetime.date(2026, 9, 26)
        self._patch(self._build(rows))
        return bcomp.compute(stores=[self.STORE], day=day)


class Test新机口径(_BenefitDbCase, unittest.TestCase):
    """新机 = **手机零售净 + 美团/抖音转线上**，普通批发分销**不算新机**。

    用户 2026-09-26 拍板：「新机的数据来源按照我刚才跟你说的来就行」——
    即源表口径 (134+16)×0.9 里的 134=手机零售净、16=美团转线上（万达实数）。
    ⚠ 修前 bug：`compute` 对手机行**不分单据类型全算**（含京东/天猫等普通分销），
    万达 194 vs 文档口径 181。
    ⚠ 判「美团/抖音」要同时看**客户/顾客字段**：万达备注含美团=0 台、
    客户=美团外卖=16 台 —— 只看备注会把转线上全漏掉。
    """

    STORE = "青岛城阳万达店"

    def test日期窗口_看上月全月(self):
        """**日期窗口**（2026-09-29 用户：「每月 1 号手动拉上个月全月」）——
        `day=8/31` → 窗口 `8/1 ～ 8/31`、进度封顶 **100%**、`day` 回传 31。"""
        import datetime
        d = self._compute([{}], day=datetime.date(2026, 8, 31))
        self.assertEqual(d["start"], "2026-08-01")
        self.assertEqual(d["end"], "2026-08-31")
        self.assertEqual(d["day"], 31)
        self.assertEqual(d["progress"], 1.0, "8/31 = 整月，进度必须 100%")

    def test进度_截止28日是九三三(self):
        """源表右上角实写 93.3%（= 28/30）—— 旧式 `(day-1)/30` 给的是 90%。"""
        import datetime
        d = self._compute([{}], day=datetime.date(2026, 9, 28))
        self.assertAlmostEqual(d["progress"], 28 / 30.0, places=4)

    def test普通分销不算新机(self):
        d = self._compute([
            {},                                                     # 零售1
            {"单据类型": "分销", "备注": "9.13京东国补订单362",
             "客户/顾客": "京东到家", "支付时间": "2026-09-13 10:00:00"},   # 京东 → 不算
            {"单据类型": "分销", "备注": "转线上",
             "客户/顾客": "天猫商城", "支付时间": "2026-09-14 10:00:00"},    # 天猫 → 不算
        ])
        st = d["stores"][0]
        self.assertEqual(st["new"], 1,
                         "京东/天猫普通分销不该算新机，应只剩零售1台（修前=3）")
        self.assertEqual(st["new_retail"], 1)
        self.assertEqual(st["new_online"], 0)

    def test转线上按客户字段认(self):
        """万达实况：备注只写「转线上」不写美团，美团身份在 客户/顾客 字段。"""
        d = self._compute([
            {},
            {"单据类型": "分销", "备注": "转线上 返顾客250",
             "客户/顾客": "美团外卖", "支付时间": "2026-09-15 10:00:00"},
            {"单据类型": "分销", "备注": "",
             "客户/顾客": "抖音小时达", "支付时间": "2026-09-16 10:00:00"},
        ])
        st = d["stores"][0]
        self.assertEqual(st["new"], 3, "零售 + 美团转线上 + 抖音 = 3")
        self.assertEqual(st["new_retail"], 1)
        self.assertEqual(st["new_online"], 2)

    def test演示机样机不算新机_口径E(self):
        """口径E（用户 2026-09-26 拍板）：商品名含演示机/-演 **或** 串号标识含「样」都排。"""
        d = self._compute([
            {"商品名称": "智能手机/华为/Pura 80 Pro+ 全网通版-演示机",
             "串号标识": "J,新", "支付时间": "2026-09-10 10:00:00"},          # 商品名口径
            {"商品名称": "智能手机/华为/nova 16 普通机", "串号标识": "样,新",
             "支付时间": "2026-09-11 10:00:00"},                              # 标识口径
            {"商品名称": "智能手机/华为/nova 16 正常机", "串号标识": "J,新",
             "支付时间": "2026-09-12 10:00:00"},                              # 正常机 → 留
        ])
        self.assertEqual(d["stores"][0]["new"], 1,
                         "演示机(商品名)与样机(标识)都该排，只剩正常机1台（修前=3）")

    def test送Care机型不算新机(self):
        """折叠屏 care-fold 活动送 Care+ 的机型不算新机；**Pura X View 要算**（活动 exclude）。"""
        d = self._compute([
            {"商品名称": "智能手机/华为/Pura X Max HOP-AL00(12GB+512GB)全网通版-零度白"},
            {"商品名称": "智能手机/华为/Mate X7 DEL-AL10(12GB+512GB) 全网通版 曜石黑",
             "支付时间": "2026-09-13 10:00:00"},
            {"商品名称": "智能手机/华为/Pura X View VOL-AL00(12GB+512GB)全网通版",
             "支付时间": "2026-09-14 10:00:00"},
        ])
        st = d["stores"][0]
        self.assertEqual(st["new"], 1,
                         "X Max / X7 是 care-fold 送 Care+ 机型应剔；View 要留（修前=3）")
        self.assertEqual(d.get("transfer_rows"), 0, "Care+机型是口径剔除，不算转单行")

    def test备注含美团也算(self):
        # 备注里就算写了别家店名也不再剔（2026-09-29：转单算转出店）
        d = self._compute([
            {"单据类型": "分销", "备注": "美团线上下单",
             "客户/顾客": "", "支付时间": "2026-09-17 10:00:00"},
        ])
        self.assertEqual(d["stores"][0]["new_online"], 1,
                         "备注含美团也要认（老口径不倒退）")

    def test备注点名别家店的转单算本店(self):
        """2026-09-29 拍板：转单行**算转出店**（原门店），不再整行剔。

        用户：「算转出店的，因为这个是转出店没有这个线上平台，通过别的店走的量，
        增值业务肯定要算是原门店的销售」—— 剔了会两边都算不着。
        """
        d = self._compute([
            {},
            {"单据类型": "分销", "备注": "顺和汇美团下单",
             "客户/顾客": "美团外卖", "支付时间": "2026-09-18 10:00:00"},
        ])
        st = d["stores"][0]
        self.assertEqual(st["new"], 2,
                         "转单行算转出店 → 零售1 + 转线上1 = 2（修前剔成 1）")
        self.assertEqual(d.get("transfer_rows"), 1, "转单行数要记台账")
        self.assertIn("已计入本店", d.get("note", ""),
                      "note 必须报出台账，别静默（坑13 同类）")

    def test_Care利润只算含Care且含华为路径的行(self):
        """Care+ **利润**只算「商品名称含 `Care+` 且含 `/华为/` 路径」的行
        —— 用户 2026-09-26 拍板（人算 23/26 对上、误差 5377 → 416）。

        剔掉的四类：Mate XT 那种 899 大额延保（名称里没有 `/华为/` 段）、
        延长服务宝（不含 `Care+`）、matepad/电脑那类 Care+（没有 `/华为/`）。
        ⚠ Care+ **达成台量不收紧** —— 人算的件数里含 Mate XT 那台。
        """
        rows = [
            {"一级分类": "手机平板周边", "二级分类": "延保服务",
             "商品名称": "延保服务/华为/Pura X View/HUAWEI Care+(一年期)",
             "零售考核毛利": 69, "支付时间": "2026-09-10 10:00:00"},           # 算
            {"一级分类": "手机平板周边", "二级分类": "延保服务",
             "商品名称": "延保服务/Mate XT 2 | ULTIMATE DESIGN HUAWEI Care+(一年期)",
             "零售考核毛利": 899, "支付时间": "2026-09-11 10:00:00"},          # 无 /华为/ → 利润剔
            {"一级分类": "手机平板周边", "二级分类": "延保服务",
             "商品名称": "延保服务/华为/nova 15 /延长服务宝1年",
             "零售考核毛利": 76, "支付时间": "2026-09-12 10:00:00"},           # 无 Care+ → 利润剔
            {"一级分类": "电脑周边", "二级分类": "延保服务",
             "商品名称": "延保服务/matepad 11.5寸/HUAWEI Care+ 12月",
             "零售考核毛利": 64, "支付时间": "2026-09-13 10:00:00"},           # 无 /华为/ → 利润剔
        ]
        st = self._compute(rows)["stores"][0]
        self.assertAlmostEqual(st["care_profit"], 69, places=6,
                               msg="只有「/华为/ + Care+」那行算利润（修前=1108）")
        self.assertAlmostEqual(st["care"], 4, places=6,
                               msg="达成台量不收紧：4 行都算件数（store_row 里叫 care）")
        # 利润合计跟着 care_profit 走（这批行没有无忧/权益利润）
        self.assertAlmostEqual(st["profit_total"], 69, places=6,
                               msg="利润合计 = 权益利润 + Care+利润 + 无忧利润")


class Test明细下钻(_BenefitDbCase, unittest.TestCase):
    """点门店行上**每个能下钻的数字**弹出来的明细 —— 合计必须等于页面那个数。

    用户 2026-09-29：「每个门店新机销量那个数字，点开要有纳入统计的门店对应的
    销售单号、商品名称和数量，以及销售时间，包含退货。」
    同日追加：「几个具体达成的，点开也显示一下对应的销售单据？」+「金额类也一起做」
    +「带上毛利吧」。
    ⚠ 最要紧的一条：明细走 `row_kind()`、页面也走它 —— 判据写两份就会
      「页面 2 台、明细加出来 3 台」，而这种不一致没人报、只会被业务抓包。
    """

    def _detail_pair(self, rows, store=None, day=None):
        """同一份库上算 **页面** 和 **明细** —— 两边必须对得上。"""
        import datetime
        from src.features.valueadd.benefit import compute as bcomp
        day = day or datetime.date(2026, 9, 26)
        self._patch(self._build(rows, store=store))
        d = bcomp.compute(stores=[store or self.STORE], day=day)
        det = bcomp.drill_rows(store=store or self.STORE, kind="new", day=day)
        return d, det

    def test明细合计等于页面新机数(self):
        rows = [
            {"单号": "A1"},                                          # 零售 +1
            {"单号": "R1", "单据类型": "零售退", "数量": -1,
             "支付时间": "2026-09-13 09:00:00"},                      # 退货 −1
            {"单号": "M1", "单据类型": "分销", "数量": 2, "备注": "美团线上下单",
             "支付时间": "2026-09-14 10:00:00"},                      # 转线上 +2
            {"单号": "J1", "单据类型": "分销", "备注": "9.13京东国补订单",
             "客户/顾客": "京东到家", "支付时间": "2026-09-15 10:00:00"},   # 不算
            {"单号": "D1", "商品名称": "智能手机/华为/Pura 80-演示机",
             "支付时间": "2026-09-16 10:00:00"},                      # 演示机不算
        ]
        d, det = self._detail_pair(rows)
        st = d["stores"][0]
        self.assertAlmostEqual(det["total"], st["new"], places=6,
                               msg="明细合计必须等于页面「新机」——同一个 new_bucket")
        self.assertAlmostEqual(det["shown"], st["new"], places=6,
                               msg="权益页不乘 0.9，shown 就是页面数")
        self.assertEqual(det["factor"], 1.0)
        nos = {r["no"] for r in det["rows"]}
        self.assertEqual(nos, {"A1", "R1", "M1"},
                         "京东分销 / 演示机不该出现在明细里（页面也没算它们）")
        ret = [r for r in det["rows"] if r["no"] == "R1"][0]
        self.assertEqual(ret["qty"], -1, "退货行要进来（负数），合计里已冲减")
        self.assertEqual(ret["typ"], "零售退", "单据类型让退货一眼看得见")
        self.assertEqual(ret["ts"], "2026-09-13 09:00:00")
        self.assertTrue(ret["name"], "商品名称要带上")

    def test只回本店的单(self):
        import datetime
        rows = [{"单号": "A1", "门店": "甲店"},
                {"单号": "B1", "门店": "乙店"}]
        from src.features.valueadd.benefit import compute as bcomp
        self._patch(self._build(rows, store="甲店"))
        day = datetime.date(2026, 9, 26)
        det_a = bcomp.drill_rows(store="甲店", kind="new", day=day)
        det_b = bcomp.drill_rows(store="乙店", kind="new", day=day)
        self.assertEqual({r["no"] for r in det_a["rows"]}, {"A1"})
        self.assertEqual({r["no"] for r in det_b["rows"]}, {"B1"},
                         "查乙店只能拿到乙店的单 —— 别把别家的销售单摊出来")

    def test没给门店就报错不是空成功(self):
        """空店名 = 「没指定门店」—— **不许当成"随便哪家"去查**（坑同类）。"""
        import datetime
        from src.features.valueadd.benefit import compute as bcomp
        self._patch(self._build([{"单号": "A1"}]))
        d = bcomp.drill_rows(store="", kind="new", day=datetime.date(2026, 9, 26))
        self.assertFalse(d.get("ok"))
        self.assertIn("门店", d.get("why") or "")


    def test_不认识的指标回错不是空成功(self):
        """`kind` 打错 → 400 那条路的源头：**别默默回一张空表**。"""
        from src.features.valueadd.benefit import compute as bcomp
        self._patch(self._build([{"单号": "A1"}]))
        d = bcomp.drill_rows(store=self.STORE, kind="nope")
        self.assertFalse(d.get("ok"))
        self.assertIn("指标", d.get("why") or "")

    def test_达成与利润那些格的明细也对得上(self):
        """用户 2026-09-29：「几个具体达成的，点开也显示一下对应的销售单据」
        +「金额类也一起做」+「带上毛利吧」。

        ⚠ 关键那条：**Care+ 件数认全部延保行、利润只认 `care_profit_ok` 的行** ——
          页面本来就这么算的；金额类明细不按这个收，毛利合计就会跟页面对不上。
        """
        import datetime
        from src.features.valueadd.benefit import compute as bcomp
        day = datetime.date(2026, 9, 26)
        rows = [
            {"单号": "W1", "一级分类": "会员", "二级分类": "会员",
             "商品名称": "无忧会员 优选版本-二年299元（新）", "数量": 1,
             "零售考核毛利": 100, "支付时间": "2026-09-10 10:00:00"},
            {"单号": "C1", "一级分类": "手机平板周边", "二级分类": "延保服务",
             "商品名称": "延保服务/华为/nova 16/HUAWEI Care+(一年期)", "数量": 1,
             "零售考核毛利": 69, "支付时间": "2026-09-11 10:00:00"},
            {"单号": "C2", "一级分类": "手机平板周边", "二级分类": "延保服务",
             "商品名称": "延保服务/Mate XT 2 | ULTIMATE DESIGN HUAWEI Care+(一年期)",
             "数量": 1, "零售考核毛利": 899, "支付时间": "2026-09-12 10:00:00"},
            {"单号": "A1", "支付时间": "2026-09-13 10:00:00"},   # 新机，不该混进来
        ]
        self._patch(self._build(rows))
        d = bcomp.compute(stores=[self.STORE], day=day)
        st = d["stores"][0]
        cases = [
            ("wuyou", st["wuyou"], {"W1"}),
            ("care", st["care"], {"C1", "C2"}),
            ("total", st["total"], {"W1", "C1", "C2"}),
            ("tier_profit", st["tier_profit"], {"W1"}),
            ("care_profit", st["care_profit"], {"C1"}),
        ]
        for kind, want, nos in cases:
            det = bcomp.drill_rows(store=self.STORE, kind=kind, day=day)
            self.assertTrue(det["ok"], (kind, det))
            self.assertAlmostEqual(det["total"], want, places=6,
                                   msg="kind=%s 的合计要等于页面那一格" % kind)
            self.assertEqual({r["no"] for r in det["rows"]}, nos, kind)
            for r in det["rows"]:
                self.assertIn("profit", r, "明细行要带毛利列（用户：带上毛利吧）")
        self.assertEqual(st["care"], 2, "件数认全部延保服务行")
        self.assertAlmostEqual(st["care_profit"], 69, places=6,
                               msg="利润只认「含 Care+ 且含 /华为/」的行 —— C2 不算")
        # ⚠ 「利润合计」**故意不可下钻**：它是 后返+权益利润+Care+利润，
        #   后返是台数×单价（这里 100）不是毛利 —— 硬做会 269≠169 对不上。
        self.assertNotIn("profit_total", bcomp.DRILL_KINDS)
        self.assertAlmostEqual(st["profit_total"], 269, places=6,
                               msg="100 后返 + 100 权益 + 69 Care+（它含后返）")


if __name__ == "__main__":
    unittest.main()
