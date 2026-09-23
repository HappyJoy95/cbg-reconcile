# -*- coding: utf-8 -*-
"""月度生意计划的**口径**（纯函数、零 IO）—— 照 `pos_metric.py` / `attain/metric.py` 分的。

用户 2026-09-21 立项：「统计每个店**当月和上个月同期**的**销量、销售额、利润和环比升降**」，
按**折叠机 / FD / ND / 穿戴 / 音频 / 平板 / 电脑**七块分开，**块 → 系列 → 机型**三层。

分两个文件不是洁癖：**口径能不能脱离数据库单测**全看这条。

## 口径从哪来（**照抄，不重新发明**）

| 来源 | 定的什么 |
|---|---|
| 用户 2026-09-21 第一轮 | FD = Mate + Pura；ND = nova + 畅享；**`Pura X View` 算 FD**；上月同期 = 同天数；利润 = **零售考核毛利** |
| 用户 2026-09-21 第二轮 | **`wiko` / `麦芒` → ND**；联想Moto / 外购手机 / 手机办公机**不纳入**；核销·客情单**不计入** |
| 用户 2026-09-21 第三轮 | **只算 `config/stores.yaml` 名单内的 28 家店**（云商那份导出有 43 家，多的是联想专卖店/部门/机场店） |
| 用户 2026-09-21 第四轮 | 展开**三级也做**（块 → 系列 → 机型） |
| 用户 2026-09-21 第五轮 | **联想笔记本去掉** ⇒ 同理 `外购笔记本` / `笔记本办公机` / `联想平板电脑` / `外购平板电脑` / `外购手表` 一律**不纳入**（见 `CAT2_SKIP`）；「合计」列也要**上月同期对比** |
| `attain/metric.py` | 单据类型白名单、演示机 / 外调剔除、`数量` 是 TEXT |
| `comparison/category.py` | 「电脑」**只算笔记本**（不含台式机/显示器/打印机） |

## ⚠⚠ 四个实测坑（都真踩过，别改回去）

1. **词照抄云商，不许"顺手改"**：`mate X5系列`（小写 m）、`Nova filp系列`
   （源数据里 flip 拼成了 **filp**）、`nova 15系列`（nova 后面**有空格**）。
   按"看起来该是什么"写映射 ⇒ **整批静默漏掉**，页面上只表现为"这个系列怎么这么少"。
2. **`Pura X View` 归 FD，不归折叠机**（用户明确纠正过）。它跟 `Pura X系列` /
   `Pura X Max系列` 挨着（都是 Pura 线），**不能按前缀猜**，只能按精确词列表。
   实测它 10 天 170 台、占 FD 的 **38%** —— 归错就是最大的一处错。
3. **`数量` 在库里是 TEXT**（照 attain 实测）。一律 `int(float(x or 0))`（见 `qty_of`）。
4. **退货在源数据里已经是负数**（`分销退` −1、`零售退` −1~−5）⇒ **直接相加**，
   不要再取反；台量是 `SUM(数量)` 而**不是** `COUNT(*)`（一条退货能退多台）。

## 认不出的词**必须报出来**（本项目的红线）

三种返回值，别混：

| 返回 | 含义 | 处理 |
|---|---|---|
| 七块之一 | 参与统计 | **算** |
| `SKIP` | 认得出、用户明确说不纳入（联想Moto / 外购手机 / 手机办公机） | 不算，**计数报出来** |
| `OTHER` | 认得出、但不在这七块里（智慧屏 / 全屋智能 / 配件 / 会员…） | 不算，只汇总一个总数 |
| `UNKNOWN` | **没见过这个词** | 不算，**而且必须报出来** |
| `None` | 空的 | 不算 |

静默归并的后果是「口径悄悄变了，页面上一点看不出来」—— 这个项目最忌讳的就是这个。
云商上了新系列就会冒出新词，靠 `UNKNOWN` 那条路径把它顶出来。
"""

from __future__ import annotations

import calendar
import datetime
from typing import Dict, List, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------- 七个大块
#: 展示顺序（页面、导出、JSON 都按它排）—— 用户 2026-09-21 报的顺序。
BLOCKS = ("折叠机", "FD", "ND", "穿戴", "音频", "平板", "电脑")

