"""输出：差异 Excel + 控制台摘要。

原则：**无差异也要落盘**。否则「今天没报表」和「脚本没跑」分不清 ——
这是这类自动对账最常见的失效方式。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from .reconcile import ReconcileResult

# 业务在国内，报告一律按东八区 —— 跟 ctx 里的「生成时间」保持一致
CST = datetime.timezone(datetime.timedelta(hours=8))
from .xlsx_io import write_sheets

MISSING_HEADER = ["串号", "商品名称", "云商单号", "单据类型", "卖出店", "支付时间", "金额", "串号标识", "店员"]
# 反向差异 = 华为报了量、云商"该本店报量的销售"里没有的。
# 主要用途：**门店核对调拨过来的货有没有出库** —— 所以必须带上云商侧那一行。
REVERSE_HEADER = ["类型", "串号", "商品名称", "华为单号", "报量时间",
                  "云商出库", "串号标识", "云商卖出店", "云商单号", "云商金额", "云商支付时间"]
MATCHED_HEADER = ["串号", "商品名称", "云商单号", "卖出店", "支付时间", "华为单号", "华为建单时间"]


def build_sheets(res: ReconcileResult, ctx: dict) -> dict:
    missing_rows = [[s.sn, s.item, s.doc_no, s.doc_type, s.seller, s.pay_time,
                     s.amount, s.marker, s.clerk] for s in res.missing]

    # 一条反向差异可能对上多行云商记录（比如先退货又重卖），逐行展开
    reverse_rows = []
    for it in res.reverse:
        base = [it.kind, it.sn, it.info.get("item", ""), it.info.get("documentNo", ""),
                it.info.get("time", "")]
        if not it.yun:
            reverse_rows.append(base + ["⚠️ 查无出库记录", "", "", "", "", ""])
        else:
            for y in it.yun:
                reverse_rows.append(base + ["✅ 已出库", y.marker, y.seller,
                                            y.doc_no, y.amount, y.pay_time])

    matched_rows = [[s.sn, s.item, s.doc_no, s.seller, s.pay_time,
                     i.get("documentNo", ""), i.get("time", "")] for s, i in res.matched]

    skipped_rows = [[k, v] for k, v in sorted(res.skipped.items(), key=lambda x: -x[1])]
    info_rows = [[k, v] for k, v in ctx.items()]

    return {
        # ⚠ 口径名用门店自己的话：「玲珑」= 华为那个销售系统的代号。
        #   改名前叫「未报量」，但那个词容易被读成"该报没报"（带责备意味），
        #   而实际含义只是"两边记录对不上" —— 所以两个方向改成对称的说法。
        "玲珑无但云商有": (MISSING_HEADER, missing_rows),
        "反向差异": (REVERSE_HEADER, reverse_rows),
        "已报量": (MATCHED_HEADER, matched_rows),
        "过滤统计": (["跳过原因", "条数"], skipped_rows),
        "运行信息": (["项", "值"], info_rows),
    }


def summary_dict(res: ReconcileResult, ctx: dict) -> dict:
    """给前端用的摘要。写报告时落一份旁车 json，列历史时就不用去解析 xlsx 了。"""
    return {
        "store": ctx.get("门店", ""),
        "store_code": ctx.get("华为门店编码", ""),
        "marker": ctx.get("串号标识", ""),
        "date": ctx.get("目标日", ""),
        "generated_at": ctx.get("生成时间", ""),
        "sales_range": ctx.get("销售区间", ""),
        "cbg_range": ctx.get("华为区间", ""),
        "total_rows": res.total_rows,
        "missing": len(res.missing),
        "matched": len(res.matched),
        "reverse": len(res.reverse),
        "reverse_transfer": len(res.reverse_transfer),
        "reverse_unknown": len(res.reverse_unknown),
        "reverse_unshipped": len(res.reverse_unshipped),
        "skipped": res.skipped,
        "missing_sns": [s.sn for s in res.missing],
    }


def report_path(out_dir, date_tag: str, store: str, when=None) -> Path:
    """报告存哪。

    **文件名带生成时刻，永不覆盖** —— 同一个目标日可能跑很多次
    （定时任务跑一次、发现不对手动再跑一次、补跑…），
    只按目标日命名会把前一次的盖掉，而"上一次跑出来是什么样"往往正是要对比的。

    万一同一秒内跑两次（脚本连点），再退化成 `-2`、`-3` 后缀，仍然不覆盖。
    """
    when = when or datetime.datetime.now(CST)
    base = Path(out_dir) / f"差异_{date_tag}_{store or 'unknown'}_{when:%Y%m%d-%H%M%S}"
    path = base.with_suffix(".xlsx")
    n = 2
    while path.exists():
        path = base.with_name(base.name + f"-{n}").with_suffix(".xlsx")
        n += 1
    return path


def write_report(out_dir, res: ReconcileResult, ctx: dict, date_tag: str, store: str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = report_path(out_dir, date_tag, store)
    write_sheets(path, build_sheets(res, ctx))
    path.with_suffix(".json").write_text(
        json.dumps(summary_dict(res, ctx), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def delete_report(out_dir, name: str) -> tuple[bool, str]:
    """删一份报告 —— xlsx 和它的摘要 json 一起删。

    ⚠ 只允许删 out/ 目录下的文件（防目录穿越）。
    """
    out_dir = Path(out_dir).resolve()
    target = (out_dir / name).resolve()
    if not str(target).startswith(str(out_dir)) or target.parent != out_dir:
        return False, "路径不合法"
    if target.suffix.lower() != ".xlsx":
        return False, "只能删 xlsx"
    if not target.is_file():
        return False, "文件不存在"

    removed = [target.name]
    try:
        target.unlink()
    except OSError as e:
        return False, f"删不掉：{e}"
    side = target.with_suffix(".json")
    if side.exists():
        try:
            side.unlink()
            removed.append(side.name)
        except OSError:
            pass                                   # 摘要删不掉不影响主文件已经删了
    return True, "已删除 " + " 和 ".join(removed)


def list_reports(out_dir) -> list[dict]:
    """列出历史报告（新→旧）。摘要优先读旁车 json，没有就现算必填的几项。"""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    items = []
    for x in out_dir.glob("差异_*.xlsx"):
        side = x.with_suffix(".json")
        if side.exists():
            try:
                s = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                s = {}
        else:
            s = {}
        if not s:
            try:
                from .xlsx_io import read_rows
                s = {"missing": max(len(read_rows(x)) - 1, 0), "matched": None,
                     "reverse": None, "date": "", "store": "", "_derived": True}
            except Exception:                        # noqa: BLE001
                s = {"missing": None}
        st = x.stat()
        items.append({
            "name": x.name, "size": st.st_size, "mtime": st.st_mtime,
            "has_json": side.exists(), **s,
        })
    items.sort(key=lambda d: d["mtime"], reverse=True)
    return items


def load_report(path) -> dict:
    """读回一份报告：{summary, sheets}。summary 优先读旁车 json，旧的报告则现算。"""
    from .xlsx_io import read_sheets

    path = Path(path)
    sheets = read_sheets(path)
    side = path.with_suffix(".json")
    if side.exists():
        summary = json.loads(side.read_text(encoding="utf-8"))
    else:
        summary = {"store": "", "date": "", "generated_at": "",
                   "missing": max(len(sheets.get("玲珑无但云商有", [])) - 1, 0),
                   "matched": max(len(sheets.get("已报量", [])) - 1, 0),
                   "reverse": max(len(sheets.get("反向差异", [])) - 1, 0),
                   "reverse_transfer": None, "reverse_unknown": None,
                   "reverse_unshipped": None,
                   "total_rows": None, "skipped": {}, "_derived": True}
    return {"summary": summary, "sheets": sheets}


def summary_lines(res: ReconcileResult, ctx: dict) -> list[str]:
    lines = [
        f"门店 {ctx.get('门店', '?')}（华为 {ctx.get('华为门店编码') or '会话默认'} / 标识 {ctx.get('串号标识', '?')}）",
        f"核对区间 {ctx.get('销售区间', '?')}   华为区间 {ctx.get('华为区间', '?')}",
        f"云商原始 {res.total_rows} 行 → 该本店报量 {len(res.matched) + len(res.missing)} 台",
        f"  ✅ 已报量 {len(res.matched)} 台",
        f"  ❌ 玲珑无但云商有 {len(res.missing)} 台",
        f"  🔁 反向差异 {len(res.reverse)} 台（华为已报量、但不是本店标识的货）",
    ]
    if res.reverse:
        lines.append(f"       └ 调拨进来 {len(res.reverse_transfer)} · 来源不明 {len(res.reverse_unknown)}"
                     f" · 其中 {len(res.reverse_unshipped)} 台云商查不到出库")
    if res.missing:
        lines.append("")
        lines.append("玲珑无但云商有清单：")
        for s in res.missing:
            lines.append(f"  {s.sn:<18} {s.item[:30]:<32} {s.seller} {s.pay_time} ¥{s.amount} ({s.doc_no})")
    else:
        lines.append("")
        lines.append("✅ 全部已报量，没有差异。")
    if res.reverse_unshipped:
        lines.append("")
        lines.append("⚠️ 玲珑有但云商无（云商查不到出库，要问一句：货呢？）：")
        for it in res.reverse_unshipped:
            lines.append(f"  {it.sn:<18} {str(it.info.get('item', ''))[:30]:<32} "
                         f"华为单 {it.info.get('documentNo', '')}")
    if res.skipped:
        lines.append("")
        lines.append("过滤统计：" + "，".join(f"{k} {v}" for k, v in
                                          sorted(res.skipped.items(), key=lambda x: -x[1])))
    return lines
