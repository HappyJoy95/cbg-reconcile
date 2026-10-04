"""本机 HTTP 控制台的兼容命名空间。

旧调用方通过 ``src.web`` 访问，当前实现放在 ``src.http.app``。这个包公开同一组
对象，并把模块级替身/补丁同步给实现模块，保留原有测试与扩展点。
"""
import sys
from types import ModuleType

from . import app as _implementation

# 包的公开命名空间沿用旧 web.py 的符号，包括内部兼容测试会访问的名称。
globals().update({name: value for name, value in vars(_implementation).items()
                  if not (name.startswith("__") and name.endswith("__"))})


class _HTTPNamespace(ModuleType):
    """让旧 ``src.web`` 上的属性替身仍被 ``src.http.app`` 内的代码读取。"""

    _PACKAGE_METADATA = frozenset(("__name__", "__package__", "__spec__",
                                   "__loader__", "__path__", "__file__",
                                   "__cached__", "__class__"))

    def __getattribute__(self, name):
        if name not in _HTTPNamespace._PACKAGE_METADATA:
            namespace = ModuleType.__getattribute__(self, "__dict__")
            implementation = namespace.get("_implementation")
            if implementation is not None and hasattr(implementation, name):
                return getattr(implementation, name)
        return ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        ModuleType.__setattr__(self, name, value)
        if name not in self._PACKAGE_METADATA:
            namespace = ModuleType.__getattribute__(self, "__dict__")
            implementation = namespace.get("_implementation")
            if implementation is not None:
                setattr(implementation, name, value)

    def __delattr__(self, name):
        ModuleType.__delattr__(self, name)
        if name not in self._PACKAGE_METADATA:
            namespace = ModuleType.__getattribute__(self, "__dict__")
            implementation = namespace.get("_implementation")
            if implementation is not None and hasattr(implementation, name):
                delattr(implementation, name)


sys.modules[__name__].__class__ = _HTTPNamespace
