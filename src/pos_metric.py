"""POS 合规率 —— **纯函数内核，不做任何 IO**。

照着 `src/reconcile.py` 的样子写：业务规则只在这个文件里，输入是已经取好的数据，
输出是算好的分数。**不连数据库、不发请求** —— 所以它能脱离网络单独跑回归测试。

## 口径（2026-09-16 用户逐条确认）

    分数 = 分子 / 分母

    **分母** = Σ 订单金额，只算这些单：
        ① 非国补       —— 精确匹配（见下「两个口径」）
        ② 非即时零售   —— `business_type == 10`
        ③ 非 Care+     —— `scenario_type == 10`
        ④ **团单保留** —— 用户原话「算进去吧，这个是要走单独的申诉的」
      − 本月退货：扣掉被退原单的金额

    **分子** = Σ **非现金**支付金额（同一批订单）
        —— 用户原话「不是现金都算在分子里的」（微信 / 支付宝 / 花呗 / …）
      − 本月退货：扣掉被退原单的非现金部分

    **退货**：在**退货发生的当月**扣减，**不在原单当月**。
        用户原话「当月退货当月扣除，如果这单在分子或者分母里，做相应的扣减」。
        ⚠ 所以「9 月退 8 月的单」→ **9 月扣**，8 月不动。

## ⭐ 两个口径（用户要求并行出两份）

用户原话：「标签和备注分两个接口吧，一个按标签算，一个按备注算，出两个 pos 合规率的数据」

* **按标签** = `orderLabelList` 里有 `国补`
* **按备注** = `remark == '国补'`

⚠ 实测这两个**会不一致**（30 天窗口 4 单：3 单只有备注、1 单只有标签），
所以两个分数本来就是不同的数 —— 这不是 bug，是**就是要看两个**。

## ⚠ 三个实测事实，决定了这个指标的性质

1. **国补和即时零售的单 100% 是现金**（非现金 = 0）⇒
   **分子完全不受「国补怎么认」影响**，**分母怎么划分数就怎么走**。
   这是个「分母游戏」—— 口径写错会被当成经营问题。
2. **国补没有机器可读的标记**（试过 `scenario_type` / `business_type` /
   `order_business_type` / `business_mode_code` / `delivery_status`，**都区分不出来**），
   只能字符串精确匹配。⚠ 门店备注写法一变（「国补 」带空格、「国家补贴」）→
   **一整批单不会被排除** → 分母变大 → 分数被拉低。
   用户明确要求**精确匹配、不做模糊匹配、不加告警**（「门店忘了是门店傻逼」）。
3. **单笔大额现金单能让月度分数摆十几个点** ——
   实测 2026-08 有 1 张团单 39,000（全现金），一个人就把当月压掉 **10 个点**。
   ⇒ **看月度分数时必须同时看分母**，不然会把「这个月有笔团单」看成「POS 用少了」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: 两个口径的名字 → 用哪个字段判「国补」
BY_LABEL = "label"
BY_REMARK = "remark"
BOTH = (BY_LABEL, BY_REMARK)

#: 机器可读的排除编码（好过字符串匹配，别改回字符串）
INSTANT_RETAIL_BUSINESS_TYPE = 10      # 即时零售（与 remark 前缀「即时零售：」实测完全一致）
CARE_SCENARIO_TYPE = 10                # HUAWEI Care+ 服务单（与商品名含 Care+ 实测完全一致）

#: 唯一算「现金」的收款方式。**别的全都算非现金。**
#:
#: ⚠⚠ **不许写成白名单**（比如 `in ("银商MIS微信", "银商MIS支付宝")`）——
#: 用户 2026-09-16 两句话把这件事定死了：
#:   * 「不是现金都算在分子里的」
#:   * 「**银商mis刷卡算分子吧，属于非现金**」（他专门为这条回来纠正过一次）
#:
#: 收款方式字典实测有 8 种，其中 **4 种从没用过**：
#:   用过：现金(1) / 银商MIS微信(19-6) / 银商MIS支付宝(19-5) / 花呗分期(21-40)
#:   没用过：**银商MIS刷卡(4-4)** / 公对公打款(20-27) / 华为会员积分(13-36) / 支付宝(19-2)
#: 写成白名单的话，门店哪天开始**刷卡**或走**公对公打款**，
#: 那些单会被**静默漏出分子** —— 分数被低估，而且看不出原因。
CASH_MEDIA = "现金"

#: 「团单」类的备注取值（**精确匹配**，和国补一个规矩）。
#: 实测全库只有这两条：`团单`(39,000) / `团单出单`(2,520)。
#: ⚠ 用户 2026-09-16 定：团单**保留在分母里**（「这个是要走单独的申诉的」），
#: 所以默认 **不排除**；只有「申诉后口径」才把它排掉。
TEAM_REMARKS = ("团单", "团单出单")


def is_team(remark) -> bool:
    """这条备注算不算「团单」（申诉后才排除）。"""
    return str(remark or "").strip() in TEAM_REMARKS


def is_noncash(media_name) -> bool:
    """这个收款方式算不算**非现金**（= 进分子）。

    ⚠ **这是全项目唯一一处判「是不是现金」的地方** ——
    原来它写在 IO 层（`pos_report.load` 里 `!= "现金"`），
    **纯函数的测试根本覆盖不到**：哪天有人把 IO 层改成白名单，
    23 条测试照样全绿。挪进内核就是为了让它可测。
    """
    return str(media_name or "").strip() != CASH_MEDIA


@dataclass(frozen=True)
class Order:
    """一个订单 —— 只留算分要用的字段。"""

    document_no: str
    month: str                  # "2026-09"（下单月）
    amount: float               # 订单金额（**订单级**，别按 SN 展开）
    noncash: float              # 该单的非现金支付金额合计
    is_gb_label: bool = False   # orderLabelList 里有「国补」
    is_gb_remark: bool = False  # remark ==「国补」（精确）
    is_instant_retail: bool = False
    is_care: bool = False
    is_team: bool = False       # 团单 / 团单出单 —— **只在「申诉后口径」里排除**

    def eligible(self, by: str, exclude_team: bool = False) -> bool:
        """这个单进不进分母/分子。

        `exclude_team=True` = **申诉后口径**：把团单也排掉
        （用户要两个数并排看：现状用来申诉，申诉后是"如果通过"的样子）。
        """
        if self.is_instant_retail or self.is_care:
            return False
        if exclude_team and self.is_team:
            return False
        return not (self.is_gb_label if by == BY_LABEL else self.is_gb_remark)


@dataclass(frozen=True)
class Returned:
    """一张退货 —— 关键字段是**退货发生的月**，不是原单的月。"""

    month: str                  # 退货月（"2026-09"）
    amount: float               # 退回的金额（全额退就是原单金额）
    orig: Optional[Order] = None   # 原单；**查不到就是 None**（跨年、或不在库里）
    noncash_refund: float = 0.0    # 退款走非现金的金额
    # ⚠ **`noncash_refund` 只用来交叉核对，不参与扣减。**
    #   扣减看的是「**原单当时算在哪一边**」（见 `deduction`）——
    #   用户原话：「你得看这个对应的销售单是算在了分子里还是分母里还是都有，
    #   扣的时候也做对应的扣减」。
    #   实测 4 张退货的退款方式**都与原收款方式一致**，所以两种算法碰巧同结果；
    #   但原单现金买、退款走微信时，拿退款方式去扣分子就**扣错边**了。

    @property
    def matched(self) -> bool:
        return self.orig is not None

    def share(self) -> float:
        """退回金额占原单的比例（部分退货时 <1；原单金额为 0 时按 0 处理）。"""
        if not self.orig or not self.orig.amount:
            return 0.0
        return min(1.0, self.amount / self.orig.amount)

    def deduction(self, by: str, exclude_team: bool = False) -> tuple:
        """该从 (分母, 分子) 各扣多少。**看原单算在哪一边。**

        * 原单**只进了分母**（全额现金）→ 分母扣 `amount`，**分子不动**
        * 原单**两边都在**（有非现金）→ 分母扣 `amount`，
          分子扣 `原单的非现金 × 退回比例`（部分退货按比例摊）
        * 原单**压根没进**（国补/即时零售/Care+）→ **都不扣**（扣了反而错）
        * 原单**查不到** → **都不扣**（调用方记进 `orphan_returns`）
        """
        if not self.orig or not self.orig.eligible(by, exclude_team):
            return 0.0, 0.0
        return self.amount, self.orig.noncash * self.share()

    def odd_refund(self) -> bool:
        """退款走的方式和原收款方式对不上吗？（**只报告，不影响扣减**）

        对不上不一定错（门店可能手工改退款渠道），但值得看一眼 ——
        这种单子最容易把分子分母的对账搞乱。
        """
        if not self.orig:
            return False
        return abs(self.noncash_refund - self.orig.noncash * self.share()) > 0.01


@dataclass
class MonthResult:
    month: str
    den: float = 0.0            # 分母（已扣减退货）
    num: float = 0.0            # 分子（已扣减退货）
    orders: int = 0             # 进分母的单数
    returned: int = 0           # 本月退货单数
    cut_den: float = 0.0        # 退货从分母扣掉的
    cut_num: float = 0.0        # 退货从分子扣掉的
    orphan_returns: List[str] = field(default_factory=list)   # 原单查不到的退货
    odd_refunds: List[str] = field(default_factory=list)      # 退款方式和原收款方式对不上的（只报告）

    @property
    def rate(self) -> Optional[float]:
        """分数（百分数）。**分母为 0 时返回 None，不是 0** —— 那不是一个「0 分」。"""
        return None if not self.den else round(self.num / self.den * 100, 2)

    def to_dict(self) -> dict:
        return {"month": self.month, "分母": round(self.den, 2), "分子": round(self.num, 2),
                "分数": self.rate, "单数": self.orders, "退货单数": self.returned,
                "退货扣减": {"分母": round(self.cut_den, 2), "分子": round(self.cut_num, 2)},
                "原单查不到的退货": list(self.orphan_returns),
                "退款方式异常": list(self.odd_refunds)}


def months_of(orders, returns) -> List[str]:
    """数据里出现过哪些月（订单月 ∪ 退货月，都算）。"""
    return sorted({o.month for o in orders} | {r.month for r in returns})


def score_month(orders, returns, month: str, by: str = BY_LABEL,
                exclude_team: bool = False) -> MonthResult:
    """算一个月的分数。**纯函数。**

    ⚠ 退货扣减的两条规矩（用户原话「如果这单在分子或者分母里，做相应的扣减」）：
      1. 只在**退货当月**扣，原单当月不动；
      2. 原单**本来就没进分母**（国补 / 即时零售 / Care+）就**不扣** ——
         它压根没被算进去，扣了反而错。
    """
    res = MonthResult(month=month)
    for o in orders:
        if o.month != month or not o.eligible(by, exclude_team):
            continue
        res.den += o.amount
        res.num += o.noncash
        res.orders += 1

    for r in returns:
        if r.month != month:
            continue
        res.returned += 1
        if not r.matched:
            # ⚠ 原单查不到 → **不猜**，记下来让人去看（跨年退货就会这样）
            res.orphan_returns.append("month=%s amount=%.2f" % (r.month, r.amount))
            continue
        cd, cn = r.deduction(by, exclude_team)   # ⭐ **看原单算在哪一边**，不看退款走什么方式
        res.cut_den += cd
        res.cut_num += cn
        if r.odd_refund():
            res.odd_refunds.append("month=%s amount=%.2f" % (r.month, r.amount))

    res.den -= res.cut_den
    res.num -= res.cut_num
    return res


def score_all(orders, returns, by: str = BY_LABEL,
              exclude_team: bool = False) -> List[MonthResult]:
    """所有月，按月升序。"""
    return [score_month(orders, returns, m, by, exclude_team)
            for m in months_of(orders, returns)]


def both(orders, returns) -> Dict[str, List[MonthResult]]:
    """**两个口径一起出** —— 用户要的就是两份数据。"""
    return {by: score_all(orders, returns, by) for by in BOTH}


def totals(orders, returns, by: str = BY_LABEL,
           exclude_team: bool = False) -> MonthResult:
    """全窗口合起来算一个（不按月切）。用来跟月度交叉核对。"""
    whole = MonthResult(month="(全部)")
    for o in orders:
        if not o.eligible(by, exclude_team):
            continue
        whole.den += o.amount
        whole.num += o.noncash
        whole.orders += 1
    for r in returns:
        whole.returned += 1
        if not r.matched:
            whole.orphan_returns.append("month=%s amount=%.2f" % (r.month, r.amount))
            continue
        cd, cn = r.deduction(by, exclude_team)
        whole.cut_den += cd
        whole.cut_num += cn
        if r.odd_refund():
            whole.odd_refunds.append("month=%s amount=%.2f" % (r.month, r.amount))
    whole.den -= whole.cut_den
    whole.num -= whole.cut_num
    return whole


def render(by: str, months: List[MonthResult]) -> List[str]:
    """给人看的行（**纯字符串**，调用方自己 print）。"""
    head = "口径=%s（%s）" % (by, "按标签 orderLabelList" if by == BY_LABEL else "按备注 remark")
    out = [head, "  %-9s %13s %13s %9s %6s %8s" % ("月份", "分母", "分子", "分数", "单数", "退货扣")]

    def line(m, label=None):
        orphan = ""
        if m.orphan_returns:
            orphan += "  ⚠ 原单查不到 %d 张" % len(m.orphan_returns)
        if m.odd_refunds:
            orphan += "  ⚠ 退款方式对不上 %d 张" % len(m.odd_refunds)
        return "  %-9s %13.2f %13.2f %8s%% %6d %8.2f%s" % (
            label or m.month, m.den, m.num,
            "  —  " if m.rate is None else "%6.2f" % m.rate, m.orders, m.cut_den, orphan)

    for m in months:
        out.append(line(m))
    return out
