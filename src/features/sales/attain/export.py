"""周度达成的**导出为 Excel** —— 只管把这一页的数据摆成表。

⚠ **写盘不在这儿**：调 `modules.notify.export_xlsx`（用户 2026-09-21 定的分工：
   「功能模块**调用**『导出为 excel』来把自己的数据生成为 excel 到本地」）。
  这里要做、也只做两件事：**摆表** + **写口径**。

出三张表（「说明」由 `notify.export_xlsx` 自动补，且**放在最后**）：

| 表 | 是什么 | 谁看 |
|---|---|---|
| **总览** | 一行一店；每个产品列**三格：达成 / 目标 / 达成率**（表头两行，产品名横跨三格） | 跟页面一致，区长扫一眼用 |
| **谁卖的** | 门店 / 产品列 / 人 / 台量 / 商品 | 页面上"悬停看得到"的那份，导出后能查 |

⚠ 三处**不许静默**（这个功能最怕"看着很合理的空"）：
  * 某列没配编码 → 页面上是「—」，导出件里也必须是**空单元格**，**不许写 0**
    （写 0 就是"这家店一列都没卖"，而真相是"这列没算"）；
  * 门店名在云商里匹配不上 → 「匹配」列写出来（`✗`），别让那一行混在正常行里；
  * 期间/数据截至写进「说明」表 —— 文件一离开这个程序，页面上那些提示就没人看得见了。

⚠ 2026-09-21 用户看过第一版导出件之后改的口径（原话）：

> 「这个下面**不要只有达成率**，每个品项要有**达成/目标/达成率三个**。
>   **目标与达成 sheet 就不要了**」

⇒ 那张长表（一行一个"门店 × 产品列"）**删掉了**；它原来承载的**列占比**
   搬进了总览的**分组表头**（`X6/X7/Pura x max（15%）`）——
   不然这个信息就跟着那张表一起没了。要透视的话，总览那张表现在也能直接透视
   （一行一店，每个指标一列）。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

#: 达成率那几列的 Excel 格式 —— **存数字、显示成百分比**（见 `xlsx_io.write_sheets`）。
#: 达成率 / 占比那几格的 Excel 格式 —— **存数字、显示成百分比**。
RATE_FMT = "0.0%"


def _rows(d: dict) -> List[dict]:
    return [r for r in (d.get("rows") or []) if isinstance(r, dict)]


def _rate(v):
    """`None` = 这列没算（**不是 0**）⇒ 空单元格。见模块头第三条。"""
    return "" if v is None else round(float(v), 4)


def overview_sheet(d: dict) -> tuple:
    """一行一店；每个产品**三格：达成 / 目标 / 达成率**（用户 2026-09-21 看图后定的）。

    表头是**两行**：

    ```
    | 门店        | X6/X7/Pura x max（15%） ||| Mate70 Air（15%） ||| 总达成率 |
    |             | 达成 | 目标 | 达成率      | 达成 | 目标 | 达成率 |          |
    ```

    ⚠ 产品名那格**横跨三列**（第二、三格写 `None` = 跟左边合并，见 `xlsx_io.write_sheets`）；
      「门店 / 云商门店 / 匹配 / 总达成率」那几格**竖跨两行**（第二行留空）。
    ⚠ 产品名后面带上**列占比** —— 原来它长在「目标与达成」那张表里，
      那张表用户让删了，不放这儿这个信息就没了。
    """
    cols = [str(c) for c in (d.get("columns") or [])]
    weights = list(d.get("weights") or [])
    head_top = ["门店", "云商门店", "匹配"]
    head_sub = ["", "", ""]
    for i, col in enumerate(cols):
        share = ""
        if i < len(weights):
            share = "（%d%%）" % round(float(weights[i]) * 100)
        head_top += [col + share, None, None]
        head_sub += ["达成", "目标", "达成率"]
    head_top.append("总达成率")
    head_sub.append("")

    rows = []
    for r in _rows(d):
        targets = list(r.get("targets") or [])
        actuals = list(r.get("actuals") or [])
        rates = list(r.get("rates") or [])
        line = [r.get("store") or "", r.get("erp_name") or "",
                "✓" if r.get("matched") else "✗（云商里没这个名字）"]
        for i in range(len(cols)):
            line += [actuals[i] if i < len(actuals) else "",
                     targets[i] if i < len(targets) else "",
                     _rate(rates[i] if i < len(rates) else None)]
        line.append(_rate(r.get("total")))
        rows.append(line)
    # 每个产品的第 3 格（达成率）+ 最后那格总达成率 → 百分比
    fmt = {3 * (i + 1) + 3: RATE_FMT for i in range(len(cols))}
    fmt[3 * len(cols) + 4] = RATE_FMT
    return ([head_top, head_sub], rows, fmt)


def people_sheet(d: dict) -> tuple:
    """谁卖的 —— 页面上的二级/三级菜单（人 → 商品）摊平。

    ⚠ 形状来自 `metric.StoreResult.people`：`[[[人, 台量, [商品名…]], …] 每个产品列]`。
      没这一项（老数据 / 别的机器算的）就**不出这张表**，别出个空壳。
    """
    cols = [str(c) for c in (d.get("columns") or [])]
    headers = ["门店", "产品列", "人", "台量", "商品"]
    rows = []
    for r in _rows(d):
        people = r.get("people") or []
        for i, col in enumerate(cols):
            group = people[i] if i < len(people) else []
            for item in group or ():
                who = item[0] if len(item) > 0 else ""
                qty = item[1] if len(item) > 1 else ""
                goods = item[2] if len(item) > 2 else ()
                rows.append([r.get("store") or "", col, who, qty,
                             "；".join(str(g) for g in (goods or ()))])
    return (headers, rows, {})


def has_people(d: dict) -> bool:
    return any(r.get("people") for r in _rows(d))


def sheets_of(d: dict) -> Dict[str, tuple]:
    """这一页的数据 → `{表名: (表头, 行, 列格式)}`（`notify.export_xlsx` 认得这个形状）。"""
    out = {"总览": overview_sheet(d)}
    if has_people(d):
        out["谁卖的"] = people_sheet(d)
    return out


def file_name(d: dict) -> str:
    """文件名主干：`周度达成-2026-W38`（时间戳由推送模块补）。"""
    period = str(d.get("period") or "").strip()
    return "周度达成-%s" % period if period else "周度达成"


def _period_text(d: dict) -> str:
    period = str(d.get("period") or "").strip()
    start, end = str(d.get("start") or ""), str(d.get("end") or "")
    if start and end:
        return "%s（%s ~ %s）" % (period or "（没有期间）", start, end)
    return period or "（没有期间）"


def meta_of(d: dict, *, who: str = "") -> dict:
    """写进「说明」表的那些话 —— **口径写清楚，别让人猜**。"""
    missing = [str(x) for x in (d.get("missing_columns") or [])]
    m = {
        "期间": _period_text(d),
        "数据截至": str(d.get("data_until") or "") or "（不知道）",
        "门店数": len(_rows(d)),
        "产品列数": len(d.get("columns") or []),
        "数据来源": "%s（算于 %s）" % (d.get("file") or "out/attain-<年>.json",
                                     d.get("computed_at") or "（没记）"),
        "看的是哪些门店": str(d.get("scope") or d.get("role") or "全部") + (
            "（%s）" % d["store_filter"] if d.get("store_filter") else ""),
        "口径·单项达成率": "达成 ÷ 目标；目标为 0 记 100%；封顶 120%",
        "口径·总达成率": "按各产品列的占比加权；没配编码的列整列跳过，权重分母同步减",
        "空单元格": "这一列没算（没配编码），不是 0",
    }
    if missing:
        m["⚠ 没映射的产品列"] = "、".join(missing)
    until, end = str(d.get("data_until") or ""), str(d.get("end") or "")
    if until and end and until != end:
        m["⚠ 注意"] = "库里数据只到 %s —— 这是周中累计，不是最终达成" % until
    if d.get("error"):
        m["⚠ 后端的话"] = str(d["error"])
    # ⚠ 不写「导出人」：那是**能力层**自己补的（`notify.export_xlsx` 的 `who`）——
    #   两处都写 ⇒ 说明表里两行「导出人」（实测踩到）。
    return m


def export(root, d: dict, *, who: str = "", name: str = "", subdir=None) -> dict:
    """把达成数据导成 Excel 落到本机 → `{"ok", "why", "rel", "file", …}`。

    ⚠ `d` **必须**是已经按身份过滤过的那一份（后端走 `App.attain()`）。
      这里不做权限判断 —— 一份数据该给谁看，判据只有 `role_scope()` 一处
      （AGENTS.md 坑 18：页面藏起来不等于接口拦住了）。
    """
    # ⚠ 四个点：本文件在 `src/features/sales/attain/` 下（`....` = `src`）
    from ....modules import notify
    kw = {"subdir": subdir} if subdir else {}
    return notify.export_xlsx(sheets_of(d), name=name or file_name(d),
                              meta=meta_of(d, who=who), root=root, who=who,
                              kind="export:attain", **kw)


__all__ = ["export", "sheets_of", "file_name", "meta_of", "overview_sheet",
           "people_sheet", "has_people"]
