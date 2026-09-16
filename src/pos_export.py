"""导出某个月的 POS 分子/分母明细 → Excel。

口径照 `pos_metric.py`（纯函数），这里只负责取数和排版。

用法：
    python pos_export.py --db out/cbg-2026.db --month 2026-08 --out 明细.xlsx
    python pos_export.py --db out/cbg-2026.db --month 2026-08 --by remark

出三个 sheet：
    汇总       —— 分母/分子/分数，以及**每一步的算式**（人能对着核）
    分子明细   —— 进了分子的那些单（**这就是用户要的"8 月分子明细"**）
    分母全量   —— 全部进分母的单，带一列标出「进分子 / 未进分子」
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import pos_metric as pm

from openpyxl import Workbook                       # noqa: E402
from openpyxl.styles import Font, PatternFill       # noqa: E402


def fetch(conn, month, by):
    """把某月的订单整理成「一行一单」，并标出进没进分子。"""
    conn.row_factory = sqlite3.Row
    rows = []
    for o in conn.execute("SELECT * FROM orders WHERE substr(doc_create_time,1,7)=?", (month,)):
        pays = list(conn.execute(
            "SELECT media_name, payment_amount FROM payments WHERE document_no=?",
            (o["document_no"],)))
        noncash = sum(p["payment_amount"] or 0.0 for p in pays if pm.is_noncash(p["media_name"]))
        items = [x[0] for x in conn.execute(
            "SELECT item_name FROM order_lines WHERE document_no=? ORDER BY line_no",
            (o["document_no"],))]
        order = pm.Order(
            document_no=o["document_no"], month=month,
            amount=o["included_tax_amount"] or 0.0, noncash=noncash,
            is_gb_label=("国补" in (o["order_label_list"] or "").split("|")),
            is_gb_remark=((o["remark"] or "") == "国补"),
            is_instant_retail=(o["business_type"] == pm.INSTANT_RETAIL_BUSINESS_TYPE),
            is_care=(o["scenario_type"] == pm.CARE_SCENARIO_TYPE))
        if not order.eligible(by):
            continue
        rows.append({
            "下单时间": o["doc_create_time"] or "",
            "订单号": o["document_no"],
            "订单金额": round(order.amount, 2),
            "非现金金额": round(noncash, 2),
            "支付方式": " + ".join("%s %.2f" % (p["media_name"], p["payment_amount"] or 0)
                                for p in pays) or "（无支付记录）",
            "收银终端": o["pos_id"] or "", "设备号": o["device_no"] or "",
            "收银员": o["cashier_id"] or "", "导购": o["sales_assistant_id"] or "",
            "备注": o["remark"] or "", "标签": o["order_label_list"] or "",
            "团单": "⭐" if pm.is_team(o["remark"]) else "",
            "商品": " / ".join(items)[:120],
        })
    rows.sort(key=lambda r: r["下单时间"])
    return rows


def sheet(wb, title, headers, rows, money_cols=(), bold_last=False, widths=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="EEEEEE")
    for r in rows:
        ws.append([r.get(h, "") for h in headers])
    for i, h in enumerate(headers, 1):
        w = (widths or {}).get(h) or (14 if h in money_cols else 20)
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        if h in money_cols:
            for cell in ws[ws.cell(1, i).column_letter][1:]:
                cell.number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    return ws


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出某月 POS 分子/分母明细")
    ap.add_argument("--db", required=True)
    ap.add_argument("--month", required=True, help="2026-08")
    ap.add_argument("--out", default="")
    ap.add_argument("--by", default=pm.BY_LABEL, choices=list(pm.BOTH))
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)

    # ⚠ **不在这儿重算口径** —— 直接复用 `pos_report.load` + `pos_metric`。
    #   上一版就是在这儿自己写了一遍"退款方式不是现金就扣分子"，
    #   那是**近似**（真规则要按**原单**算，见 `Returned.deduction`），
    #   而且汇总那行 `⑥ 分子 = ④ − ⑤` 压根忘了减。**规则只能有一份。**
    from . import pos_report
    all_orders, all_returns = pos_report.load(conn)
    mr = pm.score_month(all_orders, all_returns, args.month, by=args.by)
    # ⭐ 申诉后口径：把团单/团单出单也排掉（用户 2026-09-16 要两个数并排看）
    mr_a = pm.score_month(all_orders, all_returns, args.month, by=args.by, exclude_team=True)

    rows = fetch(conn, args.month, args.by)
    rets = []
    for r in all_returns:
        if r.month != args.month:
            continue
        cd, cn = r.deduction(args.by)
        rets.append({"时间": "", "退单号": r.orig.document_no if r.orig else "（原单查不到）",
                     "原单号": r.orig.document_no if r.orig else "",
                     "退款金额": round(r.amount, 2),
                     "非现金退款": round(r.noncash_refund, 2),
                     "扣分母": round(cd, 2), "扣分子": round(cn, 2),
                     "退款方式异常": "⚠" if r.odd_refund() else ""})

    out = Path(args.out or ("pos-%s-%s.xlsx" % (args.month, args.by)))
    wb = Workbook()
    wb.remove(wb.active)

    # ⚠ 汇总表要**把排除项逐条写出来** —— 用户问过「分母减去即时零售和国补了吗」，
    #   说明光给一个分母数字不够，得让人看见减了什么。
    #   用**过滤**（eligible）而不是逐个相减：重叠的单只会被排除一次。
    exc = {"国补": [0, 0.0], "即时零售": [0, 0.0], "Care+": [0, 0.0], "全部": [0, 0.0]}
    for o in all_orders:
        if o.month != args.month:
            continue
        exc["全部"][0] += 1; exc["全部"][1] += o.amount
        if o.is_instant_retail:
            exc["即时零售"][0] += 1; exc["即时零售"][1] += o.amount
        elif o.is_care:
            exc["Care+"][0] += 1; exc["Care+"][1] += o.amount
        elif (o.is_gb_label if args.by == pm.BY_LABEL else o.is_gb_remark):
            exc["国补"][0] += 1; exc["国补"][1] += o.amount

    summary = [
        {"项": "月份", "值": args.month},
        {"项": "口径", "值": "按标签 orderLabelList" if args.by == pm.BY_LABEL else "按备注 remark"},
        {"项": "全月总单数", "值": exc["全部"][0]},
        {"项": "全月总金额", "值": round(exc["全部"][1], 2)},
        {"项": "− 国补（%s）" % ("标签" if args.by == pm.BY_LABEL else "备注"),
         "值": "%d 单 / %s" % (exc["国补"][0], format(round(exc["国补"][1], 2), ",.2f"))},
        {"项": "− 即时零售（business_type=10）",
         "值": "%d 单 / %s" % (exc["即时零售"][0], format(round(exc["即时零售"][1], 2), ",.2f"))},
        {"项": "− Care+（scenario_type=10）",
         "值": "%d 单 / %s" % (exc["Care+"][0], format(round(exc["Care+"][1], 2), ",.2f"))},
        {"项": "进分母的单数", "值": mr.orders},
        {"项": "① 分母（未扣退货）", "值": round(mr.den + mr.cut_den, 2)},
        {"项": "② 本月退货从分母扣", "值": round(mr.cut_den, 2)},
        {"项": "③ 分母 = ① − ②", "值": round(mr.den, 2)},
        {"项": "④ 分子（未扣退货）", "值": round(mr.num + mr.cut_num, 2)},
        {"项": "⑤ 本月退货从分子扣", "值": round(mr.cut_num, 2)},
        {"项": "⑥ 分子 = ④ − ⑤", "值": round(mr.num, 2)},
        {"项": "分数 = ⑥ / ③（**现状**）",
         "值": ("%.2f%%" % mr.rate) if mr.rate is not None else "—（分母为 0）"},
        {"项": "── 申诉后口径 ──", "值": "团单/团单出单也排掉"},
        {"项": "申诉后 · 分母", "值": round(mr_a.den, 2)},
        {"项": "申诉后 · 分子", "值": round(mr_a.num, 2)},
        {"项": "申诉后 · 分数", "值": ("%.2f%%" % mr_a.rate) if mr_a.rate is not None else "—（分母为 0）"},
        {"项": "本月退货单数", "值": mr.returned},
        {"项": "原单查不到的退货", "值": len(mr.orphan_returns)},
    ]
    sheet(wb, "汇总", ["项", "值"], summary, money_cols=("值",), widths={"项": 26, "值": 40})

    nc = [r for r in rows if r["非现金金额"]]
    hdr = ["下单时间", "订单号", "订单金额", "非现金金额", "支付方式",
           "收银终端", "设备号", "收银员", "导购", "备注", "标签", "团单", "商品"]
    sheet(wb, "分子明细", hdr, nc, money_cols=("订单金额", "非现金金额"),
          widths={"订单号": 30, "商品": 60, "支付方式": 30, "下单时间": 20})
    sheet(wb, "分母全量", hdr, rows, money_cols=("订单金额", "非现金金额"),
          widths={"订单号": 30, "商品": 60, "支付方式": 30, "下单时间": 20})
    if rets:
        sheet(wb, "本月退货",
              ["退单号", "原单号", "退款金额", "非现金退款", "扣分母", "扣分子", "退款方式异常"],
              rets, money_cols=("退款金额", "非现金退款", "扣分母", "扣分子"),
              widths={"退单号": 30, "原单号": 30})

    wb.save(out)
    print("已写出：%s" % out)
    print("  %s / %s：进分母 %d 单，分母 %.2f，分子 %.2f，分子明细 %d 单，分数 %s"
          % (args.db, args.month, mr.orders, mr.den, mr.num, len(nc),
             ("%.2f%%" % mr.rate) if mr.rate is not None else "—"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
