"""POS 合规率 —— 从库里取数、算、打印。

**IO 只在这个文件里**；口径全在 `pos_metric.py`（纯函数、可单测）。

用法（⚠ 2026-09-19 起在 `features/compliance/pos/` 下，别再用老路径）：
    python -m src.features.compliance.pos.pos_report --db out/cbg-2026.db
    python -m src.features.compliance.pos.pos_report --db out/cbg-2026.db --month 2026-09  # 只看一个月
    python -m src.features.compliance.pos.pos_report --db out/cbg-2026.db --json           # 出 JSON
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import pos_metric as pm

def load(conn):
    """把库里的订单/退货转成 `pos_metric` 要的纯数据。

    ⚠ 三处不能想当然：
      1. **金额用 `orders.included_tax_amount`**（订单级）——
         按 SN 展开后再 SUM 会翻倍（实测 11998 vs 5999）。
      2. **非现金 = 支付方式不是「现金」** —— 微信 / 支付宝 / 花呗 / 将来别的都算。
      3. **退货月取 `returns.doc_create_time` 的月**（不是原单的月）——
         口径就是「当月退货当月扣除」。
      4. **团单标记 `is_team` 从 remark 取** —— 它**默认不排除**，
         只影响「申诉后口径」（用户 2026-09-16 要两个数并排看）。
    """
    orders = []
    conn.row_factory = sqlite3.Row          # ⚠ 不设的话下面 o["document_no"] 会炸（行是元组）
    for o in conn.execute("SELECT * FROM orders"):
        # ⚠ 2026-09-21：**连 `payment_medias` 一起取** —— 官方公式的扣减项
        #   （现金 / 记账）判的是 `open_cash_drawer` 和 `pay_channel_name`，
        #   光看 `media_name` 认不出「公对公打款」其实是**记账**。
        #   ⚠ 关联键是 (media_no, media_member_no) 两个字段 —— 只按 media_no 会串。
        pays = list(conn.execute(
            "SELECT p.media_name, p.payment_amount, p.payment_time,"
            "       m.pay_channel_name, m.open_cash_drawer"
            "  FROM payments p"
            "  LEFT JOIN payment_medias m"
            "    ON m.media_no = p.media_no AND m.media_member_no = p.media_member_no"
            " WHERE p.document_no=?", (o["document_no"],)))
        day = (o["doc_create_time"] or "")[:10]
        orders.append(pm.Order(
            document_no=o["document_no"],
            month=(o["doc_create_time"] or "")[:7],
            amount=o["included_tax_amount"] or 0.0,
            noncash=sum(p["payment_amount"] or 0.0 for p in pays if pm.is_noncash(p["media_name"])),
            day=day,
            pays=tuple(pm.Pay(day=(p["payment_time"] or "")[:10] or day,
                              amount=p["payment_amount"] or 0.0,
                              media_name=p["media_name"] or "",
                              pay_channel=p["pay_channel_name"] or "",
                              open_cash_drawer=p["open_cash_drawer"] or 0)
                       for p in pays),
            is_gb_label=("国补" in (o["order_label_list"] or "").split("|")),
            is_gb_remark=((o["remark"] or "") == "国补"),
            is_instant_retail=(o["business_type"] == pm.INSTANT_RETAIL_BUSINESS_TYPE),
            is_care=(o["scenario_type"] == pm.CARE_SCENARIO_TYPE),
            is_team=pm.is_team(o["remark"])))

    by_no = {o.document_no: o for o in orders}
    returns = []
    for r in conn.execute("SELECT * FROM returns"):
        refunds = list(conn.execute(
            "SELECT media_name, refund_amount FROM return_refunds WHERE document_no=?",
            (r["document_no"],)))
        returns.append(pm.Returned(
            month=(r["doc_create_time"] or "")[:7],
            day=(r["doc_create_time"] or "")[:10],
            amount=r["included_tax_amount"] or 0.0,
            noncash_refund=sum(x["refund_amount"] or 0.0 for x in refunds if pm.is_noncash(x["media_name"])),
            orig=by_no.get(r["related_doc_no"])))
    return orders, returns


# ------------------------------------------------------------------ 推送用
#: 推送里最多列几个月。POS 是**月度**指标，全列会很长 ——
#: 而企微 markdown 有 4096 **字节**上限（中文一个字 3 字节）。
NOTIFY_MONTHS = 6


def _pct(v):
    """分母为 0 时分数是 `None`，显示 `—` —— **不是 0%**（建店当月就是这种）。

    ⚠ 推送正文里**不要**给 `—` 补空格对齐：那是给人扫一眼的东西，
    补了反而像排版坏了。
    """
    return "—" if v is None else "%.2f%%" % v


def official_rate(row, by: str = pm.BY_LABEL) -> Optional[float]:
    """这一行的**官方口径**分数；**没有就是 `None`**（不回落成我们那个）。

    ⚠ 回落会把"我们算的数"标成"官方"—— 那是撒谎（本项目最忌讳的一类错：
      判据和显示不一致）。老 payload（没有 `official` 字段）宁可只报旧口径。
    """
    off = (row.get("official") or {}).get(by) or {}
    return off.get("rate")


def headline(rows) -> str:
    """一句话结论：最新那个月。邮件主题和企微首行都用它。

    ⚠ 2026-09-21 起报的是**官方口径**（PPT《POS合规：计算逻辑及方法》：
      现金+记账 扣减、不扣退货、日均值）—— 它跟财经那份成绩是一个算法。
      我们自己的旧口径（整月汇总、扣退货）在正文里当**对照**列着，不丢。
    """
    if not rows:
        return "没有可算的月份"
    r = rows[-1]
    rate = official_rate(r)
    if rate is None:                       # 老 payload：只能说"我们这个口径"
        rate = (r.get(pm.BY_LABEL) or {}).get("rate")
    return "%s POS 使用率 %s%s" % (r["month"], _pct(rate),
                                   "（暂定）" if r["provisional"] else "")


def console_lines(rows) -> list:
    """命令行 / 日志里那几行（每月一条）。

    ⚠ **文案只在这里写一份** —— 手动跑（`cmd_pos`）和每天那趟（`run_daily`）
    打的是同一份，改格式时不会只改一边。
    ⚠ 分母为 0 时分数是 `None`，**不是 0** ⇒ 要打成 `—`，
    不然"这个月没有分母"会被看成"合规率 0%"。
    """
    out = []
    for row in rows:
        la = row[pm.BY_LABEL]
        fmt = lambda v: "  —  " if v is None else "%5.2f" % v
        off = official_rate(row)
        if off is None:                    # 老 payload：只能报旧口径（别标成"官方"）
            out.append("  %s%s  按标签 %s%%（申诉后 %s%%）  分母 %s"
                       % (row["month"], "（暂定）" if row["provisional"] else "        ",
                          fmt(la["rate"]), fmt(la["ap_rate"]), format(la["den"], ",.2f")))
            continue
        out.append("  %s%s  ⭐官方 %5.2f%%（日均）· 旧口径 %s%%（申诉后 %s%%）  分母 %s"
                   % (row["month"], "（暂定）" if row["provisional"] else "        ",
                      off, fmt(la["rate"]), fmt(la["ap_rate"]), format(la["den"], ",.2f")))
    return out


def notify_lines(rows, limit: int = NOTIFY_MONTHS) -> list:
    """POS 分数的**人话版** —— 邮件正文和企微正文**共用这一份**。

    ⚠ 两边各写一遍的话必然有一天对不上（这个项目已经在"同一件事两个实现"
    上栽过三次：`is_noncash` 藏在 IO 层、`pos_export` 重算退货、workspace
    留了两份测试）。所以：**格式只在这里定义一次**。
    """
    out = []
    tail = rows[-limit:] if limit else rows
    if len(rows) > len(tail):
        out.append("（只列最近 %d 个月，共 %d 个月）" % (len(tail), len(rows)))
    for r in tail:
        la, rk = r[pm.BY_LABEL], r[pm.BY_REMARK]
        off_la, off_rk = official_rate(r), official_rate(r, pm.BY_REMARK)
        # ⚠ 一条里**两个口径都给**：官方那个跟财经的成绩对得上（返利按它结算），
        #   旧口径是申诉时看惯的数。只给一个的话，两种人会各问一次。
        if off_la is None:                 # 老 payload ⇒ **别把我们的数标成"官方"**
            out.append("%s%s：按标签 %s · 按备注 %s · 申诉后 %s"
                       % (r["month"], "（暂定）" if r["provisional"] else "",
                          _pct(la["rate"]), _pct(rk["rate"]), _pct(la["ap_rate"])))
            out.append("分母 %s（其中扣掉退货 %s）"
                       % (format(la["den"], ",.2f"), format(la["cut_den"], ",.2f")))
            continue
        out.append("%s%s：⭐官方 %s（标签）/ %s（备注） · 旧口径 %s · 申诉后 %s"
                   % (r["month"], "（暂定）" if r["provisional"] else "",
                      _pct(off_la), _pct(off_rk),
                      _pct(la["rate"]), _pct(la["ap_rate"])))
        out.append("分母 %s（退货扣掉 %s，两个口径都扣；官方按**日均值**算）"
                   % (format(la["den"], ",.2f"), format(la["cut_den"], ",.2f")))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="POS 合规率（两个口径 × 按月）")
    ap.add_argument("--db", default="out/cbg.db")
    ap.add_argument("--month", default="", help="只看这一个月")
    ap.add_argument("--json", action="store_true", help="出 JSON")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        raise SystemExit("没有这个库：%s" % db)
    conn = sqlite3.connect(str(db))
    orders, returns = load(conn)

    out = {}
    for by in pm.BOTH:
        for tag, et in (("现状", False), ("申诉后", True)):
            ms = [m for m in pm.score_all(orders, returns, by, et)
                  if not args.month or m.month == args.month]
            out["%s·%s" % (by, tag)] = [m.to_dict() for m in ms]

    if args.json:
        print(json.dumps({"库": str(db), "订单": len(orders), "退货": len(returns),
                          "口径": out}, ensure_ascii=False, indent=1))
        return 0

    print("库：%s   订单 %d 张 / 退货 %d 张" % (db, len(orders), len(returns)))
    if not args.month and len(pm.months_of(orders, returns)) > 1:
        print("⚠ 逐月看，但**注意看分母** —— 单笔大额现金单能让月度分数摆十几个点")
        print("  （实测：2026-08 那张 39,000 团单，一个人就把当月压掉 10 个点）")
    print()
    for by in pm.BOTH:
        for tag, et in (("现状", False), ("⭐ 申诉后（团单/团单出单也排掉）", True)):
            months = [m for m in pm.score_all(orders, returns, by, et)
                      if not args.month or m.month == args.month]
            lines = pm.render(by, months)
            print(("  " if tag.startswith("⭐") else "") + lines[0] + ("  ｜%s" % tag))
            for line in lines[1:]:
                print(("  " if tag.startswith("⭐") else "") + line)
            t = pm.totals(orders, returns, by, et)
            print("  %-9s %13.2f %13.2f %8s%% %6d %8.2f%s"
                  % ("(合计)", t.den, t.num,
                     "  —  " if t.rate is None else "%6.2f" % t.rate, t.orders, t.cut_den,
                     "   " + tag if tag.startswith("⭐") else ""))
            if t.orphan_returns:
                print("  ⚠ 有 %d 张退货的原单不在库里（跨年？）—— 没扣，自己看"
                      % len(t.orphan_returns))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
