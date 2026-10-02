# -*- coding: utf-8 -*-
"""生活馆包里 `erp.py` **不存在**时的替身。

⚠ 不是第二份云商实现 —— 是"这里没有云商"的**哨兵**：
能安全返回空的（describe/load 类）返回空，要真干活的（登录/取数）用到即抛，
抛的信息直说"生活馆版没有云商"，别让人以为是网络问题。

只有 `edition.is_lifehall()` 的 import 分支会碰这个文件；
主包路径永远走真的 `erp.py`（它有内置公司账号凭据，**绝不能进生活馆包**）。
"""

from __future__ import annotations


class ErpError(RuntimeError):
    pass


class ErpCaptchaRequired(ErpError):
    pass


class ErpClient:
    def __init__(self, *a, **k):
        raise ErpError("生活馆版没有云商功能")


#: 路径常量 —— 只是拼路径用，文件不存在即"未配置"。
DEFAULT_ENV_FILE = ".secrets/erp.env"
STORE_ENV_FILE = ".secrets/erp-store.env"


def _no(name):
    def _f(*a, **k):
        raise ErpError("生活馆版没有云商功能（%s）" % name)
    _f.__name__ = name
    return _f


describe_credentials = lambda *a, **k: {}      # noqa: E731 —— 描述类给空最安全
describe_store_credentials = lambda *a, **k: {}  # noqa: E731
load_credentials = _no("load_credentials")
load_store_credentials = _no("load_store_credentials")
save_credentials = _no("save_credentials")
save_store_credentials = _no("save_store_credentials")
effective_env_file = _no("effective_env_file")