#: 「手机」一级下面，**二级分类 → 块**。
#:
#: ⚠⚠ 每条词都是**照抄云商的原词**（含大小写/空格/拼写错误），见模块头坑 1。
PHONE_SERIES = {
    "折叠机": (
        "Mate X6系列", "Mate X7系列", "Mate XT系列", "mate X5系列",
        "Pura X系列", "Pura X Max系列", "Pocket系列", "Nova filp系列",
    ),
    "FD": (
        "Mate70系列", "Mate80系列", "Mate60系列",
        "Pura70系列", "Pura 80系列", "Pura 90系列", "P系列",
        # ⚠ **用户明确：Pura X View 算 FD，不是折叠屏**（模块头坑 2）
        "Pura X View",
    ),
    "ND": (
        "nova 14系列", "nova 15系列", "nova 16系列",
        "畅享系列",
        # ⚠ 用户 2026-09-21 第二轮：「wiko 算 nd，麦芒 nd」
        "wiko系列", "麦芒系列",
    ),
}

#: 二级分类 → 块（把上面那张翻过来，**代码里只用这一张**）。
_SERIES_BLOCK = {}
for _b, _names in PHONE_SERIES.items():
    for _n in _names:
        _SERIES_BLOCK[_n] = _b
del _b, _names, _n

#: 「手机」下**认得出、但用户明确说不纳入**的二级（不算，计数报出来）。
PHONE_SKIP = ("联想Moto系列", "外购手机", "手机办公机")

#: 直接映射那四块里**认得出、但不纳入**的二级分类（不算，**计数报出来**）。
#:
#: ⚠⚠ 用户 2026-09-21 晚：「**联想笔记本去掉**」。这是**华为**的生意计划，
#:   店里卖的联想 / 外购 / 办公机不该算进「电脑」块 —— 而原来 `DIRECT_BLOCK`
#:   只看一级分类、二级**一概不看** ⇒ 页面上会冒出一个「联想笔记本」列。
#:   ⇒ 把这一类（联想 / 外购 / 办公机）一次都列上：**只留华为的**，
#:     不纳入的照规矩计数报出来（页面那行「不纳入的」+ 导出的「说明」表）。
#:   ⚠ **别拿全库的数字吓自己**（我第一版注释就写错了）：全库 `联想笔记本` 有 3374 行，
#:     但那几乎全是**名单外的联想专卖店**卖的 —— 这份计划只算名单内 28 家店，
#:     实测 2026-09 窗口里只有 **1 行 / 1 台**（8 月 0 行，平板/穿戴那几条是 0）。
#:     影响小 ≠ 不该改：列在页面上就是个「联想笔记本」列，用户一眼就看到。
#:   ⚠ 加词之前先查库（**记得带上名单内的店**）：`SELECT 一级分类, 二级分类, COUNT(*)
#:     FROM erp_sales GROUP BY 1,2`；词要**照抄云商的原词**（见模块头坑 1）。
CAT2_SKIP = {
    "笔记本": ("联想笔记本", "外购笔记本", "笔记本办公机"),
    "平板": ("联想平板电脑", "外购平板电脑"),
    "智能穿戴": ("外购手表",),
}

#: 一级分类 → 块（手机以外的四块）。⚠ 「电脑」**只算笔记本**，不含台式机/显示器/打印机。
DIRECT_BLOCK = {
    "智能穿戴": "穿戴",
    "音频产品": "音频",
    "平板": "平板",
    "笔记本": "电脑",
}

#: 云商**已知的**一级分类（2026-09-21 实测商品表 11,337 SKU + 销售明细）。
#: 在这张表里、但不在 `DIRECT_BLOCK` 里的 ⇒ `OTHER`（认得出、不参与）；
#: **不在这张表里的 ⇒ `UNKNOWN`**（没见过，必须报出来）。
KNOWN_CAT1 = (
    "手机", "智能穿戴", "音频产品", "平板", "笔记本", "智慧屏",
    "手机平板周边", "电脑周边", "台显打印", "全屋智能", "外购散件",
    "智能家居", "潮玩礼品", "鸿蒙汽车", "会员", "付费会员", "增值服务",
)

#: 参与统计的单据类型（**白名单** —— 靠"不在这儿"排除，不靠排除法）
IN_TYPES = ("零售", "分销", "零售退", "分销退")

#: 商品名称里带这些字 ⇒ **不是真卖的**
DROP_NAME_KEYWORDS = ("演示机", "体验机")

#: 串号标识里这个值 ⇒ 货调去别家了
TRANSFER_MARK = "外调"

#: 三个返回值里那两个"不算"的
SKIP = "不纳入"
OTHER = "其它"
UNKNOWN = "未知"

#: 指标的三个名字（顺序 = 页面上从左到右）
METRICS = ("qty", "amount", "profit")


