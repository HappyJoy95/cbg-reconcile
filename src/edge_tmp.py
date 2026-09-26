"""临时 Edge profile 目录：创建即注册退出清理。

cbg-reconcile 的 .dsh/tasks 下有 55 个 headless Edge 脚本，各自
``tempfile.mkdtemp(prefix="dsh-...")`` 建一个完整 Edge user-data-dir
（约 339MB：ProvenanceData 视觉模型 169MB + 组件 crx 缓存 155MB），
跑完只 terminate 进程、从不删目录，两天堆了 29G。

``edge_tmp()`` 与 ``tempfile.mkdtemp`` 签名行为一致，但进程退出时自动
rmtree。POSIX 下删除不依赖 Edge 进程先退（unlink 对已打开文件同样生效），
ignore_errors 保证清理失败不遮蔽脚本自身退出码。
"""

import atexit
import shutil
import tempfile


def _cleanup(path):
    shutil.rmtree(path, ignore_errors=True)


def edge_tmp(prefix="dsh-edge-"):
    """mkdtemp 的替代：返回目录路径，进程退出时整棵删除。"""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(_cleanup, path)
    return path
