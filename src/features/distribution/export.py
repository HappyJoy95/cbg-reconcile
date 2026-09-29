# -*- coding: utf-8 -*-
"""分销明细 —— 导出 Excel（数据来自 `App.dist_detail`，跟页面同一份口径）。

⚠ 写盘不在这儿：调 `modules.notify.export_xlsx`（规矩见那份的模块头）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

MONEY_FMT = "#,##0.##"
NUM_FMT = "0.##"

#: 明细列（顺序 = 页面底表的顺序，多一列「区域」放最前 —— 那是本功能的核心维度）。
HEAD = ["区域", "区域来源", "支付时间", "单号", "单据类型", "商品名称",
        "一级分类", "三级分类", "品牌", "数量", "金额", "店员",
        "客户/顾客", "备注"]

COL_FMT = {"数量": NUM_FMT, "金额": MONEY_FMT}


def sheets_of(d: Dict) -> Dict[str, tuple]:
    """`dist_detail` 的返回 → `{明细: (表头, 行), 汇总: …}`。"""
    rows: List[Dict] = d.get("rows") or []
    body = []
    for r in rows:
        body.append([r.get("_zone") or "", r.get("_zone_src") or "",
                     r.get("支付时间") or "", r.get("单号") or "",
                     r.get("单据类型") or "", r.get("商品名称") or "",
                     r.get("一级分类") or "", r.get("三级分类") or "",
                     r.get("品牌") or "", r.get("数量"), r.get("金额"),
                     r.get("店员") or "", r.get("客户/顾客") or "",
                     r.get("备注") or ""])
    # 汇总：按区域（净额口径）
    agg = {}                                        # type: Dict[str, List[float]]
    for r in rows:
        z = r.get("_zone") or "待确认"
        hit = agg.setdefault(z, [0, 0.0])
        hit[0] += 1
        try:
            hit[1] += float(r.get("金额") or 0)
        except (TypeError, ValueError):
            pass
    summ = [[z, v[0], v[1]] for z, v in sorted(agg.items(), key=lambda kv: -kv[1][1])]
    return {
        "明细": (HEAD, body, COL_FMT),
        "区域汇总": (["区域", "行数", "金额"], summ, {2: MONEY_FMT}),
    }


def meta_of(d: Dict) -> Dict:
    return {"数据范围": "%s ~ %s（%s）" % (d.get("start"), d.get("end"),
                                          d.get("store") or ""),
            "口径": "单据类型 ∈ {分销, 分销退}，净额（退单为负）；"
                    "区域 = 备注关键词 → 客户映射 → 待确认（不用门店名兜底）"}


def export(root: Path, d: Dict, who: str = "", name: str = "") -> dict:
    from ...modules import notify
    if not d.get("ok"):
        return {"ok": False, "why": d.get("why") or "明细读取失败"}
    return notify.export_xlsx(
        sheets_of(d),
        name=name or "分销明细-%s-%s" % (d.get("start"), d.get("end")),
        meta=meta_of(d), root=root, who=who)
