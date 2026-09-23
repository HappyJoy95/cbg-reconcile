"""**系统健康状态** —— 汇总证据、判"能不能启动"、决定"能不能自动更新"。

用户 2026-09-19：「加一个系统健康状态模块，**保持自动更新**和记录日志；
每个功能模块也要给它输出日志。**系统启动的时候也是先运行健康模块**。」

## 两个半边，别合并

| | 在哪 | 为什么 |
|---|---|---|
| **写**（上报） | `storage/runlog.py` | **最底层**：所有层都能调它，它自己不 import 任何业务 |
| **读/汇总** | 这里（`modules/health/checks.py`） | 对各个来源**懒加载**（`app.data_state` / `storage.migrate` / `selfupdate` …）—— 谁都不会反过来依赖它 |

⇒ 这样"每个功能都往健康写日志"**不会绕出环**。

⚠ 两个踩过的坑：
1. **别让子模块名跟转发的函数名撞车** —— 这里原来叫 `summary.py`，而下面又转发了
   runlog 的 `summary()` ⇒ 包属性被函数盖掉，`from ... import summary` 拿到的是函数；
   改成 `checks.py` 就没这回事。
2. **测试打桩要打在函数真正的家**（`modules.health.checks.X`）：
   包 `__init__` 里那些名字是**副本**，patch 它们不影响 `checks.snapshot()` 内部的查找。
3. ⚠ **私有名（`_item` 这种）没有转发** —— 测试里写 `health._item` 会 AttributeError，
   而它会被 `checks._safe()` 兜成一条"某某自己出错了"的警告 ⇒
   **真正的错误被兜底藏起来了**（本轮就是这么排查了一轮）。要读私有件就 import `checks`。
"""

from __future__ import annotations

from .checks import (BLOCKING, GROUP_LABELS, TODO, WARNING,  # noqa: F401
                      boot, boot_lines, check_auth, check_code, check_data,
                      check_notify, check_registry, check_runs,
                      auto_update, check_schema, check_theme, check_timer,
                      snapshot, update_plan)

#: 写那半（`run_record`）—— 功能/能力都用它上报，**一行**：
#:     res = health.begin("attain", note="周度达成"); ...; res.done(ok=True, rows=n)
from ...storage.runlog import Run, begin, prune, recent, record, summary  # noqa: F401

__all__ = [
    "snapshot", "boot", "boot_lines", "update_plan", "GROUP_LABELS", "BLOCKING",
    "WARNING", "TODO",
    "check_code", "check_schema", "check_registry", "check_data", "check_timer",
    "check_notify",
    "check_theme", "check_auth", "check_runs",
    "begin", "record", "recent", "summary", "prune", "Run",
]
