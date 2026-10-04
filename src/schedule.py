"""兼容旧导入路径；实现位于 :mod:`src.desktop.schedule`。"""
import sys

from .desktop import schedule as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "schedule", _implementation)
