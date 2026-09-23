# -*- coding: utf-8 -*-
"""防护膜达成的**口径**（纯函数、零 IO）。

⚠ 定稿逆推稿**不进正式包**（仓库 `.dsh/` 内部文档，打包已排除）。
   下面只保留运行时公式，不引用桌面 Excel 文件名。

```
新机     = (手机零售净 + 手机分销净·备注含美团) × 0.9
目标     = 新机 / 2
达成     = 贴膜去软膜 · 零售+分销(±退) · 净件数
跟机率   = 达成 / 新机
毛利目标 = 新机 × 台均基线
零售毛利 = 上述贴膜范围的零售考核毛利
礼包毛利 = 白名单品名的零售考核毛利
总毛利   = 零售毛利 + 礼包毛利
单张利润 = 零售毛利 / 达成
台均增值 = 总毛利 / 新机
毛利达成率 = 总毛利 / 毛利目标
礼包套餐 = 新机 × 0.2
礼包达成率 = 礼包达成 / 礼包套餐
总达成率 = 毛利达成率×0.5 + 礼包达成率×0.5
```

⚠ **京东分销不算新机**（用户 2026-09-22 定）—— 京东比价在零售里、本来就算。
⚠ ×0.9 为渠道折算系数（含义未书面确认，运行时按此算）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

#: 源表排掉的软膜品名（整串相等才排，照 conclusion.py）。
SOFT = "手机贴膜/华为/高透软膜[基础款]"

#: 参与「达成 / 零售毛利」的单据类型（含分销，**不含核销**）。
SELL = ("零售", "零售退", "分销", "分销退")

#: 新机里的渠道折算系数（源表 `新机数据!J` 的 `*0.9`）。
NEW_FACTOR = 0.9

#: 礼包白名单 —— **品名前缀**（源表 9 个指定品名；编码白名单要不到就先用这个）。
GIFT_WL = (
    "178直面屏钢化膜礼包",
    "99元光固电镀膜礼包",
    "499元全能尊享套装权益",
    "298元曲屏钢化膜礼包",
    "99元手表防护膜礼包",
    "238元直面屏钢化膜礼包",
    "398曲屏钢化膜礼包",
    "298元2次钢化膜礼包（直屏）",
    "99元2次UV膜礼包",
)

#: 台均利润基线 —— 源表**人填**的按店档位（25/30/35/40）。
#: ⚠ 本版内置；换店/改档要改这里（开发目标「本版不做的」）。
DEFAULT_BASELINE = 30
BASELINE = {
    "青岛城阳万达店": 35, "青岛城阳万象汇店": 40, "青岛城阳家佳源店": 30,
    "城阳首创奥莱店": 25, "城阳大润发店": 25, "青岛正阳路利客来店": 25,
    "青岛胶州龙湖店": 35, "胶州李哥庄店": 25, "青岛新业广场店": 40,
    "青岛悦荟店": 35, "青岛CBD万达店": 35, "青岛永旺东部店": 40,
    "青岛海信广场店": 35, "青岛麦凯乐店": 30, "青岛海信国际中心店": 25,
    "深蓝中心店": 40, "青岛顺和汇店": 35, "青岛丽达茂店": 35,
    "青岛永旺合肥路店": 30, "和达购物中心店": 30, "绿城丽达店": 25,
    "青岛市北家佳源店": 25, "胶南合美MALL店": 40, "黄岛传媒广场店": 40,
    "上街里容滙城店": 35, "市南金茂湾店": 35, "胶南泊里镇店": 25,
    "青岛鲁疆广场店": 25,
}


def baseline_of(store: str) -> int:
    return BASELINE.get(store, DEFAULT_BASELINE)


def is_gift(name: str) -> bool:
    """礼包白名单（前缀匹配；pura70pro 那笔**不在**名单里）。"""
    return any(name.startswith(w) or w in name for w in GIFT_WL)


def row(
    store: str,
    new_retail: float,
    new_meituan: float,
    film_qty: float,
    film_profit: float,
    gift_qty: float,
    gift_profit: float,
    name: Optional[str] = None,
) -> Dict:
    """一行门店（或人）的全部指标。`new_*` 是**未乘 0.9** 的原始台数。

    ⚠ 门店 / 人**同一个公式**（2026-09-22 用户：「算法和现在也是一样，
      根据个人新机销售的台量的系数推算目标」）⇒ 人那几行加起来 = 店里那一行。
    ⚠ 人也吃**本店** `baseline`（台均基线是店档，不是人档）。
    `name` 非空 = 人行（回传 `name` 给前端拆到人用）。
    """
    new = (new_retail + new_meituan) * NEW_FACTOR
    target = new / 2.0
    base = baseline_of(store)
    profit_target = new * base
    total = film_profit + gift_profit
    pkg = new * 0.2
    gift_rate = (gift_qty / pkg) if pkg else 0.0
    profit_rate = (total / profit_target) if profit_target else 0.0
    out = {
        "store": store,
        "baseline": base,
        "new_retail": new_retail,
        "new_meituan": new_meituan,
        "new": round(new, 2),
        "target": round(target, 2),
        "done": film_qty,
        "attach": (film_qty / new) if new else 0.0,
        "profit_target": round(profit_target, 2),
        "film_profit": round(film_profit, 2),
        "gift_profit": round(gift_profit, 2),
        "total_profit": round(total, 2),
        "unit_profit": (film_profit / film_qty) if film_qty else 0.0,
        "avg_addon": (total / new) if new else 0.0,
        "profit_rate": profit_rate,
        "gift_pkg": round(pkg, 2),
        "gift_done": gift_qty,
        "gift_rate": gift_rate,
        "total_rate": profit_rate * 0.5 + gift_rate * 0.5,
    }
    if name is not None:
        out["name"] = name
    return out


def summarize(rows: List[dict]) -> dict:
    """合计 —— 比率类**用合计数重算**，不拿各行比率去平均。"""
    def s(k):
        return sum(r.get(k) or 0 for r in rows)

    new, done = s("new"), s("done")
    ft, tt = s("film_profit"), s("total_profit")
    pt, pkg, gq = s("profit_target"), s("gift_pkg"), s("gift_done")
    pr = (tt / pt) if pt else 0.0
    gr = (gq / pkg) if pkg else 0.0
    return {
        "store": "合计",
        "new_retail": s("new_retail"),
        "new_meituan": s("new_meituan"),
        "new": round(new, 2),
        "target": round(s("target"), 2),
        "done": done,
        "attach": (done / new) if new else 0.0,
        "profit_target": round(pt, 2),
        "film_profit": round(ft, 2),
        "gift_profit": s("gift_profit"),
        "total_profit": round(tt, 2),
        "unit_profit": (ft / done) if done else 0.0,
        "avg_addon": (tt / new) if new else 0.0,
        "baseline": (pt / new) if new else 0.0,
        "profit_rate": pr,
        "gift_pkg": round(pkg, 2),
        "gift_done": gq,
        "gift_rate": gr,
        "total_rate": pr * 0.5 + gr * 0.5,
    }
