"""区域识别（M2）—— 备注关键词 → 客户映射 → 待确认。**不拿门店名兜底**。

## 优先级（用户 2026-09-29 拍板，开发目标 §四）

1. **备注含九区关键词** → 自动定区（`source="备注"`）
2. **客户名在映射表里**（历史人工确认）→ 沿用（`source="客户"`）
3. 都不出 → **待确认**（`source=""`），进下拉队列人工定
   ⛔ **不用门店名兜底** —— 用户明确否掉：开单门店所在区 ≠ 客户所在区。

⚠ 顺序上"备注"先于"客户映射"：备注里写死的区名是**这一行自己说的**，
比客户名的默认归类更具体（同一客户理论上可以往两个区卖）。
⚠ 识别率的底（2026-09-29 实测）：全量分销单里备注/客户名能命中九区的仅 1.3%，
所以**待确认是常态、按客户记忆是设计核心** —— 一个客户确认一次，以后全沿用。

## 映射的键 = 客户名（strip 后）

实测 25,986 行分销单**客户名无一为空** ⇒ 按客户键可靠，不存在"空键挤成一坨"。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from . import ZONES, ZONE_PENDING
from . import store


def zone_of_text(text: str) -> str:
    """在一段文本里找九区关键词，命中第一个就返回区名；没有返回 ''。

    ⚠ 只认**最长匹配优先**的朴素子串 —— 九个词互不为子集
    （"市南"≠"市北"，"胶州"和"胶南"不同词），不存在前缀吞掉的问题。
    """
    s = str(text or "")
    if not s:
        return ""
    for z in ZONES:
        if z in s:
            return z
    return ""


def identify(row: Dict, mapping: Dict[str, str]) -> Tuple[str, str]:
    """一行 → `(区, 来源)`。`区` 为 `ZONE_PENDING` 时来源为空串。

    `row` 只读 `备注` / `客户/顾客` 两列；`mapping` 是整张客户映射
    （`store.map_all()` 的结果，调用方读一次传进来，别一行读一次库）。
    """
    z = zone_of_text(row.get("备注"))
    if z:
        return z, "备注"
    cust = str(row.get("客户/顾客") or "").strip()
    z = str(mapping.get(cust) or "").strip()
    if z:
        return z, "客户"
    return ZONE_PENDING, ""


def annotate(rows: List[Dict], mapping: Dict[str, str]) -> List[Dict]:
    """给每行加 `_zone` / `_zone_src` 两个键（原地改，返回同一列表）。"""
    for r in rows:
        z, src = identify(r, mapping)
        r["_zone"] = z
        r["_zone_src"] = src
    return rows


def pending_customers(rows: List[Dict], mapping: Dict[str, str]) -> List[Dict]:
    """待确认队列 —— **按客户聚**（不是按行）：一个客户一条，带它的行数/金额。

    这是"确认一次、以后沿用"的落点：113 个客户 vs 1,368 行，
    按行点要点一千多次，按客户点点一百多次（而且第二次拉同一客户就不再出现）。
    """
    agg: Dict[str, Dict] = {}
    for r in rows:
        z, _src = identify(r, mapping)
        if z != ZONE_PENDING:
            continue
        cust = str(r.get("客户/顾客") or "").strip() or "（无客户名）"
        hit = agg.get(cust)
        if hit is None:
            hit = agg[cust] = {"customer": cust, "rows": 0, "amount": 0.0,
                               "sample_remark": ""}
        hit["rows"] += 1
        hit["amount"] += _f(r.get("金额"))
        if not hit["sample_remark"] and r.get("备注"):
            hit["sample_remark"] = str(r.get("备注"))[:60]
    return sorted(agg.values(), key=lambda x: (-x["rows"], x["customer"]))


def load_mapping(root=None) -> Dict[str, str]:
    """读整张映射（调用一次、整页共用 —— 别在聚合循环里反复开库）。"""
    return store.map_all(root)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def confirm(root, customer: str, zone: str, who: str = "") -> dict:
    """人工确认一个客户属于哪个区（或清除）。**这就是"以后可以修改"的入口**。

    `zone` 必须是九区之一或空串（空 = 清除回待确认）—— 拼错的区名会被拒，
    不然映射表里混进一个 `zone="城阳区"` 这种值，看板就多出一档假区。
    """
    z = str(zone or "").strip()
    if z and z not in ZONES:
        return {"ok": False, "why": "不认识的区：%s（可选：%s）"
                % (z, "、".join(ZONES))}
    changed = store.map_set(root, customer, z, who=who)
    return {"ok": True, "changed": changed, "customer": str(customer or "").strip(),
            "zone": z}