# ---------------------------------------------------------------- 行与结果
class Sale(NamedTuple):
    """一行销售 —— `plan.load_sales` 的产物（**已经过白名单过滤**）。"""

    store: str           # 云商门店名（要在名单里）
    cat1: str            # 一级分类
    cat2: str            # 二级分类（= 系列）
    cat3: str            # 三级分类（= 机型）
    qty: int             # 台量（**退货已是负数**）
    amount: float        # 金额（销售额）
    profit: float        # 零售考核毛利
    #: 店员（云商 `erp_sales.店员`）—— 给"门店点开拆到人"用（用户 2026-09-21）。
    #: ⚠ **放在最后且带默认值**：别的位置一动，所有 `Sale(...)` 的构造点（含测试）
    #:   都得跟着改，而它们绝大多数只关心前七个字段。
    who: str = ""


class Totals(NamedTuple):
    """一个节点的三个指标。"""

    qty: int = 0
    amount: float = 0.0
    profit: float = 0.0

    def plus(self, other):
        return Totals(self.qty + other.qty,
                      self.amount + other.amount,
                      self.profit + other.profit)

    def as_dict(self) -> dict:
        # ⚠ 台量是 int、金额两位小数：直接 `json.dump` 出去的，浮点尾巴会一路带到页面上
        return {"qty": int(self.qty),
                "amount": round(float(self.amount), 2),
                "profit": round(float(self.profit), 2)}


# ---------------------------------------------------------------- 谓词
def qty_of(raw) -> int:
    """`数量` → int。**库里是 TEXT**，所以走 `float` 再过 `int`（模块头坑 3）。"""
    try:
        return int(float(raw or 0))
    except (TypeError, ValueError):
        return 0


def money_of(raw) -> float:
    """金额 / 毛利 → float。空串、`None`、带逗号的都认。"""
    try:
        return float(str(raw or 0).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def is_counted(kind) -> bool:
    """这个单据类型计不计入。"""
    return str(kind or "").strip() in IN_TYPES


def is_demo(product) -> bool:
    """演示机 / 体验机 —— 看**商品名称**。"""
    name = str(product or "")
    return any(k in name for k in DROP_NAME_KEYWORDS)


def is_transfer(mark) -> bool:
    """外调 —— 看**串号标识**。

    ⚠ 串号标识的真实长相：`新` / `J,新` / `W,新` / `LH,新` —— 逗号分隔、位置不固定。
      所以**分段后精确比**，不写 `'外调' in mark`（哪天出现 `X外调Y` 会静默误剔）。
      —— 跟 `attain/metric.py` 一字不差，**两份各自独立**（那边是达成、这边是月度）。
    """
    text = str(mark or "").replace("，", ",")
    return any(p.strip() == TRANSFER_MARK for p in text.split(","))


def drop_reason(kind, product, mark) -> str:
    """该不该剔、为什么 —— **空字符串 = 留着**（照 `attain/metric.py`）。"""
    if is_demo(product):
        return "演示机/体验机"
    if is_transfer(mark):
        return "外调"
    if not is_counted(kind):
        return "单据类型不计入"
    return ""


def block_of(cat1, cat2) -> Optional[str]:
    """`(一级分类, 二级分类)` → 七块之一 / `SKIP` / `OTHER` / `UNKNOWN` / `None`。

    ⚠ 三种"不算"**必须分开**（模块头那张表）：
      `SKIP` = 认得出、用户说不纳入；`OTHER` = 认得出、但不在这七块；
      `UNKNOWN` = **没见过这个词** —— 那一个是要报给人看的。
    """
    c1 = str(cat1 or "").strip()
    c2 = str(cat2 or "").strip()
    if not c1:
        return None
    if c1 == "手机":
        if not c2:
            return UNKNOWN                      # 手机却没有二级 —— 认不出，报出来
        if c2 in _SERIES_BLOCK:
            return _SERIES_BLOCK[c2]
        return SKIP if c2 in PHONE_SKIP else UNKNOWN
    if c1 in DIRECT_BLOCK:
        # ⚠ 二级也要看一眼：这几块原来只看一级 ⇒ 联想/外购的笔记本、平板照样算进华为的块
        #   （用户 2026-09-21：「联想笔记本去掉」）。不在 `CAT2_SKIP` 里的照旧。
        if c2 in CAT2_SKIP.get(c1, ()):
            return SKIP
        return DIRECT_BLOCK[c1]
    return OTHER if c1 in KNOWN_CAT1 else UNKNOWN


def is_known_cat1(cat1) -> bool:
    """这个一级分类我们见过没（界面上的"要补映射表"提示按它判）。"""
    c1 = str(cat1 or "").strip()
    return c1 in KNOWN_CAT1 or c1 == "手机"


# ---------------------------------------------------------------- 期间
class Span(NamedTuple):
    """一段时间（闭区间，含首尾）。"""

    start: datetime.date
    end: datetime.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "days": self.days}


