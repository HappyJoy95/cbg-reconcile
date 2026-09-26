# -*- coding: utf-8 -*-
"""无忧会员权益（汇机保）的**口径**（纯函数、零 IO）。

⚠ 公式怎么定稿的**开发逆推稿不进正式包**（只留在仓库 `.dsh/`，打包已排除）。
   下面只保留**运行时要用的口径**，不引用桌面 Excel 文件名。

```
无忧6档   = 优选/超值/全能进阶/旗舰顶配 + 399/499防护套装
Care+     = 二级分类「延保服务」（净件数）
新机      = 手机零售净 + 分销净·备注含美团/抖音   ← **不乘 0.9**（与防护膜页不同）

店：目标 = 台量进度×0.2；总达成率 = 新机达成×0.3 + 连带达成×0.7
    后返 = 6档后返（不含 Care+）；台均利润 = (权益毛利+Care+毛利)/新机
区：目标 = 新机×0.15；达成率分子只算无忧（不含 Care+）
人：后返 = 6档后返 + Care+×150；奖金 = 利润合计×0.1
    月度奖金增收 = 奖金/7×36
赛道：前三按 50/30/20 分池，总达成率≥90% 才发；店级连带<50% 罚 300
```

配置（赛道/目标/区域）：`config/valueadd-benefit.yaml`，
缺失时回落 `DEFAULT_CONFIG`。
⚠ **店员名册不以 yaml 为准** —— 主源是系统人店表
  （`features.store.staff.rosters_by_store`），见 `compute.load_people`。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

#: 六档：(key, 中文列名, 名称匹配片段, 店/人后返单价)
TIERS: Tuple[Tuple[str, str, str, int], ...] = (
    ("opt", "优选版本", "无忧会员 优选", 100),
    ("sup", "超值版本", "无忧会员 超值", 100),
    ("adv", "全能进阶", "无忧会员 全能进阶", 150),
    ("max", "旗舰顶配", "无忧会员 旗舰顶配", 150),
    ("p399", "399防护套装", "399元轻奢防护套装", 100),
    ("p499", "499防护套装", "499元全能尊享套装", 150),
)

#: Care+ 人员后返单价（源表公式有；**不设每店 4 单门槛** —— 用户 2026-09-22）。
CARE_REBATE = 150

#: 参与计件的单据类型（与源表一致：核销不算）。
SELL_TYPES = ("零售", "零售退", "分销", "分销退")

#: 新机：手机零售 + 分销里备注含美团/抖音。
PHONE_C1 = "手机"

#: 店长赛道池（可被配置覆盖）。
DEFAULT_POOLS = {"A": 2000, "B": 1500, "C": 1000, "D": 500}

#: 赛道前三分配比例（按名次 1..3）。
TOP3_SHARE = (0.5, 0.3, 0.2)

#: 前三名各自总达成率 ≥ 此值才发该名次的份额；不足则公司收回。
TOP3_GATE = 0.90

#: 赛道（合计）**连带达成率**（合计达成 ÷ 目标）< 此值 ⇒ 该赛道负激励（元）。
#: ⚠ 源表政策写「连带率低于50%」——若按 合计/新机（常年 ~15%）会**永远触发**；
#:   结合列名与量级，这里取 **连带达成率**（源表 M 列，店级样例 0.76）。
#: ⚠ 300 是**每赛道一笔**（不是每位店长各扣 300）—— 从该赛道已发份额里扣。
TRACK_ATTACH_PENALTY = 0.50
TRACK_ATTACH_FINE = 300

#: 月度奖金增收折算（源表 `=奖金/7*36`）。
BONUS_MONTH_K = 36.0 / 7.0

#: 时间进度：与源表 `台量目标/30*(DAY(TODAY())-1)` 同口径。
DAYS_IN_MONTH = 30.0

# ---------------------------------------------------------------------------
# 配置（默认 = 9 月源表；运行时由 compute 用 yaml 覆盖）
# ---------------------------------------------------------------------------

#: 内置默认赛道：{track: {"pool": int, "stores": {店名: 台量目标}}}
DEFAULT_TRACKS: Dict[str, dict] = {
    "A": {"pool": 2000, "stores": {
        "青岛城阳万达店": 224.4, "青岛城阳万象汇店": 216.7, "青岛胶州龙湖店": 182.05,
        "青岛新业广场店": 172.15, "青岛悦荟店": 186.45, "青岛丽达茂店": 192.5,
        "胶南合美MALL店": 131.45}},
    "B": {"pool": 1500, "stores": {
        "青岛城阳家佳源店": 141.35, "青岛CBD万达店": 91.85, "黄岛传媒广场店": 81.4,
        "上街里容滙城店": 86.35, "市南金茂湾店": 99.55, "青岛永旺东部店": 129.8}},
    "C": {"pool": 1000, "stores": {
        "青岛正阳路利客来店": 77.55, "青岛顺和汇店": 77.0, "青岛永旺合肥路店": 81.4,
        "青岛麦凯乐店": 55.0, "青岛海信广场店": 69.85, "深蓝中心店": 55.55,
        "绿城丽达店": 50.6}},
    "D": {"pool": 500, "stores": {
        "城阳首创奥莱店": 26.4, "城阳大润发店": 26.95, "青岛海信国际中心店": 15.95,
        "和达购物中心店": 23.1, "青岛市北家佳源店": 27.5, "胶南泊里镇店": 25.85,
        "青岛鲁疆广场店": 19.25}},
}

#: 内置默认区域（区长 PK 分组）。
DEFAULT_REGIONS: Dict[str, List[str]] = {
    "城阳胶州": [
        "青岛城阳万达店", "青岛城阳万象汇店", "青岛城阳家佳源店", "城阳首创奥莱店",
        "城阳大润发店", "青岛正阳路利客来店", "青岛胶州龙湖店", "胶州李哥庄店",
    ],
    "市区": [
        "青岛新业广场店", "青岛悦荟店", "青岛CBD万达店", "青岛永旺东部店",
        "青岛海信广场店", "青岛麦凯乐店", "青岛海信国际中心店", "深蓝中心店",
        "青岛顺和汇店", "青岛丽达茂店", "青岛永旺合肥路店", "和达购物中心店",
        "绿城丽达店", "青岛市北家佳源店",
    ],
    "黄岛": [
        "胶南合美MALL店", "黄岛传媒广场店", "上街里容滙城店", "市南金茂湾店",
        "胶南泊里镇店", "青岛鲁疆广场店",
    ],
}

#: ⚠ **没有内置人员名册**（2026-09-22）—— 名册只认系统人店表，
#:   禁止再从用户 Excel / 写死名单读人（业务数只认 fetch 的 sqlite）。
DEFAULT_PEOPLE: List[Tuple[str, str, str]] = []


def default_config() -> dict:
    """内置默认：只有赛道/区域；**没有 people**（名册走系统人店表）。"""
    return {
        "tracks": DEFAULT_TRACKS,
        "regions": DEFAULT_REGIONS,
        "people": [],
    }


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

def tier_of(name: str) -> Optional[str]:
    """商品名 → 六档 key；都不是就 None（贴膜礼包 / 别的无忧险都落这）。"""
    name = name or ""
    for key, _label, frag, _p in TIERS:
        if frag in name:
            return key
    return None


def is_care(c2: str) -> bool:
    return (c2 or "") == "延保服务"


def is_phone(c1: str) -> bool:
    return (c1 or "") == PHONE_C1


def is_meituan(note: str) -> bool:
    n = note or ""
    return ("美团" in n) or ("抖音" in n)


def is_demo(name: str, sid: str) -> bool:
    """演示机/样机 —— 用户 2026-09-26 拍板口径 **E：两种都排**。

    * 商品名称含「演示」「样机」或以「-演」结尾（商品名口径，覆盖 12/12 台）；
    * 串号标识含「样」（`样,新` / `s,样,新`，逗号分段精确比，避免误伤）。
    ⚠ 两种口径不一致：商品名 12 台 ⊃ 标识 6 台（麦凯乐3台标识是 `J,新`/`新`）——
      E = 并集，万达 143 与麦凯乐 36 里麦凯乐会排掉3台（人算没排，差异已知）。
    """
    n = name or ""
    s = sid or ""
    if ("演示" in n) or ("样机" in n) or ("-演" in n):
        return True
    return "样" in [x.strip() for x in s.split(",")]


def is_online_cust(cust: str) -> bool:
    """`客户/顾客` 字段是不是线上平台（美团外卖 / 抖音小时达 …）。

    ⚠ 必须和 `is_meituan` 一起用（用户 2026-09-26 定的「美团转线上」口径）：
      万达实况是**备注只写「转线上」、平台身份在客户字段**（备注含美团=0台、
      客户=美团外卖=16台）—— 只看备注会把转线上全漏掉。
    """
    c = cust or ""
    return ("美团" in c) or ("抖音" in c)


def sell_ok(typ: str) -> bool:
    return (typ or "") in SELL_TYPES


def rebate_unit(tier_key: str) -> int:
    for k, _l, _f, p in TIERS:
        if k == tier_key:
            return p
    return 0


# ---------------------------------------------------------------------------
# 行指标
# ---------------------------------------------------------------------------

def _div(a: float, b: float) -> float:
    return (a / b) if b else 0.0


def store_row(
    store: str,
    *,
    track: str = "",
    day_target: float = 0.0,
    region: str = "",
    new_retail: float = 0.0,
    new_online: float = 0.0,
    tiers: Optional[Dict[str, float]] = None,
    tier_profit: float = 0.0,
    care_qty: float = 0.0,
    care_profit: float = 0.0,
    day: Optional[int] = None,
) -> Dict:
    """门店一行。`tiers` = {档key: 净件数}；`day`=今天几号（缺省 1 ⇒ 进度 0）。"""
    tiers = tiers or {}
    new = new_retail + new_online
    progress_days = float(day if day is not None else 1)
    time_progress = max(0.0, min(1.0, (progress_days - 1) / DAYS_IN_MONTH))
    slot_progress = day_target * time_progress
    new_rate = _div(new, slot_progress)
    wuyou = float(sum(tiers.get(k, 0.0) for k, _l, _f, _p in TIERS))
    care = float(care_qty)
    total = wuyou + care
    goal = slot_progress * 0.2
    attach = _div(total, new)
    attach_goal_rate = _div(total, goal)
    overall = new_rate * 0.3 + attach_goal_rate * 0.7
    rebate = float(sum(
        tiers.get(k, 0.0) * p for k, _l, _f, p in TIERS
    ))
    profit_total = rebate + care_profit + tier_profit
    return {
        "store": store,
        "track": track,
        "region": region,
        "day_target": day_target,
        "slot_progress": round(slot_progress, 4),
        "new_retail": new_retail,
        "new_online": new_online,
        "new": new,
        "new_rate": new_rate,
        "goal": round(goal, 4),
        "wuyou": wuyou,
        "care": care,
        "total": total,
        "attach": attach,
        "attach_goal_rate": attach_goal_rate,
        "overall": overall,
        "tiers": {k: float(tiers.get(k, 0.0)) for k, _l, _f, _p in TIERS},
        "tier_count": wuyou,
        "rebate": rebate,
        "care_profit": care_profit,
        "tier_profit": tier_profit,
        "profit_total": profit_total,
        "avg_profit": _div(tier_profit + care_profit, new),
    }


def region_row(
    region: str,
    *,
    stores: List[dict],
) -> Dict:
    """区域一行：聚合下属店再套区域口径（目标=新机×0.15）。"""
    new = sum(s.get("new") or 0 for s in stores)
    wuyou = sum(s.get("wuyou") or 0 for s in stores)
    care = sum(s.get("care") or 0 for s in stores)
    total = wuyou + care
    goal = new * 0.15
    tiers = {
        k: sum((s.get("tiers") or {}).get(k, 0.0) for s in stores)
        for k, _l, _f, _p in TIERS
    }
    rebate = float(sum(tiers[k] * rebate_unit(k) for k in tiers))
    tier_profit = sum(s.get("tier_profit") or 0 for s in stores)
    care_profit = sum(s.get("care_profit") or 0 for s in stores)
    profit_total = rebate + tier_profit          # 源表区域利润**不含** Care+
    return {
        "region": region,
        "stores": len(stores),
        "new": new,
        "goal": round(goal, 4),
        "wuyou": wuyou,
        "care": care,
        "total": total,
        "attach": _div(total, new),
        "goal_rate": _div(wuyou, goal),         # 分子只算无忧
        "tiers": tiers,
        "tier_count": wuyou,
        "rebate": rebate,
        "tier_profit": tier_profit,
        "profit_total": profit_total,
        "avg_profit": _div(tier_profit, new),
    }


def person_row(
    store: str,
    name: str,
    title: str,
    *,
    new_retail: float = 0.0,
    new_online: float = 0.0,
    tiers: Optional[Dict[str, float]] = None,
    tier_profit: float = 0.0,
    care_qty: float = 0.0,
    care_profit: float = 0.0,
) -> Dict:
    """人员一行（源表《跟进表》）。`合计达成` = **仅 6 档**（Care+ 单列）。"""
    tiers = tiers or {}
    new = new_retail + new_online
    wuyou = float(sum(tiers.get(k, 0.0) for k, _l, _f, _p in TIERS))
    care = float(care_qty)
    rebate = float(sum(tiers.get(k, 0.0) * p for k, _l, _f, p in TIERS))
    rebate += care * CARE_REBATE
    profit_total = rebate + care_profit + tier_profit
    bonus = profit_total * 0.1
    return {
        "store": store,
        "name": name,
        "title": title,
        "new_retail": new_retail,
        "new_online": new_online,
        "new": new,
        "tiers": {k: float(tiers.get(k, 0.0)) for k, _l, _f, _p in TIERS},
        "care": care,
        "total": wuyou,                     # 源表：合计达成不含 Care+
        "attach_ratio": _div(wuyou, new),   # 配比率
        "rebate": rebate,
        "care_profit": care_profit,
        "tier_profit": tier_profit,
        "profit_total": profit_total,
        "avg_profit": _div(tier_profit, new),  # 台均：只除权益
        "bonus": bonus,
        "bonus_month": bonus * BONUS_MONTH_K,
    }


def rank_people(rows: List[dict]) -> List[dict]:
    """按奖金降序贴排名（同奖同名次用竞赛排名；源表是顺序贴的，我们现算）。"""
    ordered = sorted(rows, key=lambda r: (-(r.get("bonus") or 0.0),
                                          str(r.get("store") or ""),
                                          str(r.get("name") or "")))
    prev = None
    rank = 0
    for i, r in enumerate(ordered, 1):
        b = r.get("bonus") or 0.0
        if prev is None or b != prev:
            rank = i
            prev = b
        r["rank"] = rank
    return ordered


# ---------------------------------------------------------------------------
# 赛道店长奖金
# ---------------------------------------------------------------------------

def allocate_track(
    track: str,
    pool: float,
    stores: List[dict],
) -> dict:
    """一个赛道的店长分配。

    * 按 **总达成率** 降序取前三；
    * 各名次 `总达成率 ≥ TOP3_GATE` 才给 `pool × 份额`，否则该份**收回**；
    * 赛道（合计）连带率 < 50% ⇒ 每位入围店长**负激励 300**
      （源表：「每个赛道连带率低于50%负激励300元」—— 挂在店长头上）。

    `stores` 每行至少要 `store` / `overall` / `attach`。
    返回 `{track, pool, rows:[{store, rank, share, paid, overall, ...}], fined, ...}`。
    """
    ranked = sorted(
        [s for s in stores if s.get("track") == track or track == ""],
        key=lambda s: (-(s.get("overall") or 0.0), str(s.get("store") or "")),
    )
    # 若调用方没打 track 标，就用传入列表原样（compute 会按赛道先筛好）
    if not any(s.get("track") for s in stores):
        ranked = sorted(stores, key=lambda s: (-(s.get("overall") or 0.0),
                                               str(s.get("store") or "")))

    total_new = sum(s.get("new") or 0 for s in stores)
    total_total = sum(s.get("total") or 0 for s in stores)
    total_goal = sum(s.get("goal") or 0 for s in stores)
    track_attach = _div(total_total, total_new)
    # 负激励看**连带达成率**（合计/目标），不是 合计/新机
    track_attach_goal = _div(total_total, total_goal)
    fine = total_goal > 0 and track_attach_goal < TRACK_ATTACH_PENALTY

    top = ranked[:3]
    rows = []
    for i, s in enumerate(top):
        share = pool * TOP3_SHARE[i]
        ok = (s.get("overall") or 0.0) >= TOP3_GATE
        rows.append({
            "store": s.get("store") or "",
            "rank": i + 1,
            "overall": s.get("overall") or 0.0,
            "attach": s.get("attach") or 0.0,
            "share": share,
            "gate_ok": ok,
            "paid": share if ok else 0.0,
            "fine": 0,
        })
    # 先按门槛发，再整赛道扣一笔 300（从已发的里面扣；不够发就扣到 0）
    paid_sum = sum(r["paid"] for r in rows)
    fine_paid = 0.0
    if fine and paid_sum > 0:
        fine_paid = min(TRACK_ATTACH_FINE, paid_sum)
        # 平摊到最后有份额的人头上（通常第一名）
        left = fine_paid
        for r in reversed(rows):
            if left <= 0:
                break
            take = min(r["paid"], left)
            r["paid"] -= take
            r["fine"] = take
            left -= take
        paid_sum -= fine_paid
    return {
        "track": track,
        "pool": pool,
        "attach": track_attach,
        "attach_goal": track_attach_goal,
        "fined": fine,
        "fine": fine_paid,
        "paid": paid_sum,
        "recycled": pool - paid_sum,
        "rows": rows,
    }


def allocate_all_tracks(tracks_cfg: dict, store_rows: List[dict]) -> List[dict]:
    """全部赛道。`tracks_cfg` = `{A: {pool, stores}|pool}` 形态均可。"""
    out = []
    for name in sorted(tracks_cfg.keys()):
        cfg = tracks_cfg[name] or {}
        pool = float(cfg.get("pool", DEFAULT_POOLS.get(name, 0)) if isinstance(cfg, dict) else cfg)
        members = [r for r in store_rows if r.get("track") == name]
        out.append(allocate_track(name, pool, members))
    return out


def summarize_stores(rows: List[dict]) -> dict:
    """门店合计 —— 比率用合计重算。"""
    if not rows:
        return store_row("合计")
    new = sum(r.get("new") or 0 for r in rows)
    nr = sum(r.get("new_retail") or 0 for r in rows)
    no = sum(r.get("new_online") or 0 for r in rows)
    wuyou = sum(r.get("wuyou") or 0 for r in rows)
    care = sum(r.get("care") or 0 for r in rows)
    total = wuyou + care
    goal = sum(r.get("goal") or 0 for r in rows)
    slot = sum(r.get("slot_progress") or 0 for r in rows)
    day_t = sum(r.get("day_target") or 0 for r in rows)
    tiers = {
        k: sum((r.get("tiers") or {}).get(k, 0.0) for r in rows)
        for k, _l, _f, _p in TIERS
    }
    rebate = float(sum(tiers[k] * rebate_unit(k) for k in tiers))
    tp = sum(r.get("tier_profit") or 0 for r in rows)
    cp = sum(r.get("care_profit") or 0 for r in rows)
    new_rate = _div(new, slot)
    attach_goal = _div(total, goal)
    return {
        "store": "合计",
        "track": "",
        "region": "",
        "day_target": day_t,
        "slot_progress": slot,
        "new_retail": nr,
        "new_online": no,
        "new": new,
        "new_rate": new_rate,
        "goal": goal,
        "wuyou": wuyou,
        "care": care,
        "total": total,
        "attach": _div(total, new),
        "attach_goal_rate": attach_goal,
        "overall": new_rate * 0.3 + attach_goal * 0.7,
        "tiers": tiers,
        "tier_count": wuyou,
        "rebate": rebate,
        "care_profit": cp,
        "tier_profit": tp,
        "profit_total": rebate + cp + tp,
        "avg_profit": _div(tp + cp, new),
    }


def summarize_people(rows: List[dict]) -> dict:
    if not rows:
        return {"store": "合计", "name": "合计", "title": "",
                "new": 0, "total": 0, "care": 0, "rebate": 0.0,
                "care_profit": 0.0, "tier_profit": 0.0, "profit_total": 0.0,
                "bonus": 0.0, "bonus_month": 0.0, "avg_profit": 0.0,
                "attach_ratio": 0.0, "rank": "",
                "tiers": {k: 0 for k, _l, _f, _p in TIERS}}
    new = sum(r.get("new") or 0 for r in rows)
    tp = sum(r.get("tier_profit") or 0 for r in rows)
    cp = sum(r.get("care_profit") or 0 for r in rows)
    rb = sum(r.get("rebate") or 0 for r in rows)
    bonus = sum(r.get("bonus") or 0 for r in rows)
    total = sum(r.get("total") or 0 for r in rows)
    care = sum(r.get("care") or 0 for r in rows)
    tiers = {
        k: sum((r.get("tiers") or {}).get(k, 0.0) for r in rows)
        for k, _l, _f, _p in TIERS
    }
    return {
        "store": "合计",
        "name": "合计",
        "title": "",
        "new_retail": sum(r.get("new_retail") or 0 for r in rows),
        "new_online": sum(r.get("new_online") or 0 for r in rows),
        "new": new,
        "tiers": tiers,
        "care": care,
        "total": total,
        "attach_ratio": _div(total, new),
        "rebate": rb,
        "care_profit": cp,
        "tier_profit": tp,
        "profit_total": rb + cp + tp,
        "avg_profit": _div(tp, new),
        "bonus": bonus,
        "bonus_month": bonus * BONUS_MONTH_K,
        "rank": "",
    }


def summarize_regions(rows: List[dict]) -> dict:
    if not rows:
        return {"region": "合计", "stores": 0, "new": 0, "goal": 0,
                "wuyou": 0, "care": 0, "total": 0, "attach": 0,
                "goal_rate": 0, "rebate": 0, "tier_profit": 0,
                "profit_total": 0, "avg_profit": 0,
                "tiers": {k: 0 for k, _l, _f, _p in TIERS}, "tier_count": 0}
    new = sum(r.get("new") or 0 for r in rows)
    wuyou = sum(r.get("wuyou") or 0 for r in rows)
    care = sum(r.get("care") or 0 for r in rows)
    goal = sum(r.get("goal") or 0 for r in rows)
    tiers = {
        k: sum((r.get("tiers") or {}).get(k, 0.0) for r in rows)
        for k, _l, _f, _p in TIERS
    }
    rebate = sum(r.get("rebate") or 0 for r in rows)
    tp = sum(r.get("tier_profit") or 0 for r in rows)
    return {
        "region": "合计",
        "stores": sum(r.get("stores") or 0 for r in rows),
        "new": new,
        "goal": goal,
        "wuyou": wuyou,
        "care": care,
        "total": wuyou + care,
        "attach": _div(wuyou + care, new),
        "goal_rate": _div(wuyou, goal),
        "tiers": tiers,
        "tier_count": wuyou,
        "rebate": rebate,
        "tier_profit": tp,
        "profit_total": rebate + tp,
        "avg_profit": _div(tp, new),
    }


def merge_config(base: dict, over: Optional[dict]) -> dict:
    """yaml 覆盖内置默认（浅合并 tracks/regions，people 整表替换）。"""
    if not over:
        return base
    out = {
        "tracks": dict(base.get("tracks") or {}),
        "regions": dict(base.get("regions") or {}),
        "people": list(base.get("people") or []),
    }
    if isinstance(over.get("tracks"), dict):
        for k, v in over["tracks"].items():
            out["tracks"][k] = v
    if isinstance(over.get("regions"), dict):
        for k, v in over["regions"].items():
            out["regions"][k] = v
    if isinstance(over.get("people"), list) and over["people"]:
        out["people"] = over["people"]
    return out


def track_of(store: str, tracks: dict) -> Tuple[str, float]:
    """店 → (赛道, 台量目标)；不在表里回 ("", 0)。"""
    for name, cfg in (tracks or {}).items():
        stores = (cfg or {}).get("stores") if isinstance(cfg, dict) else None
        if isinstance(stores, dict) and store in stores:
            try:
                return str(name), float(stores[store])
            except (TypeError, ValueError):
                return str(name), 0.0
    return "", 0.0


def region_of(store: str, regions: dict) -> str:
    for name, members in (regions or {}).items():
        if store in (members or []):
            return str(name)
    return ""


__all__ = [
    "TIERS", "CARE_REBATE", "SELL_TYPES", "DEFAULT_TRACKS", "DEFAULT_REGIONS",
    "is_online_cust", "is_demo",
    "DEFAULT_PEOPLE", "DEFAULT_POOLS", "TOP3_SHARE", "TOP3_GATE",
    "TRACK_ATTACH_PENALTY", "TRACK_ATTACH_FINE", "BONUS_MONTH_K",
    "tier_of", "is_care", "is_phone", "is_meituan", "sell_ok", "rebate_unit",
    "store_row", "region_row", "person_row", "rank_people",
    "allocate_track", "allocate_all_tracks",
    "summarize_stores", "summarize_people", "summarize_regions",
    "merge_config", "track_of", "region_of", "default_config",
]
