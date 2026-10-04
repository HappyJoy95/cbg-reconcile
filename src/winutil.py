"""兼容旧导入路径；实现位于 :mod:`src.desktop.winutil`。"""
import sys

from .desktop import winutil as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "winutil", _implementation)
