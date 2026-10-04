"""兼容旧导入路径；实现位于 :mod:`src.desktop.service`。"""
import sys

from .desktop import service as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "service", _implementation)
