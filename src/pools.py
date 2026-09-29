"""四个数据池 —— **薄壳**（实现在 `src/features/compliance/comparison/`，见那儿的说明）。

2026-09-19（M15 / 阶段 3.5）按"规则 / 读库 / 导出 / 文案"拆开了：

| 原来在这儿 | 现在在 |
|---|---|
| 口径与集合运算（`quadrants` 后半段、`is_sample_marker`、标签…） | `features/compliance/comparison/rules.py`（**纯函数**） |
| 建表 / 写 / 读（`SCHEMA` / `save_*` / `_latest_sn_set` / `details`…） | `features/compliance/comparison/store.py` |
| `export_xlsx` | `features/compliance/comparison/export.py` |
| `notify_lines` / `status` / `POOL_LABELS` | `features/compliance/comparison/text.py` |

⚠ **这个文件留着不删**：`cli.py`（`P.xxx` 二十多处）、`pools_notify`、`pools_history`
和一大批测试都 import 它。跟 `pos_*` 那次一样 —— 先搬实现、引用不动，
路径的去留等引用都换完之后再议（**别一边搬一边改调用方**）。

⚠ 池的定义、口径、以及"为什么同库 / 为什么列存全 / 快照为什么留 30 天"
那三段背景**都跟着代码走了** —— 看 `features/compliance/comparison/__init__.py` 与 `store.py` 的模块头。
壳里只留"去哪儿找"。
"""

from __future__ import annotations

from .features.compliance.comparison.export import export_xlsx                    # noqa: F401
from .features.compliance.comparison.rules import (CLOSED_STATUS, DETAIL_COLS,     # noqa: F401
                                        QUADRANT_LABELS, RETURN_BILL_TYPES,
                                        SAMPLE_MARK, combine, is_sample_marker)
from .features.compliance.comparison.store import (POOLS, POOL_RENAME, SCHEMA,     # noqa: F401
                                        SNAP_KEEP_DAYS, PoolError, _latest_sn_set,
                                        _reported_sns, _sold_sns, _table_cols,
                                        details, ensure, latest, pool_colname,
                                        pool_row_from, purge_snapshots,
                                        quadrants, replace_sales, save_sales,
                                        save_snapshot,
                                        snapshots, today)
from .features.compliance.comparison.text import (POOL_LABELS, notify_lines,       # noqa: F401
                                       status)

__all__ = [
    "POOLS", "POOL_LABELS", "POOL_RENAME", "SCHEMA", "SNAP_KEEP_DAYS", "PoolError",
    "pool_colname", "pool_row_from", "ensure", "today", "save_snapshot", "save_sales",
    "replace_sales",
    "purge_snapshots", "snapshots", "latest", "quadrants", "details",
    "is_sample_marker", "combine", "SAMPLE_MARK", "RETURN_BILL_TYPES", "CLOSED_STATUS",
    "QUADRANT_LABELS", "DETAIL_COLS", "export_xlsx", "notify_lines", "status",
]