def month_span(day) -> Span:
    """`day` 所在自然月的 **1 号 ~ day**（当月至今）。"""
    d = _as_date(day)
    return Span(d.replace(day=1), d)


def prev_month_same_span(day) -> Span:
    """**上月同期** —— 上月 1 号 ~ 上月同一天，**再夹到上月最后一天**。

    用户 2026-09-21 定的口径：「上月 1 号 ~ 上月同一天（同天数）」。

    ⚠ 三条边界（都有测试）：
      * 9/21 → 8/1 ~ 8/21（正常对齐）；
      * **3/31 → 2/1 ~ 2/28**（上月只有 28 天 ⇒ 取满整月，**不是 2/31 报错**）；
      * 闰年 2/29 同理（4/30 → 3/1 ~ 3/30 是 30 天，3/31 → 3/1 ~ 3/31）。
    """
    d = _as_date(day)
    last_prev = d.replace(day=1) - datetime.timedelta(days=1)   # 上月的最后一天
    end = last_prev.replace(day=min(d.day, last_prev.day))
    return Span(last_prev.replace(day=1), end)


def _as_date(day) -> datetime.date:
    """`date` / `datetime` / `"2026-09-21"` → `date`。"""
    if isinstance(day, datetime.datetime):
        return day.date()
    if isinstance(day, datetime.date):
        return day
    return datetime.date.fromisoformat(str(day)[:10])


def month_last_day(year: int, month: int) -> int:
    """这个月有几天（闰年 2 月 = 29）。"""
    return calendar.monthrange(year, month)[1]


# ---------------------------------------------------------------- 环比
#: 环比有五种形态 —— **前端按 kind 上色出文案，不许自己拿 rate 判**。
#:
#: | kind | 什么时候 | 页面怎么显示 |
#: |---|---|---|
#: | `up` / `down` / `flat` | 上月 > 0 | 百分比 + 箭头（`flat` = 一分不差） |
#: | `new` | 上月 = 0、本月 > 0 | **「新增」**（分母是 0，任何百分比都是编的） |
#: | `zero` | 两边都是 0 | 「—」 |
#: | `bare` | **上月 < 0** | **只显示两个绝对值** —— 负数分母的"增长率"方向是反的，容易读错 |
GROWTH_KINDS = ("up", "down", "flat", "new", "zero", "bare")

#: 浮点比较的容差 —— 金额是两位小数累加出来的，`==` 判"持平"会漏
EPS = 1e-6


def growth_kind(cur, prev) -> str:
    """环比属于哪一种（见 `GROWTH_KINDS`）。"""
    c, p = float(cur or 0), float(prev or 0)
    if p < -EPS:
        return "bare"          # 负数分母：不给百分比
    if p <= EPS:
        return "new" if c > EPS else "zero"
    if c > p + EPS:
        return "up"
    if c < p - EPS:
        return "down"
    return "flat"


def growth_rate(cur, prev) -> Optional[float]:
    """环比百分比 —— **`None` = 这次不给百分比**（`new` / `zero` / `bare` 三种）。

    ⚠ 别在外面自己算 `(c - p) / p`：分母是 0 会抛、是负数会给出方向相反的百分比。
    """
    if growth_kind(cur, prev) in ("new", "zero", "bare"):
        return None
    p = float(prev)
    return (float(cur) - p) / p


def compare(cur: Totals, prev: Totals) -> dict:
    """两组指标 → 环比三件套（每个指标一个 `{kind, rate}`）。"""
    out = {}
    for m in METRICS:
        c = getattr(cur, m)
        p = getattr(prev, m)
        r = growth_rate(c, p)
        out[m] = {"kind": growth_kind(c, p),
                  "rate": None if r is None else round(r, 4)}
    return out


# ---------------------------------------------------------------- 聚合
#: 树的层名（第 0 层是块、1 是系列、2 是机型）
LEVEL_NAMES = ("block", "series", "model")


