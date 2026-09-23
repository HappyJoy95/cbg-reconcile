"""四池的**导出**（Excel）—— M15 / 阶段 3.5。

⚠ 明细行的形状由 `store.details()` 说了算，这里只管排版。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .rules import DETAIL_COLS, QUADRANT_LABELS
# ⚠ `details` 一度**漏了 import**（只导了 `PoolError` / `_table_cols`）——
#   单测里没走到这条真路（导出要真写 xlsx），于是它一直躺到 2026-09-20：
#   那天跑 `pools --steps` 才炸出 `NameError: name 'details' is not defined`。
#   ⇒ 导 Excel 的入口现在有一条**真跑一遍**的测试（`test_pools.py`）。
from .store import PoolError, _table_cols, details

def export_xlsx(conn: sqlite3.Connection, path) -> tuple:
    """把 AD/BC 明细写成 Excel（两个 sheet）。返回 `(路径, {象限: 条数})`。

    ⚠ 表名有 **31 字符上限**（Excel 的硬限制），所以只取 `QUADRANT_LABELS` 的前半截。
    """
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    counts = {}
    for quad in ("AD", "BC"):
        rows = details(conn, quad)
        counts[quad] = len(rows)
        ws = wb.create_sheet("%s %s" % (quad, QUADRANT_LABELS[quad])[:31])
        ws.append([label for _, label in DETAIL_COLS])
        for r in rows:
            ws.append([r.get(k, "") if r.get(k) is not None else "" for k, _ in DETAIL_COLS])
        ws.freeze_panes = "A2"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    return p, counts
