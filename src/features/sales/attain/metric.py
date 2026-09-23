"""销售达成的**口径**（纯函数、零 IO）—— 照 `pos_metric.py` 的样子分出来的。

用户 2026-09-19 定的方向：功能模块里**规则与 IO 分开**，因为
「纯函数能脱离数据库和网络直接单测」—— 这一份一条 IO 都没有。

## 口径从哪来（**照抄，不重新发明**）

| 来源 | 定的什么 |
|---|---|
| `开发目标.md` 第三节 + skill 第 3/9 条 | 计入/剔除、台量的算法 |
| 目标表最后一行（办公室写的奖惩规则） | **单项封顶 120%** |
| skill 第 8 条 | 商品编码**集合精确 ∈**（防子串） |
| `2026-09-19-M2M5-销售达成设计.md` §2.2 | 三个**实测**出来的坑（下面逐条写着） |

## ⚠⚠ 三个实测坑（都真踩过 / 真查过，别改回去）

1. **`erp_sales.数量` 在库里是 TEXT**（全表 101,732 行 `typeof='text'`）。
   `SUM()` 会替你做数值转换，所以"看起来是对的" —— 但那依赖隐式行为。
   ⇒ 一律 `int(float(x or 0))`（见 `qty_of`）。
2. **退货在源数据里已经是负数**（`分销退` 全 -1、`零售退` -1~-5）。
   口径说"含退 → 冲减"，**云商导出里已经冲减好了**。
   写成 `qty if 类型 == '零售' else -qty` 会把退货**翻成正的** ——
   结果是"退了货反而更达成"。⇒ **直接相加**。
3. **一条退货可以退多台**（有一条 `-5`）⇒ 台量是 `SUM(数量)`，
   **不是 `COUNT(*)`**（后者会把它算成 1 台）。

## 剔除口径和 `pools.is_sample_marker` **故意不一样**

skill 第 9 条：「样机口径**只认商品名称的「演示机」**，**不认**串号标识的「样」」。
实测调 `is_sample_marker` 会多剔 **1900 行**（含 `样,新` 那 1816+ 行）——口径不符。
⇒ 这里的剔除只有两条：**商品名称含「演示机」/「体验机」** + **串号标识分段后等于「外调」**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple

#: 单项达成率**封顶** —— 依据是目标表最后一行写的奖惩规则（「达成120%封顶」），
#: **不是我们定的**。别改这个数，改了要回去问办公室。
CAP = 1.2

#: 计入台量的单据类型（**白名单** —— `核销` 和其它一切靠"不在这儿"被排除，
#: 不靠排除法：以后云商加一种新单据类型时，白名单是"不计入"，排除法是"计入"）。
IN_TYPES = ("零售", "分销", "零售退", "分销退")

#: 商品名称里带这些字 ⇒ **不是真卖的**（演示机在店里摆着，体验机给人试）
DROP_NAME_KEYWORDS = ("演示机", "体验机")

#: 串号标识里这个值 ⇒ 货调去别家了，不算本店卖出去
TRANSFER_MARK = "外调"


class Sale(NamedTuple):
    """一行销售 —— `load_sales` 的产物（**已经过白名单过滤**）。"""

    store: str          # 云商门店名
    code: str           # 商品编码（用来和映射表的编码集合比）
    qty: int            # 台量（**退货已是负数**，见模块头第 ② 条）
    kind: str = ""      # 单据类型（留着排查用）
    who: str = ""       # **店员**（谁卖的）—— 界面上鼠标悬停那一格要显示
    product: str = ""   # **商品名称** —— 悬停某个人时再展开一层：他都卖了哪些


# ------------------------------------------------------------------ 谓词
def qty_of(raw) -> int:
    """`数量` → int。**库里是 TEXT**，所以走 `float` 再过 `int`（`'1'`/`'-1'`/`1.0` 都对）。"""
    try:
        return int(float(raw or 0))
    except (TypeError, ValueError):
        return 0


def is_counted(kind) -> bool:
    """这个单据类型计不计入台量。"""
    return str(kind or "").strip() in IN_TYPES


def is_demo(product) -> bool:
    """演示机 / 体验机 —— 看**商品名称**。"""
    name = str(product or "")
    return any(k in name for k in DROP_NAME_KEYWORDS)


def is_transfer(mark) -> bool:
    """外调 —— 看**串号标识**。

    ⚠ 串号标识的真实长相：`新` / `J,新` / `W,新` / `Y,新` / `LH,新` ——
      **逗号分隔、位置不固定**。所以按逗号**分段后精确比**，不写 `'外调' in mark`：
      实测两种判法现在结果一样（库里只有 `外调,新` 这一种写法），
      但哪天出现 `X外调Y`，子串判法会**静默误剔**（多剔了还看不出来）。
    """
    text = str(mark or "").replace("，", ",")
    return any(p.strip() == TRANSFER_MARK for p in text.split(","))


def drop_reason(kind, product, mark) -> str:
    """该不该剔、为什么 —— **空字符串 = 留着**。

    ⚠ 剔除原因要能报出来（"这次剔了 N 行，其中演示机 a / 外调 b"）：
      静默剔除是查不出来的（`pools` 那边就吃过"数字对不上又找不到人"的亏）。
    """
    if is_demo(product):
        return "演示机/体验机"
    if is_transfer(mark):
        return "外调"
    if not is_counted(kind):
        return "单据类型不计入"
    return ""


# ------------------------------------------------------------------ 数据结构
@dataclass(frozen=True)
class Column:
    """一个产品列。"""

    name: str                  # 文档里的写法（`X6/X7/Pura x max`）
    codes: frozenset           # 商品编码集合 —— ⚠ **frozenset**：精确 ∈，防子串
    weight: float              # 占比 0~1


@dataclass(frozen=True)
class StorePlan:
    """目标表里的一行。"""

    name: str                  # **文档里**写的门店名（原样留着 —— 报错时要说这个）
    region: str                # A 列区域（合并单元格已向下填充；读不到就空）
    targets: Tuple[int, ...]   # 与 `columns` 同序


@dataclass(frozen=True)
class Plan:
    """规范化的目标表 —— M2 的产物，"长这样"就算对。"""

    period: str                # "2026-W38"（由 start 推 ISO 周，不是文档里写的）
    start: object              # datetime.date
    end: object                # datetime.date
    columns: Tuple[Column, ...]
    stores: Tuple[StorePlan, ...]

    def column_names(self) -> Tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def weights(self) -> Tuple[float, ...]:
        return tuple(c.weight for c in self.columns)


@dataclass(frozen=True)
class StoreResult:
    """一家店的核算结果 —— 直接对上前端的 `rows[]`。"""

    store: str                 # 文档里的门店名
    erp_name: str              # 匹配到的云商门店名；没匹配上 = ""
    matched: bool
    targets: Tuple[int, ...]
    actuals: Tuple[int, ...]
    rates: Tuple[Optional[float], ...]   # None = 这列没映射，**不算**
    total: Optional[float]               # None = 一列都没算（分母 0）
    #: 每列**是谁卖的**：`((人, 台量, (商品名…)), …)`，按台量降序 —— 悬停二级菜单用；
    #: 商品名是**第三级**（鼠标移到某个人那一行再展开），所以塞在同一条里
    people: Tuple[Tuple[Tuple[str, int, Tuple[str, ...]], ...], ...] = ()
    #: 区域（目标表 A 列，合并单元格已向下填充）—— 前端按它分组
    region: str = ""

    def as_dict(self) -> dict:
        return {"store": self.store, "erp_name": self.erp_name, "matched": self.matched,
                "region": self.region,
                "targets": list(self.targets), "actuals": list(self.actuals),
                "rates": [None if r is None else round(r, 4) for r in self.rates],
                "total": None if self.total is None else round(self.total, 4),
                "people": [[list(p) for p in col] for col in self.people]}


# ------------------------------------------------------------------ 三条规则
def rate_of(target, actual) -> float:
    """单项达成率 —— skill 原文三条里的前两条。

    * **目标为 0 → 记 100%**（不是 0%，也不是无穷大）；
    * **封顶 120%**（超了仍记 120%）。
    """
    if not target:
        return 1.0
    return min(float(actual) / float(target), CAP)


def total_of(weights, rates) -> Optional[float]:
    """加权总达成率 —— `None`（这列没映射）**整个跳过**，权重分母同步减掉。

    ⚠⚠ **一列都没剩下时返回 `None`，不许返回 `0.0`** —— 这是本模块最重要的一条：
      「0%」和「算不出来」在界面上是两件事（前端把 `null` 渲染成 `—`）。
      把"算不出来"写成 0，就造出了达标线以下的**假数字**。
    ⚠ 分母是 `sum(weights)` 而**不是硬编码 1**：本期 9 列恰好
      `0.15+0.15+0.1×7 = 1.0`，但缺失列被排除后就不是 1 了。
    """
    pairs = [(w, r) for w, r in zip(weights, rates) if r is not None]
    den = sum(w for w, _r in pairs)
    if not pairs or not den:
        return None
    return sum(w * r for w, r in pairs) / den


# ------------------------------------------------------------------ 核算
def sum_codes(sales, codes) -> int:
    """一堆销售行里，商品编码 ∈ `codes` 的台量合计。

    ⚠ `codes` 是 **frozenset**、用 `in` 精确比 —— 别改成 `code in 一串文本`：
      那会让 `919724` 命中 `9197245`（skill 第 8 条，Excel 那边也是这么防的）。
    """
    return sum(s.qty for s in sales if s.code in codes)


def people_by_column(plan: Plan, sales) -> Tuple[Tuple[Tuple[str, int, Tuple[str, ...]], ...], ...]:
    """每一列**是谁卖的、各卖了哪些商品** —— `((人, 台量, (商品名…)), …)`。

    ⚠ 台量按**净额**（退货也记在这个人头上）—— 跟那一列的台量口径一致，
      不然会出现"格子里 2 台、菜单里 3 台"这种对不上。
    ⚠ 没填店员的单子归到 `（没写店员）` —— 宁可显示这一条，也别让人以为漏了人。
    ⚠ 商品名给的是**这一列里他卖过的**（去重、按名称排）—— 第三级弹窗用。
    """
    out = []
    for col in plan.columns:
        tally: Dict[str, int] = {}
        names: Dict[str, set] = {}
        for s in sales:
            if not (col.codes and s.code in col.codes):
                continue
            who = s.who or "（没写店员）"
            tally[who] = tally.get(who, 0) + s.qty
            if s.product:
                names.setdefault(who, set()).add(s.product)
        out.append(tuple((who, qty, tuple(sorted(names.get(who, ()))))
                         for who, qty in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))))
    return tuple(out)


def store_result(plan: Plan, sp: StorePlan, sales, erp_name: str = "") -> StoreResult:
    """一家店：逐列 SUM → rates → total。

    `sales` 应当**已经**按这家店筛过（`compute` 那边按门店分组），
    这里只按商品编码分列 —— 9 列共用一次遍历。

    ⚠ 两处 **`None`**，别混：
      * `col.codes` 空（映射没到位）⇒ 这一列的 `rate` 是 `None`（**不算**，分母也不含它）；
      * `erp_name` 空（这家店在云商那边没匹配上）⇒ 整行 `matched=False`、`total=None`。
    """
    actuals, rates = [], []
    for i, col in enumerate(plan.columns):
        if not col.codes:                    # 映射没到位 —— 不是"卖 0 台"，是"没得算"
            actuals.append(0)
            rates.append(None)
            continue
        a = sum_codes(sales, col.codes)
        actuals.append(a)
        target = sp.targets[i] if i < len(sp.targets) else 0
        rates.append(rate_of(target, a))
    return StoreResult(store=sp.name, erp_name=erp_name or "", matched=bool(erp_name),
                       targets=tuple(int(x) for x in sp.targets),
                       actuals=tuple(actuals), rates=tuple(rates),
                       total=total_of(plan.weights(), rates),
                       people=people_by_column(plan, sales),
                       region=getattr(sp, "region", "") or "")


def all_results(plan: Plan, sales_by_store: Dict[str, list],
                store_map: Optional[Dict[str, str]] = None) -> List[StoreResult]:
    """全区：按 `plan.stores` 的顺序逐店算。

    `sales_by_store`：`{云商门店名: [Sale, …]}`（`compute` 那边分好组）。
    `store_map`：`{文档门店名: 云商门店名}` —— 没匹配上的店（不在名单里）
      走不到 `sum_codes`，直接给一行"没匹配"（`total=None`，**不是 0%**），
      界面上那一行会标出来（门店名对不上是**第一位的排查点**，见设计 §0.2）。
    """
    out = []
    for sp in plan.stores:
        erp = (store_map or {}).get(sp.name, "")
        if not erp:
            out.append(StoreResult(store=sp.name, erp_name="", matched=False,
                                   targets=tuple(int(x) for x in sp.targets),
                                   actuals=tuple(0 for _ in plan.columns),
                                   rates=tuple(None for _ in plan.columns),
                                   total=None,
                                   region=getattr(sp, "region", "") or ""))
            continue
        out.append(store_result(plan, sp, sales_by_store.get(erp, []), erp_name=erp))
    return out


def period_of(start, end) -> str:
    """期间名 —— 按 **ISO 周**推（`2026-W38`），**不是**"当前周"。

    ⚠ 文档里的 C1/D1 才是权威：办公室忘改表，我们就会照它算**上一周** ——
      所以要提醒（界面上标"数据截至"），但**不硬失败**（周一早上可能还没改完）。
    """
    iso = start.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


# ------------------------------------------------------------------ 分区汇总
def region_sums(rows, weights) -> Dict[str, dict]:
    """按区域把门店行合成一行「共计」—— **后端算好再下发**（照 film 的 film-sum）。

    返回 `{区域: {region, store, targets, actuals, rates, total, is_sum, …}}`，
    形状跟门店行对齐，前端 `renderAttain` 摆完店行接着摆这一行。

    ⚠ 率用**加总后的台量重算**（`rate_of` / `total_of` 同一口径），
      **不拿各行率去平均** —— 平均百分比和"总达成"对不上（film.summarize 同规矩）。
    ⚠ 某列**一行都没映射**（各家 rates[i] 全是 None）⇒ 汇总仍是 None，
      不许拿 0% 冒充（见 `total_of` 的红线）。
    ⚠ 接口层滤完店会**重算一遍**（`web.filter_attain_rows`）——
      否则门店看到的分区共计还是全区的，跟 `summary` 那条同一个坑。
    """
    by: Dict[str, List[dict]] = {}
    for r in rows or ():
        g = str(r.get("region") or "").strip() or "其他"
        by.setdefault(g, []).append(r)
    out: Dict[str, dict] = {}
    for g, grp in by.items():
        ncols = max([len(r.get("targets") or []) for r in grp] + [0])
        targets = [0] * ncols
        actuals = [0] * ncols
        for r in grp:
            for i, t in enumerate(r.get("targets") or []):
                if i < ncols:
                    targets[i] += int(t or 0)
            for i, a in enumerate(r.get("actuals") or []):
                if i < ncols:
                    actuals[i] += int(a or 0)
        rates: List[Optional[float]] = []
        for i in range(ncols):
            # 这一列有没有哪一家真算出来过 —— 全是 None = 没映射，汇总也给 None
            mapped = any(i < len(r.get("rates") or [])
                         and (r.get("rates") or [None] * (i + 1))[i] is not None
                         for r in grp)
            rates.append(rate_of(targets[i], actuals[i]) if mapped else None)
        total = total_of(list(weights or []), rates)
        out[g] = {
            "region": g,
            "store": "共计：%s" % g,
            "matched": True,
            "is_sum": True,
            "targets": targets,
            "actuals": actuals,
            "rates": [None if x is None else round(x, 4) for x in rates],
            "total": None if total is None else round(total, 4),
            "people": [],
        }
    return out