def collect(rows, *, with_who: bool = False) -> Tuple[Dict[tuple, Totals], dict, dict]:
    """把行按 `(门店, 块, 系列, 机型)` 累加；`with_who=True` 时**多一层店员**
    （`(门店, 店员, 块, 系列, 机型)`，给"点门店拆到人"用）。

    返回 `(累加表, 未认出的词, 明确不纳入的词)` —— 后两个都是 `{原词: 台数}`。

    ⚠ 两张表**同一套过滤与映射**（同一个函数、同一份 `block_of`）——
      另写一份"按人算"的循环，迟早出现"人对不上店里那个数"，
      而那种错最难查（两边看着都合理）。

    ⚠ **收原词而不是台数**：出问题时人要看到"哪个词不认识"，才可能去补映射表
      （照 `comparison/category.py::summary` 的规矩）。
    ⚠ 只收**参与统计**的块的行；`SKIP` / `OTHER` / `UNKNOWN` / 空的一律不进累加表。
    """
    acc: Dict[tuple, Totals] = {}
    unknown: Dict[str, int] = {}
    skipped: Dict[str, int] = {}
    for r in rows:
        blk = block_of(r.cat1, r.cat2)
        if blk in (None, SKIP, OTHER, UNKNOWN):
            if blk == UNKNOWN:
                _bump(unknown, _word_of(r))
            elif blk == SKIP:
                _bump(skipped, str(r.cat2 or "").strip())
            continue
        who = str(getattr(r, "who", "") or "").strip()
        key = ((str(r.store or ""), who, blk,
                str(r.cat2 or "").strip(), str(r.cat3 or "").strip()) if with_who
               else (str(r.store or ""), blk,
                     str(r.cat2 or "").strip(), str(r.cat3 or "").strip()))
        one = Totals(int(r.qty or 0), float(r.amount or 0), float(r.profit or 0))
        acc[key] = one if key not in acc else acc[key].plus(one)
    return acc, unknown, skipped


def _word_of(r) -> str:
    """认不出的那个"词" —— 手机看二级、其余看一级（报给人补表用的就是这个）。"""
    c1 = str(r.cat1 or "").strip()
    if c1 == "手机":
        return "手机 / %s" % (str(r.cat2 or "").strip() or "（二级为空）")
    return c1


def _bump(counter: dict, word: str) -> None:
    w = str(word or "").strip()
    if w:
        counter[w] = counter.get(w, 0) + 1


def rollup(acc: Dict[tuple, Totals], depth: int) -> Dict[tuple, Totals]:
    """把键截到 `depth` 层再加总。

    ⚠ `acc` 的键是**四层** `(门店, 块, 系列, 机型)`，所以：
      `depth=2` → `(门店, 块)`、`depth=3` → `(门店, 块, 系列)`、`depth=4` → 原样。
      **别按"第几级"数**（块是第 1 级、但键里它排第 2 位）—— 这里踩过一次：
      按"级"写成 1/2/3，`key[1]` 直接 `IndexError`。

    ⚠ 上层**必须由下层加总得来**（不许另查一次库）：两次数出来的东西对不上，
      是这类看板最经典的"总额 ≠ 明细之和"。
    """
    out: Dict[tuple, Totals] = {}
    for key, tot in acc.items():
        k = tuple(key[:depth])
        out[k] = tot if k not in out else out[k].plus(tot)
    return out


def _sub(acc: Dict[tuple, Totals], drop: int) -> Dict[tuple, Totals]:
    """把键前面 `drop` 层剥掉 —— 让"门店"和"人"**共用同一个建树函数**。

    ⚠ 门店的键是 `(门店, 块, 系列, 机型)`、人的键是 `(门店, 店员, 块, 系列, 机型)`
      ⇒ 剥掉前 1 / 2 层之后，两边的键都变成 `(块, 系列, 机型)`，
      后面 `rollup(..., 1/2/3)` 那套深度常量就能**一模一样**地用。
      不剥的话就得为"人"再写一套深度（4/5/6），写错一个数就是数错一层 —— 不值得。
    """
    out: Dict[tuple, Totals] = {}
    for key, tot in acc.items():
        k = tuple(key[drop:])
        out[k] = tot if k not in out else out[k].plus(tot)
    return out


