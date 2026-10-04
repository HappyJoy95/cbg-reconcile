"""兼容旧导入路径；实现位于 :mod:`src.desktop.runtime`。"""
import sys

from .desktop import runtime as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "runtime", _implementation)
