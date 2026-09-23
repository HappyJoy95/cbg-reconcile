# -*- coding: utf-8 -*-
"""月度生意计划的**导出为 Excel** —— 只管把这一页的数据摆成表。

⚠ **写盘不在这儿**：调 `modules.notify.export_xlsx`（用户 2026-09-21 定的分工：
   「功能模块**调用**『导出为 excel』来把自己的数据生成为 excel 到本地」）。
   这里要做、也只做两件事：**摆表** + **写口径**。

出**三张表**（「说明」由 `notify.export_xlsx` 自动补，且**放在最后**）：

| 表 | 形状 |
|---|---|
| **销量** / **销售额** / **利润** | 一行 = 门店；列 = **二级分类**（系列），每个系列三格：本月 / 上月同期 / 环比 |

⚠ 口径是用户 2026-09-21 晚定的：「**销量、销售额、利润是三个sheet分开，
  不用一级分类，用二级分类就行**」——

* **一个指标一张表**（原来是一张表里塞三组指标）；
* 列用**二级分类**（系列），**不要一级分类**（七个大块）；
  ⚠ 二级分类**不重名**（实测 25 个，跨块没有同名的）⇒ 可以直接拿它当列名。
    哪天真重名了，`series_order()` 里得带上块名（`test_plan.py` 有测试钉着）。
* 最后那一组「合计」= 这一行**所有二级分类之和**（= 七个大块相加，跟页面「合计」同口径）。

⚠ 三处**不许静默**（这个功能最怕"看着很合理的空"）：
  * 环比那格**没有百分比时留空，不许写 0** —— 上月是 0（「新增」）和"涨跌 0%"
    是两件事，写 0 就是骗人；
  * 门店名列写出来（名单外的店根本不会进这份表 —— 那是算的时候就剔掉的）；
  * 期间 / 数据截至 / 口径 / 剔除 / 不纳入，全部写进「说明」表 ——
    文件一离开这个程序，页面上那些提示就没人看得见了。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .metric import growth_rate

#: 环比那几列的 Excel 格式 —— **存数字、显示成百分比**（见 `xlsx_io.write_sheets`）
RATE_FMT = "0.0%"

#: 三个指标的中文名 —— 也是三张表的表名（用户 2026-09-21：「三个 sheet 分开」）
METRIC_LABELS = (("qty", "销量", "台"), ("amount", "销售额", "元"), ("profit", "利润", "元"))

#: 每个系列那三格的期间名（**顺序不能动**：列就是照这个铺的）
SPAN_LABELS = ("本月", "上月同期", "环比")

#: 最后一组（七个大块相加）
TOTAL_LABEL = "合计"


def _rows(d) -> List[dict]:
    return [r for r in (d.get("rows") or []) if isinstance(r, dict)]


def _cell(node: dict, metric: str, span: str):
    """取一格的值。`span="环比"` 时给**环比数字**（`None` ⇒ 留空，见模块头）。"""
    node = node or {}
    if span == "环比":
        g = (node.get("growth") or {}).get(metric) or {}
        return g.get("rate")            # None = 不给百分比（new / zero / bare）
    field = "cur" if span == "本月" else "prev"
    v = (node.get(field) or {}).get(metric)
    if v is None:
        # ⚠ 这一行**没有**这个系列（那家店没卖过）⇒ 写 **0，不写空**：
        #   矩阵表里"空"和"0"看着差不多，但求和/透视时是两回事（空会被跳过）。
        return 0 if metric == "qty" else 0.0
    return v


def _blocks_sum(blocks, metric: str, span: str):
    """合计那一格：**七个块相加**（⚠ 跟页面「合计」同一口径 —— 只加块，
    不加展开出来的系列/机型，加了就重复计）。

    ⚠ `span="环比"` 走 `growth_rate`（**别自己写 (c-p)/p**：分母是 0 会抛、
      是负数会给出方向相反的百分比 —— 见 `metric.growth_rate` 的注释）。
    """
    if span == "环比":
        cur = _blocks_sum(blocks, metric, "本月")
        prev = _blocks_sum(blocks, metric, "上月同期")
        return growth_rate(cur or 0, prev or 0)
    field = "cur" if span == "本月" else "prev"
    total = 0
    for b in blocks or ():
        total += float(((b.get(field) or {}).get(metric)) or 0)
    return int(total) if metric == "qty" else round(total, 2)


def series_order(d) -> List[Tuple[str, str]]:
    """列顺序：`[(一级分类, 二级分类), …]` —— 按七个大块的顺序，块内按名字排。

    ⚠ 跨门店取**并集**：有的店卖过某个系列、有的没卖过。列必须按全集铺开，
      不然每家店的列对不上（对比、透视都没法做）。
    ⚠ 一级分类**不占列**（用户说了不用它），只用来**定顺序**；它跟二级的对应关系
      写进「说明」表 —— 拿掉一层不等于把这个信息丢了。
    """
    blocks = [str(b) for b in (d.get("blocks") or [])]
    per: Dict[str, set] = {b: set() for b in blocks}
    for r in _rows(d):
        for b in (r.get("blocks") or []):
            name = str(b.get("name") or "")
            if name in per:
                for s in (b.get("series") or []):
                    sname = str(s.get("name") or "")
                    if sname:
                        per[name].add(sname)
    out: List[Tuple[str, str]] = []
    for b in blocks:
        for s in sorted(per[b]):
            out.append((b, s))
    return out


def column_labels(series: List[Tuple[str, str]]) -> List[str]:
    """列名 = 二级分类；**同名跨块时带上块名**（`穿戴·耳机`）。

    ⚠ 二级分类**只在它自己那一级里唯一**：同一个词挂两个一级分类是可能的
      （全库实测：`延保服务` 在 手机平板周边/电脑周边、`支架` 在 外购散件/电脑周边 ——
      那两个一级分类本来就不参与统计，所以现在这份表里没有重名）。
      真出现重名却不管：**两列同名**，看表的人以为是同一列、加的时候却分开加 ⇒ 数对不上。
    """
    seen = {}
    for _blk, name in series:
        seen[name] = seen.get(name, 0) + 1
    return [("%s·%s" % (blk, name)) if seen[name] > 1 else name for blk, name in series]


def metric_sheet(d: dict, metric: str):
    """**一个指标一张表**：行 = 门店（+ 合计行）；列 = 二级分类 ×（本月/上月同期/环比）。

    表头两行（分组表头，见 `xlsx_io.write_sheets`）：第一行是系列名（横跨它那三格），
    第二行是三格的期间名。`合计` 那一组同理。

    ⚠ 数据行的**合计**三格取自 `row.growth`（后端 `metric.total_growth` 算的，
      跟页面那个数同源）；合计**行**（各店相加）的环比现场用 `growth_rate` 算 ——
      两处都**不许**写成"把每列的百分比再平均一下"，那玩意儿没有意义。
    """
    series = series_order(d)
    labels = column_labels(series)
    head = [["门店", "区域"], ["", ""]]
    for name in labels:
        head[0] += [name, None, None]
        head[1] += list(SPAN_LABELS)
    head[0] += [TOTAL_LABEL, None, None]
    head[1] += list(SPAN_LABELS)

    regions = d.get("regions") or {}
    rows = _rows(d)
    out = []
    sums = [{"本月": 0.0, "上月同期": 0.0} for _ in series]      # 合计行用
    for r in rows:
        store = str(r.get("store") or "")
        by_series = {}
        for b in (r.get("blocks") or []):
            for s in (b.get("series") or []):
                by_series[str(s.get("name") or "")] = s
        row = [store, regions.get(store, "")]
        for i, (_blk, name) in enumerate(series):
            node = by_series.get(name) or {}
            row += [_cell(node, metric, span) for span in SPAN_LABELS]
            sums[i]["本月"] += float(_cell(node, metric, "本月") or 0)
            sums[i]["上月同期"] += float(_cell(node, metric, "上月同期") or 0)
        row += [_blocks_sum(r.get("blocks"), metric, span) for span in SPAN_LABELS]
        out.append(row)

    # 合计行：多于一家店才加（门店账号导出的就自己那一家，加一行只是重复）
    if len(rows) > 1:
        row = ["合计", ""]
        for i, (_blk, _name) in enumerate(series):
            cur, prev = sums[i]["本月"], sums[i]["上月同期"]
            row += [_fmt_sum(cur, metric), _fmt_sum(prev, metric), growth_rate(cur, prev)]
        all_cur = sum(s["本月"] for s in sums)
        all_prev = sum(s["上月同期"] for s in sums)
        row += [_fmt_sum(all_cur, metric), _fmt_sum(all_prev, metric),
                growth_rate(all_cur, all_prev)]
        out.append(row)

    fmt = {}
    for i, span in enumerate(head[1]):
        if span == "环比":
            # ⚠ `write_sheets` 的 formats 键是**从 1 起算的列号**（它内部走
            #   `get_column_letter(int(col))`）—— 拿 `enumerate` 的下标直接当键会**整体错一列**，
            #   表现是"环比那列还是小数、旁边那列变成百分比"（实测踩过一次）。
            fmt[i + 1] = RATE_FMT
    return head, out, fmt


def _fmt_sum(v, metric: str):
    return int(v) if metric == "qty" else round(float(v), 2)


def meta_of(d: dict) -> dict:
    """口径说明 —— 进「说明」表。

    ⚠ 这几条**必须跟着文件走**：文件一离开这个程序，页面上那句
      「按串号卖出去的，不是云商全部开单」就没人看得见了，
      而拿它对云商营业额会差 1% 上下、且查不出原因。
    """
    per = d.get("period") or {}
    cur, prev = per.get("cur") or {}, per.get("prev") or {}
    series = series_order(d)
    by_block: Dict[str, List[str]] = {}
    for blk, name in series:
        by_block.setdefault(blk, []).append(name)
    meta = {
        "本月": "%s ~ %s（%d 天）" % (cur.get("start", "?"), cur.get("end", "?"),
                                     cur.get("days") or 0),
        "上月同期": "%s ~ %s（%d 天）" % (prev.get("start", "?"), prev.get("end", "?"),
                                        prev.get("days") or 0),
        "数据截至": d.get("data_as_of") or "（库里没数）",
        "算出来的时刻": d.get("computed_at") or "",
        "看的是哪几家店": "门店名单里**带区域**的那 28 家（%s）"
                          % (d.get("store_filter") or "全部"),
        "三张表怎么看": "销量 / 销售额 / 利润 **各一张**；列是二级分类（系列），"
                        "每个系列三格：本月 / 上月同期 / 环比；"
                        "最后那一组「合计」= 这一行所有系列相加（= 七个大块相加，"
                        "跟页面上「合计」那一列同一个数）；"
                        "环比**空白**表示不给百分比（上月为 0 = 新增、或两边都是 0）",
        "二级分类 → 一级分类": " ｜ ".join(
            "%s（%d）：%s" % (blk, len(names), "、".join(names))
            for blk, names in by_block.items()) or "（没有数据）",
        "口径": "按串号卖出去的（跟四池对账同一份数据），**不是云商全部开单** —— "
                "无串号的行（配件 / 第三方耳机等）不在内",
        "剔除": "非白名单单据（核销 / 客情单等）、演示机 / 体验机、串号标识含「外调」的行；"
                "名单外的店（联想专卖店 / 部门 / 机场店）一条都不进",
        "不纳入的系列": "联想笔记本 / 外购笔记本 / 笔记本办公机 / 联想平板电脑 / "
                        "外购平板电脑 / 外购手表 / 联想Moto系列 / 外购手机 / 手机办公机",
        "退货": "源数据里已经是负数，直接相加（不再取反）",
        "利润": "云商「零售考核毛利」",
        "环比": "（本月 − 上月同期）/ 上月同期；上月为 0 显示「新增」、"
                "上月为负或两边都是 0 时**不给百分比**（空白格）",
    }
    unk = d.get("unknown") or {}
    if unk:
        meta["⚠ 没认出来的品类"] = "、".join("%s（%d 行）" % kv for kv in sorted(unk.items()))
    skip = d.get("skipped") or {}
    if skip:
        meta["⚠ 实际没纳入的（本期间）"] = "、".join(
            "%s（%d 行）" % kv for kv in sorted(skip.items()))
    drop = d.get("dropped") or {}
    if drop:
        meta["剔除了多少行"] = "、".join("%s %d 行" % kv for kv in sorted(drop.items()))
    for i, w in enumerate(d.get("warnings") or []):
        meta["⚠ 提醒%d" % (i + 1)] = w
    return meta


def export(root, d: dict, who: str = "", name: str = "") -> dict:
    """摆表 → 交给 `notify.export_xlsx` 落盘。返回它的结果（**绝不抛**）。"""
    from ....modules.notify import export_xlsx
    per = (d.get("period") or {}).get("cur") or {}
    stem = name or ("月度生意计划-%s" % (per.get("end") or ""))
    sheets = {}
    for key, label, _unit in METRIC_LABELS:
        sheets[label] = metric_sheet(d, key)          # 表名就是指标名（销量/销售额/利润）
    return export_xlsx(sheets, name=stem, meta=meta_of(d), root=root,
                       who=who, kind="export:plan")
