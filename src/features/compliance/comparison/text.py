"""四池的**文案**（推送正文 / 状态表）—— M15 / 阶段 3.5。

⚠ 文案只在这一份里写（邮件和企微共用），跟 `pos_report.notify_lines` 一个道理。
"""

from __future__ import annotations

import sqlite3

from . import category as CAT
from .rules import QUADRANT_LABELS
from .store import POOLS, category_scope, details, snapshots

#: 池的中文名（打印用）
POOL_LABELS = {
    "lg-stock": "池B 玲珑在库",
    "erp-stock": "池D 云商在库",
    "erp-sales": "池C 云商销售单",
}

def notify_lines(conn: sqlite3.Connection, *, limit: int = 20, marks=None) -> list:
    """推送文案的正文行 —— **纯文本**，邮件和企微共用一份。

    两段，各带条数；每条一行，串号 + 机型 + 门店 + 单号/库龄。
    超过 `limit` 台就只列前几台并说明"详见 Excel"（推送不是报表，
    几十台堆在群里没人看；完整清单走附件）。

    `marks` = `pools_notify.annotate()` 的结果。**第一次出现的打 `★`**，
    推过的弱化并标出"已推 N 次、上次几号" —— 批发单那种
    「云商 9-14 报、玲珑要求 9-18 报」会连着推好几天，
    不区分的话门店第二天就不看了，**那几天新冒出来的反而被淹掉**。
    """
    out = []
    for quad in ("AD", "BC"):
        rows = details(conn, quad)
        if not rows:
            continue
        fresh = sum(1 for r in rows if ((marks or {}).get(r["sn"]) or {}).get("new", True))
        head = "【%s】%s —— %d 台" % (quad, QUADRANT_LABELS[quad], len(rows))
        if marks is not None:
            head += "（其中新出现 %d 台）" % fresh
        out.append(head)
        for r in rows[:limit]:
            m = (marks or {}).get(r["sn"]) or {}
            fresh = marks is None or m.get("new", True)
            model = str(r.get("玲珑机型") or r.get("云商机型") or "")[:40]
            store = (r.get("玲珑门店") or r.get("云商门店")
                     or r.get("玲珑仓") or r.get("云商仓") or "")
            doc = r.get("云商单号") or r.get("玲珑单号") or ""
            age = r.get("玲珑库龄") or r.get("云商库龄") or ""

            # ⚠ **`★` 顶到行首**，不是缀在行尾 —— 用户 2026-09-17 实测反馈
            #   「标注不是很醒目」。缀在后面等于没有，扫的时候看不见。
            #   企微那边会把 ★ 开头的整行染成橙红（`wecom.build_pools_markdown`）。
            out.append(("%s %s  %s" % ("★" if fresh else " ", r["sn"], model)).rstrip())
            tail = []
            if store:
                tail.append("门店 %s" % store)
            if age:
                tail.append("挂了 %s 天" % age)
            if marks is not None and not fresh and m.get("first"):
                # 「首次」不是「上次」—— 见 `pools_notify.annotate` 的注释
                tail.append("已推 %d 次，首次 %s" % (m.get("count") or 1, m["first"]))
            if tail:
                out.append("    " + " · ".join(tail))
            if doc:
                out.append("    单号 %s" % doc)
            out.append("")            # 条目之间空一行，不然糊成一坨
        if len(rows) > limit:
            out.append("  …另有 %d 台，见附件 Excel" % (len(rows) - limit))
        out.append("")

    # ⚠ **范围说明**（2026-09-21 起只对账六个大类）—— 排除掉的量必须让人看见，
    #   否则门店看到差异从 18 变 16，只会以为"数据变了"，而不是"口径收窄了"。
    #   本项目最忌讳的就是"少给了还不吭声"（跟 `BC_样机` 单列一项一个道理）。
    _keep, stats = category_scope(conn)
    if stats.get("_skipped"):
        # ⚠ 退化（品类列读不到）**必须说出来** —— 静默不过滤 = 口径悄悄变了
        out.append("【范围】%s" % stats["_skipped"])
    dropped = stats.get("其它", 0) + stats.get("未知", 0) + stats.get("空", 0)
    words = sorted(stats.get("_unknown_words") or {})
    if dropped or words:
        line = "【范围】只对账 %s" % " / ".join(CAT.CATS)
        if dropped:
            line += " —— 另有 %d 台其它品类（配件/全屋智能等）未纳入" % dropped
        if words:
            # ⚠ 词要**点名**，而且**跟台数无关**：这台机器可能恰好在别的池认出来了、
            #   所以不计入「未知台数」—— 但那个词本身仍要有人去补映射表，
            #   等它哪天只出现在那一侧，就会被悄悄排掉。
            line += "；⚠ 有认不出的品类词：%s%s —— 要补 category.py 的映射表" % (
                "、".join(words[:5]), " 等" if len(words) > 5 else "")
        out.append(line)
    return out

def status(conn: sqlite3.Connection) -> list[tuple]:
    """四个池子各多少行 / 最新到哪天。返回 `[(池名, 说明, 行数)]`。"""
    out = []
    for pool, (table, is_snap) in POOLS.items():
        try:
            n = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        except sqlite3.OperationalError:
            out.append((POOL_LABELS[pool], "（表还没建）", 0))
            continue
        if is_snap:
            days = snapshots(conn, pool)
            newest = days[0][0] if days else "—"
            out.append((POOL_LABELS[pool],
                        "快照 %d 天，最新 %s" % (len(days), newest), n))
        else:
            docs = conn.execute(
                "SELECT COUNT(DISTINCT document_no) FROM %s" % table).fetchone()[0]
            out.append((POOL_LABELS[pool], "%d 张单据" % docs, n))
    # 池 A 在 orders / order_lines
    try:
        o = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        l = conn.execute("SELECT COUNT(*) FROM order_lines").fetchone()[0]
        out.append(("池A 玲珑销售单", "%d 张单据 / %d 明细行" % (o, l), o))
    except sqlite3.OperationalError:
        out.append(("池A 玲珑销售单", "（表还没建）", 0))
    return out
