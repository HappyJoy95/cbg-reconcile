"""POS 合规率 —— 从库里取数、算、打印。

**IO 只在这个文件里**；口径全在 `pos_metric.py`（纯函数、可单测）。

用法：
    python pos_report.py --db out/cbg-2026.db
    python pos_report.py --db out/cbg-2026.db --month 2026-09    # 只看一个月
    python pos_report.py --db out/cbg-2026.db --json             # 出 JSON，给别的程序吃
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
        pays = list(conn.execute(
            "SELECT media_name, payment_amount FROM payments WHERE document_no=?",
            (o["document_no"],)))
        orders.append(pm.Order(
            document_no=o["document_no"],
            month=(o["doc_create_time"] or "")[:7],
            amount=o["included_tax_amount"] or 0.0,
            noncash=sum(p["payment_amount"] or 0.0 for p in pays if pm.is_noncash(p["media_name"])),
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
            amount=r["included_tax_amount"] or 0.0,
            noncash_refund=sum(x["refund_amount"] or 0.0 for x in refunds if pm.is_noncash(x["media_name"])),
            orig=by_no.get(r["related_doc_no"])))
    return orders, returns


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