def _blocks_of(cur: Dict[tuple, Totals], prev: Dict[tuple, Totals]) -> List[dict]:
    """`(块[, 系列[, 机型]]): Totals` 两张表 → **七块的树**（带环比）。

    ⚠ 键已经是"剥过实体"的（见 `_sub`）：`rollup(..., 1/2/3)` = 块 / 块+系列 / 全量。
    ⚠ 块级**永远列满七块**（"这块这个月没卖"要看得见）；系列/机型只列**真有数**的。
    """
    blk_cur = rollup(cur, 1)
    blk_prev = rollup(prev, 1)
    ser_cur = rollup(cur, 2)
    ser_prev = rollup(prev, 2)
    mod_cur = rollup(cur, 3)
    mod_prev = rollup(prev, 3)
    out = []
    for blk in BLOCKS:
        c = blk_cur.get((blk,), Totals())
        pv = blk_prev.get((blk,), Totals())
        srows = []
        names = {k[1] for k in ser_cur if k[0] == blk} | {k[1] for k in ser_prev if k[0] == blk}
        for name in sorted(names):
            sc = ser_cur.get((blk, name), Totals())
            sp = ser_prev.get((blk, name), Totals())
            mnames = ({k[2] for k in mod_cur if k[0] == blk and k[1] == name}
                      | {k[2] for k in mod_prev if k[0] == blk and k[1] == name})
            mrows = []
            for mname in sorted(mnames):
                mc = mod_cur.get((blk, name, mname), Totals())
                mp = mod_prev.get((blk, name, mname), Totals())
                mrows.append({"name": mname, "cur": mc.as_dict(), "prev": mp.as_dict(),
                              "growth": compare(mc, mp)})
            srows.append({"name": name, "cur": sc.as_dict(), "prev": sp.as_dict(),
                          "growth": compare(sc, sp), "models": mrows})
        out.append({"name": blk, "cur": c.as_dict(), "prev": pv.as_dict(),
                    "growth": compare(c, pv), "series": srows})
    return out


def people_of(cur_acc: Dict[tuple, Totals], prev_acc: Dict[tuple, Totals],
              stores: List[str]) -> Dict[str, List[dict]]:
    """`{门店: [{"name": 店员, "blocks": […], "cur": {…}, "prev": {…}}, …]}` —— **点门店拆到人**。

    传进来的累加表是**带人那一层**的（`collect(rows, with_who=True)`，键
    `(门店, 店员, 块, 系列, 机型)`）。

    ⚠ 口径跟门店那一层**完全一样**（同一个 `collect` 过滤 + 同一个 `_blocks_of`）⇒
      **"这家店所有人的数加起来 = 店里那一行" 这条恒等式成立**（有测试钉着）。
      守住它比什么都重要：两份口径各算各的，迟早出现"人对不上店"，而那种错最难查。
    ⚠ 人按**本月金额从多到少**排（页面上一眼看到主力），并列按名字 ⇒ 顺序稳定。
    ⚠ **本月上月全 0 的人不列**（卖了又退光的）：列出来只是噪声。
      只有上月有数的人**仍然列**（环比要看得见，他会显示成 ↓100%）。
    """
    cur_by: Dict[tuple, Dict[tuple, Totals]] = {}
    for key, tot in cur_acc.items():
        cur_by.setdefault((key[0], key[1]), {})[key[2:]] = tot
    prev_by: Dict[tuple, Dict[tuple, Totals]] = {}
    for key, tot in prev_acc.items():
        prev_by.setdefault((key[0], key[1]), {})[key[2:]] = tot

    out: Dict[str, List[dict]] = {}
    for store in stores:
        rows = []
        names = ({k[1] for k in cur_by if k[0] == store}
                 | {k[1] for k in prev_by if k[0] == store})
        for who in names:
            cur = cur_by.get((store, who), {})
            prev = prev_by.get((store, who), {})
            blocks = _blocks_of(cur, prev)
            c = _sum_blocks(blocks, "cur")
            pv = _sum_blocks(blocks, "prev")
            # ⚠ `any(dict)` 看的是**键**（永远为真）—— 要 `any(dict.values())`。
            #   踩过一次：写了 `any(c)`，于是"卖了又退光"的人照样占一行（全 0）。
            if not any(c.values()) and not any(pv.values()):
                continue                                  # 全 0 的人不占一行
            rows.append({"name": who or "（没写店员）", "blocks": blocks,
                         "cur": c, "prev": pv,
                         # 人那一行的「合计」环比 —— 跟门店那行同一个函数、同一口径
                         "growth": total_growth(blocks)})
        rows.sort(key=lambda r: (-float(r["cur"]["amount"]), r["name"]))
        out[store] = rows
    return out


def _sum_blocks(blocks: List[dict], field: str) -> dict:
    """七块相加 —— **跟页面上「合计」那一列同一口径**（只加块，不加展开出来的明细）。

    ⚠ 别拿"所有叶子"相加：系列/机型是块的下一层，加进去就重复计了
      （页面上那条图例专门写了这句）。
    """
    return _blocks_totals(blocks, field).as_dict()


def _blocks_totals(blocks: List[dict], field: str) -> Totals:
    """七块相加 → `Totals`（要拿它算环比的用这个；只看数的用 `_sum_blocks`）。"""
    tot = Totals()
    for b in blocks or ():
        d = b.get(field) or {}
        tot = tot.plus(Totals(int(d.get("qty") or 0), float(d.get("amount") or 0),
                              float(d.get("profit") or 0)))
    return tot


