"""收银库持久绑定单店；旧无归属流水不按当前编码补身份。"""
from __future__ import annotations

import sqlite3
from ...modules.auth import runtime


def check(root, store_code=None, write=False, conn=None):
    """业务连接存在时复用其写事务；不提交、不另开连接。"""
    if conn is None:
        from . import store
        with sqlite3.connect(str(store.ensure(root)), timeout=20) as owned:
            owned.execute('BEGIN IMMEDIATE')
            return check(root, store_code, write, conn=owned)
    if write and not conn.in_transaction:
        raise ValueError('收银写入归属检查必须在业务写事务内')
    code = str(store_code or '').strip()
    lifehall = runtime.is_lifehall(root)
    has_owner = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cashier_owner'").fetchone()
    owner = conn.execute('SELECT store_code FROM cashier_owner WHERE id=1').fetchone() if has_owner else None
    if owner:
        if not code or code != owner[0]:
            raise ValueError('收银数据归属门店编码与当前操作不一致；保留原数据，请先核对归属')
        return
    # 完整版的旧低层调用暂时兼容；所有 App/HTTP 入口都会显式传当前门店编码。
    # 一旦提供编码，就必须和生活馆一样执行持久归属检查，不能让 ERP 入口在换店后
    # 读到上一家店留下的收银流水。已有绑定时，缺编码也不能借兼容路径绕过。
    if store_code is None and not lifehall:
        return
    if not code:
        raise ValueError('收银操作缺少明确本店门店编码')
    if conn.execute('SELECT 1 FROM sale_entries LIMIT 1').fetchone():
        raise ValueError('已有收银流水没有可信门店归属；保留原数据，不能按当前店码补身份')
    if write:
        conn.execute('CREATE TABLE IF NOT EXISTS cashier_owner(id INTEGER PRIMARY KEY CHECK(id=1), store_code TEXT NOT NULL)')
        conn.execute('INSERT INTO cashier_owner(id,store_code) VALUES(1,?)', (code,))


def failure(root, store_code=None, write=False, conn=None):
    try:
        check(root, store_code, write, conn=conn)
    except ValueError as error:
        return {'ok': False, 'forbidden': True, 'why': str(error)}
    except (sqlite3.Error, OSError) as error:
        return {'ok': False, 'why': str(error)}
    return None
