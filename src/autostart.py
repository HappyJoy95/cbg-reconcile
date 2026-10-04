"""兼容旧导入路径；实现位于 :mod:`src.desktop.autostart`。"""
import sys

from .desktop import autostart as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "autostart", _implementation)
