"""三张看板的聚合（纯函数，无 IO）—— 区域 / 机型 / 销售员。

## 三条口径（开发目标 §四 已定）

1. **净额**：分销 + 分销退一起算（退单是负金额行，直接求和就是净额）。
   ⚠ 别按 `单据类型` 分开再减 —— 那样"退货"在界面上就没有位置了，
   而用户要的三张图都不区分正负单。
2. **销售员 = `店员` 列**：实测 `业务员` 列 1,368 行全空（销售报表的"业务员"
   没人填），能用的只有 `店员`（HandlerName）。
3. **`未分类` 单列一档**：三级分类里有大量字面量「未分类」，静默丢弃会让
   各分类合计 ≠ 总额（对不上账的看板等于坏看板）。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from . import ZONES, ZONE_PENDING
from . import region as region_mod


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _bucket(rows: List[Dict], key: str) -> List[Dict]:
    """按某列聚合成 `{key, rows, qty, amount}` 列表，按金额降序。

    空值/None 归「（空）」—— 跟「未分类」是两回事，别合并：
    「未分类」是 ERP 里的字面量（有人维护过分类但没归到三级），
    「（空）」是压根没值，混了就查不出是哪边坏了。
    """
    agg: Dict[str, Dict] = {}
    for r in rows:
        k = str(r.get(key) or "").strip() or "（空）"
        hit = agg.get(k)
        if hit is None:
            hit = agg[k] = {"key": k, "rows": 0, "qty": 0.0, "amount": 0.0}
        hit["rows"] += 1
        hit["qty"] += _f(r.get("数量"))
        hit["amount"] += _f(r.get("金额"))
    return sorted(agg.values(), key=lambda x: -x["amount"])


# ---------------------------------------------------------------- ① 区域

def region_board(rows: List[Dict], mapping: Dict[str, str]) -> Dict:
    """九区 + 待确认的分布，以及待确认队列（按客户聚）。"""
    region_mod.annotate(rows, mapping)
    agg = {z: {"zone": z, "rows": 0, "qty": 0.0, "amount": 0.0}
           for z in ZONES}
    agg[ZONE_PENDING] = {"zone": ZONE_PENDING, "rows": 0, "qty": 0.0, "amount": 0.0}
    for r in rows:
        hit = agg[r["_zone"]]
        hit["rows"] += 1
        hit["qty"] += _f(r.get("数量"))
        hit["amount"] += _f(r.get("金额"))
    board = [agg[z] for z in ZONES] + [agg[ZONE_PENDING]]
    return {
        "zones": board,
        "totals": _totals(rows),
        "pending": region_mod.pending_customers(rows, mapping),
        "mapping": sorted(
            [{"customer": k, "zone": v} for k, v in mapping.items() if v],
            key=lambda x: x["customer"]),
    }


# ---------------------------------------------------------------- ② 机型

#: 未分类的**商品级拆解** —— 给前端「未分类 ▸ 展开」用（用户 2026-09-29）。
UNCLASSIFIED = "未分类"

#: 云商商品档案**没配三级分类**、我们本地补的档（用户 2026-09-29：
#: 「x view 单独处理一下吧，云商没有三级分类我们补一下」）。
#: `(商品名称包含的子串, 补出的三级分类)` —— 只对三级=「未分类」的行生效，
#: 云商哪天自己配上档了，这段自然不触发（条件先判未分类）。
CAT3_FIX = (("Pura X View", "Pura X View"),)


def fix_cat3(rows: List[Dict]) -> List[Dict]:
    """本地补三级分类（原地改，返回同一列表）。

    ⚠ 补的是**显示档位**，不动落库的原值 —— 重拉/导出原始列照旧，
    只有看板和明细的展示层按这里归档。
    """
    for r in rows:
        if str(r.get("三级分类") or "").strip() != UNCLASSIFIED:
            continue
        name = str(r.get("商品名称") or "")
        for key, val in CAT3_FIX:
            if key in name:
                r["三级分类"] = val
                break
    return rows


def model_board(rows: List[Dict]) -> Dict:
    """一级分类 + 三级分类两个维度（页内并列）。

    ⚠ 额外给 `cat3_unclassified`：三级=「未分类」的行按**商品名称**再拆一层 ——
      补完 Pura X View 后剩下的未分类（促销品那些）点开能看清是些什么货，
      而不是只看到一个笼统的「未分类」数字。
    """
    fix_cat3(rows)
    unclass = [r for r in rows
               if str(r.get("三级分类") or "").strip() == UNCLASSIFIED]
    return {
        "cat1": _bucket(rows, "一级分类"),
        "cat3": _bucket(rows, "三级分类"),
        "cat3_unclassified": _bucket(unclass, "商品名称"),
        "totals": _totals(rows),
    }


# ---------------------------------------------------------------- ③ 销售员

def salesman_board(rows: List[Dict]) -> Dict:
    """店员总销售额排行 + 每人的一级分类构成（100% 堆叠用）。"""
    people: Dict[str, Dict] = {}
    for r in rows:
        name = str(r.get("店员") or "").strip() or "（无店员）"
        hit = people.get(name)
        if hit is None:
            hit = people[name] = {"name": name, "rows": 0, "qty": 0.0,
                                  "amount": 0.0, "mix": {}}
        amt = _f(r.get("金额"))
        hit["rows"] += 1
        hit["qty"] += _f(r.get("数量"))
        hit["amount"] += amt
        cat = str(r.get("一级分类") or "").strip() or "（空）"
        hit["mix"][cat] = hit["mix"].get(cat, 0.0) + amt
    out = []
    for p in sorted(people.values(), key=lambda x: -x["amount"]):
        total_abs = sum(abs(v) for v in p["mix"].values()) or 1.0
        # 占比按**绝对值**算 —— 退单是负金额，按代数和算占比会出现负百分比
        # （"手机 -12%"在堆叠图里没法画）。总额仍显示代数和（净额口径）。
        p["mix"] = [{"cat": k, "amount": v, "pct": abs(v) / total_abs}
                    for k, v in sorted(p["mix"].items(), key=lambda kv: -abs(kv[1]))]
        out.append(p)
    return {"people": out, "totals": _totals(rows)}


def _totals(rows: List[Dict]) -> Dict:
    return {"rows": len(rows),
            "amount": sum(_f(r.get("金额")) for r in rows),
            "qty": sum(_f(r.get("数量")) for r in rows)}