def total_growth(blocks: List[dict]) -> dict:
    """「合计」那一列的**环比** —— 分子分母都是**七个块相加**（跟合计那个数同一口径）。

    ⚠ 用户 2026-09-21：「**合计也加上和上个月同期对比**」。
      别拿 `blocks[].growth` 去平均、也别拿"全部叶子"去算：
      前者是七个百分比、后者会把系列/机型重复算一遍 —— 两种都跟合计那个数对不上。
    """
    return compare(_blocks_totals(blocks, "cur"), _blocks_totals(blocks, "prev"))


def tree(cur_acc: Dict[tuple, Totals], prev_acc: Dict[tuple, Totals],
         stores: List[str]) -> List[dict]:
    """两组累加表 → **按门店排好的三层树**（带环比）。

    形状（直接就是 `/api/plan` 的 `rows[]`）：

    ```
    [{"store": "青岛城阳万达店",
      "blocks": [{"name": "FD", "cur": {...}, "prev": {...}, "growth": {...},
                  "series": [{"name": "Mate80系列", ..., "models": [{"name": "Mate80", ...}]}]}]}]
    ```

    ⚠ **名单里的每一家店都要出现**，哪怕一行销售都没有（`cur`/`prev` 全 0）——
      "0 是 0"和"这家店不见了"是两件事（§三·0）。
    ⚠ 只有**真的有数**的块/系列/机型才展开（全 0 的块不占位置）；
      块级永远列满七块（页面上一眼看到"这块这个月没卖"）。
    ⚠ 键前面**多剥一层**才是"这家店自己的"（`_sub(acc, 1)`）—— 见 `_sub` 那段说明。
    """
    out = []
    for store in stores:
        cur = {k[1:]: v for k, v in cur_acc.items() if k[0] == store}
        prev = {k[1:]: v for k, v in prev_acc.items() if k[0] == store}
        blocks = _blocks_of(cur, prev)
        out.append({"store": store, "blocks": blocks,
                    # 门店那一行的「合计」环比（用户 2026-09-21：「合计也加上和上个月同期对比」）
                    "growth": total_growth(blocks)})
    return out

def summarize(rows) -> dict:
    """从**树的 rows** 重算七块合计 + 一个总计（给「总览」那张表用）。

    ⚠⚠ **为什么要有它，而不是把算好的合计直接存一份给前端**：
      接口层会**按身份把 `rows` 滤掉几家**（门店只看自己那家）。
      滤完还显示"名单内全部 28 家的合计"，门店看到的合计就比自己的明细
      大十几倍 —— 而页面上不会有任何异常，这是最难解释的一种错。
      ⇒ **合计必须跟着被滤过的 rows 一起重算**。

    ⚠ 合计**从同一棵树加**（不是另查一次库 / 不是另留一份累加表）⇒
      "合计 == 明细之和"**天然成立**，不靠两处口径对齐。
    """
    per = {b: {"cur": Totals(), "prev": Totals()} for b in BLOCKS}
    for r in rows or ():
        for blk in (r.get("blocks") or ()):
            name = blk.get("name")
            if name not in per:
                continue
            per[name]["cur"] = per[name]["cur"].plus(_tot(blk.get("cur")))
            per[name]["prev"] = per[name]["prev"].plus(_tot(blk.get("prev")))
    out = {}
    tot_c, tot_p = Totals(), Totals()
    for b in BLOCKS:
        c, p = per[b]["cur"], per[b]["prev"]
        out[b] = {"cur": c.as_dict(), "prev": p.as_dict(), "growth": compare(c, p)}
        tot_c = tot_c.plus(c)
        tot_p = tot_p.plus(p)
    return {"blocks": out,
            "total": {"cur": tot_c.as_dict(), "prev": tot_p.as_dict(),
                      "growth": compare(tot_c, tot_p)}}


def _tot(d) -> Totals:
    """`{"qty":…, "amount":…, "profit":…}` → `Totals`（树的 dict 反解回来）。"""
    d = d or {}
    return Totals(int(d.get("qty") or 0), float(d.get("amount") or 0),
                  float(d.get("profit") or 0))


