"""跨进程运行上下文锁：任务与入口/编码写入互斥；崩溃由 OS 释放。

文件不删除，不根据年龄抢锁。不同 root 独立；线程各持自己的句柄，不继承给子进程。
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

from ...paths import ROOT

_locks = {}
_locks_guard = threading.Lock()


def _file_lock(handle, acquire):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


@contextlib.contextmanager
def guard(root=None):
    """非阻塞取得锁；yield bool。未取得时不修改运行选择，不释放别人的锁。"""
    base = Path(root or ROOT).resolve()
    key = str(base)
    with _locks_guard:
        local = _locks.setdefault(key, threading.Lock())
    acquired = local.acquire(False)
    if not acquired:
        yield False
        return
    handle = None
    locked = False
    try:
        path = base / '.secrets/runtime-context.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), 'a+b')
        os.set_inheritable(handle.fileno(), False)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        try:
            _file_lock(handle, True)
            locked = True
        except (OSError, IOError):
            pass
        yield locked
    finally:
        try:
            if locked:
                _file_lock(handle, False)
        finally:
            if handle is not None:
                handle.close()
            local.release()
