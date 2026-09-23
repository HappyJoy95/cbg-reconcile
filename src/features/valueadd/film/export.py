# -*- coding: utf-8 -*-
"""防护膜达成 —— 导出 Excel（照中台那张月度目标/达成底稿的版式）。

⚠ 写盘不在这儿：调 `modules.notify.export_xlsx`。
⚠ 版式对齐源表：区域分组 + 每区「共计」+ 底部「合计」；率类存数字、显示百分比。
⚠ **样式**（2026-09-23 用户：「两个导出格式参考一下这两个表，对应数据列做好相应的美化」）：
   表头/合计 = 纯红底白字微软雅黑（源表 `#FF0000`），正文细边框居中。
"""

from __future__ import annotations

from typing import Dict, List

RATE_FMT = "0.00%"
RATE_FMT0 = "0%"
NUM_FMT = "0.##"
INT_FMT = "0"
MONEY_FMT = "#,##0.##"

#: 表头顺序 = 源表截图那一排（区域 / 门店 / … / 台均增值利润）。
HEAD = [
    "区域", "门店", "新机销售", "目标", "达成", "防护膜单张平均利润",
    "跟机率", "毛利目标", "台均利润基线", "零售毛利达成", "礼包毛利达成",
    "总毛利达成", "毛利达成率", "总达成率", "礼包套餐", "礼包达成",
    "礼包达成率", "台均增值利润",
]

COL_FMT = {
    "跟机率": RATE_FMT, "毛利达成率": RATE_FMT, "总达成率": RATE_FMT,
    "礼包达成率": RATE_FMT0,
    "新机销售": NUM_FMT, "目标": NUM_FMT, "防护膜单张平均利润": NUM_FMT,
    "毛利目标": MONEY_FMT, "零售毛利达成": MONEY_FMT, "礼包毛利达成": MONEY_FMT,
    "总毛利达成": MONEY_FMT, "礼包套餐": NUM_FMT, "达成": INT_FMT,
    "礼包达成": INT_FMT, "台均增值利润": NUM_FMT,
}

#: 源表样式：表头纯红 + 白字，正文细边框，「共计 / 合计」整行反白。
#: `cf` = 中台目标底稿的条件标色（**只盖非合计数据行**）：
#:   跟机率&lt;30% 粉 · 毛利达成率&lt;90% 粉 / &gt;90% 紫 · 总达成率&gt;100% 紫
#:   · 台均增值利润&lt;20 粉 / &gt;27 紫
STYLE = {
    "head_fill": "FFFF0000",
    "head_font_color": "FFFFFFFF",
    "font": "微软雅黑",
    "head_size": 11,
    "body_size": 10,
    "border": True,
    "align": "center",
    "total_fill": "FFFF0000",
    "total_font_color": "FFFFFFFF",
    "cf": [
        {"col": "跟机率", "op": "lessThan", "v": 0.3,
         "fill": "FFFF99CC", "font": "FF800000"},
        {"col": "毛利达成率", "op": "lessThan", "v": 0.9,
         "fill": "FFFF99CC", "font": "FF800000"},
        {"col": "毛利达成率", "op": "greaterThan", "v": 0.9,
         "fill": "FFCCCCFF", "font": "FF000000"},
        {"col": "总达成率", "op": "greaterThan", "v": 1,
         "fill": "FFCCCCFF", "font": "FF000000"},
        {"col": "台均增值利润", "op": "lessThan", "v": 20,
         "fill": "FFFF99CC", "font": "FF800000"},
        {"col": "台均增值利润", "op": "greaterThan", "v": 27,
         "fill": "FFCCCCFF", "font": "FF000000"},
    ],
}

#: 区域显示顺序 —— 按 `config/stores.yaml` 的区名（源表是北/市/西/服务站，
#: 我们名单是西北区/市区/南区；服务站若在名单里排最后）。
REGION_ORDER = ("西北区", "市区", "南区", "服务站", "北区", "西区")


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _row(region: str, store: str, r: dict) -> list:
    return [
        region, store,
        _f(r.get("new")), _f(r.get("target")), _f(r.get("done")),
        _f(r.get("unit_profit")),
        _f(r.get("attach")),
        _f(r.get("profit_target")), r.get("baseline") or "",
        _f(r.get("film_profit")), _f(r.get("gift_profit")), _f(r.get("total_profit")),
        _f(r.get("profit_rate")), _f(r.get("total_rate")),
        _f(r.get("gift_pkg")), _f(r.get("gift_done")), _f(r.get("gift_rate")),
        _f(r.get("avg_addon")),
    ]


def sheet_of(d: dict) -> tuple:
    """一张「数据」表：分区 → 店行 + 该区「共计」→ 最后「合计」。"""
    from . import metric
    rows: List[dict] = [r for r in (d.get("rows") or []) if isinstance(r, dict)]
    by_region: Dict[str, List[dict]] = {}
    for r in rows:
        by_region.setdefault(str(r.get("region") or "其他"), []).append(r)

    ordered = [g for g in REGION_ORDER if g in by_region]
    ordered += [g for g in sorted(by_region) if g not in REGION_ORDER]

    out: List[list] = []
    for g in ordered:
        grp = by_region[g]
        first = True
        for r in grp:
            out.append(_row(g if first else "", r.get("store") or "", r))
            first = False
        sub = metric.summarize(grp)
        out.append(_row("共计：", "", sub))

    total = metric.summarize(rows)
    out.append(_row("合计", "", total))
    return HEAD, out, COL_FMT, STYLE


def file_name(d: dict) -> str:
    start = str(d.get("start") or "")
    end = str(d.get("end") or "")
    try:
        m = int(start[5:7]) if len(start) >= 7 else 0
        day = int(end[8:10]) if len(end) >= 10 else 0
    except ValueError:
        m = day = 0
    if m and day:
        return "%d月防护膜数据达成-截止到%d日" % (m, day)
    return "防护膜数据达成"


def meta_of(d: dict) -> dict:
    return {
        "窗口": "%s ~ %s" % (d.get("start") or "", d.get("end") or ""),
        "时间进度": str(d.get("progress") or ""),
        "看的是哪些门店": str(d.get("scope") or d.get("role") or "全部") + (
            "（%s）" % d["store_filter"] if d.get("store_filter") else ""),
        "口径·新机": "(手机零售净台 + 分销净台·备注含美团) × 0.9；京东分销不算",
        "口径·达成": "贴膜 · 去高透软膜 · 零售+分销(±退) · 净件数（不含核销）",
        "口径·跟机率": "达成 ÷ 新机（目标 35%）",
        "口径·毛利目标": "新机 × 台均利润基线（25/30/35/40 按店）",
        "口径·总达成率": "毛利达成率×50% + 礼包达成率×50%",
        "礼包": "9 个膜类礼包品名；不含无忧会员 / 399 轻奢",
    }


def sheets_of(d: dict) -> dict:
    return {"数据": sheet_of(d)}


def export(root, d: dict, *, who: str = "", name: str = "") -> dict:
    from ....modules import notify
    return notify.export_xlsx(sheets_of(d), name=name or file_name(d),
                              meta=meta_of(d), root=root, who=who,
                              kind="export:film")


__all__ = ["export", "sheets_of", "sheet_of", "file_name", "meta_of",
           "HEAD", "COL_FMT", "STYLE"]
