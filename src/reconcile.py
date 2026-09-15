"""对账内核：归属过滤 + 差集。**业务规则只在这个文件里**，纯函数、无 IO，可单独回归测试。

规则（2026-09-15 与业务确认，见设计文档 §四）：

    云商当日销售
      ├─ 串号标识含本店码（忽略大小写，`Y` / `y` 都算）
      ├─ 卖出店 = 本店            → 纳入 ✅
      ├─ 卖出店 = 其他体验店       → 跳过（体验店之间走调拨，由买入店报量）
      ├─ 卖出店 = 非体验店         → 纳入 ✅ ← 核心场景「B 店卖了 A 店的货」
      └─ 退货 / 核销               → 跳过

    差集 = 未报量

**反向差异不做过滤，只做分类**（2026-09-15 业务补充）：
「体验店卖另一个体验店的货」有两个方向 ——

    方向一：本店的货被别的体验店卖了   → 调拨处理，不进未报量（上面第 3 条）
    方向二：别的体验店的货在本店卖了、还被报量在本店名下
            → **要保留**：门店靠它核对「调拨过来的货有没有出库」

方向二不是错，是**有用的信息**，所以不丢弃、也不算成"未报量"，而是
分类成「调拨进来」（标识属于其他体验店）和「来源不明」（标识不是任何体验店 /
云商里查不到），并把**云商侧的出库记录带上**，让人一眼能核对：

    华为报了量 + 云商有出库记录   → 正常 ✅
    华为报了量 + 云商查无出库     → ⚠️ 要问一句：货呢？

> 教训：对账工具的取舍不是"报不报"，而是"报出来有没有用"。
> 有用就带着上下文报，而不是一刀切掉 —— 切掉就没法核对了。
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field

# 串号标识里跟"门店归属"无关的标记（机况 / 来源）。见 config/stores.yaml。
MARKER_NOISE = {"新", "样", "外调", "办公机", "旧", "自用", "外地", "销售助手", "售后换回"}


def marker_codes(marker) -> set[str]:
    """把 `Y,新` / `新,Y` / `s,新` 拆成门店码集合，丢掉机况词。

    ⚠ 顺序不固定（实测同时见过 `W,新` 和 `新,Y`），所以必须按逗号拆开逐个比。
    """
    if marker is None:
        return set()
    out = set()
    for part in str(marker).replace("，", ",").split(","):
        c = part.strip()
        if c and c not in MARKER_NOISE:
            out.add(c)
    return out


def is_own_marker(marker, code: str) -> bool:
    """串号标识是否含本店码。**忽略大小写** —— 顺和汇在数据里是小写 `s`。"""
    if not code:
        return False
    return code.strip().lower() in {c.lower() for c in marker_codes(marker)}


# --------------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class SaleRow:
    """云商销售明细的一行（对账只关心这些列）。"""

    sn: str
    item: str
    doc_no: str
    doc_type: str
    seller: str
    pay_time: str
    amount: str
    marker: str
    clerk: str = ""

    @classmethod
    def from_erp(cls, d: dict) -> "SaleRow":
        def g(k):
            v = d.get(k)
            return "" if v is None else str(v).strip()

        return cls(
            sn=g("串号"), item=g("商品名称"), doc_no=g("单号"), doc_type=g("单据类型"),
            seller=g("门店"), pay_time=g("支付时间"), amount=g("金额"),
            marker=g("串号标识"), clerk=g("店员"),
        )


@dataclass(frozen=True)
class ReverseItem:
    """华为报了量、但云商这边"该本店报量的销售"里没有的那台。

    类型：
      调拨进来  —— 串号标识属于**其他体验店**（货是调过来的）
      来源不明  —— 标识不是任何体验店（J/jc/外调…），或云商里根本查不到
    """

    sn: str
    info: dict                 # 华为侧：documentNo / item / amount / time / guide
    kind: str
    yun: tuple = ()            # 云商侧所有相关行（SaleRow），空 = 查不到出库

    @property
    def shipped(self) -> bool:
        """云商里有没有出库记录 —— 门店核对调拨货就看这一列。"""
        return bool(self.yun)


@dataclass
class ReconcileResult:
    missing: list[SaleRow] = field(default_factory=list)          # 云商有、华为无
    matched: list[tuple] = field(default_factory=list)            # (SaleRow, 华为信息)
    reverse: list[ReverseItem] = field(default_factory=list)      # 华为有、云商该管销售里没有
    skipped: dict = field(default_factory=dict)                   # 跳过的原因 → 条数
    total_rows: int = 0                                           # 云商原始行数

    @property
    def ok(self) -> bool:
        return not self.missing

    # ---- 反向差异的两个切片（前端/报表按这个分类展示）----
    @property
    def reverse_transfer(self) -> list:
        return [r for r in self.reverse if r.kind == KIND_TRANSFER]

    @property
    def reverse_unknown(self) -> list:
        return [r for r in self.reverse if r.kind != KIND_TRANSFER]

    @property
    def reverse_unshipped(self) -> list:
        """调拨进来的、但云商查不到出库记录的 —— 这才是要追的。"""
        return [r for r in self.reverse if not r.shipped]


KIND_TRANSFER = "调拨进来"
KIND_UNKNOWN = "来源不明"


# ------------------------------------------------------------------- 反向差异
def sn_row_index(rows: list[dict]) -> dict[str, list[SaleRow]]:
    """**全部**云商行（不做任何过滤）按 SN 归组。

    用来回答两件事："华为报的这个 SN 在云商里是谁家的货？" 以及
    "它到底出库了没有？"
    """
    idx: dict[str, list[SaleRow]] = collections.defaultdict(list)
    for d in rows:
        row = SaleRow.from_erp(d)
        if row.sn:
            idx[row.sn].append(row)
    return dict(idx)


def classify_reverse(reported: dict, seen: set, index: dict, *, own_marker: str,
                     experience_markers) -> list[ReverseItem]:
    """把「华为有、云商该管销售里没有」的 SN 分类，并带上云商出库情况。"""
    others = {m.strip().lower() for m in experience_markers if m} - {own_marker.strip().lower()}
    out: list[ReverseItem] = []
    for sn, info in reported.items():
        if sn in seen:
            continue
        yun = tuple(index.get(sn) or ())
        codes = set()
        for r in yun:
            codes |= {c.strip().lower() for c in marker_codes(r.marker)}
        kind = KIND_TRANSFER if (codes & others) else KIND_UNKNOWN
        out.append(ReverseItem(sn=sn, info=info, kind=kind, yun=yun))
    out.sort(key=lambda r: (r.kind != KIND_TRANSFER, r.sn))
    return out


# ----------------------------------------------------------------------- 过滤
def classify_sales(rows: list[dict], *, marker: str, own_store: str,
                   experience_stores: set[str], include_types=(),
                   exclude_types=()) -> tuple[list[SaleRow], dict]:
    """把云商销售明细筛成「该由本店报量的那些行」。返回 (保留行, 跳过统计)。"""
    include, exclude = set(include_types or ()), set(exclude_types or ())
    kept: list[SaleRow] = []
    skipped: collections.Counter = collections.Counter()

    for d in rows:
        row = SaleRow.from_erp(d)
        if not row.sn:
            skipped["无串号"] += 1
            continue
        if not is_own_marker(row.marker, marker):
            skipped["非本店归属"] += 1
            continue
        if row.doc_type in exclude:
            skipped[f"排除单据类型:{row.doc_type}"] += 1
            continue
        if include and row.doc_type not in include:
            # 名单外的类型不静默丢弃 —— 计数上报，让人看得见
            skipped[f"未识别单据类型:{row.doc_type or '(空)'}"] += 1
            continue
        if row.seller != own_store and row.seller in experience_stores:
            skipped["其他体验店卖出(调拨处理)"] += 1
            continue
        kept.append(row)
    return kept, dict(skipped)


# ----------------------------------------------------------------------- 差集
def reconcile(sales: list[SaleRow], reported: dict[str, dict], *,
              total_rows: int | None = None, skipped: dict | None = None,
              index: dict | None = None, own_marker: str = "",
              experience_markers=()) -> ReconcileResult:
    """差集。

    index 传 `sn_row_index(全部云商行)` —— 反向差异靠它分类并带上出库情况。
    不传的话反向差异会全部落在「来源不明」，仍然不会丢。
    """
    res = ReconcileResult(total_rows=total_rows if total_rows is not None else len(sales),
                          skipped=dict(skipped or {}))
    seen = set()
    for s in sales:
        seen.add(s.sn)
        if s.sn in reported:
            res.matched.append((s, reported[s.sn]))
        else:
            res.missing.append(s)
    res.reverse = classify_reverse(reported, seen, index or {}, own_marker=own_marker,
                                   experience_markers=experience_markers)
    return res
