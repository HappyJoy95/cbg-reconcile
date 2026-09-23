"""建表、**动态加列**、列缓存 —— 都在这一层里（M15 / 阶段 3.4）。

## 动态加列

接口加一个字段，**不用回来改代码**：下次抓取自己就把列建出来
（`ALTER TABLE ADD COLUMN` 不影响老数据）。这条是 2026-09-16 用户定的
"全部保留，为了以后扩展用"落到代码上的样子 —— **接口多给什么，库里就多一列**。

## 列缓存：为什么"不用清"了

老实现把缓存放在模块级的 `_COLS_CACHE`，键是 `(id(conn), 表名)` ——
因为 `sqlite3.Connection` **既不支持弱引用、也不能挂属性**。
而 `id()` 在连接释放后会被**复用**：于是"缓存里说这张表有这个列、新库其实没有"
⇒ `ALTER` 被跳过 ⇒ 写入直接
`OperationalError: table order_lines has no column named service_goods`。
（实测复现过：同一个进程开两个内存库，第二个必炸。Web 是长驻进程，
跨年那天要建 `cbg-2027.db`，而缓存里还留着 2026 那个库的列 —— 一年只错一次。）

当时的规矩是"**谁自己开连接谁记得清**"（`pools.ensure()` 里那句手写的
`clear_col_cache()` 就是它，注释写着"测试当场抓到的"）。

现在：缓存挂在**连接对象自己身上**（`db._Conn._cbg_cols`，连接由 `db.connect` 用
`factory=` 造出来）。连接的缓存跟着连接走 ⇒ **不存在"别人的缓存"**，
于是"清理"这个动作从 API 里消失了 —— 不是"记得清"，是**没有可清的东西**。

⚠ 不是我们造的连接（裸 `sqlite3.connect`，比如某些测试）**不做缓存**，
每次都真读一次 `PRAGMA table_info`。慢一点，但**绝不会读错别人的列**。
"""

from __future__ import annotations

import sqlite3

#: 列名里的非法/别扭字符（驼峰转下划线用，见 `dump.colname`）
import re

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def colname(field: str) -> str:
    """接口字段名 → 列名：驼峰转下划线、全小写。

    ⚠ **故意不写字段映射表。** 手写映射一定会漏，而且**接口加一个字段就得回来改**。
    """
    return _CAMEL.sub("_", field).lower()


def cache_of(conn):
    """这条连接的列缓存；**不是我们造的连接就返回 `None`**（那就每次真读）。"""
    cols = getattr(conn, "_cbg_cols", None)
    return cols if isinstance(cols, dict) else None


def table_cols(conn, table: str) -> set:
    """这张表现在有哪些列。"""
    cache = cache_of(conn)
    if cache is not None and table in cache:
        return cache[table]
    have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if cache is not None:
        cache[table] = have
    return have


def forget(conn, table: str = "") -> None:
    """把某张表（或全部）的列缓存丢掉 —— 加完列之后调。"""
    cache = cache_of(conn)
    if cache is None:
        return
    if table:
        cache.pop(table, None)
    else:
        cache.clear()


def ensure_columns(conn, table: str, row: dict) -> None:
    """表里没有的列，**当场补上**。

    ⚠ 列名**必须加引号**：云商导出的表头里有 `69码` 这种数字开头的，
    裸写就是 `unrecognized token`。加引号对已有的英文列名零影响。
    """
    have = table_cols(conn, table)
    added = False
    for name, val in row.items():
        if name in have:
            continue
        typ = ("INTEGER" if isinstance(val, int) and not isinstance(val, bool)
               else "REAL" if isinstance(val, float) else "TEXT")
        conn.execute('ALTER TABLE %s ADD COLUMN "%s" %s' % (table, name, typ))
        have.add(name)
        added = True
    if added:
        forget(conn, table)          # 下次重读一次（`have` 已经是新的了，稳妥起见）
    # ⚠ 不 commit —— 事务归调用方（`db.tx`）管
