# -*- coding: utf-8 -*-
"""无忧会员权益 —— 导出 Excel（四张表：门店 / 区域 / 人员 / 赛道奖金）。

⚠ 写盘不在这儿：调 `modules.notify.export_xlsx`。
⚠ 版式对齐中台那张增值统计底稿：深红表头 + 微软雅黑 + 细边框，
   「合计」整行反白（2026-09-23 美化）。
"""

from __future__ import annotations

from typing import Dict, List

from . import metric

RATE = "0.0%"
RATE2 = "0.00%"
NUM = "0.##"
INT = "0"
MONEY = "#,##0.00"
MONEY0 = "#,##0.##"

STORE_HEAD = [
    "赛道", "门店", "台量目标", "台量进度", "新机销售", "新机达成率",
    "无忧目标", "无忧达成", "Care+达成", "合计达成", "连带率", "连带达成率",
    "总达成率", "优选", "超值", "全能进阶", "旗舰顶配", "399套装", "499套装",
    "后返", "Care+利润", "会员权益利润", "利润合计", "台均利润", "店长奖金",
]
STORE_FMT = {
    "新机达成率": RATE, "连带率": RATE, "连带达成率": RATE, "总达成率": RATE,
    "台量目标": NUM, "台量进度": NUM, "新机销售": NUM, "无忧目标": NUM,
    "无忧达成": INT, "Care+达成": INT, "合计达成": INT,
    "优选": INT, "超值": INT, "全能进阶": INT, "旗舰顶配": INT,
    "399套装": INT, "499套装": INT,
    "后返": MONEY0, "Care+利润": MONEY0, "会员权益利润": MONEY0,
    "利润合计": MONEY0, "台均利润": MONEY0, "店长奖金": MONEY,
}

REGION_HEAD = [
    "区域", "门店数", "新机销售", "区域目标", "无忧达成", "Care+达成",
    "合计达成", "连带率", "达成率", "后返", "会员权益利润", "利润合计", "台均利润",
]
REGION_FMT = {
    "连带率": RATE, "达成率": RATE, "新机销售": NUM, "区域目标": NUM,
    "无忧达成": INT, "Care+达成": INT, "合计达成": INT, "门店数": INT,
    "后返": MONEY0, "会员权益利润": MONEY0, "利润合计": MONEY0, "台均利润": MONEY0,
}

PERSON_HEAD = [
    "排名", "门店", "职位", "销售顾问", "主机销售", "配比率",
    "优选", "超值", "全能进阶", "旗舰顶配", "399套装", "499套装",
    "Care+", "合计达成", "后返", "Care+利润", "权益套餐利润", "利润合计",
    "台均利润", "奖金合计", "月度奖金增收",
]
PERSON_FMT = {
    "配比率": RATE, "后返": MONEY0, "Care+利润": MONEY0, "权益套餐利润": MONEY0,
    "利润合计": MONEY0, "台均利润": MONEY0, "奖金合计": MONEY,
    "月度奖金增收": MONEY, "主机销售": INT,
    "优选": INT, "超值": INT, "全能进阶": INT, "旗舰顶配": INT,
    "399套装": INT, "499套装": INT, "Care+": INT, "合计达成": INT,
}

TRACK_HEAD = ["赛道", "奖金池", "名次", "门店", "总达成率", "连带率",
              "应分份额", "门槛≥90%", "负激励", "实发"]
TRACK_FMT = {"总达成率": RATE, "连带率": RATE, "应分份额": MONEY,
             "负激励": MONEY, "实发": MONEY, "奖金池": MONEY}

#: 源表《汇机保》样式：深红 `#C00000` 表头 + 白字微软雅黑，正文细边框，合计反白。
STYLE = {
    "head_fill": "FFC00000",
    "head_font_color": "FFFFFFFF",
    "font": "微软雅黑",
    "head_size": 11,
    "body_size": 10,
    "border": True,
    "align": "center",
    "total_fill": "FFC00000",
    "total_font_color": "FFFFFFFF",
}

#: 源表条件标色（只盖非合计数据行）—— 粉红=差，浅绿=好。
_CF_BAD = {"fill": "FFFFC7CE", "font": "FF9C0006"}
_CF_OK = {"fill": "FFC6EFCE", "font": "FF006100"}

#: 门店达成：连带率&lt;10% 红 · 连带达成率&lt;50% 红 · 总达成率&gt;90% 绿
CF_STORE = [
    {"col": "连带率", "op": "lessThan", "v": 0.1, **_CF_BAD},
    {"col": "连带达成率", "op": "lessThan", "v": 0.5, **_CF_BAD},
    {"col": "总达成率", "op": "greaterThan", "v": 0.9, **_CF_OK},
]

#: 区域：连带率&lt;10% 红 / &gt;15% 绿（源表「区域达成」I 列）
CF_REGION = [
    {"col": "连带率", "op": "lessThan", "v": 0.1, **_CF_BAD},
    {"col": "连带率", "op": "greaterThan", "v": 0.15, **_CF_OK},
]

#: 人员（跟进表）：配比率&lt;10% 红 / &gt;15% 绿
CF_PERSON = [
    {"col": "配比率", "op": "lessThan", "v": 0.1, **_CF_BAD},
    {"col": "配比率", "op": "greaterThan", "v": 0.15, **_CF_OK},
]


def _t(r: dict) -> List[float]:
    t = r.get("tiers") or {}
    return [float(t.get(k) or 0) for k, _l, _f, _p in metric.TIERS]


