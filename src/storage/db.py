"""连接与事务 —— **唯一**建 sqlite 连接的地方。

⚠ 连接是用 `factory=` 造出来的（见 `_Conn`）：列缓存挂在连接对象上，
所以"新开一个连接要不要清缓存"这个问题**不存在了**（M15 / 阶段 3.4）。
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path


class _Conn(sqlite3.Connection):
    """带一格的连接 —— `_cbg_cols` 就是这张连接的列缓存。

    ⚠ 这就是这一层存在的理由：缓存**跟着连接走**，
    不会出现"上一个连接的列集合被下一个连接读到"（`id()` 复用那个坑）。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._cbg_cols = {}


def connect(path, *, named: bool = False) -> sqlite3.Connection:
    """建一个连接。`named=True` 时行按列名取（`r["sn"]`）。

    ⚠ **`named` 默认 `False`（元组），这个默认值很要紧** ——

    Python 的 `%` 格式化**只对元组展开**：`"%-9s %4d" % row` 在 `row` 是元组时正常，
    换成别的类型就只允许**一个**占位符。而 `dump.main` 末尾那段汇总打印
    （按 POS/设备、按支付方式…）全是这种写法。

    实测踩过：给主连接设了 `Row` 之后，门店跑「整个项目」时数据**已经写进库了**，
    却崩在最后的汇总打印上，退出码 9 ⇒ 整条日常流程中止 ⇒ 报账排查和 POS 都没跑。
    **最坏的一种失败：活儿干完了，但工具说自己失败了。**

    `named=True` 给**读**的人用（`check_freshness` / `reported_sns_from_db` 要按列名取）。
    一个开关两种用法，所以把差别摆在**函数名**上（`open_db`），别靠"记得传参数"。
    """
    conn = sqlite3.connect(str(path), factory=_Conn)
    if named:
        conn.row_factory = sqlite3.Row
    return conn


def read_only(path) -> sqlite3.Connection:
    """只读连接（`mode=ro` 的 URI）—— 判据、看板这类**只该看**的地方用它。

    ⚠ 只读连接开不出 `-wal`/`-shm` 之外的东西，也**写不进去**：
    这是"判据不许改库"那条测试的底气（比"记得别写"可靠）。
    """
    return sqlite3.connect("file:%s?mode=ro" % Path(path), uri=True, factory=_Conn)


def open_db(path) -> sqlite3.Connection:
    """给**读**的人用：行按列名取。见 `connect` 里那段"为什么两种默认值"。"""
    return connect(path, named=True)


@contextlib.contextmanager
def tx(path, *, named: bool = False):
    """开连接 + 事务：正常退出 `commit()`，异常 `rollback()` 后**原样抛出**。

    ⚠ **这一版只把"开连接 + 收尾"收拢，不合并现有的 `commit()` 点** ——
    把"一次采集做成一个事务"是**行为变更**（失败时整批回滚），
    得单独评估（M15 设计文档 D2 的 ②，本版不做）。

    用来 `with db.tx(p) as conn:`，跟普通连接一样用。
    """
    conn = connect(path, named=named)
    try:
        yield conn
        conn.commit()
    except BaseException:
        # ⚠ `BaseException`：`SystemExit` / `KeyboardInterrupt` 也要回滚
        #   （这个项目在"SystemExit 穿过 except Exception"上踩过两次）
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