def region_sums(rows, regions) -> dict:
    """按区域把门店行合成一行「共计」—— **后端算好再下发**（照 film 的 film-sum）。

    返回 `{区域: {store, region, blocks, growth, is_sum}}`，`blocks` 形状跟
    `tree()` 的一行完全一样（七块 + 系列 + 机型都并进去），前端 `one()` / `planCellHtml`
    直接当普通行渲染。

    ⚠ 环比用**加总后的 cur/prev 重算**（`compare` / `total_growth` 同一口径），
      **不拿各行率去平均** —— 跟 `summarize` 一条规矩。
    ⚠ 接口层滤完店会**重算一遍**（`web.filter_plan_rows`）——
      否则门店看到的分区共计还是全区的，跟 `summary` 那条同一个坑。
    ⚠ 区域来自**名单**（`regions` = `{店名: 区}`），不是行上自带的 ——
      和导出、前端排序同一份判据；没配区域的归「其他」。
    """
    by: Dict[str, List[dict]] = {}
    for r in rows or ():
        store = str(r.get("store") or "")
        reg = str((regions or {}).get(store) or "").strip() or "其他"
        by.setdefault(reg, []).append(r)
    out: Dict[str, dict] = {}
    for reg, grp in by.items():
        out[reg] = _merge_tree_rows(grp, label="共计：%s" % reg, region=reg)
    return out


def _merge_tree_rows(rows: List[dict], label: str = "", region: str = "") -> dict:
    """多行（`tree()` 形状）**逐层加总**成一行 —— 系列/机型也要并，不只块级。

    ⚠ 上层由下层加总、各层环比重算（`compare` / `total_growth`），
      别拿七块的百分比去平均（见 `total_growth` 的 docstring）。
    ⚠ 系列/机型**只列真有数的**（跟 `_blocks_of` 一致）；块级仍列满七块。
    """
    per: Dict[str, dict] = {}
    for r in rows or ():
        for blk in r.get("blocks") or ():
            name = str(blk.get("name") or "")
            if name not in per:
                per[name] = {"cur": Totals(), "prev": Totals(), "series": {}}
            cell = per[name]
            cell["cur"] = cell["cur"].plus(_tot(blk.get("cur")))
            cell["prev"] = cell["prev"].plus(_tot(blk.get("prev")))
            for s in blk.get("series") or ():
                sn = str(s.get("name") or "")
                if sn not in cell["series"]:
                    cell["series"][sn] = {"cur": Totals(), "prev": Totals(), "models": {}}
                sc = cell["series"][sn]
                sc["cur"] = sc["cur"].plus(_tot(s.get("cur")))
                sc["prev"] = sc["prev"].plus(_tot(s.get("prev")))
                for m in s.get("models") or ():
                    mn = str(m.get("name") or "")
                    if mn not in sc["models"]:
                        sc["models"][mn] = {"cur": Totals(), "prev": Totals()}
                    mc = sc["models"][mn]
                    mc["cur"] = mc["cur"].plus(_tot(m.get("cur")))
                    mc["prev"] = mc["prev"].plus(_tot(m.get("prev")))
    blocks = []
    for name in BLOCKS:
        cell = per.get(name) or {"cur": Totals(), "prev": Totals(), "series": {}}
        mrows = []
        for sn in sorted(cell["series"]):
            sc = cell["series"][sn]
            models = []
            for mn in sorted(sc["models"]):
                mc = sc["models"][mn]
                models.append({"name": mn, "cur": mc["cur"].as_dict(),
                               "prev": mc["prev"].as_dict(),
                               "growth": compare(mc["cur"], mc["prev"])})
            mrows.append({"name": sn, "cur": sc["cur"].as_dict(),
                          "prev": sc["prev"].as_dict(),
                          "growth": compare(sc["cur"], sc["prev"]), "models": models})
        blocks.append({"name": name, "cur": cell["cur"].as_dict(),
                       "prev": cell["prev"].as_dict(),
                       "growth": compare(cell["cur"], cell["prev"]), "series": mrows})
    return {"store": label, "region": region, "is_sum": True, "people": [],
            "blocks": blocks, "growth": total_growth(blocks)}


def missing_months(conn_days: int, span: Span) -> str:
    """上月数据够不够算环比 —— 不够就返回一句给人看的话（空串 = 够）。

    ⚠ 用户 2026-09-21 纠正过：「数据库是全月的」—— `erp-dump` 月初 3 天会把上月整个
      重拉一遍，所以正常跑着的机器**本来就有**。这道体检只兜一种情况：
      **那台机器整月没开机**（补漏错过了）。默默地拿半份上月去算环比，比不显示更糟。
    """
    if conn_days >= span.days:
        return ""
    return ("上月数据只覆盖 %d 天（应有 %d 天）—— 环比可能失真，"
            "等下一次抓数补上" % (conn_days, span.days))