def store_rows(rows: List[dict]) -> List[list]:
    out = []
    for r in rows:
        tt = _t(r)
        out.append([
            r.get("track") or "", r.get("store") or "",
            r.get("day_target") or 0, r.get("slot_progress") or 0,
            r.get("new") or 0, r.get("new_rate") or 0,
            r.get("goal") or 0, r.get("wuyou") or 0, r.get("care") or 0,
            r.get("total") or 0, r.get("attach") or 0,
            r.get("attach_goal_rate") or 0, r.get("overall") or 0,
        ] + tt + [
            r.get("rebate") or 0, r.get("care_profit") or 0,
            r.get("tier_profit") or 0, r.get("profit_total") or 0,
            r.get("avg_profit") or 0, r.get("manager_bonus") or 0,
        ])
    return out


def region_rows(rows: List[dict]) -> List[list]:
    out = []
    for r in rows:
        out.append([
            r.get("region") or "", r.get("stores") or 0,
            r.get("new") or 0, r.get("goal") or 0,
            r.get("wuyou") or 0, r.get("care") or 0, r.get("total") or 0,
            r.get("attach") or 0, r.get("goal_rate") or 0,
            r.get("rebate") or 0, r.get("tier_profit") or 0,
            r.get("profit_total") or 0, r.get("avg_profit") or 0,
        ])
    return out


def person_rows(rows: List[dict]) -> List[list]:
    out = []
    for r in rows:
        tt = _t(r)
        out.append([
            r.get("rank") or "", r.get("store") or "", r.get("title") or "",
            r.get("name") or "", r.get("new") or 0, r.get("attach_ratio") or 0,
        ] + tt + [
            r.get("care") or 0, r.get("total") or 0,
            r.get("rebate") or 0, r.get("care_profit") or 0,
            r.get("tier_profit") or 0, r.get("profit_total") or 0,
            r.get("avg_profit") or 0, r.get("bonus") or 0,
            r.get("bonus_month") or 0,
        ])
    return out


def track_rows(tracks: List[dict]) -> List[list]:
    out = []
    for t in tracks or []:
        pool = t.get("pool") or 0
        for r in t.get("rows") or []:
            out.append([
                t.get("track") or "", pool, r.get("rank") or 0,
                r.get("store") or "", r.get("overall") or 0,
                r.get("attach") or 0, r.get("share") or 0,
                "是" if r.get("gate_ok") else "否",
                r.get("fine") or 0, r.get("paid") or 0,
            ])
    return out


def sheets_of(d: dict) -> Dict[str, tuple]:
    stores = [r for r in (d.get("stores") or []) if isinstance(r, dict)]
    regions = [r for r in (d.get("regions") or []) if isinstance(r, dict)]
    people = [r for r in (d.get("people") or []) if isinstance(r, dict)]
    # 底部「合计」照源表 —— 比率用合计数重算（`metric.summarize_*`），不拿各行平均
    if stores:
        stores = list(stores) + [metric.summarize_stores(stores)]
    if regions:
        regions = list(regions) + [metric.summarize_regions(regions)]
    if people:
        people = list(people) + [metric.summarize_people(people)]
    sheets = {
        "门店": (STORE_HEAD, store_rows(stores), STORE_FMT,
                dict(STYLE, cf=CF_STORE)),
        "区域": (REGION_HEAD, region_rows(regions), REGION_FMT,
                dict(STYLE, cf=CF_REGION)),
        "人员": (PERSON_HEAD, person_rows(people), PERSON_FMT,
                dict(STYLE, cf=CF_PERSON)),
        "赛道奖金": (TRACK_HEAD, track_rows(d.get("tracks")), TRACK_FMT, STYLE),
    }
    return sheets


def file_name(d: dict) -> str:
    start = str(d.get("start") or "")
    end = str(d.get("end") or "")
    try:
        m = int(start[5:7])
        day = int(end[8:10])
    except (ValueError, TypeError):
        return "无忧会员权益达成"
    if m and day:
        return "%d月无忧会员权益达成-截止到%d日" % (m, day)
    return "无忧会员权益达成"


def meta_of(d: dict) -> dict:
    return {
        "窗口": "%s ~ %s" % (d.get("start") or "", d.get("end") or ""),
        "口径·新机": "手机零售净 + 分销净·备注含美团/抖音（**不乘 0.9**）",
        "口径·无忧": "优选/超值/全能/旗舰 + 399/499套装 净件数（贴膜礼包不算）",
        "口径·店目标": "台量目标×时间进度×0.2；总达成=新机达成×0.3+连带达成×0.7",
        "口径·区目标": "新机×0.15；达成率分子只算无忧",
        "口径·人后返": "6档后返 + Care+×150（无4单门槛）",
        "口径·奖金": "利润合计×0.1；月度增收=奖金/7×36",
        "赛道": "前三 50/30/20，总达成≥90% 才发；赛道连带<50% 负激励300",
        "配置": "config/valueadd-benefit.yaml",
        "看的是哪些门店": str(d.get("scope") or d.get("role") or "全部") + (
            "（%s）" % d["store_filter"] if d.get("store_filter") else ""),
    }


def export(root, d: dict, *, who: str = "", name: str = "") -> dict:
    from ....modules import notify
    return notify.export_xlsx(sheets_of(d), name=name or file_name(d),
                              meta=meta_of(d), root=root, who=who,
                              kind="export:benefit")


__all__ = ["export", "sheets_of", "file_name", "meta_of", "STYLE",
           "CF_STORE", "CF_REGION", "CF_PERSON",
           "STORE_HEAD", "REGION_HEAD", "PERSON_HEAD", "TRACK_HEAD"]
