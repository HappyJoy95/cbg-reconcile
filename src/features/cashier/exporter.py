# -*- coding: utf-8 -*-
"""收银当月导出 —— 两个 sheet，列逐列对齐用户那份《9月份机场销售表》。

开发目标见 `.dsh/docs/2026-09-30-收银暂存入库与导出-开发目标.md`。
口径（用户 2026-09-30 逐条定的）：

* **一个品一行**：玲珑多行单按 `order_lines` 拆开；`合计` = 订单合计（每行重复），
  `金额` = 该商品自己的销售金额。手工单没有"订单"概念 ⇒ 合计取自身金额（已知近似，
  写进「说明」表）。
* **每天的行下面跟一条「M.D 合计」**（用户第 7 条）：数量/合计/金额/`#`/`.`/`-`
  与各支付列全加总；第二天的行接着往下排。
* `#` = 实际成本 = 政策表 `入库价 − 无条件单台返利金额 − 有条件最高单台返利金额`
 （表里有算好的 `成本` 列就直接用）；**政策表没有成本就按 0**（用户第 10 条）。
* `.` = 毛利 = `金额 − #`；`-` = so 奖励 —— 政策「时间」列（可能是具体日期、
  也可能是 `2026.04.01-2026.04.30` 这种时间段）罩得住销售日期**且**编码命中
  才给值，**拿不到填 0**（用户第 10 条）。
* 客户信息那批没数据的列**保留空列占位**（用户定）；智选商品成本表不管（用户定）。
* 支付按 `payments` 落到对应列（列名 = 录入卡清单，国补两列原名 付以旧换新(旧)/(新)）；
  多行单**只落首行** —— 每行都填会把求和翻倍。
* 第二张表 = **现行政策表**（`price_policy` **最新一份快照**、原样表头；快照保留
  之后老的份数在库里另查）。

⚠ 写盘走 `modules.notify.export_xlsx`（`out/exports/`、同名编号、运行记录、
  最后补一张「说明」表）—— 跟其它导出一个路子，这里只管"表长什么样"。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Dict, List, Optional, Sequence, Tuple

from ...storage import db as _db
from . import store

#: 销售表列头 —— 顺序 = 用户《9月份机场销售表》第 1 行逐列（**别调**，
#: 调了就跟人家那张表对不上位）。没数据的列保留空占位。
#: ⚠ 第 10 条（2026-09-30 用户）：`#` = 成本（政策表没有就按 **0**）、
#:   `.` = 毛利 = 金额 − 成本、新增 `-` = so 奖励（日期窗不命中填 **0**）；
#:   支付两列改名「国补实付 / 国补优惠」（原 付以旧换新(旧)/(新)）。
HEAD = [
    "时间", "品类", "编码", "明细", "序列号", "数量", "合计", "金额",
    "#", ".", "-", "销售员",
    "助手", "C扫B", "POS", "现金", "公对公",
    "国补实付", "国补优惠", "预收款",
    "企业微信", "支付宝直连", "微信直连",
    "发票备注", "服务", "备注", "京东到家配送地址", "姓名", "电话",
    "身份证号", "住址（送货地址）", "购买机型", "金额", "发票号",
]

#: 能落值的支付列（= 录入卡的 `cashierPAY_METHODS`，存原始字符串不是枚举）
PAY_METHODS = [
    "助手", "C扫B", "POS", "现金", "公对公",
    "国补实付", "国补优惠", "预收款",
    "企业微信", "支付宝直连", "微信直连",
]

#: 每日「合计」行要加总的列（支付列另外按 PAY_METHODS 加总）
SUM_COLS = ["数量", "合计", "金额", "#", ".", "-"]

#: 政策表还没刷新时那张 sheet 的兜底表头（照用户 Sheet1 那份）
POLICY_FALLBACK_HEAD = [
    "商品名称", "商品编码", "入库价", "提货价*",
    "无条件单台返利金额", "有条件最高单台返利金额", "成本", "so", "时间",
]

MONEY_FMT = "#,##0.00"


def _num(v) -> Optional[float]:
    """任意值 → float；不是数（含 nan/inf）当"没有"回 None。"""
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _t(s) -> str:
    """`2026-09-30 10:00` → `"9.30"`（参考表「时间」列就是月.日）。"""
    s = str(s or "")
    try:
        return "%d.%d" % (int(s[5:7]), int(s[8:10]))
    except (IndexError, ValueError):
        return s


def _policy_facts(root=None) -> Tuple[Dict[str, float], Dict[str, list]]:
    """**最新一份**政策快照 → `(成本表, so 日期窗表)`。

    * 成本表 `{编码: 实际成本}` —— 缺成本列就算「入库价 − 无条件 − 有条件」，
      都算不出**不进表**（`_line_row` 那边按 0 兜底，用户第 10 条）；
    * so 表 `{编码: [(起, 止, 奖励), …]}` —— 政策「时间」列可能是具体日期
      也可能是时间段（`2026.04.01-2026.04.30`），都要认（用户第 10 条）。
    """
    costs: Dict[str, float] = {}
    sos: Dict[str, list] = {}
    conn = _db.open_db(str(store.ensure(root)))
    try:
        try:
            mx = conn.execute("SELECT MAX(fetched_at) FROM price_policy").fetchone()[0]
            if not mx:
                return costs, sos
            rows = conn.execute(
                "SELECT * FROM price_policy WHERE fetched_at=?", (mx,)).fetchall()
        except Exception:                                   # noqa: BLE001
            return costs, sos                               # 还没刷新过政策表
        for raw in rows:
            r = dict(raw)
            code = str(r.get("goods_code") or r.get("商品编码") or "").strip()
            if not code:
                continue
            cost = _num(r.get("成本"))
            if cost is None:
                base = _num(r.get("入库价"))
                if base is None:
                    # 用户 Sheet1 的入库价是公式「提货价* − so」—— 没存值就照算
                    t = (_num(r.get("提货价*")) or _num(r.get("基准提货价*"))
                         or _num(r.get("提货价")))
                    if t is not None:
                        base = t - (_num(r.get("so")) or 0.0)
                if base is not None:
                    cost = (base
                            - (_num(r.get("无条件单台返利金额")) or 0.0)
                            - (_num(r.get("有条件最高单台返利金额")) or 0.0))
            if cost is not None:
                costs[code] = round(cost, 2)
            win = _parse_window(r.get("时间"))
            if win:
                so = _num(r.get("so"))
                sos.setdefault(code, []).append(
                    (win[0], win[1], so if so is not None else 0.0))
    finally:
        conn.close()
    return costs, sos


def _parse_window(val) -> Optional[Tuple[_dt.date, _dt.date]]:
    """政策「时间」列 → `(起, 止)`；认不出回 `None`（= 不参与 so 命中）。

    照用户 Sheet1 的两种写法：`2026.04.01-2026.04.30`（时间段）、
    `2026.04.01`（具体日期）；分隔符 `.` `/` 都认，短式 `04.01-04.30` 借前半的年。
    """
    s = str(val or "").strip().replace("/", ".")
    if not s:
        return None
    parts = [p.strip() for p in s.split("-") if p.strip()]
    if not parts:
        return None

    def one(p: str, year: Optional[int] = None):
        m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$", p)
        if m:
            try:
                return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        m = re.match(r"^(\d{1,2})\.(\d{1,2})$", p)
        if m and year:
            try:
                return _dt.date(year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return None
        return None

    start = one(parts[0])
    if start is None:
        return None
    end = one(parts[1], year=start.year) if len(parts) > 1 else start
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    return start, end


def _cost_of(code: str, costs: Dict[str, float]) -> Optional[float]:
    code = str(code or "").strip()
    if not code:
        return None
    if code in costs:
        return costs[code]
    # 聚合卡的编码是 "a|b" 拼的 —— 逐段找，第一个命中的算数
    for part in code.split("|"):
        part = part.strip()
        if part and part in costs:
            return costs[part]
    return None


def _so_of(code: str, sold_day: str, sos: Dict[str, list]) -> float:
    """so 奖励：编码命中**且**销售日期落在政策「时间」窗内 → 奖励值，
    否则 **0**（用户第 10 条：拿不到就填 0）。"""
    d = None
    try:
        d = _dt.date(int(str(sold_day)[:4]), int(str(sold_day)[5:7]),
                     int(str(sold_day)[8:10]))
    except (TypeError, ValueError):
        return 0.0
    code = str(code or "").strip()
    for c in ([code] + [p.strip() for p in code.split("|")]) if code else []:
        for start, end, val in sos.get(c, ()):
            if start <= d <= end:
                return float(val)
    return 0.0


def _line_row(time_s: str, category: str, code, name, sn, qty, total,
              amount, seller, pays: dict, note: str,
              costs: dict, sos: dict = None, sold_day: str = "") -> list:
    d = {h: None for h in HEAD}
    d["时间"] = time_s
    if category:
        d["品类"] = category
    if code:
        d["编码"] = code
    if name:
        d["明细"] = name
    if sn:
        d["序列号"] = sn
    d["数量"] = qty
    d["合计"] = total
    d["金额"] = amount
    # `#` 成本：政策表没有就按 **0**（用户第 10 条 —— 不再留空）
    cost = _cost_of(code, costs)
    d["#"] = 0.0 if cost is None else cost
    if _num(amount) is not None:
        d["."] = round(float(amount) - float(d["#"]), 2)
    # `-` so 奖励：窗内给值，拿不到给 **0**
    d["-"] = _so_of(code, sold_day, sos or {})
    if seller:
        d["销售员"] = seller
    for method, amt in (pays or {}).items():
        if method in PAY_METHODS:
            d[method] = amt
    if note:
        d["备注"] = note
    return [d[h] for h in HEAD]


def _pay_map(payments) -> dict:
    out = {}
    for p in (payments or []):
        if isinstance(p, dict):
            m = str(p.get("method") or "").strip()
            a = _num(p.get("amount"))
            if m and a is not None:
                out[m] = a
    return out


def _entry_rows(e: dict, conn, costs: dict, sos: dict = None) -> List[list]:
    """一条流水（= 一个订单）→ 一到多行。

    优先级（2026-09-30 第三轮，用户定「导出跟卡片商品行走」）：
    ① 卡片有 `products` → 每行商品一行（**品类取行上的**，空了回退卡片品类）；
    ② 老玲珑卡没商品行 → 查 `order_lines` 拆行；
    ③ 都没有 → 整卡一行（老口径）。
    之后 **每个配件再补一行**：品类恒「配件」、数量 1、编码/SN 空、
    合计 = 订单合计（用户选项：配件自成一行）。
    """
    time_s = _t(e.get("sold_at"))
    sold_day = str(e.get("sold_at") or "")[:10]
    category = str(e.get("category") or "")
    seller = str(e.get("seller") or "")
    note = str(e.get("note") or "")
    pays = _pay_map(e.get("payments"))
    total = e.get("amount")
    prods = e.get("products") or []
    out: List[list] = []
    if prods:
        for i, p in enumerate(prods):
            # 支付只落首行：每行都填的话，把支付列求和会翻倍
            out.append(_line_row(
                time_s, str(p.get("category") or "") or category,
                p.get("code"), p.get("name"), p.get("sn"),
                p.get("quantity"), total, p.get("amount"),
                seller, pays if i == 0 else {}, note, costs, sos, sold_day))
    else:
        lines: Sequence[dict] = []
        dn = str(e.get("external_id") or "")
        if e.get("source") == "linglong" and dn:
            try:
                lines = [dict(r) for r in conn.execute(
                    "SELECT * FROM order_lines WHERE document_no=? ORDER BY line_no",
                    (dn,)).fetchall()]
            except Exception:                                   # noqa: BLE001
                lines = []
            if lines:
                try:
                    o = conn.execute(
                        "SELECT included_tax_amount FROM orders WHERE document_no=?",
                        (dn,)).fetchone()
                    if o and _num(o[0]) is not None:
                        total = _num(o[0])      # 合计 = 订单合计（每行重复）
                except Exception:                               # noqa: BLE001
                    pass
        if not lines:
            out.append(_line_row(time_s, category, e.get("goods_code"),
                                 e.get("goods_name"), e.get("sn"),
                                 e.get("quantity"), total, e.get("amount"),
                                 seller, pays, note, costs, sos, sold_day))
        else:
            for i, ln in enumerate(lines):
                amt = _num(ln.get("included_tax_amount"))
                if amt is None:
                    up = _num(ln.get("unit_price"))
                    q = _num(ln.get("quantity"))
                    if up is not None:
                        amt = round(up * (q if q is not None else 1), 2)
                if amt is None and len(lines) == 1:
                    amt = _num(e.get("amount"))
                out.append(_line_row(
                    time_s, category,
                    ln.get("ean") or ln.get("sku") or "",
                    ln.get("item_name") or "", ln.get("sn") or "",
                    _num(ln.get("quantity")) if _num(ln.get("quantity")) is not None
                    else e.get("quantity"),
                    total, amt, seller, pays if i == 0 else {}, note,
                    costs, sos, sold_day))
    # 配件：每个一行（品类恒「配件」，支付不再落 —— 首行商品已经有了）
    for acc in (e.get("accessories") or []):
        if not isinstance(acc, dict):
            continue
        out.append(_line_row(time_s, "配件", None, acc.get("name"), None,
                             1, total, acc.get("amount"), seller, {}, note,
                             costs, sos, sold_day))
    return out


def _policy_sheet(root=None) -> Tuple[list, List[list]]:
    """**最新一份** `price_policy` 快照 → `(表头, 行)`（原样表头，记账列不进导出）。

    ⚠ 快照保留（2026-09-30）之后老的份数也在库里 —— 这张 sheet 只导
      `MAX(fetched_at)` 那份（导出的是"现行"政策表，历史另查库）。
    """
    conn = _db.open_db(str(store.ensure(root)))
    try:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(price_policy)")]
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM price_policy WHERE fetched_at ="
                " (SELECT MAX(fetched_at) FROM price_policy)").fetchall()]
        except Exception:                                   # noqa: BLE001
            cols, rows = [], []
        keep: List[Tuple[str, str]] = []
        has_cn = "商品编码" in cols
        for c in cols:
            if c == "fetched_at":
                continue
            if c == "goods_code":
                if not has_cn:
                    keep.append((c, "商品编码"))     # 老库只有记账名 ⇒ 换回原表头
                continue
            keep.append((c, c))
        head = [name for _, name in keep]
        body = [[r.get(col) for col, _ in keep] for r in rows]
    finally:
        conn.close()
    if not body:
        head = list(POLICY_FALLBACK_HEAD)
        body = [["（政策表还没刷新 —— 控制台「刷新政策数据」拉一次，"
                 "这里就是现行整表）"] + [None] * (len(head) - 1)]
    return head, body


def sheets(root=None, month: str = "") -> Dict[str, Tuple[list, List[list]]]:
    """当月那张销售表 + 政策表 —— `{表名: (表头, 行)}`。

    ⚠ 月份格式不对抛 `ValueError`（调用方转成给人看的 why）。
    ⚠ 行序 = **时间正序**（参考表从月初排到月末），同天按 id 正序；
      **每天的行下面跟一条「M.D 合计」**（用户第 7 条）—— 数量/合计/金额/
      `#`/`.`/`-` 与各支付列全加总；「合计」列加的是**各订单的合计**
      （按单算一次，不按行加 —— 多行单的合计是重复的）。
    """
    month = str(month or "").strip()
    if len(month) != 7 or month[4] != "-":
        raise ValueError("月份格式不对（要 2026-09）")
    rows = [r for r in store.list_entries(root)
            if str(r.get("sold_at") or "").startswith(month)]
    rows.sort(key=lambda r: (str(r.get("sold_at") or ""), int(r.get("id") or 0)))
    costs, sos = _policy_facts(root)
    conn = _db.open_db(str(store.ensure(root)))
    try:
        body: List[list] = []
        cur_day = ""
        sums: Dict[str, float] = {}
        order_total = 0.0
        for e in rows:
            day = str(e.get("sold_at") or "")[:10]
            if cur_day and day != cur_day:
                body.append(_total_row(cur_day, sums, order_total))
                sums, order_total = {}, 0.0
            cur_day = day
            grow = _entry_rows(e, conn, costs, sos)
            for row in grow:
                for col in SUM_COLS + PAY_METHODS:
                    v = _num(row[HEAD.index(col)])
                    if v is not None:
                        sums[col] = sums.get(col, 0.0) + v
            order_total += _num(e.get("amount")) or 0.0
            body.extend(grow)
        if cur_day:
            body.append(_total_row(cur_day, sums, order_total))
    finally:
        conn.close()
    head, policy = _policy_sheet(root)
    return {
        "%d月份销售表" % int(month[5:7]): (HEAD, body),
        "政策表": (head, policy),
    }


def _total_row(day: str, sums: dict, order_total: float) -> list:
    """某天的「合计」行 —— 时间列写 `M.D 合计`，数字列按 `sums` 加总。"""
    d = {h: None for h in HEAD}
    try:
        d["时间"] = "%d.%d 合计" % (int(day[5:7]), int(day[8:10]))
    except (TypeError, ValueError, IndexError):
        d["时间"] = "合计"
    d["明细"] = "合计"
    for col in SUM_COLS:
        if col == "合计":
            d[col] = round(order_total, 2)
            continue
        v = sums.get(col)
        if v is not None:
            d[col] = round(v, 2)
    for col in PAY_METHODS:
        v = sums.get(col)
        if v is not None:
            d[col] = round(v, 2)
    return [d[h] for h in HEAD]


def export(root=None, month: str = "", who: str = "") -> dict:
    """导出当月两个 sheet 并落盘（返回值形状跟其它导出一致，绝不抛）。"""
    from ...modules import notify
    try:
        book = sheets(root, month)
    except ValueError as e:
        return {"ok": False, "why": str(e)}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "why": "%s: %s" % (type(e).__name__, e)}
    meta = {
        "月份": month,
        "口径": "一个品一行；合计=订单合计（手工单取自身金额）；"
                "金额=单商品金额；每天行末尾有「M.D 合计」",
        "成本(# )": "政策表 入库价 − 无条件单台返利 − 有条件最高单台返利"
                "（政策表没有就按 0）",
        "毛利(.)": "金额 − 成本",
        "so(-)": "政策「时间」窗罩得住销售日期且编码命中 → 给奖励值，否则 0",
        "支付": "多行单只落首行（防求和翻倍）；没数据的列留空占位",
        "包含": "当月已入库 + 待入库（暂存）的全部非排除行",
    }
    return notify.export_xlsx(book, name="收银-%s" % month, root=root, who=who,
                              meta=meta)


__all__ = ["HEAD", "PAY_METHODS", "sheets", "export"]
