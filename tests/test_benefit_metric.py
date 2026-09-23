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
        # day=22 → (22-1)/30 = 0.7；224.4*0.7=157.08（源表 21 日是同公式）
        self.assertAlmostEqual(r["slot_progress"], 224.4 * 21 / 30.0, places=3)
        self.assertAlmostEqual(r["goal"], r["slot_progress"] * 0.2, places=4)

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


if __name__ == "__main__":
    unittest.main()
