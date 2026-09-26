"""四池的**规则**（口径）—— 纯逻辑，**不碰数据库**（M15 / 阶段 3.5）。

照 `features/compliance/pos/` 那套分工：`pos_metric`（纯函数口径）↔ 这里；
`pos_report`（读库）↔ `store.py`。

⚠ 这个模块**只许 import 标准库** —— 口径要能脱离库单测（跟 `pos_metric` / `reconcile` 一样）。
"""

from __future__ import annotations

#: 云商 `串号标识` 里的**样机**标记。
#:
#: 实测（2026-09-17，`erp_sales` 全表 101732 行的 `串号标识`）：
#:   `样,新` 1816 · `s,样,新` 47 · `s,样，新,新` 8 · `样机,N,新` 6 · `N,样,新` 6 …
#: ⚠ 三个坑，缺一个就会漏：
#:   ① **位置不固定**（`样,新` 和 `N,样,新` 都有）⇒ 必须按逗号拆开**逐段**比
#:   ② **中文逗号**（`s,样，新,新`）⇒ 两种分隔符都要切
#:   ③ **有「样机」这种写法**，不只是「样」⇒ 用 `startswith`
#: ⚠ 另有一列 `lg_stock.tag_name`，那是**营销标签**（精品推荐/热销/预售…），
#:   **跟样机无关** —— 别找错地方。
SAMPLE_MARK = "样"

def is_sample_marker(marker) -> bool:
    """云商 `串号标识` 里有没有样机标记。

    **业务含义**（用户 2026-09-17 原话）：
    「云商卖了样机开单，但是玲珑开不了」——
    样机在玲珑那边**报不了量**，所以它会**一直挂在玲珑在库**，天天出现在 BC 里。
    **那是误报，要排除。**
    """
    if not marker:
        return False
    for part in str(marker).replace("，", ",").split(","):
        if part.strip().startswith(SAMPLE_MARK):
            return True
    return False

#: 池C 里**不算"卖了"**的单据类型 —— 退货后货回库，会同时出现在 C 和 D，
#: 那**是正常的**；不剔会造出一堆假异常（实测 `C∩D=10` 里 7 个就是这么来的）。
RETURN_BILL_TYPES = ("零售退", "分销退")

#: 事件种类（`net_sold` 的入参）
SALE = "sale"
RETURN = "return"


def event_kind(bill_type) -> str:
    """池C 一行的事件种类 —— 退货行 `RETURN`，其余（含 NULL / 核销 / 客情单）`SALE`。

    ⚠ **核销 / 客情单算"卖了"是有意的**（用户 2026-09-23 拍板方案 A：维持现状）：
    实测参考库它们**一个都没进 AD/BC** —— 客情单 1711 行全是手机贴膜且
    **零串号**；核销 245 个真串号进了池C 但**品类全是"其它"**（贴膜/保护壳）
    ⇒ 被六类过滤挡在 keep 之外。⚠ 边界：核销里有 707 行 head=「耳机麦克」
    判**音频（六类）**，现在没进 C **只因为那些行没有 ≥8 位串号** ——
    哪天出现串号级核销单（整机核销带真 SN），它会以六类进池C，
    那时要不要改成"不算卖"（方案 B/C）再议。
    （旁证：`config/stores.yaml` 的 `exclude` 含核销，注释"金额为 0"。）
    """
    return RETURN if str(bill_type or "") in RETURN_BILL_TYPES else SALE


def net_sold(events) -> bool:
    """时间序净额 —— 这个串号最终算不算「卖掉了」。**纯函数**。

    `events` = `[(kind, time)]`，`kind` 见 `event_kind`，`time` 是可字典序比较的
    时间串（`YYYY-MM-DD HH:MM:SS`；空串 = 没有时间，排在最早）。

    规则（用户 2026-09-23 拍的「时间序净额冲销」）：

    * 比 **最后一次销售** 和 **最后一次退货**：退货更晚 ⇒ 货回库了，**没卖掉**；
    * 退货之后**又卖出去** ⇒ 算卖掉（实测 `6HR0226528000127` 卖→退→再卖，
      是真差异，一刀切"有退货就剔"会把它误杀）；
    * **同一刻**分不出先后 ⇒ 判「没卖掉」（保守方向：宁可少报一条差异，
      也不报假差异 —— 本项目最忌讳假差异）。

    ⚠ 为什么用字符串比：`支付时间` 实测 146960 行全非空、格式统一到秒，
      字典序 == 时间序。日期短串（`2026-09-01`）比长串小 ⇒ 同天无时分秒时
      退货赢，方向仍是保守的那侧。
    ⚠⚠ **"有没有销售"看 `has_sale`，不看时间戳是否为空** —— 支付时间是**可空**的
      （测试和精简库里常为空）。拿 `last_sale` 的 truthiness 当"没卖过"，
      会把"卖了但没记时间"的机器判成没卖掉 ⇒ 从池C 消失 ⇒ BC 少报（真踩过）。
    """
    has_sale = has_return = False
    last_sale = ""
    last_return = ""
    for kind, t in events:
        t = str(t or "")
        if kind == RETURN:
            has_return = True
            if t > last_return:
                last_return = t
        else:
            has_sale = True
            if t > last_sale:
                last_sale = t
    if not has_sale:
        return False               # 只退没销（孤儿退货）：没卖掉
    if not has_return:
        return True                # 没退过：卖掉了（时间为空也算卖）
    return last_sale > last_return  # 同刻 / 销售没时间 → False（保守：货回库了）

#: 池A 里**不算"报了量"**的订单状态
CLOSED_STATUS = ("已关闭",)

#: 两个"有事"的象限，以及它们的说法（推送里直接用人话）
QUADRANT_LABELS = {
    "AD": "玲珑报了、云商没报",
    "BC": "云商报了、玲珑没报",
}

#: 明细表的列顺序（Excel / 推送共用一份，别处再排一次就会两边不一致）
DETAIL_COLS = (
    ("sn", "串号"),
    ("direction", "方向"),
    ("问题", "问题"),
    ("玲珑机型", "玲珑机型"),
    ("玲珑门店", "玲珑门店"),
    ("玲珑仓", "玲珑仓"),
    ("玲珑库龄", "玲珑库龄"),
    ("玲珑单号", "玲珑单号"),
    ("玲珑时间", "玲珑时间"),
    ("玲珑金额", "玲珑金额"),
    ("云商机型", "云商机型"),
    ("云商门店", "云商门店"),
    ("云商仓", "云商仓"),
    ("云商库龄", "云商库龄"),
    ("云商单号", "云商单号"),
    ("云商时间", "云商时间"),
    ("云商单据类型", "云商单据类型"),
    ("云商状态", "云商状态"),
)


def combine(a, b, c, c_sample, d) -> dict:
    """把四个集合合成四象限 —— **纯函数**（不碰库，能单测）。

    原来是 `quadrants(conn)` 后半段，2026-09-19（M15 / 阶段 3.5）搬出来：
    取数归 `store.quadrants`，**口径归这儿** —— 跟 `pos_metric` 一个地位。

    `c_sample` 是"云商卖出的**样机**"那一批（`is_sample_marker` 挑出来的）：
    ⚠ **单列一项**，不混进 BC 也不静默丢掉 —— "云商卖了样机、玲珑报不了量"
    是**已知的正常情况**，但排除掉多少台必须让人看得见
    （本项目最忌讳"少给了还不吭声"）。
    """
    out = {
        "A": a, "B": b, "C": c, "D": d,
        "AC": a & c, "AD": a & d, "BC": b & c, "BD": b & d,
        "BC_样机": b & c_sample,
    }
    c_all = c | c_sample                     # 算"只在 C"时样机也算 C
    every = a | b | c_all | d
    for name, s in (("A", a), ("B", b), ("C", c_all), ("D", d)):
        others = set()
        for n2, s2 in (("A", a), ("B", b), ("C", c_all), ("D", d)):
            if n2 != name:
                others |= s2
        out["only_" + name] = s - others
    out["all"] = every
    return out
